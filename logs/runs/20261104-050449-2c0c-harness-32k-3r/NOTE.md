# Harness-fixes run: `config/qwen_harness.yaml`, 57 development challenges, 3 rounds, 32k context

Same model, instance type and challenge list as the baseline record
(`logs/runs/20261103-233511-6b9b-baseline-32k-3r/`), with the harness options on:
`repeat_detection: note`, `no_tool_nudge: true`, `lenient_tool_calls: true`,
`max_output_chars: 20000` (`stop_after_first_call` off). Qwen2.5-7B-Instruct-AWQ served by
vLLM on a `g6.xlarge` (L4, 24 GB), `--max-model-len 32768`.

The record was produced in two parts with identical agent code and configuration:

| Part | Run ID | Challenges | Code | Timing (UTC, 2026-11-04) |
|---|---|---|---|---|
| 1 | `20261104-050449-2c0c` | 1–20 (`challenges.txt` order) | `3db03b3` | challenges 05:31–05:56; hung on challenge 21 (`2016f-rev-palo_alto`) in a Ghidra decompile with no time limit (issue I14); instance terminated at 06:52 |
| 2 | `20261104-065735-636e` | 21–57 (`config/challenges/rerun-2c0c-remaining.txt`) | `bc99a85` (adds the I14 time limits only) | challenges 07:31–08:54, `DONE exit=0`, instance terminated 09:00 |

Part 1's transcript for `2016f-rev-palo_alto` was never written (the process was killed with the
instance); the challenge ran in full in part 2. The I13 fix (dropping truncated invoke fragments)
is in neither part, so `tool_call_parsing.dropped_invokes` is absent from these logs and the
`Unknown tool ` results it would have prevented are present.

Files:

- `logs/Qwen7B_Harness_0/*.json`: the 57 conversation logs (20 from part 1, 37 from part 2).
- `report.md`: `scripts/run_report.py` over the 57 logs.
- `paired-vs-baseline.md`: `scripts/paired_analysis.py` against the baseline record.
- `findings.md`: transcript review of what changed against the baseline.
- `summary.txt`: per-challenge solved / rounds / finish reason / seconds over the merged logs.
- `job-excerpt.log`: the stage lines of both job logs (setup, per-challenge start times, finish).
- `run_config.yaml`, `challenges.txt`, `nvidia-smi.txt`: as uploaded by the runner.

AWS cost of the record: part 1 about 1h50m including the 50 idle minutes of the hang, part 2
about 2h, both on `g6.xlarge` at about $0.80/h, about $3.1 in total.
