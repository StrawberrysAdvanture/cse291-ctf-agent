#!/usr/bin/env python3
"""Count the tool calls that lenient parsing (issue I11) would recover from existing logs.

Usage:
    python scripts/recover_lost_calls.py LOG_DIR [-o report.md]

For every assistant reply in the conversation logs under LOG_DIR, parse the raw reply text
the way the lenient XML parser does: every <invoke> element counts, with or without the
<function_calls> wrapper, and parameters may be given either as <command>...</command> or
in the tool-description schema (<parameter><name>command</name><value>...</value>).
Reports how many replies without a registered tool call contain a parseable call, and how
many registered calls that failed with "Missing required arguments" would have parsed.

Needs beautifulsoup4 and lxml (the project's dependencies); run with the venv python.
"""
import argparse
import json
from collections import Counter
from pathlib import Path

from bs4 import BeautifulSoup

# Required parameters per tool, as declared in llm_ctf/tools/tools.py.
REQUIRED = {
    "run_command": ["command"],
    "check_flag": ["flag"],
    "give_up": ["confirm"],
    "createfile": ["path", "contents"],
    "decompile_function": ["path"],
    "disassemble_function": ["path"],
}


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


def parse_invokes(text):
    """(tool name, {param: value}, used schema-style parameters) for every <invoke> in text."""
    soup = BeautifulSoup(text or "", "lxml")
    calls = []
    for invocation in soup.find_all("invoke"):
        name_elem = invocation.find("tool_name")
        name = name_elem.text.strip() if name_elem else ""
        params = {}
        schema_style = False
        for param in REQUIRED.get(name, []):
            elem = invocation.find(param)
            if elem is not None:
                params[param] = elem.text
        for n in invocation.find_all("name"):
            key = n.text.strip()
            if key in REQUIRED.get(name, []) and key not in params:
                container = n.parent if n.parent is not None and n.parent.name == "parameter" else None
                value = container.find("value") if container is not None else n.find_next("value")
                if value is not None:
                    params[key] = value.text
                    schema_style = True
        calls.append((name, params, schema_style))
    return calls


def raw_reply(message):
    """The model's full reply text. For replies with registered tool calls the log's `content`
    holds only the text before the tool-call block; the raw reply is kept under extra.response."""
    try:
        raw = message["extra"]["response"]["data"]["choices"][0]["message"]["content"]
        if raw:
            return raw
    except (KeyError, IndexError, TypeError):
        pass
    return message.get("content") or ""


def parseable(name, params):
    return name in REQUIRED and all(p in params for p in REQUIRED[name])


def analyze(logs):
    c = Counter()
    per_challenge = {}
    for challenge, log in logs.items():
        items = [m[1] if isinstance(m, list) and len(m) == 2 else m for m in log.get("messages") or []]
        recovered_here = 0
        for i, m in enumerate(items):
            if not isinstance(m, dict) or m.get("role") != "assistant":
                continue
            c["replies"] += 1
            registered = len(m.get("tool_calls") or [])
            text = raw_reply(m)
            calls = parse_invokes(text)
            good = [x for x in calls if parseable(x[0], x[1])]
            if registered == 0:
                c["replies_without_registered_call"] += 1
                if "<invoke>" in text:
                    c["unwrapped_invoke_replies"] += 1
                    if good:
                        c["unwrapped_replies_recoverable"] += 1
                        c["calls_recovered_from_unwrapped"] += len(good)
                        recovered_here += len(good)
                    if "<function_calls>" not in text and "```xml" in text:
                        c["unwrapped_in_xml_fence"] += 1
                else:
                    c["prose_only_replies"] += 1
            else:
                # Registered calls whose arguments failed to parse: look at the tool results
                # that follow this reply for the formatter's error message.
                j = i + 1
                while j < len(items) and isinstance(items[j], dict) and items[j].get("role") == "tool":
                    try:
                        result = json.loads(items[j].get("content") or "{}")
                    except json.JSONDecodeError:
                        result = {}
                    if "Missing required arguments" in str(result.get("error", "")):
                        c["registered_calls_with_missing_args"] += 1
                    j += 1
                schema = [x for x in calls if x[2] and parseable(x[0], x[1])]
                if schema:
                    c["schema_style_calls_now_parseable"] += len(schema)
                    recovered_here += len(schema)
        if recovered_here:
            per_challenge[challenge] = recovered_here
    return c, per_challenge


def render(c, per_challenge, name):
    lines = [f"# Lenient tool-call parsing: calls recoverable from {name}", ""]
    lines.append(f"Replies: {c['replies']}. Without a registered tool call: {c['replies_without_registered_call']} "
                 f"(unwrapped `<invoke>`: {c['unwrapped_invoke_replies']}, of which inside a ```xml fence: "
                 f"{c['unwrapped_in_xml_fence']}; prose only: {c['prose_only_replies']}).")
    lines.append("")
    lines.append("| What lenient parsing recovers | count |\n|---|---|")
    lines.append(f"| Replies whose unwrapped invoke parses to at least one valid call | {c['unwrapped_replies_recoverable']} of {c['unwrapped_invoke_replies']} |")
    lines.append(f"| Tool calls recovered from those replies | {c['calls_recovered_from_unwrapped']} |")
    lines.append(f"| Registered calls that failed with 'Missing required arguments' | {c['registered_calls_with_missing_args']} |")
    lines.append(f"| Schema-style calls that now parse | {c['schema_style_calls_now_parseable']} |")
    lines.append("")
    lines.append(f"Challenges gaining at least one call: {len(per_challenge)}; " +
                 ", ".join(f"{k} (+{v})" for k, v in sorted(per_challenge.items(), key=lambda kv: -kv[1])[:12]) + ".")
    lines.append("")
    lines.append("Caveat: a recovered call would have changed the rest of the conversation, so these counts "
                 "say what the parser would have accepted, not what the run would have achieved.")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("log_dir")
    parser.add_argument("-o", "--output")
    args = parser.parse_args()
    c, per_challenge = analyze(load_logs(args.log_dir))
    text = render(c, per_challenge, Path(args.log_dir).name)
    if args.output:
        Path(args.output).write_text(text)
    print(text)


if __name__ == "__main__":
    main()
