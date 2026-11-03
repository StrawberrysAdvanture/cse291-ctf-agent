#!/usr/bin/env bash
# Serve the baseline model. Default settings match model_server in
# experiments/phase1/qwen_phase1.yaml (the frozen Phase 1 baseline).
#
# Context window and rope scaling are overridable via env vars, so the same
# script serves the Phase 2 context-window experiment without a second copy.
# Defaults reproduce the Phase 1 baseline byte-for-byte:
#   MAX_MODEL_LEN=32768, no rope scaling.
# Phase 2 (64k) is served with:
#   MAX_MODEL_LEN=65536 \
#   ROPE_SCALING='{"rope_type":"yarn","factor":2.0,"original_max_position_embeddings":32768}' \
#   scripts/start-vllm.sh
set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

source "$PROJECT_ROOT/vllm-env/bin/activate"

unset LD_LIBRARY_PATH

# awq_marlin needs compute capability >= 8.0 (L4, L40S, A10G); use --quantization awq on a T4
QUANTIZATION="${QUANTIZATION:-awq_marlin}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-32768}"
GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.85}"

# Optional YaRN rope scaling to extend context beyond the 32768 native window.
# Set ROPE_SCALING to a vLLM rope-scaling JSON string to enable it.
ROPE_ARGS=()
if [ -n "${ROPE_SCALING:-}" ]; then
  ROPE_ARGS=(--rope-scaling "$ROPE_SCALING")
fi

exec vllm serve Qwen/Qwen2.5-7B-Instruct-AWQ \
  --revision b25037543e9394b818fdfca67ab2a00ecc7dd641 \
  --served-model-name Qwen/Qwen2.5-7B-Instruct \
  --quantization "$QUANTIZATION" \
  --dtype half \
  --max-model-len "$MAX_MODEL_LEN" \
  --gpu-memory-utilization "$GPU_MEM_UTIL" \
  "${ROPE_ARGS[@]}" \
  --seed 0 \
  --host 127.0.0.1 \
  --port 8000
