#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [ ! -x .venv/bin/python ]; then
  echo '缺少 .venv，请先按 APP_SETUP.md 安装桥接依赖。' >&2
  exit 1
fi
exec .venv/bin/python -u bridge/server.py "$@"
