#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_ROOT/.venv/bin/python}"
REDIS_BIN="${REDIS_BIN:-$(command -v redis-server || true)}"
RUNTIME_DIR="$PROJECT_ROOT/runtime/redis"

if [[ -z "$REDIS_BIN" ]]; then
  echo "redis-server is required to start Redis" >&2
  exit 1
fi
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python 环境不存在或不可执行: $PYTHON_BIN" >&2
  exit 1
fi

cd "$PROJECT_ROOT"
mkdir -p "$RUNTIME_DIR"
if ! pgrep -x redis-server >/dev/null; then
  "$REDIS_BIN" \
    --bind 127.0.0.1 \
    --protected-mode yes \
    --appendonly yes \
    --dir "$RUNTIME_DIR" \
    --dbfilename dump.rdb \
    --daemonize yes
fi

for attempt in $(seq 1 10); do
  if "$PYTHON_BIN" scripts/enable_redis_auth.py; then
    echo "redis ready"
    exit 0
  fi
  sleep 1
done

echo "Redis 启动或认证验证失败" >&2
exit 1
