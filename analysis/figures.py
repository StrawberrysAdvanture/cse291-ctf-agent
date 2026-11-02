"""Phase 1 report figures, from the analysis CSVs.

  python analysis/figures.py RESULTS_DIR [-o OUTDIR]

RESULTS_DIR holds attempts.csv and rounds.csv (analysis/metrics.py output).
Writes:
  fig1_outcomes.png   per-challenge outcome spread across attempts (held-out + dev)
  fig2_context.png    prompt-token context growth per round, vs the 32k window
"""
import argparse
import csv
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

# One colour per finish reason; shared by both the bars and the legend.
FINISH_COLORS = {
    "solved": "#2e7d32",
    "max_rounds": "#90a4ae",
    "context_length": "#ef6c00",
    "give_up": "#6a1b9a",
    "max_time": "#c62828",
    "error": "#000000",
    "setup_error": "#b0bec5",
}


def read_rows(path):
    with open(path) as f:
        return list(csv.DictReader(f))


def fig_outcomes(attempts, out):
    """Stacked horizontal bar per challenge: how its attempts ended."""
    rows = [r for r in attempts if r["set"] in ("development", "heldout")]
    by = defaultdict(lambda: defaultdict(int))
    order = []
    for r in sorted(rows, key=lambda r: (r["set"], r["category"], r["challenge"])):
        key = (r["set"], r["category"], r["challenge"])
        if key not in by:
            order.append(key)
        by[key][r["finish_reason"]] += 1

    labels = [f'{s[:3]}/{cat}/{c}' for (s, cat, c) in order]
    reasons = [x for x in FINISH_COLORS if any(by[k].get(x) for k in order)]
    fig, ax = plt.subplots(figsize=(9, max(4, 0.32 * len(order))))
    left = [0] * len(order)
    for reason in reasons:
        vals = [by[k].get(reason, 0) for k in order]
        ax.barh(labels, vals, left=left, color=FINISH_COLORS[reason], label=reason)
        left = [l + v for l, v in zip(left, vals)]
    ax.set_xlabel("attempts")
    ax.set_title("Per-challenge outcome spread across attempts (dev + held-out)")
    ax.invert_yaxis()
    ax.tick_params(axis="y", labelsize=7)
    ax.legend(handles=[Patch(color=FINISH_COLORS[r], label=r) for r in reasons],
              loc="lower right", fontsize=8)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    print("wrote", out)


def fig_context(rounds, out, context_limit=32768):
    """Prompt tokens vs round: one faint line per attempt, bold mean, limit marked."""
    series = defaultdict(list)
    for r in rounds:
        pt = r.get("prompt_tokens")
        if pt in (None, "", "None"):
            continue
        series[(r["set"], r["challenge"], r["attempt"])].append((int(r["round"]), int(float(pt))))

    fig, ax = plt.subplots(figsize=(8, 5))
    per_round = defaultdict(list)
    for pts in series.values():
        pts.sort()
        xs, ys = zip(*pts)
        ax.plot(xs, ys, color="#b0bec5", alpha=0.35, linewidth=0.8)
        for x, y in pts:
            per_round[x].append(y)
    xs = sorted(per_round)
    means = [sum(per_round[x]) / len(per_round[x]) for x in xs]
    ax.plot(xs, means, color="#1565c0", linewidth=2.5, label="mean prompt tokens")
    ax.axhline(context_limit, color="#c62828", linestyle="--", linewidth=1.2,
               label=f"context window ({context_limit})")
    ax.set_xlabel("round")
    ax.set_ylabel("prompt tokens")
    ax.set_title("Context growth per round (all attempts)")
    ax.legend(loc="upper left", fontsize=9)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    print("wrote", out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results_dir", type=Path)
    ap.add_argument("-o", "--outdir", type=Path, default=Path("report/phase1/figs"))
    args = ap.parse_args()
    args.outdir.mkdir(parents=True, exist_ok=True)
    attempts = read_rows(args.results_dir / "attempts.csv")
    rounds = read_rows(args.results_dir / "rounds.csv")
    fig_outcomes(attempts, args.outdir / "fig1_outcomes.png")
    fig_context(rounds, args.outdir / "fig2_context.png")


if __name__ == "__main__":
    main()
