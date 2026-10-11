# 新阿發 壓力測試（k6）

國泰證券「新阿發」AI 客服的壓力測試工具與文件。依據「新阿發 壓力測試規劃 v1.4」開發：

- 工具：k6
- 執行：Cloud Run Jobs 多個 task 平行
- 報表：原始數據存 GCS，合併後產生報告

## 目錄

| 路徑 | 內容 |
|---|---|
| [docs/](docs/) | 文件：環境調查、架構、量測紀錄、dashboard 教學、交接 |
| [k6-poc/](k6-poc/) | SIT 連線試跑腳本（極輕量：每 10 秒 1 次，3 分鐘） |
| [k6-mock/](k6-mock/) | 依規劃 v1.4 的壓測腳本（mock 版）與本機 mock server：逐階加壓、多輪對話、情境比例 |
| [runner/](runner/) | 壓測機（Cloud Run Jobs）的容器與啟動腳本：execution segment 分流、CSV 定時上傳 GCS |
| [report-job/](report-job/) | 壓測報表 Job（Python，Cloud Run Jobs，手動執行）：合併所有 task 的原始數據，產生單一 HTML 報告 |
| [scripts/](scripts/) | 本機模擬整套流程、在 k6 dashboard 加上教學說明 |

## 快速開始（本機，不連任何環境）

需要 k6（v2.3.0）、Node.js、Python 3.10 以上。

```bash
# 1. 只看 k6 dashboard：mock server + 完整逐階加壓（約 15 分鐘）
node k6-mock/mock-server.js &
K6_WEB_DASHBOARD=true K6_WEB_DASHBOARD_OPEN=true \
K6_WEB_DASHBOARD_EXPORT=k6-mock/results/report.html \
  k6 run -e RESULTS_DIR=k6-mock/results k6-mock/script.js

# 在匯出的 dashboard 上加中文教學說明
python3 scripts/annotate-dashboard.py k6-mock/results/report.html

# 2. 模擬正式機制：3 個 task → 定時上傳（本機資料夾模擬 GCS）→ 合併報告（約 5 分鐘）
scripts/local-sim.sh
```

## 文件

- [docs/README.md](docs/README.md)：文件索引
- [docs/handover-claude-desktop.md](docs/handover-claude-desktop.md)：給 Claude Desktop 製作客戶簡報用的交接

## 安全注意事項

- **mock 腳本只能打本機：** [k6-mock/script.js](k6-mock/script.js) 有防呆，`BASE_URL` 只接受 `127.0.0.1`／`localhost`。
- **對任何真實環境執行前，都要先取得負責人同意，**並遵守 [docs/tasks/2026-10-07-k6-poc-handover.md](docs/tasks/2026-10-07-k6-poc-handover.md) 的限制。
- **不要把帳號、token、金鑰寫進檔案或 commit；**一律用環境變數。
