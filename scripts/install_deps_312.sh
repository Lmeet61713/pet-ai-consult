#!/usr/bin/env bash
# 一键：在服务器上用 Python 3.12 重建项目 venv 并安装 requirements.lock
# 背景：项目要求 Python >=3.12（见 Dockerfile.api / pyproject.toml），若 venv 建成了 3.10，
#       会导致 numpy==2.3.5 等找不到可用的 wheel、以及一堆 Requires-Python >=3.11 被忽略。
#   用法：bash scripts/install_deps_312.sh   （须在项目根目录或任意位置，脚本会自动定位）
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${VENV_DIR:-/root/.virtualenvs/pet-consult-v7.3-final-source-20260825}"

# 1) 找一个 Python 3.12（优先 conda pet-mm 里的，其次 PATH 上的 python3.12）
PY312=""
if [[ -x /root/autodl-tmp/conda/pet-mm/bin/python3.12 ]]; then
  PY312=/root/autodl-tmp/conda/pet-mm/bin/python3.12
elif command -v python3.12 >/dev/null 2>&1; then
  PY312="$(command -v python3.12)"
fi

if [[ -z "$PY312" ]]; then
  echo "[ERROR] 未找到 python3.12。请先确认是否存在："
  echo "        ls -l /root/autodl-tmp/conda/pet-mm/bin/python3.12"
  echo "        或  which python3.12"
  exit 1
fi

echo "[1/5] 使用 Python 3.12: $PY312"
"$PY312" --version

# 2) 用 3.12 重建 venv（仅重建目标 venv 目录，路径由 VENV_DIR 固定）
echo "[2/5] 重建 venv: $VENV_DIR"
if [[ "$VENV_DIR" != /root/.virtualenvs/* ]]; then
  echo "[ERROR] VENV_DIR 不合法，拒绝删除: $VENV_DIR"
  exit 1
fi
rm -rf "$VENV_DIR"
"$PY312" -m venv "$VENV_DIR"

# 3) 激活
echo "[3/5] 激活 venv"
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
python --version

# 4) 安装依赖
echo "[4/5] 安装依赖 (requirements.lock)，可能耗时较长"
cd "$PROJECT_ROOT"
pip install -r requirements.lock

# 5) 校验
echo "[5/5] 校验当前解释器："
which python
python -c "import sys; print('OK  Python', sys.version.split()[0], '@', sys.executable)"
echo "==== 完成。请确认 python --version 为 3.12.x 且路径指向 $VENV_DIR ===="

