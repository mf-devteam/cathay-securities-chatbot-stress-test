// 聊天 API（POST /api/chat/ask）與結果分類。API 規格見 docs/investigation.md 第 3 節。
import http from 'k6/http';
import { check } from 'k6';
import { Counter, Rate } from 'k6/metrics';
import { BASE_URL, CHANNEL, DEVICE, REQUEST_TIMEOUT, RUN_ID } from './config.js';
import { stageTags } from './stages.js';

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

// 一段對話一個測試身分（訪客）。訪客每日次數以 ip 計算，ip 取 RFC 2544 保留網段 198.18.0.0/15，不與真實使用者重疊
export function newTestUser() {
    const sessionId = generateSessionId();
    return {
        sessionId,
        ip: `198.${18 + Math.floor(Math.random() * 2)}.${Math.floor(Math.random() * 256)}.${1 + Math.floor(Math.random() * 254)}`,
        udid: `k6-${RUN_ID}-${sessionId}`,
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
        channel: CHANNEL,
        recaptchaToken: '', // 壓測期間受測端停用 reCAPTCHA，腳本不做任何繞過
        ip: user.ip,
        udid: user.udid,
        userData: {},
    };
    const res = http.post(`${BASE_URL}/api/chat/ask`, JSON.stringify(body), {
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
    });
    return record(res, tags, scenario.expectStatus);
}
