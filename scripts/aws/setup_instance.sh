#!/usr/bin/env bash
# One-time setup of the Phase 1 instance (plain Ubuntu 22.04). Run as the ubuntu user,
# from ~/project/cse291-ctf-agent, on a cheap CPU instance; then change the instance
# type to g6.xlarge. Safe to re-run: every step skips work that is already done.
set -euo pipefail

PROJECT=~/project
REPO=$PROJECT/cse291-ctf-agent
cd "$REPO"

echo "== system packages"
sudo apt-get update -q
sudo DEBIAN_FRONTEND=noninteractive apt-get install -yq \
    build-essential git curl unzip jq rsync \
    docker.io docker-compose-v2 openjdk-17-jdk-headless \
    nvidia-headless-550-server nvidia-utils-550-server
sudo usermod -aG docker ubuntu

echo "== auto-stop: power off 3 hours after every boot (shutdown behavior is 'stop')"
sudo tee /etc/systemd/system/autostop.service > /dev/null <<'UNIT'
[Unit]
Description=Stop the instance 180 minutes after boot to stay under the daily budget
[Service]
Type=oneshot
ExecStart=/sbin/shutdown -h +180 "auto-stop: 3 hours since boot"
[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable autostop.service

echo "== python environments (uv-managed Python 3.11)"
command -v uv > /dev/null || curl -LsSf https://astral.sh/uv/0.5.11/install.sh | sh
export PATH=$HOME/.local/bin:$PATH
[ -d $PROJECT/ctf-env ] || uv venv --python 3.11 $PROJECT/ctf-env
VIRTUAL_ENV=$PROJECT/ctf-env uv pip install -q -r requirements.txt
[ -d $PROJECT/vllm-env ] || uv venv --python 3.11 $PROJECT/vllm-env
VIRTUAL_ENV=$PROJECT/vllm-env uv pip install -q 'vllm==0.7.3' 'transformers==4.49.0' 'huggingface_hub[cli]'

echo "== model weights (pinned revision)"
$PROJECT/vllm-env/bin/huggingface-cli download Qwen/Qwen2.5-7B-Instruct-AWQ \
    --revision b25037543e9394b818fdfca67ab2a00ecc7dd641 > /dev/null

echo "== dataset"
$PROJECT/ctf-env/bin/python -c "from nyuctf.dataset import CTFDataset; CTFDataset(split='development'); CTFDataset(split='test')"

echo "== agent image, internal network, Ghidra"
sg docker -c ./setup.sh

echo "== challenge server images for the Phase 1 subset"
sg docker -c "$PROJECT/ctf-env/bin/python scripts/aws/pull_challenge_images.py"

echo "setup complete"
