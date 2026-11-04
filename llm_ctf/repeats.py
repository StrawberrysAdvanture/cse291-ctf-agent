"""Detection of repeated tool calls (issue I1).

A tool call is a repeat when the same tool was already called with the same
arguments earlier in the conversation. The key is the tool name plus its parsed
arguments with the tool's declared defaults filled in, so ``run_command(command="ls")``
and ``run_command(command="ls", timeout=10)`` count as the same call. A tool
without arguments has the key ``"<name> {}"``, so every later call to it is a repeat.

The conversation records every call here regardless of the mode and acts on repeats
only when the mode is not ``off``:

- ``off``: run the call as usual; repeats are only counted in the log.
- ``note``: run the call again and add a ``note`` field to its result saying which
  round made the same call and whether the output is identical.
- ``skip``: do not run the call; return the earlier result with a note. The earlier
  result may be stale if the container changed in between, so ``note`` is the
  setting to prefer for experiments.

The comparison is always against the most recent earlier call with the same key.
"""

import copy
import json
from typing import Any, Dict, List, Optional

REPEAT_MODES = ("off", "note", "skip")


def canonical_arguments(tool, parsed_arguments: Optional[dict]) -> dict:
    """The parsed arguments with the tool's declared defaults filled in, as plain JSON types."""
    args = dict(parsed_arguments or {})
    for name, info in tool.parameters.items():
        if name not in args and "default" in info:
            args[name] = info["default"]
    return json.loads(json.dumps(args, sort_keys=True, default=str))


def call_key(tool, tool_call) -> str:
    """The string that identifies a call: tool name and canonical arguments."""
    return f"{tool.name} {json.dumps(canonical_arguments(tool, tool_call.parsed_arguments), sort_keys=True)}"


class RepeatDetector:
    """Remembers the result of every tool call by key and records the repeats for the log."""

    def __init__(self, mode: str = "off"):
        if mode not in REPEAT_MODES:
            raise ValueError(f"Unknown repeat_detection mode {mode!r}; use one of {REPEAT_MODES}")
        self.mode = mode
        # key -> {"result": the most recent result (a deep copy), "rounds": every round that made the call}
        self.history: Dict[str, Dict[str, Any]] = {}
        self.repeats: List[dict] = []

    def previous(self, key: str) -> Optional[dict]:
        """The stored result and rounds of the earlier calls with this key, or None."""
        entry = self.history.get(key)
        if entry is None:
            return None
        return {"result": entry["result"], "rounds": list(entry["rounds"])}

    def remember(self, key: str, round_no: int, result: dict):
        """Store a deep copy of result as the latest result for key and add round_no to its rounds."""
        entry = self.history.get(key)
        if entry is None:
            entry = {"result": None, "rounds": []}
            self.history[key] = entry
        entry["result"] = copy.deepcopy(result)
        entry["rounds"].append(round_no)

    def add_repeat(self, *, round_no: int, earlier_rounds: List[int], tool: str, arguments: dict,
                   action: str, identical: Optional[bool]):
        """Record one repeat for the log. identical is None when the call was skipped."""
        self.repeats.append({
            "round": round_no,
            "earlier_rounds": list(earlier_rounds),
            "tool": tool,
            "arguments": arguments,
            "action": action,
            "identical": identical,
        })

    def to_log(self) -> dict:
        return {"mode": self.mode, "count": len(self.repeats), "repeats": list(self.repeats)}
