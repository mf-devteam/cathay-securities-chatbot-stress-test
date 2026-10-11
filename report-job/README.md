# 壓測報表 Job（Cloud Run Jobs，手動執行）

需要看報告時，到 Cloud Run Console 按「執行」（或執行 `gcloud run jobs execute`）。程式會讀取所有壓測機（task）**目前已上傳**的 k6 原始數據，合併重算後產生一份 HTML 報告，存回 GCS 供下載。壓測進行中或結束後都可以執行。整體機制見 [docs/architecture.md](../docs/architecture.md)。

## 產出

```
gs://<bucket>/<prefix>/<RUN_ID>/reports/
├── report-YYYYMMDD-HHMMSS.html   每次執行一份（台北時間）
├── latest.html                   最新一份
└── summary-YYYYMMDD-HHMMSS.json  精簡數字（給後續分析或比較用）

gs://<bucket>/<prefix>/<RUN_ID>/tasks/<task>/
└── dashboard-annotated.html      各 task k6 dashboard 的教學版（每次執行時重新產生）
```

HTML 是單一檔案，圖表為內嵌 SVG，下載後不需要網路就能開啟。內容包括：

- **總覽：** 請求數、成功數、錯誤率、FAQ／業務 p95、完成對話數。
- **各階段判定：** 只計各階段的持續期間，對照參考門檻標示 ✓／✗。
- **趨勢圖：**
  - 送出量 vs 成功量
  - 回應時間 p50／p95／p99
  - FAQ vs 業務 p95
  - 錯誤率
  - VU 數

  每張圖都以背景色塊標示加壓階段。
- **明細：** 各階段 × 類別的 avg、p50、p90、p95、p99、max、錯誤率、429、業務錯誤。
- **各 task 的狀態：** running 或 finished、資料截至時間、是否還在寫入中，以及該 task k6 dashboard 的連結（教學版／原始）。
- **教學輔助：**
  - 頁首「閱讀指南」（三步驟）
  - 可展開的「名詞解釋」
  - 每個區塊的「怎麼看」說明
  - 右上角按鈕可以一次隱藏全部說明（列印前使用）

## 各 task dashboard 的教學版

每次執行時，程式會讀取已結束 task 上傳的 `tasks/<task>/dashboard.html`，用 [annotate.py](annotate.py) 加上中文說明框後，另存為 `dashboard-annotated.html`；原檔不修改。

- 頁首標明「只代表這個 task 約 1/N 的流量」。
- 合併報告用相對路徑連結過去：在 GCS 或本機資料夾開啟時可以直接點；單獨下載合併報告時，要連同 `tasks/` 一起下載。
- 設定 `ANNOTATE_DASHBOARDS=false` 可以關閉。

本機單獨使用同一份說明：`python3 scripts/annotate-dashboard.py <report.html>`。

## 正確性

- **百分位數用全部請求的原始數據重算，**不是各 task 的 p95 平均（平均出來的數字是錯的）。算法與 k6 相同，都是線性內插。
- **可以讀寫入中的檔案：** 壓測機上傳的 gzip 檔可能還沒寫完，程式讀到哪算到哪，最後不完整的一行會丟掉。
- **讀 GCS 時鎖定物件版本（generation），**讀取中途壓測機覆蓋上傳也不會讀到混合內容。
- **請求結果分類與 k6 腳本一致：**
  - 錯誤 = 5xx、timeout（status 0／408／504）
  - 429 另外計
  - 符合預期回應（`expected_response`）= 成功
  - 其他 = 業務錯誤

## 參數

| 環境變數 | 命令列 | 說明 |
|---|---|---|
| SOURCE_URI | `--source` | 必填。`gs://<bucket>/<prefix>/<RUN_ID>` 或本機資料夾 |
| RUN_ID | `--run-id` | 顯示用，預設取路徑最後一段 |
| EXPECTED_TASKS | `--expected-tasks` | 預期 task 數；缺少 task 時會提示 |
| REPORT_TITLE | `--title` | 預設「新阿發 壓力測試報告」 |
| ANNOTATE_DASHBOARDS | — | 預設 `true`：替各 task 的 k6 dashboard 產生教學版 |
| THRESHOLDS_JSON | — | 參考門檻，預設為規劃 v1.4 的證券方參考值：`{"faq":{"p95":10000,"p99":20000},"business":{"p95":5000,"p99":10000},"error_rate":0.005}` |

## 本機執行

```bash
cd report-job
python3 -m unittest discover -s tests          # 單元測試（不需要安裝套件）
python3 main.py --source ../.local-gcs/runs/<RUN_ID>   # 讀本機資料夾不需要 google-cloud-storage
```

完整的本機模擬流程見 [scripts/local-sim.sh](../scripts/local-sim.sh)。

## 部署（草案，尚未在 GCP 驗證）

```bash
gcloud run jobs deploy k6-report \
  --source report-job --region <region> \
  --cpu 4 --memory 8Gi --task-timeout 30m --max-retries 0 \
  --service-account <report-sa> \   # 需要 bucket 的讀取與寫入（reports/）權限
  --set-env-vars SOURCE_URI=gs://<bucket>/runs/<RUN_ID>,EXPECTED_TASKS=<N>

# 每輪壓測換 RUN_ID 時，可以在執行時覆寫：
gcloud run jobs execute k6-report --region <region> \
  --update-env-vars SOURCE_URI=gs://<bucket>/runs/<RUN_ID>
```

**記憶體：** 每個請求在記憶體中約佔 75 bytes（原始欄位，加上依階段、類別、時間區間分組的回應時間複本），排序時另外需要暫存。一整輪約 2,000 萬次請求，估計需要 2～4 GB，建議配置 8 GiB。

**執行時間：** 會隨數據量增加，可能需要數分鐘。正式規模的處理時間待實測。

**下載方式：**
- 有 bucket 讀取權限的人，可以從 GCS Console 直接下載。
- 要給沒有 GCP 權限的人，另外產生期限連結（signed URL），例如 `gcloud storage sign-url`，需要具備簽署權限的 service account。
