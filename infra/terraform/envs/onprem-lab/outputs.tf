output "snat_ip" {
  description = "對外固定 IP（受測端有 IP 白名單時加入這個 IP）"
  value       = google_compute_address.snat.address
}

output "control_ssh" {
  description = "登入 control（IAP，等同地端的跳板機）"
  value       = "gcloud compute ssh ${google_compute_instance.control.name} --zone ${var.zone} --project ${var.project_id} --tunnel-through-iap"
}

output "runner_ips" {
  description = "runner 內部 IP（已寫入 control 的 /etc/k6-stress/stress.env）"
  value       = local.runner_ips
}

output "target_base_url" {
  description = "壓測目標"
  value       = local.base_url
}

output "bucket" {
  description = "程式交付用的 bucket"
  value       = google_storage_bucket.lab.name
}
