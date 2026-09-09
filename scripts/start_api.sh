#!/usr/bin/env bash
# FastAPI 启动（问诊端口段入口：127.0.0.1:8100）
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python 环境不存在或不可执行: $PYTHON_BIN" >&2
  exit 1
fi

cd "$PROJECT_ROOT"
exec "$PYTHON_BIN" -m uvicorn app.main:create_app \
  --factory --host 127.0.0.1 --port "${APP_PORT:-8100}" --workers 1
