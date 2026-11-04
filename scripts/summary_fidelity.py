#!/usr/bin/env python3
"""Measure how faithfully the memory summaries keep the exact values from the tool outputs.

Usage:
    python scripts/summary_fidelity.py LOG_DIR [-o report.md] [--worst 15] [--cap 0]

For each summarized action in the conversation logs of a memory run (written by
llm_ctf_solve.py with memory enabled), the tool outputs are re-read from the logged
transcript, the exact values in them (flags, URLs, host:port pairs, paths, hex
addresses, credentials) are extracted with the same FactExtractor that the facts
block uses, and each value is looked up verbatim in the summary the agent was shown.

The report gives, per summarizer (llm, truncate, mask, verbatim) and per kind of
value, how many values the raw outputs held and how many survived into the summary
(recall), the compression ratio of the summaries, the actions with the lowest recall,
and the actions where the llm summarizer fell back to truncation.

Alignment: memory entry with step i describes the i-th assistant message of the
transcript, and its tool outputs are the `tool` messages that follow that assistant
message. Entries whose summary is null (still in the raw tail when the run ended)
and entries whose action called no tool are counted and skipped. The final action of
a run usually has no memory entry (it is only recorded when the run ends because the
context length was exceeded); such actions are counted too.

By default every value in an output is checked (--cap 0). With --cap 6 only the first
six values of each kind per action are checked, which is what the facts block keeps
(flags are never capped, as in the facts block).

Unlike the other scripts in this directory, this one needs the project's
dependencies, because it imports llm_ctf.memory; run it with the project's Python.
"""
import argparse
import json
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from llm_ctf.memory import FactExtractor, result_text  # noqa: E402
from llm_ctf.tools import ToolResult  # noqa: E402

KINDS = ("flag", "url", "hostport", "path", "address", "credential")
SUMMARIZER_ORDER = ("llm", "truncate", "mask", "verbatim")
FLAG_FORMAT_RE = re.compile(r"The flag format for this CTF is (.+?)\.(?:\s|$)")
FALLBACK_PREFIX = "Memory: summarizer"
COMPRESSION_BUCKETS = (("<2x", 0, 2), ("2-5x", 2, 5), ("5-20x", 5, 20), (">20x", 20, math.inf))


# ----------------------------------------------------------------------------- loading (as in run_report.py)

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


# ----------------------------------------------------------------------------- alignment

@dataclass
class Action:
    """One model reply (its text and tool calls) with the results of those tool calls."""
    step: int
    thought: str = ""
    tool_calls: List[Tuple[str, str]] = field(default_factory=list)   # (tool name, arguments)
    results: List[Tuple[str, dict]] = field(default_factory=list)     # (tool name, result dict)


@dataclass
class ActionReport:
    """The fidelity check of one memory entry against the tool outputs of its action."""
    challenge: str
    step: int
    summarizer: str
    values: List[Tuple[str, str]]
    present: List[Tuple[str, str]]
    lost: List[Tuple[str, str]]
    recall: Optional[float]        # None when the outputs held no value to check
    compression: Optional[float]   # full_chars / summary_chars; None when summary_chars is 0
    fallback: bool                 # the llm summarizer fell back to truncation
    n_results: int


def actions(log) -> List[Action]:
    """The actions of a run, in order: actions()[i] is the action of memory step i + 1.

    Every assistant message starts an action; the tool messages that follow it, up to the
    next assistant or user message, are its results. User and system messages are ignored.
    """
    out: List[Action] = []
    for m in messages(log):
        role = m.get("role")
        if role == "assistant":
            calls = []
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                calls.append((str(fn.get("name") or ""), str(fn.get("arguments") or "")))
            out.append(Action(step=len(out) + 1, thought=m.get("content") or "", tool_calls=calls))
        elif role == "tool" and out:
            content = m.get("content")
            content = "" if content is None else str(content)
            try:
                result = json.loads(content)
            except json.JSONDecodeError:
                result = {"raw": content}
            if not isinstance(result, dict):
                result = {"raw": content}
            out[-1].results.append((str(m.get("name") or ""), result))
    return out


def raw_text(action: Action) -> str:
    """The text the memory's fact extractor saw for this action: every result, rendered as in ActionMemory.add."""
    return "\n".join(result_text(ToolResult(name, "", result)) for name, result in action.results)


def flag_format_of(log) -> Optional[str]:
    """The flag format of the run: from args.memory_config, else parsed from the system prompt."""
    cfg = (log.get("args") or {}).get("memory_config") or {}
    if cfg.get("flag_format"):
        return str(cfg["flag_format"])
    for m in messages(log):
        if m.get("role") == "system":
            match = FLAG_FORMAT_RE.search(m.get("content") or "")
            return match.group(1) if match else None
    return None


def configured_summarizer(log) -> str:
    cfg = (log.get("args") or {}).get("memory_config") or {}
    return str(cfg.get("summarizer") or "llm")


class UncappedExtractor(FactExtractor):
    """The memory's extractor without the per-kind cap, so every value in an output is checked."""
    PER_KIND_PER_ACTION = 10 ** 9


def make_extractor(flag_format: Optional[str], cap: int = 0) -> FactExtractor:
    """cap 0 checks every value; cap N checks at most N values per kind per action (6 is the facts block's cap)."""
    if cap and cap > 0:
        extractor = FactExtractor(flag_format)
        extractor.PER_KIND_PER_ACTION = cap
        return extractor
    return UncappedExtractor(flag_format)


def check_entry(log_name: str, entry: dict, action: Action, extractor: FactExtractor,
                configured: str) -> ActionReport:
    """Look up every value of the action's outputs in the entry's summary (plain, case-sensitive substring)."""
    summary = entry.get("summary") or ""
    summarizer = str(entry.get("summarizer") or "unknown")
    values = extractor.extract(raw_text(action)) if action.results else []
    present = [(kind, value) for kind, value in values if value in summary]
    lost = [(kind, value) for kind, value in values if value not in summary]
    full_chars = entry.get("full_chars") or 0
    summary_chars = entry.get("summary_chars") or 0
    return ActionReport(
        challenge=log_name,
        step=int(entry.get("step") or action.step),
        summarizer=summarizer,
        values=values,
        present=present,
        lost=lost,
        recall=len(present) / len(values) if values else None,
        compression=full_chars / summary_chars if summary_chars else None,
        fallback=(configured == "llm" and summarizer == "truncate"),
        n_results=len(action.results),
    )


# ----------------------------------------------------------------------------- analysis

def analyze(logs: Dict[str, dict], cap: int = 0, worst: int = 15) -> dict:
    stats = {
        "cap": cap,
        "n_logs": len(logs),
        "n_with_memory": 0,
        "n_scored": 0,                 # entries with a summary and tool outputs, checked
        "n_raw_tail": 0,               # entries with summary null (raw tail at the end of the run)
        "n_no_tool": 0,                # entries whose action called no tool
        "n_final_without_entry": 0,    # assistant messages without a memory entry
        "n_unaligned": 0,              # entries whose step has no assistant message
        "by_kind": defaultdict(lambda: {"found": 0, "present": 0}),          # (summarizer, kind)
        "by_summarizer": defaultdict(lambda: {"n_actions": 0, "compressions": [], "recalls": []}),
        "reports": [],
        "fallbacks": [],               # (challenge, step)
        "fallback_reasons": Counter(),
        "verbatim": [0, 0],            # verbatim entries with full recall, verbatim entries with values
    }
    for name in sorted(logs):
        log = logs[name]
        entries = log.get("memory")
        if not entries:
            continue
        stats["n_with_memory"] += 1
        acts = actions(log)
        extractor = make_extractor(flag_format_of(log), cap)
        configured = configured_summarizer(log)
        steps_with_entry = set()
        for entry in entries:
            step = entry.get("step")
            if not isinstance(step, int) or isinstance(step, bool) or not 1 <= step <= len(acts):
                stats["n_unaligned"] += 1
                continue
            steps_with_entry.add(step)
            if entry.get("summary") is None:
                stats["n_raw_tail"] += 1
                continue
            if configured == "llm" and entry.get("summarizer") == "truncate":
                stats["fallbacks"].append((name, step))
            action = acts[step - 1]
            if not action.results:
                stats["n_no_tool"] += 1
                continue
            report = check_entry(name, entry, action, extractor, configured)
            stats["n_scored"] += 1
            stats["reports"].append(report)
            per = stats["by_summarizer"][report.summarizer]
            per["n_actions"] += 1
            if report.compression is not None:
                per["compressions"].append(report.compression)
            if report.recall is not None:
                per["recalls"].append(report.recall)
            for kind, _ in report.values:
                stats["by_kind"][(report.summarizer, kind)]["found"] += 1
            for kind, _ in report.present:
                stats["by_kind"][(report.summarizer, kind)]["present"] += 1
            if report.summarizer == "verbatim" and report.recall is not None:
                stats["verbatim"][1] += 1
                if report.recall == 1.0:
                    stats["verbatim"][0] += 1
        stats["n_final_without_entry"] += sum(1 for a in acts if a.step not in steps_with_entry)
        for line in log.get("debug_log") or []:
            if isinstance(line, str) and line.startswith(FALLBACK_PREFIX):
                stats["fallback_reasons"][line.strip()] += 1
    stats["worst"] = sorted((r for r in stats["reports"] if r.recall is not None),
                            key=lambda r: (r.recall, -len(r.lost), r.challenge, r.step))[:worst]
    return stats


# ----------------------------------------------------------------------------- report

def pct(num, den) -> str:
    return f"{100 * num / den:.1f}%" if den else "–"


def percentile(values, p: float):
    """Linear interpolation between the sorted values; p is in 0..100."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def fmt(x, digits=1) -> str:
    if x is None:
        return "–"
    if isinstance(x, float):
        return f"{x:.{digits}f}"
    return str(x)


def cell(text: str) -> str:
    """Escape a value for a markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ")


def summarizer_names(stats) -> List[str]:
    names = set(stats["by_summarizer"]) | {s for s, _ in stats["by_kind"]}
    return [s for s in SUMMARIZER_ORDER if s in names] + sorted(names - set(SUMMARIZER_ORDER))


def render(stats: dict, name: str = "") -> str:
    out = []
    w = out.append
    w(f"# Summary fidelity: {name}\n")
    w(f"Logs: {stats['n_logs']} (with memory entries: {stats['n_with_memory']}). "
      f"Actions aligned: {stats['n_scored']}; entries skipped: raw tail (summary null) {stats['n_raw_tail']}, "
      f"no tool output {stats['n_no_tool']}; final actions without entry {stats['n_final_without_entry']}.\n")
    if stats["n_unaligned"]:
        w(f"Entries whose step has no assistant message (alignment problem): {stats['n_unaligned']}.\n")
    cap = stats.get("cap") or 0
    w("Recall is the share of the exact values found in an action's tool outputs (by the facts block's regexes"
      + (f", at most {cap} per kind per action" if cap else ", uncapped")
      + ") that appear verbatim in its summary.\n")

    w("## Recall of exact values, by summarizer and kind\n")
    w("| summarizer | kind | values in raw outputs | present in summary | recall |\n|---|---|---|---|---|")
    for summarizer in summarizer_names(stats):
        kinds = [k for k in KINDS if (summarizer, k) in stats["by_kind"]]
        kinds += sorted(k for s, k in stats["by_kind"] if s == summarizer and k not in KINDS)
        found_all = present_all = 0
        for kind in kinds:
            counts = stats["by_kind"][(summarizer, kind)]
            found_all += counts["found"]
            present_all += counts["present"]
            w(f"| {summarizer} | {kind} | {counts['found']} | {counts['present']} | {pct(counts['present'], counts['found'])} |")
        w(f"| {summarizer} | all kinds | {found_all} | {present_all} | {pct(present_all, found_all)} |")
        recalls = stats["by_summarizer"][summarizer]["recalls"]
        macro = f"{100 * statistics.mean(recalls):.1f}%" if recalls else "–"
        w(f"| {summarizer} | mean per-action recall | {len(recalls)} action(s) | | {macro} |")
    if not summarizer_names(stats):
        w("| – | – | 0 | 0 | – |")
    w("")

    w("## Compression (action chars / summary chars)\n")
    w("| summarizer | actions | median | p10 | p90 | max | <2x | 2-5x | 5-20x | >20x |\n|---|---|---|---|---|---|---|---|---|---|")
    for summarizer in summarizer_names(stats):
        per = stats["by_summarizer"][summarizer]
        ratios = per["compressions"]
        if not ratios:
            w(f"| {summarizer} | {per['n_actions']} | – | – | – | – | – | – | – | – |")
            continue
        buckets = [sum(1 for r in ratios if lo <= r < hi) for _, lo, hi in COMPRESSION_BUCKETS]
        w(f"| {summarizer} | {per['n_actions']} | {fmt(statistics.median(ratios))} | {fmt(percentile(ratios, 10))} "
          f"| {fmt(percentile(ratios, 90))} | {fmt(max(ratios))} | " + " | ".join(str(b) for b in buckets) + " |")
    w("")

    worst = stats["worst"]
    w(f"## Actions with the lowest recall (worst {len(worst)})\n")
    w("| challenge | step | summarizer | recall | values lost |\n|---|---|---|---|---|")
    for r in worst:
        lost = ", ".join(f"{kind}: `{cell(value)}`" for kind, value in r.lost) or "–"
        w(f"| {r.challenge} | {r.step} | {r.summarizer} | {100 * r.recall:.0f}% ({len(r.present)}/{len(r.values)}) | {lost} |")
    if not worst:
        w("| – | – | – | – | no action had a value to check |")
    w("")

    w("## Notes\n")
    fallbacks = stats["fallbacks"]
    if fallbacks:
        shown = ", ".join(f"{ch}#{step}" for ch, step in fallbacks[:40])
        if len(fallbacks) > 40:
            shown += f", ... ({len(fallbacks) - 40} more)"
        w(f"- {len(fallbacks)} action(s) in {len(set(ch for ch, _ in fallbacks))} challenge(s) fell back to `truncate` "
          f"(llm summarizer failed or empty): {shown}")
        for reason, count in stats["fallback_reasons"].most_common():
            w(f"  - {cell(reason)} ({count})")
    else:
        w("- No action fell back from the llm summarizer to `truncate`.")
    ok, total = stats["verbatim"]
    if total:
        w(f"- verbatim entries should have 100% recall (sanity check: {ok}/{total} did).")
    else:
        w("- verbatim entries should have 100% recall (sanity check: no verbatim entry had a value to check).")
    return "\n".join(out) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("log_dir")
    parser.add_argument("-o", "--output", help="write the markdown report to this file as well as printing it")
    parser.add_argument("--worst", type=int, default=15, help="number of lowest-recall actions to list")
    parser.add_argument("--cap", type=int, default=0,
                        help="check at most this many values per kind per action (0: all; 6: what the facts block keeps)")
    args = parser.parse_args()
    stats = analyze(load_logs(args.log_dir), cap=args.cap, worst=args.worst)
    text = render(stats, Path(args.log_dir).name)
    if args.output:
        Path(args.output).write_text(text)
    print(text)


if __name__ == "__main__":
    main()
