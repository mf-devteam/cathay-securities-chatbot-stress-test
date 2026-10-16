// 所有參數都從環境變數讀取：Cloud Run Job 的環境變數會直接成為 k6 的 __ENV，本機用 -e 名稱=值。
// 預設值依「新阿發壓測 Cloud Run 部署規格建議 v1.0」與「新阿發_壓測負載試算表_v1.4」。
// 參數一覽見 runner/k6/README.md。

const raw = (name, def) => (__ENV[name] !== undefined && __ENV[name] !== '' ? __ENV[name] : def);

function num(name, def) {
    const v = Number(raw(name, def));
    if (!Number.isFinite(v)) throw new Error(`${name} 必須是數字，收到：${__ENV[name]}`);
    return v;
}

const bool = (name, def) => String(raw(name, def)) === 'true';

// ---------- 受測端 ----------
export const BASE_URL = String(raw('BASE_URL', '')).replace(/\/+$/, '');
if (!BASE_URL) throw new Error('BASE_URL 必填');
// 防呆：打本機以外的位址（lab、UAT）要明確設定 ALLOW_REMOTE_TARGET=true（由 Job 定義設定），避免本機誤打真實環境
const IS_LOCAL_TARGET = /^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(BASE_URL);
if (!IS_LOCAL_TARGET && !bool('ALLOW_REMOTE_TARGET', 'false')) {
    throw new Error(`BASE_URL 不是本機（${BASE_URL}）：對 lab／UAT 壓測時需設定 ALLOW_REMOTE_TARGET=true`);
}
export const REQUEST_TIMEOUT = raw('REQUEST_TIMEOUT', '60s');
export const CHANNEL = raw('CHANNEL', 'web');
export const DEVICE = raw('DEVICE', 'Web');

// ---------- 本輪識別 ----------
export const RUN_ID = raw('RUN_ID', 'local');

// ---------- 負載模型（試算表 01 分頁） ----------
// 1 倍的每秒新開對話數（證券方 10/8 回覆）
export const BASE_CONV_RATE = num('BASE_CONV_RATE', 118);
// 每段對話的訊息數（開場問候之外），平均 9.5
export const MSGS_MIN = num('MSGS_MIN', 8);
export const MSGS_MAX = num('MSGS_MAX', 11);
// 兩則訊息之間的思考時間（秒），±THINK_JITTER 隨機
export const THINK_TIME_S = num('THINK_TIME_S', 20);
export const THINK_JITTER = num('THINK_JITTER', 0.25);
// 對話 p95 時長（秒），用來估算 VU 數：VU = 每秒新開對話數 × 對話 p95 時長
export const CONV_P95_S = num('CONV_P95_S', 391);
if (MSGS_MIN < 1 || MSGS_MAX < MSGS_MIN) throw new Error('MSGS_MIN／MSGS_MAX 設定錯誤');

// ---------- 逐階加壓 ----------
// 格式：階段代號:佔 1 倍:持續分鐘[:爬升分鐘]，以分號分隔（逗號會被 gcloud --update-env-vars 當成分隔符號）
// 爬升分鐘省略時用 RAMP_MIN；試算表 02 分頁 Smoke 爬升 1 分鐘、其他 3 分鐘
// 階段代號與報表 Job 的顯示名稱對應：smoke、low5、low10、baseline、mid50、x1、x2、x3、recovery
const DEFAULT_STAGE_PLAN = 'smoke:0.01:10:1;low5:0.05:15;low10:0.1:15;baseline:0.25:15;mid50:0.5:15;x1:1:30;recovery:0.25:15';
export const RAMP_MIN = num('RAMP_MIN', 3);
export const STAGE_PLAN = parseStagePlan(raw('STAGE_PLAN', DEFAULT_STAGE_PLAN));
// full＝STAGE_PLAN 全部；smoke＝只跑第一階段
export const STAGE_SCOPE = raw('STAGE_SCOPE', 'full');
if (!['full', 'smoke'].includes(STAGE_SCOPE)) throw new Error(`STAGE_SCOPE 只能是 full／smoke，收到：${STAGE_SCOPE}`);
// 階段時間壓縮倍率（只壓縮階段長度，不壓縮對話本身）；正式壓測為 1，本機模擬可調大
export const TIME_SCALE = num('TIME_SCALE', 1);
export const GRACEFUL_STOP = raw('GRACEFUL_STOP', '60s');

function parseStagePlan(text) {
    const stages = text.split(';').map((s) => s.trim()).filter(Boolean).map((part) => {
        const [id, pct, holdMin, rampMin] = part.split(':');
        const stage = { id, pct: Number(pct), holdMin: Number(holdMin), rampMin: rampMin === undefined ? RAMP_MIN : Number(rampMin) };
        if (!/^[a-z0-9_-]+$/.test(id || '') || !(stage.pct > 0) || !(stage.holdMin > 0) || !(stage.rampMin >= 0)) {
            throw new Error(`STAGE_PLAN 格式錯誤（應為 代號:佔1倍:持續分鐘[:爬升分鐘]），收到：${part}`);
        }
        return stage;
    });
    if (stages.length === 0) throw new Error('STAGE_PLAN 不能是空的');
    return stages;
}

// ---------- 業務情境 ----------
// 格式：情境代號:權重，以分號分隔；權重為相對值，會自動正規化（業務情境組合黃色列的比例）
const DEFAULT_SCENARIO_WEIGHTS = 'B2:28;B2-2:36;B3:15;B4:5;B5:2;C1:12';
export const SCENARIO_WEIGHTS = parseWeights(raw('SCENARIO_WEIGHTS', DEFAULT_SCENARIO_WEIGHTS));

function parseWeights(text) {
    const out = {};
    for (const part of text.split(';').map((s) => s.trim()).filter(Boolean)) {
        const [id, weight] = part.split(':');
        const w = Number(weight);
        if (!id || !(w >= 0)) throw new Error(`SCENARIO_WEIGHTS 格式錯誤（應為 代號:權重），收到：${part}`);
        out[id] = w;
    }
    return out;
}

// ---------- VU 數（全部 task 的合計；execution segment 會按比例分給各 task） ----------
const ACTIVE_PEAK_PCT = Math.max(...(STAGE_SCOPE === 'smoke' ? STAGE_PLAN.slice(0, 1) : STAGE_PLAN).map((s) => s.pct));
export const PRE_VUS = Math.max(1, Math.ceil(num('PRE_VUS', ACTIVE_PEAK_PCT * BASE_CONV_RATE * CONV_P95_S)));
export const MAX_VUS = Math.max(PRE_VUS, Math.ceil(num('MAX_VUS', PRE_VUS * 1.2)));

// ---------- 門檻與停止條件 ----------
// 與報表 Job 共用同一個 THRESHOLDS_JSON（毫秒／比例），預設為證券方文件參考值
const DEFAULT_THRESHOLDS = { faq: { p95: 10000, p99: 20000 }, business: { p95: 5000, p99: 10000 }, error_rate: 0.005 };
export const THRESHOLDS = (() => {
    const text = raw('THRESHOLDS_JSON', '');
    if (!text) return DEFAULT_THRESHOLDS;
    try {
        const t = JSON.parse(text);
        for (const flow of ['faq', 'business']) {
            if (!(t[flow] && t[flow].p95 > 0 && t[flow].p99 > 0)) throw new Error(`缺少 ${flow}.p95／p99`);
        }
        if (!(t.error_rate >= 0)) throw new Error('缺少 error_rate');
        return t;
    } catch (e) {
        throw new Error(`THRESHOLDS_JSON 格式錯誤：${e.message}`);
    }
})();
export const ABORT = bool('ABORT', 'true');
export const ABORT_DELAY = raw('ABORT_DELAY', '3m');
// 停止條件：錯誤率 > ABORT_ERROR_RATE、p99 > 門檻 × ABORT_P99_FACTOR、任何 HTTP 429
export const ABORT_ERROR_RATE = num('ABORT_ERROR_RATE', 0.05);
export const ABORT_P99_FACTOR = num('ABORT_P99_FACTOR', 2);

// ---------- 輸出 ----------
export const RESULTS_DIR = String(raw('RESULTS_DIR', 'results')).replace(/\/+$/, '');
