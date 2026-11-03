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
    # Qwen2.5-7B's full context. Its AWQ weights take ~5.5 GB, so a 24 GB GPU has >14 GB
    # of KV cache for it. GPUs under 20 GB (T4) keep the local baseline's 4096.
    if [ "$gpu_mib" -ge 20000 ]; then
        MAX_MODEL_LEN=32768
    else
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

# Download the dataset now (while the model loads) so that listing challenges later
# does not trigger a download whose git output would be mistaken for challenge names.
set_status "SETUP downloading the NYU CTF dataset"
python -m nyuctf.download

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

challenge_file="$OUT/challenges.txt"
if [ "$CHALLENGES" = "all" ]; then
    # Names are written to a file by Python itself, so nothing printed by the
    # dataset code can end up in the list.
    python - "$SPLIT" "$challenge_file" <<'PY'
import sys
from nyuctf.dataset import CTFDataset
names = [name for name, _ in CTFDataset(split=sys.argv[1]).all()]
with open(sys.argv[2], "w") as f:
    f.write("\n".join(names) + "\n")
PY
else
    echo "$CHALLENGES" | tr ',' '\n' | grep . > "$challenge_file"
fi

total=$(grep -c . "$challenge_file")
echo "Challenges to run ($total): $(paste -sd' ' "$challenge_file")"
n=0; crashed=0
while IFS= read -r chal; do
    [ -n "$chal" ] || continue
    n=$((n + 1))
    set_status "RUNNING challenge $n/$total: $chal"
    if ! python llm_ctf_solve.py --challenge "$chal" --split "$SPLIT" \
            --config "$RUN_CONFIG" -L "$LOG_DIR"; then
        echo "!!! llm_ctf_solve.py exited with an error on $chal"
        crashed=$((crashed + 1))
    fi
done < "$challenge_file"

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
