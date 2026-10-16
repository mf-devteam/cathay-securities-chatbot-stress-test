// 新阿發 BFF 的本機 mock server：只用來看 k6 dashboard 長相，不連任何真實環境。
// 模擬 POST /api/chat/ask（回應格式同 BFF）、登入與 OTP 重送。
// 回應時間依情境抽樣（對數常態分布），並隨同時處理中的請求數變慢，用來模擬加壓後的惡化。
//
// 啟動：node mock-server.js        （預設 http://127.0.0.1:8090）
// 參數（環境變數）：
//   MOCK_PORT           預設 8090（Cloud Run 上改讀 PORT）
//   MOCK_HOST           預設 127.0.0.1；部署成 Cloud Run Service 時設 0.0.0.0
//   MOCK_FAQ_MEAN_MS    FAQ 問答平均回應時間，預設 9570（10/7 UAT 前端實測平均）
//   MOCK_BIZ_MEAN_MS    業務查詢平均回應時間，預設 3500（mock 假設值，Smoke 實測後修正）
//   MOCK_INIT_MEAN_MS   開場問候平均回應時間，預設 9570（試算表假設值）
//   MOCK_LOGIN_MEAN_MS  登入／OTP 平均回應時間，預設 800（mock 假設值）
//   MOCK_P95_RATIO      p95 ÷ 平均，預設 2（試算表：20.25 ÷ 9.57 ≈ 2.1）
//   MOCK_CAPACITY       同時處理中請求數的「舒適上限」，超過後明顯變慢、開始回 503，預設 150
//   MOCK_LB_TIMEOUT_MS  模擬負載平衡器 timeout，預設 30000（超過即回 504）
//   MOCK_BASE_ERROR     基本 5xx 機率，預設 0.001
//   MOCK_TIME_SCALE     回應時間縮放倍率（例如 0.1 = 快 10 倍，快速驗證用），預設 1

const http = require('http');

const num = (name, def) => (process.env[name] !== undefined ? Number(process.env[name]) : def);
const PORT = num('MOCK_PORT', num('PORT', 8090));
const HOST = process.env.MOCK_HOST || '127.0.0.1';
const P95_RATIO = num('MOCK_P95_RATIO', 2);
const CAPACITY = num('MOCK_CAPACITY', 150);
const LB_TIMEOUT_MS = num('MOCK_LB_TIMEOUT_MS', 30000);
const BASE_ERROR = num('MOCK_BASE_ERROR', 0.001);
const TIME_SCALE = num('MOCK_TIME_SCALE', 1);
const MEAN_MS = {
    faq: num('MOCK_FAQ_MEAN_MS', 9570),
    business: num('MOCK_BIZ_MEAN_MS', 3500),
    init: num('MOCK_INIT_MEAN_MS', 9570),
    login: num('MOCK_LOGIN_MEAN_MS', 800),
};

// 由 p95 ÷ 平均 反推對數常態分布的 sigma：1.645σ − σ²/2 = ln(ratio)
const SIGMA = (() => {
    const c = Math.log(P95_RATIO);
    const disc = 1.645 * 1.645 - 2 * c;
    return disc > 0 ? 1.645 - Math.sqrt(disc) : 0.5;
})();

function gaussian() {
    let u = 0;
    while (u === 0) u = Math.random();
    return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * Math.random());
}

function sampleLatency(meanMs) {
    const mu = Math.log(meanMs) - (SIGMA * SIGMA) / 2;
    return Math.exp(mu + SIGMA * gaussian());
}

let inflight = 0;
let total = 0;

// 超過舒適上限後，回應時間以平方成長（最多 4 倍），5xx 機率也跟著上升
function loadFactor() {
    const ratio = inflight / CAPACITY;
    return ratio <= 1 ? 1 : Math.min(4, ratio * ratio);
}

function overloadErrorRate() {
    const ratio = inflight / CAPACITY;
    return ratio <= 1.2 ? 0 : Math.min(0.3, (ratio - 1.2) * 0.2);
}

function send(res, status, body) {
    res.writeHead(status, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(body));
}

function errorBody(status, errorCode, message, extra = {}) {
    return { status, errorCode, message, ...extra };
}

// 依 k6 腳本帶來的 X-Load-Test-Case／X-Load-Test-Flow header 決定回應內容（真實 BFF 會忽略這些 header）
function buildAskResponse(scenario, flow) {
    switch (scenario) {
        case 'B5': // Model Armor 攔截：BFF 以 HTTP 205 回 E1004
            return [205, errorBody(205, 'E1004', '無法回應不合適的內容，建議你詢問其他問題。')];
        case 'B4': // 知識庫外：仍為 FAQ 回答，內容為婉拒說明
            return [200, { status: 200, data: [faqItem('很抱歉，這個問題目前不在阿發的服務範圍內。')] }];
        case 'B1': // 開場問候
            return [200, { status: 200, data: [faqItem('你好，我是阿發，有什麼可以幫你的嗎？')] }];
        default:
            if (flow === 'business') {
                return [200, { status: 200, data: [{ mode: 'TPI', isLogin: false, content: [{ type: 'text', text: `[mock] ${scenario} 查詢結果` }] }] }];
            }
            return [200, { status: 200, data: [faqItem(`[mock] ${scenario} FAQ 回答內容`)] }];
    }
}

function faqItem(content) {
    return { mode: 'FAQ', isLogin: false, content, hasReferences: false, references: [], botNewMsgId: Math.random().toString(16).slice(2), link: [] };
}

const server = http.createServer((req, res) => {
    let raw = '';
    req.on('data', (chunk) => (raw += chunk));
    req.on('end', () => {
        if (req.method === 'GET' && req.url === '/healthz') {
            return send(res, 200, { status: 'OK', inflight, total, capacity: CAPACITY });
        }

        let kind;
        let scenario = String(req.headers['x-load-test-case'] || req.headers['x-mock-scenario'] || 'B2');
        const flowHeader = String(req.headers['x-load-test-flow'] || '');
        if (req.method === 'POST' && req.url === '/api/chat/ask') {
            let body = {};
            try {
                body = JSON.parse(raw || '{}');
            } catch (_) {
                return send(res, 422, errorBody(422, 'E1002', '格式錯誤'));
            }
            if (body.isInitialized) scenario = 'B1';
            kind = scenario === 'B1' ? 'init' : ['faq', 'business'].includes(flowHeader) ? flowHeader : scenario.startsWith('B') ? 'faq' : 'business';
        } else if (req.method === 'POST' && (req.url === '/mock/login' || req.url === '/mock/otp/resend')) {
            kind = 'login';
        } else {
            return send(res, 404, errorBody(404, 'E1007', 'not found'));
        }

        inflight += 1;
        total += 1;
        const latency = sampleLatency(MEAN_MS[kind]) * loadFactor() * TIME_SCALE;
        const lbTimeout = LB_TIMEOUT_MS * TIME_SCALE;
        const failed = Math.random() < BASE_ERROR + overloadErrorRate();

        const finish = (status, body, delay) =>
            setTimeout(() => {
                inflight -= 1;
                send(res, status, body);
            }, delay);

        if (latency > lbTimeout) {
            return finish(504, errorBody(504, 'LB_TIMEOUT', '[mock] 負載平衡器逾時'), lbTimeout);
        }
        if (failed) {
            return finish(503, errorBody(503, 'E1009', '[mock] 服務忙碌'), latency * 0.3);
        }
        if (kind === 'login') {
            return finish(200, { status: 200, data: { result: 'ok' } }, latency);
        }
        const [status, body] = buildAskResponse(scenario, kind === 'init' ? 'faq' : kind);
        return finish(status, body, latency);
    });
});

server.keepAliveTimeout = 65000;
server.listen(PORT, HOST, () => {
    console.log(`[mock] listening on http://${HOST}:${PORT}`);
    console.log(`[mock] mean(ms)=${JSON.stringify(MEAN_MS)} p95/mean=${P95_RATIO} sigma=${SIGMA.toFixed(3)} capacity=${CAPACITY} timeScale=${TIME_SCALE}`);
});

setInterval(() => {
    if (total > 0) console.log(`[mock] inflight=${inflight} total=${total} loadFactor=${loadFactor().toFixed(2)}`);
}, 10000).unref();
