# AWS usage log (account 432797645771, us-west-2)

Billing data lags 12–16 hours, so spending is tracked here by hand.
Limits (AWS Educate dashboard): $20/day, $200 total. Daily usage resets on the dashboard's own ~daily boundary (BurnRate is an estimate, not a real-time ledger). Disk: 60 GB gp3, about $0.16/day while it exists.

| Session start (UTC) | Stop (UTC) | Instance type | Rate | Hours | Est. cost | Purpose |
|---|---|---|---|---|---|---|
| 2026-11-01 06:40 | 2026-11-01 08:26 | t3.medium | $0.0416/hr | 1.77 | $0.07 | Instance setup |
| 2026-11-01 08:27 | 2026-11-01 11:27 | g6.xlarge | $0.81/hr | 3.00 | $2.43 | vLLM, pilot (3 attempts); auto-stopped |
| 2026-11-01 19:23 | 2026-11-01 20:57 | g6.xlarge | $0.81/hr | 1.57 | $1.27 | Dev set (30 attempts); stopped early (over $3/day) |
| 2026-11-01 23:43 | 2026-11-02 04:12 | g6.xlarge | $0.81/hr | 4.48 | $3.61 | Held-out (60) + 5 dev re-runs + ShreeRamQuest; stopped |
| 2026-11-02 06:50 | 2026-11-02 08:42 | g6.xlarge | $0.81/hr | 1.87 | $1.51 | Uniform dev re-run (30 attempts on fixed commit); stopped |

## Reconciliation (CloudTrail, 2026-11-03)
Billable windows verified against CloudTrail Start/Stop events (OS `shutdown -h`
stops leave no StopInstances event, so pilot + held-out ends are from wall-clock).
Total incurred to date: ~10.9 g6 hrs x $0.8048 = ~$8.8 compute + $0.07 t3 + ~$0.5 EBS
= **~$9.4**. The AWS Educate dashboard on 2026-11-03 showed only $0.95 recorded
(g6 $0.80 = ~1 hr of ~11 run) -- billing lag, not the final figure; it will
converge upward as usage propagates. Well under $20/day and $200 total.
