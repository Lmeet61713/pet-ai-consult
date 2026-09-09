#!/usr/bin/env bash
set -euo pipefail

NGINX_BIN=/root/autodl-tmp/tools/nginx/sbin/nginx
NGINX_CONFIG=/root/autodl-tmp/projects/pet-consult/configs/nginx.conf
NGINX_RUNTIME=/root/autodl-tmp/projects/pet-consult/runtime/nginx
NGINX_PID="$NGINX_RUNTIME/nginx.pid"
NGINX_CLIENT_BODY_TEMP="${NGINX_CLIENT_BODY_TEMP:-/tmp/pet-consult-nginx-client-body}"
GATEWAY_HEALTH_URLS="${GATEWAY_HEALTH_URLS:-http://127.0.0.1:6006/health/live http://127.0.0.1:6008/health/live}"

mkdir -p "$NGINX_RUNTIME"
install -d -m 0700 -o nobody -g nogroup "$NGINX_CLIENT_BODY_TEMP"
"$NGINX_BIN" -t -c "$NGINX_CONFIG"

if [[ -s "$NGINX_PID" ]] && kill -0 "$(<"$NGINX_PID")" 2>/dev/null; then
    "$NGINX_BIN" -s reload -c "$NGINX_CONFIG"
    echo "nginx reloaded"
else
    "$NGINX_BIN" -c "$NGINX_CONFIG"
    echo "nginx started"
fi

for url in $GATEWAY_HEALTH_URLS; do
    code=""
    for attempt in $(seq 1 10); do
        code="$(curl -sS -o /dev/null -w "%{http_code}" --max-time 3 "$url" || true)"
        if [[ "$code" == "200" ]]; then
            echo "nginx gateway ready: $url"
            break
        fi
        sleep 1
    done
    if [[ "$code" != "200" ]]; then
        echo "nginx gateway health check failed: $url (HTTP $code)" >&2
        exit 1
    fi
done
