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

- `--repeat-detection note` (or `parameter.repeat_detection: note` in a config): when a tool call repeats an earlier call with the same arguments, the call runs again and its result gains a `note` ("Same call as in round N; the output is identical."). `skip` returns the earlier result without running the call; that can be stale if the container changed in between, so prefer `note`.
- `--no-tool-nudge on` (`parameter.no_tool_nudge: true`): after a reply with no tool call, the harness sends a message that restates the tool-call format with an example instead of "Please proceed to the next step". The prompt is `no_tool_call` and can be overridden in the config's `prompts` section.

- `--lenient-tool-calls on` (`parameter.lenient_tool_calls: true`): accept an `<invoke>` block without the `<function_calls>` wrapper, and parameters written as `<parameter><name>command</name><value>…</value></parameter>`, as tool calls (issue I11). In the 32k baseline, 69 of 101 replies without a tool call were such blocks. A reply cut off by the token cap in the middle of an `<invoke>` loses only that fragment; the log counts it in `tool_call_parsing.dropped_invokes` (issue I13).
- `--stop-after-first-call on` (`parameter.stop_after_first_call: true`): add `</invoke>` as a stop sequence, so each reply makes one tool call and the model sees its result before the next.
- `--max-output-chars N` (`parameter.max_output_chars: N`, 0 = no cap): shorten every tool output longer than N characters, keeping the head and the tail, with a note saying how much was cut (issue I12). The memory option `save_outputs` still saves the full output.

`config/qwen_harness.yaml` turns on repeat detection (`note`), the nudge, lenient parsing and a 20,000-character cap. Every conversation log records `repeat_detection`, `no_tool_call`, `tool_call_parsing` and `output_cap`, in every mode.

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
