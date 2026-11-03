# Memory baselines: three designs to decide between

**Purpose.** Reduce the designs in [`memory-literature.md`](memory-literature.md) to three memory
baselines, run each against the no-memory agent on the same challenges, and decide from the
results whether one of them is sufficient or whether a more complex design is needed.

**Selection rule.** One design per family that (a) the literature shows to be competitive,
(b) works with an untrained 7B model and a small context, (c) is implemented in this repo
(`llm_ctf/memory.py`), and (d) runs on one 24 GB GPU. Reference numbers in brackets are those of
`memory-literature.md`. All three are implemented but **untested** as of 2026-11-03; memory v1
(the running experiment) is the only memory design with a result so far.

## The three baselines

| | **M1: Masked history** | **M2: Model-written summaries** | **M3: Structured state** |
|---|---|---|---|
| Family | Sliding window with placeholders: SWE-agent [1], observation masking [3], Cybench [32], Anthropic tool-result clearing [14] | Summaries of older history written by the model: EnIGMA's LM summarizer [31], OpenHands condenser [2], ReSum [5], HackSynth [34] | Always-in-context facts plus restorable outputs: Letta memory blocks [16], Mem0 facts [20], Cybench's command log [32], VulnBot's summarizer [39], EnIGMA's file summarizer [31], Manus [15] |
| What the model sees | The task; for each old action its reasoning and exact tool call with the output replaced by `output omitted (N lines)`; the latest action in full | The task; for each old action an ≤80-word summary written by the model, which is given the challenge description; short actions verbatim; the latest action in full | As M1, plus a pinned "facts" block of exact values (flag-like strings, paths, URLs, host:port, addresses, credentials) extracted by regular expressions, and the path of every full output saved in the container |
| Extra cost | None | One model call per summarized action | None (one `docker exec` per output) |
| Strongest evidence | Within 1 point of LLM summarization at about half the cost on SWE-bench Verified [3]; collapsed history beat full history, 18.0% vs 15.0% [1] | +1.3 points over no summarizer in EnIGMA (Claude 3.5 Sonnet) [31]; compression raised Qwen3-14B on AppWorld from 25.6% to 33.9% [7] | Removing the state-tracking summarizer cut VulnBot's subtask success from 55 to 27 [39]. No CTF result for regex-extracted facts; this arm tests that idea |
| Main risk | Old outputs are gone; the agent must re-run a command to see them again | The 7B model paraphrases or drops values; untrained small models manage memory poorly [5][6]; doubles calls | Regex noise (hexdumps, `find` output); the facts budget; the agent may ignore the saved files |
| Config | `config/qwen_memory_mask.yaml` | `config/qwen_memory_llm.yaml` | `config/qwen_memory_facts.yaml` |

Shared settings: the latest action is kept verbatim with a 2,500-character cap (`raw_tail: 1`),
old entries are merged into one-line records instead of dropped (`overflow: merge`), and the
summarized part of the memory is capped at 4,000 characters. Memory options are described in
[`memory-experiments.md`](memory-experiments.md).

### Why these three and not the others

- **Memory v1** (`config/qwen_memory.yaml`: model summaries of every action, including the latest) is the design now running. It is the review's minority design; it stays as the reference point but is not a candidate.
- **`truncate`** (head and tail of each output) is a weaker form of M1's placeholder and can be added as a fourth arm if M1 wins.
- **The v2 bundle** (`config/qwen_memory_v2.yaml`) is M2 and M3 combined; run it only if both help on their own.
- **Planner/executor splits** (D-CIPHER [35]), **context folding** [8], **subgoal memory** (HiAgent [21]), **retrieval** [36][41] and **training** (MEM1 [6], Cyber-Zero [42]) are the "something better" tier: larger changes whose published gains come from stronger or trained models.

## How to decide

Run each baseline paired against the no-memory agent **B** on the same 57 development
challenges, same commit, same instance type and context (32k), and compare with
`python scripts/paired_analysis.py <B logs> <Mi logs>`.

**A memory baseline is sufficient if, against B:**

1. solve rate is not lower (McNemar p > 0.05 and at most 2 net challenges lost);
2. the repeated-command share is not higher (issue I1 in `logs/issues.md`);
3. mean prompt tokens per call are at least 40% lower;
4. the share of replies without a tool call is not higher (issue I9).

Among the baselines that qualify, take the cheapest: M1, then M3, then M2.

**Something better is needed if** every baseline fails criterion 1 or 2, in particular if the
challenges lost are ones where B's history was long (`max prompt` in the per-challenge table),
which means the memory is dropping what the model needed. The next tier, in order of effort:
repeat detection in the harness, an agent-written plan-and-status block, a separate summarizer
model, subgoal-scoped memory, and training.

**What each outcome means**

| Result | Reading |
|---|---|
| M1 ≈ B | Masking is sufficient at this horizon; summaries are not needed |
| M2 > M1 | The 7B summarizer adds value; worth tuning its prompt on failures (ACON's method [7]) |
| M3 > M1 | Exact-value memory matters; invest in extraction and a plan block rather than in summaries |
| all Mi < B | Memory costs information at 3 rounds; repeat at 10 rounds before concluding |

## A caveat about the horizon

With `max_rounds: 3` (four model calls) and a 32k context, B never reaches the context limit,
so the arms may tie on solve rate and the decision falls to cost and repeats. Memory is meant for
longer runs: at 10 to 30 rounds (the upstream `launch.sh` used 30), B's prompts grow to many
thousands of tokens, where models use the middle of a long prompt poorly [45][46][48]. The
decisive comparison is therefore **B versus the best Mi at 10 rounds**, run after the 3-round
screening. Change `max_rounds` in the configs for that run; nothing else differs.

## Run plan

The account allows two `g6.xlarge` instances at once. After the current pair (B at
`20261103-233511-6b9b`, v1 at `20261103-233532-7780`) finishes:

```bash
# Screening at 3 rounds (M1 and M3 make no extra model calls and can run together)
./aws/ctf-aws.sh launch --gpu-mem 24 --config config/qwen_memory_mask.yaml  --challenges all
./aws/ctf-aws.sh launch --gpu-mem 24 --config config/qwen_memory_facts.yaml --challenges all
# then
./aws/ctf-aws.sh launch --gpu-mem 24 --config config/qwen_memory_llm.yaml   --challenges all

# Analysis, each against the no-memory run
python scripts/paired_analysis.py logs/runs/<B>/logs logs/runs/<Mi>/logs --names baseline M1 -o logs/runs/paired-B-vs-M1.md
```

Each run takes about 30 minutes of setup plus the challenges (2–5 minutes each). Record every
run in `logs/worklog-<date>.md` and copy its outputs to `logs/runs/`.
