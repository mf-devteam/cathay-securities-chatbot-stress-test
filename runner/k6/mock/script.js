// 新阿發 壓力測試規劃 v1.4 的 k6 腳本骨架（mock 版）
// 只打本機 mock-server.js，目的是預覽 k6 Web Dashboard，數值不代表任何真實環境。
//
// 對應規劃：
//   - 負載以「每秒新開對話數」計，每個 iteration = 一位使用者跑完整個多輪對話
//     （（登入）→ 開場問候 → 8～11 則訊息，每則間隔約 20 秒思考時間）
//   - 逐階加壓：Smoke 1% → 5% → 10% → Baseline 25% → 50% → 1 倍 → 2 倍 → 3 倍 → Recovery 25%，每階爬升 3 分鐘
//   - 業務情境分配（佔全部訊息）：FAQ 問答 10%、業務查詢 90%，三種流量組合（MIX）
//   - 指標：FAQ 問答／業務查詢分開統計；錯誤率 = 5xx + timeout，業務錯誤另列
import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter, Rate } from 'k6/metrics';
import { SharedArray } from 'k6/data';
import exec from 'k6/execution';

// ---------- 參數（環境變數，執行時可覆寫） ----------
const BASE_URL = (__ENV.BASE_URL || 'http://127.0.0.1:8090').replace(/\/+$/, '');
// 防呆：mock 腳本只允許打本機，避免誤打 SIT／UAT
if (!/^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(BASE_URL)) {
    throw new Error(`mock 腳本只允許 BASE_URL 指向本機，收到：${BASE_URL}`);
}
// mock 的「1 倍」每秒新開對話數。真實 1 倍為 118（證券方 10/8），本機預設縮小 100 倍
const BASE_CONV_RATE = Number(__ENV.BASE_CONV_RATE || 1.18);
// 時間壓縮倍率：規劃一輪約 3.6 小時，預設 ÷15 約 14.5 分鐘看完整個加壓曲線
const TIME_SCALE = Number(__ENV.TIME_SCALE || 15);
// STAGES：full（完整逐階加壓）／smoke（只跑 Smoke）
const STAGES = __ENV.STAGES || 'full';
// MIX：baseline（營運基準 10／90）／business（100% 業務查詢）／faq（100% FAQ 問答）
const MIX = __ENV.MIX || 'baseline';
const THINK_TIME_S = Number(__ENV.THINK_TIME_S || 20);
const MSGS_MIN = Number(__ENV.MSGS_MIN || 8);
const MSGS_MAX = Number(__ENV.MSGS_MAX || 11);
// 對話中登入的比例：規劃待確認，mock 預設 20%（假設值）；登入中 10% 需 OTP 重送
const LOGIN_RATIO = Number(__ENV.LOGIN_RATIO || 0.2);
const OTP_RESEND_RATIO = Number(__ENV.OTP_RESEND_RATIO || 0.1);
const MAX_VUS = Number(__ENV.MAX_VUS || 1500);
const ABORT = (__ENV.ABORT || 'true') === 'true';
const ABORT_DELAY = __ENV.ABORT_DELAY || '3m';
// handleSummary 的輸出目錄（多個 k6 同時跑時各自指定，避免互相覆蓋）
const RESULTS_DIR = (__ENV.RESULTS_DIR || 'results').replace(/\/+$/, '');

// ---------- 逐階加壓（規劃 p.9） ----------
const PLAN_STAGES = [
    { id: 'smoke', name: 'Smoke', pct: 0.01, holdMin: 10 },
    { id: 'low5', name: '低量 5%', pct: 0.05, holdMin: 15 },
    { id: 'low10', name: '低量 10%', pct: 0.1, holdMin: 15 },
    { id: 'baseline', name: 'Baseline', pct: 0.25, holdMin: 15 },
    { id: 'mid50', name: '中量', pct: 0.5, holdMin: 15 },
    { id: 'x1', name: '1 倍', pct: 1, holdMin: 30 },
    { id: 'x2', name: '2 倍', pct: 2, holdMin: 15 },
    { id: 'x3', name: '3 倍', pct: 3, holdMin: 60 },
    { id: 'recovery', name: 'Recovery', pct: 0.25, holdMin: 15 },
];
const RAMP_MIN = 3;

function scaledSeconds(m) {
    return Math.max(1, Math.round((m * 60) / TIME_SCALE));
}

function minutes(m) {
    return `${scaledSeconds(m)}s`;
}

// ramping-arrival-rate 的 target 需為整數，以「每小時新開對話數」表示
function convPerHour(pct) {
    return Math.max(1, Math.round(pct * BASE_CONV_RATE * 3600));
}

const ACTIVE_STAGES = STAGES === 'smoke' ? PLAN_STAGES.slice(0, 1) : PLAN_STAGES;

// 預先建立的 VU 數：每段對話要跑數分鐘，VU 臨時才建立會來不及而丟掉新對話（dropped_iterations），
// 預設依「最高每秒對話數 × 每段對話平均秒數 × 1.2」估算；PRE_VUS 可覆寫
// 每段對話平均秒數 ≈ 開場（約 10 秒）＋ 平均訊息數 ×（思考時間 ＋ 平均回應約 5 秒）
const EST_CONV_SECONDS = 10 + ((MSGS_MIN + MSGS_MAX) / 2) * (THINK_TIME_S + 5);
const PEAK_CONV_RATE = Math.max(...ACTIVE_STAGES.map((s) => s.pct)) * BASE_CONV_RATE;
const PRE_VUS = Math.min(MAX_VUS, Number(__ENV.PRE_VUS || Math.ceil(PEAK_CONV_RATE * EST_CONV_SECONDS * 1.2)));

// 每段（爬升／持續）的結束秒數，用來替每個請求標上 stage／phase tag，報表才能分階段統計
const STAGE_SCHEDULE = (() => {
    const schedule = [];
    let t = 0;
    for (const s of ACTIVE_STAGES) {
        t += scaledSeconds(RAMP_MIN);
        schedule.push({ stage: s.id, phase: 'ramp', endSec: t });
        t += scaledSeconds(s.holdMin);
        schedule.push({ stage: s.id, phase: 'hold', endSec: t });
    }
    return schedule;
})();

function currentStage() {
    const elapsed = exec.instance.currentTestRunDuration / 1000;
    for (const seg of STAGE_SCHEDULE) {
        if (elapsed < seg.endSec) return seg;
    }
    return { stage: 'drain', phase: 'drain' }; // 階段結束後、gracefulStop 期間仍在跑的對話
}

function buildStages() {
    const plan = ACTIVE_STAGES;
    const stages = [];
    for (const s of plan) {
        stages.push({ target: convPerHour(s.pct), duration: minutes(RAMP_MIN) });
        stages.push({ target: convPerHour(s.pct), duration: minutes(s.holdMin) });
    }
    return stages;
}

// ---------- 業務情境分配（規劃 p.11，佔全部訊息 %） ----------
const SCENARIOS = [
    { id: 'B2', name: 'FAQ 單輪', flow: 'faq', weight: 6.2, scope: 'included' },
    { id: 'B3', name: 'FAQ 多輪', flow: 'faq', weight: 2.6, scope: 'included' },
    { id: 'B4', name: '知識庫外', flow: 'faq', weight: 0.9, scope: 'included' },
    { id: 'B5', name: 'Model Armor 攔截', flow: 'faq', weight: 0.3, scope: 'included', expectStatus: 205 },
    { id: 'C1', name: '查 ETF 清單', flow: 'business', weight: 27.0, scope: 'included' },
    { id: 'C6', name: '查個人資料', flow: 'business', weight: 18.0, scope: 'included' },
    { id: 'C2', name: 'ETF 申購', flow: 'business', weight: 11.3, scope: 'conditional' },
    { id: 'C4', name: 'KYC 過期', flow: 'business', weight: 2.3, scope: 'conditional' },
    { id: 'C5', name: 'KYC 評估', flow: 'business', weight: 4.5, scope: 'conditional' },
    { id: 'A5', name: '密碼補發', flow: 'business', weight: 4.5, scope: 'conditional' },
    { id: 'A6', name: '密碼變更', flow: 'business', weight: 6.8, scope: 'conditional' },
    { id: 'C7', name: '修改資料', flow: 'business', weight: 6.8, scope: 'conditional' },
    { id: 'D1', name: '未滿 18 歲', flow: 'business', weight: 2.3, scope: 'conditional' },
    { id: 'D2', name: '未滿 18 歲', flow: 'business', weight: 2.3, scope: 'conditional' },
    { id: 'D3', name: '65 歲以上', flow: 'business', weight: 4.5, scope: 'conditional' },
];

const ACTIVE_SCENARIOS = SCENARIOS.filter((s) => MIX === 'baseline' || s.flow === MIX);
if (ACTIVE_SCENARIOS.length === 0) {
    throw new Error(`MIX 只能是 baseline／business／faq，收到：${MIX}`);
}
const TOTAL_WEIGHT = ACTIVE_SCENARIOS.reduce((sum, s) => sum + s.weight, 0);

function pickScenario() {
    let r = Math.random() * TOTAL_WEIGHT;
    for (const s of ACTIVE_SCENARIOS) {
        r -= s.weight;
        if (r <= 0) return s;
    }
    return ACTIVE_SCENARIOS[ACTIVE_SCENARIOS.length - 1];
}

const questions = new SharedArray('questions', () => {
    const q = JSON.parse(open('./data/questions.json'));
    return [q];
});

function questionFor(scenarioId) {
    const list = questions[0][scenarioId] || questions[0].default;
    return list[Math.floor(Math.random() * list.length)];
}

// ---------- 自訂指標 ----------
const errors5xxTimeout = new Rate('errors_5xx_timeout'); // 錯誤率：5xx + timeout
const businessErrors = new Counter('business_errors'); // 業務錯誤另列（非預期的 2xx／4xx）
const chatSuccess = new Counter('chat_success'); // 成功量（與 http_reqs 送出量並列）
const http429 = new Counter('http_429');
const conversationsDone = new Counter('conversations_completed');

// ---------- k6 options ----------
const abortRule = (threshold) => ({ threshold, abortOnFail: ABORT, delayAbortEval: ABORT_DELAY });

export const options = {
    scenarios: {
        conversation: {
            executor: 'ramping-arrival-rate',
            startRate: 0,
            timeUnit: '1h',
            preAllocatedVUs: PRE_VUS,
            maxVUs: MAX_VUS,
            stages: buildStages(),
            gracefulStop: '60s',
        },
    },
    summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)'],
    thresholds: {
        // 證券方文件參考值（正式門檻於 Baseline 後共同確認）；p99 > 門檻 2 倍為停止條件
        'http_req_duration{flow:faq}': ['p(95)<10000', 'p(99)<20000', abortRule('p(99)<40000')],
        'http_req_duration{flow:business}': ['p(95)<5000', 'p(99)<10000', abortRule('p(99)<20000')],
        // 只觀察：開場問候、登入
        'http_req_duration{flow:init}': ['max>=0'],
        'http_req_duration{flow:login}': ['max>=0'],
        // 錯誤率 ≤ 0.5%（參考值）；> 5% 為停止條件
        errors_5xx_timeout: ['rate<=0.005', abortRule('rate<0.05')],
        'errors_5xx_timeout{flow:faq}': ['rate<=0.005'],
        'errors_5xx_timeout{flow:business}': ['rate<=0.005'],
        // HTTP 429（次數上限）：立即停止
        http_429: [{ threshold: 'count<1', abortOnFail: ABORT }],
        // 送出量與成功量並列（只觀察）
        'http_reqs{flow:faq}': ['count>=0'],
        'http_reqs{flow:business}': ['count>=0'],
        'http_reqs{flow:init}': ['count>=0'],
        'http_reqs{flow:login}': ['count>=0'],
        'errors_5xx_timeout{flow:init}': ['rate>=0'],
        'errors_5xx_timeout{flow:login}': ['rate>=0'],
        'chat_success{flow:faq}': ['count>=0'],
        'chat_success{flow:business}': ['count>=0'],
    },
};

http.setResponseCallback(http.expectedStatuses(200, 205));

// ---------- 請求 ----------
function generateSessionId() {
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

function newTestUser() {
    const sessionId = generateSessionId();
    return {
        sessionId,
        ip: `198.${18 + Math.floor(Math.random() * 2)}.${Math.floor(Math.random() * 256)}.${1 + Math.floor(Math.random() * 254)}`,
        udid: `k6-mock-${sessionId}`,
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

function stageTags() {
    const seg = currentStage();
    return { stage: seg.stage, phase: seg.phase };
}

function ask(user, scenario, userInput, isInitialized) {
    // 情境代號用 case（k6 內建已有 scenario tag，同名會混淆）
    const tags = {
        flow: isInitialized ? 'init' : scenario.flow,
        case: scenario.id,
        scope: scenario.scope,
        ...stageTags(),
        name: 'POST /api/chat/ask',
    };
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
        channel: 'web',
        recaptchaToken: '',
        ip: user.ip,
        udid: user.udid,
        userData: {},
    };
    const res = http.post(`${BASE_URL}/api/chat/ask`, JSON.stringify(body), {
        headers: { 'Content-Type': 'application/json', 'X-Mock-Scenario': scenario.id },
        timeout: '60s',
        tags,
    });
    return record(res, tags, scenario.expectStatus || 200);
}

function login() {
    const tags = { flow: 'login', case: 'A1', scope: 'included', ...stageTags(), name: 'POST /mock/login' };
    const res = http.post(`${BASE_URL}/mock/login`, '{}', { headers: { 'Content-Type': 'application/json' }, timeout: '60s', tags });
    record(res, tags, 200);
    if (Math.random() < OTP_RESEND_RATIO) {
        const otpTags = { flow: 'login', case: 'A3', scope: 'included', ...stageTags(), name: 'POST /mock/otp/resend' };
        const otpRes = http.post(`${BASE_URL}/mock/otp/resend`, '{}', { headers: { 'Content-Type': 'application/json' }, timeout: '60s', tags: otpTags });
        record(otpRes, otpTags, 200);
    }
}

function thinkTime() {
    // 平均 THINK_TIME_S，±25% 隨機
    return THINK_TIME_S * (0.75 + Math.random() * 0.5);
}

const INIT = { id: 'INIT', flow: 'init', scope: 'included' };

// 一次 iteration = 一位使用者的完整多輪對話
export default function () {
    const user = newTestUser();
    if (Math.random() < LOGIN_RATIO) login();

    ask(user, INIT, '你好', true);

    const messages = MSGS_MIN + Math.floor(Math.random() * (MSGS_MAX - MSGS_MIN + 1));
    for (let i = 0; i < messages; i++) {
        sleep(thinkTime());
        const scenario = pickScenario();
        ask(user, scenario, questionFor(scenario.id), false);
    }
    conversationsDone.add(1);
}

// ---------- 摘要 ----------
export function handleSummary(data) {
    const m = data.metrics;
    const v = (name, key) => (m[name] && m[name].values[key] !== undefined ? m[name].values[key] : null);
    const sec = (ms) => (ms === null ? '    -' : (ms / 1000).toFixed(2).padStart(6) + 's');
    const pct = (r) => (r === null ? '-' : (r * 100).toFixed(2) + '%');
    const dur = data.state.testRunDurationMs / 1000;

    const lines = [
        '',
        `===== 新阿發壓測（mock）摘要｜MIX=${MIX} STAGES=${STAGES} 1倍=${BASE_CONV_RATE} 對話/秒 時間÷${TIME_SCALE} =====`,
        `完成對話數   : ${v('conversations_completed', 'count') || 0}`,
        `送出／成功   : ${v('http_reqs', 'count') || 0} ／ ${v('chat_success', 'count') || 0}（${((v('http_reqs', 'count') || 0) / dur).toFixed(2)} req/s）`,
        `錯誤率       : ${pct(v('errors_5xx_timeout', 'rate'))}（5xx + timeout）  業務錯誤 ${v('business_errors', 'count') || 0}  HTTP 429 ${v('http_429', 'count') || 0}`,
        '',
        '類別        請求數      avg      p50      p95      p99      max   錯誤率',
    ];
    for (const flow of ['faq', 'business', 'init', 'login']) {
        const k = `http_req_duration{flow:${flow}}`;
        if (!m[k]) continue;
        const count = v(`http_reqs{flow:${flow}}`, 'count');
        const err = v(`errors_5xx_timeout{flow:${flow}}`, 'rate');
        lines.push(
            `${flow.padEnd(10)} ${String(count === null ? '-' : count).padStart(6)} ${sec(v(k, 'avg'))} ${sec(v(k, 'med'))} ${sec(v(k, 'p(95)'))} ${sec(v(k, 'p(99)'))} ${sec(v(k, 'max'))}   ${pct(err)}`
        );
    }
    lines.push('', '門檻判定（參考值）：');
    for (const [name, metric] of Object.entries(m)) {
        if (!metric.thresholds) continue;
        for (const [expr, res] of Object.entries(metric.thresholds)) {
            if (/>=0$/.test(expr)) continue; // 只為了讓子指標出現在報表，不是門檻
            lines.push(`  ${res.ok ? '✓' : '✗'} ${name} ${expr}`);
        }
    }
    lines.push('');

    return {
        stdout: lines.join('\n'),
        [`${RESULTS_DIR}/summary.json`]: JSON.stringify(data, null, 2),
    };
}
