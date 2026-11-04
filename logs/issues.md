# Issues log

Problems seen while running the baseline and memory agents, with evidence and next steps.
Add an entry per issue; update its status when it changes. Run IDs refer to `logs/runs/`.

| ID | Status | Area | Issue |
|---|---|---|---|
| I1 | open | agent | The model repeats the same action (same command, same output) across rounds |
| I2 | measuring | agent | Runs end on `context_length` with a 4096-token context |
| I3 | fixed | runner | Dataset download output was mistaken for challenge names |
| I4 | fixed | runner | Instance had no outbound network (default security group) |
| I5 | fixed | runner | No on-demand capacity for `g6e.xlarge` (48 GB) in `us-west-2` |
| I6 | fixed | harness | vLLM backend logs omitted tool outputs and user messages |
| I7 | open | harness | The loop makes `max_rounds + 1` model calls |
| I8 | open | memory | Memory v1 never shows the latest output verbatim |
| I9 | open | agent | Replies with no tool call: the model describes steps instead of running them |
| I10 | open | runner | Some challenges' own services fail to start (`docker compose up` non-zero); the cause was not logged |

## I1. Repeated actions

- **Observed by:** the team in local runs with Qwen2.5-7B-Instruct (reported 2026-11-03). Not yet quantified.
- **Why it matters:** with `max_rounds: 3` a repeated command wastes a third of the budget; in memory mode a summary that hides the outcome of a command invites running it again.
- **How to measure:** `scripts/paired_analysis.py` reports, per arm, the share of tool calls whose command exactly repeats an earlier command in the same challenge, and the number of challenges with at least one repeat. Compare baseline against memory: if memory raises the rate, summaries are losing the outcome.
- **Candidate fixes (from `docs/memory-literature.md`):**
  1. Keep the latest action verbatim (`raw_tail: 1`), so the model sees the output of what it just ran.
  2. A merged "earlier actions" list (`overflow: merge`) that keeps one line per tried command.
  3. A pinned facts block (`facts: true`) so exact values survive summarization.
  4. A harness-side check: if a tool call repeats an earlier one, append a note to the tool result ("you already ran this at action N; the output was the same") instead of running it again. Not implemented.
- **Next step:** run the 32k paired comparison, read the per-challenge repeat counts, and look at 3–5 transcripts with repeats to see whether the repeat follows an identical, a summarized, or an empty output.

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

- **Observed:** run `7a17`, challenge `2013f-cry-stfu`: both model replies were prose ("we need to understand what a Secure Test File Unit is…", "Let's proceed by examining…") with no `<function_calls>` block, so no command ran and the harness sent "Please proceed to the next step". The challenge ended on context length without a single tool call.
- **Why it matters:** rounds spent on prose are lost, and memory cannot help a run that never produces an observation.
- **How to measure:** share of model calls with zero tool calls, per arm (`tool_calls` per challenge in `scripts/paired_analysis.py`; a per-call count is easy to add).
- **Candidate fixes:** a shorter keep-going message that restates the tool-call format with an example; `max_tokens` lower than 1024 so prose replies cost less context; a format check that re-prompts once when a reply has no tool call. None implemented.

## I8. Latest output never verbatim (memory v1)

- **Observed:** by design in v1; all actions, including the most recent, are summarized.
- **Change:** `raw_tail` option implemented (`docs/memory-experiments.md`, experiment 1), untested.

## I10. Challenge services fail to start

- **Observed:** run `6b9b` (baseline, 32k), 00:09 UTC: `2013q-cry-slurp` crashed in `CTFConversation.__enter__` because `docker compose -f .../crypto/slurp/docker-compose.yml up -d --force-recreate` returned non-zero. nyuctf runs compose with its output captured and `check=True`, so the log shows the command but not the error. The run continued with the next challenge; the crashed challenge has no conversation log and counts as `no_log` in the analysis.
- **Fix so far:** `CTFConversation.__enter__` now prints compose's stdout and stderr before re-raising, so the next run shows the cause.
- **Next step:** read the cause in the next run's `job.log`; if it is an image build failure inside the challenge's compose file, decide whether to pre-build or skip those challenges and mark them in the results.
