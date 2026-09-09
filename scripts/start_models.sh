#!/usr/bin/env bash
# 本地模型栈一键启动(单卡 RTX 5090 共存): 4B 视觉(:8101) + VisionGateway(:8102) + 9B 生成 FP8(:8001)
# 依赖: vllm 0.27.1 + torch 2.13.0+cu130(2026-08-18 升级, SM120 全速)
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"
export VLLM_USE_FLASHINFER_SAMPLER=0 VLLM_ENFORCE_EAGER=0

# ===== 端口占用预检查（防止重复启动导致 address already in use）=====
PORT_CHECK_LIST=(8101 8102 8001)
for port in "${PORT_CHECK_LIST[@]}"; do
  if timeout 1 bash -c "</dev/tcp/127.0.0.1/${port}" 2>/dev/null; then
    echo "ERROR: 端口 ${port} 已被占用，服务可能已在运行。请先执行 scripts/stop_all.sh 再启动。" >&2
    exit 1
  fi
done
echo "[0/3] 端口检查通过: 8101/8102/8001 空闲"

echo '[1/3] 4B 视觉 vLLM (:8101, util 0.37)'
nohup env VLLM_MODEL_PATH=/root/autodl-tmp/models/Qwen3.5-4B VLLM_MODEL_NAME=Qwen3.5-4B \
  VLLM_PORT=8101 VLLM_GPU_MEMORY_UTILIZATION=0.37 VLLM_ENFORCE_EAGER=0 \
  bash scripts/start_vllm.sh > runtime/vllm-4b.log 2>&1 < /dev/null &
nohup env VLLM_MODEL_NAME=Qwen3.5-4B bash scripts/start_vision.sh > runtime/vision.log 2>&1 < /dev/null &

echo '[2/3] 等待 4B 就绪(避免显存探测竞争)'
for i in $(seq 1 40); do
  curl -s -m 2 http://127.0.0.1:8101/v1/models >/dev/null 2>&1 && break
  sleep 3
done

echo '[3/3] 9B 生成 vLLM FP8 (:8001, util 0.45, maxlen 4096)'
nohup env VLLM_MODEL_PATH=/root/autodl-tmp/models/Qwen3.5-9B VLLM_MODEL_NAME=Qwen3.5-9B \
  VLLM_PORT=8001 VLLM_GPU_MEMORY_UTILIZATION=0.55 VLLM_MAX_MODEL_LEN=4096 VLLM_MAX_NUM_SEQS=8 \
  VLLM_ENFORCE_EAGER=0 VLLM_QUANTIZATION=fp8 \
  bash scripts/start_vllm.sh > runtime/vllm-9b.log 2>&1 < /dev/null &

echo "模型启动中: 4B(:8101) + vision(:8102) + 9B-FP8(:8001); 9B 首次加载约 4-6 分钟(FP8 转换)"
