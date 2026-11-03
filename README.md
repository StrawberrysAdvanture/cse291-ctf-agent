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
