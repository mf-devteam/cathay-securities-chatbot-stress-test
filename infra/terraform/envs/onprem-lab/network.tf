# 模擬證券方 DMZ：VM 沒有外部 IP，對外經 Cloud NAT 的 1 個固定 IP（＝地端防火牆 SNAT）
# 對內只開 SSH：IAP → control（＝VPN／跳板機）、control → runner；runner → mock 8090（lab 受測端）

resource "google_compute_network" "lab" {
  name                    = "k6-onprem-vpc"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "dmz" {
  name                     = "k6-onprem-dmz"
  network                  = google_compute_network.lab.id
  region                   = var.region
  ip_cidr_range            = var.subnet_cidr
  private_ip_google_access = true
}

resource "google_compute_router" "lab" {
  name    = "k6-onprem-router"
  network = google_compute_network.lab.id
  region  = var.region
}

# 對外固定 IP（＝地端 SNAT IP）：受測端有 IP 白名單時加入這個 IP
resource "google_compute_address" "snat" {
  name   = "k6-onprem-snat"
  region = var.region
}

resource "google_compute_router_nat" "snat" {
  name                               = "k6-onprem-snat"
  router                             = google_compute_router.lab.name
  region                             = var.region
  nat_ip_allocate_option             = "MANUAL_ONLY"
  nat_ips                            = [google_compute_address.snat.self_link]
  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"
  subnetwork {
    name                    = google_compute_subnetwork.dmz.id
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }
  # 每台 runner 最多約 3,500 條連線打同一個目的地
  enable_dynamic_port_allocation      = true
  enable_endpoint_independent_mapping = false
  min_ports_per_vm                    = 4096
  max_ports_per_vm                    = 16384
  tcp_time_wait_timeout_sec           = 30
  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

resource "google_compute_firewall" "iap_ssh" {
  name          = "k6-onprem-allow-iap-ssh"
  network       = google_compute_network.lab.id
  source_ranges = ["35.235.240.0/20"] # IAP TCP forwarding
  target_tags   = var.iap_ssh_to_all ? ["k6-control", "k6-runner", "k6-mock"] : ["k6-control"]
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

resource "google_compute_firewall" "control_to_runner" {
  name        = "k6-onprem-allow-control-ssh"
  network     = google_compute_network.lab.id
  source_tags = ["k6-control"]
  target_tags = ["k6-runner"]
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

resource "google_compute_firewall" "to_mock" {
  count       = var.enable_mock ? 1 : 0
  name        = "k6-onprem-allow-mock"
  network     = google_compute_network.lab.id
  source_tags = ["k6-control", "k6-runner"]
  target_tags = ["k6-mock"]
  allow {
    protocol = "tcp"
    ports    = ["8090"]
  }
}
