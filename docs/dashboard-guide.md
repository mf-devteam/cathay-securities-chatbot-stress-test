# k6 Web Dashboard 怎麼看

k6 內建的 Web Dashboard 有兩種用法：

| 用法 | 怎麼開 | 特性 |
|---|---|---|
| **即時 dashboard** | 執行時設 `K6_WEB_DASHBOARD=true`，瀏覽器開 http://127.0.0.1:5665 | 每 10 秒自動更新，k6 結束後就關閉 |
| **匯出的 HTML 報告** | 再加 `K6_WEB_DASHBOARD_EXPORT=report.html` | k6 結束時存成單一 HTML，可以離線打開、轉寄 |

匯出的 HTML 可以加上中文教學說明（黃色說明框，右上角按鈕可以隱藏）：

- **正式壓測：** 報表 Job 每次執行時，會自動替每個已結束 task 的 dashboard 產生教學版（`tasks/<task>/dashboard-annotated.html`），並在頁首標明「只代表這個 task 約 1/N 的流量」。合併報告的「資料源」表裡有連結。
- **本機：**

```bash
python3 report-job/annotate.py runner/k6/mock/results/report.html
# → runner/k6/mock/results/report-annotated.html
```

合併報告本身也有教學輔助：頁首有「閱讀指南（三步驟）」和可展開的「名詞解釋」，每個區塊附「怎麼看」說明，右上角按鈕可以一次隱藏全部說明（列印或貼進正式報告前使用）。

範例（本機 mock，2026-10-11 完整加壓曲線，數字見 [measurements.md](measurements.md) M4）：
- 教學版 dashboard：[samples/2026-10-11-mock-full/k6-dashboard-annotated.html](samples/2026-10-11-mock-full/k6-dashboard-annotated.html)
- 截圖：[images/k6-dashboard-annotated.png](images/k6-dashboard-annotated.png)
- 對照用的合併報告（3 個 task）：[samples/2026-10-11-local-sim/reports/merged-report-final.html](samples/2026-10-11-local-sim/reports/merged-report-final.html)（頁尾「資料來源」表可以點到 3 個 task 的教學版 dashboard）

---

## 先知道這三件事

1. **dashboard 的數字都是「全部請求合在一起」。**
   - 沒有分 FAQ 問答和業務查詢。
   - 沒有分加壓階段。
   - 規劃要求的分類統計與各階段判定，請看報表 Job 產出的報告（[report-job/](../report-job/)）。
2. **橫軸是時間，加壓階段要對照時間來看。**
   - 逐階加壓的順序是 Smoke 1% → 5% → 10% → Baseline 25% → 50% → 1 倍 → 2 倍 → 3 倍 → Recovery 25%。
   - 從 VU 圖的階梯就能看出目前在哪一階。
3. **在 Cloud Run 上分多個 task 跑時，每個 task 都有自己的 dashboard，只代表 1/N 的負載。**
   - 正式數字要看合併後的報告。
   - 各 task 的 dashboard 用來排查個別壓測機的問題。

---

## Overview（總覽）

| 圖表 | 看什麼 | 怎麼判斷 |
|---|---|---|
| **HTTP Performance overview** | 每秒請求數（Request Rate）、p95 回應時間、失敗比例，三條線放在同一張圖 | 請求量上升時 p95 也明顯上升 → 系統開始吃力；Failed 離開 0 → 開始出錯 |
| **VUs** | 同時在對話中的虛擬使用者數，以及每秒請求數 | 應該隨加壓階段階梯式上升。VU 卡在上限不再增加、Summary 又出現 `dropped_iterations` → 壓測機不夠力，不是受測系統的問題 |
| **Transfer Rate** | 每秒收／送的網路流量 | 壓測報告通常不需要；用來確認壓測機網路不是瓶頸 |
| **HTTP Request Duration** | 回應時間 avg、p90、p95、p99 隨時間的變化 | p50 穩定但 p99 上升 → 少數請求卡住；全部一起上升 → 整體變慢。規劃要的 p50 在 Summary 表的 `med` 欄 |
| **Iteration Duration** | 一次 iteration 的時間。本腳本中一次 iteration 等於**一整段對話**（開場 + 8～11 則訊息 + 思考時間） | 數值是一段對話要多久（數分鐘），**不是回應時間**，不要拿來判定 |

## Timings（時間拆解）

用來找出「時間花在哪裡」。一個請求的時間 = Sending（送出）+ **Waiting（等伺服器處理）** + Receiving（收回應）。

| 圖表 | 意義 | 偏高時代表 |
|---|---|---|
| **Request Duration** | 請求總時間（與 Overview 相同） | — |
| **Request Failed Rate** | 非預期回應的比例（本腳本只把 200、205 視為預期） | **包含業務錯誤**，跟規劃的「錯誤率（5xx + timeout）」不同。規劃的錯誤率要看 Summary 的 `errors_5xx_timeout` |
| **Request Rate** | 每秒送出的請求數，也就是規劃的「每秒處理量（送出量）」 | 成功量看 Summary 的 `chat_success` |
| **Request Waiting** | 等待伺服器開始回應的時間，也就是伺服器處理時間 | AI 回答慢主要反映在這裡。Duration ≈ Waiting 代表時間都花在伺服器端 |
| **TLS handshaking** | 建立 HTTPS 連線的時間 | 連線沒有重複使用，或負載平衡器負擔大（mock 走 http，所以是 0） |
| **Request Sending／Receiving** | 上傳請求／下載回應的時間 | 頻寬問題（通常很小） |
| **Request Connecting** | 建立 TCP 連線的時間 | 連線建立有瓶頸，例如 Cloud NAT 的 port 不夠 |
| **Request Blocked** | k6 等待可用連線的時間（壓測機端排隊） | 壓測機端有瓶頸 |

## Summary（整輪彙總表）

**整輪是從頭到尾全部加總，混合了 Smoke 到 3 倍所有階段，不能代表某一個階段的容量。**

| 表 | 重點列 | 說明 |
|---|---|---|
| **Trends** | `http_req_duration` | 回應時間：avg 平均、**med 中位數（= p50）**、p90、p95、p99、min、max |
| | `iteration_duration` | 一段對話的時長，不是回應時間 |
| **Counters** | `http_reqs` | 送出量（count 是總數，rate 是整輪平均每秒） |
| | `chat_success` | 成功量（自訂指標） |
| | `conversations_completed` | 完成的對話數 |
| | `dropped_iterations` | **大於 0 代表壓測機來不及開新對話**，實際負載低於設定 |
| | `business_errors`、`http_429` | 只有發生時才會出現；出現 429 依規劃要立即停止 |
| **Rates** | `errors_5xx_timeout` | **規劃定義的錯誤率**（5xx + timeout） |
| | `http_req_failed` | 含業務錯誤，不等於規劃的錯誤率 |
| | `checks` | 回應檢查通過率 |
| **Gauges** | `vus`、`vus_max` | 結束當下的 VU 數、最多建立的 VU 數 |

---

## 規劃 v1.4 指標對照

| 規劃項目 | 在 dashboard 哪裡 | 在合併報告哪裡 |
|---|---|---|
| 每秒處理量（送出量） | Request Rate 圖、`http_reqs` | 圖 ①、各階段表「每秒請求」 |
| 每秒處理量（成功量） | 只有 `chat_success` 總計 | 圖 ① 綠色虛線 |
| 回應時間 p50／p95／p99 | 圖上有 p95／p99（整體），p50 在 Trends 表 | 圖 ②、明細表 |
| FAQ／業務查詢分開 | ✗ | 圖 ③、各階段表、明細表 |
| 錯誤率（5xx + timeout） | 只有 `errors_5xx_timeout` 總計 | 圖 ④、各階段表 |
| 業務錯誤另列 | 只有 `business_errors` 總計 | 明細表 |
| 各階段判定（✓／✗） | ✗ | 各階段判定表 |
| 壓測機是否足夠 | VUs 圖、`dropped_iterations` | 圖 ⑤、頁首警告 |
