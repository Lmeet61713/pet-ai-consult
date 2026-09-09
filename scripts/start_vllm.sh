#!/usr/bin/env bash
set -euo pipefail

VLLM_BIN="${VLLM_BIN:-/root/autodl-tmp/conda/pet-mm/bin/vllm}"
MODEL_PATH="${VLLM_MODEL_PATH:-/root/autodl-tmp/models/Qwen3.5-9B}"
HOST="${VLLM_HOST:-127.0.0.1}"
PORT="${VLLM_PORT:-8101}"
ENFORCE_EAGER="${VLLM_ENFORCE_EAGER:-1}"

# The installed FlashInfer build rejects this host's SM120 during sampler
# initialization; vLLM's native PyTorch sampler is the supported fallback.
export VLLM_USE_FLASHINFER_SAMPLER="${VLLM_USE_FLASHINFER_SAMPLER:-0}"

VLLM_ARGS=(
  serve "$MODEL_PATH"
  --host "$HOST"
  --port "$PORT"
  --served-model-name "${VLLM_MODEL_NAME:-Qwen3.5-9B}"
  --max-model-len "${VLLM_MAX_MODEL_LEN:-8192}"
  --gpu-memory-utilization "${VLLM_GPU_MEMORY_UTILIZATION:-0.70}"
  --max-num-seqs "${VLLM_MAX_NUM_SEQS:-4}"
  --scheduling-policy "${VLLM_SCHEDULING_POLICY:-priority}"
  --limit-mm-per-prompt '{"image": 1}'
)
if [[ "$ENFORCE_EAGER" == "1" ]]; then
  VLLM_ARGS+=(--enforce-eager)
fi
# 可选量化(如 fp8): 传 VLLM_QUANTIZATION=fp8 启用
if [[ -n "${VLLM_QUANTIZATION:-}" ]]; then
  VLLM_ARGS+=(--quantization "${VLLM_QUANTIZATION}")
fi
# 可选投机解码(如 MTP): 传 VLLM_SPECULATIVE_CONFIG='{"method":"qwen3_5_mtp","num_speculative_tokens":4}' 启用
if [[ -n "${VLLM_SPECULATIVE_CONFIG:-}" ]]; then
  VLLM_ARGS+=(--speculative-config "${VLLM_SPECULATIVE_CONFIG}")
fi

exec "$VLLM_BIN" "${VLLM_ARGS[@]}"