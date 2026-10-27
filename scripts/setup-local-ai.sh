#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [ ! -x .venv/bin/python ]; then
  echo '请用 Python 3.12 建立虚拟环境：python3.12 -m venv .venv' >&2
  exit 1
fi
.venv/bin/python -m pip install -r bridge/requirements-ai.txt
.venv/bin/python scripts/download-models.py "$@"
.venv/bin/python -c 'import sys; sys.path.insert(0,"bridge"); from local_ai import LocalTranslator; t=LocalTranslator(); t.load(); print("Local YAMNet + Whisper Tiny ready")'
