# Resource-Bounded CTF Agents — CSE 291A (Option 7)

A fork of the [NYU CTF Bench](https://nyu-llm-ctf.github.io) agent harness used to study how a
**small, self-hosted** LLM solves CTF challenges under a fixed step and token budget, and what
limits it. The agent operates in a Docker environment, interacts with CTF challenges, and is
evaluated on the NYU CTF Bench dataset.

**Team:** Jackie Liu, Benjamin Yu, Steven Zhang

## What's in this fork

- **Self-hosted baseline.** Qwen2.5-7B-Instruct-AWQ served with vLLM on a single AWS g6.xlarge
  (NVIDIA L4, 24 GB), 32k context — no hosted API.
- **Instrumented harness.** Per-attempt logging of rounds, wall/model/tool time, tokens, context
  growth, invalid commands, repeated actions, truncation, and finish reasons.
- **Independent scoring.** `analysis/score.py` re-scores each attempt outside the harness against
  the answer key, checks flag origin (earned vs. possibly memorized), and flags suspicious access
  (dataset metadata, `challenge.json`, Docker socket, network).
- **Reproducible experiments.** Fresh container per attempt, deterministic seeds, pinned model /
  vLLM / dataset versions; no failed run or timeout silently dropped.

## Layout

| Path | Contents |
|---|---|
| `llm_ctf/` | Agent harness (conversation loop, tools, formatters) |
| `config/` | Agent configs (`qwen_baseline.yaml`, `base_config.yaml`) |
| `experiments/phase1/` | Batch runner, challenge set, Qwen config, AWS usage log |
| `analysis/` | Scoring, metrics, figures, trace inspection |
| `results/phase1/`, `results/phase2/` | Scored CSVs and logs |
| `report/phase1/` | Phase 1 report (`report.md`, `report.pdf`, build script) |
| `docs/local-setup.md` | Local development setup (GPU + vLLM + Docker) |
| `scripts/` | vLLM launch, AWS helpers, log/HTML utilities |

## Setup

See [`docs/local-setup.md`](docs/local-setup.md) for the full local recipe (vLLM, Docker image and
network, dataset download). In brief:

1. Install Docker and Python 3.11 (a virtualenv or conda env is recommended).
2. Install dependencies: `python3 -m pip install -r requirements.txt`
3. Build the Docker image and network: `./setup.sh`
4. Download the dataset: `python3 -m nyuctf.download`
5. Serve the model: `scripts/start-vllm.sh`

## Usage

Solve a single challenge:

```bash
python3 llm_ctf_solve.py -c config/qwen_baseline.yaml --split <test|development> --challenge <challenge-name>
```

Run a Phase 1 batch (multiple attempts per challenge) and score it:

```bash
python3 experiments/phase1/run_batch.py --set {dev,heldout} --attempts 3 --config experiments/phase1/qwen_phase1.yaml
python3 analysis/score.py logs/phase1 <dataset_dir> -o results/phase1/attempts.csv
```

## Report

The Phase 1 baseline write-up is in [`report/phase1/report.md`](report/phase1/report.md); rebuild
the styled PDF with `cd report/phase1 && ./build-pdf.sh` (needs `pandoc` + Chrome).

## Acknowledgements

Built on the [NYU CTF Bench](https://github.com/NYU-LLM-CTF/llm_ctf_automation) agent framework.
See `LICENSE` for upstream licensing.
