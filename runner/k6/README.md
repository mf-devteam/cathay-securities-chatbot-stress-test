# k6 腳本

整個 `k6/` 目錄會打包進壓測機映像檔（[Dockerfile](../Dockerfile)），用環境變數 `K6_SCRIPT` 選擇要跑的腳本。

| 路徑 | 內容 |
|---|---|
| [mock/](mock/) | 依規劃 v1.4 的 mock 版腳本與本機 mock server。`BASE_URL` 只接受本機 |
| （待建立）正式腳本 | 依 [docs/api-inventory.md](../../docs/api-inventory.md) 的盤點結果撰寫 |

## 正式腳本的預定結構

以 mock 腳本為基礎（逐階加壓、情境加權、`stage`／`phase`／`flow`／`case` tag 都沿用，報表 Job 依這些 tag 分組），把 mock API 換成盤點出來的真實 API：

```
k6/
├── script.js          入口：options（逐階加壓、門檻）、default function（一位使用者的多輪對話）
├── lib/               共用：sessionId／msgId、測試身分、record()、stage tag
├── flows/             各業務情境的 API 呼叫順序（B2.js、C1.js…，對應 api-inventory 的情境代號）
└── data/              問句、測試帳號對照（帳號密碼不放 repo，用環境變數或 Secret Manager）
```

## 撰寫時要沿用的事項

從 SIT 試跑與環境調查（[docs/investigation.md](../../docs/investigation.md)）得到的：

- **`maxRedirects: 0`：** Cloud Armor 對非白名單 IP 回 302 導向維護頁，跟隨轉址會變成 HTTP 200 的 HTML，被誤判成功。
- **只有預期的狀態碼算成功：** BFF 用 205（護欄擋下）、206（AI 無法生成）回錯誤，不能當成功。
- **reCAPTCHA：** 壓測期間由環境負責人暫時關閉，腳本不做任何繞過。
- **訪客每日次數（`maxCountGuest`）以 ip 計算：** 測試身分的 ip 用 RFC 2544 保留網段 `198.18.0.0/15`，不與真實使用者重疊。
