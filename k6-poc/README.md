# 新阿發 /ask k6 輕量試跑

對聊天 API `POST /api/chat/ask` 打極輕量流量（每 10 秒 1 次、3 分鐘、約 18 次），用來驗證 k6 流程與 HTML 報告格式。**這不是壓力測試。** 硬性限制見 [docs/tasks/2026-10-07-k6-poc-handover.md](../docs/tasks/2026-10-07-k6-poc-handover.md)，API 調查結果見 [docs/investigation.md](../docs/investigation.md)。

## 安裝

需要 k6 v0.49 以上（內建 Web Dashboard）。本機已安裝 v2.3.0。

```bash
brew install k6
k6 version
```

## 設定

```bash
cp .env.example .env   # 填入 BASE_URL；.env 不會被 commit
```

| 變數 | 必填 | 說明 |
|---|---|---|
| BASE_URL | 是 | BFF 對外入口（負載平衡器位址，不含 `/api`） |
| RECAPTCHA_TOKEN | 否 | 未設定就送空字串，BFF 會回 403 E1010（見下方說明） |
| CHANNEL | 否 | 預設 `web` |
| MAX_QUESTIONS_PER_USER | 否 | 每個測試身分提問幾則就換新身分，預設 3（訪客每日上限 4） |

## 執行

**每次執行前都要先取得負責人同意，只執行一次。** 必須從 Cloud Armor 白名單網路執行，否則所有請求都會被 302 導向維護頁。

```bash
cd k6-poc
mkdir -p results
set -a; source .env; set +a
K6_WEB_DASHBOARD=true \
K6_WEB_DASHBOARD_EXPORT=results/report.html \
k6 run --out json=results/raw.json script.js
```

執行期間出現 429、大量 5xx 或逾時，請立即按 Ctrl+C 中止。腳本也會在錯誤率 > 20% 或出現任何 429 時自動中止。

## 腳本行為

- **情境：** FAQ 問答（訪客、不登入），所有請求都帶 tag `flow: faq`，另以 `step: init`／`step: ask` 區分開場問候與一般提問。
- **對話流程：** 每個 VU 開一個對話，先送一次開場問候（`isInitialized: true`），之後每次 iteration 送一則問句。問滿 `MAX_QUESTIONS_PER_USER` 則就換新的測試身分、開新對話。
- **測試身分：** `ip` 取自 RFC 2544 測試保留網段 `198.18.0.0/15`，`udid` 為 `k6-test-<sessionId>`，方便在 log 裡篩選。每個對話用一組新身分，模擬多位使用者各自發問，不會碰到單一身分的每日上限。
- **成功判斷：** HTTP 200、body `status === 200`、`data` 非空，一般提問還要有非空的 FAQ `content`。BFF 有些錯誤用 205／206 回，腳本只把 HTTP 200 算成功。
- **不跟隨轉址：** 被 Cloud Armor 導向維護頁時會直接算成失敗。
- **結束時間：** 3 分鐘後停止送出新請求，還在等待回應的請求最多再等 30 秒（k6 預設 gracefulStop）。

## 產出

| 檔案 | 內容 |
|---|---|
| `results/report.html` | k6 Web Dashboard 匯出的 HTML 報告 |
| `results/summary.json` | 完整統計（handleSummary 輸出） |
| `results/raw.json` | 每個請求的原始資料 |

`results/` 已加入 `.gitignore`。

## 關於 reCAPTCHA

BFF 會對每次 `/ask` 驗證 reCAPTCHA Enterprise token。token 只能由瀏覽器產生、2 分鐘內有效且只能驗證一次，k6 無法以正常方式取得。不關閉 reCAPTCHA 的話，所有請求都會在 BFF 被擋下（403 E1010），不會到達 AI；錯誤率超過門檻，腳本會在第一次評估時自動中止。詳見 [docs/investigation.md](../docs/investigation.md) 第 4 節。
