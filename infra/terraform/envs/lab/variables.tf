variable "project_id" {
  description = "壓測專案的 GCP project ID"
  type        = string
}

variable "region" {
  description = "Cloud Run Jobs、GCS、NAT 所在區域"
  type        = string
  default     = "asia-east1"
}

variable "notification_email" {
  description = "壓測通知（開始、結束、告警）的收件信箱"
  type        = string
}

variable "runner_job_name" {
  description = "壓測機的 Cloud Run Job 名稱（通知告警依此篩選 log）"
  type        = string
  default     = "k6-runner"
}
