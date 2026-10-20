# 地端壓測（證券方 DMZ）

依「新阿發_壓力測試_地端執行環境規劃_v1.0」：3 台 k6-runner＋1 台 control-report，放在證券方 DMZ，經防火牆 SNAT 打 GCP UAT 的 External LB。

- **業務情境：** 「業務情境組合」黃色列 8 項（與 Cloud Run 版相同）：B1 開場問候為每段對話開頭；B2、B2-2、B3、B4、B5、C1 依權重抽選（`SCENARIO_WEIGHTS='B2:28;B2-2:36;B3:15;B4:5;B5:2;C1:12'`）；A4 Token 續期需要登入，不在腳本流程內
- **階段：** S0 5 → S1 30 → S2 118 → S3 354 → S4 30 TPS，每階爬升 1 分鐘，一輪約 2 小時 15 分
- **lab：** 沒有地端機器時，用 [infra/terraform/envs/onprem-lab](../infra/terraform/envs/onprem-lab/README.md) 在 GCP 開 VM 模擬，跑的是同一份 `install.sh`

```
onprem/
├── install.sh              VM 安裝（--role runner|control；lab 另有 mock）
├── preflight.sh            連線預檢（不需要 k6，VM 建好即可執行）
├── bin/
│   ├── k6ctl               control：check／preflight／start／status／wait／stop／collect／report／dashboard
│   └── k6-agent            runner：由 k6ctl 透過 SSH 呼叫（背景跑 k6 + sar，記錄 PID 與 exit code）
├── config/
│   ├── defaults.env        規劃的正式參數（隨程式版本控管）
│   └── stress.env.example  環境設定範本 → /etc/k6-stress/stress.env
└── bundle/build-bundle.sh  打包安裝包（--with-deps：含離線套件）
```

## 1. 打包

```bash
onprem/bundle/build-bundle.sh                # VM 可連外（線上安裝套件）
onprem/bundle/build-bundle.sh --with-deps    # DMZ 無法連外：在同版 Ubuntu 上執行（lab control VM，或 Docker ubuntu:24.04）
```

產出 `dist/k6-stress-onprem-<版本>[-offline].tar.gz` 和 `.sha256`，帶到 control 後 scp 給各 runner。

## 2. 安裝（每台 VM 一次，root）

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
| control | SSH 金鑰、`k6ctl`、`/etc/k6-stress/stress.env`；python3-venv 建立 `/opt/k6-stress/venv`，安裝 pandas、plotly（[固定版本](config/report-requirements.txt)） |

可以重複執行（例如換新版程式、補加公鑰）。`/etc/k6-stress/stress.env` 已存在時不會被覆蓋。

## 3. 設定（control）

編輯 `/etc/k6-stress/stress.env`：至少填 `BASE_URL`、`FRONTEND_URL`、`RUNNERS`。其他參數沿用 [config/defaults.env](config/defaults.env)，要改時寫在 stress.env 覆蓋（不要改 defaults.env）。

## 4. 操作（control）

以任何帳號執行 `k6ctl` 都會自動切換成 `k6`（sudo）。

```bash
k6ctl check                         # SSH、k6 版本、nofile、時間差
k6ctl preflight                     # 連線預檢：control 與每台 runner 各跑一次，報告存在 /var/lib/k6-stress/preflight/

k6ctl start 20261105-r1             # 確認畫面輸入 yes 後，同步程式與參數到 runner 並同時啟動
k6ctl start 20261105-smoke STAGE_SCOPE=smoke    # 只跑 S0（K=V 只覆蓋這一輪）
k6ctl status                        # 每台 runner：執行中（PID）／已結束 exit code
k6ctl report                        # 收集＋產生「截至目前」的報表（壓測中也可以）
k6ctl stop                          # 全部停止（k6 收尾約 1 分鐘）
k6ctl wait                          # 等全部結束後自動 collect + report
k6ctl dashboard 0                   # 壓測中：SSH tunnel 到第 0 台 runner 的 k6 即時畫面（畫面會印出本機要開的 tunnel）
```

RUN_ID 省略時用最近一次的 RUN_ID。

**結束判斷（exit code）：** `0` 正常結束且門檻通過、`99` 門檻未過或觸發自動中止、`105` 手動中止、其他為異常（看 `runner.log`）。

**輸出（control 的 `/var/lib/k6-stress/runs/<RUN_ID>/`）：**

| 路徑 | 內容 |
|---|---|
| `reports/latest.html` | 合併報表（每分鐘 TPS、p95、錯誤率，分階段統計） |
| `raw/task-NNN.csv.gz` | 各 runner 的 k6 原始數據 |
| `tasks/task-NNN/` | 各 runner 的 k6 dashboard、summary.json、console log、`sar.bin`、`sar-summary.txt` |
| `run.env`、`runners.txt` | 本輪實際使用的參數與 runner 順序（task 編號） |

報表檔帶回本機：`gcloud compute scp`（lab）或透過跳板機 scp。

## 5. 連線預檢（不經 control）

VM 建好、還沒安裝時，證券方可以直接執行（只需要 Ubuntu 內建工具）：

```bash
./onprem/preflight.sh https://<UAT 網域>
```

判讀見規劃 p.17：步驟 5 回 `403` 代表網路已通、只差 reCAPTCHA 暫停；`302` 代表 Cloud Armor 白名單尚未加入。

## 與規劃 v1.0 的差異

- **爬升時間：** 規劃的階段時間加總為 2 小時 10 分；實作每階多 1 分鐘爬升（避免流量瞬間跳升），一輪約 2 小時 15 分。不需要時把 `STAGE_PLAN` 的第 4 欄改成 `0`。
- **業務情境：** 規劃 p.4 寫「只測未登入 AI 對話、排除會觸發地端 intent 的問題」；實作依「業務情境組合」黃色列，含 B2-2（FAQ intent）與 C1（查看 ETF 清單）。
- **k6 即時畫面：** runner 的 dashboard 只聽 `127.0.0.1:5665`，不對外開 port，經 `k6ctl dashboard` 的 SSH tunnel 查看，與規劃 p.13 相同。每台只代表 1/3 負載。
