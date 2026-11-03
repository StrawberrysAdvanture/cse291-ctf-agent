# Work logs

Records of what was done on this fork, when, and with which runs, so the work can be
followed and reproduced.

| File | Contents |
|---|---|
| `worklog-2026-11-03.md` | Timestamped log of the session that added the AWS runner, the action memory, and the first paired experiment |
| `runs/<run-id>-<name>/` | Records of experiment runs: the agent config, per-challenge conversation logs (`logs/*.json`), `summary.txt`, and the instance's `job.log` |

Run IDs are the `RUN_ID` values printed by `aws/ctf-aws.sh launch` (UTC timestamp plus a random suffix). The same ID tags the EC2 instance and names the S3 prefix `s3://llm-ctf-runs-<account>-us-west-2/runs/<run-id>/`.

Local conversation logs that `llm_ctf_solve.py` writes under `logs/<user>/` are ignored by Git; only this README, the work logs, and `runs/` are tracked.
