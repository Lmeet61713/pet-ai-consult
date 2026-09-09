#!/usr/bin/env bash
set -euo pipefail

# AutoDL native one-command startup. Docker is intentionally not required:
# the image services use AutoDL's host CUDA/Conda environment.
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="$PROJECT_ROOT/runtime"
SUPERVISORCTL_BIN="${SUPERVISORCTL_BIN:-/root/autodl-tmp/envs/pet-mm/bin/supervisorctl}"
START_NGINX="${START_NGINX:-0}"
GUARD_MODE="${GUARD_MODE:-off}"

case "$GUARD_MODE" in
  off|shadow|enforce) ;;
  *)
    echo "GUARD_MODE 必须是 off、shadow 或 enforce，当前为: $GUARD_MODE" >&2
    exit 2
    ;;
esac

cd "$PROJECT_ROOT"
mkdir -p "$RUNTIME_DIR"

wait_for_http() {
  local name="$1"
  local url="$2"
  local attempts="$3"
  local code
  for attempt in $(seq 1 "$attempts"); do
    code="$(curl -sS -o /dev/null -w "%{http_code}" --max-time 5 "$url" || true)"
    if [[ "$code" == "200" ]]; then
      echo "$name ready (attempt $attempt)"
      return 0
    fi
    sleep 2
  done
  echo "$name 未在预期时间内就绪，最后 HTTP $code" >&2
  return 1
}

supervisor_running() {
  local config="$1"
  [[ -x "$SUPERVISORCTL_BIN" ]] && PET_CONSULT_ROOT="$PROJECT_ROOT" \
    "$SUPERVISORCTL_BIN" -c "$config" status >/dev/null 2>&1
}

start_supervisor() {
  local name="$1"
  local script="$2"
  local config="$3"
  local log="$4"
  if supervisor_running "$config"; then
    echo "$name Supervisor already running"
    return 0
  fi
  nohup bash "$script" >"$log" 2>&1 < /dev/null &
  echo $! >"$RUNTIME_DIR/$name-supervisor.pid"
  sleep 1
  supervisor_running "$config"
}

echo "[1/5] Starting Redis"
bash scripts/start_redis.sh

if [[ "$GUARD_MODE" == "off" ]]; then
  echo "[2/5] Guard skipped (GUARD_MODE=off)"
else
  echo "[2/5] Starting Guard (mode=$GUARD_MODE)"
  start_supervisor \
    guard \
    scripts/start_guard_supervisor.sh \
    "$PROJECT_ROOT/configs/guard-supervisord.conf" \
    "$RUNTIME_DIR/guard-supervisor.launch.log"
  wait_for_http "Guard" "http://127.0.0.1:8103/health" 30
fi

echo "[3/5] Starting vLLM and VisionGateway"
start_supervisor \
  vision \
  scripts/start_vision_supervisor.sh \
  "$PROJECT_ROOT/configs/vision-supervisord.conf" \
  "$RUNTIME_DIR/vision-supervisor.launch.log"
wait_for_http "vLLM" "http://127.0.0.1:8101/v1/models" 90
wait_for_http "VisionGateway" "http://127.0.0.1:8102/health" 30

if [[ "${VISION_WARMUP:-1}" == "1" ]]; then
  echo "[3/5] Warming VisionGateway with production request shape"
  WARMUP_PYTHON="${VISION_WARMUP_PYTHON:-$PROJECT_ROOT/.venv/bin/python}"
  if [[ ! -x "$WARMUP_PYTHON" ]]; then
    echo "Vision warmup Python 不存在或不可执行: $WARMUP_PYTHON" >&2
    exit 1
  fi
  VISION_TIMEOUT_SECONDS="${VISION_WARMUP_TIMEOUT_SECONDS:-60}" \
    "$WARMUP_PYTHON" scripts/warmup_vision.py
else
  echo "[3/5] Vision warmup skipped (VISION_WARMUP=0)"
fi

echo "[4/5] Starting API"
if ! curl -sS -o /dev/null --max-time 3 http://127.0.0.1:18100/health/live; then
  nohup bash scripts/start_api.sh >"$RUNTIME_DIR/api.stdout.log" \
    2>"$RUNTIME_DIR/api.stderr.log" < /dev/null &
  echo $! >"$RUNTIME_DIR/api.pid"
fi
wait_for_http "API live" "http://127.0.0.1:18100/health/live" 30
wait_for_http "API ready" "http://127.0.0.1:18100/health/ready" 30

if [[ "$START_NGINX" == "1" ]]; then
  echo "[5/5] Starting Nginx"
  bash scripts/start_nginx.sh
else
  echo "[5/5] Nginx skipped (set START_NGINX=1 only after HTTPS/network validation)"
fi

echo "All requested services are ready"
bash scripts/status_all.sh
