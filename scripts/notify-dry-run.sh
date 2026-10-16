#!/usr/bin/env bash
# 壓測通知的 dry run：確認 Terraform 建立的通知管道（Email）真的收得到信。
# 不啟動任何壓測，也不會對受測系統送出流量。
#
# 做的事：
#   1. 檢查「[k6] 通知測試」告警政策已啟用，且連到 Email 通知管道（印出收件信箱）
#   2. 寫入一筆通知測試事件（Cloud Logging log：k6-notify-test）
#   3. 確認事件已寫入 Cloud Logging；之後約 1～5 分鐘內，收件信箱會收到主旨「[k6 壓測] 通知測試 …」的信
#
# 用法：
#   scripts/notify-dry-run.sh                 檢查 + 寄出測試信
#   scripts/notify-dry-run.sh --check-only    只檢查設定，不寄信
# 參數（環境變數）：
#   PROJECT_ID   預設 cfh-de-chatbot-lab
#
# 需要：已執行 terraform apply（infra/terraform/envs/lab）；gcloud 已登入，且有該專案
#       Monitoring 讀取與 Logging 寫入權限（例如 roles/monitoring.viewer、roles/logging.logWriter）。
# 注意：告警政策 5 分鐘內只會寄一封，連續執行時第二次可能收不到信。
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-cfh-de-chatbot-lab}"
POLICY_NAME="[k6] 通知測試"
LOG_ID="k6-notify-test"
CHECK_ONLY=false
[ "${1:-}" = "--check-only" ] && CHECK_ONLY=true

log() { echo "[notify] $(date +%H:%M:%S) $*"; }
fail() { log "失敗：$*"; exit 1; }

command -v gcloud >/dev/null || fail "找不到 gcloud"
command -v python3 >/dev/null || fail "找不到 python3"
ACCOUNT="$(gcloud config get-value account 2>/dev/null)"
[ -n "$ACCOUNT" ] || fail "gcloud 尚未登入（gcloud auth login）"
TOKEN="$(gcloud auth print-access-token)"
log "專案：${PROJECT_ID}，執行者：${ACCOUNT}"

api() { # api <path 或完整 URL> [curl 參數…]
    local url="$1"; shift
    [[ "$url" == https://* ]] || url="https://monitoring.googleapis.com/v3/${url}"
    curl -fsS -H "Authorization: Bearer ${TOKEN}" "$@" "$url"
}

# ---------- 1. 檢查告警政策與通知管道 ----------
policies="$(api "projects/${PROJECT_ID}/alertPolicies" -G --data-urlencode "filter=display_name=\"${POLICY_NAME}\"")" ||
    fail "讀不到告警政策（權限不足？）"
# 輸出：<是否啟用>\t<通知管道 1>\t<通知管道 2>…
policy_info="$(printf '%s' "$policies" | python3 -c '
import json, sys
ps = json.load(sys.stdin).get("alertPolicies", [])
if len(ps) != 1:
    sys.exit(f"找到 {len(ps)} 個告警政策")
p = ps[0]
print("\t".join([str(p.get("enabled", False)).lower()] + p.get("notificationChannels", [])))
')" || fail "告警政策「${POLICY_NAME}」不存在或重複，請先 terraform apply"

IFS=$'\t' read -r -a fields <<< "$policy_info"
[ "${fields[0]}" = "true" ] || fail "告警政策「${POLICY_NAME}」已停用"
[ "${#fields[@]}" -gt 1 ] || fail "告警政策沒有設定通知管道"
log "告警政策「${POLICY_NAME}」：已啟用"

recipients=0
for channel in "${fields[@]:1}"; do
    info="$(api "$channel" | python3 -c '
import json, sys
c = json.load(sys.stdin)
print(c.get("type"), c.get("labels", {}).get("email_address", "-"), str(c.get("enabled", False)).lower())
')" || fail "讀不到通知管道 ${channel}"
    read -r ctype email enabled <<< "$info"
    log "  通知管道：${ctype} ${email}（enabled=${enabled}）"
    [ "$ctype" = "email" ] && [ "$enabled" = "true" ] && recipients=$((recipients + 1))
done
[ "$recipients" -gt 0 ] || fail "沒有已啟用的 Email 通知管道"

if [ "$CHECK_ONLY" = true ]; then
    log "設定檢查通過（--check-only，不寄信）"
    exit 0
fi

# ---------- 2. 寫入通知測試事件 ----------
RUN_ID="dryrun-$(date +%Y%m%d-%H%M%S)"
payload="$(python3 -c 'import json, sys; print(json.dumps({"event": "NOTIFY_TEST", "run_id": sys.argv[1], "requested_by": sys.argv[2]}))' "$RUN_ID" "$ACCOUNT")"
gcloud logging write "$LOG_ID" "$payload" --payload-type=json --severity=NOTICE --project="$PROJECT_ID" >/dev/null
log "已寫入通知測試事件：${RUN_ID}"

# ---------- 3. 確認事件已進 Cloud Logging ----------
filter="logName=\"projects/${PROJECT_ID}/logs/${LOG_ID}\" AND jsonPayload.run_id=\"${RUN_ID}\""
found=""
for _ in $(seq 1 12); do
    found="$(gcloud logging read "$filter" --project="$PROJECT_ID" --freshness=10m --limit=1 --format='value(insertId)' 2>/dev/null || true)"
    [ -n "$found" ] && break
    sleep 5
done
[ -n "$found" ] || fail "60 秒內在 Cloud Logging 找不到事件（寫入權限或 log 排除規則？）"
log "Cloud Logging 已收到事件"

cat <<EOF

[notify] 接下來：約 1～5 分鐘內，收件信箱應收到主旨「[k6 壓測] 通知測試 ${RUN_ID}」的信。
[notify] 沒收到時依序檢查：
  1. 垃圾郵件匣（寄件者通常是 alerting-noreply@google.com）
  2. 告警事件是否有建立：https://console.cloud.google.com/monitoring/alerting?project=${PROJECT_ID}
  3. 5 分鐘內是否已執行過一次（同一告警 5 分鐘內只寄一封）
EOF
