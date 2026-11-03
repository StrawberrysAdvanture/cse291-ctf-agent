# shellcheck shell=bash
# Shared helpers for the scripts that run on the EC2 instance.
set -a; . /etc/llm-ctf-run.env; set +a

S3_RUN="s3://${BUCKET}/runs/${RUN_ID}"
OUT=/home/ubuntu/outputs

set_status() {
    echo "[$(date -u +%H:%M:%S)] >>> $*"
    printf '%s\n' "$*" | aws s3 cp - "$S3_RUN/status" --only-show-errors || true
}

sync_outputs() {
    aws s3 sync "$OUT" "$S3_RUN/outputs" --only-show-errors >/dev/null 2>&1 || true
}
