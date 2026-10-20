variable "project_id" {
  description = "lab 的 GCP project ID"
  type        = string
}

variable "region" {
  description = "VM、NAT、bucket 所在區域"
  type        = string
  default     = "asia-east1"
}

variable "zone" {
  description = "VM 所在 zone"
  type        = string
  default     = "asia-east1-b"
}

variable "subnet_cidr" {
  description = "模擬 DMZ 網段；VM 依序取固定內部 IP：control .10、runner .11 起、mock .20"
  type        = string
  default     = "10.60.0.0/24"
}

# ---------- VM 規格：預設小規格驗證流程；校正試跑時改成規劃 p.9 的規格 ----------
variable "runner_count" {
  description = "runner 台數（規劃為 3）"
  type        = number
  default     = 3
}

variable "runner_machine_type" {
  description = "runner 機型（規劃：8 vCPU／32 GB → n2-standard-8 或 e2-standard-8）"
  type        = string
  default     = "e2-standard-2"
}

variable "runner_disk_gb" {
  description = "runner 磁碟大小（規劃：100 GB SSD）"
  type        = number
  default     = 30
}

variable "control_machine_type" {
  description = "control 機型（規劃：4 vCPU／16 GB → e2-standard-4）"
  type        = string
  default     = "e2-standard-2"
}

variable "control_disk_gb" {
  description = "control 磁碟大小（規劃：200 GB SSD）"
  type        = number
  default     = 50
}

variable "disk_type" {
  description = "磁碟類型（規劃為 SSD：pd-ssd；lab 驗證用 pd-balanced 即可）"
  type        = string
  default     = "pd-balanced"
}

# ---------- 受測端 ----------
variable "enable_mock" {
  description = "是否建立 mock server VM（受測端替身）"
  type        = bool
  default     = true
}

variable "mock_machine_type" {
  description = "mock server 機型"
  type        = string
  default     = "e2-standard-2"
}

variable "mock_capacity" {
  description = "mock server 同時處理中請求數的舒適上限（超過後變慢、回 503）"
  type        = number
  default     = 1000
}

variable "target_base_url" {
  description = "壓測目標；空字串＝lab 的 mock server。只能填 lab 內我們自己部署的目標，不要填 SIT／UAT（客戶環境）"
  type        = string
  default     = ""
}

variable "target_frontend_url" {
  description = "連線預檢步驟 4 的前端頁面；空字串時用 target_base_url/"
  type        = string
  default     = ""
}

variable "stage_plan" {
  description = "lab 的 STAGE_PLAN（縮小版，約 26 分鐘）；空字串＝規劃的正式階段（S3 354 TPS，約 2 小時 15 分）"
  type        = string
  default     = "s0:1tps:3:1;s1:3tps:5:1;s2:6tps:5:1;s3:12tps:5:1;s4:3tps:3:1"
}

variable "max_vus" {
  description = "全部 runner 合計的 VU 上限（小規格 VM 要調低，避免記憶體不足）；0＝用 defaults.env 的 10500"
  type        = number
  default     = 600
}

variable "iap_ssh_to_all" {
  description = "是否允許 IAP SSH 直接登入 runner／mock（除錯用）；false＝只能登入 control，與地端只開跳板機到 control 相同"
  type        = bool
  default     = false
}
