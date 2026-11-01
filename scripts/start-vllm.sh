#!/usr/bin/env bash
# Serve the Phase 1 baseline model. Settings must match model_server in
# experiments/phase1/qwen_phase1.yaml.
set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

source "$PROJECT_ROOT/vllm-env/bin/activate"

unset LD_LIBRARY_PATH

# awq_marlin needs compute capability >= 8.0 (L4, A10G); use --quantization awq on a T4
QUANTIZATION="${QUANTIZATION:-awq_marlin}"

exec vllm serve Qwen/Qwen2.5-7B-Instruct-AWQ \
  --revision b25037543e9394b818fdfca67ab2a00ecc7dd641 \
  --served-model-name Qwen/Qwen2.5-7B-Instruct \
  --quantization "$QUANTIZATION" \
  --dtype half \
  --max-model-len 32768 \
  --gpu-memory-utilization 0.85 \
  --seed 0 \
  --host 127.0.0.1 \
  --port 8000
