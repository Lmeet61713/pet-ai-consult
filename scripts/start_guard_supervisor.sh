#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -n "${SUPERVISORD_BIN:-}" ]]; then
    SUPERVISORD_BIN="$SUPERVISORD_BIN"
elif [[ -x /root/autodl-tmp/envs/pet-mm/bin/supervisord ]]; then
    SUPERVISORD_BIN=/root/autodl-tmp/envs/pet-mm/bin/supervisord
else
    SUPERVISORD_BIN="$(command -v supervisord || true)"
fi

if [[ -z "$SUPERVISORD_BIN" ]]; then
    echo "supervisord is required to run the Guard service" >&2
    exit 1
fi

mkdir -p "$PROJECT_ROOT/runtime/guard"
export PET_CONSULT_ROOT="$PROJECT_ROOT"
exec "$SUPERVISORD_BIN" -c "$PROJECT_ROOT/configs/guard-supervisord.conf"
