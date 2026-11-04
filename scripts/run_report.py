#!/usr/bin/env python3
"""Report on a single run's conversation logs (as written by llm_ctf_solve.py).

Usage:
    python scripts/run_report.py LOG_DIR [-o report.md] [--early-calls 4]

Reports solve rates (overall and by category), finish reasons, model calls and prompt
sizes, tool-call statistics (repeated commands, replies without a tool call, tool errors,
time-outs, flag submissions) and a per-challenge table. Standard library only.
"""
import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path


def load_logs(directory):
    logs = {}
    for path in sorted(Path(directory).rglob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if "finish_reason" in data and "messages" in data:
            logs[path.stem] = data
    return logs


def messages(log):
    for item in log.get("messages") or []:
        m = item[1] if isinstance(item, list) and len(item) == 2 else item
        if isinstance(m, dict):
            yield m


def challenge_stats(log, early_calls):
    tokens = log.get("prompt_tokens") or []
    challenge = log.get("challenge") or {}
    replies = 0
    replies_without_tools = 0
    keys = []
    tool_results = 0
    tool_errors = 0
    timeouts = 0
    flag_checks = 0
    for m in messages(log):
        role = m.get("role")
        if role == "assistant":
            replies += 1
            calls = m.get("tool_calls") or []
            if not calls:
                replies_without_tools += 1
            for tc in calls:
                fn = tc.get("function") or {}
                keys.append(f"{fn.get('name', '')} {fn.get('arguments', '')}".strip())
                if fn.get("name") in ("check_flag", "checkflag"):
                    flag_checks += 1
        elif role == "tool":
            tool_results += 1
            try:
                result = json.loads(m.get("content") or "{}")
            except json.JSONDecodeError:
                result = {}
            if result.get("error"):
                tool_errors += 1
            if result.get("timed_out"):
                timeouts += 1
    repeats = sum(1 for i, k in enumerate(keys) if k in keys[:i])
    runtime = log.get("runtime") or {}
    solved = bool(log.get("solved"))
    rounds = log.get("rounds")
    return {
        "solved": solved,
        "early": solved and rounds is not None and rounds <= early_calls,
        "finish": log.get("finish_reason") or "unknown",
        "category": challenge.get("category") or "?",
        "rounds": rounds,
        "calls": len(tokens),
        "max_prompt": max(tokens) if tokens else None,
        "mean_prompt": statistics.mean(tokens) if tokens else None,
        "replies": replies,
        "no_tool": replies_without_tools,
        "tool_calls": len(keys),
        "repeats": repeats,
        "tool_errors": tool_errors,
        "timeouts": timeouts,
        "flag_checks": flag_checks,
        "runtime": runtime.get("total"),
        "model_time": runtime.get("model"),
        "tool_time": runtime.get("tools"),
    }


def fmt(x, digits=1):
    if x is None:
        return "–"
    if isinstance(x, float):
        return f"{x:.{digits}f}"
    return str(x)


def report(logs, name, early_calls):
    rows = {ch: challenge_stats(log, early_calls) for ch, log in logs.items()}
    n = len(rows)
    out = []
    w = out.append
    w(f"# Run report: {name}\n")
    if not n:
        w("No conversation logs found.\n")
        return "\n".join(out)
    solved = sum(r["solved"] for r in rows.values())
    early = sum(r["early"] for r in rows.values())
    w(f"Challenges: **{n}**. Solved: **{solved}** ({100 * solved / n:.1f}%). "
      f"Solved within the first {early_calls} model calls: {early}.\n")

    w("## By category\n")
    w("| Category | n | solved | solved % |\n|---|---|---|---|")
    by_cat = defaultdict(list)
    for r in rows.values():
        by_cat[r["category"]].append(r)
    for cat in sorted(by_cat):
        rs = by_cat[cat]
        s = sum(r["solved"] for r in rs)
        w(f"| {cat} | {len(rs)} | {s} | {100 * s / len(rs):.0f}% |")
    w("")

    w("## How runs ended\n")
    w("| finish_reason | count |\n|---|---|")
    for reason, count in Counter(r["finish"] for r in rows.values()).most_common():
        w(f"| {reason} | {count} |")
    w("")

    def mean_of(key):
        vals = [r[key] for r in rows.values() if r[key] is not None]
        return statistics.mean(vals) if vals else None

    def max_of(key):
        vals = [r[key] for r in rows.values() if r[key] is not None]
        return max(vals) if vals else None

    w("## Model calls and context\n")
    w("| Metric | value |\n|---|---|")
    w(f"| Model calls per challenge (mean) | {fmt(mean_of('calls'))} |")
    w(f"| Rounds per challenge (mean) | {fmt(mean_of('rounds'))} |")
    w(f"| Prompt tokens per call (mean over challenges) | {fmt(mean_of('mean_prompt'), 0)} |")
    w(f"| Max prompt tokens, median over challenges | {fmt(statistics.median([r['max_prompt'] for r in rows.values() if r['max_prompt'] is not None]) if any(r['max_prompt'] is not None for r in rows.values()) else None, 0)} |")
    w(f"| Largest prompt in any call | {fmt(max_of('max_prompt'))} |")
    w(f"| Wall time per challenge, mean (s) | {fmt(mean_of('runtime'), 0)} |")
    w(f"| Model time per challenge, mean (s) | {fmt(mean_of('model_time'), 0)} |")
    w(f"| Tool time per challenge, mean (s) | {fmt(mean_of('tool_time'), 0)} |")
    total_runtime = sum(r["runtime"] or 0 for r in rows.values())
    w(f"| Total wall time (h) | {total_runtime / 3600:.2f} |")
    w("")

    w("## Tool use\n")
    replies = sum(r["replies"] for r in rows.values())
    no_tool = sum(r["no_tool"] for r in rows.values())
    calls = sum(r["tool_calls"] for r in rows.values())
    repeats = sum(r["repeats"] for r in rows.values())
    w("| Metric | value |\n|---|---|")
    w(f"| Model replies | {replies} |")
    w(f"| Replies with no tool call (issue I9) | {no_tool} ({100 * no_tool / replies:.1f}% of replies) |" if replies else "| Replies with no tool call | 0 |")
    w(f"| Challenges where every reply lacked a tool call | {sum(1 for r in rows.values() if r['replies'] and r['no_tool'] == r['replies'])} |")
    w(f"| Tool calls | {calls} |")
    w(f"| Repeated tool calls (issue I1) | {repeats} ({100 * repeats / calls:.1f}% of calls)|" if calls else "| Repeated tool calls | 0 |")
    w(f"| Challenges with at least one repeat | {sum(1 for r in rows.values() if r['repeats'])} |")
    w(f"| Tool results that were errors | {sum(r['tool_errors'] for r in rows.values())} |")
    w(f"| Commands that timed out | {sum(r['timeouts'] for r in rows.values())} |")
    w(f"| check_flag calls | {sum(r['flag_checks'] for r in rows.values())} |")
    w("")

    w("## Per challenge\n")
    w(f"| Challenge | cat | solved | finish | calls | max prompt | no-tool replies | repeats | errors | time (s) |\n|---|---|---|---|---|---|---|---|---|---|")
    for ch in sorted(rows):
        r = rows[ch]
        w(f"| {ch} | {r['category']} | {'✓' if r['solved'] else ''} | {r['finish']} | {r['calls']} | {fmt(r['max_prompt'])} "
          f"| {r['no_tool']}/{r['replies']} | {r['repeats']} | {r['tool_errors']} | {fmt(r['runtime'], 0)} |")
    return "\n".join(out) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("log_dir")
    parser.add_argument("--name", default=None, help="run name for the title (default: the directory name)")
    parser.add_argument("--early-calls", type=int, default=4,
                        help="count challenges solved within this many model calls (a 3-round run makes 4)")
    parser.add_argument("-o", "--output")
    args = parser.parse_args()
    text = report(load_logs(args.log_dir), args.name or Path(args.log_dir).name, args.early_calls)
    if args.output:
        Path(args.output).write_text(text)
    print(text)


if __name__ == "__main__":
    main()
