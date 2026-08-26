#!/usr/bin/env sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
COMPOSE_FILE="$PROJECT_ROOT/deploy/compose.yaml"

compose() {
  if [ -f "$PROJECT_ROOT/.env" ]; then
    docker compose --env-file "$PROJECT_ROOT/.env" -f "$COMPOSE_FILE" "$@"
  else
    docker compose -f "$COMPOSE_FILE" "$@"
  fi
}

require_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "未找到 docker，请先安装 Docker Engine 和 Docker Compose v2。" >&2
    exit 1
  fi
  docker compose version >/dev/null
  docker info >/dev/null
}
