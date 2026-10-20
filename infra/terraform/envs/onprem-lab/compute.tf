# 4 台 VM（規劃 p.9）＋lab 專用的 mock server。全部 Ubuntu 24.04、無外部 IP、OS Login。
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
  image = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
  labels = {
    app = "k6-stress"
    env = "onprem-lab"
  }
  startup = { for role in ["control", "runner", "mock"] : role => templatefile("${path.module}/templates/startup.sh.tftpl", {
    role          = role
    bucket        = google_storage_bucket.lab.name
    mock_capacity = var.mock_capacity
  }) }
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
    enable-oslogin = "TRUE"
    startup-script = local.startup["control"]
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
    enable-oslogin = "TRUE"
    startup-script = local.startup["runner"]
  }

  depends_on = [google_storage_bucket_object.src, google_storage_bucket_iam_member.node, google_compute_router_nat.snat]
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
      image = local.image
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
    enable-oslogin = "TRUE"
    startup-script = local.startup["mock"]
  }

  depends_on = [google_storage_bucket_object.src, google_storage_bucket_iam_member.node, google_compute_router_nat.snat]
}
