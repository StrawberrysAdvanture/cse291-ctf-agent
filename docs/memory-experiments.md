# Memory experiments: what is implemented and what to explore next

This file records the memory options implemented after the literature review in
[`memory-literature.md`](memory-literature.md): what each option does, which papers it is taken
from (numbers in brackets are that file's reference numbers), where it lives in the code, how to
turn it on, and its status. **None of the options below has been run against a real model or
challenge yet.** They were written on 2026-11-03 and only the existing unit tests
(`tests/test_memory.py`, which exercise the v1 behaviour with all new options off) were run.

Everything is configured in the `memory` section of an experiment config and defaults to off, so
`config/qwen_memory.yaml` (memory v1) behaves as before. Two ready-made configs combine the
options:

| Config | Summarizer | Options on | Extra model calls |
|---|---|---|---|
| `config/qwen_memory.yaml` (v1) | `llm` | none | 1 per action |
| `config/qwen_memory_mask.yaml` (M1) | `mask` | raw tail, merge | none |
| `config/qwen_memory_llm.yaml` (M2) | `llm` | raw tail, long-output threshold, task-aware summarizer, merge | 1 per action longer than the threshold |
| `config/qwen_memory_facts.yaml` (M3) | `mask` | raw tail, facts, saved outputs, merge | none |
| `config/qwen_memory_v2.yaml` | `llm` | everything above | 1 per action longer than the threshold |

M1–M3 are the three memory baselines proposed for the decision in [`memory-baselines.md`](memory-baselines.md).

Compare any two runs with `python scripts/paired_analysis.py <logs A> <logs B>`; it reports paired
solve rates, finish reasons, prompt tokens, repeated commands and summary compression.

## Implemented options

### 1. Raw tail: keep the newest actions verbatim

- **Config:** `raw_tail: 1` (number of most recent actions shown in full), `raw_tail_chars: 2500` (head-and-tail cap per raw action).
- **What it does:** the newest `raw_tail` actions are rendered in full (reasoning, tool calls, outputs) under "Action N (most recent, shown in full)". An action is summarized only when it leaves the tail, so summarization is delayed by `raw_tail` steps and each run makes `raw_tail` fewer summarizer calls (the actions still in the tail when the run ends are never summarized; they are logged with `summary: null`).
- **Code:** `ActionMemory.add` / `ActionMemory.render` in `llm_ctf/memory.py`; template `llm_ctf/prompts/templates/default/memory.md.jinja2` (`raw` entries).
- **Based on:** SWE-agent keeps the last 5 observations raw [1]; the Complexity Trap study keeps the last 10 turns [3]; AgentDiet never reduces the last 2 steps [4]; Anthropic's tool-result clearing keeps the 3 most recent [14]; Cybench keeps the last 3 iterations [32]; D-CIPHER's executors keep the last 5 [35]; AutoAttacker shows the latest result next to its summary [40]. Memory v1 was in the minority (with HackSynth [34] and ReSum [5]) in summarizing even the newest output.
- **Addresses:** v1 limitation "the latest output is never seen verbatim"; issue I1 (repeated actions) if repeats follow summarized outputs.
- **Status:** implemented, untested.

### 2. `mask` summarizer: observation masking

- **Config:** `summarizer: mask` (or `--memory mask`).
- **What it does:** keeps the reasoning (shortened) and the exact tool calls; replaces each output with `output omitted (N lines, M chars)`. No model call. Deviation from pure masking: the return code, a timed-out flag and short tool *error* messages are kept because they signal that an action failed.
- **Code:** `MaskingSummarizer` in `llm_ctf/memory.py`.
- **Based on:** observation masking matched LLM summarization within 1 point on SWE-bench Verified in 4 of 5 configurations at about half the cost [3]; SWE-agent's collapsed history beat full history (18.0% vs 15.0%) [1]. EnIGMA's simple summarizer lost 2.6 points while its LM summarizer gained 1.3 [31], so the comparison is not settled for CTF.
- **Addresses:** v1 limitation "`llm` doubles the number of model calls"; gives the comparison arm needed to decide whether `llm` summaries are worth their cost on a 7B model.
- **Status:** implemented, untested.

### 3. Summarize only long actions

- **Config:** `long_output_chars: 600` (0 = off).
- **What it does:** if an action's full text (reasoning, calls, outputs) is at most this long, it is kept verbatim (logged with summarizer `verbatim`) instead of being summarized. Applies to every summarizer, so with `llm` it also saves the summarizer call for short actions.
- **Code:** `ActionMemory._summarize`.
- **Based on:** EnIGMA summarizes only outputs above a line threshold [31]; ACON compresses only above a threshold [7]; AgentDiet skips steps under 500 tokens [4]; VulnBot extracts only from results over 8,000 characters [39].
- **Addresses:** information loss on outputs that are already compact (error messages, single values).
- **Status:** implemented, untested. The 600-character default is a guess; the median action size in a memory run's log (`full_chars`) is the right place to set it from.

### 4. Saved outputs: compression that can be undone

- **Config:** `save_outputs: true`, `output_dir: ~/.agent_outputs`.
- **What it does:** when an action is recorded, each tool output (stdout, stderr, error fields) is written to `<output_dir>/step_<n>_<i>_<tool>.txt` inside the agent's container with one `docker exec`. The memory entry ends with "Full run_command output saved in the container at <path> (N lines)", and the memory block's footer tells the agent to use `head`, `tail`, `sed -n` or `grep` on those files.
- **Code:** `ContainerOutputStore` in `llm_ctf/memory.py`; the vLLM backend passes a getter for `CTFEnvironment.container`.
- **Based on:** EnIGMA's simple summarizer saves long output to a file the agent can open [31]; Manus keeps the path or URL of anything it drops [15]; Anthropic recommends lightweight identifiers with just-in-time loading [13]; MemGPT's evicted messages stay searchable [16]; HiAgent retrieves on demand [21].
- **Addresses:** v1 limitation "no retrieval".
- **Status:** implemented, untested. Costs one `docker exec` per tool result (30 s timeout; failures are logged and ignored). The files are visible to the agent's own commands, so a `ls -R ~` by the agent will show them.

### 5. Pinned facts block

- **Config:** `facts: true`, `max_facts_chars: 1200`. `flag_format` is filled automatically from the challenge by `llm_ctf_solve.py`.
- **What it does:** after each action, regular expressions extract from the tool outputs: flag-like strings (the challenge's flag format, plus a generic `name{...}` pattern), URLs, `host:port` pairs, absolute paths, hex addresses, and `password/user/key/token = value` pairs (at most 6 new values per kind per action). They are de-duplicated and rendered at the end of the memory block, just before the next-step instruction, as "Facts collected so far". No model call. When over the character budget, the oldest facts are dropped (the review suggests never evicting; a cap is still needed in a small context). Facts are also logged (`memory_facts` in the conversation log).
- **Code:** `FactExtractor`, `FactsBlock` in `llm_ctf/memory.py`.
- **Based on:** Letta's always-in-context memory blocks [16]; Mem0's explicit fact store [20]; Cybench's self-maintained log of exact commands [32]; VulnBot's summarizer that records ports, versions and shell state, whose removal had the largest effect in its ablation [39]; placement at the end follows the findings that models use the start and end of a prompt best [46][48] and Manus's practice of rewriting the plan at the end [15]. Deterministic extraction instead of asking the model follows the evidence that untrained small models manage their own memory poorly [5][6].
- **Addresses:** v1 limitation "no structured memory"; loss of exact values when the 7B summarizer paraphrases.
- **Status:** implemented, untested. Expected problems: hexdumps and `find` output can flood the address and path kinds (mitigated by the per-action cap); the credential pattern will match prose such as "the key is 3".

### 6. Task-aware summarizer

- **Config:** `summarizer_sees_task: true` (with `summarizer: llm`).
- **What it does:** the summarizer prompt (`memory_summarize.md.jinja2`) gains the challenge description and the facts block as it was before the action being summarized, and one extra rule: say what the action established or ruled out with respect to finding the flag. The rule against suggesting next steps stays. (The first version passed the facts block after the action's own values had been added, which told the summarizer not to repeat exactly the values the action produced; fixed on 2026-11-04.)
- **Code:** `LLMSummarizer.summarize`, `ActionMemory._summarize`, template `memory_summarize.md.jinja2`.
- **Based on:** EnIGMA's LM summarizer receives the challenge statement and the last command [31]; ReSum conditions the summary on the question [5]; ACON tunes compression guidelines from failures [7]; Anthropic advises tuning for recall first [13].
- **Addresses:** the v1 summarizer could not tell which values mattered.
- **Status:** implemented, untested. The failure-driven prompt revision from ACON [7] (compare runs solved without memory but not with it) is not automated; `scripts/paired_analysis.py` lists the challenges to look at.

### 7. Merge old entries instead of dropping them

- **Config:** `overflow: merge`, `max_earlier_chars: 800` (default `overflow: drop`, the v1 behaviour).
- **What it does:** when the summaries do not fit `max_memory_chars`, the oldest ones become one line each under "Earlier actions": the tool call(s) and the first line of the outcome (for example `Action 2: run_command(command='file flag.stfu') -> flag.stfu: data`). Only when that list also exceeds its budget are the oldest lines dropped, and the count is stated. Nothing is rewritten by a model.
- **Code:** `ActionRecord.one_line`, `ActionMemory.render`; template section `earlier`.
- **Based on:** ACE's context-collapse finding, where a wholesale rewrite shrank a context from 18,282 to 122 tokens and cut accuracy [23]; Mem0's itemized fact store [20]; the recursive-summary designs that carry the rewrite risk [16][28]; errors stored in memory propagate [47].
- **Addresses:** v1 limitation "old summaries are dropped silently" and issue I1 (the list of tried commands survives).
- **Status:** implemented, untested.

## Other changes in the same commit

- The memory block's `saved_dir` footer and the facts block are rendered by `memory.md.jinja2`; configs can override the template under `prompts:` as before.
- `--memory mask` added to `llm_ctf_solve.py`; other options come from the config.
- Conversation logs gain `memory_facts` and, per action, `saved` (paths written by option 4).

## Not implemented: promising things to explore

From the review's ranked list (numbers 8–13 there), plus harness ideas from `logs/issues.md`:

1. ~~Repeat detection in the harness~~ (issue I1): implemented on 2026-11-04 as `repeat_detection: note | skip` (README, "Harness options"), untested on a model. A nudge after a reply without a tool call (issue I9) was implemented at the same time as `no_tool_nudge`.
2. **Plan-and-status section** written by the agent at the end of each reply, keeping only the latest (Cybench [32], Manus [15]). Needs a format-compliance check for the 7B model first; the 4k baseline log shows replies with no tool call at all (issue I9), so compliance is not a given.
3. **Separate summarizer model** via `summarizer_model` (needs a second vLLM server or a larger GPU) [2][4][5][7].
4. **Subgoal-scoped memory** (HiAgent [21]) or a planner/executor split (D-CIPHER [35]); larger changes, and the published gains come from stronger or trained models.
5. **Cross-attempt reflection** (Reflexion [18]) if several attempts per challenge are run.
6. **Retrieval of CTF knowledge** (CRAKEN [36], CTFAgent [41]); competes for the same context budget.
7. **Training** the 7B model to manage its memory (MEM1 [6], ReSum [5]) or on CTF trajectories (Cyber-Zero [42]).

## What to measure

For every option: paired solve rate, `finish_reason` counts (especially `context_length`), mean and
maximum `prompt_tokens`, rounds per run, repeated-command share, and for `llm` summaries the
fraction of flag-like strings, paths and addresses in the raw output that survive into the summary
(`scripts/summary_fidelity.py`, run with the project's venv). `summary_seconds` gives the added cost.
