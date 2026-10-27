#!/usr/bin/env bash
# 打包地端安裝包：dist/k6-stress-onprem-<版本>[-<OS>-offline].tar.gz
#
#   onprem/bundle/build-bundle.sh                只有程式（VM 可連外或有內部套件庫時用，install.sh 線上安裝套件）
#   onprem/bundle/build-bundle.sh --with-deps    含離線套件（DMZ 無法連外時用，install.sh --offline）
#
# --with-deps 下載的系統套件依「執行打包的 OS」而定，要與目標相同。一般用 Docker 打包（本機需要 Docker，不需要 root）：
#   onprem/bundle/build-in-docker.sh el9.6        RHEL 9.6（Rocky Linux 9.6 容器）
#   onprem/bundle/build-in-docker.sh ubuntu24.04  Ubuntu 24.04
#
# RHEL：套件固定取自 Rocky Linux 9.6 vault（與 RHEL 9.6 二進位相容；不需要 Red Hat 訂閱，版本不會漂到 9.7 之後），
#   打包時以 Rocky 官方金鑰驗證每個 rpm 的簽章。改用其他來源：EL_REPOS=system（用打包機上已設定的套件庫，例如已註冊的 RHEL 9.6）
#
# 安裝包內容：
#   k6-stress/runner/ report-job/ onprem/     程式（與 repo 相同結構）
#   k6-stress/deps/rpms/                      RHEL：套件與完整相依＋repodata（install.sh 只裝目標機缺少的）、SIGNATURES
#   k6-stress/deps/debs/*.deb                 Ubuntu：套件與完整相依（install.sh 只安裝系統上還沒有的套件）
#   k6-stress/deps/k6/                        k6 執行檔與官方 checksum
#   k6-stress/deps/wheels/                    pandas、plotly（control 報表環境；依打包機的 Python 版本）
#
# 環境變數：K6_VERSION（預設 2.3.0，需與 install.sh 一致）、ARCH（預設 amd64）、EL_RELEASE（預設 9.6）、BUNDLE_VERSION（預設 git describe）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
K6_VERSION="${K6_VERSION:-2.3.0}"
ARCH="${ARCH:-amd64}"
EL_RELEASE="${EL_RELEASE:-9.6}"
EL_REPOS="${EL_REPOS:-rocky-vault}"
WITH_DEPS=false
[ "${1:-}" = "--with-deps" ] && WITH_DEPS=true

# 與 install.sh 的套件清單一致（runner＋control；mock 只有 lab 用，不打包）
DEB_PACKAGES=(rsync curl openssl ca-certificates bind9-dnsutils sysstat python3 python3-venv)
RPM_PACKAGES=(rsync curl openssl ca-certificates bind-utils policycoreutils-python-utils openssh-clients sudo sysstat python3)

VERSION="${BUNDLE_VERSION:-$(git -C "$ROOT" describe --always --dirty 2>/dev/null || date +%Y%m%d)}"
NAME="k6-stress-onprem-${VERSION}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
STAGE="${WORK}/k6-stress"
mkdir -p "$STAGE"

log() { echo "[bundle] $*"; }
die() { echo "[bundle] 錯誤：$*" >&2; exit 1; }

for d in runner report-job onprem; do
    rsync -a --exclude results/ --exclude __pycache__/ --exclude tests/ --exclude .DS_Store "${ROOT}/${d}/" "${STAGE}/${d}/"
done

# RHEL：下載完整相依樹，驗證簽章，建立 repodata（install.sh 用 dnf --repofrompath 安裝）
download_rpms() {
    local dir="${STAGE}/deps/rpms" repo_args=() key
    command -v createrepo_c >/dev/null || die "需要 createrepo_c（dnf install createrepo_c）"
    dnf download --help >/dev/null 2>&1 || die "需要 dnf download（dnf install dnf-plugins-core）"
    mkdir -p "$dir"
    if [ "$EL_REPOS" = rocky-vault ]; then
        local base="https://dl.rockylinux.org/vault/rocky/${EL_RELEASE}"
        repo_args=(--disablerepo='*'
            --repofrompath="el-baseos,${base}/BaseOS/$(uname -m)/os/"
            --repofrompath="el-appstream,${base}/AppStream/$(uname -m)/os/")
        echo "Rocky Linux ${EL_RELEASE} vault（BaseOS、AppStream）— 供 RHEL ${EL_RELEASE} 使用" > "${dir}/BUILT_ON"
    else
        . /etc/os-release
        echo "${PRETTY_NAME}（打包機的套件庫）" > "${dir}/BUILT_ON"
    fi
    log "下載套件與相依（$(cat "${dir}/BUILT_ON")）"
    # --alldeps：完整相依樹，不受打包機已安裝套件影響；目標機已安裝的由 dnf 略過
    dnf download -q "${repo_args[@]}" --resolve --alldeps --arch="$(uname -m)" --arch=noarch \
        --destdir "$dir" "${RPM_PACKAGES[@]}"
    # 排除 Rocky 自行修改的套件（release 含 .rocky.，以及 rocky-release／repos／gpg-keys）：
    # 與 RHEL 原廠同名套件版本號不同，會與目標機已安裝的套件衝突（例如 openssh-clients 綁定 openssh 的 rocky 版）。
    # 這些都是 RHEL 一定已安裝的系統套件，目標機用原廠版本即可
    local f excluded=()
    for f in "$dir"/*.rpm; do
        case "$(basename "$f")" in
            *.rocky.*|rocky-*) excluded+=("$(basename "$f")"); rm -f "$f" ;;
        esac
    done
    printf '%s\n' "${excluded[@]}" > "${dir}/EXCLUDED"
    log "  $(ls "$dir"/*.rpm | wc -l) 個 rpm（排除 Rocky 專屬套件 ${#excluded[@]} 個，見 deps/rpms/EXCLUDED）"

    # 簽章：用獨立的 rpm 資料庫匯入金鑰驗證，不需要 root、不動打包機
    key="${EL_GPG_KEY:-/etc/pki/rpm-gpg/RPM-GPG-KEY-Rocky-9}"
    if [ "$EL_REPOS" = rocky-vault ] || [ -f "$key" ]; then
        [ -f "$key" ] || die "找不到簽章金鑰 ${key}（EL_GPG_KEY 指定）"
        local db="${WORK}/rpmdb"
        mkdir -p "$db"
        rpmkeys --dbpath "$db" --import "$key"
        if ! rpmkeys --dbpath "$db" --checksig "$dir"/*.rpm > "${dir}/SIGNATURES"; then
            grep -v ': digests signatures OK$' "${dir}/SIGNATURES" >&2
            die "有 rpm 簽章驗證失敗"
        fi
        log "  簽章驗證通過（$(basename "$key")）"
    else
        log "  警告：沒有簽章金鑰，略過驗證"
    fi
    createrepo_c -q "$dir"
}

download_debs() {
    . /etc/os-release
    log "下載套件與相依（${PRETTY_NAME}）"
    mkdir -p "${STAGE}/deps/debs"
    # 完整相依樹（不含 recommends）；目標機已安裝的會由 install.sh 略過
    mapfile -t all < <(apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts \
        --no-breaks --no-replaces --no-enhances "${DEB_PACKAGES[@]}" | grep -E '^[a-z0-9]' | sort -u)
    # 逐一下載：清單中的虛擬套件沒有 deb，略過即可
    (cd "${STAGE}/deps/debs" && for p in "${all[@]}"; do apt-get download -qq "$p" 2>/dev/null || echo "  略過 ${p}（虛擬套件或無候選版本）"; done)
    echo "${PRETTY_NAME}" > "${STAGE}/deps/debs/BUILT_ON"
    log "  $(ls "${STAGE}/deps/debs"/*.deb | wc -l) 個 deb"
}

if $WITH_DEPS; then
    if command -v dnf >/dev/null; then
        download_rpms
        os_tag="el${EL_RELEASE}"
    elif command -v apt-get >/dev/null; then
        download_debs
        . /etc/os-release
        os_tag="${ID}${VERSION_ID}"
    else
        die "--with-deps 需要在 RHEL／Rocky（dnf）或 Ubuntu（apt）上執行，或用 build-in-docker.sh"
    fi
    NAME="${NAME}-${os_tag}-offline"

    log "下載 Python 套件（pandas、plotly；$(python3 --version)）"
    python3 -m venv "${WORK}/venv" || die "需要 python3-venv"
    "${WORK}/venv/bin/pip" download -q --disable-pip-version-check --only-binary=:all: \
        -d "${STAGE}/deps/wheels" -r "${ROOT}/onprem/config/report-requirements.txt"
    log "  $(ls "${STAGE}/deps/wheels" | wc -l) 個 wheel"

    log "下載 k6 v${K6_VERSION}（${ARCH}）"
    mkdir -p "${STAGE}/deps/k6"
    base="https://github.com/grafana/k6/releases/download/v${K6_VERSION}"
    curl -fsSL -o "${STAGE}/deps/k6/k6-v${K6_VERSION}-linux-${ARCH}.tar.gz" "${base}/k6-v${K6_VERSION}-linux-${ARCH}.tar.gz"
    curl -fsSL -o "${STAGE}/deps/k6/k6-v${K6_VERSION}-checksums.txt" "${base}/k6-v${K6_VERSION}-checksums.txt"
    (cd "${STAGE}/deps/k6" && grep " k6-v${K6_VERSION}-linux-${ARCH}.tar.gz\$" "k6-v${K6_VERSION}-checksums.txt" | sha256sum -c --quiet -) ||
        die "k6 checksum 不符"
fi

mkdir -p "${ROOT}/dist"
tar -C "$WORK" -czf "${ROOT}/dist/${NAME}.tar.gz" k6-stress
(cd "${ROOT}/dist" && sha256sum "${NAME}.tar.gz" > "${NAME}.tar.gz.sha256" 2>/dev/null ||
    shasum -a 256 "${NAME}.tar.gz" > "${NAME}.tar.gz.sha256")
log "完成：dist/${NAME}.tar.gz（$(du -h "${ROOT}/dist/${NAME}.tar.gz" | cut -f1)）"
