# CLAUDE.md

國泰證券「新阿發」AI 客服的 k6 壓力測試工具。客戶＝證券方；我方＝MF（Microfusion）。
這份檔案記錄**文件裡看不出來的背景、決策與地雷**。一般說明：地端版 [onprem/README.md](onprem/README.md)、雲端版 [docs/cloud-run.md](docs/cloud-run.md)；`docs/` 其餘是 10 月規劃期的資料，可以先跳過。
文件整理原則：**一個版本一份說明**，不要在子目錄另開 README（`runner/k6/` 的腳本與 mock 說明除外）。

## 目前進度與待辦（2026-11-23）

**下一版交付：程式 `c1f1a18`＋手冊 v1.6**，交付資料夾 `~/Downloads/新阿發壓測工具_交付_20261123/`。內容（相對 11/16 的 `54f0049`＋v1.5）：

- 選單 13 單獨跑甲或乙（1 倍／3 倍，不休息，報表上另一支「未執行」）；選單 14 選擇測試情境；選單 7 列出最近 8 組測試再挑報表（單獨跑甲、乙是兩組）。
- 甲、乙之間休息 15 分鐘（試跑 2 分鐘）。
- B2-2「密碼補發」→「ETF募集」：預設問句，加上 `install.sh` 安裝時修正客戶改過的 questions.json。
- 報表程式只多了「只跑腳本甲／乙」的文字（使用者要求報表不重新設計）。

**lab 驗證（RHEL 9 control＋mock，lab 負載 1 倍＝6 TPS）：**

| 項目 | 結果 |
|---|---|
| 只跑甲 1 倍（`20261123-v3x1a-01`） | ✓ 沒休息、乙「未執行」、報表自動產生；甲「未通過」是 mock 慢（p95 7.16 秒），符合預期 |
| 選單 7 的測試列表 | ✓（在 control 上直接執行 `suite_line`，互動畫面待使用者操作） |
| B2-2 問句修正 | ✓ lab control 的問句檔被修正、有備份與紀錄；再裝一次不會重複修改 |
| 只跑乙、完整甲＋乙（休息 15 分鐘）、試跑（休息 2 分鐘）、舊版試跑（選單 9 → 1） | **還沒跑** |
| RHEL 離線包安裝（`--offline`） | **還沒做**：lab 目前裝的是 Mac 打的「只有程式」包。install.sh 這次只加了問句修正，沒動套件 |

- lab 的乙目前拿掉了 B4（使用者測選單 14 時改的），測完用選單 14 → 9 恢復。
- 口頭提醒客戶（**我們不動客戶的設定檔**）：
  - 更新後照手冊 v1.6 附錄「更新成新版」步驟 5，確認 B2-2 第 1 句是「ETF募集」。
  - B5 問句被改成「我想知道台股交易手續費怎麼算」，要用選單 11 改回「我要放火燒你全家」。
  - 乙目前沒有測 B5，要測時用選單 14 加回來。
  - 他們的 stress.env 把「1 倍」設成 39 TPS、「3 倍」設成 118 TPS，新的單獨執行按鈕也照這個設定跑。

## 兩個版本

| | 雲端版（Cloud Run Jobs） | 地端版（目前主力） |
|---|---|---|
| 位置 | `runner/`、`report-job/`、`infra/terraform/envs/lab` | `onprem/`（install.sh、k6ctl、k6menu、k6suite）；共用 `runner/k6` 與 `report-job` |
| 環境 | GCP 多個 task 平行，數據放 GCS | 證券方 DMZ：3 台 runner＋1 台 control，RHEL 9.6、**離線安裝** |
| 範圍 | 1 倍上限、「業務情境組合」Excel 黃色列 8 項 | 只測未登入訪客，打到 3 倍（354 TPS） |

目前開發分支：`feature/onprem-new-scenarios-v3`（比 main 新很多，還沒合併）。

## 地端版的三種測試（都在 k6menu）

1. **v3 分開測試（主選單 1～8、13、14，目前客戶在用）：** 腳本甲＝業務查詢（B2-2、C1）→ 休息 → 腳本乙＝FAQ（B2、B3、B4、B5）→ 合併成一份 HTML 報表，甲、乙分開統計。
   - 甲、乙各自打滿 118／354 TPS（**不依**占比拆）。
   - 門檻 p95：甲 6 秒、乙 20 秒；失敗率暫定 0.5%（`V3_FAIL_RATE`）。
   - 程式：`script-v3-a-business.js`、`script-v3-b-faq.js`、`lib/v3.js`、`report-job/v3_report.py`、`onprem/bin/k6suite`。
   - **對客戶一律稱「腳本甲／乙」**，程式內部代號仍是 A／B。
   - 選單 13 可以單獨跑甲或乙（`k6suite start x1 --only A`），不休息，報表上另一支顯示「未執行」。選單 14 開關每支腳本的情境（寫進 stress.env 的 `V3_A_WEIGHTS`／`V3_B_WEIGHTS`）。
   - 甲、乙之間休息 `V3_GAP_MIN`＝15 分鐘（2026-11-22 起）；試跑另用 `V3_SMOKE_GAP_MIN`＝2 分鐘。
2. **對話回應時間測試（選單 9 → 子選單）：** `script-qa.js`、`qa_report.py`、`QA_*` 參數。
3. **原始逐階加壓（選單 9 → 子選單）：** `script.js`，S0 5 → S1 30 → S2 118 → S3 354 → S4 30 TPS。

- 問句共用 `runner/k6/data/questions.json`，**不要拆檔**。客戶可以在選單 11 修改 `/etc/k6-stress/questions.json`，`k6ctl start` 時會同步到 runner。B1 固定用程式附的問句。客戶改過的問句要強制修正時，加在 `install.sh` 的 `migrate_questions`（一次性替換、先備份；2026-11-23 起把 B2-2「密碼補發」換成「ETF募集」，因為密碼補發會進 CA 流程，數據不對）。客戶曾把 B5 改成一般問句（11/19），我們決定不鎖、口頭提醒改回「我要放火燒你全家」。
- 重新安裝不會覆蓋 `/etc/k6-stress/` 的 stress.env 與 questions.json。客戶的 stress.env 目前把 1 倍改成 39 TPS、3 倍改成 118 TPS（`V3_PLAN_X*`），乙的情境拿掉了 B5（11/20 報表）。
- TPS＝聊天 API（`/api/chat/ask`）每秒請求數，包含開場問候。

## 慣例

- **參數一律走環境變數。** 列表參數用分號字串（`B2:28;C1:12`、`x1:118tps:10:1`），不用 JSON，因為 `gcloud --update-env-vars` 會用逗號切斷 JSON。`THRESHOLDS_JSON` 是唯一例外。新增參數時要同步更新 [runner/k6/README.md](runner/k6/README.md) 的參數一覽。
- **預設值放 `onprem/config/defaults.env`**（跟著版本控管）；現場值寫在 control 的 `/etc/k6-stress/stress.env` 覆蓋，不要叫客戶改 defaults.env。
- **新需求不要動到既有測試。** 加新測試時另開 script／report 檔，舊腳本、舊報告保持原樣（客戶要求）。
- **寫給客戶的指令要能直接複製貼上**，不要放 `<編號>` 這類佔位符（客戶會原樣貼上）。用 `$(date …)`、`$(ls -1t … | head -1)` 自動帶入。客戶執行人員不熟指令，以 k6menu 操作為主。
- **git add 要逐一列出檔案**，不要用 `git add -A` 或 `git add .`。根目錄有客戶的 xlsx／pdf，`logs/` 是客戶 log，都不能進 git。
- 單元測試：`cd report-job && python3 -m unittest discover -s tests`

## 地雷（踩過的）

- **打包一定要從乾淨的 commit。** `build-in-docker.sh` 會把工作區未 commit 的修改一起包進去。做法：在家目錄下開乾淨的 worktree 再打包（colima 只掛載 home）。
- **RHEL 驗證要用真的 RHEL 映像離線安裝**（GCE `rhel-9-v20250611`＝RHEL 9.6）。Rocky 容器和連網的 lab 都測不出套件衝突（10/27 客戶安裝時，Rocky 的 openssh 跟 RHEL 原廠衝突）。
- 客戶 root 的 PATH 沒有 `/usr/local/bin` → 指令要用 `/usr/bin/k6ctl` 連結或寫完整路徑。
- `k6ctl start` 不會擋「已經有一輪在跑」（k6menu 會擋），要先 `k6ctl status`。
- exit code：`0` 通過、`99` 門檻沒過或自動中止、`105` 手動停止。看到 99 先查是哪個門檻觸發，不一定是工具壞掉。
- `terraform apply` 會被 Claude 的 auto mode 擋下，請使用者自己執行。

## 環境與權限

- **lab＝`cfh-de-chatbot-lab`：** 我們唯一能自己操作的 GCP 專案。地端模擬在 `infra/terraform/envs/onprem-lab`（GCE VM＋mock），資源一律用 `k6` 前綴。
- **SIT／UAT 是客戶環境，我們不能操作，也不要從 lab 打 SIT／UAT。** UAT 的 LB、Cloud Armor、Cloud Run 規格由證券方 infra 負責。
- 對任何真實環境送流量前，都要先取得負責人同意。

## 客戶 UAT 的已知狀況（2026-11-20）

- 10/30～11/04 連線被拒：ISP DDoS 白名單已修好。
- 11/11 BFF 排隊：客戶已調高 Cloud Run 規格。
- 11/12 1 倍跑出大量 429：根因是 Cloud Run「沒有可用 instance」，測試當下的 revision 最大 instance 數＝3，設定沒生效。
- 現況：bff、middleware 的 concurrency 還是 10（應該是 80）。**下次跑 1 倍／3 倍之前，要提醒客戶把 concurrency 改成 80，部署設定也要一起改**，否則重新部署會被蓋回去。
- 容量估算：1 倍約 900、3 倍約 2,700 個同時請求；60 × 80 = 4,800 足夠。
- 就算沒負載，業務 p95 也有 7～8 秒，主要是 LLM 慢。
- 11/17 1 倍：Cloud SQL CPU 100%，主因是資料庫沒有索引；索引 SQL 在 middleware 的 `feature/history-indexes` 分支，配額問題等客戶回覆。分析在 `logs/1117/`。
- 11/20：客戶只跑了 39 TPS 的甲（跑完手動停止），失敗率 0.70%（門檻 0.5%）、p95 2.63 秒。

## 不在 repo 裡的資料（交接時另外提供）

- 客戶 log：`logs/`（公司雲端硬碟），每次的分析寫在 `logs/<日期>/*_問題分析.md`。
- 交付包與操作手冊：`~/Downloads/新阿發壓測工具_交付_<日期>/`。最新是 20261116：安裝包 `k6-stress-onprem-54f0049-el9.6-offline`＋手冊 v1.5（選單操作）。
- 規劃文件：「新阿發 壓力測試規劃 v1.4」、「地端執行環境規劃 v1.0」、負載試算表。
- 支援信箱：tsc-pdev.service@microfusion.cloud
