<!--
Phase 1 report draft. Final submission must be a PDF, 1-2 pages + appendix.
Fill the [BRACKETED] numbers from analysis/ outputs once dev + held-out runs finish:
  results/phase1/by_set.csv, by_category.csv, attempts.csv
Repo link goes in the appendix. Sanitized traces + answer keys stay out of the public repo.
-->

# Resource-Bounded CTF Agents — Phase 1 Baseline

**Team:** [names] · **Option 7** · **Repo:** [link]

## 1. Setup

A tool-using agent (fixed model, prompt, tool set, step budget) on NYU CTF Bench.

- **Model:** Qwen2.5-7B-Instruct-AWQ, self-hosted with vLLM 0.7.3 on one AWS g6.xlarge
  (NVIDIA L4, 24 GB), 32k context, temperature 0.6, max_tokens 1024, pinned revision.
- **Agent:** NYU CTF harness. Tools: run_command, create_file, decompile, disassemble,
  check_flag, give_up. Tool calls are formatted/parsed as XML (no native tool calling).
- **Budget:** [MAX_ROUNDS] model calls and [MAX_TIME] s wall-clock per attempt.
- **Isolation:** agent container on an internal Docker network (no internet); only the
  challenge server is reachable. Answer keys are never mounted or shown to the agent.
- **Subset:** [N_DEV] development + [N_HELDOUT] held-out challenges across 6 categories,
  chosen for low point value and buildable environments (see experiments/phase1/challenges.yaml;
  exclusions documented there). [K] attempts per challenge.

## 2. Verification

Each attempt is rescored outside the harness (analysis/score.py) against the answer key
read from the dataset on the host. A solve also records flag origin: a success counts as
earned only if the flag appeared in a tool output before submission; otherwise it is flagged
as possibly memorized. [#UNEXPLAINED] of [#SOLVED] solves were earned from tool output.
No attempt accessed dataset metadata, solutions, or the network. [CROSSCHECK vs harness verdict]

## 3. Results

Solve rate, cost, and execution metrics, per set and per category.

- Held-out solve rate: [X]% of attempts; [Y]/[N_HELDOUT] challenges solved at least once.
- Development solve rate: [X]% of attempts; [Y]/[N_DEV] solved at least once.
- Cost: [$] total GPU, [mean] s and [mean] tokens per attempt.

[TABLE by_category: solve rate, mean rounds, mean tokens, invalid cmds, truncations, finish reasons]
[FIG 1: outcome distribution per challenge across attempts — show spread, not only successes]
[FIG 2: context growth (prompt tokens) vs round]

## 4. Failure analysis

Failed traces hand-labeled (analysis/failures.csv) into: missing knowledge, tool-interface,
lost findings, unproductive loop. Counts: [TABLE].

Leading bottleneck: [BOTTLENECK]. Evidence: [e.g. truncated_rounds — replies cut off at
max_tokens before emitting a tool call; mean [T] truncated rounds/attempt; on [chal] the
agent made 0 tool calls in [R] rounds]. Also prominent: [unproductive loops — mean
[I] ineffective repeats/attempt].

## 5. Phase 2 direction

[MECHANISM, justified by §4 — e.g. compact/structured tool feedback, or output budgeting to
prevent truncation; or memory of verified findings to cut repeats], compared against the
simple agent and fixed retries at equal total budget, with an ablation, on held-out challenges.

## Appendix: timeline and milestones

- Phase 1 (done): harness instrumentation, isolation, independent scoring, baseline runs,
  failure analysis. Repo: [link], commit [hash].
- [date]: Phase 2 mechanism implemented.
- [date]: held-out evaluation + ablation + retries baseline.
- Dec 10: Phase 2 code and final report.
