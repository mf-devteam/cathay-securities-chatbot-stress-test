// 新阿發 壓測腳本（正式版）：依規格書 v1.0、試算表 v1.4 與業務情境組合（黃色列）
//   - 負載以「每秒新開對話數」計，每個 iteration = 一位訪客跑完整段對話：
//     開場問候（B1）→ 8～11 則訊息（平均 9.5），每則間隔約 20 秒思考時間，每則依權重抽一個情境
//   - 逐階加壓：STAGE_PLAN（預設 Smoke 1% → 5% → 10% → Baseline 25% → 50% → 1 倍 → Recovery 25%）
//   - 指標：FAQ／業務查詢分開統計；錯誤率 = 5xx + timeout，業務錯誤另列
// 本機打 runner/k6/mock/mock-server.js；打 lab／UAT 需設定 ALLOW_REMOTE_TARGET=true。參數見 runner/k6/README.md。
import { sleep } from 'k6';
import { SharedArray } from 'k6/data';
import {
    ABORT,
    ABORT_DELAY,
    ABORT_ERROR_RATE,
    ABORT_P99_FACTOR,
    BASE_CONV_RATE,
    GRACEFUL_STOP,
    MAX_VUS,
    MSGS_MAX,
    MSGS_MIN,
    PRE_VUS,
    RESULTS_DIR,
    RUN_ID,
    STAGE_SCOPE,
    THINK_JITTER,
    THINK_TIME_S,
    THRESHOLDS,
    TIME_SCALE,
} from './lib/config.js';
import { ACTIVE_STAGES, TOTAL_SECONDS, buildStages, stageMarker } from './lib/stages.js';
import { ACTIVE, OPENING, SHARES, pickScenario } from './lib/scenarios.js';
import { ask, conversationsDone, newTestUser } from './lib/chat.js';

export { stageMarker };

const questions = new SharedArray('questions', () => [JSON.parse(open('./data/questions.json'))]);

for (const id of [OPENING.id, ...ACTIVE.map((s) => s.id)]) {
    if (!Array.isArray(questions[0][id]) || questions[0][id].length === 0) throw new Error(`data/questions.json 缺少情境 ${id} 的問句`);
}

function questionFor(id) {
    const list = questions[0][id];
    return list[Math.floor(Math.random() * list.length)];
}

// ---------- k6 options ----------
const abortRule = (threshold) => ({ threshold, abortOnFail: ABORT, delayAbortEval: ABORT_DELAY });
const { faq, business, error_rate: errorRate } = THRESHOLDS;

export const options = {
    scenarios: {
        conversation: {
            executor: 'ramping-arrival-rate',
            startRate: 0,
            timeUnit: '1h',
            preAllocatedVUs: PRE_VUS,
            maxVUs: MAX_VUS,
            stages: buildStages(),
            gracefulStop: GRACEFUL_STOP,
        },
        // 只有 1 個 VU：多個 task 時只會落在其中一個 task，階段切換事件不會重複
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
    thresholds: {
        // 參考門檻（證券方文件）；p99 > 門檻 × ABORT_P99_FACTOR 為停止條件
        'http_req_duration{flow:faq}': [`p(95)<${faq.p95}`, `p(99)<${faq.p99}`, abortRule(`p(99)<${faq.p99 * ABORT_P99_FACTOR}`)],
        'http_req_duration{flow:business}': [`p(95)<${business.p95}`, `p(99)<${business.p99}`, abortRule(`p(99)<${business.p99 * ABORT_P99_FACTOR}`)],
        // 只觀察：開場問候
        'http_req_duration{flow:init}': ['max>=0'],
        // 錯誤率參考門檻；> ABORT_ERROR_RATE 為停止條件
        errors_5xx_timeout: [`rate<=${errorRate}`, abortRule(`rate<${ABORT_ERROR_RATE}`)],
        'errors_5xx_timeout{flow:faq}': [`rate<=${errorRate}`],
        'errors_5xx_timeout{flow:business}': [`rate<=${errorRate}`],
        // HTTP 429：立即停止
        http_429: [{ threshold: 'count<1', abortOnFail: ABORT }],
        // 只為了讓子指標出現在摘要（不是門檻）
        'http_reqs{flow:faq}': ['count>=0'],
        'http_reqs{flow:business}': ['count>=0'],
        'http_reqs{flow:init}': ['count>=0'],
        'errors_5xx_timeout{flow:init}': ['rate>=0'],
        'chat_success{flow:faq}': ['count>=0'],
        'chat_success{flow:business}': ['count>=0'],
    },
};

export function setup() {
    console.log(
        `[k6] RUN_ID=${RUN_ID} 1倍=${BASE_CONV_RATE} 對話/秒 階段=${ACTIVE_STAGES.map((s) => s.id).join('→')} ` +
            `時間÷${TIME_SCALE} 情境占比 ${SHARES} VU=${PRE_VUS}/${MAX_VUS}（全部 task 合計）`
    );
}

function thinkTime() {
    return THINK_TIME_S * (1 - THINK_JITTER + Math.random() * 2 * THINK_JITTER);
}

// 一次 iteration = 一位訪客的完整對話
export default function () {
    const user = newTestUser();
    ask(user, OPENING, questionFor(OPENING.id), true);

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
        `===== 新阿發壓測摘要｜RUN_ID=${RUN_ID} STAGE_SCOPE=${STAGE_SCOPE} 1倍=${BASE_CONV_RATE} 對話/秒 時間÷${TIME_SCALE} =====`,
        `情境占比     : ${SHARES}`,
        `完成對話數   : ${v('conversations_completed', 'count') || 0}`,
        `送出／成功   : ${v('http_reqs', 'count') || 0} ／ ${v('chat_success', 'count') || 0}（${((v('http_reqs', 'count') || 0) / dur).toFixed(2)} req/s）`,
        `錯誤率       : ${pct(v('errors_5xx_timeout', 'rate'))}（5xx + timeout）  業務錯誤 ${v('business_errors', 'count') || 0}  HTTP 429 ${v('http_429', 'count') || 0}`,
        '',
        '類別        請求數      avg      p50      p95      p99      max   錯誤率',
    ];
    for (const flow of ['faq', 'business', 'init']) {
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
