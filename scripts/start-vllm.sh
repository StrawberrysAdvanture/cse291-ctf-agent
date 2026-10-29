#!/usr/bin/env bash
set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

source "$PROJECT_ROOT/vllm-env/bin/activate"

unset LD_LIBRARY_PATH

exec vllm serve Qwen/Qwen2.5-7B-Instruct-AWQ \
  --served-model-name Qwen/Qwen2.5-7B-Instruct \
  --quantization awq \
  --dtype half \
  --max-model-len 4096 \
  --gpu-memory-utilization 0.80 \
  --host 127.0.0.1 \
  --port 8000
