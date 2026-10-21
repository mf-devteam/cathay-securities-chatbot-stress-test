# 地端壓測 lab（GCE VM 模擬證券方 DMZ）

在 lab project 開 VM 模擬地端環境，驗證 [onprem/](../../../../onprem/README.md) 的安裝與操作流程。VM 開機時會自動執行與地端相同的 `onprem/install.sh`。

| 地端 | lab |
|---|---|
| 證券方 DMZ 網段 | VPC `k6-onprem-vpc`／subnet `k6-onprem-dmz`（10.60.0.0/24） |
| 防火牆 SNAT 固定 IP | Cloud NAT `k6-onprem-snat` + 靜態 IP（output `snat_ip`） |
| VPN／跳板機 → control SSH | IAP TCP forwarding，只開到 control |
| control → runner SSH | 防火牆只放行 control → runner TCP 22 |
| MF 帶安裝包過去 | bucket `<project>-k6-onprem-lab/src/`，開機時下載 |
| GCP UAT LB（受測端） | mock server VM `k6-mock-01`（預設），或 lab 的阿發 |

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

### 目前部署（2026-10-20）

| 項目 | 值 |
|---|---|
| 對外固定 IP（`snat_ip`） | `35.229.164.234`：lab VM 對外連線都從這個 IP 出去；受測端有 IP 白名單時加入這個 IP |
| control | `k6-control-report-01`（10.60.0.10） |
| runner | `k6-runner-01`～`03`（10.60.0.11～13） |
| 受測端 | mock `k6-mock-01`（`http://10.60.0.20:8090`） |
| 程式 bucket | `cfh-de-chatbot-lab-k6-onprem-lab` |

`terraform destroy` 後重建，靜態 IP 會換成新的，這裡要跟著更新（`terraform output snat_ip`）。

## 使用

```bash
$(terraform output -raw control_ssh)      # 登入 control（權限與第一次登入見下方「登入 control」）

k6ctl check
k6ctl preflight
k6ctl start lab-$(date +%Y%m%d-%H%M)
k6ctl wait                                 # 結束後自動產生報表

# 報表帶回本機（在本機執行）
gcloud compute scp --tunnel-through-iap --zone asia-east1-b \
  k6-control-report-01:/var/lib/k6-stress/runs/<RUN_ID>/reports/latest.html .
```

`apply` 不會啟動壓測，壓測一律由 `k6ctl start` 手動開始。

### 登入 control（同事第一次使用）

**1. 需要的權限（在 `cfh-de-chatbot-lab` 的 IAM）**

| 角色 | 用途 |
|---|---|
| `IAP-secured Tunnel User`（`roles/iap.tunnelResourceAccessor`） | 經 IAP 連到 control（防火牆只開 IAP → control 22，連不到其他 VM） |
| 可以修改 VM metadata 的角色，例如 `Editor` 或 `Compute Instance Admin (v1)` | `gcloud compute ssh` 要把你的公鑰寫進 control 的 metadata |

沒有 IAM 管理權限時請專案管理員加。也可以在 `terraform.tfvars` 設定 `iap_members = ["user:<帳號>"]`，只授權在 control 這台 VM 上，再 apply。權限加完約 1～7 分鐘生效。

**2. 登入**

```bash
gcloud auth login            # 用公司帳號登入 gcloud（第一次）
gcloud compute ssh k6-control-report-01 --zone asia-east1-b --project cfh-de-chatbot-lab --tunnel-through-iap
```

第一次登入時，gcloud 會產生 SSH 金鑰（按 Enter 用預設值），並寫進 control 的 metadata，等十幾秒就能連上，登入後可以用 sudo。

- 出現 `consider installing NumPy` 的警告：只是 IAP 傳輸效能建議，可以忽略。
- 登入畫面提示 `New release … available`：不要升級。
- 證券方地端是 RHEL 9.6：`terraform.tfvars` 設 `onprem_os = "rhel"` 後 apply，control 與 runner 會**重建**成 RHEL 9（GCE 只提供最新 9.x，另計授權費），mock 不受影響。重建前先把要留的報表帶回本機。

**3. 登入後**

```bash
sudo tail -5 /var/log/k6-startup.log    # 最後一行是 ==== … done 代表安裝完成
k6ctl check                              # 3 台 runner 都要 ✓
```

**連不上時**

先加 `--troubleshoot` 再跑一次，它會檢查網路、IAP 權限、VM 狀態，指出卡在哪一層。這個參數可能會要求啟用 Network Management／Monitoring API，同意即可（不收費）。

| 錯誤訊息 | 原因 | 處理 |
|---|---|---|
| `does not have permission … importSshPublicKey … roles/compute.osLoginExternalUser` | VM 開了 OS Login，我方帳號屬於外部組織 | 確認 `enable_oslogin = false`（預設）後 apply |
| `4033: 'not authorized'`，或 troubleshoot 顯示缺 `iap.tunnelInstances.accessViaIAP` | 缺 IAP 權限，或剛加還沒生效 | 補 `roles/iap.tunnelResourceAccessor`，等幾分鐘再試 |
| `Permission denied (publickey)`、`Could not SSH into the instance`（IAP 權限已確認） | 公鑰還沒套用；或 VM 開機後才切換 OS Login，sshd 沒重新載入設定 | 等 1 分鐘重試；還不行就重開 control：`gcloud compute instances reset k6-control-report-01 --zone asia-east1-b --project cfh-de-chatbot-lab` |

### 為什麼不用 OS Login

lab 專案屬於外部組織。用 OS Login 的話，我方帳號要有該組織層級的 `roles/compute.osLoginExternalUser`，要對方組織管理員才能授權。所以預設 `enable_oslogin = false`，改由 `gcloud compute ssh` 把公鑰寫進該台 VM 的 metadata。`block-project-ssh-keys` 已開啟，專案層級的公鑰不會套用到這些 VM。拿到組織權限後，可以改 `enable_oslogin = true` 再 apply。

## 受測端

lab 是我們唯一能自己操作的環境。SIT／UAT 都是客戶環境，我們不能自己調整設定（reCAPTCHA、白名單、instance 數），**不要從 lab 打 SIT／UAT**。

| 受測端 | 用途 | 流量路徑 |
|---|---|---|
| mock server `k6-mock-01`（預設） | 驗證工具流程：安裝、啟停、收集、報表 | VPC 內部直連，不經 NAT |
| lab 的阿發（我們自己的部署） | 接近客戶環境的實測 | NAT 固定 IP → External LB → Cloud Armor → BFF → AI，與規劃 p.5 的地端路徑相同 |

### 打 lab 阿發前的必做項目

對應規劃 p.16「網路需求：雲端」。壓測前完成，測完恢復：

| # | 項目 | 為什麼 | 沒做會怎樣（`k6ctl preflight`） | 測完 |
|---|---|---|---|---|
| 1 | Cloud Armor 白名單加入 `35.229.164.234`（`snat_ip`） | 5 台 VM 都經同一個 Cloud NAT 出去，阿發看到的來源 IP 只有這一個 | 步驟 4／5 回 302，被導到維護頁 | 可保留（只有 lab VM 用這個 IP） |
| 2 | 暫停 reCAPTCHA | 壓測工具拿不到 reCAPTCHA token | 步驟 5 回 403 | **恢復** |
| 3 | 調高訪客每日次數上限 | 一段對話 8～11 則訊息，超過一般上限 | 預檢會過，但壓測中會出現業務錯誤 | **恢復** |
| 4 | 確認 AI 配額與費用 | 每則訊息都會呼叫 Conversational Agent／Gemini／Model Armor；縮小版一輪約 8,000 次請求 | 配額不足時，壓測中會回錯誤 | — |
| 5 | Cloud Run instance 設定 | 確認 max／min instances 足以承載這輪流量，測試中不要調整 | — | — |

### 改打 lab 阿發

control 已開機時，直接改 control 上的 `/etc/k6-stress/stress.env`，立即生效，不需要 apply：

```bash
sudo -u k6 vi /etc/k6-stress/stress.env
# BASE_URL=https://<阿發 API 網域>          腳本打 ${BASE_URL}/api/chat/ask
# FRONTEND_URL=https://<阿發前端網址>       預檢步驟 4
```

建議依序執行：

1. `k6ctl preflight`：每台只送 2 個請求，確認上面的必做項目都已完成。步驟 5 要是 HTTP 200 加上 AI 回覆。
2. `k6ctl start <RUN_ID> STAGE_SCOPE=smoke`：只跑 S0，最高 1 TPS，約 4 分鐘。
3. smoke 沒問題，再跑完整的縮小版（最高 12 TPS，約 26 分鐘）。

要讓之後重建的 control 預設打阿發，在 `terraform.tfvars` 設定：

```hcl
target_base_url     = "https://<阿發 API 網域>"
target_frontend_url = "https://<阿發前端網址>"
enable_mock         = false
```

`stress.env` 只在 control 第一次開機時放置，之後 apply 不會覆蓋已開機的 control。

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
