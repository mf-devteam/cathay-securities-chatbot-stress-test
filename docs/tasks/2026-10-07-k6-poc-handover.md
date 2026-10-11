# 任務：新阿發 k6 輕量試跑（UAT）

## 目的

用 k6 對 UAT 的聊天 API（/ask）打**極輕量**的流量，目的只是確認三件事：

1. k6 腳本能正確走完「開啟對話 → 送出訊息 → 收到完整回應」流程
2. k6 內建 Web Dashboard 能產出 HTML 報告，並看得到回應時間 p50／p95／p99、錯誤率、每秒處理量
3. 評估這種報告格式是否適合作為正式壓測報告的基礎

**這不是壓力測試。** 不追求負載量，任何會對 UAT 造成壓力的設定都不允許。

## 硬性限制（必須遵守）

| 項目 | 上限 |
|---|---|
| 負載 | 每 10 秒 1 則訊息（0.1 msg/s） |
| 總執行時間 | 3 分鐘（含爬升） |
| /ask 總呼叫次數 | 約 20 次，不得超過 30 次 |
| 同時虛擬使用者（VU） | preAllocatedVUs 2，maxVUs 5 |
| 單一請求 timeout | 60 秒 |
| 自動中止 | 錯誤率 > 20%，或出現任何 429，立即中止 |

其他禁止事項：

- 不得修改 BFF、Middleware 或任何 repo 的程式碼與設定。
- 不得修改 UAT 的任何雲端設定（Cloud Armor、Cloud Run、reCAPTCHA 等）。
- 不得繞過 reCAPTCHA 或任何驗證機制；如果無法以正常方式取得可用的 token，**停下來回報，不要自行想辦法繞過**。
- 不得把帳號、密碼、token、API key 寫進檔案或 commit；一律用環境變數。
- 不得重複執行；每次執行前都要先問我。

## 步驟一：先調查，不要執行（完成後回報給我確認）

BFF repo 位於本機：`/Users/jason.hsiao/Documents/git/cathay-securities-chatbot-bff`（Node.js／TypeScript + Express）。

請只讀取程式碼，整理以下資訊回報給我：

| 要確認的項目 | 說明 |
|---|---|
| /ask 的完整路徑、HTTP method | — |
| /ask 的 request body 與必要 header | 欄位名稱、型別、範例（不要放真實資料） |
| 開啟對話（建立 session）需要呼叫哪支 API | 回傳的 session id 或 token 在哪裡 |
| 是否需要登入（SSO／OTP）才能呼叫 /ask | FAQ 問答是否可不登入 |
| reCAPTCHA 驗證在哪一層、怎麼帶 token | UAT 是否有測試金鑰或測試模式設定（只讀設定，不修改） |
| 單一對話的聊天次數上限 | 設定值與超過時的回應 |
| /ask 的回應是一次回完整內容，還是 streaming（SSE／WebSocket） | 決定 k6 如何計時 |
| 成功回應的判斷方式 | HTTP status 與回應內容中的哪個欄位 |

如果有任何一項無法從程式碼確認，請標示「待確認」，不要猜。

**回報後等我確認，才進行步驟二。**

## 步驟二：建立腳本（不執行）

目錄結構：

```
k6-poc/
├── README.md            # 安裝、設定、執行方式
├── script.js            # k6 腳本
├── .env.example         # 環境變數範例（不含真實值）
├── data/
│   └── questions.json   # 測試問句（我提供，先放 3 筆 placeholder）
└── results/             # 執行結果輸出（加入 .gitignore）
```

腳本要求：

- k6 版本需 v0.49 以上（內建 Web Dashboard）；若本機沒有，告訴我安裝指令，由我決定是否安裝。
- 使用 `constant-arrival-rate` executor：`rate: 1`、`timeUnit: '10s'`、`duration: '3m'`、`preAllocatedVUs: 2`、`maxVUs: 5`。
- 先只做 **FAQ 問答**一個 scenario；如果 FAQ 問答也必須登入，回報給我，不要自行處理登入。
- 每個請求加上 tag：`flow: faq`，報表要能依 flow 分開看。
- 依步驟一查到的聊天次數上限，在達到上限前開新對話。
- `summaryTrendStats` 設為 `['avg','min','med','max','p(90)','p(95)','p(99)']`。
- thresholds：
  - `http_req_failed{flow:faq}`: `rate<0.2`，設定 `abortOnFail: true`
  - 遇到 HTTP 429 立即中止
  - 回應時間只做觀察，不設中止條件
- 用 `check()` 驗證回應成功（依步驟一確認的判斷方式）。
- 用 `handleSummary()` 輸出 `results/summary.json`，並在 console 印出精簡摘要。
- 環境變數：`BASE_URL`、以及步驟一確認需要的其他值。

**建立完成後，把 script.js 內容給我看，等我確認，才進行步驟三。**

## 步驟三：執行（我確認後才執行）

```bash
K6_WEB_DASHBOARD=true \
K6_WEB_DASHBOARD_EXPORT=results/report.html \
k6 run --out json=results/raw.json script.js
```

- 執行期間如果出現 429、大量 5xx 或 timeout，立即中止（Ctrl+C）並回報。
- 只執行一次。

## 步驟四：整理結果

產出 `k6-poc/RESULT.md`，內容：

| 項目 | 內容 |
|---|---|
| 執行時間、k6 版本、執行環境 | — |
| /ask 呼叫次數、成功數、失敗數、錯誤率 | — |
| 回應時間 | avg、min、p50、p90、p95、p99、max |
| 對照 | 與 10/7 前端實測比較（平均 9.57 秒、最長 20.25 秒、最短 2.85 秒） |
| 報告評估 | `results/report.html` 的 Dashboard 有哪些圖表、哪些適合放進正式壓測報告、還缺什麼 |
| 問題與限制 | 執行過程遇到的問題 |

最後告訴我 `results/report.html` 的位置，我要用瀏覽器打開看效果。