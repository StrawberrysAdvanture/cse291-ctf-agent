#!/usr/bin/env bash
# Run the CTF agent with a vLLM-served model on a single-GPU AWS EC2 instance.
#
# Quick start:
#   export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...   # (or `aws configure`)
#   ./aws/ctf-aws.sh launch --gpu-mem 24
#   ./aws/ctf-aws.sh logs <run-id> -f
#   ./aws/ctf-aws.sh results <run-id>
#
# The instance serves the model with vLLM, runs the agent, uploads everything
# to S3 and terminates itself. See docs/aws.md for details.
#
# Written for bash 3.2+ so it also runs on macOS.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
export AWS_DEFAULT_REGION="$REGION" AWS_PAGER=""

PROJECT_TAG="llm-ctf"
ROLE_NAME="llm-ctf-runner"
LAUNCHED_ID=""   # set by launch_type_in_zones on success
LAST_ERROR=""    # last capacity error seen by launch_type_in_zones
AMI_NAME_PATTERN="Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04)*"
VLLM_IMAGE_DEFAULT="vllm/vllm-openai:v0.10.1.1"

die() { echo "error: $*" >&2; exit 1; }
log() { echo "==> $*" >&2; }

usage() {
    cat <<'EOF'
Usage: aws/ctf-aws.sh <command> [options]

Commands:
  launch --gpu-mem GB [options] [-- COMMAND...]
                       Start a single-GPU instance, serve the model, run the agent.
  status [RUN_ID]      Show one run, or list active runs.
  logs RUN_ID [-f]     Print the run's log (-f: keep following until it ends).
  results RUN_ID       Download the run's outputs to ./aws-runs/RUN_ID/.
  stop RUN_ID          Terminate the run's instance now.

launch options:
  --gpu-mem GB         Required. Minimum GPU memory. The cheapest single-GPU
                       instance with at least this much is chosen; the other
                       usable types are fallbacks (24 -> g6 L4, 48 -> g6e L40S,
                       96 -> g7e RTX PRO 6000).
  --model HF_ID        Hugging Face checkpoint to serve
                       (default: Qwen/Qwen2.5-7B-Instruct-AWQ, the baseline model).
  --served-name NAME   Model name the agent uses; must be in VLLMBackend.MODELS
                       (default: --model with a trailing -AWQ removed).
  --config FILE        Agent config (default: config/qwen_baseline.yaml). Its model
                       is replaced with --served-name.
  --challenges LIST    Comma-separated challenge names, or "all"
                       (default: 2016q-cry-sleeping_guard).
  --challenges-file FILE
                       File with one challenge name per line (blank lines and
                       # comments are ignored). Must be inside the repository, which
                       is uploaded to the instance. Not with --challenges.
  --split SPLIT        development | test (default: development).
  --max-model-len N    vLLM context length (default: 32768, the model's full
                       context, on GPUs with >= 20 GB; 4096 on smaller GPUs).
  --max-hours H        Hard limit; the instance terminates after H hours (default: 12).
  --disk GB            Root volume size (default: 200).
  --instance-type T    Use only this type (no fallback).
  --wait-capacity MIN  If no type has capacity, retry every 5 minutes for up to
                       MIN minutes (default: 0, give up after one pass).
  --keep               Do not terminate when the job ends (still bounded by --max-hours).
  --yes, -y            Do not ask for confirmation.
  --dry-run            Show what would be launched, then exit.
  -- COMMAND...        Run this command on the instance instead of the challenge
                       loop. $RUN_CONFIG and $LOG_DIR are set for it.

Credentials come from the standard AWS CLI sources (environment variables,
`aws configure`, or a profile via AWS_PROFILE). Region: $AWS_REGION, default us-west-2.
An optional HF_TOKEN in your environment is forwarded for gated models.
EOF
}

need_aws() {
    command -v aws >/dev/null 2>&1 || die "the AWS CLI is not installed: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
    ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) \
        || die "no valid AWS credentials. Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY (or run 'aws configure')."
    BUCKET="llm-ctf-runs-${ACCOUNT}-${REGION}"
}

# ---------------------------------------------------------------- instance selection

# Prints "gpu_mib vcpus type gpu_name" lines for single-NVIDIA-GPU x86 types, smallest first.
gpu_instance_candidates() {
    local min_mib=$1
    aws ec2 describe-instance-types \
        --filters Name=current-generation,Values=true Name=processor-info.supported-architecture,Values=x86_64 \
        --query "InstanceTypes[?GpuInfo.Gpus[0].Count==\`1\` && GpuInfo.Gpus[0].Manufacturer=='NVIDIA' && GpuInfo.Gpus[0].MemoryInfo.SizeInMiB>=\`${min_mib}\`].[GpuInfo.Gpus[0].MemoryInfo.SizeInMiB,VCpuInfo.DefaultVCpus,InstanceType,GpuInfo.Gpus[0].Name]" \
        --output text | sort -n -k1,1 -k2,2
}

# On-demand vCPU quota for the instance family, or empty if unknown.
vcpu_quota() {
    local code
    case "$1" in
        g*) code=L-DB2E81BA ;;   # Running On-Demand G and VT instances
        p*) code=L-417A185B ;;   # Running On-Demand P instances
        *) return 0 ;;
    esac
    aws service-quotas get-service-quota --service-code ec2 --quota-code "$code" \
        --query 'Quota.Value' --output text 2>/dev/null | cut -d. -f1 || true
}

# Name of the vCPU quota that applies to an instance type, for messages.
vcpu_quota_name() {
    case "$1" in
        g*) echo "Running On-Demand G and VT instances (L-DB2E81BA)" ;;
        p*) echo "Running On-Demand P instances (L-417A185B)" ;;
        *) echo "vCPU" ;;
    esac
}

# HH:MM in UTC for a Unix timestamp (GNU and BSD date).
utc_clock() { date -u -d "@$1" +%H:%M 2>/dev/null || date -u -r "$1" +%H:%M; }

availability_zones_for() {
    aws ec2 describe-instance-type-offerings --location-type availability-zone \
        --filters "Name=instance-type,Values=$1" \
        --query 'InstanceTypeOfferings[].Location' --output text
}

# ---------------------------------------------------------------- one-time setup

ensure_bucket() {
    if aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then return; fi
    log "Creating S3 bucket $BUCKET"
    if [ "$REGION" = "us-east-1" ]; then
        aws s3api create-bucket --bucket "$BUCKET" >/dev/null
    else
        aws s3api create-bucket --bucket "$BUCKET" \
            --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
    fi
}

ensure_instance_profile() {
    if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
        log "Creating IAM role $ROLE_NAME (lets the instance write to the S3 bucket)"
        aws iam create-role --role-name "$ROLE_NAME" \
            --tags "Key=Project,Value=$PROJECT_TAG" \
            --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
    fi
    aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name s3-run-bucket \
        --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:PutObject\",\"s3:ListBucket\"],\"Resource\":[\"arn:aws:s3:::llm-ctf-runs-${ACCOUNT}-*\",\"arn:aws:s3:::llm-ctf-runs-${ACCOUNT}-*/*\"]}]}"
    if ! aws iam get-instance-profile --instance-profile-name "$ROLE_NAME" >/dev/null 2>&1; then
        aws iam create-instance-profile --instance-profile-name "$ROLE_NAME" >/dev/null
        aws iam add-role-to-instance-profile --instance-profile-name "$ROLE_NAME" --role-name "$ROLE_NAME"
    fi
}

# Security group with no inbound rules and all outbound traffic allowed. The VPC's
# default group is not used because hardened accounts often strip its egress rules.
ensure_security_group() {
    local vpc=$1 sg
    sg=$(aws ec2 describe-security-groups --filters "Name=vpc-id,Values=$vpc" "Name=group-name,Values=$ROLE_NAME" \
        --query 'SecurityGroups[0].GroupId' --output text)
    if [ -z "$sg" ] || [ "$sg" = "None" ]; then
        log "Creating security group $ROLE_NAME (outbound only, no inbound)"
        sg=$(aws ec2 create-security-group --group-name "$ROLE_NAME" --vpc-id "$vpc" \
            --description "llm-ctf runner: outbound only, no inbound" \
            --tag-specifications "ResourceType=security-group,Tags=[{Key=Project,Value=$PROJECT_TAG}]" \
            --query GroupId --output text)
    fi
    if ! aws ec2 describe-security-groups --group-ids "$sg" \
            --query "SecurityGroups[0].IpPermissionsEgress[?IpProtocol=='-1'].IpRanges[].CidrIp" --output text | grep -q '0.0.0.0/0'; then
        aws ec2 authorize-security-group-egress --group-id "$sg" \
            --ip-permissions 'IpProtocol=-1,IpRanges=[{CidrIp=0.0.0.0/0}]' >/dev/null
    fi
    echo "$sg"
}

latest_ami() {
    aws ec2 describe-images --owners amazon \
        --filters "Name=name,Values=$AMI_NAME_PATTERN" Name=state,Values=available \
        --query 'sort_by(Images,&CreationDate)[-1].[ImageId,RootDeviceName]' --output text
}

# Prints FILE's path relative to the repo root. FILE may be repo-relative (like
# --config), relative to the current directory, or absolute. Fails if it is not
# an existing file inside the repository.
repo_relative() {
    local f=$1 abs
    [ -f "$REPO_ROOT/$f" ] && f="$REPO_ROOT/$f"
    [ -f "$f" ] || return 1
    abs="$(cd "$(dirname "$f")" && pwd)/$(basename "$f")"
    case "$abs" in "$REPO_ROOT"/*) printf '%s\n' "${abs#"$REPO_ROOT"/}" ;; *) return 1 ;; esac
}

# ---------------------------------------------------------------- launch

# Tries TYPE in every zone of AZS (comma-separated). Success: sets LAUNCHED_ID, returns 0.
# Returns 1 when every zone lacks capacity or the vCPU quota is in use (LAST_ERROR set).
# Dies on any other error. Uses cmd_launch's ami, root_dev, sg, disk, tmp and run_id.
launch_type_in_zones() {
    local type=$1 vcpus=$2 azs=$3 az subnet out="" attempt
    for az in ${azs//,/ }; do
        subnet=$(aws ec2 describe-subnets --filters Name=default-for-az,Values=true "Name=availability-zone,Values=$az" \
            --query 'Subnets[0].SubnetId' --output text)
        [ -n "$subnet" ] && [ "$subnet" != "None" ] || continue
        for attempt in 1 2 3 4 5 6; do
            if out=$(aws ec2 run-instances \
                    --image-id "$ami" --instance-type "$type" --subnet-id "$subnet" --security-group-ids "$sg" \
                    --iam-instance-profile "Name=$ROLE_NAME" \
                    --instance-initiated-shutdown-behavior terminate \
                    --metadata-options HttpTokens=required,HttpEndpoint=enabled \
                    --block-device-mappings "DeviceName=$root_dev,Ebs={VolumeSize=$disk,VolumeType=gp3,DeleteOnTermination=true}" \
                    --user-data "file://$tmp/user-data.sh" \
                    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=llm-ctf-$run_id},{Key=Project,Value=$PROJECT_TAG},{Key=RunId,Value=$run_id}]" \
                    --query 'Instances[0].InstanceId' --output text 2>&1); then
                LAUNCHED_ID=$out; return 0
            fi
            case "$out" in
                *"Invalid IAM Instance Profile"*|*iamInstanceProfile*)   # new IAM profile still propagating
                    [ "$attempt" -lt 6 ] || die "IAM instance profile $ROLE_NAME still not usable after 60 s: $out"
                    sleep 10 ;;
                *InsufficientInstanceCapacity*|*Unsupported*)
                    log "No capacity for $type in $az"; continue 2 ;;
                *VcpuLimitExceeded*)   # account-wide, so no point trying other zones
                    log "$type needs $vcpus vCPUs, but the '$(vcpu_quota_name "$type")' quota is used by other running instances (aws/ctf-aws.sh status lists ours); treating as no capacity"
                    LAST_ERROR=$out; return 1 ;;
                *) die "$out" ;;
            esac
        done
    done
    LAST_ERROR=${out:-"no default subnet in any zone that offers $type"}
    return 1
}

cmd_launch() {
    local gpu_mem="" model="Qwen/Qwen2.5-7B-Instruct-AWQ" served="" config="config/qwen_baseline.yaml"
    local challenges="2016q-cry-sleeping_guard" split="development" max_len="" max_hours=12
    local disk=200 itype="" keep=0 yes=0 dry=0 job_cmd="" wait_capacity=0
    local challenges_file="" challenges_given=0 n_chal=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --gpu-mem) gpu_mem=$2; shift 2 ;;
            --model) model=$2; shift 2 ;;
            --served-name) served=$2; shift 2 ;;
            --config) config=$2; shift 2 ;;
            --challenges) challenges=$2; challenges_given=1; shift 2 ;;
            --challenges-file) challenges_file=$2; shift 2 ;;
            --split) split=$2; shift 2 ;;
            --max-model-len) max_len=$2; shift 2 ;;
            --max-hours) max_hours=$2; shift 2 ;;
            --disk) disk=$2; shift 2 ;;
            --instance-type) itype=$2; shift 2 ;;
            --wait-capacity) wait_capacity=$2; shift 2 ;;
            --keep) keep=1; shift ;;
            -y|--yes) yes=1; shift ;;
            --dry-run) dry=1; shift ;;
            --) shift; job_cmd=$(printf '%q ' "$@"); break ;;
            -h|--help) usage; exit 0 ;;
            *) die "unknown option for launch: $1 (see --help)" ;;
        esac
    done

    [ -n "$gpu_mem" ] || [ -n "$itype" ] || die "--gpu-mem is required (e.g. --gpu-mem 24)"
    case "$gpu_mem" in ''|*[!0-9]*) [ -n "$itype" ] || die "--gpu-mem must be a whole number of GB" ;; esac
    case "$max_hours" in ''|*[!0-9]*|0) die "--max-hours must be a positive whole number" ;; esac
    case "$wait_capacity" in ''|*[!0-9]*) die "--wait-capacity must be a whole number of minutes" ;; esac
    [ -n "$served" ] || served="${model%-AWQ}"
    [ -f "$REPO_ROOT/$config" ] || die "config file not found: $config"
    if [ -n "$challenges_file" ]; then
        local rel bad
        [ "$challenges_given" = 0 ] || die "--challenges and --challenges-file are mutually exclusive"
        rel=$(repo_relative "$challenges_file") \
            || die "challenge file not found or outside the repository: $challenges_file (it must be inside the repo, which is uploaded to the instance)"
        challenges_file=$rel
        # The upload takes what `git ls-files -co --exclude-standard` lists, so an ignored file would be missing.
        ( cd "$REPO_ROOT" && git ls-files -z -co --exclude-standard -- "$challenges_file" | tr '\0' '\n' | grep -qxF "$challenges_file" ) \
            || die "$challenges_file is excluded by .gitignore, so it would not be uploaded to the instance"
        # Same reading rules as aws/remote/job.sh: # comments and blank lines are skipped, CR is dropped.
        bad=$(grep -n -v '^[[:space:]]*#' "$REPO_ROOT/$challenges_file" | tr -d '\r' | grep -v '^[0-9]*:$' | grep '[[:space:]]' | head -1 || true)
        [ -z "$bad" ] || die "$challenges_file line ${bad%%:*}: challenge names must not contain whitespace: '${bad#*:}'"
        n_chal=$(grep -v '^[[:space:]]*#' "$REPO_ROOT/$challenges_file" | tr -d '\r' | grep -c . || true)
        [ "$n_chal" -gt 0 ] || die "no challenge names in $challenges_file"
    fi
    grep -qF "\"$served\"" "$REPO_ROOT/llm_ctf/backends/vllm_backend.py" \
        || die "'$served' is not in VLLMBackend.MODELS (llm_ctf/backends/vllm_backend.py). Add it there, or pass --served-name."

    need_aws

    # Pick the instance types: every candidate that is offered here and fits the vCPU quota,
    # cheapest first. The first one is launched; the others are fallbacks.
    local candidates
    if [ -n "$itype" ]; then
        candidates=$(aws ec2 describe-instance-types --instance-types "$itype" \
            --query 'InstanceTypes[0].[GpuInfo.Gpus[0].MemoryInfo.SizeInMiB,VCpuInfo.DefaultVCpus,InstanceType,GpuInfo.Gpus[0].Name]' \
            --output text) || die "unknown instance type $itype"
    else
        # 10% tolerance: an A10G reports 22888 MiB but is sold as a 24 GB GPU.
        candidates=$(gpu_instance_candidates $(( gpu_mem * 1024 * 9 / 10 )))
        [ -n "$candidates" ] || die "no single-GPU instance type in $REGION has ${gpu_mem} GB of GPU memory"
    fi

    # usable_types: "type1 type2 ..."; usable_info: one "type mib vcpus az1,az2,... gpu name" line
    # per type (the GPU name can contain spaces, so it stays last).
    local usable_types="" usable_info="" skipped="" mib vcpus t gname quota azs
    while read -r mib vcpus t gname; do
        [ -n "$t" ] || continue
        azs=$(availability_zones_for "$t" </dev/null | tr '\t\n' ',,' | sed 's/,*$//')
        if [ -z "$azs" ]; then skipped="$skipped $t(not offered in $REGION)"; continue; fi
        quota=$(vcpu_quota "$t" </dev/null)
        if [ -n "$quota" ] && [ "$quota" != "None" ] && [ "$vcpus" -gt "$quota" ]; then
            skipped="$skipped $t(needs $vcpus vCPUs, quota is $quota)"; continue
        fi
        usable_types="${usable_types:+$usable_types }$t"
        usable_info="$usable_info$t $mib $vcpus $azs $gname
"
    done <<EOF
$candidates
EOF
    [ -n "$usable_types" ] || die "no usable instance type. Skipped:$skipped. Request a quota increase in the Service Quotas console, or try another AWS_REGION."
    local primary_type gpu_desc fallbacks=""
    primary_type=${usable_types%% *}
    read -r t mib vcpus azs gname <<< "$usable_info"   # first line
    gpu_desc="1x $gname ($(( mib / 1024 )) GiB)"
    case "$usable_types" in *" "*) fallbacks=$(echo "${usable_types#* }" | sed 's/ /, /g') ;; esac

    local ami_info ami root_dev
    ami_info=$(latest_ami); ami=$(echo "$ami_info" | cut -f1); root_dev=$(echo "$ami_info" | cut -f2)
    [ -n "$ami" ] && [ "$ami" != "None" ] || die "could not find the AWS Deep Learning AMI in $REGION"

    local run_id job_desc
    run_id="$(date -u +%Y%m%d-%H%M%S)-$(printf '%04x' $RANDOM)"
    if [ -n "$job_cmd" ]; then job_desc=$job_cmd
    elif [ -n "$challenges_file" ]; then job_desc="challenges: $n_chal from $challenges_file ($split split)"
    else job_desc="challenges: $challenges ($split split)"
    fi

    {
        echo
        echo "  Run ID        $run_id"
        echo "  Region        $REGION"
        echo "  Instance      $primary_type, $gpu_desc, on-demand"
        if [ -n "$fallbacks" ]; then
            echo "  Fallbacks     $fallbacks  (tried in this order if $primary_type has no capacity; a later type can cost more)"
        fi
        if [ "$wait_capacity" -gt 0 ]; then
            echo "  Wait          up to ${wait_capacity} min for capacity, retrying every 5 min"
        fi
        echo "  Model         $model  (served as $served)"
        echo "  Job           $job_desc"
        echo "  Config        $config"
        echo "  Time limit    ${max_hours}h, then the instance terminates"
        echo "  Outputs       s3://$BUCKET/runs/$run_id/"
        if [ -n "$skipped" ]; then echo "  Skipped      $skipped"; fi
        echo
    } >&2
    if [ "$dry" = 1 ]; then log "Dry run: nothing launched."; return; fi
    if [ "$yes" = 0 ] && [ -t 0 ]; then
        printf 'Launch? This starts a billed GPU instance. [y/N] ' >&2
        local answer; read -r answer
        case "$answer" in y|Y|yes) ;; *) die "cancelled" ;; esac
    fi

    local vpc sg
    vpc=$(aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)
    [ -n "$vpc" ] && [ "$vpc" != "None" ] || die "no default VPC in $REGION. Create one with: aws ec2 create-default-vpc"
    ensure_bucket
    ensure_instance_profile
    sg=$(ensure_security_group "$vpc")

    # Package the working tree (tracked + untracked files, minus .gitignore'd ones such as keys.cfg).
    tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT   # global: the trap runs after this function returns
    log "Uploading code"
    ( cd "$REPO_ROOT"
      git ls-files -z -co --exclude-standard |
          while IFS= read -r -d '' f; do [ -e "$f" ] && printf '%s\0' "$f"; done |
          COPYFILE_DISABLE=1 tar --null -T - -czf "$tmp/code.tar.gz" )
    aws s3 cp "$tmp/code.tar.gz" "s3://$BUCKET/runs/$run_id/code.tar.gz" --only-show-errors

    # Settings for the instance, written as a shell-sourceable env file.
    {
        printf 'RUN_ID=%q\n' "$run_id"
        printf 'BUCKET=%q\n' "$BUCKET"
        printf 'AWS_DEFAULT_REGION=%q\n' "$REGION"
        printf 'MODEL=%q\n' "$model"
        printf 'SERVED_NAME=%q\n' "$served"
        printf 'AGENT_CONFIG=%q\n' "$config"
        printf 'CHALLENGES=%q\n' "$challenges"
        printf 'CHALLENGES_FILE=%q\n' "$challenges_file"
        printf 'SPLIT=%q\n' "$split"
        printf 'MAX_MODEL_LEN=%q\n' "$max_len"
        printf 'VLLM_IMAGE=%q\n' "${VLLM_IMAGE:-$VLLM_IMAGE_DEFAULT}"
        printf 'KEEP_INSTANCE=%q\n' "$keep"
        printf 'JOB_CMD=%q\n' "$job_cmd"
        printf 'HF_TOKEN=%q\n' "${HF_TOKEN:-}"
    } > "$tmp/run.env"

    {
        echo '#!/bin/bash'
        echo "shutdown -h +$(( max_hours * 60 ))   # hard time limit; shutdown terminates the instance"
        echo "install -m 640 -o root -g ubuntu /dev/null /etc/llm-ctf-run.env"
        echo "cat > /etc/llm-ctf-run.env <<'RUNENV'"
        cat "$tmp/run.env"
        echo "RUNENV"
        cat "$REPO_ROOT/aws/remote/user-data.sh"
    } > "$tmp/user-data.sh"

    # Launch: each usable type in turn, each in every zone that offers it. With
    # --wait-capacity, the whole list is tried again every 5 minutes.
    local pass=0 max_passes=$(( 1 + (wait_capacity + 4) / 5 )) line instance_id="" inst_type=""
    while :; do
        pass=$((pass + 1))
        for t in $usable_types; do
            line=$(printf '%s' "$usable_info" | awk -v t="$t" '$1 == t')
            [ -n "$line" ] || die "internal error: no record for $t"
            read -r t mib vcpus azs gname <<< "$line"
            log "Launching $t"
            if launch_type_in_zones "$t" "$vcpus" "$azs"; then instance_id=$LAUNCHED_ID; inst_type=$t; break; fi
        done
        [ -z "$instance_id" ] || break
        [ "$pass" -lt "$max_passes" ] \
            || die "no capacity for $(echo "$usable_types" | sed 's/ /, /g') in $REGION. Last error: ${LAST_ERROR%.}. Retry later, pass --wait-capacity MIN, or try another AWS_REGION."
        log "No capacity for any type. Attempt $((pass + 1)) of $max_passes at $(utc_clock $(( $(date +%s) + 300 ))) UTC (Ctrl-C stops waiting; nothing is running yet)"
        sleep 300
    done
    [ "$inst_type" = "$primary_type" ] || log "Launched on fallback type $inst_type ($primary_type had no capacity)"
    printf 'LAUNCHED %s %s\n' "$instance_id" "$inst_type" | aws s3 cp - "s3://$BUCKET/runs/$run_id/status" --only-show-errors

    cat >&2 <<EOF

Launched $instance_id. Setup (model download, Docker image build) takes about 20-30 minutes.

  Follow the log:   aws/ctf-aws.sh logs $run_id -f
  Check status:     aws/ctf-aws.sh status $run_id
  Get results:      aws/ctf-aws.sh results $run_id
  Stop early:       aws/ctf-aws.sh stop $run_id
EOF
    echo "$run_id"
}

# ---------------------------------------------------------------- monitoring

run_instances() {  # instance id, type, state, launch time for a run
    aws ec2 describe-instances --filters "Name=tag:RunId,Values=$1" \
        --query 'Reservations[].Instances[].[InstanceId,InstanceType,State.Name,LaunchTime]' --output text
}

run_status() {
    aws s3 cp "s3://$BUCKET/runs/$1/status" - 2>/dev/null || echo "unknown"
}

cmd_status() {
    need_aws
    if [ $# -ge 1 ]; then
        echo "Job:      $(run_status "$1")"
        echo "Instance: $(run_instances "$1" | tr '\t' ' ')"
        return
    fi
    local rows
    rows=$(aws ec2 describe-instances \
        --filters "Name=tag:Project,Values=$PROJECT_TAG" Name=instance-state-name,Values=pending,running,stopping,shutting-down \
        --query 'Reservations[].Instances[].[Tags[?Key==`RunId`]|[0].Value,InstanceType,State.Name,LaunchTime]' --output text)
    if [ -z "$rows" ]; then echo "No active runs in $REGION."; return; fi
    printf '%-24s %-14s %-10s %-26s %s\n' RUN_ID TYPE STATE LAUNCHED JOB
    echo "$rows" | while read -r rid t st lt; do
        printf '%-24s %-14s %-10s %-26s %s\n' "$rid" "$t" "$st" "$lt" "$(run_status "$rid")"
    done
}

cmd_logs() {
    [ $# -ge 1 ] || die "usage: logs RUN_ID [-f]"
    need_aws
    local run_id=$1 follow=0 shown=0
    [ "${2:-}" = "-f" ] && follow=1
    tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT   # global, see cmd_launch
    while true; do
        if aws s3 cp "s3://$BUCKET/runs/$run_id/outputs/job.log" "$tmp" --only-show-errors 2>/dev/null; then
            local size; size=$(wc -c < "$tmp" | tr -d ' ')
            if [ "$size" -gt "$shown" ]; then tail -c +"$(( shown + 1 ))" "$tmp"; shown=$size; fi
        fi
        [ "$follow" = 1 ] || break
        case "$(run_status "$run_id")" in
            DONE*|FAILED*) echo; echo "[job finished: $(run_status "$run_id")]"; break ;;
        esac
        if [ -z "$(run_instances "$run_id" | grep -E 'pending|running')" ] && [ "$shown" -gt 0 ]; then
            sleep 20  # allow the final upload to land, then print the rest
            aws s3 cp "s3://$BUCKET/runs/$run_id/outputs/job.log" "$tmp" --only-show-errors 2>/dev/null && tail -c +"$(( shown + 1 ))" "$tmp"
            echo; echo "[instance is no longer running: $(run_status "$run_id")]"; break
        fi
        sleep 20
    done
}

cmd_results() {
    [ $# -ge 1 ] || die "usage: results RUN_ID"
    need_aws
    local dest="$REPO_ROOT/aws-runs/$1"
    mkdir -p "$dest"
    aws s3 sync "s3://$BUCKET/runs/$1/outputs" "$dest" --only-show-errors
    echo "Job: $(run_status "$1")"
    echo "Downloaded to $dest"
}

cmd_stop() {
    [ $# -ge 1 ] || die "usage: stop RUN_ID"
    need_aws
    local ids
    ids=$(aws ec2 describe-instances --filters "Name=tag:RunId,Values=$1" \
        Name=instance-state-name,Values=pending,running,stopping,stopped \
        --query 'Reservations[].Instances[].InstanceId' --output text)
    [ -n "$ids" ] || { echo "No running instance for $1."; return; }
    aws ec2 terminate-instances --instance-ids $ids --query 'TerminatingInstances[].[InstanceId,CurrentState.Name]' --output text
    printf 'FAILED stopped by user\n' | aws s3 cp - "s3://$BUCKET/runs/$1/status" --only-show-errors || true
    echo "Outputs uploaded so far: aws/ctf-aws.sh results $1"
}

case "${1:-}" in
    launch) shift; cmd_launch "$@" ;;
    status|list) shift; cmd_status "$@" ;;
    logs) shift; cmd_logs "$@" ;;
    results) shift; cmd_results "$@" ;;
    stop) shift; cmd_stop "$@" ;;
    -h|--help|help|"") usage ;;
    *) die "unknown command: $1 (see --help)" ;;
esac
