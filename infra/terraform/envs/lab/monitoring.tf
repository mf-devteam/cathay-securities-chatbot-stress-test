# 壓測通知與告警（設計見 infra/terraform/README.md「監控與告警」）
# lab 專案與其他團隊共用：資源名稱一律加 k6 前綴，不修改既有的通知管道與告警

locals {
  # 通知測試事件寫入的 log 名稱（scripts/notify-dry-run.sh 寫入）
  notify_test_log_id = "k6-notify-test"
}

resource "google_monitoring_notification_channel" "email" {
  display_name = "k6 壓測通知（${var.notification_email}）"
  type         = "email"
  labels = {
    email_address = var.notification_email
  }
  user_labels = {
    app = "k6-stress-test"
  }
}

# 通知測試：寫入一筆測試事件就寄信，用來確認通知管道收得到信（scripts/notify-dry-run.sh）。
# 與壓測開始／結束通知相同，都是 log-based alert，能一併驗證整條寄信路徑。
resource "google_monitoring_alert_policy" "notify_test" {
  display_name = "[k6] 通知測試"
  combiner     = "OR"

  conditions {
    display_name = "收到通知測試事件"
    condition_matched_log {
      filter = "logName=\"projects/${var.project_id}/logs/${local.notify_test_log_id}\" AND jsonPayload.event=\"NOTIFY_TEST\""
      label_extractors = {
        run_id       = "EXTRACT(jsonPayload.run_id)"
        requested_by = "EXTRACT(jsonPayload.requested_by)"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.id]

  alert_strategy {
    # log-based alert 必填；5 分鐘內重複觸發只會寄一封
    notification_rate_limit {
      period = "300s"
    }
    auto_close = "1800s"
  }

  documentation {
    subject   = "[k6 壓測] 通知測試 $${log.extracted_label.run_id}"
    mime_type = "text/markdown"
    content   = <<-EOT
      ## 新阿發壓測：通知測試

      這封信由 `scripts/notify-dry-run.sh` 觸發，用來確認通知管道可以正常寄信，**不代表有壓測正在執行**。

      - 專案：${var.project_id}
      - 測試代號：$${log.extracted_label.run_id}
      - 觸發者：$${log.extracted_label.requested_by}

      收到這封信，代表壓測開始／結束通知與告警都會寄到這個信箱。
    EOT
  }

  user_labels = {
    app = "k6-stress-test"
  }
}

# ---------- 壓測執行通知（A1～A4） ----------
# runner/entrypoint.sh（task-000）與 k6 腳本（單一 VU）寫到 stdout 的固定格式事件：
#   STRESS_TEST_EVENT=START run_id=… tasks=… stage_scope=… time_scale=… dest=…
#   STRESS_TEST_EVENT=STAGE run_id=… stage=… pct=… conv_per_s=… api_per_s=… hold_min=… time_scale=…
#   STRESS_TEST_EVENT=END   run_id=… k6_exit=… dest=…
locals {
  runner_log_filter = "resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"${var.runner_job_name}\""

  run_events = {
    start = {
      name     = "[k6] 壓測開始"
      severity = "WARNING"
      filter   = "textPayload:\"STRESS_TEST_EVENT=START\""
      labels   = { tasks = "tasks", stage_scope = "stage_scope", time_scale = "time_scale", dest = "dest" }
      subject  = "[k6 壓測] 開始 $${log.extracted_label.run_id}"
      body     = <<-EOT
        ## 新阿發壓測：開始

        - 壓測代號：$${log.extracted_label.run_id}
        - task 數：$${log.extracted_label.tasks}
        - 階段範圍：$${log.extracted_label.stage_scope}（時間壓縮 ÷$${log.extracted_label.time_scale}）
        - 原始數據：$${log.extracted_label.dest}

        到 1 倍的完整一輪約 2.2 小時。各階段開始時會另外通知。
      EOT
    }
    stage = {
      name     = "[k6] 進入新階段"
      severity = "WARNING"
      filter   = "textPayload:\"STRESS_TEST_EVENT=STAGE\""
      labels   = { stage = "stage", pct = "pct", conv_per_s = "conv_per_s", api_per_s = "api_per_s", hold_min = "hold_min" }
      subject  = "[k6 壓測] 進入階段 $${log.extracted_label.stage}（$${log.extracted_label.run_id}）"
      body     = <<-EOT
        ## 新阿發壓測：進入新階段

        - 壓測代號：$${log.extracted_label.run_id}
        - 階段：$${log.extracted_label.stage}（佔 1 倍 $${log.extracted_label.pct}）
        - 目標流量：每秒 $${log.extracted_label.conv_per_s} 個新對話、約 $${log.extracted_label.api_per_s} 次聊天 API
        - 持續：$${log.extracted_label.hold_min} 分鐘（另加爬升時間）

        實際請求量明顯低於目標時，先檢查壓測機與 NAT，而不是受測系統。
      EOT
    }
    end_ok = {
      name     = "[k6] 壓測結束"
      severity = "WARNING"
      filter   = "textPayload=~\"STRESS_TEST_EVENT=END .*k6_exit=0( |$)\""
      labels   = { k6_exit = "k6_exit", dest = "dest" }
      subject  = "[k6 壓測] 結束 $${log.extracted_label.run_id}"
      body     = <<-EOT
        ## 新阿發壓測：結束（門檻全部通過）

        - 壓測代號：$${log.extracted_label.run_id}
        - 原始數據：$${log.extracted_label.dest}

        產生正式報告：`gcloud run jobs execute k6-report --update-env-vars SOURCE_URI=$${log.extracted_label.dest}`
      EOT
    }
    end_failed = {
      name     = "[k6] 壓測結束：門檻未過或異常"
      severity = "CRITICAL"
      filter   = "textPayload=~\"STRESS_TEST_EVENT=END .*k6_exit=[1-9]\""
      labels   = { k6_exit = "k6_exit", dest = "dest" }
      subject  = "[k6 壓測] 結束（k6 exit $${log.extracted_label.k6_exit}）$${log.extracted_label.run_id}"
      body     = <<-EOT
        ## 新阿發壓測：結束，門檻未過或執行異常

        - 壓測代號：$${log.extracted_label.run_id}
        - k6 exit code：$${log.extracted_label.k6_exit}
          - 99：門檻未通過，或觸發停止條件（錯誤率 > 5%、p99 > 門檻 2 倍、HTTP 429）而中止
          - 其他：腳本或執行錯誤，查 `tasks/task-000/k6-console.log`
        - 原始數據：$${log.extracted_label.dest}

        先產生報告確認是哪個階段、哪個指標超標：`gcloud run jobs execute k6-report --update-env-vars SOURCE_URI=$${log.extracted_label.dest}`
      EOT
    }
  }
}

resource "google_monitoring_alert_policy" "run_event" {
  for_each = local.run_events

  display_name = each.value.name
  combiner     = "OR"
  severity     = each.value.severity

  conditions {
    display_name = each.value.name
    condition_matched_log {
      filter = "${local.runner_log_filter} AND ${each.value.filter}"
      label_extractors = merge(
        { run_id = "REGEXP_EXTRACT(textPayload, \"run_id=(\\\\S+)\")" },
        { for label, key in each.value.labels : label => "REGEXP_EXTRACT(textPayload, \"${key}=([^\\\\s\\\"]+)\")" }
      )
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.id]

  alert_strategy {
    notification_rate_limit {
      period = "300s"
    }
    auto_close = "1800s"
  }

  documentation {
    subject   = each.value.subject
    mime_type = "text/markdown"
    content   = each.value.body
  }

  user_labels = {
    app = "k6-stress-test"
  }
}
