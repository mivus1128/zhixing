#!/usr/bin/env sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN=python
else
  echo "未找到 Python 3。" >&2
  exit 1
fi

"$PYTHON_BIN" "$PROJECT_ROOT/scripts/check_public_tree.py"
"$PYTHON_BIN" "$PROJECT_ROOT/scripts/check_version.py"

cmp "$PROJECT_ROOT/deploy/api.Dockerfile" "$PROJECT_ROOT/source/deploy/api.Dockerfile"
cmp "$PROJECT_ROOT/deploy/web.Dockerfile" "$PROJECT_ROOT/source/deploy/web.Dockerfile"

(
  cd "$PROJECT_ROOT/source/backend"
  PYTHONDONTWRITEBYTECODE=1 "$PYTHON_BIN" -m tests.smoke
)

if ! command -v npm >/dev/null 2>&1; then
  echo "未找到 npm。" >&2
  exit 1
fi

(
  cd "$PROJECT_ROOT/source/frontend"
  npm ci
  npm run check
)

diff -qr "$PROJECT_ROOT/source/frontend/dist" "$PROJECT_ROOT/frontend-dist"

COMPOSE_VALIDATED=0
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  if [ -f "$PROJECT_ROOT/.env" ]; then
    docker compose --env-file "$PROJECT_ROOT/.env" -f "$PROJECT_ROOT/deploy/compose.yaml" --profile collector config >/dev/null
  else
    docker compose -f "$PROJECT_ROOT/deploy/compose.yaml" --profile collector config >/dev/null
  fi
  COMPOSE_VALIDATED=1
else
  echo "提示：本机没有 Docker Compose，已跳过 Compose 解析校验。"
fi

if [ "$COMPOSE_VALIDATED" -eq 1 ]; then
  echo "全部公开版校验通过。"
else
  echo "源码与公开内容校验通过；Compose 尚未验证，发布前必须由 CI 或 Docker 主机完成。"
fi
