terraform {
  required_version = ">= 1.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }

  # state bucket 用 -backend-config 指定（見 docs/cloud-run.md「初始化與套用」），不和其他團隊共用
  backend "gcs" {
    prefix = "k6-stress/lab"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
