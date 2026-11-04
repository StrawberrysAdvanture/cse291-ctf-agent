# LLM CTF Automation Framework

This repository holds the baseline automation framework for evaluation on the [NYU CTF Bench](https://nyu-llm-ctf.github.io).
It implements an LLM Agent that operates in a docker environment and interacts with CTF challenges to solve them.

## Installation and Setup

The setup requires docker to be installed on the system, please follow instructions for your OS.
The code is tested with atleast python 3.10, earlier versions may work but it is not tested.
It is recommended to create a virtualenv or conda environment for this setup.

Follow these instructions to setup:

1. Clone this repository: `git clone https://github.com/NYU-LLM-CTF/llm_ctf_automation`
2. `cd llm_ctf_automation`
3. Install python dependencies: `python3 -m pip install -r requirements.txt`
4. Run the setup script to build the docker image and create the network: `./setup.sh`
5. Download the dataset (will take a few minutes): `python3 -m nyuctf.download`

## Usage

```python
python3 llm_ctf_solve.py -c config/base_config.yaml --split <test|development> --challenge <challenge-name>
```

## Action memory

To fit long attempts into a small context window, the agent can replace each past action (model reply + tool outputs) with a short summary instead of resending the full conversation. Enable it with `--memory llm` (the model writes the summaries) or `--memory truncate` (no extra model call), or use `config/qwen_memory.yaml`. See [docs/memory.md](docs/memory.md).

## Harness options

Two options address issues I1 and I9 in `logs/issues.md`; both are off by default, so the baseline is unchanged.

- `--repeat-detection note` (or `parameter.repeat_detection: note` in a config): when a tool call repeats an earlier call with the same arguments, the call runs again and its result gains a `note` ("Same call as in round N; the output is identical."). `skip` returns the earlier result without running the call; that can be stale if the container changed in between. `skip_identical` runs a repeated call once more and, once a repeat has reproduced the earlier output exactly, serves further repeats of that call from the store with a note; commands whose output changes keep running, and any call never seen before (an edited file, a new command) resets the counts, so an edit-then-rerun loop still runs. In the harness-fixes run the `note` alone did not stop the model from re-running timed-out network reads every round, so `skip_identical` is the setting the v2 configs use.
- `--no-tool-nudge on` (`parameter.no_tool_nudge: true`): after a reply with no tool call, the harness sends a message that restates the tool-call format with an example instead of "Please proceed to the next step". The prompt is `no_tool_call` and can be overridden in the config's `prompts` section.

- `--lenient-tool-calls on` (`parameter.lenient_tool_calls: true`): accept an `<invoke>` block without the `<function_calls>` wrapper, and parameters written as `<parameter><name>command</name><value>…</value></parameter>`, as tool calls (issue I11). In the 32k baseline, 69 of 101 replies without a tool call were such blocks. A reply cut off by the token cap in the middle of an `<invoke>` loses only that fragment; the log counts it in `tool_call_parsing.dropped_invokes` (issue I13). A program named as the tool (`<tool_name>python3</tool_name>` with a `command`, `args` or `path` parameter, which the model did 27 times in the harness-fixes run) runs through `run_command` as `python3 <value>`, with a note in the result; the log lists these under `rerouted_calls`. A name that is a prefix of a real tool (`run`, `dis`) is a truncated call and stays unknown.
- `--stop-after-first-call on` (`parameter.stop_after_first_call: true`): add `</invoke>` as a stop sequence, so each reply makes one tool call and the model sees its result before the next.
- `--max-output-chars N` (`parameter.max_output_chars: N`, 0 = no cap): shorten every tool output longer than N characters, keeping the head and the tail, with a note saying how much was cut (issue I12). The memory option `save_outputs` still saves the full output.
- `--max-output-tokens N` and `--max-reply-output-tokens N` (`parameter.max_output_tokens`, `parameter.max_reply_output_tokens`, 0 = off): the same cap measured in estimated tokens, per output and for all outputs of one reply together. `llm_ctf/tokens.py` estimates without a tokenizer (about 2.8 characters per token for prose, 1.0 for hex dumps and escaped bytes, fitted to vLLM's reported prompt sizes so that it errs high). Needed because two 20,000-character outputs of binary data in one reply overran the 32k context in the harness-fixes run.

`config/qwen_harness.yaml` turns on repeat detection (`note`), the nudge, lenient parsing and a 20,000-character cap; its record is `logs/runs/20261104-050449-2c0c-harness-32k-3r/`. `config/qwen_harness_v2.yaml` turns on everything (`skip_identical`, the nudge, lenient parsing with rerouting, `stop_after_first_call`, 20,000 characters and 3,000 tokens per output, 6,000 tokens per reply) at 3 rounds, and the memory configs use the same options at 30 rounds; none of these v2 settings has been run yet. Every conversation log records `repeat_detection`, `no_tool_call`, `tool_call_parsing`, `output_cap` and `rerouted_calls`, in every mode.

## Running on AWS

You can run the agent and a locally served model (vLLM) on a single AWS GPU instance. You need an AWS access key and the amount of GPU memory to request:

```bash
# 1. Install the AWS CLI: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html
# 2. Provide your AWS access key (IAM console -> Users -> Security credentials -> Create access key)
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
aws sts get-caller-identity                     # checks the key works

# 3. Launch: the 7B Qwen baseline on a 24 GB GPU, all development challenges
./aws/ctf-aws.sh launch --gpu-mem 24 --challenges all

# 4. Follow progress and download the results
./aws/ctf-aws.sh logs <RUN_ID> -f
./aws/ctf-aws.sh results <RUN_ID>               # -> ./aws-runs/<RUN_ID>/summary.txt
```

The instance terminates itself when the job is done. See [docs/aws.md](docs/aws.md) for all options, choosing GPU memory (including the 32B model), costs and troubleshooting.
