# API 盤點（寫正式 k6 腳本的依據）

依「新阿發 壓力測試規劃 v1.4」的業務情境，逐一到各 repo 確認使用者操作時實際呼叫哪些 API、順序與前置條件。盤點完成的情境才寫進正式 k6 腳本（[k6/](../k6/README.md)）。

情境代號與比例與 mock 腳本（[k6/mock/script.js](../k6/mock/script.js) 的 `SCENARIOS`）一致。

## 狀態說明

- **待查：** 還沒到 repo 確認
- **已確認：** 已從程式碼確認 API 與順序
- **已驗證：** 已在 SIT 實際打過

## 總表

| 代號 | 情境 | 佔訊息 % | 範圍 | 呼叫的 API（依順序） | 前置條件（登入、測試帳號、資料） | 來源 repo／檔案 | 狀態 |
|---|---|---|---|---|---|---|---|
| INIT | 開場問候 | — | included | `POST /api/chat/ask`（`isInitialized=true`） | 無 | | 已驗證（2026-10-08，reCAPTCHA 403） |
| A1 | 登入 | — | included | | | | 待查 |
| A3 | OTP 重送 | — | included | | | | 待查 |
| B2 | FAQ 單輪 | 6.2 | included | `POST /api/chat/ask` | 無 | | 已確認（[investigation.md](investigation.md)） |
| B3 | FAQ 多輪 | 2.6 | included | `POST /api/chat/ask`（同一 sessionId 連續發問） | 無 | | 待查 |
| B4 | 知識庫外 | 0.9 | included | `POST /api/chat/ask` | 無 | | 待查 |
| B5 | Model Armor 攔截 | 0.3 | included | `POST /api/chat/ask`（預期 205） | 無 | | 待查 |
| C1 | 查 ETF 清單 | 27.0 | included | | | | 待查（是否另呼叫 `/api/chat/tpi`） |
| C6 | 查個人資料 | 18.0 | included | | 需登入 | | 待查 |
| C2 | ETF 申購 | 11.3 | conditional | | | | 待查 |
| C4 | KYC 過期 | 2.3 | conditional | | | | 待查 |
| C5 | KYC 評估 | 4.5 | conditional | | | | 待查 |
| A5 | 密碼補發 | 4.5 | conditional | | | | 待查 |
| A6 | 密碼變更 | 6.8 | conditional | | | | 待查 |
| C7 | 修改資料 | 6.8 | conditional | | | | 待查 |
| D1 | 未滿 18 歲（查詢） | 2.3 | conditional | | 特定測試帳號 | | 待查 |
| D2 | 未滿 18 歲（申購） | 2.3 | conditional | | 特定測試帳號 | | 待查 |
| D3 | 65 歲以上 | 4.5 | conditional | | 特定測試帳號 | | 待查 |

## 各情境明細

每個情境盤點時補一節，格式：

```
### C1 查 ETF 清單

- 使用者操作：……
- 呼叫順序：
  1. POST /api/chat/ask（body 重點欄位：……）
  2. ……
- 成功判定：HTTP 狀態碼、body 欄位
- 會不會打到下游（Middleware／AI／券商系統）：……
- 副作用（會不會寫資料、扣次數、發簡訊）：……
- 來源：<repo>/<檔案>:<行號>
```
