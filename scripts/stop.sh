#!/usr/bin/env sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$SCRIPT_DIR/_common.sh"

require_docker
compose --profile collector down
echo "容器已停止，runtime、archives 和其中的配置仍然保留。"
echo "浏览器 Cookie/profile 不持久化，下次启动可能需要重新登录。"
