#!/usr/bin/env bash
# 壓測機啟動腳本：Cloud Run Jobs 的每個 task 執行一次；本機模擬時直接執行。
#
# 做的事：
#   1. 依 task 編號用 k6 execution segment 只跑 1/N 的負載
#   2. k6 原生輸出 CSV（gzip）到容器本機
#   3. 背景每 UPLOAD_INTERVAL_S 秒把「目前的」CSV 上傳一次（覆蓋同名檔），讓報表 Job 隨時能讀到最新資料
#   4. k6 結束（或收到 SIGTERM）後做最後一次上傳，並上傳該 task 的 dashboard HTML、summary.json、console log
#   5. 通知事件（stdout → Cloud Logging → log-based alert 寄信，見 infra/terraform/README.md「監控與告警」）：
#      task-000 寫出 STRESS_TEST_EVENT=START／END；k6 腳本寫出的 STRESS_TEST_EVENT=STAGE 轉送到 stdout
#
# 輸出位置（OUTPUT_URI/RUN_ID 底下）：
#   raw/task-000.csv.gz            k6 原始數據（持續覆蓋為最新）
#   status/task-000.json           task 狀態（running／finished、最後上傳時間、k6 exit code）
#   tasks/task-000/dashboard.html  該 task 的 k6 dashboard（只代表 1/N 負載）
#   tasks/task-000/summary.json    該 task 的 k6 摘要
#   tasks/task-000/k6-console.log  該 task 的 k6 console 輸出
#
# 環境變數：
#   RUN_ID              本輪壓測代號，例如 20261020-baseline；未設定時用 Cloud Run 的 execution 名稱
#   OUTPUT_URI          必填，gs://<bucket>/<prefix> 或本機資料夾（模擬 GCS）
#   K6_SCRIPT           k6 腳本路徑，預設 /app/k6/script.js（正式腳本）
#   UPLOAD_INTERVAL_S   上傳間隔秒數，預設 300
#   WORK_DIR            本機暫存目錄，預設 /tmp/k6-out（Cloud Run 為記憶體）
#   TASK_INDEX／TASK_COUNT  本機模擬用；Cloud Run 會自動帶 CLOUD_RUN_TASK_INDEX／CLOUD_RUN_TASK_COUNT
#   THRESHOLD_FAIL_EXIT_ZERO  預設 true：k6 因門檻未過回 exit 99 時改回 0，避免 Cloud Run 視為失敗而重跑壓測
#   其他 k6 腳本參數（BASE_URL、STAGE_PLAN、SCENARIO_WEIGHTS…，見 k6/README.md）直接以環境變數傳入，k6 會讀到 __ENV
set -euo pipefail

RUN_ID="${RUN_ID:-${CLOUD_RUN_EXECUTION:-}}"
: "${RUN_ID:?RUN_ID 必填（Cloud Run 上未設定時自動用 execution 名稱）}"
export RUN_ID # k6 腳本也用 RUN_ID（事件與 udid）
: "${OUTPUT_URI:?OUTPUT_URI 必填（gs://bucket/prefix 或本機資料夾）}"
K6_SCRIPT="${K6_SCRIPT:-/app/k6/script.js}"
UPLOAD_INTERVAL_S="${UPLOAD_INTERVAL_S:-300}"
TASK_INDEX="${CLOUD_RUN_TASK_INDEX:-${TASK_INDEX:-0}}"
TASK_COUNT="${CLOUD_RUN_TASK_COUNT:-${TASK_COUNT:-1}}"
THRESHOLD_FAIL_EXIT_ZERO="${THRESHOLD_FAIL_EXIT_ZERO:-true}"

TASK_NAME="$(printf 'task-%03d' "$TASK_INDEX")"
WORK_DIR="${WORK_DIR:-/tmp/k6-out}/${TASK_NAME}"
DEST="${OUTPUT_URI%/}/${RUN_ID}"
mkdir -p "$WORK_DIR"

log() { echo "[runner ${TASK_NAME}] $(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }

# 通知事件：只由 task-000 寫出，避免 N 個 task 各寄一封（格式固定，告警政策以此擷取欄位）
event() { # event <START|END> <key=value…>
    if [ "$TASK_INDEX" -eq 0 ]; then
        echo "STRESS_TEST_EVENT=$1 run_id=${RUN_ID} $2"
    fi
}

# ---------- 上傳（gs:// 走 GCS JSON API；其他視為本機資料夾） ----------
gcs_token() {
    curl -fsS -H 'Metadata-Flavor: Google' \
        'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token' |
        sed -E 's/.*"access_token":"([^"]+)".*/\1/'
}

upload() { # upload <本機檔案> <相對路徑>
    local src="$1" rel="$2"
    [ -f "$src" ] || return 0
    if [[ "$DEST" == gs://* ]]; then
        local bucket="${DEST#gs://}"; bucket="${bucket%%/*}"
        local prefix="${DEST#gs://${bucket}}"; prefix="${prefix#/}"
        local object="${prefix:+${prefix}/}${rel}"
        local encoded; encoded="$(printf '%s' "$object" | sed -e 's/\//%2F/g')"
        curl -fsS -X POST --data-binary "@${src}" \
            -H "Authorization: Bearer $(gcs_token)" \
            -H 'Content-Type: application/octet-stream' \
            "https://storage.googleapis.com/upload/storage/v1/b/${bucket}/o?uploadType=media&name=${encoded}" >/dev/null
    else
        mkdir -p "$(dirname "${DEST}/${rel}")"
        # 先複製到暫存名再改名，避免報表 Job 讀到複製到一半的檔案
        cp "$src" "${DEST}/${rel}.part" && mv "${DEST}/${rel}.part" "${DEST}/${rel}"
    fi
}

write_status() { # write_status <state> [k6_exit]
    printf '{"task":"%s","task_index":%s,"task_count":%s,"state":"%s","updated_at":"%s","k6_exit":%s}\n' \
        "$TASK_NAME" "$TASK_INDEX" "$TASK_COUNT" "$1" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${2:-null}" > "${WORK_DIR}/status.json"
    upload "${WORK_DIR}/status.json" "status/${TASK_NAME}.json"
}

upload_raw() {
    upload "${WORK_DIR}/raw.csv.gz" "raw/${TASK_NAME}.csv.gz" || log "上傳 raw 失敗（下次重試）"
}

# ---------- execution segment：第 i 個 task 跑 i/N ~ (i+1)/N ----------
seq_parts="0"
for ((i = 1; i <= TASK_COUNT; i++)); do seq_parts+=",${i}/${TASK_COUNT}"; done
[ "$TASK_COUNT" -eq 1 ] && seq_parts="0,1"
segment="${TASK_INDEX}/${TASK_COUNT}:$((TASK_INDEX + 1))/${TASK_COUNT}"
[ "$TASK_INDEX" -eq 0 ] && segment="0:1/${TASK_COUNT}"
[ "$TASK_COUNT" -eq 1 ] && segment="0:1"

log "RUN_ID=${RUN_ID} segment=${segment} dest=${DEST} upload_interval=${UPLOAD_INTERVAL_S}s"
write_status running
event START "tasks=${TASK_COUNT} stage_scope=${STAGE_SCOPE:-full} time_scale=${TIME_SCALE:-1} dest=${DEST}"

# ---------- 執行 k6 ----------
export K6_CSV_TIME_FORMAT=unix_milli
export K6_WEB_DASHBOARD=true
# 預設不開 HTTP server，只在結束時匯出 HTML；地端版設 5665＋K6_WEB_DASHBOARD_HOST=127.0.0.1，經 SSH tunnel 看即時畫面
export K6_WEB_DASHBOARD_PORT="${K6_WEB_DASHBOARD_PORT:--1}"
export K6_WEB_DASHBOARD_EXPORT="${WORK_DIR}/dashboard.html"
export RESULTS_DIR="$WORK_DIR"

# k6 的完整輸出寫到 console log；其中的通知事件（STRESS_TEST_EVENT=STAGE）另外轉送到 stdout，讓 Cloud Logging 收到
k6 run --execution-segment "$segment" --execution-segment-sequence "$seq_parts" \
    --out "csv=${WORK_DIR}/raw.csv.gz" "$K6_SCRIPT" \
    > >(tee -a "${WORK_DIR}/k6-console.log" | awk '/STRESS_TEST_EVENT=/ { print; fflush() }') 2>&1 &
K6_PID=$!

# Cloud Run 逾時或手動取消時會送 SIGTERM：轉給 k6，讓它正常收尾與匯出
trap 'log "收到 SIGTERM，通知 k6 結束"; kill -TERM "$K6_PID" 2>/dev/null || true' TERM INT

# ---------- 背景定時上傳 ----------
(
    while kill -0 "$K6_PID" 2>/dev/null; do
        sleep "$UPLOAD_INTERVAL_S" &
        wait $! || true
        kill -0 "$K6_PID" 2>/dev/null || break
        upload_raw && write_status running && log "已上傳目前的 raw"
    done
) &
UPLOADER_PID=$!

set +e
wait "$K6_PID"
K6_EXIT=$?
# trap 打斷 wait 時 k6 可能還在收尾，再等一次
while kill -0 "$K6_PID" 2>/dev/null; do wait "$K6_PID"; K6_EXIT=$?; done
set -e
kill "$UPLOADER_PID" 2>/dev/null || true
wait "$UPLOADER_PID" 2>/dev/null || true

# ---------- 最後上傳 ----------
log "k6 結束，exit=${K6_EXIT}，最後上傳"
upload_raw
upload "${WORK_DIR}/dashboard.html" "tasks/${TASK_NAME}/dashboard.html" || true
upload "${WORK_DIR}/summary.json" "tasks/${TASK_NAME}/summary.json" || true
upload "${WORK_DIR}/k6-console.log" "tasks/${TASK_NAME}/k6-console.log" || true
write_status finished "$K6_EXIT"
event END "k6_exit=${K6_EXIT} dest=${DEST}"

# exit 99 = 門檻未通過（測試本身有跑完）。不回非 0，避免 Cloud Run 把 task 判定失敗而重跑壓測
if [ "$K6_EXIT" -eq 99 ] && [ "$THRESHOLD_FAIL_EXIT_ZERO" = "true" ]; then
    log "門檻未通過（exit 99），以 exit 0 結束"
    exit 0
fi
exit "$K6_EXIT"
