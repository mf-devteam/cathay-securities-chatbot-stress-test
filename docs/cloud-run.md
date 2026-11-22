# 雲端版（Cloud Run Jobs）

> **早期版本，目前沒在用。**現在的主力是地端版，見 [onprem/README.md](../onprem/README.md)。
> 這份是雲端版的完整說明（原本分散在 `runner/`、`report-job/`、`infra/terraform/` 的 README 與根目錄 README，2026-11-23 合併）。
> k6 腳本（`runner/k6/`）和報表程式（`report-job/`）兩個版本共用，改動時兩邊都會受影響。

依「新阿發 壓力測試規劃 v1.4」：Cloud Run Jobs 多個 task 平行跑 k6，原始數據定時上傳 GCS，再由報表 Job 合併產生一份 HTML 報告。範圍是 1 倍上限、「業務情境組合」黃色列 8 項。整體機制與設計決策見 [architecture.md](architecture.md)。

目錄：

- [本機模擬（不連任何環境）](#本機模擬不連任何環境)
- [操作：跑一輪壓測](#操作跑一輪壓測)
- [壓測機（runner）](#壓測機runner)
- [報表 Job（report-job）](#報表-jobreport-job)
- [Terraform](#terraform)

| 路徑 | 內容 |
|---|---|
| `runner/Dockerfile`、`runner/entrypoint.sh` | 壓測機映像檔與每個 task 的啟動腳本 |
| `runner/k6/` | k6 腳本（兩版共用），說明見 [runner/k6/README.md](../runner/k6/README.md) |
| `report-job/` | 報表程式（兩版共用）；`Dockerfile`、`sources.py` 的 GCS 讀取只有雲端版用 |
| `infra/terraform/envs/lab/` | 雲端版的 GCP 資源（目前只有通知與告警） |
| `scripts/` | 本機模擬整套流程（`local-sim.sh`）、通知 dry run（`notify-dry-run.sh`） |

## 本機模擬（不連任何環境）

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

## 操作：跑一輪壓測

前提：已用 [infra/terraform/](#terraform) 部署；受測端已暫停 reCAPTCHA、Cloud Armor 已加入 NAT IP 白名單。

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

## 壓測機（runner）

Cloud Run Jobs 的每個 task 執行一次 [entrypoint.sh](../runner/entrypoint.sh)，各跑 1/N 的負載，並把 k6 原生的 CSV 原始數據定時上傳到 GCS。整體機制見 [docs/architecture.md](architecture.md)。

| 檔案 | 內容 |
|---|---|
| [Dockerfile](../runner/Dockerfile) | 映像檔（build context 為本目錄） |
| [entrypoint.sh](../runner/entrypoint.sh) | 每個 task 的啟動腳本 |
| [k6/](../runner/k6/README.md) | k6 正式腳本與本機 mock server，整個目錄打包進映像檔 |

### 每個 task 做的事

1. 依 `CLOUD_RUN_TASK_INDEX`／`CLOUD_RUN_TASK_COUNT`，用 k6 的 execution segment 只跑自己那一份（第 i 個 task 跑 i/N～(i+1)/N）。
2. k6 輸出 `--out csv=…csv.gz` 到容器本機（Cloud Run 的本機檔案放在記憶體）。
3. 背景每 `UPLOAD_INTERVAL_S` 秒把目前的 CSV 上傳一次，覆蓋同名檔案，讓報表 Job 隨時讀得到最新資料。
4. k6 結束或收到 SIGTERM 時做最後一次上傳，並上傳該 task 的 dashboard HTML、`summary.json` 和 console log。
5. k6 因門檻未通過而回 exit 99 時，預設改回 exit 0，避免 Cloud Run 把 task 判定失敗而重跑壓測。
6. 寫出通知事件到 stdout（→ Cloud Logging → 告警寄信）：task-000 寫 `STRESS_TEST_EVENT=START`／`END`；k6 腳本的 `STRESS_TEST_EVENT=STAGE` 轉送到 stdout，其餘 k6 輸出只寫進 `k6-console.log`。

### GCS 上的檔案

```
gs://<bucket>/<prefix>/<RUN_ID>/
├── raw/task-000.csv.gz            k6 原始數據（持續覆蓋為最新）
├── status/task-000.json           task 狀態：running／finished、最後上傳時間、k6 exit code
├── tasks/task-000/dashboard.html  該 task 的 k6 dashboard（只代表 1/N 負載）
├── tasks/task-000/dashboard-annotated.html  教學版（由報表 Job 產生）
├── tasks/task-000/summary.json
├── tasks/task-000/k6-console.log
└── reports/                       報表 Job 的輸出（見 report-job/）
```

### 環境變數

| 名稱 | 必填 | 預設 | 說明 |
|---|---|---|---|
| RUN_ID | 否 | Cloud Run 的 execution 名稱（`CLOUD_RUN_EXECUTION`） | 本輪壓測代號，例如 `20261020-baseline`；建議每次執行時用 `--update-env-vars` 指定 |
| OUTPUT_URI | 是 | — | `gs://<bucket>/<prefix>`；本機模擬時為資料夾路徑 |
| UPLOAD_INTERVAL_S | 否 | 300 | 上傳間隔（秒） |
| K6_SCRIPT | 否 | `/app/k6/script.js` | k6 腳本（映像檔內含整個 `k6/` 目錄） |
| WORK_DIR | 否 | `/tmp/k6-out` | 本機暫存目錄 |
| THRESHOLD_FAIL_EXIT_ZERO | 否 | true | exit 99（門檻未通過）是否改回 0 |
| BASE_URL、STAGE_PLAN、SCENARIO_WEIGHTS… | 依腳本 | — | 直接傳給 k6 腳本（`__ENV`），一覽見 [k6/README.md](../runner/k6/README.md) |

### 上傳方式

- **`gs://`：** 用 Cloud Run 執行身分（service account）透過 metadata server 取得 token，以 curl 呼叫 GCS JSON API 上傳。映像檔不需要安裝 gcloud。
- **本機資料夾：** 先複製成 `.part`，再改名，避免報表 Job 讀到複製到一半的檔案。

### 建置與部署（草案，尚未在 GCP 驗證）

```bash
# 在 repo 根目錄
docker build -t <region>-docker.pkg.dev/<project>/<repo>/k6-runner:<tag> runner
docker push <region>-docker.pkg.dev/<project>/<repo>/k6-runner:<tag>

gcloud run jobs deploy k6-runner \
  --image <region>-docker.pkg.dev/<project>/<repo>/k6-runner:<tag> \
  --region <region> --tasks <N> --parallelism <N> \
  --max-retries 0 \                # 不重試：重試會再跑一次壓測
  --task-timeout 6h \
  --cpu 8 --memory 32Gi \
  --service-account <runner-sa> \  # 需要對 bucket 的 storage.objects.create／delete 權限
  --network <vpc> --subnet <subnet> --vpc-egress all-traffic \  # 經 Cloud NAT 固定 IP 出去
  --set-env-vars OUTPUT_URI=gs://<bucket>/runs,BASE_URL=<受測入口>,...

# 每一輪壓測：執行一次（不需要重新部署），RUN_ID 與 task 數可以每次覆寫
gcloud run jobs execute k6-runner --region <region> \
  --update-env-vars RUN_ID=20261020-baseline \
  --tasks <N>
```

之後改用 Terraform 管理 Job 定義（見 [infra/terraform/](#terraform)）；執行仍用 `gcloud run jobs execute`。

### 待確認

- `grafana/k6:2.3.0` 映像檔 tag 是否存在（與本機開發版本一致）。
- 目前預設腳本是 mock 版（只允許打本機）；正式腳本完成後改 `K6_SCRIPT` 預設值。
- 每個 task 能承載的 VU 數與記憶體，要在 Smoke 時量測（見 [docs/measurements.md](measurements.md)）。

## 報表 Job（report-job）

需要看報告時，到 Cloud Run Console 按「執行」（或執行 `gcloud run jobs execute`）。程式會讀取所有壓測機（task）**目前已上傳**的 k6 原始數據，合併重算後產生一份 HTML 報告，存回 GCS 供下載。壓測進行中或結束後都可以執行。整體機制見 [docs/architecture.md](architecture.md)。

### 產出

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

### 各 task dashboard 的教學版

每次執行時，程式會讀取已結束 task 上傳的 `tasks/<task>/dashboard.html`，用 [annotate.py](../report-job/annotate.py) 加上中文說明框後，另存為 `dashboard-annotated.html`；原檔不修改。

- 頁首標明「只代表這個 task 約 1/N 的流量」。
- 合併報告用相對路徑連結過去：在 GCS 或本機資料夾開啟時可以直接點；單獨下載合併報告時，要連同 `tasks/` 一起下載。
- 設定 `ANNOTATE_DASHBOARDS=false` 可以關閉。

本機單獨使用同一份說明：`python3 report-job/annotate.py <report.html>`。

### 正確性

- **百分位數用全部請求的原始數據重算，**不是各 task 的 p95 平均（平均出來的數字是錯的）。算法與 k6 相同，都是線性內插。
- **可以讀寫入中的檔案：** 壓測機上傳的 gzip 檔可能還沒寫完，程式讀到哪算到哪，最後不完整的一行會丟掉。
- **讀 GCS 時鎖定物件版本（generation），**讀取中途壓測機覆蓋上傳也不會讀到混合內容。
- **請求結果分類與 k6 腳本一致：**
  - 錯誤 = 5xx、timeout（status 0／408／504）
  - 429 另外計
  - 符合預期回應（`expected_response`）= 成功
  - 其他 = 業務錯誤

### 參數

| 環境變數 | 命令列 | 說明 |
|---|---|---|
| SOURCE_URI | `--source` | 必填。`gs://<bucket>/<prefix>/<RUN_ID>` 或本機資料夾 |
| RUN_ID | `--run-id` | 顯示用，預設取路徑最後一段 |
| EXPECTED_TASKS | `--expected-tasks` | 預期 task 數；缺少 task 時會提示 |
| REPORT_TITLE | `--title` | 預設「新阿發 壓力測試報告」 |
| ANNOTATE_DASHBOARDS | — | 預設 `true`：替各 task 的 k6 dashboard 產生教學版 |
| THRESHOLDS_JSON | — | 參考門檻，預設為規劃 v1.4 的證券方參考值：`{"faq":{"p95":10000,"p99":20000},"business":{"p95":5000,"p99":10000},"error_rate":0.005}` |

### 本機執行

```bash
cd report-job
python3 -m unittest discover -s tests          # 單元測試（不需要安裝套件）
python3 main.py --source ../.local-gcs/runs/<RUN_ID>   # 讀本機資料夾不需要 google-cloud-storage
```

完整的本機模擬流程見 [scripts/local-sim.sh](../scripts/local-sim.sh)。

### 部署（草案，尚未在 GCP 驗證）

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

## Terraform

管理壓測專案的 GCP 資源。整體架構見 [docs/architecture.md](architecture.md)。目前已完成通知管道與壓測執行通知（`monitoring.tf`），其餘依下方結構陸續撰寫。

### 結構

```
infra/terraform/
├── modules/
│   ├── k6-runner/       （待撰寫）Cloud Run Job「k6-runner」+ 執行用 service account + IAM
│   └── k6-report/       （待撰寫）Cloud Run Job「k6-report」+ 執行用 service account + IAM
└── envs/
    └── lab/             壓測專案 cfh-de-chatbot-lab
        ├── versions.tf    provider 版本、GCS backend（bucket 於 init 時指定）
        ├── variables.tf   project_id、region、notification_email
        ├── terraform.tfvars.example
        ├── monitoring.tf  通知管道（Email）、通知測試、壓測開始／階段／結束通知（見「監控與告警」）
        ├── outputs.tf
        ├── main.tf      （待撰寫）組合 modules
        ├── storage.tf   （待撰寫）GCS bucket（原始數據、報告；lifecycle 自動刪除舊資料）
        ├── registry.tf  （待撰寫）Artifact Registry（runner／report 映像檔）
        └── network.tf   （待撰寫）VPC、subnet、Cloud Router、Cloud NAT（固定 IP，給受測端 Cloud Armor 白名單）
```

lab 專案與其他團隊共用：本 repo 的資源名稱一律加 `k6` 前綴，不修改既有的通知管道、告警與 bucket，也不使用其他團隊的 state bucket。Monitoring、Logging API 在 lab 已啟用，Terraform 不管理 API 的開關（避免 destroy 時關掉別人在用的 API）。

### 初始化與套用

state 放在專用的 GCS bucket，第一次使用前手動建立一次（名稱待確認，以下以 `cfh-de-chatbot-lab-k6-tfstate` 為例）：

```bash
gcloud storage buckets create gs://cfh-de-chatbot-lab-k6-tfstate \
  --project cfh-de-chatbot-lab --location asia-east1 --uniform-bucket-level-access
gcloud storage buckets update gs://cfh-de-chatbot-lab-k6-tfstate --versioning

cd infra/terraform/envs/lab
cp terraform.tfvars.example terraform.tfvars
terraform init -backend-config="bucket=cfh-de-chatbot-lab-k6-tfstate"
terraform plan
terraform apply
```

apply 後用 [scripts/notify-dry-run.sh](../scripts/notify-dry-run.sh) 確認收得到通知信（見「監控與告警」）。

### 管理範圍：Terraform 只管「定義」，不管「執行」

| 動作 | 用什麼 | 頻率 |
|---|---|---|
| 建立／修改 Job 定義（映像檔、CPU／記憶體、task 數、SA、網路、固定環境變數） | `terraform apply` | 映像檔或設定變更時 |
| 跑一輪壓測 | `gcloud run jobs execute k6-runner --update-env-vars RUN_ID=… --tasks N` | 每輪一次 |
| 產生報告 | `gcloud run jobs execute k6-report …` 或 Console 按「執行」 | 需要時 |
| 中止壓測 | `gcloud run jobs executions cancel <execution>` | 需要時 |

- `terraform apply` **不會**啟動壓測；不要用 Terraform 觸發執行（例如 `start_execution_token`），避免 apply 時意外打出流量。
- 重跑一輪不需要重新部署，再 `execute` 一次即可。每次執行的參數用 `--update-env-vars`／`--tasks` 覆寫，只對該次有效，不會改到 Job 定義，Terraform 也不會偵測到差異。
- `RUN_ID` 沒指定時，runner 會用 Cloud Run 的 execution 名稱，不會覆蓋前一輪的資料。

### Job 定義的必要設定

| 設定 | 值 | 理由 |
|---|---|---|
| `max_retries` | 0 | 重試等於再跑一次壓測 |
| `task_count`／`parallelism` | 相同（N） | 所有 task 必須同時開始，execution segment 才能正確分攤負載 |
| `timeout` | 大於整輪壓測時間（規劃約 3.6 小時） | 逾時會送 SIGTERM，runner 會最後上傳後結束 |
| VPC egress | all-traffic，經 Cloud NAT | 受測端 Cloud Armor 白名單只放行固定 IP |
| `K6_SCRIPT`、`OUTPUT_URI`、`BASE_URL`、`ALLOW_REMOTE_TARGET`、`THRESHOLDS_JSON` | 固定環境變數 | 每輪不同的參數（RUN_ID、STAGE_SCOPE、SCENARIO_WEIGHTS、TIME_SCALE…）在 execute 時覆寫；參數一覽見 [runner/k6/README.md](../runner/k6/README.md)。`THRESHOLDS_JSON` 只定義一次，同時給 k6-runner 與 k6-report |

### Cloud NAT（固定 IP 與 port 數）

所有 task 都經同一組 NAT IP 打同一個目的地（受測端 LB 的 IP:443）。每條連線佔一個 NAT port，每個 NAT IP 對同一目的地最多約 64,512 個 port。k6 每個 VU 大約維持 1 條 keep-alive 連線。

粗估（以 mock 腳本公式：3 倍 = 354 對話／秒 × 每段對話約 250 秒）：同時約 9 萬個 VU，需要約 9 萬條連線，**1 個 NAT IP 不夠**。

| 設定 | 建議值 | 理由 |
|---|---|---|
| NAT IP | 手動指定靜態 IP，數量 = ⌈N × 每 task port 數 ÷ 64,512⌉ ＋ 1 | 固定 IP 才能加入受測端 Cloud Armor 白名單；多 1 個當餘裕 |
| 動態 port 分配 | 開啟；min 8192、max 16384（須為 2 的次方） | 每個 task 約 4,000 VU，min 留 2 倍餘裕 |
| TCP TIME_WAIT 逾時 | 30 秒（預設 120 秒） | 連線關閉後 port 較快回收 |
| NAT log | 只記錯誤 | 出現 `OUT_OF_RESOURCES` 代表 port 不夠 |

#### IP 怎麼來

```
task-000 ─┐  內部 IP：自動從 subnet 取得（每個容器約 2 個），不用管
task-001 ─┼─► Cloud NAT ─► 外部靜態 IP × K（自己預留）─► 受測端 LB
task-…  ─┘                 NAT 自動把所有 IP 的 port 分給各 task
```

- **內部 IP：** Cloud Run 用 Direct VPC egress 時，每個 task 自動從 subnet 拿 IP。只要 subnet 夠大（建議 `/24`）。
- **外部 IP：** 預留 K 個靜態 IP，交給 Cloud NAT 使用。K 就是要給受測端加白名單的 IP 數。

```hcl
resource "google_compute_address" "nat" {
  count  = var.nat_ip_count            # K
  name   = "k6-nat-${count.index}"
  region = var.region
}

resource "google_compute_router_nat" "k6" {
  name                                = "k6-nat"
  router                              = google_compute_router.k6.name
  region                              = var.region
  nat_ip_allocate_option              = "MANUAL_ONLY"
  nat_ips                             = google_compute_address.nat[*].self_link
  source_subnetwork_ip_ranges_to_nat  = "LIST_OF_SUBNETWORKS"
  subnetwork {
    name                    = google_compute_subnetwork.k6.id
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }
  enable_dynamic_port_allocation      = true
  enable_endpoint_independent_mapping = false   # 動態 port 分配需要關閉
  min_ports_per_vm                    = 8192
  max_ports_per_vm                    = 16384
  tcp_time_wait_timeout_sec           = 30
  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

output "nat_ips" { value = google_compute_address.nat[*].address }  # 給受測端加白名單
```

#### 要確認

- port 是以每個 task（容器）為單位分配，**Smoke 時要確認**：監控 `nat/dropped_sent_packets_count`（reason = OUT_OF_RESOURCES）與 k6 的連線錯誤。
- 受測端 Cloud Armor 的白名單規則，優先順序要高於任何速率限制規則，否則集中在少數 IP 的流量可能被限流而回 429。

### IAM

| 身分 | 權限 |
|---|---|
| runner SA | bucket：建立／覆蓋物件（`roles/storage.objectUser` 限定 bucket） |
| report SA | bucket：讀取全部、寫入 `reports/`、`tasks/*/dashboard-annotated.html` |
| 執行壓測的人 | `roles/run.jobsExecutorWithOverrides`（執行時要覆寫環境變數／task 數，需要 `run.jobs.runWithOverrides`），限定在這兩個 Job |
| 看報告的人 | bucket 讀取，或用期限連結（signed URL） |

### 監控與告警

所有通知寄到同一個 Email 通知管道（`notification_email`，lab 為 `tsc-pdev.service@microfusion.cloud`），由 [envs/lab/monitoring.tf](../infra/terraform/envs/lab/monitoring.tf) 建立。

#### 原則

- **告警不取代報表。** Cloud Monitoring 的告警不知道目前在哪個加壓階段，只負責「即時發現異常、決定要不要中止」；各階段通過與否，以報表 Job 的分階段判定為準（只計各階段的持續期間）。
- **門檻沿用規劃 v1.4 的證券方參考值與停止條件**，與 k6 腳本、報表 Job 使用同一組數字：
  - 參考門檻：FAQ p95 ≤ 10 秒、p99 ≤ 20 秒；業務查詢 p95 ≤ 5 秒、p99 ≤ 10 秒；錯誤率（5xx + timeout）≤ 0.5%
  - 停止條件：錯誤率 > 5%、p99 > 門檻 2 倍、出現任何 HTTP 429
- **低流量階段要防誤報。** Smoke 每秒只有約 12 次請求，一筆錯誤就會讓比例超標；比例型告警都要加「最少請求數」條件。
- **log-based alert 有寄信頻率限制：** 同一條告警 5 分鐘內最多寄一封，所以「階段切換」這類事件不能短於 5 分鐘（正式壓測各階段都 ≥ 11 分鐘，不受影響；lab 用 `TIME_SCALE` 壓縮時間時，部分階段通知會被合併掉，屬正常）。

#### 各階段的流量基準（1 倍上限）

做監控 Dashboard 的參考線，也用來確認流量確實送達（實際請求量明顯低於目標，代表壓測機或 NAT 有問題，而不是受測系統很閒）。

| 階段 | 佔 1 倍 | 持續＋爬升（分） | 對話／秒 | 聊天 API／秒 | k6 VU（p95） |
|---|---|---|---|---|---|
| Smoke | 1% | 10＋1 | 1.18 | 12 | 462 |
| 低量 5% | 5% | 15＋3 | 5.9 | 62 | 2,307 |
| 低量 10% | 10% | 15＋3 | 11.8 | 124 | 4,614 |
| Baseline | 25% | 15＋3 | 29.5 | 310 | 11,534 |
| 中量 50% | 50% | 15＋3 | 59 | 620 | 23,068 |
| 1 倍 | 100% | 30＋3 | 118 | 1,239 | 46,135 |
| Recovery | 25% | 15＋3 | 29.5 | 310 | 11,534 |

另外兩種壓測型態的監控重點：

- **Soak（實測容量 70%，2～4 小時）：** 看受測端記憶體與連線數是否持續上升（洩漏），以及 Token 續期是否失敗。
- **Spike（市場事件、IPO 尖峰）：** 看受測端 instance 數擴充的速度，以及擴充期間的 5xx、429。

#### 告警清單

**A. 壓測執行（壓測專案內）**

| # | 告警 | 來源 | 條件 | 等級 | 狀態 |
|---|---|---|---|---|---|
| A0 | 通知測試 | log `k6-notify-test`（`scripts/notify-dry-run.sh` 寫入） | 出現即通知 | 資訊 | **已完成** |
| A1 | 壓測開始 | runner log，只由 task-000 寫出 `STRESS_TEST_EVENT=START` | 出現即通知；信件帶 RUN_ID、task 數、階段範圍、GCS 路徑 | 資訊 | **已完成**（本機驗證事件格式；雲端待 lab 驗證） |
| A2 | 進入新階段 | k6 腳本以單一 VU 在每個階段開始時寫出 `STRESS_TEST_EVENT=STAGE` | 出現即通知；信件帶階段名稱與目標流量 | 資訊 | **已完成**（同上） |
| A3 | 壓測結束 | runner log，task-000 寫出 `STRESS_TEST_EVENT=END`，`k6_exit` = 0 | 出現即通知；信件帶 GCS 路徑與產生報告的指令 | 資訊 | **已完成**（同上） |
| A4 | 門檻未過或觸發停止條件 | 同 A3，`k6_exit` ≠ 0（99 = 門檻未過或停止條件中止） | 出現即通知；每輪只會收到 A3、A4 其中一封 | 嚴重 | **已完成**（同上） |
| A5 | 壓測機 task 失敗 | `run.googleapis.com/job/completed_task_attempt_count`（result = failed） | > 0 | 嚴重 | 待做 |
| A6 | 壓測機記憶體偏高 | `run.googleapis.com/container/memory/utilizations`（job = k6-runner） | > 85% 持續 2 分鐘；代表每個 task 的 VU 太多，有被砍掉的風險 | 警告 | 待做 |
| A7 | NAT port 不足 | `router.googleapis.com/nat/dropped_sent_packets_count`（reason = OUT_OF_RESOURCES） | > 0 | 嚴重 | 待做 |

**B. 受測系統（依參考門檻與停止條件）**

受測系統的指標在受測端的專案。lab（cfh-de-chatbot-lab）內的受測端（mock 或我方自行部署的目標）可以直接建；SIT／UAT 是客戶環境，我方無法操作，需要客戶授權讀取指標（把客戶專案加入本專案的 metrics scope），或由客戶自行設定。

Cloud Run 的指標分不出 FAQ 與業務查詢（同一個 `/api/chat/ask`），所以延遲告警以較寬的 FAQ 門檻當上限；FAQ／業務分開的數字看報表。

| # | 告警 | 指標（BFF、Middleware 各一條） | 警告 | 嚴重（對應停止條件） |
|---|---|---|---|---|
| B1 | 回應時間 | `run.googleapis.com/request_latencies` p95／p99 | p95 > 10 秒 或 p99 > 20 秒，持續 3 分鐘 | p99 > 40 秒（門檻 2 倍），持續 3 分鐘 |
| B2 | 5xx 比例 | `run.googleapis.com/request_count`（5xx ÷ 全部） | > 0.5%，持續 3 分鐘，且每分鐘 ≥ 100 次請求 | > 5%，持續 3 分鐘 |
| B3 | HTTP 429 | `run.googleapis.com/request_count`（response_code = 429） | — | > 0（任何 429 即停止） |
| B4 | 負載平衡器逾時 | `loadbalancing.googleapis.com/https/request_count`（response_code = 504） | > 0；回應超過 30 秒被 LB 切斷 | — |
| B5 | instance 數接近上限 | `run.googleapis.com/container/instance_count` ÷ max instances | > 80% | — |
| B6 | CPU／記憶體 | `run.googleapis.com/container/cpu/utilizations`、`memory/utilizations` | > 85%，持續 5 分鐘 | — |
| B7 | AI 配額 | Vertex AI／Gemini 的 429 或 `serviceruntime.googleapis.com/quota/exceeded` | > 0（規劃 E5：自然觀察） | — |

#### 通知 dry run

確認通知管道收得到信（不啟動壓測、不送出任何流量）：

```bash
scripts/notify-dry-run.sh --check-only   # 只檢查告警政策與通知管道設定
scripts/notify-dry-run.sh                # 寫入一筆測試事件，約 1～5 分鐘內收到「[k6 壓測] 通知測試 …」
```

腳本走的是和 A1～A4 相同的 log-based alert 路徑，收得到測試信就代表之後的開始／結束通知也會寄到。執行者需要該專案的 Monitoring 讀取與 Logging 寫入權限。同一條告警 5 分鐘內只寄一封，連續執行時第二封可能不會寄出。
