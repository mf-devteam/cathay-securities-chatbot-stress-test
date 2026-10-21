// 聊天 API（POST /api/chat/ask）與結果分類。API 規格見 docs/investigation.md 第 3 節。
import http from 'k6/http';
import { check } from 'k6';
import { Counter, Rate } from 'k6/metrics';
import { BASE_URL, CHANNEL, DEVICE, REQUEST_TIMEOUT, RUN_ID, SIMULATE_FRONTEND } from './config.js';
import { stageTags } from './stages.js';
import { bubbleRequests, sendBubbles } from './frontend.js';

// ---------- 自訂指標 ----------
export const errors5xxTimeout = new Rate('errors_5xx_timeout'); // 錯誤率：5xx + timeout
export const businessErrors = new Counter('business_errors'); // 業務錯誤另列（非預期的 2xx／4xx）
export const chatSuccess = new Counter('chat_success'); // 成功量（與 http_reqs 送出量並列）
export const http429 = new Counter('http_429');
export const conversationsDone = new Counter('conversations_completed');

// 預期狀態碼：200 一般回應、205 B5 護欄攔截（k6 與報表 Job 都以此判定 expected_response）
http.setResponseCallback(http.expectedStatuses(200, 205));

function generateSessionId() {
    // 與前端相同的格式：tpw + 日時分秒 + 隨機字元
    const now = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    const ts = pad(now.getDate()) + pad(now.getHours()) + pad(now.getMinutes()) + pad(now.getSeconds());
    return 'tpw' + ts + Math.floor(Math.random() * 10) + Math.random().toString(36).substring(2, 9);
}

function generateMsgId() {
    return 'xxxxxxxxxxxx4xxxyxxxxxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
        const r = (Math.random() * 16) | 0;
        return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
    });
}

// 一段對話一個測試身分（訪客）。Web 訪客每日次數以 ip 計算（BFF buildChatLimitKey），ip 取 RFC 2544 保留網段 198.18.0.0/15，不與真實使用者重疊。
// SIMULATE_FRONTEND 時，ip／udid／channel 會改成進站後 session-info 回傳的值（與前端相同；Web 進站的 UDID 為空字串）
export function newTestUser() {
    const sessionId = generateSessionId();
    return {
        sessionId,
        ip: `198.${18 + Math.floor(Math.random() * 2)}.${Math.floor(Math.random() * 256)}.${1 + Math.floor(Math.random() * 254)}`,
        udid: `k6-${RUN_ID}-${sessionId}`,
        channel: CHANNEL,
    };
}

function record(res, tags, expectStatus) {
    const isTimeout = res.status === 0 || res.status === 408 || res.status === 504;
    const is5xx = res.status >= 500;
    errors5xxTimeout.add(is5xx || isTimeout, tags);
    if (res.status === 429) http429.add(1, tags);

    const ok = check(res, { [`HTTP ${expectStatus}`]: (r) => r.status === expectStatus }, tags);
    if (ok) {
        chatSuccess.add(1, tags);
    } else if (!is5xx && !isTimeout && res.status !== 429) {
        businessErrors.add(1, tags);
    }
    return ok;
}

// scenario：lib/scenarios.js 的情境（含 id、flow、expectStatus）
export function ask(user, scenario, userInput, isInitialized) {
    // 情境代號用 case（k6 內建已有 scenario tag，同名會混淆）
    const tags = { flow: scenario.flow, case: scenario.id, ...stageTags(), name: 'POST /api/chat/ask' };
    const body = {
        sessionId: user.sessionId,
        newSessionId: user.sessionId,
        intent: '',
        secondBotIntent: '',
        msgId: generateMsgId(),
        role: 'user',
        userInput,
        device: DEVICE,
        isInitialized,
        channel: user.channel,
        recaptchaToken: '', // 壓測期間受測端停用 reCAPTCHA，腳本不做任何繞過
        ip: user.ip,
        udid: user.udid,
        userData: {},
    };
    const askParams = {
        headers: {
            'Content-Type': 'application/json',
            // 識別壓測流量（受測端 log 篩選用）；mock server 依此決定回應類型
            'X-Load-Test-Run': RUN_ID,
            'X-Load-Test-Case': scenario.id,
            'X-Load-Test-Flow': scenario.flow,
        },
        // Cloud Armor 對非白名單 IP 回 302 導向維護頁，跟隨轉址會變成 HTTP 200 的 HTML 而被誤判成功
        redirects: 0,
        timeout: REQUEST_TIMEOUT,
        tags,
    };
    if (!SIMULATE_FRONTEND) {
        return record(http.post(`${BASE_URL}/api/chat/ask`, JSON.stringify(body), askParams), tags, scenario.expectStatus);
    }

    // 前端送出時就先掛載泡泡：一般訊息為使用者泡泡＋loading 泡泡，開場（silent）只有 loading 泡泡；
    // 回應後 loading 換成 AI 泡泡（每筆 data 一個；錯誤時為一個錯誤泡泡），各自再抓一次回饋選項
    const responses = http.batch([
        { method: 'POST', url: `${BASE_URL}/api/chat/ask`, body: JSON.stringify(body), params: askParams },
        ...bubbleRequests(isInitialized ? 1 : 2),
    ]);
    const res = responses[0];
    let aiBubbles = 1;
    try {
        const data = res.json('data');
        if (Array.isArray(data) && data.length > 0) aiBubbles = data.length;
    } catch (_) {
        // 非 JSON（例如 LB 錯誤頁）：前端顯示一個錯誤泡泡
    }
    const ok = record(res, tags, scenario.expectStatus);
    sendBubbles(aiBubbles);
    return ok;
}
