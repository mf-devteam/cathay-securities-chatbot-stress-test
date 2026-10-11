#!/usr/bin/env bash
# 本機模擬「Cloud Run Jobs 多個 task 壓測 → 定時上傳 GCS → 手動產生合併報告」的完整流程。
# 不連任何雲端：GCS 以本機資料夾 .local-gcs/ 模擬，受測系統為 k6-mock/mock-server.js。
#
# 用法：scripts/local-sim.sh            （預設約 4～5 分鐘）
# 可調參數（環境變數）：
#   TASK_COUNT=3          模擬的 task 數
#   SIM_TIME_SCALE=60     k6 階段時間壓縮倍率（規劃 3.6 小時 ÷ 60 ≈ 3.6 分鐘）
#   UPLOAD_INTERVAL_S=20  各 task 上傳間隔
#   REPORT_AT_S=120       開始後幾秒產生一次「壓測中」的報告（模擬中途按下報表 Job）
#   MOCK_PORT=8092
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TASK_COUNT="${TASK_COUNT:-3}"
SIM_TIME_SCALE="${SIM_TIME_SCALE:-60}"
UPLOAD_INTERVAL_S="${UPLOAD_INTERVAL_S:-20}"
REPORT_AT_S="${REPORT_AT_S:-120}"
MOCK_PORT="${MOCK_PORT:-8092}"
RUN_ID="${RUN_ID:-local-$(date +%Y%m%d-%H%M%S)}"
GCS_ROOT="${ROOT}/.local-gcs/runs"
DEST="${GCS_ROOT}/${RUN_ID}"
WORK="${ROOT}/.local-gcs/work/${RUN_ID}"
mkdir -p "$DEST" "$WORK"

log() { echo "[sim] $(date +%H:%M:%S) $*"; }

# 1. mock server（回應時間縮短 0.3 倍、容量調低，讓 3 倍階段看得出變慢）
MOCK_PORT="$MOCK_PORT" MOCK_TIME_SCALE=0.3 MOCK_CAPACITY=60 node "${ROOT}/k6-mock/mock-server.js" > "${WORK}/mock-server.log" 2>&1 &
MOCK_PID=$!
cleanup() { kill "$MOCK_PID" 2>/dev/null || true; }
trap cleanup EXIT
sleep 1
curl -fsS "http://127.0.0.1:${MOCK_PORT}/healthz" >/dev/null || { log "mock server 沒有啟動"; exit 1; }
log "RUN_ID=${RUN_ID}，模擬 GCS：${DEST}"

# 2. 同時啟動 N 個 task（等同 Cloud Run Jobs 的 --tasks=N）
pids=()
for ((i = 0; i < TASK_COUNT; i++)); do
    RUN_ID="$RUN_ID" OUTPUT_URI="$GCS_ROOT" TASK_INDEX="$i" TASK_COUNT="$TASK_COUNT" \
    UPLOAD_INTERVAL_S="$UPLOAD_INTERVAL_S" WORK_DIR="$WORK" K6_SCRIPT="${ROOT}/k6-mock/script.js" \
    BASE_URL="http://127.0.0.1:${MOCK_PORT}" BASE_CONV_RATE=2 TIME_SCALE="$SIM_TIME_SCALE" THINK_TIME_S=3 \
    ABORT=false \
        bash "${ROOT}/runner/entrypoint.sh" > "${WORK}/runner-${i}.log" 2>&1 &
    pids+=($!)
done
log "已啟動 ${TASK_COUNT} 個 task"

# 3. 壓測進行中產生一次報告（模擬在 Console 按下報表 Job）
sleep "$REPORT_AT_S"
log "壓測進行中，產生「截至目前」的報告"
python3 "${ROOT}/report-job/main.py" --source "$DEST" --run-id "$RUN_ID" --expected-tasks "$TASK_COUNT" | sed 's/^/    /'
cp "${DEST}/reports/latest.html" "${DEST}/reports/mid-run.html"

# 4. 等全部 task 結束，再產生最終報告
for pid in "${pids[@]}"; do wait "$pid" || log "有 task 非 0 結束（見 ${WORK}/runner-*.log）"; done
log "全部 task 結束，產生最終報告"
python3 "${ROOT}/report-job/main.py" --source "$DEST" --run-id "$RUN_ID" --expected-tasks "$TASK_COUNT" | sed 's/^/    /'

log "完成。"
log "  壓測中報告：${DEST}/reports/mid-run.html"
log "  最終報告：  ${DEST}/reports/latest.html"
log "  各 task dashboard：${DEST}/tasks/task-*/dashboard.html"
