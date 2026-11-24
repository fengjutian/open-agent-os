#!/usr/bin/env sh
# 统一启动脚本（macOS / Linux）。
# 用法：./scripts/dev.sh [--executor] [--backend-port 8000] ...
set -eu
exec "${PYTHON:-python3}" "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/dev.py" "$@"
