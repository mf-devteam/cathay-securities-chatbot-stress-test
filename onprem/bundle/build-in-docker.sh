#!/usr/bin/env bash
# 用 Docker 打包離線安裝包（本機任何 OS 皆可，需要 Docker 與連外）：
#   onprem/bundle/build-in-docker.sh el9.6        RHEL 9.6（Rocky Linux 9.6 容器；套件固定取自 9.6 vault）
#   onprem/bundle/build-in-docker.sh ubuntu24.04  Ubuntu 24.04
# 產出：dist/k6-stress-onprem-<版本>-<OS>-offline.tar.gz（容器內以 root 打包，結束後把檔案擁有者改回目前使用者）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TARGET="${1:-}"
VERSION="$(git -C "$ROOT" describe --always --dirty 2>/dev/null || date +%Y%m%d)"
OWNER="$(id -u):$(id -g)"

case "$TARGET" in
    el9.*)
        image="rockylinux/rockylinux:${TARGET#el}"
        prepare="dnf install -y -q dnf-plugins-core createrepo_c rsync >/dev/null"
        env_args=(-e "EL_RELEASE=${TARGET#el}")
        ;;
    ubuntu*)
        image="ubuntu:${TARGET#ubuntu}"
        prepare="apt-get update -qq && apt-get install -y -qq curl python3-venv rsync >/dev/null"
        env_args=()
        ;;
    *) echo "用法：$0 el9.6|ubuntu24.04" >&2; exit 2 ;;
esac

echo "[docker] ${image}（版本 ${VERSION}）"
docker run --rm --platform linux/amd64 -v "${ROOT}:/src" -w /src \
    -e "BUNDLE_VERSION=${VERSION}" ${env_args[@]+"${env_args[@]}"} "$image" \
    bash -c "${prepare} && onprem/bundle/build-bundle.sh --with-deps && chown -R ${OWNER} dist"
