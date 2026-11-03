#!/usr/bin/env python3
"""Paired comparison of two runs over the same challenges (e.g. baseline vs. action memory).

Usage:
    python scripts/paired_analysis.py BASELINE_LOG_DIR MEMORY_LOG_DIR [--names baseline memory] [-o report.md]

Each directory is searched recursively for conversation logs (<challenge>.json) written
by llm_ctf_solve.py. Challenges are paired by name; a challenge with no log in one arm
is counted as a crash (no log written) for that arm.

Standard library only.
"""
import argparse
import json
import math
import statistics
from collections import Counter
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


def exact_mcnemar(b, c):
    """Two-sided exact McNemar test (binomial test on the discordant pairs)."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    p = sum(math.comb(n, i) for i in range(0, k + 1)) / 2 ** n
    return min(1.0, 2 * p)


def sign_test(diffs):
    """Two-sided exact sign test on paired differences (zeros dropped)."""
    pos = sum(1 for d in diffs if d > 0)
    neg = sum(1 for d in diffs if d < 0)
    return pos, neg, exact_mcnemar(pos, neg)


def fmt(x, digits=1):
    if x is None:
        return "–"
    if isinstance(x, float):
        return f"{x:.{digits}f}"
    return str(x)


def tool_call_keys(log):
    """One key per logged tool call: tool name plus its arguments, in order."""
    keys = []
    for item in log.get("messages") or []:
        message = item[1] if isinstance(item, list) and len(item) == 2 else item
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            keys.append(f"{fn.get('name', '')} {fn.get('arguments', '')}".strip())
    return keys


def repeat_stats(log):
    """(tool calls, calls that exactly repeat an earlier call in the same challenge)."""
    keys = tool_call_keys(log)
    repeats = sum(1 for i, k in enumerate(keys) if k in keys[:i])
    return len(keys), repeats


def summarize_arm(log):
    """Per-challenge metrics for one arm (log may be None if the run crashed before logging)."""
    if log is None:
        return {"solved": False, "finish": "no_log", "rounds": None, "calls": None,
                "max_prompt": None, "mean_prompt": None, "runtime": None, "model_time": None,
                "category": None, "memory": None, "tool_calls": None, "repeats": None}
    tokens = log.get("prompt_tokens") or []
    challenge = log.get("challenge") or {}
    n_calls, n_repeats = repeat_stats(log)
    return {
        "tool_calls": n_calls,
        "repeats": n_repeats,
        "solved": bool(log.get("solved")),
        "finish": log.get("finish_reason") or "unknown",
        "rounds": log.get("rounds"),
        "calls": len(tokens) if tokens else None,
        "max_prompt": max(tokens) if tokens else None,
        "mean_prompt": statistics.mean(tokens) if tokens else None,
        "runtime": (log.get("runtime") or {}).get("total"),
        "model_time": (log.get("runtime") or {}).get("model"),
        "category": challenge.get("category"),
        "memory": log.get("memory"),
    }


def analyze(a_logs, b_logs, names):
    na, nb = names
    challenges = sorted(set(a_logs) | set(b_logs))
    rows = []
    for ch in challenges:
        A, B = summarize_arm(a_logs.get(ch)), summarize_arm(b_logs.get(ch))
        category = A["category"] or B["category"] or (ch.split("-")[1] if "-" in ch else "?")
        rows.append((ch, category, A, B))

    out = []
    w = out.append
    n = len(rows)
    a_solved = sum(r[2]["solved"] for r in rows)
    b_solved = sum(r[3]["solved"] for r in rows)
    both = sum(r[2]["solved"] and r[3]["solved"] for r in rows)
    only_a = [r[0] for r in rows if r[2]["solved"] and not r[3]["solved"]]
    only_b = [r[0] for r in rows if r[3]["solved"] and not r[2]["solved"]]
    neither = n - both - len(only_a) - len(only_b)

    w(f"# Paired analysis: {na} vs {nb}\n")
    w(f"Challenges paired: **{n}** (logs found: {na} {len(a_logs)}, {nb} {len(b_logs)})\n")
    w("## Solve rate\n")
    w(f"| | {na} | {nb} |\n|---|---|---|")
    w(f"| Solved | {a_solved}/{n} ({100*a_solved/max(n,1):.1f}%) | {b_solved}/{n} ({100*b_solved/max(n,1):.1f}%) |\n")
    w(f"Paired outcomes: both solved {both}, only {na} {len(only_a)}, only {nb} {len(only_b)}, neither {neither}.  ")
    w(f"Exact McNemar test on the {len(only_a) + len(only_b)} discordant pairs: p = {exact_mcnemar(len(only_a), len(only_b)):.3f}.\n")
    if only_a:
        w(f"- Solved only by {na}: " + ", ".join(f"`{c}`" for c in only_a))
    if only_b:
        w(f"- Solved only by {nb}: " + ", ".join(f"`{c}`" for c in only_b))
    w("")

    w("## How runs ended\n")
    reasons = sorted(set(r[2]["finish"] for r in rows) | set(r[3]["finish"] for r in rows))
    ca, cb = Counter(r[2]["finish"] for r in rows), Counter(r[3]["finish"] for r in rows)
    w(f"| finish_reason | {na} | {nb} |\n|---|---|---|")
    for reason in reasons:
        w(f"| {reason} | {ca[reason]} | {cb[reason]} |")
    w("")
    transitions = Counter((r[2]["finish"], r[3]["finish"]) for r in rows if r[2]["finish"] != r[3]["finish"])
    if transitions:
        w(f"Paired changes in finish_reason ({na} → {nb}):\n")
        for (fa, fb), count in transitions.most_common():
            w(f"- {fa} → {fb}: {count}")
        w("")

    w("## Context use (prompt tokens per model call, as reported by vLLM)\n")
    pairs = [(r[2]["max_prompt"], r[3]["max_prompt"]) for r in rows
             if r[2]["max_prompt"] is not None and r[3]["max_prompt"] is not None]
    if pairs:
        diffs = [b - a for a, b in pairs]
        pos, neg, p = sign_test(diffs)
        w(f"| | {na} | {nb} |\n|---|---|---|")
        w(f"| Median of per-challenge max prompt | {fmt(statistics.median(a for a, _ in pairs), 0)} | {fmt(statistics.median(b for _, b in pairs), 0)} |")
        w(f"| Largest prompt in any call | {max(a for a, _ in pairs)} | {max(b for _, b in pairs)} |")
        means = [(r[2]["mean_prompt"], r[3]["mean_prompt"]) for r in rows
                 if r[2]["mean_prompt"] is not None and r[3]["mean_prompt"] is not None]
        w(f"| Mean prompt per call (avg over challenges) | {fmt(statistics.mean(a for a, _ in means), 0)} | {fmt(statistics.mean(b for _, b in means), 0)} |\n")
        w(f"Paired difference in max prompt ({nb} − {na}) over {len(pairs)} challenges: "
          f"median {fmt(statistics.median(diffs), 0)} tokens; {nb} larger in {pos}, smaller in {neg}; "
          f"sign test p = {p:.3g}.\n")
    else:
        w("No challenges have prompt_tokens in both arms.\n")

    w("## Rounds and time\n")
    def paired(key):
        return [(r[2][key], r[3][key]) for r in rows if r[2][key] is not None and r[3][key] is not None]
    w(f"| Metric (mean over paired challenges) | {na} | {nb} | paired diff, median |\n|---|---|---|---|")
    for key, label in [("rounds", "Rounds"), ("calls", "Agent model calls"),
                       ("runtime", "Wall time per challenge (s)"), ("model_time", "Model time per challenge (s)")]:
        p_ = paired(key)
        if p_:
            w(f"| {label} | {fmt(statistics.mean(a for a, _ in p_))} | {fmt(statistics.mean(b for _, b in p_))} "
              f"| {fmt(statistics.median(b - a for a, b in p_))} |")
    w("")

    w("## Repeated actions (issue I1 in logs/issues.md)\n")
    w("A repeat is a tool call whose name and arguments exactly match an earlier call in the same challenge.\n")
    w(f"| | {na} | {nb} |\n|---|---|---|")
    ta = [(r[2]["tool_calls"], r[2]["repeats"]) for r in rows if r[2]["tool_calls"] is not None]
    tb = [(r[3]["tool_calls"], r[3]["repeats"]) for r in rows if r[3]["tool_calls"] is not None]
    def share(t):
        calls = sum(c for c, _ in t)
        return f"{sum(x for _, x in t)}/{calls} ({100 * sum(x for _, x in t) / calls:.1f}%)" if calls else "–"
    w(f"| Repeated calls / all tool calls | {share(ta)} | {share(tb)} |")
    w(f"| Challenges with at least one repeat | {sum(1 for _, x in ta if x)}/{len(ta)} | {sum(1 for _, x in tb if x)}/{len(tb)} |\n")
    rp = [(r[2]["repeats"], r[3]["repeats"]) for r in rows if r[2]["repeats"] is not None and r[3]["repeats"] is not None]
    if rp:
        pos, neg, p = sign_test([b - a for a, b in rp])
        w(f"Paired: {nb} has more repeats in {pos} challenges, fewer in {neg}; sign test p = {p:.3g}.\n")

    mem_entries = [m for r in rows for m in (r[3]["memory"] or [])]
    if mem_entries:
        w(f"## Memory summaries ({nb})\n")
        by = Counter(m.get("summarizer") for m in mem_entries)
        full = [m["full_chars"] for m in mem_entries]
        summ = [m["summary_chars"] for m in mem_entries]
        ratio = [f / max(s, 1) for f, s in zip(full, summ)]
        secs = [m.get("summary_seconds", 0) for m in mem_entries]
        w(f"- Actions summarized: {len(mem_entries)} ({', '.join(f'{k}: {v}' for k, v in by.items())})")
        w(f"- Action size: median {statistics.median(full):.0f} chars, max {max(full)}")
        w(f"- Summary size: median {statistics.median(summ):.0f} chars, max {max(summ)}")
        w(f"- Compression (action chars / summary chars): median {statistics.median(ratio):.1f}x")
        w(f"- Summary time: median {statistics.median(secs):.1f} s, total {sum(secs):.0f} s\n")

    w("## By category\n")
    cats = sorted(set(r[1] for r in rows))
    w(f"| Category | n | {na} solved | {nb} solved |\n|---|---|---|---|")
    for cat in cats:
        rs = [r for r in rows if r[1] == cat]
        w(f"| {cat} | {len(rs)} | {sum(r[2]['solved'] for r in rs)} | {sum(r[3]['solved'] for r in rs)} |")
    w("")

    w("## Per challenge\n")
    w(f"| Challenge | {na} solved | {nb} solved | {na} finish | {nb} finish | {na} max prompt | {nb} max prompt | {na} repeats | {nb} repeats |\n|---|---|---|---|---|---|---|---|---|")
    for ch, cat, A, B in rows:
        w(f"| {ch} | {'✓' if A['solved'] else ''} | {'✓' if B['solved'] else ''} | {A['finish']} | {B['finish']} "
          f"| {fmt(A['max_prompt'])} | {fmt(B['max_prompt'])} | {fmt(A['repeats'])} | {fmt(B['repeats'])} |")
    return "\n".join(out) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("a_dir")
    parser.add_argument("b_dir")
    parser.add_argument("--names", nargs=2, default=["baseline", "memory"])
    parser.add_argument("-o", "--output")
    args = parser.parse_args()
    report = analyze(load_logs(args.a_dir), load_logs(args.b_dir), args.names)
    if args.output:
        Path(args.output).write_text(report)
    print(report)


if __name__ == "__main__":
    main()
