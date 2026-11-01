"""Print an attempt as a readable transcript, for failure analysis.

Usage: python analysis/show_trace.py LOG.json [--max-output 1500]

Failure labels go in analysis/failures.csv, one row per failed attempt:
  set,challenge,attempt,category,label,evidence
with label one of the four Phase 1 categories:
  missing_knowledge   the model did not know the technique or misread the artifact
  tool_interface      the tools got in the way: bad output format, truncation, timeouts,
                      unparseable calls, interactive programs the tool cannot drive
  lost_findings       an earlier observation needed for the solve was forgotten or ignored
  unproductive_loop   the same or equivalent actions repeated without new information
and `evidence` naming the rounds that show it.
"""
import argparse
import json
from pathlib import Path


def clip(text, n):
    text = text if isinstance(text, str) else json.dumps(text)
    return text if len(text) <= n else text[:n] + f"\n... [{len(text) - n} more chars]"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log", type=Path)
    ap.add_argument("--max-output", type=int, default=1500)
    args = ap.parse_args()

    d = json.loads(args.log.read_text())
    m = d["metrics"]
    print(f"# {d['challenge']['path']}  finish={d['finish_reason']}  solved={d['solved']}  rounds={d['rounds']}")
    print(f"# tokens in/out={m['prompt_tokens']}/{m['completion_tokens']}  max_context={m['max_context_tokens']}  "
          f"tool_calls={m['tool_calls']}  nonzero_rc={m['nonzero_returncodes']}  timeouts={m['command_timeouts']}")
    rounds = {r["round"]: r for r in d["round_log"]}
    rnd = -1
    for _, msg in d["messages"]:
        role = msg["role"]
        if role == "system":
            continue
        if role == "assistant":
            rnd += 1
            r = rounds.get(rnd, {})
            print(f"\n=== round {rnd}  ({r.get('prompt_tokens')} prompt tokens, {r.get('model_latency') or 0:.1f}s)")
            if msg.get("content"):
                print(clip(msg["content"].strip(), args.max_output))
            for tc in msg.get("tool_calls", []):
                fn = tc.get("function", {})
                print(f"  -> {fn.get('name')}({clip(fn.get('arguments'), 400)})")
            err = (msg.get("extra") or {}).get("error")
            if err:
                print(f"  !! tool call parse error: {err.get('data')}")
        elif role == "tool":
            print(f"  <- {msg.get('name')}: {clip(msg.get('content', ''), args.max_output)}")
        elif role == "user":
            print(f"\n[user] {clip(msg['content'], 300 if rnd < 0 else args.max_output)}")
    if d.get("exception_info"):
        print("\n!! exception:\n" + d["exception_info"][-1500:])


if __name__ == "__main__":
    main()
