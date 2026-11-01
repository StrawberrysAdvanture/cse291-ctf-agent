# AWS usage log (account 432797645771, us-west-2)

Billing data lags 12–16 hours, so spending is tracked here by hand.
Limits: $3/day and $50 total. Disk: 60 GB gp3, about $0.16/day while it exists.

| Session start (UTC) | Stop (UTC) | Instance type | Rate | Hours | Est. cost | Purpose |
|---|---|---|---|---|---|---|
| 2026-11-01 06:40 | 2026-11-01 08:26 | t3.medium | $0.0416/hr | 1.77 | $0.07 | Instance setup |
| 2026-11-01 08:27 | 2026-11-01 11:27 | g6.xlarge | $0.81/hr | 3.00 | $2.43 | vLLM, pilot (3 attempts); auto-stopped |
| 2026-11-01 19:23 | 2026-11-01 20:57 | g6.xlarge | $0.81/hr | 1.57 | $1.27 | Dev set (30 attempts); stopped early (over $3/day) |
