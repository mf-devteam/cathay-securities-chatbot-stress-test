#!/usr/bin/env bash
# 打包地端安裝包：dist/k6-stress-onprem-<版本>.tar.gz
#
#   onprem/bundle/build-bundle.sh                只有程式（VM 可連外時用，install.sh 線上安裝套件）
#   onprem/bundle/build-bundle.sh --with-deps    含離線套件（DMZ 無法連外時用，install.sh --offline）
#
# --with-deps 要在「與目標相同版本」的 Ubuntu 上執行（需要連外，不需要 root），例如 lab 的 control VM，或：
#   docker run --rm -v "$PWD":/src -w /src ubuntu:24.04 bash -c 'apt-get update -qq && apt-get install -y -qq curl python3-venv rsync >/dev/null && onprem/bundle/build-bundle.sh --with-deps'
#
# 安裝包內容：
#   k6-stress/runner/ report-job/ onprem/     程式（與 repo 相同結構）
#   k6-stress/deps/debs/*.deb                 套件與完整相依（install.sh 只安裝系統上還沒有的套件）
#   k6-stress/deps/k6/                        k6 執行檔與官方 checksum
#   k6-stress/deps/wheels/                    pandas、plotly（control 報表環境）
#
# 環境變數：K6_VERSION（預設 2.3.0，需與 install.sh 一致）、ARCH（預設 amd64）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
K6_VERSION="${K6_VERSION:-2.3.0}"
ARCH="${ARCH:-amd64}"
WITH_DEPS=false
[ "${1:-}" = "--with-deps" ] && WITH_DEPS=true

# 與 install.sh 的套件清單一致（runner＋control；mock 只有 lab 用，不打包）
PACKAGES=(rsync curl openssl ca-certificates bind9-dnsutils sysstat python3 python3-venv)

VERSION="$(git -C "$ROOT" describe --always --dirty 2>/dev/null || date +%Y%m%d)"
NAME="k6-stress-onprem-${VERSION}"
$WITH_DEPS && NAME="${NAME}-offline"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
STAGE="${WORK}/k6-stress"
mkdir -p "$STAGE"

log() { echo "[bundle] $*"; }
die() { echo "[bundle] 錯誤：$*" >&2; exit 1; }

for d in runner report-job onprem; do
    rsync -a --exclude results/ --exclude __pycache__/ --exclude tests/ --exclude .DS_Store "${ROOT}/${d}/" "${STAGE}/${d}/"
done

if $WITH_DEPS; then
    command -v apt-get >/dev/null || die "--with-deps 需要在 Ubuntu 上執行"
    . /etc/os-release
    log "下載套件與相依（${PRETTY_NAME}）"
    mkdir -p "${STAGE}/deps/debs"
    # 完整相依樹（不含 recommends）；目標機已安裝的會由 install.sh 略過
    mapfile -t all < <(apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts \
        --no-breaks --no-replaces --no-enhances "${PACKAGES[@]}" | grep -E '^[a-z0-9]' | sort -u)
    # 逐一下載：清單中的虛擬套件沒有 deb，略過即可
    (cd "${STAGE}/deps/debs" && for p in "${all[@]}"; do apt-get download -qq "$p" 2>/dev/null || echo "  略過 ${p}（虛擬套件或無候選版本）"; done)
    echo "${PRETTY_NAME}" > "${STAGE}/deps/debs/BUILT_ON"
    log "  $(ls "${STAGE}/deps/debs"/*.deb | wc -l) 個 deb"

    log "下載 Python 套件（pandas、plotly）"
    python3 -m venv "${WORK}/venv" || die "需要 python3-venv"
    "${WORK}/venv/bin/pip" download -q --disable-pip-version-check --only-binary=:all: \
        -d "${STAGE}/deps/wheels" -r "${ROOT}/onprem/config/report-requirements.txt"
    log "  $(ls "${STAGE}/deps/wheels" | wc -l) 個 wheel"

    log "下載 k6 v${K6_VERSION}（${ARCH}）"
    mkdir -p "${STAGE}/deps/k6"
    base="https://github.com/grafana/k6/releases/download/v${K6_VERSION}"
    curl -fsSL -o "${STAGE}/deps/k6/k6-v${K6_VERSION}-linux-${ARCH}.tar.gz" "${base}/k6-v${K6_VERSION}-linux-${ARCH}.tar.gz"
    curl -fsSL -o "${STAGE}/deps/k6/k6-v${K6_VERSION}-checksums.txt" "${base}/k6-v${K6_VERSION}-checksums.txt"
fi

mkdir -p "${ROOT}/dist"
tar -C "$WORK" -czf "${ROOT}/dist/${NAME}.tar.gz" k6-stress
(cd "${ROOT}/dist" && sha256sum "${NAME}.tar.gz" > "${NAME}.tar.gz.sha256" 2>/dev/null ||
    shasum -a 256 "${NAME}.tar.gz" > "${NAME}.tar.gz.sha256")
log "完成：dist/${NAME}.tar.gz（$(du -h "${ROOT}/dist/${NAME}.tar.gz" | cut -f1)）"
