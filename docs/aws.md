# Running on AWS (single GPU)

`aws/ctf-aws.sh` runs the agent with a vLLM-served model on one AWS GPU instance. You provide AWS credentials and the GPU memory you need. The script chooses the instance, starts it, serves the model, runs the challenges, uploads the results to S3 and terminates the instance.

By default it runs the **original baseline**: `Qwen/Qwen2.5-7B-Instruct-AWQ`, served as `Qwen/Qwen2.5-7B-Instruct`, with `config/qwen_baseline.yaml` and a 4096-token context. This is the same setup as [local-setup.md](local-setup.md), so it fits a 24 GB GPU.

## 1. One-time setup

### Install the AWS CLI

Follow the [AWS CLI install guide](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) and check it with `aws --version`. You also need `git`, `tar` and `bash` (the bash 3.2 that ships with macOS works).

### Get an AWS access key

The script uses an **access key ID** and a **secret access key** (together, "the AWS API key"):

1. Sign in to the [AWS console](https://console.aws.amazon.com/) and open **IAM → Users**. Select your user, or create one.
2. The user needs permission to use **EC2, S3 and IAM**. The simplest option is the `AdministratorAccess` policy on a dedicated account. If you use narrower permissions, the user must be able to:
   - launch and terminate EC2 instances, and create a security group;
   - create an S3 bucket and read and write its objects;
   - create an IAM role and instance profile and pass that role to EC2.
3. Open **Security credentials → Create access key → Command Line Interface (CLI)**. Copy both values; the secret is shown only once.

If your organization uses AWS SSO or IAM Identity Center, you get temporary credentials instead, which include a session token. Copy all three values from the SSO portal ("Access keys"), or run `aws sso login`.

**Never commit these keys.** Keep them in your shell environment or in `~/.aws/credentials`.

### Give the keys to the AWS CLI

Do one of the following:

```bash
# Option A: environment variables (current shell only)
export AWS_ACCESS_KEY_ID=AKIA...
export AWS_SECRET_ACCESS_KEY=...
export AWS_SESSION_TOKEN=...        # only for temporary/SSO credentials

# Option B: save them once in ~/.aws/credentials
aws configure                        # prompts for the key ID, secret and region (us-west-2)
```

Check that they work:

```bash
aws sts get-caller-identity
```

## 2. Run

```bash
./aws/ctf-aws.sh launch --gpu-mem 24                         # one smoke-test challenge
./aws/ctf-aws.sh launch --gpu-mem 24 --challenges all        # all 57 development challenges
./aws/ctf-aws.sh launch --gpu-mem 24 --challenges-file config/challenges/dev-subset-25.txt   # 25-challenge stratified subset
```

`launch` shows the instance it chose and asks for confirmation (`-y` skips the prompt). It then prints a **RUN_ID**. The first steps on the instance take about 15–25 minutes: downloading the model, building the `ctfenv` Docker image and installing Python packages. After that the challenges run one after another.

```bash
./aws/ctf-aws.sh logs <RUN_ID> -f       # follow progress until the job ends
./aws/ctf-aws.sh status                 # list active runs
./aws/ctf-aws.sh results <RUN_ID>       # download outputs to ./aws-runs/<RUN_ID>/
./aws/ctf-aws.sh stop <RUN_ID>          # terminate early
```

`results` downloads:

- `summary.txt`: solved / rounds / finish reason for each challenge;
- `logs/`: the agent's full conversation JSON for each challenge;
- `job.log`: everything the instance printed;
- `vllm.log`: the model server's output.

Your machine can be turned off while a run is in progress. The instance does not depend on your shell or your credentials after launch.

## 3. Options

| Option | Default | Meaning |
|---|---|---|
| `--gpu-mem GB` | required | Minimum GPU memory. The cheapest single-GPU instance with at least this much is used; the other usable types are fallbacks. |
| `--challenges LIST` | `2016q-cry-sleeping_guard` | Comma-separated names, or `all` |
| `--challenges-file FILE` | — | One challenge name per line (`#` comments allowed); must be inside the repo. Cannot be combined with `--challenges` |
| `--split` | `development` | `development` or `test` |
| `--model HF_ID` | `Qwen/Qwen2.5-7B-Instruct-AWQ` | Hugging Face checkpoint to serve |
| `--config FILE` | `config/qwen_baseline.yaml` | Agent config; its `model` is replaced with the served name |
| `--max-model-len N` | 4096 on GPUs under 20 GB, 32768 otherwise | vLLM context length |
| `--max-hours H` | 12 | Hard limit; the instance terminates after this |
| `--instance-type T` | automatic | Use only this type (no fallback) |
| `--wait-capacity MIN` | 0 | If no instance type has capacity, retry every 5 minutes for up to MIN minutes |
| `--keep` | off | Leave the instance running after the job (still bounded by `--max-hours`) |
| `--dry-run` | off | Show what would be launched, then exit |
| `-- COMMAND...` | challenge loop | Run any command on the instance; `$RUN_CONFIG` and `$LOG_DIR` are set |

The region comes from `AWS_REGION` (default `us-west-2`). On our account, the organization blocks EC2 in `us-east-1`.

### GPU memory guide

`--gpu-mem` maps to these instances in `us-west-2`:

| `--gpu-mem` | Instance | GPU | Fits |
|---|---|---|---|
| **24** | `g6.xlarge` | L4 | **7B baseline** (AWQ about 5.5 GB; BF16 about 15 GB) |
| 48 | `g6e.xlarge` | L40S | 32B in 4-bit AWQ (about 19 GB) with 32k context, or FP8 |
| 96 | `g7e.2xlarge` | RTX PRO 6000 | 32B in full BF16 (about 65 GB) |

The table shows the first choice. The launch summary's `Fallbacks` line lists the other types that satisfy `--gpu-mem` and the vCPU quota, cheapest first; when the first type has no capacity in any zone, they are tried in that order (`g6e.xlarge -> g6e.2xlarge -> g7e.2xlarge` for 48). A fallback can cost more than the first choice, so read the summary before confirming, or pass `--instance-type` to pin one type.

To run the 32B model:

```bash
./aws/ctf-aws.sh launch --gpu-mem 48 --model Qwen/Qwen2.5-32B-Instruct-AWQ --config config/qwen32b.yaml
```

To serve any other model, add its served name (the `--model` id without a trailing `-AWQ`) to `VLLMBackend.MODELS` in `llm_ctf/backends/vllm_backend.py`, or pass `--served-name`.

### Challenge subsets

`config/challenges/dev-subset-25.txt` holds 25 of the 57 development challenges, chosen so that every category keeps its share: cry 5/11, for 5/10, msc 3/6, and 4/10 each for pwn, rev and web. Pass it with `--challenges-file`. The run takes about half as long as `--challenges all` and still covers every category.

File format: one challenge name per line; blank lines and lines starting with `#` are ignored; names must not contain whitespace. The file must be inside the repository and not excluded by `.gitignore`, because the instance reads it from the uploaded working tree. The launcher checks all of this before it starts anything.

Other sizes come from the same recipe, which is deterministic (the same list and size always give the same subset):

```bash
python3 scripts/challenge_subset.py config/challenges/dev-all.txt 40 > config/challenges/dev-subset-40.txt
```

The script splits the size across categories in proportion to their counts and picks evenly spaced names within each category. A size below the number of categories (6) drops some categories. `config/challenges/dev-all.txt` is the full development split in dataset order, copied from the `Challenges to run (57):` line of run `20261103-233511-6b9b`; its header comment has the command that regenerates it from the installed dataset.

## How it works

1. **Locally**, `ctf-aws.sh`:
   - checks the credentials and picks the cheapest instance type that is offered in the region and fits your vCPU quota, keeping the other usable types as fallbacks;
   - on first use, creates:
     - the S3 bucket `llm-ctf-runs-<account>-<region>`;
     - the IAM role `llm-ctf-runner`, which can only access that bucket;
     - a security group `llm-ctf-runner` in the default VPC that allows outbound traffic and no inbound traffic. The VPC's default security group is not used because hardened accounts (including ours) remove its outbound rules.
   - uploads the working tree (git-tracked and untracked files; `.gitignore`'d files such as `keys.cfg` are left out);
   - launches the AWS Deep Learning Base GPU AMI (Ubuntu 22.04).
2. **On the instance** (`aws/remote/`):
   - starts vLLM (`vllm/vllm-openai:v0.10.1.1` Docker image, listening on `127.0.0.1:18000` only; not 8000, which several challenges' services publish);
   - while the model loads, creates the agent venv, runs `setup.sh` and writes `keys.cfg`;
   - runs `llm_ctf_solve.py` for each challenge.
3. **Results:** outputs are uploaded to `s3://<bucket>/runs/<RUN_ID>/outputs/` every 30 seconds. When the job ends, the instance terminates itself.

## Costs and safety

- On-demand prices in `us-west-2` are roughly $0.80/h for `g6.xlarge`, $1.86/h for `g6e.xlarge`, $2.24/h for `g6e.2xlarge` and $3.36/h for `g7e.2xlarge` (a fallback can be one of the dearer types). Check current prices for your region.
- The instance terminates when the job ends or fails. `--max-hours` is a hard limit: the shutdown is scheduled at boot, so it applies even if the job hangs.
- Run `./aws/ctf-aws.sh status` to check for leftover instances.
- No inbound ports are opened.
- `HF_TOKEN` (needed only for gated models) is passed through instance user-data, which other users of the AWS account can read. Use a read-only token.
- Results stay in S3 until you delete them: `aws s3 rm --recursive s3://llm-ctf-runs-<account>-<region>/runs/<RUN_ID>/`.

## Troubleshooting

| Message | Fix |
|---|---|
| `no valid AWS credentials` | Set the keys (step 1); check with `aws sts get-caller-identity`. Temporary credentials expire, so get new ones. |
| `No capacity for ...` / `no capacity for g6e.xlarge, ... in us-west-2` | AWS has no free GPUs of those types right now. The launcher tries every zone of every usable type before giving up; pass `--wait-capacity 60` to keep retrying every 5 minutes for an hour, `--instance-type` to pin one type, or try another `AWS_REGION`. |
| `VcpuLimitExceeded` / `quota is used by other running instances` | The G-family vCPU quota (8 on our account) is partly used by another running instance; `./aws/ctf-aws.sh status` lists ours. Wait for it to finish, use `--wait-capacity`, or request a higher quota. |
| `needs N vCPUs, quota is M` | Request a higher "Running On-Demand G and VT instances" quota under **Service Quotas → EC2**. |
| Status stays at `LAUNCHED`; the console shows `i/o timeout` | The instance has no outbound internet. Check that the `llm-ctf-runner` security group allows outbound traffic and that the default VPC routes `0.0.0.0/0` to an internet gateway. |
| `UnauthorizedOperation ... service control policy` | Your organization blocks that region; use another `AWS_REGION`. |
| `'X' is not in VLLMBackend.MODELS` | Register the served model name in `llm_ctf/backends/vllm_backend.py`. |
| Job `FAILED` | Read `job.log` and `vllm.log` from `./aws/ctf-aws.sh results <RUN_ID>`. |
