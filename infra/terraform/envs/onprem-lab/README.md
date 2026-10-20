# 地端壓測 lab（GCE VM 模擬證券方 DMZ）

在 lab project 開 VM 模擬地端環境，驗證 [onprem/](../../../../onprem/README.md) 的安裝與操作流程。VM 開機時會自動執行與地端相同的 `onprem/install.sh`。

| 地端 | lab |
|---|---|
| 證券方 DMZ 網段 | VPC `k6-onprem-vpc`／subnet `k6-onprem-dmz`（10.60.0.0/24） |
| 防火牆 SNAT 固定 IP | Cloud NAT `k6-onprem-snat` + 靜態 IP（output `snat_ip`） |
| VPN／跳板機 → control SSH | IAP TCP forwarding，只開到 control |
| control → runner SSH | 防火牆只放行 control → runner TCP 22 |
| MF 帶安裝包過去 | bucket `<project>-k6-onprem-lab/src/`，開機時下載 |
| GCP UAT LB（受測端） | mock server VM `k6-mock-01`（預設），或我們在 lab 內自己部署的目標 |

| VM | 內部 IP | 預設機型 | 規劃規格 |
|---|---|---|---|
| k6-control-report-01 | 10.60.0.10 | e2-standard-2 | 4 vCPU／16 GB／200 GB SSD |
| k6-runner-01～03 | 10.60.0.11～13 | e2-standard-2 | 8 vCPU／32 GB／100 GB SSD |
| k6-mock-01 | 10.60.0.20 | e2-standard-2 | （lab 專用） |

預設是小規格加縮小版階段（最高 12 TPS，約 26 分鐘），只用來驗證流程。要做單台 runner 校正試跑時，把 `terraform.tfvars` 改成規劃規格（見 [terraform.tfvars.example](terraform.tfvars.example)）。

## 部署

state bucket 與雲端版共用（見 [infra/terraform/README.md](../../README.md)「初始化與套用」），prefix 不同。

```bash
cd infra/terraform/envs/onprem-lab
cp terraform.tfvars.example terraform.tfvars
terraform init -backend-config="bucket=cfh-de-chatbot-lab-k6-tfstate"
terraform apply
```

apply 完約 5 分鐘內，VM 會安裝完成（runner 會先等 control 上傳 SSH 公鑰）。進度看各 VM 的 `/var/log/k6-startup.log`。

## 使用

```bash
$(terraform output -raw control_ssh)      # 登入 control（需要 roles/iap.tunnelResourceAccessor 與 OS Login 權限）

k6ctl check
k6ctl preflight
k6ctl start lab-$(date +%Y%m%d-%H%M)
k6ctl wait                                 # 結束後自動產生報表

# 報表帶回本機（在本機執行）
gcloud compute scp --tunnel-through-iap --zone asia-east1-b \
  k6-control-report-01:/var/lib/k6-stress/runs/<RUN_ID>/reports/latest.html .
```

`apply` 不會啟動壓測，壓測一律由 `k6ctl start` 手動開始。

## 受測端

lab 是我們唯一能自己操作的環境。SIT／UAT 都是客戶環境，我們不能自己調整設定（reCAPTCHA、白名單、instance 數），**不要從 lab 打 SIT／UAT**。

預設打 lab 內的 mock server。如果要改打我們在 lab 內自己部署的目標：

```hcl
target_base_url = "https://<lab 內的受測端>"
enable_mock     = false
```

目標前面若有 IP 白名單，要加入的是 `snat_ip`（lab VM 對外的固定 IP）。

`stress.env` 只在 control 第一次開機時放置，之後 apply 不會覆蓋。已開機的 control 要直接改 `/etc/k6-stress/stress.env` 的 `BASE_URL`，或刪掉這個檔再重開機。

## 更新程式

`terraform apply` 會更新 bucket 內的程式，但已開機的 VM 不會自動重新下載。做法：

```bash
# 在 control 上重跑 startup script（重新下載並執行 install.sh）
sudo google_metadata_script_runner startup
```

runner 不用逐台更新：`k6ctl start` 每次都會把 control 上的程式同步到 runner。

## 注意

- control 重建時會產生新的 SSH 金鑰，runner 要重開機才會抓到新的公鑰。
- 不用時 `terraform destroy`，或把 VM 停機以節省費用。靜態 IP 在 VM 停機時仍會計費。
