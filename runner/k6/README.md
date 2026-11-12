# k6 腳本

整個 `k6/` 目錄會打包進壓測機映像檔（[Dockerfile](../Dockerfile)），預設執行正式腳本 `script.js`。所有參數都是環境變數：在 Cloud Run 上寫在 Job 定義（Terraform）或執行時用 `--update-env-vars` 覆寫，本機用 `k6 run -e 名稱=值`。

```
k6/
├── script.js          入口：k6 options（逐階加壓、門檻）、一位訪客的完整對話、摘要
├── script-qa.js       對話回應時間測試：以一問一答為單位（見下方「對話回應時間測試」）
├── lib/
│   ├── config.js      讀取全部環境變數與預設值（下方參數一覽）
│   ├── stages.js      STAGE_PLAN → 逐階加壓、stage／phase tag、階段切換事件
│   ├── scenarios.js   業務情境與權重
│   ├── chat.js        POST /api/chat/ask、結果分類、自訂指標
│   └── frontend.js    模擬前端的進站、頁面載入、泡泡請求（SIMULATE_FRONTEND）
├── data/questions.json  各情境的問句（暫定，正式問句待提供）
└── mock/              本機 mock server（受測端替身）
```

## 腳本做什麼

一次 iteration ＝ 一位訪客的完整對話，負載以「每秒新開對話數」控制（`ramping-arrival-rate`）：

1. 開場問候（B1 開啟聊天）：`/ask`，`isInitialized=true`
2. 8～11 則訊息（平均 9.5），每則間隔約 20 秒思考時間（±25%），每則依權重抽一個情境

業務情境只做「業務情境組合」中**標黃色的列**：

| 代號 | 情境 | 預設權重 | 判定門檻（`flow`） | 說明 |
|---|---|---|---|---|
| B2 | FAQ 問答 | 28 | FAQ | |
| B2-2 | FAQ intent | 36 | 業務查詢 | 非 AI，經 TPI／證券地端 |
| B3 | 多輪對話 | 15 | FAQ | 同一對話中的追問 |
| B4 | 知識庫以外問題 | 5 | FAQ | |
| B5 | Model Armor 攔截 | 2 | FAQ | 預期 HTTP 205 |
| C1 | 查看 ETF 清單 | 12 | 業務查詢 | 非 AI，經 TPI |
| B1 | 開啟聊天 | （每段對話 1 次） | 只觀察（`init`） | 開場問候 |
| A4 | Token 續期 | — | — | 需要登入，不在腳本流程內；Soak 時觀察受測端 |

權重是相對值，會自動正規化（黃色列加總 98%）。AI 情境套 FAQ 門檻、非 AI 情境套業務查詢門檻，報表 Job 依 `flow` tag 分開統計。

每個請求的 tag：`stage`／`phase`（分階段）、`flow`（faq／business／init）、`case`（情境代號），寫進 CSV 給報表 Job 分組。另帶 header `X-Load-Test-Run`、`X-Load-Test-Case`、`X-Load-Test-Flow`，方便受測端從 log 篩出壓測流量。

## 參數一覽

| 參數 | 預設 | 說明 |
|---|---|---|
| **受測端** | | |
| BASE_URL | （必填） | 受測端入口，例如 `https://<網域>`；腳本會打 `${BASE_URL}/api/chat/ask` |
| ALLOW_REMOTE_TARGET | false | BASE_URL 不是本機時必須設為 `true`（Job 定義設定），避免本機誤打真實環境 |
| REQUEST_TIMEOUT | 60s | 單一請求逾時（受測端 LB 30 秒會先逾時） |
| SIMULATE_FRONTEND | true | 模擬前端在聊天 API 以外的請求（見下方「前端請求」）；`false` 只打 `/api/chat/ask` |
| FRONTEND_URL | （空） | 前端頁面網址（進站 302 的目標）；有設定時每位訪客 GET 一次頁面 HTML，也是連線預檢步驟 4 的網址 |
| CHANNEL／DEVICE | web／Web | `/ask` body 的 `channel`、`device` |
| RUN_ID | local | 本輪代號；Cloud Run 上由 entrypoint.sh 帶入 |
| **負載模型**（試算表 01 分頁） | | |
| BASE_CONV_RATE | 118 | 1 倍的每秒新開對話數 |
| MSGS_MIN／MSGS_MAX | 8／11 | 每段對話的訊息數（不含開場） |
| THINK_TIME_S | 20 | 訊息間的思考時間（秒） |
| THINK_JITTER | 0.25 | 思考時間 ± 比例 |
| CONV_P95_S | 391 | 對話 p95 時長（秒），用來估 VU 數 |
| **逐階加壓**（試算表 02 分頁） | | |
| STAGE_PLAN | `smoke:0.01:10:1;low5:0.05:15;low10:0.1:15;baseline:0.25:15;mid50:0.5:15;x1:1:30;recovery:0.25:15` | `代號:佔1倍:持續分鐘[:爬升分鐘]`，分號分隔。一輪約 134 分鐘。「佔1倍」也可以寫成聊天 API 每秒請求數，例如 `s3:354tps:60`（依每段對話的請求數 1＋(MSGS_MIN＋MSGS_MAX)÷2 換算；地端版用這個寫法，見 [onprem/config/defaults.env](../../onprem/config/defaults.env)） |
| RAMP_MIN | 3 | STAGE_PLAN 沒寫爬升分鐘時的預設 |
| STAGE_SCOPE | full | `full`＝全部階段；`smoke`＝只跑第一階段 |
| TIME_SCALE | 1 | 階段時間壓縮倍率（只壓縮階段，不壓縮對話）；本機模擬用 |
| GRACEFUL_STOP | 60s | 最後一階段結束後，等進行中對話的時間 |
| **業務情境** | | |
| SCENARIO_WEIGHTS | `B2:28;B2-2:36;B3:15;B4:5;B5:2;C1:12` | `代號:權重`，分號分隔；只跑 FAQ 可設 `B2:1` |
| **VU 數**（全部 task 合計，k6 依 execution segment 按比例分給各 task） | | |
| PRE_VUS | 最高階段每秒對話數 × CONV_P95_S | 1 倍時 46,138 |
| MAX_VUS | PRE_VUS × 1.2 | |
| **門檻與停止條件** | | |
| THRESHOLDS_JSON | `{"faq":{"p95":10000,"p99":20000},"business":{"p95":5000,"p99":10000},"error_rate":0.005}` | 毫秒／比例；與報表 Job 共用同一個值 |
| ABORT | true | 是否啟用停止條件 |
| ABORT_DELAY | 3m | 停止條件延後多久開始判定 |
| ABORT_ERROR_RATE | 0.05 | 錯誤率超過即停止 |
| ABORT_P99_FACTOR | 2 | p99 超過門檻幾倍即停止；另外任何 HTTP 429 立即停止 |
| **輸出** | | |
| RESULTS_DIR | results | `summary.json` 的目錄；entrypoint.sh 會設定 |

分號分隔的原因：`gcloud run jobs execute --update-env-vars` 以逗號分隔多個變數，值裡有逗號（例如 JSON）時要另外跳脫。

## 對話回應時間測試（script-qa.js）

客戶 2026-11-12 新增的測試，與正式腳本並存（正式腳本、情境與原本的報告都不變）。判定方式、負載與停止條件依客戶 2026-11-12 的回覆：

| 項目 | 正式腳本 script.js | 對話回應時間測試 script-qa.js |
|---|---|---|
| 單位 | 請求 | 對話＝使用者提問的一問一答（一次 `/api/chat/ask`）；開場問候照送，但不計入對話統計，只單獨列出 |
| 負載 | 每秒新開對話數；TPS 要等對話累積 | 直接控制聊天 API 每秒請求數（含開場問候，TPS 定義與地端規劃相同）：`STAGE_PLAN` 的爬升分鐘就是 TPS 爬升時間。地端 1 倍 118、3 倍 354 TPS 各跑一輪，1 分鐘爬升＋10 分鐘 |
| 訪客流程 | 開場＋8～11 則，中間思考時間 | 每個 VU 一位訪客：開場＋8～11 則後換下一位；間隔由 TPS 決定，不另外 sleep |
| 錯誤／失敗 | 錯誤率＝5xx＋timeout | 失敗率＝對話失敗筆數 ÷ 對話總筆數；5xx、timeout、429、業務錯誤（例如 B5 沒回 205）都算，失敗後照樣繼續問 |
| 門檻 | THRESHOLDS_JSON | FAQ p95 ≤ 8 秒（含 AI 生成時間）、業務查詢 p95 ≤ 2 秒；只判定不中止 |
| 停止條件 | 錯誤率、p99、429 | 預設不停止，一律打完；`QA_ABORT=true` 時失敗率 > `ABORT_ERROR_RATE` 停止 |
| 報表 | `reports/latest.html` | 另外產生 `reports/qa-latest.html`（原本的報表照常產生） |

情境、問句、請求內容與判定沿用 `lib/`。指標 `qa_failed`（使用者提問）、`qa_opening_failed`（開場問候）都是 Rate，tag：flow、case、stage、phase，寫進 CSV 給報表 Job；回應時間用 `http_req_duration{flow:faq|business}`（開場問候 `flow:init` 只單獨列出）。

| 參數 | 預設 | 說明 |
|---|---|---|
| STAGE_PLAN | （同正式腳本） | 用 TPS 寫，例如 `x3:354tps:10:1`＝1 分鐘爬升到 354 TPS、維持 10 分鐘；地端沒指定時用 `QA_STAGE_PLAN`（k6ctl 自動帶入）。1 倍、3 倍要分開跑（VU 依最高 TPS 準備，同一輪放兩段時，低的那段大多在送開場問候，提問量會偏低） |
| QA_FAQ_MAX_MS | 8000 | FAQ 回應時間門檻（毫秒） |
| QA_BUSINESS_MAX_MS | 2000 | 業務查詢回應時間門檻（毫秒） |
| QA_LATENCY_PCT | 95 | 以第幾百分位判定回應時間 |
| QA_FAIL_RATE | THRESHOLDS_JSON 的 error_rate（0.005） | 失敗率門檻 |
| QA_ABORT | false | `true`＝失敗率 > ABORT_ERROR_RATE（ABORT_DELAY 後）自動停止 |
| QA_PRE_VUS／QA_MAX_VUS | 最高 TPS × 20／× 30 | VU 數（全部 task 合計） |

報表 Job 讀同名的 `QA_*` 環境變數；原始數據有 `qa_failed`／`qa_opening_failed` 時自動產生對話報表。Cloud Run 版執行時設 `K6_SCRIPT=/app/k6/script-qa.js`。

本機：

```bash
node runner/k6/mock/mock-server.js &
mkdir -p runner/k6/results
k6 run -e BASE_URL=http://127.0.0.1:8090 -e STAGE_PLAN='x3:20tps:3:1' -e TIME_SCALE=3 -e RESULTS_DIR=runner/k6/results runner/k6/script-qa.js
```

## 階段切換事件

`stage_marker` scenario 只有 1 個 VU（多個 task 時只會落在其中一個），在每個階段開始時寫一行：

```
STRESS_TEST_EVENT=STAGE run_id=… stage=x1 pct=1 conv_per_s=118.00 api_per_s=1239 hold_min=30 time_scale=1
```

entrypoint.sh 把這行轉送到 stdout → Cloud Logging → 「進入新階段」通知（[infra/terraform/README.md](../../infra/terraform/README.md)「監控與告警」）。

## 本機執行

```bash
node runner/k6/mock/mock-server.js &          # 受測端替身，預設 http://127.0.0.1:8090
mkdir -p runner/k6/results                    # k6 不會自動建立 summary.json 的資料夾
k6 run -e BASE_URL=http://127.0.0.1:8090 -e TIME_SCALE=15 -e RESULTS_DIR=runner/k6/results runner/k6/script.js
```

多個 task＋報表的完整流程見 [scripts/local-sim.sh](../../scripts/local-sim.sh)。

## 撰寫時要沿用的事項

從 SIT 試跑與環境調查（[docs/investigation.md](../../docs/investigation.md)）得到的：

- **不跟隨轉址（`redirects: 0`）：** Cloud Armor 對非白名單 IP 回 302 導向維護頁，跟隨轉址會變成 HTTP 200 的 HTML，被誤判成功。
- **只有預期的狀態碼算成功：** BFF 用 205（護欄擋下）、206（AI 無法生成）回錯誤，只有 B5 預期 205。
- **reCAPTCHA：** 壓測期間由環境負責人暫時關閉，腳本不做任何繞過。
- **訪客每日次數（`maxCountGuest`）以 ip 計算：** 測試身分的 ip 用 RFC 2544 保留網段 `198.18.0.0/15`，不與真實使用者重疊。
- **前端請求（SIMULATE_FRONTEND，依前端與 BFF 程式 2026-10-20）：** 每段對話除了 `/api/chat/ask`，還會送出：
  - 進站：`GET /api/redirect?Channel=web&isWeb=1`（帶 `X-Forwarded-For: <測試身分 ip>`，BFF 會把它當成訪客 IP 存進 Redis）→ 302 取得 sid
  - 載入頁面（同時送出）：維護檢查、`/api/user/session-info/:sid`（換回 ip／UDID／Channel，之後的 `/ask` 都用這些值）、招呼語 `/api/dynamicType/intro`、3 個 Drawer 的 `get-dynamic-type`（typeId 2、6、7）、reCAPTCHA siteKey；有設定 `FRONTEND_URL` 時另外 GET 頁面 HTML
  - 每個訊息泡泡掛載時 `get-dynamic-type` typeId 3：開場 1＋AI 回覆數、每則訊息 2（使用者＋loading）＋AI 回覆數

  一段對話約 49 個 BFF 請求（聊天 API 10.5 個）。這些請求的 `flow` 為 `entry`／`page`／`bubble`，報表另外統計，不計入聊天 API 的 TPS；`STAGE_PLAN` 的 tps 仍指聊天 API。不含 JS／CSS 等靜態檔與 Google reCAPTCHA 本身的請求。
- **B2-2、C1 實際呼叫的 API 待確認：** 目前都走 `/api/chat/ask`；若 API 盤點（[docs/api-inventory.md](../../docs/api-inventory.md)）確認還會呼叫其他 API（例如 `/api/chat/tpi`），在 `lib/` 加對應的呼叫，並維持 `flow`／`case` tag。
