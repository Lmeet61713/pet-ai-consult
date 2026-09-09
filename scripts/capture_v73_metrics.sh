#!/usr/bin/env bash
set -euo pipefail

V73_DURATION_SECONDS="${1:-300}"
V73_INTERVAL_SECONDS="${2:-2}"
V73_OUTPUT_DIR="${3:-runtime/v73-metrics-$(date +%Y%m%d-%H%M%S)}"

if ! [[ "$V73_DURATION_SECONDS" =~ ^[0-9]+$ ]] || (( V73_DURATION_SECONDS < 1 )); then
  echo "duration must be a positive integer" >&2
  exit 2
fi
if ! [[ "$V73_INTERVAL_SECONDS" =~ ^[0-9]+$ ]] || (( V73_INTERVAL_SECONDS < 1 )); then
  echo "interval must be a positive integer" >&2
  exit 2
fi

mkdir -p "$V73_OUTPUT_DIR"

docker compose ps >"$V73_OUTPUT_DIR/compose-ps.txt"
docker compose config >"$V73_OUTPUT_DIR/compose-effective.yaml"

# 只导出性能相关白名单，避免把数据库、Redis、JWT 等凭据写入报告。
for V73_SERVICE in consult-api-1 consult-vision-gateway consult-vllm-vision consult-vllm-text; do
  {
    echo "service=$V73_SERVICE"
    docker compose ps -q "$V73_SERVICE" | xargs -r docker inspect \
      --format '{{range .Config.Env}}{{println .}}{{end}}' \
      | grep -E '^(CONSULT_WORKER_COUNT|QUEUE_MAX_PENDING|TEXT_MAX_ACTIVE|IMAGE_MAX_ACTIVE|IMAGE_MAX_ACTIVE_SLOTS|VISION_CONCURRENCY|VISION_MAX_QUEUE_SIZE|VISION_MAX_TOKENS|VISION_CACHE_ENABLED|TEXT_VLLM_MAX_NUM_SEQS|VISION_VLLM_MAX_NUM_SEQS|VLLM_BASE_URL|VLLM_MODEL_NAME)=' || true
    docker compose ps -q "$V73_SERVICE" | xargs -r docker inspect \
      --format 'cmd={{json .Config.Cmd}}'
  } >"$V73_OUTPUT_DIR/inspect-$V73_SERVICE.txt"
done

V73_STARTED="$(date +%s)"
echo "timestamp,index,name,utilization_gpu_pct,memory_used_mib,memory_total_mib,power_w" \
  >"$V73_OUTPUT_DIR/nvidia-smi.csv"
echo "timestamp,service,cpu_pct,memory_usage,net_io,block_io,pids" \
  >"$V73_OUTPUT_DIR/docker-stats.csv"

while (( $(date +%s) - V73_STARTED < V73_DURATION_SECONDS )); do
  V73_TS="$(date --iso-8601=seconds)"
  nvidia-smi \
    --query-gpu=index,name,utilization.gpu,memory.used,memory.total,power.draw \
    --format=csv,noheader,nounits \
    | sed "s/^/$V73_TS,/" >>"$V73_OUTPUT_DIR/nvidia-smi.csv"
  docker stats --no-stream --format \
    "$V73_TS,{{.Name}},{{.CPUPerc}},{{.MemUsage}},{{.NetIO}},{{.BlockIO}},{{.PIDs}}" \
    >>"$V73_OUTPUT_DIR/docker-stats.csv"

  for V73_SERVICE in consult-vllm-vision consult-vllm-text; do
    V73_PORT=8001
    [[ "$V73_SERVICE" == "consult-vllm-text" ]] && V73_PORT=8002
    {
      echo "# timestamp=$V73_TS service=$V73_SERVICE"
      docker compose exec -T "$V73_SERVICE" python3 -c \
        "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:${V73_PORT}/metrics', timeout=2).read().decode())" \
        2>/dev/null \
        | grep -E 'vllm:(num_requests_running|num_requests_waiting|gpu_cache_usage_perc|prompt_tokens_total|generation_tokens_total|request_prompt_tokens|request_generation_tokens)' || true
    } >>"$V73_OUTPUT_DIR/vllm-metrics.promlog"
  done
  sleep "$V73_INTERVAL_SECONDS"
done

echo "metrics saved to $V73_OUTPUT_DIR"
