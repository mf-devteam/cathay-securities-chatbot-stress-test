# API 盤點（寫正式 k6 腳本的依據）

依「業務情境組合」最終版，逐一到各 repo 確認使用者操作時實際呼叫哪些 API、順序與前置條件。**本次範圍只有表上標黃色的列**（2026-10-16 確認）：A4、B1、B2、B2-2、B3、B4、B5、C1。

情境代號與權重與正式腳本（[runner/k6/lib/scenarios.js](../runner/k6/lib/scenarios.js)、`SCENARIO_WEIGHTS` 預設值）一致；目前全部情境都先走 `POST /api/chat/ask`。

## 狀態說明

- **待查：** 還沒到 repo 確認
- **已確認：** 已從程式碼確認 API 與順序
- **已驗證：** 已在 SIT 實際打過

## 總表

| 代號 | 情境 | 預設權重 | 判定門檻 | 呼叫的 API（依順序） | 前置條件（登入、測試帳號、資料） | 來源 repo／檔案 | 狀態 |
|---|---|---|---|---|---|---|---|
| B1 | 開啟聊天（開場問候） | 每段對話 1 次 | 只觀察 | `POST /api/chat/ask`（`isInitialized=true`） | 無 | | 已驗證（2026-10-08，reCAPTCHA 403）；**待查：**進聊天頁時是否另外讀 Firestore 參數、CloudSQL 預設問題的 API |
| B2 | FAQ 問答 | 28 | FAQ | `POST /api/chat/ask` | 無 | | 已確認（[investigation.md](investigation.md)） |
| B2-2 | FAQ intent | 36 | 業務查詢 | `POST /api/chat/ask`（暫定） | 無 | | 待查（經 TPI／證券地端；是否另呼叫 `/api/chat/tpi`） |
| B3 | 多輪對話 | 15 | FAQ | `POST /api/chat/ask`（同一 sessionId 連續發問） | 無 | | 待查 |
| B4 | 知識庫以外問題 | 5 | FAQ | `POST /api/chat/ask` | 無 | | 待查 |
| B5 | Model Armor 攔截 | 2 | FAQ | `POST /api/chat/ask`（預期 205） | 無 | | 待查 |
| C1 | 查看 ETF 清單 | 12 | 業務查詢 | `POST /api/chat/ask`（暫定） | 無 | | 待查（經 TPI；是否另呼叫 `/api/chat/tpi`） |
| A4 | Token 續期／過期 | — | — | — | 需登入 | | 不在腳本流程內（訪客不會拿到 JWT）；Soak 時觀察受測端 Redis 與 Token 刷新 |

不在本次範圍（業務情境組合未標黃色）：A1、A2、A3 登入與 OTP，A5、A6 密碼，C2、C5、C6、C7。

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
