#!/bin/bash
# Runs as the `ubuntu` user on the instance: serves the model with vLLM,
# sets up the agent environment, and runs the challenges (or JOB_CMD).
set -euo pipefail
. "$(dirname "$0")/common.sh"
REPO=/home/ubuntu/repo
cd "$REPO"

# ------------------------------------------------------------------ 1. model server
set_status "STARTING_VLLM $MODEL"
nvidia-smi
gpu_mib=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1 | tr -d ' ')
extra_args=()
if [ -z "$MAX_MODEL_LEN" ]; then
    if [ "$gpu_mib" -ge 40000 ]; then
        MAX_MODEL_LEN=32768
    else
        # Same as the local baseline. (A 32B model needs a 48 GB GPU; see docs/aws.md.)
        MAX_MODEL_LEN=4096
    fi
fi
echo "GPU memory: ${gpu_mib} MiB, max model length: $MAX_MODEL_LEN"

mkdir -p "$HOME/.cache/huggingface"
docker_env=()
[ -n "${HF_TOKEN:-}" ] && docker_env+=(-e "HF_TOKEN=$HF_TOKEN")
# Published on 127.0.0.1 only; the agent runs on this host.
docker run -d --name vllm --gpus all --ipc=host \
    -p 127.0.0.1:8000:8000 \
    -v "$HOME/.cache/huggingface:/root/.cache/huggingface" \
    "${docker_env[@]+"${docker_env[@]}"}" \
    "$VLLM_IMAGE" \
    --model "$MODEL" \
    --served-model-name "$SERVED_NAME" \
    --max-model-len "$MAX_MODEL_LEN" \
    --gpu-memory-utilization 0.90 \
    --host 0.0.0.0 --port 8000 \
    "${extra_args[@]+"${extra_args[@]}"}"

# ------------------------------------------------------------------ 2. agent environment
# (runs while vLLM downloads and loads the weights)
set_status "SETUP agent environment"
python3 -m venv "$HOME/ctf-env"
. "$HOME/ctf-env/bin/activate"
pip install -q --upgrade pip
pip install -q -r requirements.txt pyyaml

set_status "SETUP building ctfenv Docker image (slow on first run)"
./setup.sh

printf 'MODEL_URL: http://127.0.0.1:8000/v1\n' > keys.cfg

# Agent config with the model set to whatever vLLM is serving.
export RUN_CONFIG="$OUT/run_config.yaml" LOG_DIR="$OUT/logs"
mkdir -p "$LOG_DIR"
python - "$AGENT_CONFIG" "$RUN_CONFIG" "$SERVED_NAME" <<'PY'
import sys, yaml
src, dst, model = sys.argv[1:]
cfg = yaml.safe_load(open(src))
cfg.setdefault("parameter", {})
cfg["parameter"]["backend"] = "vllm"
cfg["parameter"]["model"] = model
yaml.safe_dump(cfg, open(dst, "w"), sort_keys=False)
PY

# ------------------------------------------------------------------ 3. wait for the model
set_status "WAITING_FOR_MODEL (downloading/loading weights)"
for i in $(seq 1 240); do   # up to 60 minutes
    if curl -sf http://127.0.0.1:8000/v1/models >/dev/null; then break; fi
    if [ -z "$(docker ps -q -f name=^vllm$)" ]; then
        echo "vLLM exited during startup. Last log lines:"
        docker logs --tail 60 vllm
        exit 1
    fi
    [ "$i" = 240 ] && { echo "vLLM did not become ready within 60 minutes"; docker logs --tail 60 vllm; exit 1; }
    sleep 15
done
curl -s http://127.0.0.1:8000/v1/models; echo

# ------------------------------------------------------------------ 4. run
if [ -n "$JOB_CMD" ]; then
    set_status "RUNNING custom command"
    bash -c "$JOB_CMD"
    exit $?
fi

if [ "$CHALLENGES" = "all" ]; then
    challenge_list=$(python - "$SPLIT" <<'PY'
import sys
from nyuctf.dataset import CTFDataset
for name, _ in CTFDataset(split=sys.argv[1]).all():
    print(name)
PY
)
else
    challenge_list=$(echo "$CHALLENGES" | tr ',' '\n')
fi

total=$(echo "$challenge_list" | grep -c .)
n=0; crashed=0
for chal in $challenge_list; do
    n=$((n + 1))
    set_status "RUNNING challenge $n/$total: $chal"
    if ! python llm_ctf_solve.py --challenge "$chal" --split "$SPLIT" \
            --config "$RUN_CONFIG" -L "$LOG_DIR"; then
        echo "!!! llm_ctf_solve.py exited with an error on $chal"
        crashed=$((crashed + 1))
    fi
done

echo "Finished $total challenge(s); $crashed exited with an error."
python - "$LOG_DIR" <<'PY' | tee "$OUT/summary.txt" || true
import json, sys
from pathlib import Path
rows = []
for p in sorted(Path(sys.argv[1]).rglob("*.json")):
    try:
        d = json.loads(p.read_text())
    except Exception:
        continue
    rows.append((p.stem, d.get("solved"), d.get("rounds"), d.get("finish_reason"),
                 round((d.get("runtime") or {}).get("total") or 0)))
print(f"{'challenge':45} {'solved':7} {'rounds':7} {'finish_reason':15} seconds")
for r in rows:
    print(f"{r[0]:45} {str(r[1]):7} {str(r[2]):7} {str(r[3]):15} {r[4]}")
print(f"solved {sum(1 for r in rows if r[1])}/{len(rows)}")
PY
[ "$crashed" = 0 ]
