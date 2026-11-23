# 地端版（目前主力）

依「新阿發_壓力測試_地端執行環境規劃_v1.0」：3 台 k6-runner＋1 台 control-report，放在證券方 DMZ，經防火牆 SNAT 打 GCP UAT 的 External LB。證券方執行人員在 control 上用 `k6menu` 操作。

- **範圍：** 只測未登入訪客，打到 3 倍（354 TPS）
- **OS：** RHEL 9.6（證券方，**離線安裝**）；Ubuntu 24.04／22.04 也支援（lab 預設）
- **lab：** 沒有地端機器時，在 GCP 開 VM 模擬，跑的是同一份 `install.sh`（見下方「lab」）
- 背景、決策、踩過的地雷、客戶環境現況見 [CLAUDE.md](../CLAUDE.md)

目錄：

- [1. 程式在哪裡](#1-程式在哪裡)
- [2. 三種測試](#2-三種測試)
- [3. 打包](#3-打包)
- [4. 安裝（每台 VM 一次，root）](#4-安裝每台-vm-一次root)
- [5. 設定（control）](#5-設定control)
- [6. 操作：k6menu](#6-操作k6menu)
- [7. 底層指令（進階操作、除錯）](#7-底層指令進階操作除錯)
- [8. 連線預檢（不經 control）](#8-連線預檢不經-control)
- [9. lab（GCE VM 模擬證券方 DMZ）](#9-labgce-vm-模擬證券方-dmz)
- [10. 與規劃 v1.0 的差異](#10-與規劃-v10-的差異)

## 1. 程式在哪裡

地端版自己的程式在 `onprem/`，k6 腳本與報表程式和雲端版共用：

```
onprem/
├── install.sh              VM 安裝（--role runner|control；lab 另有 mock）
├── preflight.sh            連線預檢（不需要 k6，VM 建好即可執行）
├── bin/
│   ├── k6menu              control：操作選單（給證券方執行人員，輸入數字操作）
│   ├── k6suite             control：v3 分開測試的串接（甲 → 休息 → 乙 → 合併報表），背景執行
│   ├── k6ctl               control：check／preflight／start／status／wait／stop／collect／report／dashboard
│   └── k6-agent            runner：由 k6ctl 透過 SSH 呼叫（背景跑 k6 + sar，記錄 PID 與 exit code）
├── config/
│   ├── defaults.env        預設參數（隨程式版本控管，每個參數都有註解）
│   └── stress.env.example  環境設定範本 → /etc/k6-stress/stress.env
└── bundle/
    ├── build-bundle.sh     打包安裝包（--with-deps：含離線套件，依執行的 OS）
    └── build-in-docker.sh  用 Docker 打包離線包：el9.6（RHEL 9.6）或 ubuntu24.04

runner/k6/                  k6 腳本（與雲端版共用），說明見 runner/k6/README.md
report-job/                 報表程式（與雲端版共用）：k6ctl 呼叫 main.py、qa_report.py；k6suite 呼叫 v3_report.py
infra/terraform/envs/onprem-lab/   lab 的 Terraform
```

單元測試：`cd report-job && python3 -m unittest discover -s tests`

## 2. 三種測試

| 測試 | k6menu | k6 腳本 | 報表 |
|---|---|---|---|
| **v3 分開測試**（目前客戶在用） | 主選單 2～8、13、14 | `script-v3-a-business.js`（腳本甲）、`script-v3-b-faq.js`（腳本乙）、`lib/v3.js` | `v3_report.py`：一份 HTML，甲、乙分開統計 |
| 對話回應時間測試 | 9 → 3、4 | `script-qa.js` | `qa_report.py`（`reports/qa-latest.html`） |
| 原始逐階加壓 | 9 → 1、2 | `script.js`：S0 5 → S1 30 → S2 118 → S3 354 → S4 30 TPS | `main.py`（`reports/latest.html`） |

**v3 分開測試：**

- 腳本甲＝業務查詢（B2-2、C1）→ 休息 `V3_GAP_MIN`（15 分鐘；試跑 `V3_SMOKE_GAP_MIN` 2 分鐘）→ 腳本乙＝FAQ（B2、B3、B4、B5）→ 合併報表。
- 甲、乙各自打滿 1 倍 118／3 倍 354 TPS（不依占比拆），1 分鐘爬升後固定負載 10 分鐘。
- 門檻：p95 甲 6 秒、乙 20 秒；對話失敗率 0.5%。判定只看固定負載期間。
- 也可以只跑甲或只跑乙（選單 13），不休息，報表上另一支顯示「未執行」。
- **對客戶一律稱「腳本甲／乙」**，程式內部代號是 A／B（`RUN_A`、`-A`）。
- 參數都在 [defaults.env](config/defaults.env) 的 `V3_*`。

**共通：**

- TPS＝聊天 API（`/api/chat/ask`）每秒請求數，含開場問候。開場問候照送，但不計入對話統計。
- 對話失敗＝5xx、逾時、HTTP 429，或回應內容不符預期（例如 B5 沒被 Model Armor 攔截）；失敗後照樣繼續問。
- 問句共用 [runner/k6/data/questions.json](../runner/k6/data/questions.json)。

## 3. 打包

**一定要從乾淨的 commit 打包**（`build-in-docker.sh` 會把工作區未 commit 的修改一起包進去）：在家目錄下開乾淨的 worktree 再打包（colima 只掛載 home）。步驟見 [package-delivery skill](../.claude/skills/package-delivery/SKILL.md)。

```bash
onprem/bundle/build-bundle.sh                # VM 可連外，或有內部套件庫（Satellite）：只有程式，套件線上安裝
onprem/bundle/build-in-docker.sh el9.6       # DMZ 無法連外：RHEL 9.6 離線包（本機需要 Docker，約 2 分鐘）
onprem/bundle/build-in-docker.sh ubuntu24.04 # 同上，Ubuntu 24.04
```

產出 `dist/k6-stress-onprem-<版本>[-<OS>-offline].tar.gz` 和 `.sha256`，帶到 control 後 scp 給各 runner。

**RHEL 離線包的套件來源：** Rocky Linux 9.6 vault（與 RHEL 9.6 二進位相容，固定在 9.6、不會漂到新版），打包時以 Rocky 官方金鑰驗證每個 rpm 的簽章，結果在 `deps/rpms/SIGNATURES`。安裝時 dnf 只補裝目標機缺少的套件（例如 rsync、bind-utils、sysstat 與其相依），已安裝的系統套件不動，也不會把 Rocky 金鑰匯入目標機。客戶要求只用 Red Hat 簽章的套件時，在已註冊的 RHEL 9.6 上以 `EL_REPOS=system onprem/bundle/build-bundle.sh --with-deps` 打包（需要 `dnf-plugins-core`、`createrepo_c`）。

## 4. 安裝（每台 VM 一次，root）

```bash
tar -xzf k6-stress-onprem-*.tar.gz && cd k6-stress

# control：先裝，記下最後印出的 SSH 公鑰
sudo ./onprem/install.sh --role control [--offline] [--ntp <證券方 NTP>]

# runner ×3：帶入 control 的公鑰
sudo ./onprem/install.sh --role runner --authorized-key 'ssh-ed25519 AAAA… k6-control@…' [--offline] [--ntp <證券方 NTP>]
```

| 角色 | 安裝內容 |
|---|---|
| 全部 | 帳號 `k6`（home 與數據在 `/var/lib/k6-stress`）、程式在 `/opt/k6-stress`、rsync／curl／openssl／dig |
| runner | k6 v2.3.0（驗證官方 checksum）、sysstat；`nofile 1048576`、`ip_local_port_range 1024 65535`、`tcp_tw_reuse 1`；允許 control 的公鑰 |
| control | SSH 金鑰、`k6menu`／`k6suite`／`k6ctl`、`/etc/k6-stress/stress.env`；python3-venv 建立 `/opt/k6-stress/venv`，安裝 pandas、plotly（[固定版本](config/report-requirements.txt)） |
| RHEL | SELinux enforcing 時，把 `/var/lib/k6-stress/.ssh` 標成 `ssh_home_t`（semanage＋restorecon），否則 sshd 讀不到 control 的公鑰；`--ntp` 會在 `/etc/chrony.conf` 加上 `sourcedir` 並重啟 chronyd |

可以重複執行（例如換新版程式、補加公鑰）。**`/etc/k6-stress/` 底下的 `stress.env`、`questions.json` 已存在時不會被覆蓋**，客戶改過的設定會保留。

## 5. 設定（control）

編輯 `/etc/k6-stress/stress.env`：填 `RUNNERS`（3 台 runner 的 IP）。`BASE_URL`、`FRONTEND_URL` 已預先填好 UAT（`https://afa.icathaysec-uat.com.tw`），換受測端時才要改。

其他參數沿用 [config/defaults.env](config/defaults.env)，要改時寫在 stress.env 覆蓋，**不要改 defaults.env**。門檻、休息時間、問句、測試情境，客戶可以直接在 k6menu 10、11、14 修改（同樣寫進 `/etc/k6-stress/`）。

## 6. 操作：k6menu

在 control 輸入 `k6menu`，依數字操作。開始與停止前要輸入 `yes` 確認，結果以白話顯示；測試在背景執行，關閉視窗或斷線都不會中斷。

| 選單 | 功能 |
|---|---|
| 1 | 壓測前檢查（control、runner 與受測端的連線，約 1 分鐘） |
| 2 | 試跑 甲→乙（低流量，確認流程與報表正常） |
| 3／4 | 正式測試 1 倍／3 倍（甲 → 休息 → 乙 → 合併報表） |
| 5 | 查看進度（每 10 秒更新，含預計完成時間） |
| 6 | 停止測試（還沒跑的腳本不會開始，用停止前的數據產生報表） |
| 7 | 取得報表：列出最近 8 組測試（類型、時間、甲乙結論）讓執行人員選，顯示報表位置，也可以重新產生；測試進行中可選 `y` 產生「截至目前」的報表、`l` 看之前的報表 |
| 8 | 打包問題資料：產生 `/var/lib/k6-stress/support/k6-support-*.tar.gz`（系統資訊、設定、預檢結果、最近 2 輪的 log 與報表，不含原始 CSV），寄給 `SUPPORT_CONTACT` |
| 9 | 其他測試（舊版）：1 試跑、2 逐階加壓、3／4 對話回應時間測試 1 倍／3 倍 |
| 10 | 調整設定：回應時間門檻、失敗率門檻、休息時間（寫進 stress.env，修改紀錄在 `/var/lib/k6-stress/settings-history.log`） |
| 11 | 管理測試問句（寫進 `/etc/k6-stress/questions.json`；B1 開場問候固定不開放修改） |
| 12 | 使用說明（每個按鈕在做什麼、目前的設定） |
| 13 | 單獨跑一支腳本：只跑甲或只跑乙，1 倍／3 倍 |
| 14 | 選擇測試情境：每支腳本要測哪些情境（寫進 stress.env 的 `V3_A_WEIGHTS`／`V3_B_WEIGHTS`） |

**v3 測試的輸出：** `/var/lib/k6-stress/suites/<測試編號>/reports/report-<測試編號>.html`；甲、乙各自的原始數據在 `/var/lib/k6-stress/runs/<測試編號>-A`、`-B`。

## 7. 底層指令（進階操作、除錯）

以任何帳號執行 `k6menu`、`k6suite`、`k6ctl` 都會自動切換成 `k6`（sudo）。客戶 root 的 PATH 沒有 `/usr/local/bin`，要用 `/usr/bin/k6ctl` 或完整路徑。

```bash
# v3 分開測試（k6menu 2～8、13 的底層）
k6suite start x1                    # 背景開始：甲 → 休息 → 乙 → 合併報表（smoke／x1／x3）
k6suite start x3 --only A           # 只跑腳本甲（--only B＝只跑腳本乙）
k6suite progress                    # 進度
k6suite stop                        # 整組停止
k6suite report                      # 重新產生合併報表
k6suite list                        # 所有測試編號

# 單一輪（舊版測試，也是 k6suite 每支腳本的底層）
k6ctl check                         # SSH、k6 版本、nofile、時間差
k6ctl preflight                     # 連線預檢：control 與每台 runner 各跑一次，報告存在 /var/lib/k6-stress/preflight/
k6ctl start 20261105-r1             # 確認畫面輸入 yes 後，同步程式與參數到 runner 並同時啟動
k6ctl start 20261105-smoke STAGE_SCOPE=smoke    # K=V 只覆蓋這一輪
k6ctl status                        # 每台 runner：執行中（PID）／已結束 exit code
k6ctl report                        # 收集＋產生「截至目前」的報表（壓測中也可以）
k6ctl stop                          # 全部停止（k6 收尾約 1 分鐘）
k6ctl wait                          # 等全部結束後自動 collect + report
k6ctl dashboard 0                   # 壓測中：SSH tunnel 到第 0 台 runner 的 k6 即時畫面（每台只代表 1/3 負載）
```

- RUN_ID 省略時用最近一次的 RUN_ID。
- `k6ctl start` 不會擋「已經有一輪在跑」（k6menu、k6suite 會擋），要先 `k6ctl status`。
- **exit code：** `0` 正常結束且門檻通過、`99` 門檻未過或觸發自動中止、`105` 手動中止、其他為異常（看 `runner.log`）。看到 99 先查是哪個門檻觸發，不一定是工具壞掉。

**單一輪的輸出（control 的 `/var/lib/k6-stress/runs/<RUN_ID>/`）：**

| 路徑 | 內容 |
|---|---|
| `reports/latest.html` | 合併報表（每分鐘 TPS、p95、錯誤率，分階段統計） |
| `reports/qa-latest.html` | 對話回應時間報表（有對話數據時才產生） |
| `raw/task-NNN.csv.gz` | 各 runner 的 k6 原始數據 |
| `tasks/task-NNN/` | 各 runner 的 k6 dashboard、summary.json、console log、`sar.bin`、`sar-summary.txt` |
| `run.env`、`runners.txt`、`questions.json` | 本輪實際使用的參數、runner 順序（task 編號）、問句 |

## 8. 連線預檢（不經 control）

VM 建好、還沒安裝時，證券方可以直接執行（只需要 RHEL／Ubuntu 內建工具）：

```bash
./onprem/preflight.sh https://<UAT 網域>
```

判讀見規劃 p.17：步驟 5 回 `403` 代表網路已通、只差 reCAPTCHA 暫停；`302` 代表 Cloud Armor 白名單尚未加入。

## 9. lab（GCE VM 模擬證券方 DMZ）

在 lab 專案 `cfh-de-chatbot-lab` 開 VM 模擬地端環境，驗證安裝與操作流程。VM 開機時會自動執行與地端相同的 `onprem/install.sh`。Terraform 在 [infra/terraform/envs/onprem-lab](../infra/terraform/envs/onprem-lab/)；驗證步驟見 [lab-verify skill](../.claude/skills/lab-verify/SKILL.md)。

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

預設是小規格加縮小版階段（最高 12 TPS，約 26 分鐘），只用來驗證流程。要做單台 runner 校正試跑時，把 `terraform.tfvars` 改成規劃規格（見 [terraform.tfvars.example](../infra/terraform/envs/onprem-lab/terraform.tfvars.example)）。

### 部署

state bucket 與雲端版共用（建立方式見 [docs/cloud-run.md](../docs/cloud-run.md#初始化與套用)），prefix 不同。`terraform apply` 會被 Claude 的 auto mode 擋下，請自己執行。

```bash
cd infra/terraform/envs/onprem-lab
cp terraform.tfvars.example terraform.tfvars
terraform init -backend-config="bucket=cfh-de-chatbot-lab-k6-tfstate"
terraform apply
```

apply 完約 5 分鐘內，VM 會安裝完成（runner 會先等 control 上傳 SSH 公鑰）。進度看各 VM 的 `/var/log/k6-startup.log`。

**目前部署（2026-10-20）：**

| 項目 | 值 |
|---|---|
| 對外固定 IP（`snat_ip`） | `35.229.164.234`：lab VM 對外連線都從這個 IP 出去；受測端有 IP 白名單時加入這個 IP |
| control | `k6-control-report-01`（10.60.0.10） |
| runner | `k6-runner-01`～`03`（10.60.0.11～13） |
| 受測端 | mock `k6-mock-01`（`http://10.60.0.20:8090`） |
| 程式 bucket | `cfh-de-chatbot-lab-k6-onprem-lab` |

`terraform destroy` 後重建，靜態 IP 會換成新的，這裡要跟著更新（`terraform output snat_ip`）。

### 使用

```bash
$(terraform output -raw control_ssh)      # 登入 control（權限與第一次登入見下方）

k6ctl check
k6menu                                     # 和客戶一樣用選單操作

# 報表帶回本機（在本機執行）
gcloud compute scp --tunnel-through-iap --zone asia-east1-b \
  k6-control-report-01:/var/lib/k6-stress/suites/<測試編號>/reports/latest.html .
```

`apply` 不會啟動壓測，壓測一律手動開始。

### 更新程式

`terraform apply` 會更新 bucket 內的程式，但已開機的 VM 不會自動重新下載。做法：

```bash
# 在 control 上重跑 startup script（重新下載並執行 install.sh）
sudo google_metadata_script_runner startup
```

runner 不用逐台更新：`k6ctl start` 每次都會把 control 上的程式同步到 runner。

### 登入 control（第一次使用）

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
- 證券方地端是 RHEL 9.6：`terraform.tfvars` 設 `onprem_os = "rhel"` 後 apply，control 與 runner 會**重建**成 RHEL 9（GCE 只提供最新 9.x，另計授權費），mock 不受影響。重建前先把要留的報表帶回本機。**驗證離線安裝的套件衝突要用真的 RHEL 映像**（`rhel-9-v20250611`＝RHEL 9.6），Rocky 容器和連網的 lab 都測不出來。

**3. 登入後**

```bash
sudo tail -5 /var/log/k6-startup.log    # 最後一行是 ==== … done 代表安裝完成
k6ctl check                              # 3 台 runner 都要 ✓
```

**連不上時：** 先加 `--troubleshoot` 再跑一次，它會檢查網路、IAP 權限、VM 狀態，指出卡在哪一層。這個參數可能會要求啟用 Network Management／Monitoring API，同意即可（不收費）。

| 錯誤訊息 | 原因 | 處理 |
|---|---|---|
| `does not have permission … importSshPublicKey … roles/compute.osLoginExternalUser` | VM 開了 OS Login，我方帳號屬於外部組織 | 確認 `enable_oslogin = false`（預設）後 apply |
| `4033: 'not authorized'`，或 troubleshoot 顯示缺 `iap.tunnelInstances.accessViaIAP` | 缺 IAP 權限，或剛加還沒生效 | 補 `roles/iap.tunnelResourceAccessor`，等幾分鐘再試 |
| `Permission denied (publickey)`、`Could not SSH into the instance`（IAP 權限已確認） | 公鑰還沒套用；或 VM 開機後才切換 OS Login，sshd 沒重新載入設定 | 等 1 分鐘重試；還不行就重開 control：`gcloud compute instances reset k6-control-report-01 --zone asia-east1-b --project cfh-de-chatbot-lab` |

**為什麼不用 OS Login：** lab 專案屬於外部組織。用 OS Login 的話，我方帳號要有該組織層級的 `roles/compute.osLoginExternalUser`，要對方組織管理員才能授權。所以預設 `enable_oslogin = false`，改由 `gcloud compute ssh` 把公鑰寫進該台 VM 的 metadata。`block-project-ssh-keys` 已開啟，專案層級的公鑰不會套用到這些 VM。拿到組織權限後，可以改 `enable_oslogin = true` 再 apply。

### 受測端

lab 是我們唯一能自己操作的環境。SIT／UAT 都是客戶環境，我們不能自己調整設定（reCAPTCHA、白名單、instance 數），**不要從 lab 打 SIT／UAT**。

| 受測端 | 用途 | 流量路徑 |
|---|---|---|
| mock server `k6-mock-01`（預設） | 驗證工具流程：安裝、啟停、收集、報表 | VPC 內部直連，不經 NAT |
| lab 的阿發（我們自己的部署） | 接近客戶環境的實測 | NAT 固定 IP → External LB → Cloud Armor → BFF → AI，與規劃 p.5 的地端路徑相同 |

**打 lab 阿發前的必做項目**（對應規劃 p.16「網路需求：雲端」，壓測前完成，測完恢復）：

| # | 項目 | 為什麼 | 沒做會怎樣（`k6ctl preflight`） | 測完 |
|---|---|---|---|---|
| 1 | Cloud Armor 白名單加入 `35.229.164.234`（`snat_ip`） | 5 台 VM 都經同一個 Cloud NAT 出去，阿發看到的來源 IP 只有這一個 | 步驟 4／5 回 302，被導到維護頁 | 可保留（只有 lab VM 用這個 IP） |
| 2 | 暫停 reCAPTCHA | 壓測工具拿不到 reCAPTCHA token | 步驟 5 回 403 | **恢復** |
| 3 | 調高訪客每日次數上限 | 一段對話 8～11 則訊息，超過一般上限 | 預檢會過，但壓測中會出現業務錯誤 | **恢復** |
| 4 | 確認 AI 配額與費用 | 每則訊息都會呼叫 Conversational Agent／Gemini／Model Armor；縮小版一輪約 8,000 次請求 | 配額不足時，壓測中會回錯誤 | — |
| 5 | Cloud Run instance 設定 | 確認 max／min instances 足以承載這輪流量，測試中不要調整 | — | — |

**改打 lab 阿發：** control 已開機時，直接改 control 上的 `/etc/k6-stress/stress.env`，立即生效，不需要 apply：

```bash
sudo -u k6 vi /etc/k6-stress/stress.env
# BASE_URL=https://<阿發 API 網域>          腳本打 ${BASE_URL}/api/chat/ask
# FRONTEND_URL=https://<阿發前端網址>       預檢步驟 4
```

建議依序：`k6ctl preflight`（每台只送 2 個請求，步驟 5 要是 HTTP 200 加上 AI 回覆）→ k6menu 2 試跑 → 沒問題再跑正式測試。

要讓之後重建的 control 預設打阿發，在 `terraform.tfvars` 設定：

```hcl
target_base_url     = "https://<阿發 API 網域>"
target_frontend_url = "https://<阿發前端網址>"
enable_mock         = false
```

`stress.env` 只在 control 第一次開機時放置，之後 apply 不會覆蓋已開機的 control。

### 注意

- control 重建時會產生新的 SSH 金鑰，runner 要重開機才會抓到新的公鑰。
- 不用時 `terraform destroy`，或把 VM 停機以節省費用。靜態 IP 在 VM 停機時仍會計費。

## 10. 與規劃 v1.0 的差異

- **爬升時間：** 規劃的階段時間加總為 2 小時 10 分；逐階加壓每階多 1 分鐘爬升（避免流量瞬間跳升），一輪約 2 小時 15 分。不需要時把 `STAGE_PLAN` 的第 4 欄改成 `0`。
- **業務情境：** 規劃 p.4 寫「只測未登入 AI 對話、排除會觸發地端 intent 的問題」；實作依「業務情境組合」黃色列，含 B2-2（FAQ intent）與 C1（查看 ETF 清單）。
- **測試方式：** 客戶 2026-11-13 改為 v3 分開測試（業務查詢、FAQ 分兩支腳本，各自判定），原本的逐階加壓與對話回應時間測試保留在選單 9。
- **k6 即時畫面：** runner 的 dashboard 只聽 `127.0.0.1:5665`，不對外開 port，經 `k6ctl dashboard` 的 SSH tunnel 查看，與規劃 p.13 相同。每台只代表 1/3 負載。
