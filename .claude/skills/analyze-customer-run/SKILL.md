---
name: analyze-customer-run
description: 分析證券方地端壓測的結果或失敗原因（k6 打包檔、報表、GCP LB／Cloud Run log、防火牆 log）。用於「客戶跑完了幫我看」「為什麼中止」「為什麼 429／錯誤率高」。
---

# 分析客戶的壓測結果

客戶的資料放在 `logs/<MMDD>/`（不進 git）。結論寫成 `logs/<MMDD>/<RUN_ID>_問題分析.md`，對客戶說明用白話。

## 1. 先確認拿到的是對的那一輪

- 打包檔裡的 `runs/<RUN_ID>/run.env`、`runners.txt` 的時間要跟客戶說的那一輪對得上（客戶曾經打包到舊的 runs）。
- 各 task 的 exit code：`tasks/task-NNN/` 底下的 log，或 `k6ctl status` 的輸出。
  - `0` 通過；`99` 門檻沒過或自動中止；`105` 手動停止。
  - 看到 99 先找出是哪個門檻（`k6-console.log`）：錯誤率 >5%、p99 超過門檻 ×2、429 立即停，或只是最後判定沒過。

## 2. 分層定位：問題在哪一段？

依序比對，找出請求在哪一層消失或變慢：

| 層 | 資料 | 看什麼 |
|---|---|---|
| k6 | `raw/task-*.csv.gz`、`k6-console.log` | 錯誤類型：status 0／`connection refused` 代表沒連上；429／5xx 是後端回的 |
| runner 主機 | `sar-summary.txt` | CPU、記憶體、TCP 重傳率、新連線失敗 |
| 客戶防火牆 | PA log（客戶提供） | `tcp-rst-from-server`、`aged-out` |
| GCP LB | LB log JSON | `response_sent` 數量是否等於 k6 成功數；`client_disconnected` 通常是中止時還在途中的請求 |
| Cloud Run | revision、instance 數、log | 429 的訊息是「waiting too long for an available instance」而且沒有 instanceId → Cloud Run 排隊，不是 app 限流 |

**判斷原則：**
- k6 送出的數量 ≫ LB 收到的 → 網路或防火牆問題。
- LB 收到但回應慢，而且連非 AI 的 API 也一起變慢 → BFF 排隊（concurrency 或 instance 不足）。
- 只有聊天慢 → LLM。

## 3. 容量估算（Little's law）

同時請求數 ≈ TPS × 平均回應秒數。1 倍約 900、3 倍約 2,700。把結果跟 Cloud Run 的 `concurrency × max instances` 比較，並且要確認**測試當下的 revision** 設定真的有生效。

## 4. 產出

- 分析檔：時間軸、各層數字、結論、要請客戶提供或調整的項目。
- 給客戶的訊息：白話、短，要客戶執行的指令要能直接複製貼上。
- 已知的歷史問題見 CLAUDE.md 的「客戶 UAT 的已知狀況」，先比對是不是同一個問題。
