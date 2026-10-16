output "notification_channel_id" {
  description = "壓測通知管道（Email）"
  value       = google_monitoring_notification_channel.email.id
}

output "notify_test_policy_id" {
  description = "通知測試用的告警政策"
  value       = google_monitoring_alert_policy.notify_test.id
}

output "notify_test_log_id" {
  description = "通知測試事件寫入的 log 名稱"
  value       = local.notify_test_log_id
}
