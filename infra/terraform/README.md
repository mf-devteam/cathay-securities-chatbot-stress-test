# Terraform

管理壓測專案的 GCP 資源。整體架構見 [docs/architecture.md](../../docs/architecture.md)。目前已完成通知管道與壓測執行通知（`monitoring.tf`），其餘依下方結構陸續撰寫。

## 結構

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

## 初始化與套用

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

apply 後用 [scripts/notify-dry-run.sh](../../scripts/notify-dry-run.sh) 確認收得到通知信（見「監控與告警」）。

## 管理範圍：Terraform 只管「定義」，不管「執行」

| 動作 | 用什麼 | 頻率 |
|---|---|---|
| 建立／修改 Job 定義（映像檔、CPU／記憶體、task 數、SA、網路、固定環境變數） | `terraform apply` | 映像檔或設定變更時 |
| 跑一輪壓測 | `gcloud run jobs execute k6-runner --update-env-vars RUN_ID=… --tasks N` | 每輪一次 |
| 產生報告 | `gcloud run jobs execute k6-report …` 或 Console 按「執行」 | 需要時 |
| 中止壓測 | `gcloud run jobs executions cancel <execution>` | 需要時 |

- `terraform apply` **不會**啟動壓測；不要用 Terraform 觸發執行（例如 `start_execution_token`），避免 apply 時意外打出流量。
- 重跑一輪不需要重新部署，再 `execute` 一次即可。每次執行的參數用 `--update-env-vars`／`--tasks` 覆寫，只對該次有效，不會改到 Job 定義，Terraform 也不會偵測到差異。
- `RUN_ID` 沒指定時，runner 會用 Cloud Run 的 execution 名稱，不會覆蓋前一輪的資料。

## Job 定義的必要設定

| 設定 | 值 | 理由 |
|---|---|---|
| `max_retries` | 0 | 重試等於再跑一次壓測 |
| `task_count`／`parallelism` | 相同（N） | 所有 task 必須同時開始，execution segment 才能正確分攤負載 |
| `timeout` | 大於整輪壓測時間（規劃約 3.6 小時） | 逾時會送 SIGTERM，runner 會最後上傳後結束 |
| VPC egress | all-traffic，經 Cloud NAT | 受測端 Cloud Armor 白名單只放行固定 IP |
| `K6_SCRIPT`、`OUTPUT_URI`、`BASE_URL`、`ALLOW_REMOTE_TARGET`、`THRESHOLDS_JSON` | 固定環境變數 | 每輪不同的參數（RUN_ID、STAGE_SCOPE、SCENARIO_WEIGHTS、TIME_SCALE…）在 execute 時覆寫；參數一覽見 [runner/k6/README.md](../../runner/k6/README.md)。`THRESHOLDS_JSON` 只定義一次，同時給 k6-runner 與 k6-report |

## Cloud NAT（固定 IP 與 port 數）

所有 task 都經同一組 NAT IP 打同一個目的地（受測端 LB 的 IP:443）。每條連線佔一個 NAT port，每個 NAT IP 對同一目的地最多約 64,512 個 port。k6 每個 VU 大約維持 1 條 keep-alive 連線。

粗估（以 mock 腳本公式：3 倍 = 354 對話／秒 × 每段對話約 250 秒）：同時約 9 萬個 VU，需要約 9 萬條連線，**1 個 NAT IP 不夠**。

| 設定 | 建議值 | 理由 |
|---|---|---|
| NAT IP | 手動指定靜態 IP，數量 = ⌈N × 每 task port 數 ÷ 64,512⌉ ＋ 1 | 固定 IP 才能加入受測端 Cloud Armor 白名單；多 1 個當餘裕 |
| 動態 port 分配 | 開啟；min 8192、max 16384（須為 2 的次方） | 每個 task 約 4,000 VU，min 留 2 倍餘裕 |
| TCP TIME_WAIT 逾時 | 30 秒（預設 120 秒） | 連線關閉後 port 較快回收 |
| NAT log | 只記錯誤 | 出現 `OUT_OF_RESOURCES` 代表 port 不夠 |

### IP 怎麼來

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

### 要確認

- port 是以每個 task（容器）為單位分配，**Smoke 時要確認**：監控 `nat/dropped_sent_packets_count`（reason = OUT_OF_RESOURCES）與 k6 的連線錯誤。
- 受測端 Cloud Armor 的白名單規則，優先順序要高於任何速率限制規則，否則集中在少數 IP 的流量可能被限流而回 429。

## IAM

| 身分 | 權限 |
|---|---|
| runner SA | bucket：建立／覆蓋物件（`roles/storage.objectUser` 限定 bucket） |
| report SA | bucket：讀取全部、寫入 `reports/`、`tasks/*/dashboard-annotated.html` |
| 執行壓測的人 | `roles/run.jobsExecutorWithOverrides`（執行時要覆寫環境變數／task 數，需要 `run.jobs.runWithOverrides`），限定在這兩個 Job |
| 看報告的人 | bucket 讀取，或用期限連結（signed URL） |

## 監控與告警

所有通知寄到同一個 Email 通知管道（`notification_email`，lab 為 `tsc-pdev.service@microfusion.cloud`），由 [envs/lab/monitoring.tf](envs/lab/monitoring.tf) 建立。

### 原則

- **告警不取代報表。** Cloud Monitoring 的告警不知道目前在哪個加壓階段，只負責「即時發現異常、決定要不要中止」；各階段通過與否，以報表 Job 的分階段判定為準（只計各階段的持續期間）。
- **門檻沿用規劃 v1.4 的證券方參考值與停止條件**，與 k6 腳本、報表 Job 使用同一組數字：
  - 參考門檻：FAQ p95 ≤ 10 秒、p99 ≤ 20 秒；業務查詢 p95 ≤ 5 秒、p99 ≤ 10 秒；錯誤率（5xx + timeout）≤ 0.5%
  - 停止條件：錯誤率 > 5%、p99 > 門檻 2 倍、出現任何 HTTP 429
- **低流量階段要防誤報。** Smoke 每秒只有約 12 次請求，一筆錯誤就會讓比例超標；比例型告警都要加「最少請求數」條件。
- **log-based alert 有寄信頻率限制：** 同一條告警 5 分鐘內最多寄一封，所以「階段切換」這類事件不能短於 5 分鐘（正式壓測各階段都 ≥ 11 分鐘，不受影響；lab 用 `TIME_SCALE` 壓縮時間時，部分階段通知會被合併掉，屬正常）。

### 各階段的流量基準（1 倍上限）

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

### 告警清單

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

受測系統的指標在受測端的專案。lab 的受測端（SIT 或 mock）也在 cfh-de-chatbot-lab，可以直接建；正式壓測時受測端在客戶專案，需要客戶授權讀取指標（把客戶專案加入本專案的 metrics scope），或由客戶自行設定。

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

### 通知 dry run

確認通知管道收得到信（不啟動壓測、不送出任何流量）：

```bash
scripts/notify-dry-run.sh --check-only   # 只檢查告警政策與通知管道設定
scripts/notify-dry-run.sh                # 寫入一筆測試事件，約 1～5 分鐘內收到「[k6 壓測] 通知測試 …」
```

腳本走的是和 A1～A4 相同的 log-based alert 路徑，收得到測試信就代表之後的開始／結束通知也會寄到。執行者需要該專案的 Monitoring 讀取與 Logging 寫入權限。同一條告警 5 分鐘內只寄一封，連續執行時第二封可能不會寄出。
