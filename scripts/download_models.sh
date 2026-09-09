#!/bin/bash
# 模型一键下载（v1.4 部署包：新服务器首次部署执行，幂等可重跑）
# 源：ModelScope 国内源（快）；目标：/data/models（NVMe 持久化盘，重启不重下）
# 用法：MODEL_DIR=/data/models bash scripts/download_models.sh
set -euo pipefail

MODEL_DIR="${MODEL_DIR:-/data/models}"
mkdir -p "$MODEL_DIR"

download() {
  local name="$1" repo="$2"
  if [ -d "$MODEL_DIR/$name" ] && [ -n "$(ls -A "$MODEL_DIR/$name" 2>/dev/null)" ]; then
    echo "[skip] $name 已存在"
    return
  fi
  echo "[download] $name <- $repo"
  if command -v modelscope >/dev/null 2>&1; then
    modelscope download --model "$repo" --local_dir "$MODEL_DIR/$name"
  else
    pip install -q modelscope && modelscope download --model "$repo" --local_dir "$MODEL_DIR/$name"
  fi
}

# 视觉模型（卡1，4B，实测 1.5-2.5s/张）
download Qwen3.5-4B Qwen/Qwen3.5-4B
# 文本生成模型（卡2，9B；DeepSeek 过渡期可跳过）
download Qwen3.5-9B Qwen/Qwen3.5-9B
# 检索嵌入（CPU 运行）
download bge-m3 BAAI/bge-m3
# 审核 Guard（归审核 GPU）
download Qwen3Guard-Gen-0.6B Qwen/Qwen3Guard-Gen-0.6B

echo "=== 下载完成，目录：$MODEL_DIR ==="
du -sh "$MODEL_DIR"/* 2>/dev/null