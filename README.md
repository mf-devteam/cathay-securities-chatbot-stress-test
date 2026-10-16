# 新阿發 壓力測試（k6）

國泰證券「新阿發」AI 客服的壓力測試工具與文件。依據「新阿發 壓力測試規劃 v1.4」開發：

- 工具：k6
- 執行：Cloud Run Jobs 多個 task 平行
- 報表：原始數據存 GCS，合併後產生報告

## 目錄

```
.
├── runner/             Cloud Run Job「k6-runner」（壓測機）
│   ├── Dockerfile
│   ├── entrypoint.sh   execution segment 分流、CSV 定時上傳 GCS
│   └── k6/             k6 正式腳本（script.js、lib/、data/）
│       └── mock/       本機 mock server（受測端替身）
├── report-job/         Cloud Run Job「k6-report」（合併報告）
├── infra/terraform/    GCP 資源（Job、IAM、GCS、網路）的 IaC
├── scripts/            本機模擬整套流程（local-sim.sh）、通知 dry run（notify-dry-run.sh）
└── docs/               文件
```

| 路徑 | 內容 |
|---|---|
| [runner/](runner/) | 壓測機（Cloud Run Jobs）：容器、啟動腳本與 [k6 腳本](runner/k6/)。業務情境依「業務情境組合」黃色列；[runner/k6/mock/](runner/k6/mock/) 是本機 mock server |
| [report-job/](report-job/) | 壓測報表 Job（Python，Cloud Run Jobs，手動執行）：合併所有 task 的原始數據，產生單一 HTML 報告 |
| [infra/terraform/](infra/terraform/) | Terraform：Cloud Run Jobs、service account／IAM、GCS bucket、Artifact Registry、VPC／Cloud NAT、通知與告警 |
| [scripts/](scripts/) | 本機模擬整套流程（mock → 多個 runner task → report-job）；通知 dry run |
| [docs/](docs/) | 文件：環境調查、API 盤點、架構、量測紀錄、dashboard 教學、交接 |

SIT 連線試跑腳本（`k6-poc/`）已移除，需要時見 git 歷史（commit 560a85b）。

## 快速開始（本機，不連任何環境）

需要 k6（v2.3.0）、Node.js、Python 3.10 以上。

```bash
# 1. 只看 k6 dashboard：mock server + 逐階加壓到 1 倍（時間 ÷15，約 9 分鐘）
node runner/k6/mock/mock-server.js &
mkdir -p runner/k6/results
K6_WEB_DASHBOARD=true K6_WEB_DASHBOARD_OPEN=true \
K6_WEB_DASHBOARD_EXPORT=runner/k6/results/report.html \
  k6 run -e BASE_URL=http://127.0.0.1:8090 -e TIME_SCALE=15 -e RESULTS_DIR=runner/k6/results runner/k6/script.js

# 在匯出的 dashboard 上加中文教學說明
python3 report-job/annotate.py runner/k6/results/report.html

# 2. 模擬正式機制：3 個 task → 定時上傳（本機資料夾模擬 GCS）→ 合併報告（約 4 分鐘）
scripts/local-sim.sh
```

## 雲端壓測操作

前提：已用 [infra/terraform/](infra/terraform/README.md) 部署；受測端已暫停 reCAPTCHA、Cloud Armor 已加入 NAT IP 白名單。

```bash
REGION=<region>; BUCKET=<bucket>; RUN_ID=20261020-baseline

# 1. 開始壓測
gcloud run jobs execute k6-runner --region $REGION \
  --update-env-vars RUN_ID=$RUN_ID

# 2. 看進度
gcloud run jobs executions list --job k6-runner --region $REGION

# 3. 產生報告（壓測中、結束後都可以）
gcloud run jobs execute k6-report --region $REGION \
  --update-env-vars SOURCE_URI=gs://$BUCKET/runs/$RUN_ID

# 4. 下載報告
gcloud storage cp gs://$BUCKET/runs/$RUN_ID/reports/latest.html .
```

**中途停止（全部 task 一起停）：**

```bash
gcloud run jobs executions list --job k6-runner --region $REGION     # 找 execution 名稱
gcloud run jobs executions cancel <execution 名稱> --region $REGION
```

- 停止前已上傳的數據會保留，可以照步驟 3 產生「截至停止時」的報告。
- 最後一個上傳間隔（預設 5 分鐘）內的數據可能遺失。

## 文件

- [docs/README.md](docs/README.md)：文件索引
- [docs/handover-claude-desktop.md](docs/handover-claude-desktop.md)：給 Claude Desktop 製作客戶簡報用的交接

## 安全注意事項

- **k6 腳本預設只能打本機：** [runner/k6/script.js](runner/k6/script.js) 的 `BASE_URL` 不是 `127.0.0.1`／`localhost` 時，必須另外設定 `ALLOW_REMOTE_TARGET=true`（由 Job 定義設定），避免本機誤打真實環境。
- **對任何真實環境執行前，都要先取得負責人同意，**並遵守 [docs/tasks/2026-10-07-k6-poc-handover.md](docs/tasks/2026-10-07-k6-poc-handover.md) 的限制。
- **不要把帳號、token、金鑰寫進檔案或 commit；**一律用環境變數。
