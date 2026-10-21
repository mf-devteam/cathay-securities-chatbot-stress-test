// 模擬前端（cathay-securities-chatbot-frontend）在聊天 API 以外送出的請求，讓 BFF／Middleware／Redis 的負載與真實訪客相同。
// 依據前端與 BFF 程式（2026-10-20）：
//   進站   GET /api/redirect?Channel=web&isWeb=1 → 302 /#/?sid=…（BFF 把進站參數與 x-forwarded-for 的 IP 存進 Redis）
//   載入   同時送出：維護檢查、session-info（換回 ip／UDID／Channel）、招呼語、3 個 Drawer 的 dynamicType、reCAPTCHA siteKey
//   泡泡   每個訊息泡泡（使用者、loading、AI）掛載時 POST get-dynamic-type typeId 3（回饋選項）
// 這些請求的 flow tag 為 entry／page／bubble，報表另外統計，不計入聊天 API 的 TPS。
import http from 'k6/http';
import { Counter } from 'k6/metrics';
import { BASE_URL, CHANNEL, FRONTEND_URL, REQUEST_TIMEOUT, RUN_ID } from './config.js';
import { stageTags } from './stages.js';

// 進站失敗（沒拿到 sid 或 session-info 失敗）：該段對話改用腳本自己的測試身分繼續
export const entryFailed = new Counter('entry_failed');

const JSON_HEADERS = { 'Content-Type': 'application/json', 'X-Load-Test-Run': RUN_ID };

function params(flow, name, extra = {}) {
    return {
        headers: { ...JSON_HEADERS, ...(extra.headers || {}) },
        redirects: 0,
        timeout: REQUEST_TIMEOUT,
        tags: { flow, case: flow, ...stageTags(), name },
        ...(extra.responseCallback ? { responseCallback: extra.responseCallback } : {}),
    };
}

function dynamicType(typeId, flow) {
    return {
        method: 'POST',
        url: `${BASE_URL}/api/dynamicType/get-dynamic-type`,
        body: JSON.stringify({ typeId }),
        params: params(flow, `POST /api/dynamicType/get-dynamic-type typeId=${typeId}`),
    };
}

// 進站＋載入頁面。成功時以 session-info 回傳的 ip／UDID／Channel 更新 user（與前端相同，/ask 用這些值）
export function enterSite(user) {
    // 真實的 ip 由 BFF 從 x-forwarded-for 第一個值取得。壓測流量都從同一個 NAT IP 出去，所以帶入測試身分的 ip，
    // 讓每位訪客的每日次數分開計算（與真實使用者各自有 IP 相同）
    const redirect = http.get(
        `${BASE_URL}/api/redirect?Channel=${encodeURIComponent(CHANNEL)}&isWeb=1`,
        params('entry', 'GET /api/redirect', { headers: { 'X-Forwarded-For': user.ip }, responseCallback: http.expectedStatuses(302) })
    );
    const m = /[?&]sid=([0-9a-fA-F-]+)/.exec(String(redirect.headers.Location || redirect.headers.location || ''));
    const sid = m ? m[1] : null;

    const requests = [
        { method: 'GET', url: `${BASE_URL}/api/maintenance/check?check=status`, params: params('page', 'GET /api/maintenance/check') },
        { method: 'GET', url: `${BASE_URL}/api/dynamicType/intro`, params: params('page', 'GET /api/dynamicType/intro') },
        dynamicType(2, 'page'), // NoticeDrawer
        dynamicType(6, 'page'), // AIDeclarationDrawer
        dynamicType(7, 'page'), // PersonalDataNoticeDrawer
        { method: 'GET', url: `${BASE_URL}/api/recaptcha/get-site-key`, params: params('page', 'GET /api/recaptcha/get-site-key') },
    ];
    if (FRONTEND_URL) requests.push({ method: 'GET', url: FRONTEND_URL, params: params('entry', 'GET 前端頁面') });
    if (sid) requests.push({ method: 'GET', url: `${BASE_URL}/api/user/session-info/${sid}`, params: params('entry', 'GET /api/user/session-info/:sid') });

    const responses = http.batch(requests);
    if (!sid) return false;
    const info = responses[responses.length - 1];
    if (info.status !== 200) return false;
    try {
        const data = info.json('data') || {};
        if (data.ip) user.ip = data.ip;
        user.udid = data.UDID !== undefined ? data.UDID : user.udid;
        if (data.Channel) user.channel = data.Channel;
    } catch (_) {
        return false;
    }
    return true;
}

// 訊息泡泡掛載時的回饋選項請求（typeId 3）
export function bubbleRequests(count) {
    const out = [];
    for (let i = 0; i < count; i++) out.push(dynamicType(3, 'bubble'));
    return out;
}

export function sendBubbles(count) {
    if (count > 0) http.batch(bubbleRequests(count));
}
