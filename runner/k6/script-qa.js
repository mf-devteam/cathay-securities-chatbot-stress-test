// 新阿發 對話回應時間測試（客戶 2026-11-12 新增）：以「一問一答」為單位看回應時間與失敗率。
// 與正式腳本 script.js 的差異：
//   - 一次 iteration ＝ 一則「對話」（一問一答＝一次 /api/chat/ask），負載直接以聊天 API 每秒請求數（TPS）控制，
//     STAGE_PLAN 的爬升分鐘就是實際 TPS 爬升的時間（script.js 是每秒新開對話數，TPS 要等對話累積才會到）
//   - 訪客仍是真實的對話流程：每個 VU 扮演一位訪客，先開場問候（B1），再依權重問 8～11 則，問完換下一位訪客；
//     不另外 sleep 思考時間（間隔由每秒請求數決定）
//   - 對話失敗＝回應不是預期結果（5xx、timeout、HTTP 429、業務錯誤，例如 B5 沒回 205），失敗後照樣繼續問
//   - 失敗率＝對話失敗筆數 ÷ 對話總筆數（qa_failed）；回應時間門檻 FAQ 8 秒、業務查詢 2 秒（QA_* 參數）
//   - 開場問候照樣送出（真實訪客流程），但不計入對話統計（客戶 2026-11-12 回覆：只驗證 AI 處理使用者提問的能力），
//     另記在 qa_opening_failed 單獨列示
//   - 預設不自動停止，一律打完（客戶回覆；QA_ABORT=true 才在失敗率 > ABORT_ERROR_RATE 時停止）
//   - TPS 指聊天 API 每秒請求數（含開場問候，與地端規劃的 TPS 定義相同）
// 情境、問句、請求內容與判定都沿用 lib/（chat.js 的 ask() 回傳是否符合預期），不改正式腳本。
// 負載用 STAGE_PLAN（例如 x3:354tps:10:1＝1 分鐘爬升到 354 TPS、維持 10 分鐘）；地端預設值見 onprem/config/defaults.env 的 QA_STAGE_PLAN。
import { SharedArray } from 'k6/data';
import { Rate } from 'k6/metrics';
import {
    ABORT_DELAY,
    ABORT_ERROR_RATE,
    BASE_CONV_RATE,
    CALLS_PER_CONV,
    GRACEFUL_STOP,
    MSGS_MAX,
    MSGS_MIN,
    RESULTS_DIR,
    RUN_ID,
    SIMULATE_FRONTEND,
    STAGE_SCOPE,
    THRESHOLDS,
    TIME_SCALE,
} from './lib/config.js';
import { ACTIVE_STAGES, TOTAL_SECONDS, stageMarker, stageTags } from './lib/stages.js';
import { ACTIVE, OPENING, SHARES, pickScenario } from './lib/scenarios.js';
import { ask, newTestUser } from './lib/chat.js';
import { enterSite, entryFailed } from './lib/frontend.js';

export { stageMarker };

const raw = (name, def) => (__ENV[name] !== undefined && __ENV[name] !== '' ? __ENV[name] : def);
function num(name, def) {
    const v = Number(raw(name, def));
    if (!Number.isFinite(v)) throw new Error(`${name} 必須是數字，收到：${__ENV[name]}`);
    return v;
}

// ---------- 參數（與報表 Job 共用同名環境變數） ----------
const QA_FAQ_MAX_MS = num('QA_FAQ_MAX_MS', 8000); // FAQ（含 AI 生成時間）
const QA_BUSINESS_MAX_MS = num('QA_BUSINESS_MAX_MS', 2000); // 一般業務查詢
const QA_LATENCY_PCT = num('QA_LATENCY_PCT', 95); // 以第幾百分位判定回應時間
const QA_FAIL_RATE = num('QA_FAIL_RATE', THRESHOLDS.error_rate); // 失敗率參考門檻
const QA_ABORT = String(raw('QA_ABORT', 'false')) === 'true'; // 預設一律打完
// VU：每秒請求數 × 每個 iteration 的時間（回應時間＋前端泡泡請求；回應變慢時需要更多 VU）
const PEAK_TPS = Math.max(...ACTIVE_STAGES.map((s) => s.pct * BASE_CONV_RATE * CALLS_PER_CONV));
const QA_PRE_VUS = Math.max(1, Math.ceil(num('QA_PRE_VUS', PEAK_TPS * 20)));
const QA_MAX_VUS = Math.max(QA_PRE_VUS, Math.ceil(num('QA_MAX_VUS', PEAK_TPS * 30)));
if (!(QA_LATENCY_PCT > 0 && QA_LATENCY_PCT < 100)) throw new Error('QA_LATENCY_PCT 必須介於 0～100');

const questions = new SharedArray('questions', () => [JSON.parse(open('./data/questions.json'))]);
for (const id of [OPENING.id, ...ACTIVE.map((s) => s.id)]) {
    if (!Array.isArray(questions[0][id]) || questions[0][id].length === 0) throw new Error(`data/questions.json 缺少情境 ${id} 的問句`);
}
function questionFor(id) {
    const list = questions[0][id];
    return list[Math.floor(Math.random() * list.length)];
}

// 對話失敗（1）／成功（0），tag：flow、case、stage、phase。開場問候另記，不計入對話統計
const qaFailed = new Rate('qa_failed');
const qaOpeningFailed = new Rate('qa_opening_failed');

// ---------- k6 options ----------
// ramping-arrival-rate 的 target 須為整數：以「每小時請求數」表示
const reqPerHour = (s) => Math.max(1, Math.round(s.pct * BASE_CONV_RATE * CALLS_PER_CONV * 3600));
const scaledSeconds = (min) => Math.max(1, Math.round((min * 60) / TIME_SCALE));
function buildStages() {
    const out = [];
    for (const s of ACTIVE_STAGES) {
        out.push({ target: reqPerHour(s), duration: `${s.rampMin > 0 ? scaledSeconds(s.rampMin) : 1}s` });
        out.push({ target: reqPerHour(s), duration: `${scaledSeconds(s.holdMin)}s` });
    }
    return out;
}

const pct = `p(${QA_LATENCY_PCT})`;
const thresholds = {
    // 回應時間只判定、不中止（客戶要求固定負載打完為止）
    'http_req_duration{flow:faq}': [`${pct}<${QA_FAQ_MAX_MS}`],
    'http_req_duration{flow:business}': [`${pct}<${QA_BUSINESS_MAX_MS}`],
    'http_req_duration{flow:init}': ['max>=0'],
    // 失敗率（不含開場問候）；QA_ABORT=true 時 > ABORT_ERROR_RATE 停止
    qa_failed: [`rate<=${QA_FAIL_RATE}`].concat(
        QA_ABORT ? [{ threshold: `rate<${ABORT_ERROR_RATE}`, abortOnFail: true, delayAbortEval: ABORT_DELAY }] : []
    ),
    'qa_failed{flow:faq}': [`rate<=${QA_FAIL_RATE}`],
    'qa_failed{flow:business}': [`rate<=${QA_FAIL_RATE}`],
    qa_opening_failed: ['rate>=0'],
    // 只為了讓子指標出現在摘要
    'http_reqs{flow:faq}': ['count>=0'],
    'http_reqs{flow:business}': ['count>=0'],
    'http_reqs{flow:init}': ['count>=0'],
    entry_failed: ['count>=0'],
};

export const options = {
    scenarios: {
        qa: {
            executor: 'ramping-arrival-rate',
            startRate: 0,
            timeUnit: '1h',
            preAllocatedVUs: QA_PRE_VUS,
            maxVUs: QA_MAX_VUS,
            stages: buildStages(),
            gracefulStop: GRACEFUL_STOP,
        },
        stage_marker: {
            executor: 'per-vu-iterations',
            vus: 1,
            iterations: 1,
            maxDuration: `${TOTAL_SECONDS + 60}s`,
            gracefulStop: '0s',
            exec: 'stageMarker',
        },
    },
    summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)'],
    thresholds,
};

export function setup() {
    console.log(
        `[k6] 對話回應時間測試 RUN_ID=${RUN_ID} 最高 ${PEAK_TPS.toFixed(1)} TPS 階段=${ACTIVE_STAGES.map((s) => s.id).join('→')} ` +
            `時間÷${TIME_SCALE} 門檻 FAQ ${pct}<${QA_FAQ_MAX_MS}ms 業務 ${pct}<${QA_BUSINESS_MAX_MS}ms 失敗率≤${QA_FAIL_RATE} 自動停止=${QA_ABORT} ` +
            `情境占比 ${SHARES} VU=${QA_PRE_VUS}/${QA_MAX_VUS}（全部 task 合計） 模擬前端=${SIMULATE_FRONTEND}`
    );
}

// 每個 VU 一位訪客：問完這位訪客的訊息數後換下一位（VU 內的變數會跨 iteration 保留）
let user = null;
let remaining = 0;
let firstVisitor = true;

export default function () {
    const stage = stageTags(); // 以送出時間歸屬階段（與 ask() 內的請求 tag 一致）
    let scenario;
    let ok;
    if (remaining <= 0) {
        user = newTestUser();
        // 所有 VU 都在爬升期開始第一位訪客；第一位的訊息數改成 1～MSGS_MAX 隨機，錯開之後換人的時間，
        // 避免開場問候集中成一波一波（lab 2026-11-12 實測）
        remaining = firstVisitor
            ? 1 + Math.floor(Math.random() * MSGS_MAX)
            : MSGS_MIN + Math.floor(Math.random() * (MSGS_MAX - MSGS_MIN + 1));
        firstVisitor = false;
        if (SIMULATE_FRONTEND && !enterSite(user)) entryFailed.add(1);
        scenario = OPENING;
        ok = ask(user, scenario, questionFor(scenario.id), true);
    } else {
        remaining--;
        scenario = pickScenario();
        ok = ask(user, scenario, questionFor(scenario.id), false);
    }
    (scenario === OPENING ? qaOpeningFailed : qaFailed).add(!ok, { flow: scenario.flow, case: scenario.id, ...stage });
}

// ---------- 摘要 ----------
export function handleSummary(data) {
    const m = data.metrics;
    const v = (name, key) => (m[name] && m[name].values[key] !== undefined ? m[name].values[key] : null);
    const sec = (ms) => (ms === null ? '    -' : (ms / 1000).toFixed(2).padStart(6) + 's');
    const rate = (r) => (r === null ? '-' : (r * 100).toFixed(2) + '%');
    const limits = { faq: QA_FAQ_MAX_MS, business: QA_BUSINESS_MAX_MS };

    const lines = [
        '',
        `===== 新阿發 對話回應時間測試｜RUN_ID=${RUN_ID} STAGE_SCOPE=${STAGE_SCOPE} 最高 ${PEAK_TPS.toFixed(1)} TPS 時間÷${TIME_SCALE} =====`,
        `情境占比   : ${SHARES}`,
        `對話總筆數 : ${v('qa_failed', 'passes') + v('qa_failed', 'fails') || 0}（一問一答＝一次 /api/chat/ask；不含開場問候）`,
        `對話失敗   : ${v('qa_failed', 'passes') || 0}  失敗率 ${rate(v('qa_failed', 'rate'))}（5xx、timeout、429、業務錯誤）`,
        `進站失敗   : ${v('entry_failed', 'count') || 0}（SIMULATE_FRONTEND=${SIMULATE_FRONTEND}）`,
        '',
        `類別        筆數      avg      p50      p95      p99      max   失敗率   門檻（${pct}）`,
    ];
    for (const flow of ['faq', 'business', 'init']) {
        const k = `http_req_duration{flow:${flow}}`;
        const count = v(`http_reqs{flow:${flow}}`, 'count');
        if (!m[k] || !count) continue;
        const lim = limits[flow] ? `≤ ${limits[flow] / 1000}s` : '只觀察（不計入）';
        lines.push(
            `${flow.padEnd(10)} ${String(count).padStart(6)} ${sec(v(k, 'avg'))} ${sec(v(k, 'med'))} ${sec(v(k, 'p(95)'))} ${sec(v(k, 'p(99)'))} ${sec(v(k, 'max'))}  ${rate(flow === 'init' ? v('qa_opening_failed', 'rate') : v(`qa_failed{flow:${flow}}`, 'rate')).padStart(7)}   ${lim}`
        );
    }
    lines.push('', '門檻判定：');
    for (const [name, metric] of Object.entries(m)) {
        if (!metric.thresholds) continue;
        for (const [expr, res] of Object.entries(metric.thresholds)) {
            if (/>=0$/.test(expr)) continue;
            lines.push(`  ${res.ok ? '✓' : '✗'} ${name} ${expr}`);
        }
    }
    lines.push('');
    return {
        stdout: lines.join('\n'),
        [`${RESULTS_DIR}/summary.json`]: JSON.stringify(data, null, 2),
    };
}
