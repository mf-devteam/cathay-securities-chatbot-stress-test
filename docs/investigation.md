# 新阿發 k6 試跑：前置調查結果

> 用途：交給 Claude Desktop，作為產出「壓測規劃報告（給客戶）」的輸入資料。
> 調查日期：2026-10-07。調查方式：只讀程式碼（BFF／Middleware／Frontend 三個 repo）與 GCP 設定（`gcloud` 唯讀指令），**未修改任何程式或雲端設定，也未對 /ask 發送任何請求**。
> 標示「待確認」的項目代表無法從程式碼或設定確認，不要當成事實寫進報告。
> 「附錄：內部執行細節」只供我方執行參考，**不要放進客戶報告**。

---

## 1. 摘要（給報告撰寫者）

1. 聊天 API 是 `POST /api/chat/ask`，**一次回傳完整 JSON（非 streaming）**，k6 量到的回應時間就是使用者等待完整回答的時間。
2. FAQ 問答**不需登入**，以訪客身分即可測試。
3. 目前有三個前置條件必須先處理，否則測試無法進行：
   - **reCAPTCHA Enterprise**：每次 /ask 都要帶瀏覽器產生的一次性 token，k6 無法以正常方式取得 → **建議測試期間暫時關閉**（理由見第 4 節）。
   - **Cloud Armor 白名單**：測試環境只允許白名單 IP 連入，其他來源會被導向維護頁 → 需從白名單網路執行。
   - **每日聊天次數上限**：訪客每日僅 **4 次**（測試環境設定）→ 腳本需以多個測試身分模擬多位使用者，或測試期間暫時調高上限（待決定）。
4. 負載極輕：每 10 秒 1 則、總長 3 分鐘、約 18 次呼叫，目的是驗證流程與報表格式，**不是壓力測試**。

---

## 2. 測試標的與環境

| 項目 | 內容 |
|---|---|
| 系統 | 國泰證券「新阿發」AI 客服（Frontend → BFF → Middleware → AI 對話服務） |
| 測試環境 | 客戶 SIT 測試環境（客戶環境，我方無法操作；Cloud Run，asia-east1）。非客戶 UAT 專案 |
| 呼叫鏈 | 使用者 → Cloud Load Balancer（含 Cloud Armor）→ BFF（Node.js／Express）→ Middleware（Java／Spring Boot）→ AI 對話服務（Dialogflow／P3） |
| BFF 資源 | 1 vCPU／512 MiB，單一 instance 併發上限 80，request timeout 300 秒 |
| Middleware 資源 | 1 vCPU／512 MiB，併發上限 80，僅允許內部流量 |
| 負載平衡器 backend timeout | **30 秒**（AI 回應超過 30 秒會由負載平衡器直接回錯誤） |
| Middleware 呼叫 AI 的讀取逾時 | 30 秒（另有 45 秒保底） |
| k6 版本 | v2.3.0（已安裝，內建 Web Dashboard 與 HTML 報告匯出） |

**注意：** 客戶 UAT 與 SIT 的設定可能不同（例如次數上限、Cloud Armor 規則、reCAPTCHA 門檻），正式壓測前需在客戶 UAT 重新確認。

---

## 3. 聊天 API 規格（/ask）

### 3.1 端點

- `POST /api/chat/ask`
- Header：`Content-Type: application/json`（不需要授權 header）

### 3.2 Request body

| 欄位 | 型別 | 必填 | 說明 |
|---|---|---|---|
| sessionId | string | 是 | 訪客時與 newSessionId 相同；登入時為 App 的 UniqueSession |
| newSessionId | string | 是 | 一個對話視窗一組，前端產生（格式 `tpw` + 日時分秒 + 隨機字元） |
| msgId | string | 是 | 每則訊息一組，前端以 UUID（去掉 `-`）產生 |
| intent / secondBotIntent | string | 是 | 必須為空字串 `""` |
| role | `"user"` | 是 | 固定為 user |
| userInput | string | 是 | 使用者問句。含身分證、手機、Email 等個資格式會被 BFF 擋下（回 205） |
| device | `"Web"` 或 `"App"` | 否 | 影響訪客次數計算方式 |
| isInitialized | boolean | 是 | `true` = 開場問候、`false` = 一般提問 |
| channel | string | 是 | 來源通路 |
| ip | string | 實質必填 | BFF 選填，但 Middleware 規定不可空白 |
| udid | string | 是 | 裝置識別碼 |
| userData | object | 是 | 訪客帶 `{}`；有 `id` 即視為會員 |
| recaptchaToken | string | 實質必填 | 未帶即被擋（見第 4 節） |

範例（假資料）：

```json
{
  "sessionId": "tpw07143015ab3c9d1x", "newSessionId": "tpw07143015ab3c9d1x",
  "msgId": "3f2a9c...", "intent": "", "secondBotIntent": "",
  "role": "user", "userInput": "如何開戶？", "device": "Web",
  "isInitialized": false, "channel": "web", "ip": "<測試用值>", "udid": "<測試用值>",
  "userData": {}, "recaptchaToken": "<token>"
}
```

### 3.3 對話流程（前端實際行為）

1. **開啟對話**：沒有獨立的「建立 session」API。前端在本地產生 `newSessionId`，接著自動送出一次 `/ask`（`isInitialized: true`、`userInput: "你好"`）取得開場問候。Middleware 收到新的 `newSessionId` 時才建立對話紀錄。
   - 開場問候**也會呼叫 AI**，所以算在系統負載內，但**不計入每日聊天次數**。
2. **送出訊息**：同一個 `newSessionId`，每則訊息產生新的 `msgId`，`isInitialized: false`。
3. **收到回應**：一次回傳完整 JSON。

### 3.4 回應與成功判斷

成功回應（HTTP 200）：

```json
{ "status": 200, "data": [ { "mode": "FAQ", "isLogin": false, "content": "<回答文字>",
  "hasReferences": true, "references": [...], "botNewMsgId": "...", "link": [...] } ] }
```

**成功條件：** HTTP 200，且 body `status === 200`，且 `data` 是非空陣列。FAQ 回答在 `mode === "FAQ"` 那一筆的 `content`。

錯誤回應：HTTP status 與 body `status` 相同，並帶 `errorCode` 和 `message`。**有些錯誤用 2xx 回**，HTTP 層看不出失敗，必須檢查 body：

| HTTP | errorCode | 意義 |
|---|---|---|
| 205 | E1004 | 護欄擋下不當內容／含個資 |
| 206 | E1005 | AI 無法生成有效內容 |
| 403 | E1010 | reCAPTCHA 驗證失敗或未帶 token（異常流量） |
| 403 | E1011 | reCAPTCHA 瀏覽器端錯誤 |
| 408 | E1008 | 請求逾時 |
| 422 | E1002 | 欄位格式錯誤 |
| 429 | E1006 | 今日詢問次數已達上限 |
| 502 | E1009 | 服務異常 |
| 503 | CDPT0 | 資料庫交易異常 |

---

## 4. reCAPTCHA：建議測試期間暫時關閉

### 4.1 現況

- 每次 `/ask`，BFF 都會把 `recaptchaToken` 送到 Google reCAPTCHA Enterprise 評分，分數低於門檻、token 無效或沒帶 token，一律回 **403 E1010**。
- 程式裡**沒有測試模式或略過開關**。只有在 BFF 從 Secret Manager 讀不到 siteKey 或分數門檻值，或 Google API 本身出錯時，才會自動放行（設計上是避免 Google 服務異常時影響正常使用者）。

### 4.2 為什麼 k6 不能直接通過 reCAPTCHA

1. **token 只能由瀏覽器產生。** token 由網頁上的 Google JavaScript（`grecaptcha.enterprise.execute`）產生，綁定網站金鑰與網域。k6 是直接發 HTTP 請求的工具，不會執行網頁。
2. **token 有效期 2 分鐘，而且只能驗證一次。** 手動從瀏覽器複製也不能重複使用，沒辦法支撐連續 3 分鐘、約 18 次的自動請求。
3. **reCAPTCHA 的設計目的就是攔截自動化流量。** 就算用自動化瀏覽器硬產生 token，分數也會很低而被擋。這等於刻意繞過資安機制，我們不採用。lab 的紀錄顯示，連真人在瀏覽器操作都出現 0、0.3、0.9 等不同分數。
4. **測試結果會失真。** 不關閉的話，測到的會是「reCAPTCHA 擋了多少請求」，而不是「AI 客服回應多快、能承受多少流量」。

### 4.3 關閉的合理性

- 壓測要量的是 **BFF → Middleware → AI** 這條鏈的回應時間與承載能力。reCAPTCHA 是 Google 託管的外部服務，每次評分只增加少量延遲，不是系統瓶頸的評估重點。
- 業界常見的做法，是壓測時在受控的測試環境、受控的時段暫時停用人機驗證，結束後立即恢復。

### 4.4 風險控管（建議寫入報告）

| 控管項目 | 做法 |
|---|---|
| 範圍 | 只在測試環境停用，**正式環境不動** |
| 時段 | 只在約定的測試時段停用，記錄開始與恢復時間 |
| 其他防護仍有效 | Cloud Armor 白名單仍啟用，只有白名單 IP 能連入，停用期間不會對外開放 |
| 執行與恢復 | 由環境負責人執行設定變更並在結束後恢復；壓測執行方不自行修改 |
| 驗證恢復 | 恢復後以瀏覽器實際對話一次，並從 log 確認 reCAPTCHA 評分重新出現 |
| 報告註記 | 壓測報告需註明「測試期間 reCAPTCHA 停用，結果不含 reCAPTCHA 評分延遲」 |

### 4.5 目前決定（2026-10-07）

因為這是客戶的 SIT 測試環境，先**不關閉 reCAPTCHA** 試跑一次。可預期的結果：

- 所有請求都在 BFF 被擋下（403 E1010），不會到達 Middleware 與 AI，不消耗每日聊天次數，對環境幾乎沒有負載。
- 錯誤率 100% 超過 20% 的門檻，k6 會在幾秒內自動中止，只會送出 1–2 個請求。
- **能驗證的：** 執行機器的網路能否連到環境（Cloud Armor 白名單）、腳本能否正確執行、HTML 報告能否產出。
- **無法驗證的：** AI 回應時間與完整對話流程（試跑的主要目的）。

**實測結果（2026-10-08 11:42，公司白名單網路，k6 v2.3.0）：** 與預期相同。送出 1 個請求（開場問候）後收到 HTTP 403 E1010，耗時 0.23 秒；k6 在 3 秒內因錯誤率門檻自動中止。BFF log 確認請求在 reCAPTCHA 檢查就被擋下，Middleware 同時段沒有收到任何請求。網路連線（白名單）與腳本執行都正常。但**執行時間太短，k6 Web Dashboard 沒有產出 HTML 報告**（"not enough data"），所以報告格式也還無法評估。

若要取得回應時間數據，仍需依 4.3–4.4 節的做法，在約定時段暫時停用 reCAPTCHA。

**停用做法（待環境負責人確認）：** 程式已有的放行機制是在讀不到 Secret Manager 的 siteKey 時放行。把該 secret 暫時設為空值，即可在不改程式的前提下停用。前端拿到空的 siteKey 時也會略過 reCAPTCHA。BFF 會把讀到的 siteKey 快取在記憶體，所以停用時需要重新部署或重啟 BFF 讓設定生效。實際步驟由環境負責人決定。

---

## 5. 每日聊天次數上限

| 項目 | 內容 |
|---|---|
| 計算單位 | **每人每日**（沒有單一對話的上限），每天午夜（台北時間）重置 |
| 身分識別 | 會員：`userData.id`；訪客＋App：`udid`；訪客＋Web：`ip` 欄位 |
| 上限（lab 現值） | 訪客 **4 次**、會員 5 次（Firestore 設定；讀不到時預設 30） |
| 不計次的請求 | 開場問候（`isInitialized: true`）、ETF／業務類（TPI）、密碼相關（DELEGATE） |
| 超過時 | HTTP **429**、errorCode `E1006`、`errorType: ["limit_exceeded","guest"]` |

**對測試的影響：** 試跑約 18 則提問，單一訪客身分在第 5 則就會收到 429，觸發自動中止條件。兩種處理方式，**已決定採用 A**：

- **A. 多個測試身分（採用）：** 每個虛擬使用者用一組專屬的測試 `udid`／`ip`（例如 `k6-test-01`），每人開一個對話、問 3–4 則後換人。這也比較接近真實情況：多位使用者各自發問。不需要改任何設定，測試身分在 log 中也容易篩選。
- **B. 測試期間暫時調高 `maxCountGuest`：** 要改 Firestore 設定，處理方式比照 reCAPTCHA（由環境負責人執行、結束後恢復）。

---

## 6. 試跑計畫（k6）

| 項目 | 設定 |
|---|---|
| 目的 | 驗證 k6 能走完「開啟對話 → 送出訊息 → 收到完整回應」流程，評估 HTML 報告格式 |
| 情境 | 只有一個：FAQ 問答（訪客，不登入），tag `flow: faq` |
| 負載模型 | `constant-arrival-rate`：每 10 秒 1 次、持續 3 分鐘、預先配置 2 VU、最多 5 VU |
| 預計呼叫數 | 約 18 次（上限 30 次） |
| 單一請求 timeout | 60 秒（實際上負載平衡器 30 秒就會先逾時） |
| 自動中止 | 錯誤率 > 20%，或出現任何 HTTP 429 |
| 成功判斷 | HTTP 200、body `status === 200`、`data` 非空 |
| 量測指標 | 回應時間 avg／min／p50／p90／p95／p99／max、錯誤率、每秒處理量 |
| 產出 | k6 Web Dashboard HTML 報告、`summary.json`、原始資料 JSON |
| 比較基準 | 2026-10-07 前端實測：平均 9.57 秒、最長 20.25 秒、最短 2.85 秒 |

---

## 7. 待確認事項

| # | 項目 | 負責 |
|---|---|---|
| 1 | 先不停用 reCAPTCHA 試跑（見 4.5 節）；若要取得回應時間，需再約定停用時段、執行人與恢復方式 | 環境負責人 |
| 2 | ~~次數上限處理方式~~ 已決定採用方案 A（多個測試身分） | — |
| 3 | 從白名單網路執行 k6：預計 2026-10-08 在公司白名單網路，從本機執行 | 專案負責人 |
| 4 | 客戶 UAT 的次數上限、Cloud Armor 規則、reCAPTCHA 門檻是否與 lab 相同 | 客戶／環境負責人 |
| 5 | 負載平衡器 30 秒 timeout 是否符合預期（AI 回應偶爾接近 20 秒） | 環境負責人 |
| 6 | 正式測試問句（目前只有 3 筆 placeholder） | 專案負責人 |

---

## 附錄：內部執行細節（不要放進客戶報告）

- GCP 專案：`cfh-de-chatbot-lab`；Cloud Run 服務：`cathay-securities-chatbot-bff`、`cathay-securities-chatbot-middleware`、`cathay-securities-chatbot-frontend`。
- 對外入口：External HTTP(S) LB `afa-external-lb`，`/api/*` → backend `bff`，其他路徑 → `react`。HTTP 入口 `http://34.54.5.59`（reCAPTCHA log 中的 hostname 也是這個 IP）。
- Cloud Armor 政策 `maintenance-toggle-test-policy`：白名單 IP 放行，其他流量導向維護頁（302）。2026-10-07 調查時，本機出口 IP 不在白名單內，從本機打 `/healthz` 會被導到維護頁。
- reCAPTCHA 用的 secret（lab）：`mf-recaptcha-sitekey`、`mf-recaptcha-score`（只列名稱，未讀取值）。BFF 讀的是 `versions/latest`。
- 次數上限設定：Firestore 資料庫 `afa-sec-firestore`，路徑 `afa-global-config/afa-frontend-config`，欄位 `maxCountGuest`、`maxCountMember`。
- 程式依據：BFF `src/routers/chat.ts`、`src/services/recaptcha.service.ts`、`src/services/chat-control.service.ts`、`src/errors/error-mapping.ts`；Middleware `controller/ChatController.java`、`service/ChatService.java`（次數上限判斷）；Frontend `src/hooks/useChat.ts`、`src/utils/generateSessionId.ts`、`src/utils/recaptcha.ts`。
