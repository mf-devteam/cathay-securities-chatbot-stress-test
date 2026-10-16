# k6 腳本

整個 `k6/` 目錄會打包進壓測機映像檔（[Dockerfile](../Dockerfile)），預設執行正式腳本 `script.js`。所有參數都是環境變數：在 Cloud Run 上寫在 Job 定義（Terraform）或執行時用 `--update-env-vars` 覆寫，本機用 `k6 run -e 名稱=值`。

```
k6/
├── script.js          入口：k6 options（逐階加壓、門檻）、一位訪客的完整對話、摘要
├── lib/
│   ├── config.js      讀取全部環境變數與預設值（下方參數一覽）
│   ├── stages.js      STAGE_PLAN → 逐階加壓、stage／phase tag、階段切換事件
│   ├── scenarios.js   業務情境與權重
│   └── chat.js        POST /api/chat/ask、結果分類、自訂指標
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
| CHANNEL／DEVICE | web／Web | `/ask` body 的 `channel`、`device` |
| RUN_ID | local | 本輪代號；Cloud Run 上由 entrypoint.sh 帶入 |
| **負載模型**（試算表 01 分頁） | | |
| BASE_CONV_RATE | 118 | 1 倍的每秒新開對話數 |
| MSGS_MIN／MSGS_MAX | 8／11 | 每段對話的訊息數（不含開場） |
| THINK_TIME_S | 20 | 訊息間的思考時間（秒） |
| THINK_JITTER | 0.25 | 思考時間 ± 比例 |
| CONV_P95_S | 391 | 對話 p95 時長（秒），用來估 VU 數 |
| **逐階加壓**（試算表 02 分頁） | | |
| STAGE_PLAN | `smoke:0.01:10:1;low5:0.05:15;low10:0.1:15;baseline:0.25:15;mid50:0.5:15;x1:1:30;recovery:0.25:15` | `代號:佔1倍:持續分鐘[:爬升分鐘]`，分號分隔。一輪約 134 分鐘 |
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
- **B2-2、C1 實際呼叫的 API 待確認：** 目前都走 `/api/chat/ask`；若 API 盤點（[docs/api-inventory.md](../../docs/api-inventory.md)）確認還會呼叫其他 API（例如 `/api/chat/tpi`），在 `lib/` 加對應的呼叫，並維持 `flow`／`case` tag。
