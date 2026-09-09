#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_ROOT/.venv/bin/python}"
IMAGE_PATH="${SYNTHETIC_IMAGE_PATH:-/tmp/pet-consult-synthetic.png}"
API_LOG="$PROJECT_ROOT/runtime/api-synthetic-acceptance.log"

cd "$PROJECT_ROOT"
mkdir -p runtime

"$PYTHON_BIN" - "$IMAGE_PATH" <<'PY'
import sys

from PIL import Image, ImageDraw

path = sys.argv[1]
image = Image.new("RGB", (256, 256), "white")
draw = ImageDraw.Draw(image)
draw.polygon([(55, 75), (30, 20), (100, 65)], fill=(165, 105, 60))
draw.polygon([(201, 75), (226, 20), (156, 65)], fill=(165, 105, 60))
draw.ellipse((45, 45, 211, 220), fill=(210, 155, 95), outline=(70, 45, 30), width=4)
draw.ellipse((85, 100, 105, 120), fill="black")
draw.ellipse((151, 100, 171, 120), fill="black")
draw.ellipse((103, 125, 153, 180), fill=(235, 205, 165))
draw.ellipse((119, 140, 137, 154), fill="black")
draw.arc((105, 145, 151, 185), 0, 180, fill=(70, 45, 30), width=3)
image.save(path, "PNG")
print(f"synthetic_image={path} size={image.width}x{image.height}")
PY

export JWT_SIGNING_SECRET="$(openssl rand -hex 32)"
export LOG_HASH_SECRET="$(openssl rand -hex 32)"

bash scripts/start_api.sh >"$API_LOG" 2>&1 &
api_pid=$!
cleanup() {
  kill "$api_pid" 2>/dev/null || true
  wait "$api_pid" 2>/dev/null || true
}
trap cleanup EXIT

live_code="000"
for attempt in $(seq 1 20); do
  live_code="$(
    curl -sS -o /dev/null -w "%{http_code}" --max-time 3 \
  http://127.0.0.1:18100/health/live || true
  )"
  echo "api_attempt=$attempt live=$live_code"
  [[ "$live_code" == "200" ]] && break
  sleep 1
done

if [[ "$live_code" != "200" ]]; then
  tail -n 50 "$API_LOG"
  exit 1
fi

AUTODL_TEST_JWT="$($PYTHON_BIN - <<'PY'
import os
import time

import jwt

payload = {
    "sub": "synthetic-test-user",
    "tenant_id": "synthetic-test-tenant",
    "exp": int(time.time()) + 3600,
    "iss": "business-auth",
    "aud": "pet-consult",
    "scope": ["pet:consult"],
}
print(jwt.encode(payload, os.environ["JWT_SIGNING_SECRET"], algorithm="HS256"))
PY
)"
export AUTODL_TEST_JWT

set +e
"$PYTHON_BIN" scripts/autodl_acceptance.py \
  --base http://127.0.0.1:18100 \
  --image "$IMAGE_PATH" \
  --text "这是无隐私的合成卡通宠物测试数据，仅用于验证问诊链路。请给出一般观察建议。"
result=$?
set -e

if [[ "$result" -ne 0 ]]; then
  echo "API_LOG_TAIL"
  tail -n 50 "$API_LOG"
fi
exit "$result"
