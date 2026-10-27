#!/usr/bin/env bash
# 地端壓測 VM 安裝腳本（RHEL 9.6；Ubuntu 24.04／22.04 亦可）。可重複執行：已完成的步驟會略過或覆蓋成相同結果。
# 證券方地端 VM 由 MF 手動執行；lab（GCE）由 Terraform 的 startup script 自動執行，兩邊是同一份腳本。
#
# 用法（root）：
#   sudo ./onprem/install.sh --role runner  --authorized-key-file control.pub
#   sudo ./onprem/install.sh --role control
#   sudo ./onprem/install.sh --role mock                       # 只有 lab 用：受測端 mock server
#
# 選項：
#   --role runner|control|mock  必填
#   --offline                   不連外：套件從 deps/ 安裝（先用 onprem/bundle/build-bundle.sh --with-deps 打包，OS 要與目標相同）
#   --authorized-key "KEY"      runner：允許 control 以此公鑰 SSH 登入（可重複）
#   --authorized-key-file FILE  同上，從檔案讀
#   --ntp SERVER                設定 NTP 來源（證券方 NTP）；未指定時不改系統設定
#   --user NAME                 執行壓測的帳號，預設 k6
#   --skip-tuning               不調整 OS 參數（nofile、port range、tw_reuse）
#
# 做的事（依規劃 p.10～11）：
#   全部   建立 k6 帳號、程式放到 /opt/k6-stress、rsync／curl／openssl／dig
#          RHEL：SELinux 為 enforcing 時，設定 k6 帳號 .ssh 的 context（home 不在 /home，sshd 預設讀不到）
#   runner k6（固定版本）、sysstat（sar）、OS 參數、允許 control 的 SSH 公鑰
#   control 產生 SSH 金鑰、k6ctl 指令與 k6menu 操作選單、/etc/k6-stress/stress.env、python3-venv＋pandas／plotly（/opt/k6-stress/venv）
#   mock   nodejs 與 k6-mock systemd 服務（port 8090）
set -euo pipefail

K6_VERSION="${K6_VERSION:-2.3.0}"
APP_DIR=/opt/k6-stress
DATA_DIR=/var/lib/k6-stress
CONF_DIR=/etc/k6-stress
SRC_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEPS_DIR="${SRC_ROOT}/deps"

ROLE=""
OFFLINE=false
K6_USER=k6
NTP_SERVER=""
SKIP_TUNING=false
AUTH_KEYS=()

log() { echo "[install] $*"; }
die() { echo "[install] 錯誤：$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --role) ROLE="$2"; shift 2 ;;
        --offline) OFFLINE=true; shift ;;
        --authorized-key) AUTH_KEYS+=("$2"); shift 2 ;;
        --authorized-key-file) [ -f "$2" ] || die "找不到 $2"; AUTH_KEYS+=("$(cat "$2")"); shift 2 ;;
        --ntp) NTP_SERVER="$2"; shift 2 ;;
        --user) K6_USER="$2"; shift 2 ;;
        --skip-tuning) SKIP_TUNING=true; shift ;;
        -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
        *) die "未知的選項：$1" ;;
    esac
done

case "$ROLE" in runner|control|mock) ;; *) die "--role 必須是 runner、control 或 mock" ;; esac
[ "$(id -u)" -eq 0 ] || die "請用 root 執行（sudo）"
. /etc/os-release
case " ${ID:-} ${ID_LIKE:-} " in
    *" rhel "*|*" fedora "*) PKG=rpm; validated='RHEL 9.6' ;;
    *" debian "*|*" ubuntu "*) PKG=deb; validated='Ubuntu 24.04／22.04' ;;
    *) die "不支援的 OS：${PRETTY_NAME:-未知}（支援 RHEL 9、Ubuntu 24.04／22.04）" ;;
esac
case "${PKG}:${VERSION_ID:-}" in rpm:9.*|deb:24.04|deb:22.04) ;; *) log "警告：只在 ${validated} 驗證過，目前是 ${PRETTY_NAME:-未知}" ;; esac
case "$(uname -m)" in x86_64) ARCH=amd64 ;; aarch64) ARCH=arm64 ;; *) die "不支援的 CPU 架構：$(uname -m)" ;; esac

# ---------- 套件 ----------
# 與 bundle/build-bundle.sh 的清單一致。RHEL：curl 指定指令路徑（curl 與 curl-minimal 互斥，已裝哪個都可以）；
# python3 內建 venv；semanage（policycoreutils-python-utils）設定 SELinux context
if [ "$PKG" = rpm ]; then
    packages=(rsync /usr/bin/curl openssl ca-certificates bind-utils policycoreutils-python-utils openssh-clients sudo)
else
    packages=(rsync curl openssl ca-certificates bind9-dnsutils)
fi
case "$ROLE" in
    runner) packages+=(sysstat) ;;
    control) packages+=(python3); [ "$PKG" = deb ] && packages+=(python3-venv) ;;
    mock) packages+=(nodejs) ;;
esac

install_packages() {
    if [ "$PKG" = rpm ]; then
        install_rpms
    else
        install_debs
    fi
}

# RHEL：離線時把 deps/rpms 當成本機套件庫，dnf 依套件名稱解相依，只裝缺少的（已安裝的不動）
install_rpms() {
    local dnf_args=(-y -q --setopt=install_weak_deps=False) todo=() p
    # 只處理還沒安裝的：已安裝的系統套件（例如 RHEL 原廠的 openssh-clients）完全不動，避免被換成離線包裡的版本
    for p in "${packages[@]}"; do
        if [[ "$p" == /* ]]; then
            rpm -q --whatprovides "$p" >/dev/null 2>&1 && continue
        else
            rpm -q "$p" >/dev/null 2>&1 && continue
        fi
        todo+=("$p")
    done
    if [ ${#todo[@]} -eq 0 ]; then
        log "套件都已安裝：${packages[*]}"
        return
    fi
    if $OFFLINE; then
        [ -d "${DEPS_DIR}/rpms/repodata" ] || die "離線包沒有 RHEL 套件（deps/rpms）：$(cat "${DEPS_DIR}"/*/BUILT_ON 2>/dev/null || echo 不是離線包)"
        log "離線安裝缺少的套件：${todo[*]}（$(cat "${DEPS_DIR}/rpms/BUILT_ON")）"
        # 套件已在打包時以 Rocky 官方金鑰驗證過簽章（見 deps/rpms/SIGNATURES），這裡不把金鑰匯入目標機
        dnf_args+=(--disablerepo='*' --repofrompath="k6-stress-deps,file://${DEPS_DIR}/rpms"
            --setopt=k6-stress-deps.gpgcheck=0)
    else
        log "安裝缺少的套件：${todo[*]}"
    fi
    dnf install "${dnf_args[@]}" "${todo[@]}" ||
        die "套件安裝失敗：${todo[*]}。離線包不含 RHEL 原廠已有的系統套件（openssh、systemd 等）；若上方訊息提到找不到或衝突，請用貴司 RHEL 套件來源（安裝光碟或 Satellite）先安裝這些套件，再重新執行"
}

install_debs() {
    if $OFFLINE; then
        # 只安裝系統上還沒有的套件：已安裝的不動，避免把較新的系統套件降版
        local deb pkg todo=()
        for deb in "${DEPS_DIR}"/debs/*.deb; do
            [ -e "$deb" ] || continue
            pkg="$(dpkg-deb -f "$deb" Package)"
            dpkg-query -W -f '${Status}' "$pkg" 2>/dev/null | grep -q 'install ok installed' || todo+=("$deb")
        done
        if [ ${#todo[@]} -gt 0 ]; then
            log "離線安裝 ${#todo[@]} 個 deb"
            dpkg -i "${todo[@]}" >/dev/null
        fi
        local missing=()
        for p in "${packages[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
        [ ${#missing[@]} -eq 0 ] || die "離線包缺少套件：${missing[*]}（請用同版 Ubuntu 重新打包）"
    else
        log "安裝套件：${packages[*]}"
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq
        apt-get install -y -qq --no-install-recommends "${packages[@]}" >/dev/null
    fi
}

# k6：單一執行檔，固定版本並驗證 checksum
install_k6() {
    # 用完整路徑：sudo 的 PATH（secure_path）不含 /usr/local/bin
    if [ -x /usr/local/bin/k6 ] && /usr/local/bin/k6 version 2>/dev/null | grep -q "v${K6_VERSION}\b"; then
        log "k6 v${K6_VERSION} 已安裝"
        return
    fi
    local name="k6-v${K6_VERSION}-linux-${ARCH}" tmp
    tmp="$(mktemp -d)"
    if $OFFLINE; then
        cp "${DEPS_DIR}/k6/${name}.tar.gz" "${DEPS_DIR}/k6/k6-v${K6_VERSION}-checksums.txt" "$tmp/" ||
            die "離線包缺少 deps/k6/${name}.tar.gz"
    else
        local base="https://github.com/grafana/k6/releases/download/v${K6_VERSION}"
        log "下載 k6 v${K6_VERSION}"
        curl -fsSL -o "${tmp}/${name}.tar.gz" "${base}/${name}.tar.gz"
        curl -fsSL -o "${tmp}/k6-v${K6_VERSION}-checksums.txt" "${base}/k6-v${K6_VERSION}-checksums.txt"
    fi
    (cd "$tmp" && grep " ${name}.tar.gz\$" "k6-v${K6_VERSION}-checksums.txt" | sha256sum -c --quiet -) || die "k6 checksum 不符"
    tar -xzf "${tmp}/${name}.tar.gz" -C "$tmp"
    install -m 0755 "${tmp}/${name}/k6" /usr/local/bin/k6
    rm -rf "$tmp"
    log "k6 $(/usr/local/bin/k6 version | head -1)"
}

# ---------- 帳號與目錄 ----------
setup_user() {
    if ! id "$K6_USER" >/dev/null 2>&1; then
        useradd --system --create-home --home-dir "$DATA_DIR" --shell /bin/bash "$K6_USER"
        log "建立帳號 ${K6_USER}"
    fi
    install -d -o "$K6_USER" -g "$K6_USER" -m 0755 "$DATA_DIR" "${DATA_DIR}/runs" "$APP_DIR"
    install -d -o "$K6_USER" -g "$K6_USER" -m 0700 "${DATA_DIR}/.ssh"
}

# 程式：runner（k6 腳本＋entrypoint）、report-job、onprem。control 啟動壓測前會再同步一次到 runner，確保版本一致
install_app() {
    if [ "$SRC_ROOT" != "$APP_DIR" ]; then
        for d in runner report-job onprem; do
            [ -d "${SRC_ROOT}/${d}" ] || die "找不到 ${SRC_ROOT}/${d}（請在完整的安裝包內執行）"
            rsync -a --delete --exclude results/ --exclude __pycache__/ --exclude tests/ "${SRC_ROOT}/${d}/" "${APP_DIR}/${d}/"
        done
    fi
    chown -R "$K6_USER:$K6_USER" "$APP_DIR"
    chmod +x "${APP_DIR}"/onprem/*.sh "${APP_DIR}"/onprem/bundle/*.sh "${APP_DIR}"/onprem/bin/* "${APP_DIR}/runner/entrypoint.sh"
    ln -sf "${APP_DIR}/onprem/preflight.sh" /usr/local/bin/k6-preflight
}

# ---------- OS 參數（規劃 p.11） ----------
tune_os() {
    $SKIP_TUNING && { log "略過 OS 參數"; return; }
    cat > /etc/security/limits.d/90-k6-stress.conf <<EOF
# 每條連線佔 1 個檔案描述子；每台 runner 最多約 3,500 段同時對話
${K6_USER} soft nofile 1048576
${K6_USER} hard nofile 1048576
EOF
    cat > /etc/sysctl.d/90-k6-stress.conf <<'EOF'
# 每條對外連線佔 1 個來源 port，擴大範圍避免 port 用盡
net.ipv4.ip_local_port_range = 1024 65535
# 允許重用 TIME_WAIT 連線建立新的對外連線
net.ipv4.tcp_tw_reuse = 1
EOF
    sysctl -q --load /etc/sysctl.d/90-k6-stress.conf
    log "OS 參數：nofile 1048576、ip_local_port_range 1024 65535、tcp_tw_reuse 1"
}

# ---------- 時間同步 ----------
setup_ntp() {
    [ -n "$NTP_SERVER" ] || return 0
    local svc conf
    for svc in chronyd chrony; do systemctl is-active --quiet "$svc" && break; svc=""; done
    if [ -n "$svc" ]; then
        # Ubuntu 的 chrony.conf 已含 sourcedir /etc/chrony/sources.d；RHEL（/etc/chrony.conf）沒有，補上後重啟
        conf=/etc/chrony/chrony.conf; [ -f "$conf" ] || conf=/etc/chrony.conf
        install -d /etc/chrony/sources.d
        echo "server ${NTP_SERVER} iburst" > /etc/chrony/sources.d/k6-stress.sources
        if grep -Eq '^[[:space:]]*sourcedir[[:space:]]+/etc/chrony/sources.d/?[[:space:]]*$' "$conf"; then
            chronyc reload sources >/dev/null
        else
            printf '\n# k6-stress（install.sh --ntp）\nsourcedir /etc/chrony/sources.d\n' >> "$conf"
            systemctl restart "$svc"
        fi
    else
        install -d /etc/systemd/timesyncd.conf.d
        printf '[Time]\nNTP=%s\n' "$NTP_SERVER" > /etc/systemd/timesyncd.conf.d/k6-stress.conf
        timedatectl set-ntp true
        systemctl restart systemd-timesyncd
    fi
    log "NTP 來源：${NTP_SERVER}"
}

# ---------- SSH ----------
setup_control_ssh() {
    local key="${DATA_DIR}/.ssh/id_ed25519"
    if [ ! -f "$key" ]; then
        sudo -u "$K6_USER" ssh-keygen -q -t ed25519 -N '' -C "k6-control@$(hostname)" -f "$key"
    fi
    cat > "${DATA_DIR}/.ssh/config" <<'EOF'
Host *
    BatchMode yes
    StrictHostKeyChecking accept-new
    LogLevel ERROR
    ConnectTimeout 10
    ServerAliveInterval 30
EOF
    chown "$K6_USER:$K6_USER" "${DATA_DIR}/.ssh/config"
    chmod 0600 "${DATA_DIR}/.ssh/config"
}

setup_runner_ssh() {
    local file="${DATA_DIR}/.ssh/authorized_keys"
    touch "$file"
    for k in "${AUTH_KEYS[@]}"; do
        grep -qxF "$k" "$file" || echo "$k" >> "$file"
    done
    chown "$K6_USER:$K6_USER" "$file"
    chmod 0600 "$file"
    [ -s "$file" ] || log "警告：尚未加入 control 的公鑰，之後用 --authorized-key-file 再執行一次"
}

# RHEL：k6 的 home 在 /var/lib（var_lib_t），SELinux enforcing 時 sshd 讀不到 authorized_keys，需標成 ssh_home_t
setup_selinux_ssh() {
    command -v selinuxenabled >/dev/null && selinuxenabled || return 0
    local spec="${DATA_DIR}/\.ssh(/.*)?"
    if command -v semanage >/dev/null; then
        semanage fcontext -a -t ssh_home_t "$spec" 2>/dev/null || semanage fcontext -m -t ssh_home_t "$spec"
        restorecon -R "${DATA_DIR}/.ssh"
    else
        chcon -R -t ssh_home_t "${DATA_DIR}/.ssh"
        log "警告：沒有 semanage，SELinux context 用 chcon 暫時設定（系統 relabel 後會失效，再執行一次 install.sh 即可）"
    fi
    log "SELinux（$(getenforce)）：${DATA_DIR}/.ssh → ssh_home_t"
}

# ---------- 各角色 ----------
setup_control() {
    install -d -o "$K6_USER" -g "$K6_USER" -m 0750 "$CONF_DIR"
    if [ ! -f "${CONF_DIR}/stress.env" ]; then
        install -o "$K6_USER" -g "$K6_USER" -m 0640 "${APP_DIR}/onprem/config/stress.env.example" "${CONF_DIR}/stress.env"
        log "已建立 ${CONF_DIR}/stress.env，請填入 RUNNERS（runner 的 IP；UAT 網址已預先填好）"
    fi
    ln -sf "${APP_DIR}/onprem/bin/k6ctl" /usr/local/bin/k6ctl
    ln -sf "${APP_DIR}/onprem/bin/k6menu" /usr/local/bin/k6menu
    setup_control_ssh
    setup_selinux_ssh
    setup_report_venv
}

# 報表用的獨立 Python 環境（Ubuntu 24.04 預設禁止 pip 直接裝到系統 Python；RHEL 9 為 Python 3.9）：pandas、plotly（規劃 p.10）
setup_report_venv() {
    local venv="${APP_DIR}/venv" req="${APP_DIR}/onprem/config/report-requirements.txt"
    [ -x "${venv}/bin/python3" ] || sudo -u "$K6_USER" python3 -m venv "$venv"
    if $OFFLINE; then
        # pip 以 k6 執行：安裝包可能解壓在 k6 讀不到的地方（例如 /root），先複製到暫存目錄
        local wheels
        wheels="$(mktemp -d)"
        cp "${DEPS_DIR}"/wheels/*.whl "$wheels/" || die "離線包缺少 Python 套件（deps/wheels）"
        chmod -R a+rX "$wheels"
        sudo -u "$K6_USER" "${venv}/bin/pip" install -q --no-index --find-links "$wheels" -r "$req" ||
            { rm -rf "$wheels"; die "離線包缺少 Python 套件（deps/wheels）"; }
        rm -rf "$wheels"
    else
        sudo -u "$K6_USER" "${venv}/bin/pip" install -q --disable-pip-version-check -r "$req"
    fi
    log "報表 Python 環境：${venv}（$("${venv}/bin/python3" -c 'import pandas, plotly; print(f"pandas {pandas.__version__}、plotly {plotly.__version__}")')）"
}

setup_runner() {
    install_k6
    ln -sf "${APP_DIR}/onprem/bin/k6-agent" /usr/local/bin/k6-agent
    setup_runner_ssh
    setup_selinux_ssh
}

setup_mock() {
    cat > /etc/systemd/system/k6-mock.service <<EOF
[Unit]
Description=新阿發 BFF mock server（lab 壓測受測端）
After=network-online.target

[Service]
User=${K6_USER}
Environment=MOCK_HOST=0.0.0.0
Environment=MOCK_PORT=8090
EnvironmentFile=-${CONF_DIR}/mock.env
ExecStart=/usr/bin/node ${APP_DIR}/runner/k6/mock/mock-server.js
Restart=on-failure
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --quiet k6-mock
    systemctl restart k6-mock
    log "k6-mock 服務已啟動（port 8090；參數可寫在 ${CONF_DIR}/mock.env）"
}

# ---------- 執行 ----------
log "角色：${ROLE}　OS：${PRETTY_NAME:-?}　來源：${SRC_ROOT}　$($OFFLINE && echo 離線 || echo 線上)安裝"
install_packages
setup_user
install_app
setup_ntp
case "$ROLE" in
    runner) tune_os; setup_runner ;;
    control) setup_control ;;
    mock) tune_os; setup_mock ;;
esac

log "完成。"
case "$ROLE" in
    control)
        echo
        echo "  control 的 SSH 公鑰（安裝 runner 時用 --authorized-key 帶入）："
        echo "    $(cat "${DATA_DIR}/.ssh/id_ed25519.pub")"
        echo "  下一步：編輯 ${CONF_DIR}/stress.env，再執行 k6menu（操作選單）或 k6ctl check"
        ;;
    runner)
        echo "  k6：$(/usr/local/bin/k6 version | head -1)"
        echo "  nofile（${K6_USER}）：$(su - "$K6_USER" -c 'ulimit -n')"
        echo "  port range：$(sysctl -n net.ipv4.ip_local_port_range | tr '\t' ' ')　tw_reuse：$(sysctl -n net.ipv4.tcp_tw_reuse)"
        ;;
esac
