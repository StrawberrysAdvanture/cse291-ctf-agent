#!/bin/bash
# Runs as root on the instance. Installs system packages, runs job.sh as the
# `ubuntu` user, keeps S3 up to date, and terminates the instance at the end.
set -uo pipefail
. "$(dirname "$0")/common.sh"

mkdir -p "$OUT" && chown ubuntu:ubuntu "$OUT"
exec > >(tee -a "$OUT/job.log") 2>&1

finish() {
    local rc=$?
    docker logs vllm > "$OUT/vllm.log" 2>&1 || true
    nvidia-smi > "$OUT/nvidia-smi.txt" 2>&1 || true
    if [ "$rc" = 0 ]; then set_status "DONE exit=0"; else set_status "FAILED exit=$rc (see job.log)"; fi
    sync_outputs
    if [ "${KEEP_INSTANCE:-0}" = 1 ]; then
        echo "--keep was set; the instance stays up until the --max-hours limit."
    else
        echo "Terminating instance."
        shutdown -h now
    fi
}
trap finish EXIT

# Upload outputs every 30 s so `ctf-aws.sh logs -f` can follow along.
( while sleep 30; do sync_outputs; done ) &

set_status "SETUP system packages"
apt-get -o DPkg::Lock::Timeout=600 update -q
apt-get -o DPkg::Lock::Timeout=600 install -y -q python3-venv python3-pip unzip wget git curl
usermod -aG docker ubuntu

sudo -u ubuntu -H bash /home/ubuntu/repo/aws/remote/job.sh
