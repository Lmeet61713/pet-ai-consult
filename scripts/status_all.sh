#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SUPERVISORCTL_BIN="${SUPERVISORCTL_BIN:-/root/autodl-tmp/envs/pet-mm/bin/supervisorctl}"
GUARD_MODE="${GUARD_MODE:-off}"
failed=0

case "$GUARD_MODE" in
  off|shadow|enforce) ;;
  *)
    echo "GUARD_MODE 必须是 off、shadow 或 enforce，当前为: $GUARD_MODE" >&2
    exit 2
    ;;
esac

check_http() {
  local name="$1"
  local url="$2"
  local expected="${3:-200}"
  local code
  code="$(curl -sS -o /dev/null -w "%{http_code}" --max-time 5 "$url" || true)"
  if [[ "$code" == "$expected" ]]; then
    echo "PASS $name HTTP $code"
  else
    echo "FAIL $name HTTP $code" >&2
    failed=1
  fi
}

if [[ -x "$SUPERVISORCTL_BIN" ]]; then
  if [[ "$GUARD_MODE" == "off" ]]; then
    echo "SKIP Guard Supervisor (GUARD_MODE=off)"
  else
    echo "Guard Supervisor (mode=$GUARD_MODE)"
    PET_CONSULT_ROOT="$PROJECT_ROOT" "$SUPERVISORCTL_BIN" \
      -c "$PROJECT_ROOT/configs/guard-supervisord.conf" status || failed=1
  fi
  echo "Vision Supervisor"
  PET_CONSULT_ROOT="$PROJECT_ROOT" "$SUPERVISORCTL_BIN" \
    -c "$PROJECT_ROOT/configs/vision-supervisord.conf" status || failed=1
else
  echo "FAIL supervisorctl not found: $SUPERVISORCTL_BIN" >&2
  failed=1
fi

check_http "vLLM" "http://127.0.0.1:8101/v1/models"
if [[ "$GUARD_MODE" == "off" ]]; then
  echo "SKIP Guard HTTP (GUARD_MODE=off)"
else
  check_http "Guard" "http://127.0.0.1:8103/health"
fi
check_http "VisionGateway" "http://127.0.0.1:8102/health"
check_http "API live" "http://127.0.0.1:18100/health/live"
check_http "API ready" "http://127.0.0.1:18100/health/ready"

nginx_pid_file="$PROJECT_ROOT/runtime/nginx/nginx.pid"
if [[ -s "$nginx_pid_file" ]] && kill -0 "$(<"$nginx_pid_file")" 2>/dev/null; then
  check_http "Nginx gateway 6006" "http://127.0.0.1:6006/health/live"
  check_http "Nginx gateway 6008" "http://127.0.0.1:6008/health/live"
else
  echo "SKIP Nginx gateway (not running)"
fi

exit "$failed"
