# 本機 mock server（受測端替身）

[mock-server.js](mock-server.js) 模擬新阿發 BFF 的 `POST /api/chat/ask`，給正式腳本 [../script.js](../script.js) 在本機或 lab 打，用來驗證壓測機制、報表與通知。所有回應與數值都是模擬的，**不代表任何真實環境**。

## 行為

- **回應內容：** 依 k6 帶來的 `X-Load-Test-Case`／`X-Load-Test-Flow` header 決定，格式同 BFF：
  - FAQ 類（`flow=faq`）回 `mode: FAQ`；業務查詢類（`flow=business`，B2-2、C1）回 `mode: TPI`
  - B5（Model Armor 攔截）回 HTTP 205 E1004
  - 開場問候（`isInitialized=true`）回問候語
- **回應時間：** 依類別抽樣，對數常態分布，p95 約為平均的 2 倍。
  - FAQ、開場問候：平均 9.57 秒（10/7 UAT 實測）
  - 業務查詢：平均 3.5 秒（mock 假設值）
- **負載惡化：** 同時處理中的請求數超過 `MOCK_CAPACITY` 後，回應時間變慢（最多 4 倍），5xx 機率也會上升，用來模擬加壓到瓶頸。
- **逾時：** 超過 30 秒回 504，模擬負載平衡器 timeout。

## 執行

```bash
node runner/k6/mock/mock-server.js                     # http://127.0.0.1:8090
MOCK_TIME_SCALE=0.1 MOCK_CAPACITY=20 node runner/k6/mock/mock-server.js   # 快速模式
```

| 參數（環境變數） | 預設 | 說明 |
|---|---|---|
| MOCK_PORT | 8090（無則讀 PORT） | 監聽 port；部署成 Cloud Run Service 時用 Cloud Run 的 PORT |
| MOCK_HOST | 127.0.0.1 | 部署成 Cloud Run Service 時設 `0.0.0.0` |
| MOCK_FAQ_MEAN_MS／MOCK_BIZ_MEAN_MS／MOCK_INIT_MEAN_MS | 9570／3500／9570 | 各類平均回應時間 |
| MOCK_P95_RATIO | 2 | p95 ÷ 平均 |
| MOCK_CAPACITY | 150 | 同時處理中請求數的舒適上限 |
| MOCK_LB_TIMEOUT_MS | 30000 | 模擬 LB timeout |
| MOCK_BASE_ERROR | 0.001 | 基本 5xx 機率 |
| MOCK_TIME_SCALE | 1 | 回應時間縮放（0.1 = 快 10 倍） |

舊版 mock 腳本（`mock/script.js`）已由正式腳本取代，需要時見 git 歷史。
