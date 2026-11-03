# shellcheck shell=bash
# Appended to the EC2 user-data by aws/ctf-aws.sh, after the run settings
# have been written to /etc/llm-ctf-run.env. Runs as root on first boot.
set -a; . /etc/llm-ctf-run.env; set +a

fail() {
    echo "llm-ctf bootstrap failed: $*"
    command -v aws >/dev/null 2>&1 && printf 'FAILED %s\n' "$*" \
        | aws s3 cp - "s3://$BUCKET/runs/$RUN_ID/status" --only-show-errors
    shutdown -h now
    exit 1
}

if ! command -v aws >/dev/null 2>&1; then
    apt-get -o DPkg::Lock::Timeout=600 update -q &&
        apt-get -o DPkg::Lock::Timeout=600 install -y -q awscli || fail "could not install the AWS CLI"
fi

mkdir -p /home/ubuntu/repo
for _ in $(seq 1 30); do
    aws s3 cp "s3://$BUCKET/runs/$RUN_ID/code.tar.gz" /tmp/code.tar.gz --only-show-errors && break
    sleep 10
done
tar -xzf /tmp/code.tar.gz -C /home/ubuntu/repo || fail "could not download the code from S3"
chown -R ubuntu:ubuntu /home/ubuntu/repo

exec bash /home/ubuntu/repo/aws/remote/bootstrap.sh
