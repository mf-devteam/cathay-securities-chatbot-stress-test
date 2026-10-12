# 分散式壓測執行與報表機制

對應「新阿發 壓力測試規劃 v1.4」p.16（工具與壓測機）。目標是**只用 k6 原生功能**，把要建置的 infra 降到最少：

- Cloud Run Jobs
- 一個 GCS bucket
- Cloud NAT 固定 IP

## 架構

```
                          ┌──────────────── 壓測專案（與受測系統不同的 GCP 專案） ────────────────┐
                          │                                                                       │
 手動執行                  │  Cloud Run Job「k6-runner」（N 個 task 平行）                          │
 gcloud run jobs execute ─┼─►  task-000 … task-(N-1)：各跑 1/N 負載（k6 execution segment）       │
                          │      │ k6 --out csv=raw.csv.gz（容器本機）                            │
                          │      │ 每 5 分鐘上傳一次（覆蓋）＋結束時最後上傳                         │
                          │      ▼                                                                │
                          │  GCS  gs://<bucket>/runs/<RUN_ID>/                                    │
                          │        raw/task-NNN.csv.gz   status/task-NNN.json   tasks/task-NNN/…  │
                          │      ▲                    │                                           │
 需要看報告時              │      │ 讀取目前最新的檔案   ▼                                           │
 Console 按「執行」───────┼─►  Cloud Run Job「k6-report」（Python）                               │
                          │        合併重算 → reports/report-<時間>.html、latest.html             │
                          └───────────────────────────────────────────────────────────────────────┘
          k6 流量經 Cloud NAT 固定 IP ──► 受測系統（外部網域 → Load Balancer／Cloud Armor 白名單）
 使用者：從 GCS Console 下載 HTML（或用期限連結）
```

## 設計決策

| 決策 | 選擇 | 理由 |
|---|---|---|
| k6 輸出格式 | **原生 CSV（gzip）** | k6 內建，不用另外建 Prometheus 或 Grafana；壓縮後每個請求約 86 bytes（見 [measurements.md](measurements.md)） |
| 原始數據放哪裡 | **先寫容器本機，再定時上傳 GCS** | 直接把 GCS 掛載成資料夾讓 k6 寫入時，檔案通常要等關閉後才出現在 GCS，壓測中途讀不到（建置時會實際驗證） |
| 為什麼不走 Cloud Logging | 不採用 | sink 到 GCS 是每小時批次寫入，第一批可能要 2～3 小時才出現；而且 3 倍負載時每秒約 4.5 萬筆 log，有被限流或掉資料的風險 |
| 為什麼不放 BigQuery | 不採用 | 報表 Job 直接讀 CSV 就夠了；少建一個服務 |
| 百分位數怎麼算 | **合併全部原始數據重算** | 各 task 的 p95 不能平均；k6 dashboard 也不能合併多份 |
| 報告什麼時候產生 | **手動執行報表 Job** | 壓測中途想看就按一次，產出「截至目前」的報告；不用一直跑的服務 |
| 報告格式 | **單一 HTML（內嵌 SVG）** | 下載後離線可開，可以直接附在壓測報告裡 |
| 各 task 的 k6 dashboard | 保留，當附件 | 只代表 1/N 負載，用來排查個別壓測機的問題，不當正式數字 |
| 教學輔助 | 報表 Job 一併產生 | 合併報告附閱讀指南與名詞解釋；各 task 的 dashboard 自動產生中文說明的教學版，報告可以直接連過去。看報告的人不需要先學 k6 |

## 資料流與時間差

1. k6 每秒把指標寫進容器本機的 `raw.csv.gz`。
2. 壓測機每 `UPLOAD_INTERVAL_S`（預設 300 秒）上傳一次，覆蓋 GCS 上的同名檔案。
3. 報表 Job 讀取當下的版本：
   - 寫到一半的 gzip 照樣讀，最後不完整的一行會丟掉。
   - 讀取時鎖定物件版本（generation），避免讀到一半時檔案被覆蓋。
4. **報告最多比實際進度晚「一個上傳間隔 ＋ gzip 區塊延遲」**，報告上的「資料截至」會標出實際時間。

**gzip 區塊延遲（本機模擬實測）：**
- k6 的 `csv.gz` 輸出會分區塊寫入：壓縮資料累積到一定量（約 30 KB）才寫到磁碟。
- 模擬開始的第 1 分鐘，上傳的檔案只有 10 bytes，也就是只有 gzip 檔頭；約 2 分鐘後才出現第一個區塊（約 30 KB）。
- 正式規模下每個 task 每秒有數百個請求，延遲只有幾秒。
- 只有極低流量時（例如 Smoke 1% 再分給多個 task）可能延遲數分鐘。Smoke 時段看到的資料較少是正常的。
- 壓測結束時 k6 會把剩餘資料全部寫出，所以最終報告是完整的。

## 報告怎麼分階段

k6 腳本依執行時間替每個請求加上 `stage`（smoke、low5 … x3、recovery）和 `phase`（ramp／hold）的 tag，這些 tag 會寫進 CSV。報表 Job 直接依 tag 分組：

- **各階段判定只計 `hold`（持續期間）。**爬升期間不列入判定，但仍計入整輪統計。
- **FAQ／業務查詢依 `flow` tag 分開統計。**情境代號用 `case` tag（例如 B2、C1）。

## 本機模擬

[scripts/local-sim.sh](../scripts/local-sim.sh) 用本機資料夾 `.local-gcs/` 模擬 GCS，同時跑 3 個 task 打 mock server，並在壓測中途和結束後各產生一次報告。結果記錄在 [measurements.md](measurements.md)。

## 部署前待確認

| 項目 | 說明 |
|---|---|
| `grafana/k6:2.3.0` 映像檔 | tag 是否存在（與本機開發版本一致） |
| GCS 掛載行為 | 確認 Cloud Storage 掛載資料夾無法在寫入中被讀取（目前設計已避開，不依賴此行為） |
| 每個 task 的 VU 數與記憶體 | Smoke 實測後修正（試算表假設 4,000 VU／task） |
| 報表 Job 處理時間 | 正式規模（約 2,000 萬次請求）的處理時間 |
| IAM | runner：bucket 寫入；report：bucket 讀取與寫入 `reports/`；下載者：bucket 讀取或期限連結 |
| 正式 k6 腳本 | 目前映像檔放的是 mock 腳本；業務查詢的真實 API 流程（是否另呼叫 `/api/chat/tpi`）待確認 |
