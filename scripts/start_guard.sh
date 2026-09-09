#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -n "${GUARD_PYTHON:-}" ]]; then
    PYTHON_BIN="$GUARD_PYTHON"
elif [[ -x /root/autodl-tmp/conda/pet-mm/bin/python3.12 ]]; then
    PYTHON_BIN=/root/autodl-tmp/conda/pet-mm/bin/python3.12
else
    PYTHON_BIN="${PROJECT_ROOT}/.venv/bin/python"
fi
HOST="${GUARD_HOST:-127.0.0.1}"
PORT="${GUARD_PORT:-8103}"

exec "$PYTHON_BIN" -m uvicorn app.guard_server:create_app \
  --factory --host "$HOST" --port "$PORT" --workers 1
