Baseline: Qwen2.5-7B-Instruct-AWQ, vLLM 0.10.1.1, 32768-token context, config/qwen_baseline.yaml (max_rounds 3), all 57 development challenges, g6.xlarge (L4 24 GB), commit d5067ae.
Launched 2026-11-03 23:35 UTC; challenges 00:04-01:11 UTC; instance terminated 01:1x.
10 challenges crashed before the first model call because their service publishes host port 8000, which vLLM occupied (issue I10, fixed in e31db05); they are listed in crashed-challenges.txt and have no conversation log.
