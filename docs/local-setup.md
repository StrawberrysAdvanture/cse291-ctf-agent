# Local Setup — CSE 291 CTF Agent Baseline

This guide reproduces our initial development setup: the original NYU CTF Agents harness, a locally served **Qwen2.5-7B-Instruct-AWQ** model, and a Docker-based challenge environment. The goal is to establish a working baseline **before** modifying agent strategies or adding research features.

> **Status (October 2026):** The vLLM API, harness CLI, Docker image/network, and development dataset loading have been verified on one machine. A full agent-to-tool-to-observation challenge run has **not yet** been verified. This is a local-development recipe, not a guarantee that every teammate's hardware will behave identically.

## 1. Prerequisites

- Linux with Docker and permission to run `docker` commands.
- Python **3.11** and `venv` support.
- NVIDIA GPU with a compatible driver and sufficient VRAM for the chosen model. Our tested machine uses an **RTX 4070 Ti SUPER (16 GB)** and NVIDIA driver **560.35.03**.
- Git and access to the team's GitHub repository.
- Enough disk space for Python dependencies, model weights, Docker images, and the NYU CTF dataset (the dataset clone alone was approximately **1.34 GiB** of transferred data).

Our working model-serving versions are `vllm==0.7.3`, `torch==2.5.1+cu124`, and `transformers==4.49.0`. They are a **known-working combination on our machine**, not universal requirements. We did **not** upgrade the GPU driver.

## 2. Clone the repository and create the directories

Replace `<TEAM_REPO_URL>` with the URL of the team's fork.

```bash
mkdir -p ~/Documents/UCSD/CSE291/project
cd ~/Documents/UCSD/CSE291/project
git clone <TEAM_REPO_URL> nyuctf_agents
cd nyuctf_agents
```

The commands below assume this layout:

```text
project/
├── nyuctf_agents/        # Git-tracked source and configuration
├── ctf-env/              # Local-only agent Python environment
└── vllm-env/             # Local-only model-serving Python environment
```

**Do not commit** virtual environments, model downloads, credentials, experiment logs, or benchmark datasets.

## 3. Create the agent environment

From `project/`:

```bash
cd ~/Documents/UCSD/CSE291/project
python3.11 -m venv ctf-env
source ctf-env/bin/activate
python -m pip install --upgrade pip
python -m pip install -r nyuctf_agents/requirements.txt
cd nyuctf_agents
python llm_ctf_solve.py --help
```

The help output should list `--backend vllm` and the model `Qwen/Qwen2.5-7B-Instruct`. If the model is missing, verify that you checked out the team's Qwen baseline branch.

### Configure the local endpoint

Create `nyuctf_agents/keys.cfg`:

```bash
printf 'MODEL_URL: http://127.0.0.1:8000/v1\n' > keys.cfg
```

The repository's `parse_keys()` function reads `KEY: VALUE` lines. Our baseline patch changes `split(":")` to `split(":", 1)` so URLs containing colons are parsed correctly. The `keys.cfg` file is ignored by Git and must **never** contain committed credentials.

Verify:

```bash
python -c 'from llm_ctf.backends.utils import KEYS; print(KEYS["MODEL_URL"])'
```

Expected: `http://127.0.0.1:8000/v1`.

## 4. Create the vLLM environment

Use a **separate terminal** for model serving:

```bash
cd ~/Documents/UCSD/CSE291/project
python3.11 -m venv vllm-env
source vllm-env/bin/activate
python -m pip install --upgrade pip
python -m pip install 'vllm==0.7.3' 'transformers==4.49.0'
python -m pip check
```

On the tested machine, this installed PyTorch `2.5.1+cu124`. Check your actual installation:

```bash
python -c 'import torch, vllm, transformers; print(torch.__version__, vllm.__version__, transformers.__version__); print(torch.cuda.is_available())'
```

**CUDA library-path issue:** On our Ubuntu machine, `LD_LIBRARY_PATH` pointed at the system CUDA 12.1 libraries and caused a PyTorch import error involving `__nvJitLinkComplete_12_4`. The working solution was to unset `LD_LIBRARY_PATH` for the model-serving process, allowing the environment's CUDA libraries to load. Do not blindly modify system CUDA or upgrade drivers to address this specific symptom.

## 5. Start the local Qwen model server

In the **vLLM terminal**:

```bash
cd ~/Documents/UCSD/CSE291/project
source vllm-env/bin/activate
unset LD_LIBRARY_PATH

vllm serve Qwen/Qwen2.5-7B-Instruct-AWQ \
  --served-model-name Qwen/Qwen2.5-7B-Instruct \
  --quantization awq \
  --dtype half \
  --max-model-len 4096 \
  --gpu-memory-utilization 0.80 \
  --host 127.0.0.1 \
  --port 8000
```

Keep this terminal running. The AWQ checkpoint is quantized to fit the tested 16 GB GPU; it is **not** the unquantized model. The served name intentionally matches the model ID registered in the NYU harness.

From another terminal, verify the API:

```bash
curl http://127.0.0.1:8000/v1/models
```

The response should include `Qwen/Qwen2.5-7B-Instruct`.

**Networking note:** `127.0.0.1` is accessible to the agent when the agent runs directly on the same host. If a teammate runs the agent itself inside a container or on a different machine, this endpoint must be adapted securely; do not expose the inference server publicly by default.

## 6. Build the challenge Docker environment

From the repository root:

```bash
cd ~/Documents/UCSD/CSE291/project/nyuctf_agents
docker build --build-arg HOST_UID="$(id -u)" -t ctfenv .
docker network inspect ctfnet >/dev/null 2>&1 || docker network create ctfnet
```

Verify:

```bash
docker image inspect ctfenv >/dev/null && echo 'Image ready'
docker network inspect ctfnet >/dev/null && echo 'Network ready'
docker run --rm --network ctfnet ctfenv \
  bash -lc 'echo "CTF container working"; whoami; pwd'
```

On our machine, the test printed `ctfplayer` and `/home/ctfplayer`. The upstream `setup.sh` may perform additional setup, including a Ghidra download, for challenges that need those tools. Follow the upstream instructions if a selected challenge requires additional components.

**Isolation:** Use only approved benchmark challenges and local test services. Do not point the agent at public or third-party targets without authorization.

## 7. Download and inspect the development dataset

Activate `ctf-env` and run:

```bash
cd ~/Documents/UCSD/CSE291/project
source ctf-env/bin/activate
cd nyuctf_agents

python - <<'PY'
from nyuctf.dataset import CTFDataset

dataset = CTFDataset(split='development')
print('Development challenges:', len(dataset))
for name, metadata in dataset.all():
    print(name, metadata['category'], metadata['challenge'])
PY
```

The tested `v20250206` dataset has **57 development challenges** and is downloaded automatically under `~/.nyuctf/v20250206/` on first use.

**Dataset API gotcha:** `CTFDataset` subclasses `dict`, but stores records in `self.dataset`. Therefore `dataset.keys()` and `dataset.items()` may appear empty even when `len(dataset)` is 57. Use **`dataset.all()`** to iterate and **`dataset.get(challenge_id)`** to retrieve metadata.

The benchmark tree may contain reference solutions (`solution.py`, etc.). Do **not** expose those files to the agent or use them as hints. Confirm what the harness mounts into the agent's container before treating a run as valid evaluation.

## 8. Check the baseline configuration

The tracked file `config/qwen_baseline.yaml` should retain the upstream prompts and contain:

```yaml
parameter:
  max_rounds: 3
  max_cost: 3
  backend: "vllm"
  model: "Qwen/Qwen2.5-7B-Instruct"
```

The initial integration patch is deliberately small: register the model in `llm_ctf/backends/vllm_backend.py`, fix the colon parser in `llm_ctf/backends/utils.py`, and add this YAML configuration. No agent strategy or prompt changes are intended in this baseline.

## 9. First end-to-end run (not yet verified)

After confirming the vLLM server is running, the Docker environment is ready, and the selected development challenge's service requirements are satisfied, run **one instructor-approved development challenge**. For example, `2016q-cry-sleeping_guard` is present in the development dataset, but may require challenge-specific setup:

```bash
python llm_ctf_solve.py \
  --challenge 2016q-cry-sleeping_guard \
  --split development \
  --config config/qwen_baseline.yaml \
  --backend vllm \
  --model Qwen/Qwen2.5-7B-Instruct \
  --max-rounds 3
```

This command is a **planned smoke test**, not a verified successful execution. First check the selected challenge's `challenge.json`, `README.md`, and service configuration. Avoid opening solution files.

For the first trace, check whether the agent (1) receives the challenge, (2) emits XML tool calls, (3) executes commands in the Docker environment, and (4) incorporates tool observations. Solving the challenge is secondary to validating the full loop.

## 10. Reproducibility and team workflow

- Work on feature branches and open pull requests. Preserve `baseline-local-qwen` as the reference baseline until the team agrees to change it.
- Record the upstream NYU harness revision, dataset version (`v20250206`), model checkpoint, package versions, configuration, and hardware for each experiment.
- Log outcomes, wall-clock time, token usage when available, tool calls, invalid/repeated actions, and failure reasons. A three-round smoke test is **not** a statistically meaningful benchmark.
- Keep generated logs and benchmark data out of Git unless the team explicitly curates safe, compact artifacts for sharing.
- Avoid making modifications to the original prompts, execution limits, or tool behavior without recording them as experimental variables.

### Quick troubleshooting

| Symptom | Check |
|---|---|
| `FileNotFoundError: keys.cfg` | Create the ignored `keys.cfg` in the repository root. |
| `MODEL_URL` prints only `http` | Ensure `utils.py` uses `line.split(":", 1)`. |
| PyTorch `__nvJitLinkComplete_12_4` error | Unset `LD_LIBRARY_PATH` in the vLLM environment. |
| Model missing from `--help` | Check the Qwen entry in `VLLMBackend.MODELS` and your Git branch. |
| Connection refused on port 8000 | Start vLLM; check `curl http://127.0.0.1:8000/v1/models`. |
| `dataset.keys()` returns zero | Use `dataset.all()`; records are stored in `self.dataset`. |
| Docker image/network missing | Build `ctfenv` and create `ctfnet` as above. |

