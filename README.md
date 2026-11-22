# 新阿發 壓力測試（k6）

國泰證券「新阿發」AI 客服的壓力測試工具。客戶＝證券方；我方＝MF（Microfusion）。

有兩個版本，**目前主力是地端版**：

| | 地端版（目前主力） | 雲端版（早期版本，目前沒在用） |
|---|---|---|
| 說明文件 | **[onprem/README.md](onprem/README.md)** | [docs/cloud-run.md](docs/cloud-run.md) |
| 在哪裡跑 | 證券方 DMZ：3 台 runner＋1 台 control，RHEL 9.6、離線安裝 | GCP Cloud Run Jobs，多個 task 平行，數據放 GCS |
| 怎麼操作 | 客戶在 control 上用 `k6menu` 選單操作 | `gcloud run jobs execute` |
| 範圍 | 只測未登入訪客，打到 3 倍（354 TPS） | 1 倍上限、「業務情境組合」黃色列 8 項 |
| 專屬程式 | `onprem/`、`infra/terraform/envs/onprem-lab/`（我們的 lab） | `runner/Dockerfile`、`runner/entrypoint.sh`、`infra/terraform/envs/lab/`、`scripts/` |

兩版共用 k6 腳本 [runner/k6/](runner/k6/README.md) 和報表程式 `report-job/`，改這兩個目錄時兩邊都會受影響。

## 剛接手：照這個順序看

| 順序 | 文件 | 看什麼 |
|---|---|---|
| 1 | [CLAUDE.md](CLAUDE.md) | **最重要。**背景、決策、踩過的地雷、客戶環境現況、目前進度與待辦 |
| 2 | [onprem/README.md](onprem/README.md) | 地端版：三種測試、打包、安裝、k6menu 操作、我們的 lab |
| 3 | [runner/k6/README.md](runner/k6/README.md) | 要改 k6 腳本時才看：腳本流程、參數一覽 |

常做的事已經寫成 Claude Code 的 skill，直接說「打包」「在 lab 驗證」「客戶跑完幫我看」就會照步驟做：
[package-delivery](.claude/skills/package-delivery/SKILL.md)、[lab-verify](.claude/skills/lab-verify/SKILL.md)、[analyze-customer-run](.claude/skills/analyze-customer-run/SKILL.md)。

其他都可以先跳過：[docs/cloud-run.md](docs/cloud-run.md) 是雲端版；[docs/](docs/README.md) 其餘是 10 月規劃期的調查與簡報素材，只在需要查來龍去脈時看。

## 目錄

```
.
├── CLAUDE.md           背景、決策、地雷、現況（接手先看）
├── onprem/             地端版：安裝、k6menu／k6suite／k6ctl、打包（說明在 onprem/README.md）
├── runner/k6/          k6 腳本（兩版共用）；mock/ 是本機 mock server
├── report-job/         報表程式（兩版共用）；tests/ 是單元測試
├── runner/             雲端版的壓測機映像檔（Dockerfile、entrypoint.sh）
├── infra/terraform/    envs/onprem-lab＝地端模擬 lab；envs/lab＝雲端版的通知與告警
├── scripts/            雲端版的本機模擬、通知 dry run
└── docs/               cloud-run.md（雲端版說明）、規劃期的調查與量測
```

單元測試：`cd report-job && python3 -m unittest discover -s tests`

## 安全注意事項

- **k6 腳本預設只能打本機：** `BASE_URL` 不是 `127.0.0.1`／`localhost` 時，必須另外設定 `ALLOW_REMOTE_TARGET=true`，避免本機誤打真實環境。
- **對任何真實環境執行前，都要先取得負責人同意。**SIT／UAT 是客戶環境，我們不能操作，也不要從 lab 打 SIT／UAT。
- **不要把帳號、token、金鑰寫進檔案或 commit；**一律用環境變數。根目錄的客戶 xlsx／pdf 和 `logs/`（客戶 log）不能進 git。
