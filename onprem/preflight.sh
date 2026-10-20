#!/usr/bin/env bash
# 連線預檢（規劃 p.17）：VM 建好即可執行，不需要先安裝 k6；只送 2 個請求，不造成負載。
# 只用 Ubuntu 內建工具：bash、curl、openssl、getent／dig、timedatectl。
#
# 用法：
#   ./preflight.sh https://<UAT 網域> [前端頁面網址]
#   BASE_URL=https://<UAT 網域> ./preflight.sh         （k6ctl preflight 以環境變數傳入）
# 選填環境變數：
#   FRONTEND_URL          步驟 4 的前端頁面，預設 BASE_URL/
#   EXPECTED_TLS_ISSUER   預期的憑證簽發者（正規表示式）；不符時判定為可能被 SSL inspection 攔截
#   PREFLIGHT_TIMEOUT_S   對話 API 逾時秒數，預設 60
#
# 結束碼：0 全部通過（或只有警告）、1 有項目失敗
set -uo pipefail

BASE_URL="${1:-${BASE_URL:-}}"
BASE_URL="${BASE_URL%/}"
FRONTEND_URL="${2:-${FRONTEND_URL:-}}"
FRONTEND_URL="${FRONTEND_URL:-${BASE_URL}/}"
TIMEOUT_S="${PREFLIGHT_TIMEOUT_S:-60}"
[ -n "$BASE_URL" ] || { echo "用法：$0 https://<UAT 網域> [前端頁面網址]" >&2; exit 2; }

scheme="${BASE_URL%%://*}"
hostport="${BASE_URL#*://}"; hostport="${hostport%%/*}"
host="${hostport%%:*}"
port="${hostport##*:}"; [ "$port" = "$hostport" ] && port=$([ "$scheme" = https ] && echo 443 || echo 80)

FAILED=0
ok()   { printf '  ✓ %-14s %s\n' "$1" "$2"; }
warn() { printf '  ! %-14s %s\n' "$1" "$2"; }
fail() { printf '  ✗ %-14s %s\n' "$1" "$2"; FAILED=1; }
skip() { printf '  - %-14s %s\n' "$1" "$2"; }

echo "連線預檢｜$(hostname)｜$(date '+%Y-%m-%d %H:%M:%S %z')"
echo "  目標：${BASE_URL}（${host}:${port}）"

# 1. DNS
if [[ "$host" =~ ^[0-9.]+$ ]]; then
    skip "1 DNS" "目標是 IP，略過"
else
    ips="$(getent ahostsv4 "$host" | awk '{ print $1 }' | sort -u | tr '\n' ' ')"
    if [ -n "$ips" ]; then
        ok "1 DNS" "${host} → ${ips}"
    else
        fail "1 DNS" "無法解析 ${host}：檢查 DNS 設定"
    fi
fi

# 2. TCP
if timeout 5 bash -c "exec 3<>/dev/tcp/${host}/${port}" 2>/dev/null; then
    ok "2 TCP ${port}" "可連線"
else
    fail "2 TCP ${port}" "無法連線：防火牆未開出站，或經過 Proxy"
fi

# 3. TLS 憑證簽發者
if [ "$scheme" != https ]; then
    skip "3 TLS" "http，略過"
else
    cert="$(echo | timeout 10 openssl s_client -connect "${host}:${port}" -servername "$host" 2>/dev/null |
        openssl x509 -noout -issuer -enddate 2>/dev/null)"
    issuer="$(sed -n 's/^issuer=//p' <<< "$cert")"
    if [ -z "$issuer" ]; then
        fail "3 TLS" "TLS 交握失敗：可能被 SSL inspection／IPS 攔截"
    elif [ -n "${EXPECTED_TLS_ISSUER:-}" ] && ! grep -Eq "$EXPECTED_TLS_ISSUER" <<< "$issuer"; then
        fail "3 TLS" "簽發者不符（${issuer}）：可能被 SSL inspection 攔截"
    else
        ok "3 TLS" "簽發者 ${issuer}（$(sed -n 's/^notAfter=/到期 /p' <<< "$cert")）"
    fi
fi

# 4. GET 前端頁面（不跟隨轉址：Cloud Armor 對非白名單 IP 回 302 導向維護頁）
read -r code redirect <<< "$(curl -s -o /dev/null --max-time 15 -w '%{http_code} %{redirect_url}' "$FRONTEND_URL")"
case "$code" in
    200) ok "4 前端頁面" "HTTP 200" ;;
    301|302|303|307) fail "4 前端頁面" "HTTP ${code} → ${redirect:-?}：Cloud Armor 白名單尚未加入" ;;
    000) fail "4 前端頁面" "逾時或被拒絕：出站被擋或經過 Proxy" ;;
    *) warn "4 前端頁面" "HTTP ${code}" ;;
esac

# 5. POST 對話 API（訪客開場「你好」），格式同 k6 腳本 runner/k6/lib/chat.js
sid="tpw$(date +%d%H%M%S)0preflt"
body="$(printf '{"sessionId":"%s","newSessionId":"%s","intent":"","secondBotIntent":"","msgId":"%s","role":"user","userInput":"你好","device":"Web","isInitialized":true,"channel":"web","recaptchaToken":"","ip":"198.18.0.1","udid":"preflight-%s","userData":{}}' \
    "$sid" "$sid" "$(cat /proc/sys/kernel/random/uuid 2>/dev/null | tr -d '-' || echo 0)" "$(hostname)")"
resp_file="$(mktemp)"
read -r code secs <<< "$(curl -s -o "$resp_file" --max-time "$TIMEOUT_S" -w '%{http_code} %{time_total}' \
    -H 'Content-Type: application/json' -H 'X-Load-Test-Run: preflight' -H 'X-Load-Test-Case: B1' -H 'X-Load-Test-Flow: init' \
    --data-binary "$body" "${BASE_URL}/api/chat/ask")"
snippet="$(head -c 120 "$resp_file" | tr '\n' ' ')"
rm -f "$resp_file"
case "$code" in
    200) if [ -n "$snippet" ]; then ok "5 對話 API" "HTTP 200（${secs}s）整條路徑都通：${snippet}"
         else warn "5 對話 API" "HTTP 200 但回應是空的"; fi ;;
    403) warn "5 對話 API" "HTTP 403：網路已通，reCAPTCHA 尚未暫停" ;;
    301|302|303|307) fail "5 對話 API" "HTTP ${code}：Cloud Armor 白名單尚未加入" ;;
    000) fail "5 對話 API" "逾時或被拒絕（${secs}s）：出站被擋或經過 Proxy" ;;
    *) fail "5 對話 API" "HTTP ${code}（${secs}s）：${snippet}" ;;
esac

# 6. NTP（VM 之間的時間差與 SSH 由 control 的 k6ctl preflight 檢查）
synced="$(timedatectl show -p NTPSynchronized --value 2>/dev/null)"
if command -v chronyc >/dev/null && chronyc tracking >/dev/null 2>&1; then
    detail="chrony：$(chronyc tracking | awk -F': ' '/Reference ID/ { r=$2 } /System time/ { s=$2 } END { print r "，誤差 " s }')"
else
    detail="timesyncd：$(timedatectl timesync-status 2>/dev/null | awk -F': ' '/Server/ { s=$2 } /Offset/ { o=$2 } END { print s "，誤差 " o }')"
fi
if [ "$synced" = yes ]; then ok "6 NTP" "已同步（${detail}）"; else fail "6 NTP" "未同步（${detail}）：確認 NTP 來源"; fi

echo
[ "$FAILED" -eq 0 ] && echo "  結果：通過" || echo "  結果：有項目未通過"
exit "$FAILED"
