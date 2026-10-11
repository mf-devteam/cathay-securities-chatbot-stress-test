# 文件索引

| 文件 | 內容 | 讀者 |
|---|---|---|
| [handover-claude-desktop.md](handover-claude-desktop.md) | 交接：做法、量測結果、待確認事項，用來製作客戶簡報 | Claude Desktop／PM |
| [investigation.md](investigation.md) | 環境調查：/ask API 規格、reCAPTCHA、每日次數上限、SIT 試跑結果 | 開發、簡報撰寫 |
| [architecture.md](architecture.md) | 分散式壓測執行與報表機制（Cloud Run Jobs + GCS + 報表 Job）與設計決策 | 開發、Infra |
| [measurements.md](measurements.md) | 量測紀錄：CSV 大小、dashboard 能看到什麼、mock 執行、本機模擬 | 開發、簡報撰寫 |
| [dashboard-guide.md](dashboard-guide.md) | k6 Web Dashboard 怎麼看（每張圖、每張表的說明） | 所有看報告的人 |
| [tasks/2026-10-07-k6-poc-handover.md](tasks/2026-10-07-k6-poc-handover.md) | 原始任務指示：k6 輕量試跑的限制與步驟 | 開發 |
| [samples/](samples/) | 量測的原始輸出與範例報告（k6 summary、合併報告 HTML、教學版 dashboard） | 所有人 |
| [images/](images/) | 報告與 dashboard 截圖（簡報用） | Claude Desktop／PM |

各工具的使用方式見各目錄的 README：[k6-poc/](../k6-poc/README.md)、[k6-mock/](../k6-mock/README.md)、[runner/](../runner/README.md)、[report-job/](../report-job/README.md)。

**外部文件（不在 repo 內）：**
- 「新阿發 壓力測試規劃 v1.4」（簡報）
- 「新阿發 壓測負載試算表 v1.4」
