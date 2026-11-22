---
name: lab-verify
description: 在本機或 lab（GCE 模擬地端）驗證 k6 腳本、報表、k6ctl／k6menu 的修改。用於「改完幫我測」「在 lab 跑一下」「驗證新版」。
---

# 驗證修改

由輕到重，夠用就停。每一層的結果都要告訴使用者（通過、失敗、沒跑）。

## 1. 本機（不連任何環境）

```bash
cd report-job && python3 -m unittest discover -s tests && cd ..
node runner/k6/mock/mock-server.js &          # mock 受測端 127.0.0.1:8090
```
- k6 腳本：用 mock 跑短版，例如 `k6 run -e BASE_URL=http://127.0.0.1:8090 -e TIME_SCALE=15 ...`，或把 `STAGE_PLAN` 設成很小的值。
- 雲端版完整流程：`scripts/local-sim.sh`（3 個 task → 合併報告，約 4 分鐘）。
- 腳本預設只能打本機；`BASE_URL` 是遠端時必須另外設 `ALLOW_REMOTE_TARGET=true`。**不要用這個設定去打客戶環境。**

## 2. lab（改到 onprem/、k6ctl、k6menu、k6suite、安裝流程時）

說明見 [onprem/README.md「lab」](../../../onprem/README.md#9-labgce-vm-模擬證券方-dmz)。

- 先問使用者 lab VM 是否已經開著。`terraform apply` 請使用者自己執行。
- 登入：`gcloud compute ssh k6-control-report-01 --zone asia-east1-b --project cfh-de-chatbot-lab --tunnel-through-iap`
- 更新程式：照手冊的「更新步驟」裝新的安裝包，或用 `k6ctl start` 同步。
- 跑：`k6ctl check` → `k6menu` 2（v3 試跑）或 `k6ctl start lab-$(date +%Y%m%d-%H%M) STAGE_SCOPE=smoke` → `k6ctl wait`。
- lab control 的 `stress.env` 用的是 lab 專用的小負載 `V3_PLAN_*`，打的是 mock VM。mock 慢，所以 exit 99 不一定代表有問題，要看是哪個門檻觸發。
- 報表帶回本機：`gcloud compute scp --tunnel-through-iap --zone asia-east1-b k6-control-report-01:/var/lib/k6-stress/runs/<RUN_ID>/reports/latest.html .`

## 3. 回歸

改到共用的程式（`lib/`、`questions.json`、`k6ctl`、報表）時，除了新功能，也要確認**舊的測試**（選單 9 裡的逐階加壓、對話回應時間測試）還能跑、報表還能產生。

## 不要做

- 不要從 lab 打 SIT／UAT（客戶環境）。
- 不要自己停掉或刪除 lab VM，先問使用者。
