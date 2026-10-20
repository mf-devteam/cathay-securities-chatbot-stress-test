terraform {
  required_version = ">= 1.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }

  # state bucket 用 -backend-config 指定（見 README.md），與雲端版共用同一個 bucket、不同 prefix
  backend "gcs" {
    prefix = "k6-stress/onprem-lab"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}
