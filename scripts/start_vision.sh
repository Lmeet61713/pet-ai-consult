#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${VISION_PYTHON:-/root/autodl-tmp/conda/pet-mm/bin/python3.12}"
HOST="${VISION_HOST:-127.0.0.1}"
PORT="${VISION_PORT:-8102}"

# ===== 端口占用保护（避免与已运行的 VisionGateway/supervisor 重复启动冲突）=====
if timeout 1 bash -c "</dev/tcp/${HOST}/${PORT}" 2>/dev/null; then
  echo "VisionGateway 已在 ${HOST}:${PORT} 运行，跳过启动（可能是 supervisor 或 start_models.sh 已拉起）" >&2
  exit 0
fi

cd "$PROJECT_ROOT"
exec "$PYTHON_BIN" -m uvicorn app.vision_server:create_app \
  --factory --host "$HOST" --port "$PORT" --workers 1
