# 壓測機（Cloud Run Jobs）

Cloud Run Jobs 的每個 task 執行一次 [entrypoint.sh](entrypoint.sh)，各跑 1/N 的負載，並把 k6 原生的 CSV 原始數據定時上傳到 GCS。整體機制見 [docs/architecture.md](../docs/architecture.md)。

| 檔案 | 內容 |
|---|---|
| [Dockerfile](Dockerfile) | 映像檔（build context 為本目錄） |
| [entrypoint.sh](entrypoint.sh) | 每個 task 的啟動腳本 |
| [k6/](k6/README.md) | k6 正式腳本與本機 mock server，整個目錄打包進映像檔 |

## 每個 task 做的事

1. 依 `CLOUD_RUN_TASK_INDEX`／`CLOUD_RUN_TASK_COUNT`，用 k6 的 execution segment 只跑自己那一份（第 i 個 task 跑 i/N～(i+1)/N）。
2. k6 輸出 `--out csv=…csv.gz` 到容器本機（Cloud Run 的本機檔案放在記憶體）。
3. 背景每 `UPLOAD_INTERVAL_S` 秒把目前的 CSV 上傳一次，覆蓋同名檔案，讓報表 Job 隨時讀得到最新資料。
4. k6 結束或收到 SIGTERM 時做最後一次上傳，並上傳該 task 的 dashboard HTML、`summary.json` 和 console log。
5. k6 因門檻未通過而回 exit 99 時，預設改回 exit 0，避免 Cloud Run 把 task 判定失敗而重跑壓測。
6. 寫出通知事件到 stdout（→ Cloud Logging → 告警寄信）：task-000 寫 `STRESS_TEST_EVENT=START`／`END`；k6 腳本的 `STRESS_TEST_EVENT=STAGE` 轉送到 stdout，其餘 k6 輸出只寫進 `k6-console.log`。

## GCS 上的檔案

```
gs://<bucket>/<prefix>/<RUN_ID>/
├── raw/task-000.csv.gz            k6 原始數據（持續覆蓋為最新）
├── status/task-000.json           task 狀態：running／finished、最後上傳時間、k6 exit code
├── tasks/task-000/dashboard.html  該 task 的 k6 dashboard（只代表 1/N 負載）
├── tasks/task-000/dashboard-annotated.html  教學版（由報表 Job 產生）
├── tasks/task-000/summary.json
├── tasks/task-000/k6-console.log
└── reports/                       報表 Job 的輸出（見 report-job/）
```

## 環境變數

| 名稱 | 必填 | 預設 | 說明 |
|---|---|---|---|
| RUN_ID | 否 | Cloud Run 的 execution 名稱（`CLOUD_RUN_EXECUTION`） | 本輪壓測代號，例如 `20261020-baseline`；建議每次執行時用 `--update-env-vars` 指定 |
| OUTPUT_URI | 是 | — | `gs://<bucket>/<prefix>`；本機模擬時為資料夾路徑 |
| UPLOAD_INTERVAL_S | 否 | 300 | 上傳間隔（秒） |
| K6_SCRIPT | 否 | `/app/k6/script.js` | k6 腳本（映像檔內含整個 `k6/` 目錄） |
| WORK_DIR | 否 | `/tmp/k6-out` | 本機暫存目錄 |
| THRESHOLD_FAIL_EXIT_ZERO | 否 | true | exit 99（門檻未通過）是否改回 0 |
| BASE_URL、STAGE_PLAN、SCENARIO_WEIGHTS… | 依腳本 | — | 直接傳給 k6 腳本（`__ENV`），一覽見 [k6/README.md](k6/README.md) |

## 上傳方式

- **`gs://`：** 用 Cloud Run 執行身分（service account）透過 metadata server 取得 token，以 curl 呼叫 GCS JSON API 上傳。映像檔不需要安裝 gcloud。
- **本機資料夾：** 先複製成 `.part`，再改名，避免報表 Job 讀到複製到一半的檔案。

## 建置與部署（草案，尚未在 GCP 驗證）

```bash
# 在 repo 根目錄
docker build -t <region>-docker.pkg.dev/<project>/<repo>/k6-runner:<tag> runner
docker push <region>-docker.pkg.dev/<project>/<repo>/k6-runner:<tag>

gcloud run jobs deploy k6-runner \
  --image <region>-docker.pkg.dev/<project>/<repo>/k6-runner:<tag> \
  --region <region> --tasks <N> --parallelism <N> \
  --max-retries 0 \                # 不重試：重試會再跑一次壓測
  --task-timeout 6h \
  --cpu 8 --memory 32Gi \
  --service-account <runner-sa> \  # 需要對 bucket 的 storage.objects.create／delete 權限
  --network <vpc> --subnet <subnet> --vpc-egress all-traffic \  # 經 Cloud NAT 固定 IP 出去
  --set-env-vars OUTPUT_URI=gs://<bucket>/runs,BASE_URL=<受測入口>,...

# 每一輪壓測：執行一次（不需要重新部署），RUN_ID 與 task 數可以每次覆寫
gcloud run jobs execute k6-runner --region <region> \
  --update-env-vars RUN_ID=20261020-baseline \
  --tasks <N>
```

之後改用 Terraform 管理 Job 定義（見 [infra/terraform/](../infra/terraform/README.md)）；執行仍用 `gcloud run jobs execute`。

## 待確認

- `grafana/k6:2.3.0` 映像檔 tag 是否存在（與本機開發版本一致）。
- 目前預設腳本是 mock 版（只允許打本機）；正式腳本完成後改 `K6_SCRIPT` 預設值。
- 每個 task 能承載的 VU 數與記憶體，要在 Smoke 時量測（見 [docs/measurements.md](../docs/measurements.md)）。
