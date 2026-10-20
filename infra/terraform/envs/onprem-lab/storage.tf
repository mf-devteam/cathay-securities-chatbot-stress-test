# 交付程式給 VM 用的 bucket（＝地端由 MF 帶安裝包過去）：
#   src/       repo 的 runner、report-job、onprem（VM 開機時下載後執行 onprem/install.sh）
#   config/    lab 的 stress.env（control 第一次開機時放到 /etc/k6-stress/stress.env）
#   keys/      control 開機時上傳 SSH 公鑰，runner 開機時下載加入 authorized_keys（私鑰不離開 control）

locals {
  repo_root = abspath("${path.module}/../../../..")
  src_files = toset([
    for f in setunion(
      fileset(local.repo_root, "runner/entrypoint.sh"),
      fileset(local.repo_root, "runner/k6/*.js"),
      fileset(local.repo_root, "runner/k6/lib/*.js"),
      fileset(local.repo_root, "runner/k6/data/*.json"),
      fileset(local.repo_root, "runner/k6/mock/*.js"),
      fileset(local.repo_root, "report-job/*.py"),
      fileset(local.repo_root, "onprem/**"),
    ) : f if !endswith(f, ".DS_Store")
  ])

  control_ip = cidrhost(var.subnet_cidr, 10)
  runner_ips = [for i in range(var.runner_count) : cidrhost(var.subnet_cidr, 11 + i)]
  mock_ip    = cidrhost(var.subnet_cidr, 20)

  base_url     = var.target_base_url != "" ? trimsuffix(var.target_base_url, "/") : "http://${local.mock_ip}:8090"
  frontend_url = var.target_frontend_url != "" ? var.target_frontend_url : (var.target_base_url != "" ? "${local.base_url}/" : "${local.base_url}/healthz")
}

resource "google_storage_bucket" "lab" {
  name                        = "${var.project_id}-k6-onprem-lab"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true
}

resource "google_storage_bucket_object" "src" {
  for_each = local.src_files
  bucket   = google_storage_bucket.lab.name
  name     = "src/${each.value}"
  source   = "${local.repo_root}/${each.value}"
}

resource "google_storage_bucket_object" "stress_env" {
  bucket = google_storage_bucket.lab.name
  name   = "config/stress.env"
  content = templatefile("${path.module}/templates/stress.env.tftpl", {
    base_url     = local.base_url
    frontend_url = local.frontend_url
    runners      = join(";", local.runner_ips)
    stage_plan   = var.stage_plan
    max_vus      = var.max_vus
  })
}
