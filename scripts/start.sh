#!/usr/bin/env sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$SCRIPT_DIR/_common.sh"

require_docker

MODE=${1:-full}
case "$MODE" in
  full)
    if docker compose up --help 2>/dev/null | grep -q -- '--wait'; then
      compose --profile collector up -d --build --wait --wait-timeout 240
    else
      compose --profile collector up -d --build
    fi
    ;;
  web)
    if docker compose up --help 2>/dev/null | grep -q -- '--wait'; then
      compose up -d --build --wait --wait-timeout 120
    else
      compose up -d --build
    fi
    ;;
  *)
    echo "用法: bash scripts/start.sh [full|web]" >&2
    exit 2
    ;;
esac

compose --profile collector ps
echo
echo "知行已启动。默认地址: http://127.0.0.1:18765"
echo "如果修改了 .env 中的主机或端口，请使用修改后的地址。"
