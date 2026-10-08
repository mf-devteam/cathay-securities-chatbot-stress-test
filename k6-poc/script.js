// 新阿發 /api/chat/ask 輕量試跑（不是壓力測試）
// 規格與限制見 ../Claude_Handover.md、調查結果見 INVESTIGATION.md
import http from 'k6/http';
import { check } from 'k6';
import { Counter } from 'k6/metrics';
import { SharedArray } from 'k6/data';
import exec from 'k6/execution';

const BASE_URL = (__ENV.BASE_URL || '').replace(/\/+$/, '');
if (!BASE_URL) {
    throw new Error('請設定環境變數 BASE_URL，例如 BASE_URL=http://<LB 位址>');
}
// reCAPTCHA token：k6 無法以正常方式產生，預設送空字串（BFF 會回 403 E1010）
const RECAPTCHA_TOKEN = __ENV.RECAPTCHA_TOKEN || '';
const CHANNEL = __ENV.CHANNEL || 'web';
// 每個測試身分最多問幾則就換新身分＋新對話。lab 訪客每日上限 4（maxCountGuest），留 1 則餘裕
const MAX_QUESTIONS_PER_USER = parseInt(__ENV.MAX_QUESTIONS_PER_USER || '3', 10);

const questions = new SharedArray('questions', () => JSON.parse(open('./data/questions.json')));

const http429 = new Counter('http_429');

export const options = {
    scenarios: {
        faq: {
            executor: 'constant-arrival-rate',
            rate: 1,
            timeUnit: '10s',
            duration: '3m',
            preAllocatedVUs: 2,
            maxVUs: 5,
            tags: { flow: 'faq' },
        },
    },
    // 不跟隨轉址：Cloud Armor 對非白名單 IP 回 302 導向維護頁，跟隨後會變成 HTTP 200 的 HTML
    maxRedirects: 0,
    summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)'],
    thresholds: {
        'http_req_failed{flow:faq}': [{ threshold: 'rate<0.2', abortOnFail: true }],
        http_429: [{ threshold: 'count<1', abortOnFail: true }],
        // 回應時間只觀察、不中止；列出來是為了讓報表依 flow／step 分開顯示
        'http_req_duration{flow:faq}': ['max>=0'],
        'http_req_duration{flow:faq,step:init}': ['max>=0'],
        'http_req_duration{flow:faq,step:ask}': ['max>=0'],
    },
};

// 只有 HTTP 200 算成功：BFF 有些錯誤用 205／206 回，不能被當成成功
http.setResponseCallback(http.expectedStatuses(200));

// 與前端 src/utils/generateSessionId.ts 相同格式：tpw + 日時分秒 + 1 位數字 + 7 碼亂數
function generateSessionId() {
    const now = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    const ts = pad(now.getDate()) + pad(now.getHours()) + pad(now.getMinutes()) + pad(now.getSeconds());
    return 'tpw' + ts + Math.floor(Math.random() * 10) + Math.random().toString(36).substring(2, 9);
}

// 與前端相同：UUID 去掉 '-'
function generateMsgId() {
    return 'xxxxxxxxxxxx4xxxyxxxxxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
        const r = (Math.random() * 16) | 0;
        return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
    });
}

// 測試身分：ip 用 RFC 2544 網路效能測試保留網段 198.18.0.0/15，不會與真實使用者重疊；
// 訪客＋Web 的每日次數以 ip 計算，每個對話一組新身分（方案 A：模擬多位使用者各自發問）
function newTestUser() {
    const ip = `198.${18 + Math.floor(Math.random() * 2)}.${Math.floor(Math.random() * 256)}.${1 + Math.floor(Math.random() * 254)}`;
    const sessionId = generateSessionId();
    return { ip, udid: `k6-test-${sessionId}`, sessionId, asked: 0 };
}

function ask(user, userInput, isInitialized) {
    const step = isInitialized ? 'init' : 'ask';
    const body = {
        sessionId: user.sessionId,
        newSessionId: user.sessionId,
        intent: '',
        secondBotIntent: '',
        msgId: generateMsgId(),
        role: 'user',
        userInput,
        device: 'Web',
        isInitialized,
        channel: CHANNEL,
        recaptchaToken: RECAPTCHA_TOKEN,
        ip: user.ip,
        udid: user.udid,
        userData: {},
    };
    const res = http.post(`${BASE_URL}/api/chat/ask`, JSON.stringify(body), {
        headers: { 'Content-Type': 'application/json' },
        timeout: '60s',
        tags: { flow: 'faq', step, name: 'POST /api/chat/ask' },
    });

    if (res.status === 429) {
        http429.add(1, { flow: 'faq', step });
    }

    let json = null;
    try {
        json = res.json();
    } catch (_) {
        // 非 JSON（例如被 Cloud Armor 導向維護頁），交給下面的 check 判定失敗
    }
    const data = json && Array.isArray(json.data) ? json.data : [];
    const faqItem = data.find((item) => item.mode === 'FAQ');

    const checks = {
        'HTTP 200': (r) => r.status === 200,
        'body.status === 200': () => json !== null && json.status === 200,
        'data 非空': () => data.length > 0,
    };
    if (!isInitialized) {
        checks['FAQ content 非空'] = () => !!faqItem && typeof faqItem.content === 'string' && faqItem.content.trim() !== '';
    }
    const ok = check(res, checks, { flow: 'faq', step });

    if (!ok) {
        const errorCode = json && json.errorCode ? json.errorCode : '-';
        console.warn(`[${step}] HTTP ${res.status} errorCode=${errorCode} duration=${res.timings.duration.toFixed(0)}ms`);
    }
    return ok;
}

// 每個 VU 各自持有一個對話；arrival-rate 每次 iteration 只送 1 個 /ask，
// 所以「開啟對話（開場問候）」與「送出訊息」各佔一次 iteration，總請求數維持在每 10 秒 1 次
let user = null;

export default function () {
    if (!user || user.asked >= MAX_QUESTIONS_PER_USER) {
        user = newTestUser();
        // 開啟對話：前端進入聊天頁會自動送一次 isInitialized=true 的「你好」（不計入每日次數）
        ask(user, '你好', true);
        return;
    }
    const q = questions[exec.scenario.iterationInTest % questions.length];
    ask(user, q, false);
    user.asked += 1;
}

export function handleSummary(data) {
    const m = data.metrics;
    const val = (name, key) => (m[name] && m[name].values[key] !== undefined ? m[name].values[key] : null);
    const sec = (ms) => (ms === null ? '-' : (ms / 1000).toFixed(2) + 's');
    const durKey = 'http_req_duration{flow:faq}';

    const total = val('http_reqs', 'count') || 0;
    const failRate = val('http_req_failed{flow:faq}', 'rate');
    const failed = val('http_req_failed{flow:faq}', 'passes') || 0;

    const lines = [
        '',
        '===== 新阿發 /ask 試跑摘要 =====',
        `/ask 呼叫次數 : ${total}（成功 ${total - failed}／失敗 ${failed}）`,
        `錯誤率        : ${failRate === null ? '-' : (failRate * 100).toFixed(1) + '%'}`,
        `HTTP 429      : ${val('http_429', 'count') || 0}`,
        `每秒處理量    : ${(val('http_reqs', 'rate') || 0).toFixed(3)} req/s`,
        `回應時間(全部): avg ${sec(val(durKey, 'avg'))}  min ${sec(val(durKey, 'min'))}  p50 ${sec(val(durKey, 'med'))}  ` +
            `p90 ${sec(val(durKey, 'p(90)'))}  p95 ${sec(val(durKey, 'p(95)'))}  p99 ${sec(val(durKey, 'p(99)'))}  max ${sec(val(durKey, 'max'))}`,
    ];
    for (const step of ['init', 'ask']) {
        const k = `http_req_duration{flow:faq,step:${step}}`;
        lines.push(`回應時間(${step.padEnd(4)}): avg ${sec(val(k, 'avg'))}  p50 ${sec(val(k, 'med'))}  p95 ${sec(val(k, 'p(95)'))}  max ${sec(val(k, 'max'))}`);
    }
    lines.push('================================', '');

    return {
        stdout: lines.join('\n'),
        'results/summary.json': JSON.stringify(data, null, 2),
    };
}
