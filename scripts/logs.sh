#!/usr/bin/env sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$SCRIPT_DIR/_common.sh"

require_docker
if [ "$#" -gt 0 ]; then
  compose --profile collector logs --tail=200 "$@"
else
  compose --profile collector logs --tail=200
fi
