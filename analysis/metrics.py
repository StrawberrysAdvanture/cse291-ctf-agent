"""Phase 1 execution metrics, per attempt and aggregated.

Per attempt: solved (from score.py's rule), wall time, model/tool time, tokens, rounds,
context growth, tool calls, invalid commands, repeated ineffective actions, timeouts.

Definitions used in the report:
  invalid command      a tool call that returned an error, or run_command with a
                       non-zero exit code, or a model reply whose tool call could not be parsed
  repeated action      a tool call with the same tool and arguments as an earlier call
                       in the same attempt
  ineffective repeat   a repeated action whose output is identical to the earlier one
  context growth       prompt tokens of the last round minus the first round
  timeout              a command that hit its time limit, or an attempt ended by max_time

Usage: python analysis/metrics.py LOGROOT DATASET_DIR [-o OUTDIR]
"""
import argparse
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path

from traces import load_attempts, true_flag
from score import score


def normalize(arguments):
    if isinstance(arguments, dict):
        return json.dumps({k: (" ".join(v.split()) if isinstance(v, str) else v)
                           for k, v in sorted(arguments.items())})
    return " ".join(str(arguments).split())


def repeats(events):
    seen = {}
    repeated = ineffective = 0
    for ev in events:
        key = (ev.name, normalize(ev.arguments))
        if key in seen:
            repeated += 1
            if seen[key] == ev.output:
                ineffective += 1
        seen[key] = ev.output
    return repeated, ineffective


def attempt_metrics(a, flag):
    s = score(a, flag)
    rounds = a.rounds
    prompt = [r["prompt_tokens"] for r in rounds if r.get("prompt_tokens") is not None]
    completion = [r["completion_tokens"] for r in rounds if r.get("completion_tokens") is not None]
    rep, ineff = repeats(a.events)
    invalid = sum(1 for e in a.events if e.error or (e.name == "run_command" and e.returncode not in (None, 0)))
    invalid += sum(1 for r in rounds if r.get("tool_extract_error"))
    rt = a.log.get("runtime", {})
    return {
        "set": a.set, "challenge": a.challenge, "category": a.category, "attempt": a.attempt,
        "solved": s["solved"], "flag_origin": s["flag_origin"], "finish_reason": a.log.get("finish_reason"),
        "rounds": len(rounds),
        "wall_time_s": round(rt.get("total", 0), 1),
        "model_time_s": round(rt.get("model", 0), 1),
        "tool_time_s": round(rt.get("tools", 0), 1),
        "prompt_tokens": sum(prompt),
        "completion_tokens": sum(completion),
        "first_context_tokens": prompt[0] if prompt else 0,
        "max_context_tokens": max(prompt, default=0),
        "context_growth_tokens": (prompt[-1] - prompt[0]) if prompt else 0,
        "tool_calls": len(a.events),
        "invalid_commands": invalid,
        "unparsed_tool_calls": sum(1 for r in rounds if r.get("tool_extract_error")),
        "rounds_without_tool_call": sum(1 for r in rounds if not r.get("tool_calls")),
        "truncated_rounds": sum(1 for r in rounds if r.get("truncated")),
        "repeated_actions": rep,
        "ineffective_repeats": ineff,
        "command_timeouts": sum(1 for e in a.events if e.timed_out),
        "attempt_timeout": a.log.get("finish_reason") == "max_time",
        "median_tool_latency_s": round(statistics.median([e.latency for e in a.events]), 3) if a.events else None,
    }


def write_csv(path, rows):
    if not rows:
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def aggregate(rows, key):
    groups = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    out = []
    for k, g in sorted(groups.items()):
        n = len(g)
        mean = lambda f: round(sum(r[f] for r in g) / n, 1)
        out.append({
            "group": k, "attempts": n, "challenges": len({r["challenge"] for r in g}),
            "solved_attempts": sum(r["solved"] for r in g),
            "solve_rate": round(sum(r["solved"] for r in g) / n, 3),
            "challenges_solved_any": len({r["challenge"] for r in g if r["solved"]}),
            "mean_rounds": mean("rounds"), "mean_wall_time_s": mean("wall_time_s"),
            "mean_prompt_tokens": mean("prompt_tokens"), "mean_completion_tokens": mean("completion_tokens"),
            "mean_max_context_tokens": mean("max_context_tokens"),
            "mean_invalid_commands": mean("invalid_commands"), "mean_ineffective_repeats": mean("ineffective_repeats"),
            "mean_truncated_rounds": mean("truncated_rounds"),
            "command_timeouts": sum(r["command_timeouts"] for r in g),
            "attempt_timeouts": sum(r["attempt_timeout"] for r in g),
            "finish_reasons": json.dumps(dict(sorted(
                {fr: sum(1 for r in g if r["finish_reason"] == fr) for fr in {r["finish_reason"] for r in g}}.items()))),
        })
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logroot", type=Path)
    ap.add_argument("dataset_dir", type=Path)
    ap.add_argument("-o", "--outdir", type=Path, default=Path("."))
    args = ap.parse_args()
    args.outdir.mkdir(parents=True, exist_ok=True)

    attempts = list(load_attempts(args.logroot))
    rows = [attempt_metrics(a, true_flag(args.dataset_dir, a)) for a in attempts]
    write_csv(args.outdir / "attempts.csv", rows)
    by_set = aggregate(rows, lambda r: r["set"])
    by_cat = aggregate(rows, lambda r: f'{r["set"]}/{r["category"]}')
    write_csv(args.outdir / "by_set.csv", by_set)
    write_csv(args.outdir / "by_category.csv", by_cat)

    # Per-tool latency and per-round context, for figures
    tool_rows, ctx_rows = [], []
    for a in attempts:
        for e in a.events:
            tool_rows.append({"set": a.set, "challenge": a.challenge, "attempt": a.attempt, "round": e.round,
                              "tool": e.name, "latency_s": round(e.latency, 4), "error": bool(e.error),
                              "returncode": e.returncode, "timed_out": e.timed_out})
        for r in a.rounds:
            ctx_rows.append({"set": a.set, "challenge": a.challenge, "attempt": a.attempt, "round": r["round"],
                             "prompt_tokens": r.get("prompt_tokens"), "completion_tokens": r.get("completion_tokens"),
                             "model_latency_s": round(r["model_latency"], 3)})
    write_csv(args.outdir / "tool_calls.csv", tool_rows)
    write_csv(args.outdir / "rounds.csv", ctx_rows)

    for r in by_set:
        print(f'{r["group"]}: {r["solved_attempts"]}/{r["attempts"]} attempts solved, '
              f'{r["challenges_solved_any"]}/{r["challenges"]} challenges solved at least once, '
              f'finish {r["finish_reasons"]}')
    print(f"wrote CSVs to {args.outdir}")


if __name__ == "__main__":
    main()
