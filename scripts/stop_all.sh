#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_ROOT/.venv/bin/python}"
SUPERVISORCTL_BIN="${SUPERVISORCTL_BIN:-/root/autodl-tmp/envs/pet-mm/bin/supervisorctl}"
GUARD_MODE="${GUARD_MODE:-off}"
NGINX_BIN="${NGINX_BIN:-/root/autodl-tmp/tools/nginx/sbin/nginx}"
NGINX_CONFIG="$PROJECT_ROOT/configs/nginx.conf"

case "$GUARD_MODE" in
  off|shadow|enforce) ;;
  *)
    echo "GUARD_MODE 必须是 off、shadow 或 enforce，当前为: $GUARD_MODE" >&2
    exit 2
    ;;
esac

cd "$PROJECT_ROOT"

stop_pid_file() {
  local name="$1"
  local pid_file="$2"
  [[ -s "$pid_file" ]] || return 0

  local pid command
  pid="$(<"$pid_file")"
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$pid_file"
    return 0
  fi
  command="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
  if [[ "$command" != *"$PROJECT_ROOT"* ]]; then
    echo "拒绝停止 $name：PID $pid 不属于项目目录" >&2
    return 1
  fi
  kill "$pid"
  wait "$pid" 2>/dev/null || true
  rm -f "$pid_file"
  echo "$name stopped"
}

wait_pid_exit() {
  local name="$1"
  local pid="$2"
  [[ -n "$pid" ]] || return 0
  for _ in $(seq 1 30); do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "$name supervisor stopped"
      return 0
    fi
    sleep 0.5
  done
  echo "$name supervisor did not exit in time (PID $pid)" >&2
  return 1
}

echo "[1/4] Stopping Nginx when managed by this project"
if [[ -x "$NGINX_BIN" && -s "$PROJECT_ROOT/runtime/nginx/nginx.pid" ]]; then
  nginx_pid="$(<"$PROJECT_ROOT/runtime/nginx/nginx.pid")"
  if kill -0 "$nginx_pid" 2>/dev/null; then
    "$NGINX_BIN" -s quit -c "$NGINX_CONFIG" || true
  fi
  rm -f "$PROJECT_ROOT/runtime/nginx/nginx.pid"
fi

echo "[2/4] Stopping API"
stop_pid_file "API" "$PROJECT_ROOT/runtime/api.pid"

echo "[3/4] Stopping vLLM and VisionGateway"
if [[ -x "$SUPERVISORCTL_BIN" ]]; then
  vision_supervisor_pid="$(cat "$PROJECT_ROOT/runtime/vision/supervisord.pid" 2>/dev/null || true)"
  guard_supervisor_pid="$(cat "$PROJECT_ROOT/runtime/guard/supervisord.pid" 2>/dev/null || true)"
  if [[ "$GUARD_MODE" != "off" ]]; then
    echo "Stopping Guard (mode=$GUARD_MODE)"
    PET_CONSULT_ROOT="$PROJECT_ROOT" "$SUPERVISORCTL_BIN" \
      -c "$PROJECT_ROOT/configs/guard-supervisord.conf" shutdown || true
  else
    echo "Guard already skipped (GUARD_MODE=off)"
  fi
  PET_CONSULT_ROOT="$PROJECT_ROOT" "$SUPERVISORCTL_BIN" \
    -c "$PROJECT_ROOT/configs/vision-supervisord.conf" shutdown || true
  if [[ "$GUARD_MODE" != "off" ]]; then
    wait_pid_exit "Guard" "$guard_supervisor_pid"
  fi
  wait_pid_exit "Vision" "$vision_supervisor_pid"
fi

echo "[4/4] Stopping Redis after saving data"
"$PYTHON_BIN" - <<'PY'
import sys

import redis

from app.core.config import Settings

settings = Settings()
client = redis.Redis.from_url(
    settings.redis_url,
    password=settings.redis_password or None,
    decode_responses=True,
)
try:
    client.ping()
except redis.ConnectionError:
    print("Redis already stopped")
    sys.exit(0)
client.shutdown(save=True)
print("Redis stopped")
PY

port_is_listening() {
  local port="$1"
  (echo >"/dev/tcp/127.0.0.1/$port") >/dev/null 2>&1
}

ports=(6006 6008 6379 8100 8101 8102)
if [[ "$GUARD_MODE" != "off" ]]; then
  ports+=(8103)
fi
for port in "${ports[@]}"; do
  stopped=0
  for attempt in $(seq 1 15); do
    if ! port_is_listening "$port"; then
      stopped=1
      break
    fi
    sleep 1
  done
  if [[ "$stopped" != "1" ]]; then
    echo "端口 $port 仍在监听" >&2
    exit 1
  fi
done

echo "All managed services stopped"
