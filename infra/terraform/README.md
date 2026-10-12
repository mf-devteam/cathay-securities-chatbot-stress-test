# Terraform（待撰寫）

管理壓測專案的 GCP 資源。整體架構見 [docs/architecture.md](../../docs/architecture.md)。

## 預定結構

```
infra/terraform/
├── modules/
│   ├── k6-runner/       Cloud Run Job「k6-runner」+ 執行用 service account + IAM
│   └── k6-report/       Cloud Run Job「k6-report」+ 執行用 service account + IAM
└── envs/
    └── lab/             壓測專案（main.tf 組合 modules、backend.tf、terraform.tfvars.example）
        ├── storage.tf   GCS bucket（原始數據、報告；lifecycle 自動刪除舊資料）
        ├── registry.tf  Artifact Registry（runner／report 映像檔）
        └── network.tf   VPC、subnet、Cloud Router、Cloud NAT（固定 IP，給受測端 Cloud Armor 白名單）
```

## 管理範圍：Terraform 只管「定義」，不管「執行」

| 動作 | 用什麼 | 頻率 |
|---|---|---|
| 建立／修改 Job 定義（映像檔、CPU／記憶體、task 數、SA、網路、固定環境變數） | `terraform apply` | 映像檔或設定變更時 |
| 跑一輪壓測 | `gcloud run jobs execute k6-runner --update-env-vars RUN_ID=…,MIX=… --tasks N` | 每輪一次 |
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
| `K6_SCRIPT`、`OUTPUT_URI`、`BASE_URL` | 固定環境變數 | 每輪不同的參數（RUN_ID、MIX、TIME_SCALE…）在 execute 時覆寫 |

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
