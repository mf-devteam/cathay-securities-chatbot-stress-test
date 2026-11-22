# 文件索引

> 這裡大多是 **2026-10 規劃期**的調查、量測與簡報素材（當時以雲端版為主），接手時不用先看。
> 現況與決策請看根目錄的 [CLAUDE.md](../CLAUDE.md)；地端版看 [onprem/README.md](../onprem/README.md)，雲端版看 [cloud-run.md](cloud-run.md)。

| 文件 | 內容 | 讀者 |
|---|---|---|
| [cloud-run.md](cloud-run.md) | 雲端版（Cloud Run Jobs）的完整說明：操作、壓測機、報表 Job、Terraform、監控告警 | 開發、Infra |
| [handover-claude-desktop.md](handover-claude-desktop.md) | 交接：做法、量測結果、待確認事項，用來製作客戶簡報 | Claude Desktop／PM |
| [investigation.md](investigation.md) | 環境調查：/ask API 規格、reCAPTCHA、每日次數上限、SIT 試跑結果 | 開發、簡報撰寫 |
| [architecture.md](architecture.md) | 分散式壓測執行與報表機制（Cloud Run Jobs + GCS + 報表 Job）與設計決策 | 開發、Infra |
| [measurements.md](measurements.md) | 量測紀錄：CSV 大小、dashboard 能看到什麼、mock 執行、本機模擬 | 開發、簡報撰寫 |
| [dashboard-guide.md](dashboard-guide.md) | k6 Web Dashboard 怎麼看（每張圖、每張表的說明） | 所有看報告的人 |
| [tasks/2026-10-07-k6-poc-handover.md](tasks/2026-10-07-k6-poc-handover.md) | 原始任務指示：k6 輕量試跑的限制與步驟（試跑腳本已移除，見 git 歷史） | 開發 |
| [api-inventory.md](api-inventory.md) | API 盤點：各業務情境實際呼叫哪些 API（寫正式 k6 腳本的依據） | 開發 |
| [samples/](samples/) | 量測的原始輸出與範例報告（k6 summary、合併報告 HTML、教學版 dashboard） | 所有人 |
| [images/](images/) | 報告與 dashboard 截圖（簡報用） | Claude Desktop／PM |

k6 腳本與 mock server 的說明：[runner/k6/](../runner/k6/README.md)、[runner/k6/mock/](../runner/k6/mock/README.md)。

**外部文件（不在 repo 內）：**
- 「新阿發 壓力測試規劃 v1.4」（簡報）
- 「新阿發 壓測負載試算表 v1.4」
