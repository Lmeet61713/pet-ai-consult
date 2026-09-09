#!/usr/bin/env bash
set -euo pipefail

if (( $# < 3 )); then
  echo "usage: $0 image-a.jpg image-b.jpg image-c.jpg [base-url] [output-dir]" >&2
  exit 2
fi

V73_IMAGE_A="$1"
V73_IMAGE_B="$2"
V73_IMAGE_C="$3"
V73_BASE_URL="${4:-http://127.0.0.1:18100}"
V73_MATRIX_DIR="${5:-runtime/v73-matrix-$(date +%Y%m%d-%H%M%S)}"
V73_PYTHON_BIN="${V73_PYTHON_BIN:-python3}"

for V73_IMAGE in "$V73_IMAGE_A" "$V73_IMAGE_B" "$V73_IMAGE_C"; do
  [[ -f "$V73_IMAGE" ]] || { echo "missing image: $V73_IMAGE" >&2; exit 2; }
done
mkdir -p "$V73_MATRIX_DIR"

V73_COMMON=(--base "$V73_BASE_URL" --per 24 --timeout 120)

"$V73_PYTHON_BIN" scripts/load_matrix.py "${V73_COMMON[@]}" \
  --workload text --concurrency 1,2,4,6,8,12 \
  --json-out "$V73_MATRIX_DIR/text-control.json"

"$V73_PYTHON_BIN" scripts/load_matrix.py "${V73_COMMON[@]}" \
  --workload single-image --cache-mode cold --image "$V73_IMAGE_A" \
  --concurrency 1,2,4,6,8,12 \
  --json-out "$V73_MATRIX_DIR/cold-single-image.json"

"$V73_PYTHON_BIN" scripts/load_matrix.py "${V73_COMMON[@]}" \
  --workload triple-image --cache-mode cold \
  --image "$V73_IMAGE_A" --image "$V73_IMAGE_B" --image "$V73_IMAGE_C" \
  --concurrency 1,2,3,4 \
  --json-out "$V73_MATRIX_DIR/cold-triple-image.json"

"$V73_PYTHON_BIN" scripts/load_matrix.py "${V73_COMMON[@]}" \
  --workload single-image --cache-mode warm --image "$V73_IMAGE_A" \
  --concurrency 1,4,8,12 \
  --json-out "$V73_MATRIX_DIR/warm-single-image.json"

for V73_RATIO in 10 20; do
  "$V73_PYTHON_BIN" scripts/load_matrix.py "${V73_COMMON[@]}" \
    --workload mixed --cache-mode cold --image-percent "$V73_RATIO" \
    --image "$V73_IMAGE_A" --concurrency 20,24,32 \
    --json-out "$V73_MATRIX_DIR/mixed-image-${V73_RATIO}.json"
done

"$V73_PYTHON_BIN" scripts/load_matrix.py "${V73_COMMON[@]}" \
  --workload mixed --cache-mode cold --image-percent 50 \
  --image "$V73_IMAGE_A" --concurrency 12,16,20 \
  --json-out "$V73_MATRIX_DIR/mixed-image-50.json"

echo "matrix results saved to $V73_MATRIX_DIR"
