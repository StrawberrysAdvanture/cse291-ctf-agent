# Issues log

Problems seen while running the baseline and memory agents, with evidence and next steps.
Add an entry per issue; update its status when it changes. Run IDs refer to `logs/runs/`.

| ID | Status | Area | Issue |
|---|---|---|---|
| I1 | mitigation implemented, untested | agent | The model repeats the same action (same command, same output) across rounds |
| I2 | measuring | agent | Runs end on `context_length` with a 4096-token context |
| I3 | fixed | runner | Dataset download output was mistaken for challenge names |
| I4 | fixed | runner | Instance had no outbound network (default security group) |
| I5 | fixed | runner | No on-demand capacity for `g6e.xlarge` (48 GB) in `us-west-2` |
| I6 | fixed | harness | vLLM backend logs omitted tool outputs and user messages |
| I7 | open | harness | The loop makes `max_rounds + 1` model calls |
| I8 | open | memory | Memory v1 never shows the latest output verbatim |
| I9 | mitigation implemented, untested | agent | Replies with no tool call: the model describes steps instead of running them |
| I10 | fixed | runner | Challenges whose service publishes port 8000 could not start: vLLM was bound to that port |

## I1. Repeated actions

- **Observed by:** the team in local runs with Qwen2.5-7B-Instruct (reported 2026-11-03). **Quantified in run `6b9b`** (32k, 3 rounds): 114 of 261 tool calls (44%) repeated an earlier call in the same challenge; 23 of 47 challenges had at least one repeat (`logs/runs/20261103-233511-6b9b-baseline-32k-3r/report.md`; what the repeats follow is in `findings.md` there).
- **Why it matters:** with `max_rounds: 3` a repeated command wastes a third of the budget; in memory mode a summary that hides the outcome of a command invites running it again.
- **How to measure:** `scripts/paired_analysis.py` reports, per arm, the share of tool calls whose command exactly repeats an earlier command in the same challenge, and the number of challenges with at least one repeat. Compare baseline against memory: if memory raises the rate, summaries are losing the outcome.
- **Candidate fixes (from `docs/memory-literature.md`):**
  1. Keep the latest action verbatim (`raw_tail: 1`), so the model sees the output of what it just ran.
  2. A merged "earlier actions" list (`overflow: merge`) that keeps one line per tried command.
  3. A pinned facts block (`facts: true`) so exact values survive summarization.
  4. A harness-side check: if a tool call repeats an earlier one, append a note to the tool result ("you already ran this at action N; the output was the same") instead of running it again. Not implemented.
- **Change (2026-11-04):** harness-side detection implemented in `llm_ctf/repeats.py` and `CTFConversation.run_tools` (`repeat_detection: off | note | skip`, default `off`). The key is the tool name plus the parsed arguments with defaults filled in, so `run_command(command="ls")` and `run_command(command="ls", timeout=10)` match. `note` runs the call again and adds `note: "Same call as in round N; the output is identical."` (or "differs") to the result; `skip` returns the stored result with a note instead of running it (can be stale if the container changed). Every log, including baseline logs, now carries `repeat_detection.count` and one entry per repeat (`round`, `earlier_rounds`, `tool`, `arguments`, `action`, `identical`); this is the authoritative count (slightly broader than `scripts/paired_analysis.py`, which compares logged commands verbatim). The note reaches the memory block under all three summarizers. `config/qwen_harness.yaml` enables `note`.
- **Next step:** read the per-challenge repeat counts from the 32k baseline, look at 3–5 transcripts with repeats to see whether the repeat follows an identical, a summarized, or an empty output; then run `qwen_harness.yaml` against the baseline and compare `repeat_detection.count` and the share of `identical: true` repeats.

## I2. Context length

- **Observed:** run `7a17` (baseline, 4096 tokens), challenge `2013f-cry-stfu`: `finish_reason: context_length` after only **two** model calls (`prompt_tokens: [1000, 2045]`). The reply limit is 1024 tokens and vLLM rejects a request when prompt + reply limit exceeds the context, so at 4k the baseline cannot complete three rounds once the model writes long replies. NYU CTF Bench reports token overflow as 24.56% of GPT-3.5 failures. Log: `logs/runs/20261103-225656-7a17-baseline-4k-partial/`.
- **Change:** the AWS runner now serves the model's full 32,768-token context on GPUs with 20 GB or more (`aws/remote/job.sh`). The second paired run uses 32k.
- **Next step:** compare `prompt_tokens` and `finish_reason: context_length` counts between the 4k logs (`logs/runs/*-7a17-*`, `*-7074-*`, partial) and the 32k runs.

## I3. Dataset download output mistaken for challenge names (fixed)

- **Observed:** run `20261103-225656-7a17`, 23:26 UTC. The first four "challenges" were `Cloning`, `using`, `git...`, `Cloned!`; each failed with `KeyError`. Cause: the dataset was downloaded on first use inside the command that listed challenge names, and git's progress output was captured into the list, which was then split on spaces.
- **Fix:** `aws/remote/job.sh` downloads the dataset during setup and writes the challenge names to a file from Python; the loop reads the file line by line.

## I4. No outbound network (fixed)

- **Observed:** run `20261103-222816-112b`. Status never left `LAUNCHED`; the console showed `i/o timeout` to AWS endpoints. The default VPC's default security group in this account has no egress rule.
- **Fix:** the launcher creates and uses an outbound-only security group `llm-ctf-runner` (commit `815893a`).

## I5. GPU capacity (fixed)

- **Observed:** 22:26 UTC, `g6e.xlarge` had no on-demand capacity in any `us-west-2` zone. `g6.xlarge` launched after one zone failed.
- **Fix:** `aws/ctf-aws.sh launch` keeps every instance type that meets `--gpu-mem` and the vCPU quota, cheapest first, and tries each in every zone before giving up; the pre-launch summary lists the fallbacks (`g6e.xlarge -> g6e.2xlarge -> g7e.2xlarge` for 48 GB) and the status file records the type that launched. `--wait-capacity MIN` repeats the whole list every 5 minutes for up to MIN minutes. `VcpuLimitExceeded` (the quota is used by another of our instances) counts as no capacity for that type. `--instance-type` pins one type with no fallback. See `docs/aws.md`.
- **Verified with:** a stub AWS CLI covering the fallback, wait, quota, IAM-retry and fatal-error paths, and `--dry-run` against the account. Not yet exercised by a real capacity shortage; while run `6b9b` uses 4 of the 8 G-vCPUs, every 8-vCPU fallback would hit `VcpuLimitExceeded`.

## I6. Incomplete conversation logs (fixed)

- **Observed:** the vLLM backend logged only the model's replies. Tool outputs and user messages were missing, so context use could not be measured from logs.
- **Fix:** commit `6b08d87` logs them and records `prompt_tokens` for every call.

## I7. One extra model call per run

- **Observed:** `CTFConversation.run` loops while `rounds <= max_rounds`, so `max_rounds: 3` gives four model calls. Left unchanged to keep the baseline comparable with earlier results; noted in `docs/memory.md`.

## I9. Replies without tool calls

- **Observed:** run `7a17`, challenge `2013f-cry-stfu`: both model replies were prose ("we need to understand what a Secure Test File Unit is…", "Let's proceed by examining…") with no `<function_calls>` block, so no command ran and the harness sent "Please proceed to the next step". The challenge ended on context length without a single tool call. **Quantified in run `6b9b`:** 101 of 181 replies (56%) had no tool call; in 19 of 47 challenges every reply lacked one; `check_flag` was never called in the whole run (0 of 47 solved).
- **Why it matters:** rounds spent on prose are lost, and memory cannot help a run that never produces an observation.
- **How to measure:** share of model calls with zero tool calls, per arm (`tool_calls` per challenge in `scripts/paired_analysis.py`; a per-call count is easy to add).
- **Change (2026-11-04):** `no_tool_nudge` (`--no-tool-nudge on`, `parameter.no_tool_nudge: true`, default off). After a reply with no tool call the harness sends the `no_tool_call` prompt: it says that nothing ran, shows a full `run_command` example in the formatter's syntax (`ls ~/ctf_files`) and lists the available tools, instead of "Please proceed to the next step using your best judgment." Costs roughly 90–110 tokens per prose-only round. With memory on, the nudge takes the place of "Choose your next action." after the memory block. Logs now carry `no_tool_call` (`count`, `rounds`, `nudges_sent`) in every mode. Not implemented: a lower `max_tokens` for prose replies; a one-off format re-prompt.
- **Next step:** compare `no_tool_call.count` per arm between the baseline and `qwen_harness.yaml` runs, and check whether the round after a nudge contains a tool call.

## I8. Latest output never verbatim (memory v1)

- **Observed:** by design in v1; all actions, including the most recent, are summarized.
- **Change:** `raw_tail` option implemented (`docs/memory-experiments.md`, experiment 1), untested.

## I10. Challenge services fail to start

- **Observed:** run `6b9b` (baseline, 32k), 00:09 UTC: `2013q-cry-slurp` crashed in `CTFConversation.__enter__` because `docker compose -f .../crypto/slurp/docker-compose.yml up -d --force-recreate` returned non-zero. nyuctf runs compose with its output captured and `check=True`, so the log shows the command but not the error. The run continued with the next challenge; the crashed challenge has no conversation log and counts as `no_log` in the analysis.
- **Final count in `6b9b`:** 10 challenges crashed this way (`2013q-cry-slurp`, `2016q-cry-sleeping_guard`, `2016q-cry-broken_box`, `2016q-msc-coinslot`, `2016q-msc-regexpire`, `2016q-web-i_got_id`, `2016q-web-mfw`, `2016q-pwn-warmup`, `2016q-pwn-hungman`, `2015q-pwn-meme_shop`); none has a conversation log. The list is in `config/challenges/rerun-6b9b-crashed.txt` for a rerun with `--challenges-file`.
- **Cause:** the three compose files (read from the dataset repository) publish host port `8000:8000`, and the runner serves vLLM on host port 8000, so `docker compose up` fails with a port conflict. The local setup guide uses port 8000 for vLLM too, so its suggested smoke test (`2016q-cry-sleeping_guard`) would hit the same conflict.
- **Fix:** `aws/remote/job.sh` now publishes vLLM on host port 18000 (`VLLM_PORT`), and `keys.cfg` points there; `docs/local-setup.md` has a troubleshooting row. `CTFConversation.__enter__` also prints compose's output on such failures (`fbca814`).
- **Consequence for the results:** the baseline solve rate is over 47 challenges, not 57. A memory run on the old port would have lost the same 10, so paired comparisons would have been unaffected; runs after `e31db05` include them.
