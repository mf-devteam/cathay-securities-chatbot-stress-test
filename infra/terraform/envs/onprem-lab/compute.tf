# 4 台 VM（規劃 p.9）＋lab 專用的 mock server。無外部 IP。
# control／runner 的 OS 由 var.onprem_os 決定（地端為 RHEL 9.6）；mock 固定 Ubuntu 24.04。
# 登入：OS Login（enable_oslogin=true）或 gcloud compute ssh 寫入該 VM metadata 的公鑰（預設）。
# 開機時 startup script 從 bucket 下載程式並執行 onprem/install.sh（與地端相同的安裝腳本）。

resource "google_service_account" "control" {
  account_id   = "k6-onprem-control"
  display_name = "k6 地端 lab：control-report"
}

resource "google_service_account" "node" {
  account_id   = "k6-onprem-node"
  display_name = "k6 地端 lab：runner／mock"
}

# control 要上傳 SSH 公鑰（keys/control.pub），其他只需要讀
resource "google_storage_bucket_iam_member" "control" {
  bucket = google_storage_bucket.lab.name
  role   = "roles/storage.objectUser"
  member = google_service_account.control.member
}

resource "google_storage_bucket_iam_member" "node" {
  bucket = google_storage_bucket.lab.name
  role   = "roles/storage.objectViewer"
  member = google_service_account.node.member
}

locals {
  images = {
    ubuntu = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
    rhel   = "rhel-cloud/rhel-9" # GCE 只提供最新 9.x；離線包在 9.6 的驗證用 Docker（onprem/bundle/build-in-docker.sh）
  }
  image      = local.images[var.onprem_os]
  mock_image = local.images["ubuntu"]
  labels = {
    app = "k6-stress"
    env = "onprem-lab"
  }
  startup = { for role in ["control", "mock"] : role => templatefile("${path.module}/templates/startup.sh.tftpl", {
    role          = role
    bucket        = google_storage_bucket.lab.name
    mock_capacity = var.mock_capacity
    control_id    = ""
  }) }
  # runner 依 control 的 instance ID 取公鑰（另外產生，避免 control 參照自己）
  runner_startup = templatefile("${path.module}/templates/startup.sh.tftpl", {
    role          = "runner"
    bucket        = google_storage_bucket.lab.name
    mock_capacity = var.mock_capacity
    control_id    = google_compute_instance.control.instance_id
  })
}

resource "google_compute_instance" "control" {
  name                      = "k6-control-report-01"
  machine_type              = var.control_machine_type
  tags                      = ["k6-control"]
  labels                    = local.labels
  allow_stopping_for_update = true

  boot_disk {
    initialize_params {
      image = local.image
      size  = var.control_disk_gb
      type  = var.disk_type
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.dmz.id
    network_ip = local.control_ip
  }

  service_account {
    email  = google_service_account.control.email
    scopes = ["cloud-platform"]
  }

  shielded_instance_config {
    enable_secure_boot = true
  }

  metadata = {
    enable-oslogin         = var.enable_oslogin ? "TRUE" : "FALSE"
    block-project-ssh-keys = "TRUE" # 只接受寫在這台 VM 上的公鑰，不受專案層級金鑰影響
    startup-script         = local.startup["control"]
  }

  # 程式與設定上傳完、NAT 可用後才開機，startup script 才拿得到
  depends_on = [google_storage_bucket_object.src, google_storage_bucket_object.stress_env, google_storage_bucket_iam_member.control, google_compute_router_nat.snat]
}

resource "google_compute_instance" "runner" {
  count                     = var.runner_count
  name                      = format("k6-runner-%02d", count.index + 1)
  machine_type              = var.runner_machine_type
  tags                      = ["k6-runner"]
  labels                    = local.labels
  allow_stopping_for_update = true

  boot_disk {
    initialize_params {
      image = local.image
      size  = var.runner_disk_gb
      type  = var.disk_type
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.dmz.id
    network_ip = local.runner_ips[count.index]
  }

  service_account {
    email  = google_service_account.node.email
    scopes = ["cloud-platform"]
  }

  shielded_instance_config {
    enable_secure_boot = true
  }

  metadata = {
    enable-oslogin         = var.enable_oslogin ? "TRUE" : "FALSE"
    block-project-ssh-keys = "TRUE" # 只接受寫在這台 VM 上的公鑰，不受專案層級金鑰影響
    startup-script         = local.runner_startup
  }

  depends_on = [google_storage_bucket_object.src, google_storage_bucket_iam_member.node, google_compute_router_nat.snat]

  # control 重建（新的 SSH 金鑰）時一起重建：startup script 只在開機時執行，才會取得新 control 的公鑰
  lifecycle {
    replace_triggered_by = [google_compute_instance.control.id]
  }
}

resource "google_compute_instance" "mock" {
  count                     = var.enable_mock ? 1 : 0
  name                      = "k6-mock-01"
  machine_type              = var.mock_machine_type
  tags                      = ["k6-mock"]
  labels                    = local.labels
  allow_stopping_for_update = true

  boot_disk {
    initialize_params {
      image = local.mock_image
      size  = 20
      type  = "pd-balanced"
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.dmz.id
    network_ip = local.mock_ip
  }

  service_account {
    email  = google_service_account.node.email
    scopes = ["cloud-platform"]
  }

  shielded_instance_config {
    enable_secure_boot = true
  }

  metadata = {
    enable-oslogin         = var.enable_oslogin ? "TRUE" : "FALSE"
    block-project-ssh-keys = "TRUE" # 只接受寫在這台 VM 上的公鑰，不受專案層級金鑰影響
    startup-script         = local.startup["mock"]
  }

  depends_on = [google_storage_bucket_object.src, google_storage_bucket_iam_member.node, google_compute_router_nat.snat]
}

# IAP TCP forwarding 權限（等同地端「只開跳板機到 control」）：只授權在 control 這台 VM 上
resource "google_iap_tunnel_instance_iam_member" "control" {
  for_each = toset(var.iap_members)
  instance = google_compute_instance.control.name
  zone     = var.zone
  role     = "roles/iap.tunnelResourceAccessor"
  member   = each.value
}
