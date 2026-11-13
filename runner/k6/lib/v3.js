// v3 分開測試（客戶 2026-11-13 確認）：業務查詢（腳本 A）與 FAQ 問答（腳本 B）分成兩支腳本，先跑 A 再跑 B，最後合併一份報表。
// 兩支腳本的入口：script-v3-a-business.js、script-v3-b-faq.js（只差 PART），共用這裡的邏輯。
// 由 control 的 onprem/bin/k6suite 依序啟動，不影響 script.js（正式壓測）與 script-qa.js（對話回應時間測試）。
//
// 與 script-qa.js 相同的做法：
//   - 一次 iteration ＝ 一則對話（一問一答＝一次 /api/chat/ask），負載以聊天 API 每秒請求數（TPS）控制
//   - 每個 VU 扮演一位訪客：先開場問候（B1，照送但不計入統計），再問 8～11 則，問完換下一位訪客
//   - 對話失敗＝回應不是預期結果（5xx、timeout、HTTP 429、業務錯誤），失敗後照樣繼續問
//   - 指標名稱沿用 qa_failed／qa_opening_failed（報表 Job 共用）
// 不同之處：
//   - 每支腳本只抽自己的情境：A＝flow business（B2-2、C1），B＝flow faq（B2、B3、B4、B5）；
//     情境與權重由 k6suite 以 SCENARIO_WEIGHTS 帶入（defaults.env 的 V3_A_WEIGHTS／V3_B_WEIGHTS），混到別類會直接報錯
//   - 回應時間門檻各自一個：A V3_A_MAX_MS（6 秒）、B V3_B_MAX_MS（20 秒），以 V3_LATENCY_PCT（p95）判定
//   - 自動停止由執行人員開始前選擇（V3_ABORT）：開始 V3_ABORT_DELAY 後，累計失敗率 > V3_ABORT_FAIL_RATE 就停止這支腳本
import { SharedArray } from 'k6/data';
import { Rate } from 'k6/metrics';
import { BASE_CONV_RATE, CALLS_PER_CONV, GRACEFUL_STOP, MSGS_MAX, MSGS_MIN, RESULTS_DIR, RUN_ID, SIMULATE_FRONTEND, STAGE_SCOPE, TIME_SCALE } from './config.js';
import { ACTIVE_STAGES, TOTAL_SECONDS, stageTags } from './stages.js';
import { ACTIVE, OPENING, SHARES } from './scenarios.js';
import { ask, newTestUser } from './chat.js';
import { enterSite, entryFailed } from './frontend.js';

const raw = (name, def) => (__ENV[name] !== undefined && __ENV[name] !== '' ? __ENV[name] : def);
function num(name, def) {
    const v = Number(raw(name, def));
    if (!Number.isFinite(v)) throw new Error(`${name} 必須是數字，收到：${__ENV[name]}`);
    return v;
}

export const PARTS = {
    A: { name: '業務查詢', flow: 'business', maxEnv: 'V3_A_MAX_MS', maxDefault: 6000 },
    B: { name: 'FAQ 問答', flow: 'faq', maxEnv: 'V3_B_MAX_MS', maxDefault: 20000 },
};

// 每支腳本建立一次（k6 的 options、default、handleSummary 必須由入口檔案 export）
// loadQuestions：由入口檔案提供（open() 的相對路徑以入口檔案為準）
export function buildPart(partId, loadQuestions) {
    const part = PARTS[partId];
    if (!part) throw new Error(`未知的 v3 腳本：${partId}`);

    const MAX_MS = num(part.maxEnv, part.maxDefault);
    const LATENCY_PCT = num('V3_LATENCY_PCT', 95);
    const FAIL_RATE = num('V3_FAIL_RATE', 0.005);
    const ABORT = String(raw('V3_ABORT', 'false')) === 'true';
    const ABORT_FAIL_RATE = num('V3_ABORT_FAIL_RATE', 0.05);
    const ABORT_DELAY = raw('V3_ABORT_DELAY', '3m');
    if (!(LATENCY_PCT > 0 && LATENCY_PCT < 100)) throw new Error('V3_LATENCY_PCT 必須介於 0～100');

    // 只能有這支腳本自己的情境（避免 A 混到 FAQ、B 混到業務查詢）
    const wrong = ACTIVE.filter((s) => s.flow !== part.flow);
    if (wrong.length > 0) {
        throw new Error(`腳本 ${partId}（${part.name}）只能用 ${part.flow} 類的情境，SCENARIO_WEIGHTS 有：${wrong.map((s) => s.id).join('、')}`);
    }

    const PEAK_TPS = Math.max(...ACTIVE_STAGES.map((s) => s.pct * BASE_CONV_RATE * CALLS_PER_CONV));
    // VU：每秒請求數 × 每次對話的時間（回應時間＋前端泡泡請求；回應變慢時需要更多 VU）
    const PRE_VUS = Math.max(1, Math.ceil(num('V3_PRE_VUS', PEAK_TPS * 20)));
    const MAX_VUS = Math.max(PRE_VUS, Math.ceil(num('V3_MAX_VUS', PEAK_TPS * 30)));

    const questions = new SharedArray(`questions-v3-${partId}`, () => [loadQuestions()]);
    for (const id of [OPENING.id, ...ACTIVE.map((s) => s.id)]) {
        if (!Array.isArray(questions[0][id]) || questions[0][id].length === 0) throw new Error(`data/questions.json 缺少情境 ${id} 的問句`);
    }
    const questionFor = (id) => {
        const list = questions[0][id];
        return list[Math.floor(Math.random() * list.length)];
    };
    const TOTAL_WEIGHT = ACTIVE.reduce((sum, s) => sum + s.weight, 0);
    const pickScenario = () => {
        let r = Math.random() * TOTAL_WEIGHT;
        for (const s of ACTIVE) {
            r -= s.weight;
            if (r <= 0) return s;
        }
        return ACTIVE[ACTIVE.length - 1];
    };

    // 對話失敗（1）／成功（0），tag：flow、case、stage、phase。開場問候另記，不計入對話統計
    const qaFailed = new Rate('qa_failed');
    const qaOpeningFailed = new Rate('qa_opening_failed');

    // ramping-arrival-rate 的 target 須為整數：以「每小時請求數」表示
    const reqPerHour = (s) => Math.max(1, Math.round(s.pct * BASE_CONV_RATE * CALLS_PER_CONV * 3600));
    const scaledSeconds = (min) => Math.max(1, Math.round((min * 60) / TIME_SCALE));
    const stages = [];
    for (const s of ACTIVE_STAGES) {
        stages.push({ target: reqPerHour(s), duration: `${s.rampMin > 0 ? scaledSeconds(s.rampMin) : 1}s` });
        stages.push({ target: reqPerHour(s), duration: `${scaledSeconds(s.holdMin)}s` });
    }

    const pct = `p(${LATENCY_PCT})`;
    const flowTag = `{flow:${part.flow}}`;
    const options = {
        scenarios: {
            [`v3${partId.toLowerCase()}`]: {
                executor: 'ramping-arrival-rate',
                startRate: 0,
                timeUnit: '1h',
                preAllocatedVUs: PRE_VUS,
                maxVUs: MAX_VUS,
                stages,
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
        thresholds: {
            // 回應時間只判定、不中止
            [`http_req_duration${flowTag}`]: [`${pct}<${MAX_MS}`],
            'http_req_duration{flow:init}': ['max>=0'],
            qa_failed: [`rate<=${FAIL_RATE}`].concat(
                ABORT ? [{ threshold: `rate<${ABORT_FAIL_RATE}`, abortOnFail: true, delayAbortEval: ABORT_DELAY }] : []
            ),
            qa_opening_failed: ['rate>=0'],
            [`http_reqs${flowTag}`]: ['count>=0'],
            'http_reqs{flow:init}': ['count>=0'],
            entry_failed: ['count>=0'],
        },
    };

    function setup() {
        console.log(
            `[k6] v3 腳本 ${partId} ${part.name} RUN_ID=${RUN_ID} 最高 ${PEAK_TPS.toFixed(1)} TPS 階段=${ACTIVE_STAGES.map((s) => s.id).join('→')} ` +
                `時間÷${TIME_SCALE} 門檻 ${pct}<${MAX_MS}ms 失敗率≤${FAIL_RATE} ` +
                `自動停止=${ABORT ? `開始 ${ABORT_DELAY} 後失敗率>${ABORT_FAIL_RATE}` : '不停止'} ` +
                `情境占比 ${SHARES} VU=${PRE_VUS}/${MAX_VUS}（全部 task 合計） 模擬前端=${SIMULATE_FRONTEND}`
        );
    }

    // 每個 VU 一位訪客：問完這位訪客的訊息數後換下一位（VU 內的狀態會跨 iteration 保留）
    const visitor = { user: null, remaining: 0, first: true };
    function run() {
        const stage = stageTags();
        let scenario;
        let ok;
        if (visitor.remaining <= 0) {
            visitor.user = newTestUser();
            // 第一位訪客的訊息數改成 1～MSGS_MAX 隨機，錯開之後換人的時間，避免開場問候集中成一波一波
            visitor.remaining = visitor.first
                ? 1 + Math.floor(Math.random() * MSGS_MAX)
                : MSGS_MIN + Math.floor(Math.random() * (MSGS_MAX - MSGS_MIN + 1));
            visitor.first = false;
            if (SIMULATE_FRONTEND && !enterSite(visitor.user)) entryFailed.add(1);
            scenario = OPENING;
            ok = ask(visitor.user, scenario, questionFor(scenario.id), true);
        } else {
            visitor.remaining--;
            scenario = pickScenario();
            ok = ask(visitor.user, scenario, questionFor(scenario.id), false);
        }
        (scenario === OPENING ? qaOpeningFailed : qaFailed).add(!ok, { flow: scenario.flow, case: scenario.id, ...stage });
    }

    function handleSummary(data) {
        const m = data.metrics;
        const v = (name, key) => (m[name] && m[name].values[key] !== undefined ? m[name].values[key] : null);
        const sec = (ms) => (ms === null ? '    -' : (ms / 1000).toFixed(2).padStart(6) + 's');
        const rate = (r) => (r === null ? '-' : (r * 100).toFixed(2) + '%');
        const total = (v('qa_failed', 'passes') || 0) + (v('qa_failed', 'fails') || 0);
        const lines = [
            '',
            `===== 新阿發 v3 腳本 ${partId} ${part.name}｜RUN_ID=${RUN_ID} STAGE_SCOPE=${STAGE_SCOPE} 最高 ${PEAK_TPS.toFixed(1)} TPS =====`,
            `情境占比   : ${SHARES}`,
            `對話總筆數 : ${total}（不含開場問候）`,
            `對話失敗   : ${v('qa_failed', 'passes') || 0}  失敗率 ${rate(v('qa_failed', 'rate'))}（門檻 ≤ ${rate(FAIL_RATE)}）`,
            `進站失敗   : ${v('entry_failed', 'count') || 0}（SIMULATE_FRONTEND=${SIMULATE_FRONTEND}）`,
            '',
            `類別        筆數      avg      p50      p95      p99      max`,
        ];
        for (const flow of [part.flow, 'init']) {
            const k = `http_req_duration{flow:${flow}}`;
            const count = v(`http_reqs{flow:${flow}}`, 'count');
            if (!m[k] || !count) continue;
            lines.push(
                `${flow.padEnd(10)} ${String(count).padStart(6)} ${sec(v(k, 'avg'))} ${sec(v(k, 'med'))} ${sec(v(k, 'p(95)'))} ${sec(v(k, 'p(99)'))} ${sec(v(k, 'max'))}` +
                    (flow === 'init' ? '  （開場問候，不計入）' : `  門檻 ${pct} ≤ ${MAX_MS / 1000}s`)
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

    return { options, setup, run, handleSummary };
}
