# shellcheck shell=bash
# Appended to the EC2 user-data by aws/ctf-aws.sh, after the run settings
# have been written to /etc/llm-ctf-run.env. Runs as root on first boot.
set -a; . /etc/llm-ctf-run.env; set +a

command -v aws >/dev/null 2>&1 || { apt-get update -q && apt-get install -y -q awscli; }

mkdir -p /home/ubuntu/repo
for _ in $(seq 1 30); do
    aws s3 cp "s3://$BUCKET/runs/$RUN_ID/code.tar.gz" /tmp/code.tar.gz --only-show-errors && break
    sleep 10
done
tar -xzf /tmp/code.tar.gz -C /home/ubuntu/repo
chown -R ubuntu:ubuntu /home/ubuntu/repo

exec bash /home/ubuntu/repo/aws/remote/bootstrap.sh
