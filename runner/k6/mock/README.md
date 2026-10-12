# 新阿發 壓測腳本（mock 版）

依「新阿發 壓力測試規劃 v1.4」寫的 k6 腳本骨架，**只打本機的 mock server**，用來預覽 k6 Web Dashboard 和報表的樣子。所有回應與數值都是模擬的，**不代表任何真實環境**。腳本有防呆：`BASE_URL` 只接受 `127.0.0.1`／`localhost`。

正式腳本見 [../README.md](../README.md)。多個 task 合併報表的完整流程見 [docs/architecture.md](../../../docs/architecture.md) 與 [scripts/local-sim.sh](../../../scripts/local-sim.sh)。

## 對應規劃

| 規劃（v1.4） | mock 腳本做法 |
|---|---|
| 計量單位：每秒新開對話數 | `ramping-arrival-rate`，每個 iteration = 一位使用者跑完整個多輪對話 |
| 多輪對話（p.9） | （登入）→ 開場問候 → 8～11 則訊息（平均 9.5），每則間隔約 20 秒思考時間（±25%） |
| 逐階加壓（p.9） | Smoke 1% → 5% → 10% → Baseline 25% → 中量 50% → 1 倍 → 2 倍 → 3 倍 → Recovery 25%，每階爬升 3 分鐘 |
| 業務情境分配（p.11） | 15 個情境依「佔全部訊息 %」加權抽選；tag `case`（B2、C1…）、`scope`（included／conditional） |
| 三種流量組合（p.8） | `MIX=baseline`（10／90）、`MIX=business`（100% 業務查詢）、`MIX=faq`（100% FAQ） |
| 登入（p.11） | 對話前依 `LOGIN_RATIO` 登入，其中 10% OTP 重送；比例待確認，mock 預設 20% |
| 統計方式（p.14） | tag `flow`：`faq`／`business`／`init`（開場）／`login`，分開統計 |
| 分階段判定 | tag `stage`（smoke、low5 … x3、recovery）與 `phase`（ramp／hold），寫進 CSV 給報表 Job 分組 |
| 每秒處理量（p.14） | `http_reqs`（送出量）與 `chat_success`（成功量）並列 |
| 錯誤率（p.14） | `errors_5xx_timeout` = 5xx + timeout；業務錯誤另列 `business_errors` |
| 參考門檻（p.14） | FAQ p95 ≤ 10 秒／p99 ≤ 20 秒；業務 p95 ≤ 5 秒／p99 ≤ 10 秒；錯誤率 ≤ 0.5% |
| 停止條件（p.17） | 錯誤率 > 5%、p99 > 門檻 2 倍（延後 3 分鐘才開始判定）、任何 HTTP 429 立即停止 |

## Mock server 行為

[mock-server.js](mock-server.js) 模擬 BFF 的 `POST /api/chat/ask`，另有 `/mock/login`、`/mock/otp/resend`。

- **回應內容：** FAQ 回 `mode: FAQ`，業務查詢回 `mode: TPI`，B5（Model Armor 攔截）回 HTTP 205 E1004，格式同 BFF。
- **回應時間：** 依類別抽樣，分布為對數常態，p95 約為平均的 2 倍。
  - FAQ、開場問候：平均 9.57 秒（10/7 UAT 實測）。
  - 業務查詢：平均 3.5 秒（mock 假設值）。
  - 登入：平均 0.8 秒（mock 假設值）。
- **負載惡化：** 同時處理中的請求數超過 `MOCK_CAPACITY` 後，回應時間會變慢（最多 4 倍），5xx 機率也會上升，用來模擬加壓到瓶頸的樣子。
- **逾時：** 超過 30 秒回 504，模擬負載平衡器 timeout。

## 執行

需要 k6（已安裝 v2.3.0）和 Node.js。

**1. 啟動 mock server（另開一個 terminal）**

```bash
cd runner/k6/mock
node mock-server.js
```

**2a. 完整加壓曲線（約 15 分鐘，建議用這個看 dashboard）**

```bash
cd runner/k6/mock
mkdir -p results
K6_WEB_DASHBOARD=true K6_WEB_DASHBOARD_OPEN=true \
K6_WEB_DASHBOARD_EXPORT=results/report.html \
k6 run script.js
```

執行期間瀏覽器會自動開啟即時 dashboard（http://127.0.0.1:5665，每 10 秒更新），結束後報告存成 `results/report.html`。

- **前幾分鐘大多顯示 no data 是正常的：**Smoke 只有 1 倍的 1%，每分鐘不到 1 個對話，約 3 分鐘後的 Baseline 才開始有曲線。
- **加上中文教學說明：**`python3 ../../../report-job/annotate.py results/report.html`，會產出 `results/report-annotated.html`。各圖表的說明見 [docs/dashboard-guide.md](../../../docs/dashboard-guide.md)。

**2b. 快速版（約 2 分鐘，只確認能跑）**

```bash
# mock server 改用快速模式啟動：
MOCK_TIME_SCALE=0.1 MOCK_CAPACITY=20 node mock-server.js

K6_WEB_DASHBOARD=true K6_WEB_DASHBOARD_PERIOD=2s \
K6_WEB_DASHBOARD_EXPORT=results/report-quick.html \
k6 run -e TIME_SCALE=120 -e THINK_TIME_S=1 -e ABORT=false script.js
```

## 參數（`-e 名稱=值`）

| 參數 | 預設 | 說明 |
|---|---|---|
| BASE_URL | `http://127.0.0.1:8090` | 只允許本機 |
| BASE_CONV_RATE | 1.18 | mock 的「1 倍」每秒新開對話數（真實 1 倍 118 的 1/100） |
| TIME_SCALE | 15 | 階段時間壓縮倍率；規劃一輪約 3.6 小時，÷15 約 14.5 分鐘 |
| STAGES | full | `full` 完整逐階加壓／`smoke` 只跑 Smoke |
| MIX | baseline | `baseline`／`business`／`faq` |
| THINK_TIME_S | 20 | 每則訊息間的思考時間（秒） |
| MSGS_MIN／MSGS_MAX | 8／11 | 每個對話的訊息數範圍 |
| LOGIN_RATIO | 0.2 | 對話中登入的比例（mock 假設值，規劃待確認） |
| OTP_RESEND_RATIO | 0.1 | 登入中需要 OTP 重送的比例 |
| MAX_VUS | 1500 | VU 上限；3 倍時約需 1,000 個 |
| PRE_VUS | 自動估算 | 預先建立的 VU 數；預設＝最高每秒對話數 × 每段對話平均秒數 × 1.2（避免 dropped_iterations） |
| RESULTS_DIR | results | `summary.json` 的輸出目錄；多個 k6 同時跑時各自指定，避免互相覆蓋 |
| ABORT | true | 是否啟用停止條件 |
| ABORT_DELAY | 3m | 停止條件延後多久才開始判定 |

mock server 的參數（`MOCK_FAQ_MEAN_MS`、`MOCK_CAPACITY` 等）見 [mock-server.js](mock-server.js) 開頭的註解。

## 產出

| 檔案 | 內容 |
|---|---|
| `results/report.html` | k6 Web Dashboard 匯出的 HTML 報告 |
| `results/summary.json` | 完整統計 |
| console | 各類別（faq／business／init／login）的 p50／p95／p99、錯誤率，以及門檻判定 |

## 與正式壓測的差異（限制）

- **單機執行：** 規劃的 Cloud Run Jobs 多 task 平行、execution segment、NAT 固定 IP、GCS 合併報表都不在這個 mock 範圍內。
- **時間壓縮只套用在階段長度：** 對話本身仍是約 4～5 分鐘（20 秒思考時間 × 9.5 則），所以每階的同時在線人數來不及達到穩定值，加壓曲線會比真實情況平緩。要更接近，可以調低 `TIME_SCALE`（例如 `-e TIME_SCALE=5`，約 45 分鐘）。
- **k6 門檻是整輪累計值，不是「連續 3 分鐘」：** 停止條件用 `delayAbortEval` 延後判定來近似，正式壓測需另外以監控 Dashboard 判斷連續超標。
- **回應時間與錯誤都是模擬值：** 業務查詢 3.5 秒、登入 0.8 秒、登入比例 20% 都是 mock 假設值。
- **業務查詢的真實 API 流程待確認：** mock 一律走 `/api/chat/ask`，真實的業務流程後續可能還會呼叫 `/api/chat/tpi`，接上真實環境時要依實際流程調整。
