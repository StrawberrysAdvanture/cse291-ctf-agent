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
  result may be stale if the container changed in between.
- ``skip_identical``: like ``note`` the first time a call repeats; once a repeat has
  reproduced the earlier output exactly, further repeats of that call are not run and
  get the stored result with a note. Commands whose output changes keep running, and
  a call that was never seen before (a new file written, a different command) resets
  the identical counts of every call, so an edit-then-rerun loop runs the command
  again. This is the setting to prefer: in the harness-fixes run the model re-ran
  timed-out network reads every round, and ``note`` alone did not stop it.

The comparison is always against the most recent earlier call with the same key, and
"identical" is judged on the full output, before any cap shortened it.
"""

import copy
import json
from typing import Any, Dict, List, Optional

REPEAT_MODES = ("off", "note", "skip", "skip_identical")


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
        # key -> {"result": the most recent result as sent to the model (a deep copy),
        #         "full": the same before any output cap (for the identical check),
        #         "rounds": every round that made the call, "last_run": the last round that
        #         actually ran it, "identical_repeats": repeats that reproduced the output}
        self.history: Dict[str, Dict[str, Any]] = {}
        self.repeats: List[dict] = []

    def previous(self, key: str) -> Optional[dict]:
        """The stored record of the earlier calls with this key, or None."""
        entry = self.history.get(key)
        if entry is None:
            return None
        return {"result": entry["result"], "full": entry["full"], "rounds": list(entry["rounds"]),
                "last_run": entry["last_run"], "identical_repeats": entry["identical_repeats"]}

    def should_skip(self, previous: Optional[dict]) -> bool:
        """Whether a call with this earlier record is served from the store instead of run."""
        if previous is None:
            return False
        if self.mode == "skip":
            return True
        return self.mode == "skip_identical" and previous["identical_repeats"] >= 1

    def remember(self, key: str, round_no: int, result: dict, full: Optional[dict] = None,
                 identical: bool = False, ran: bool = True):
        """Store result (and full, the uncapped version) as the latest for key and add
        round_no to its rounds. identical says whether this call was a repeat that reproduced
        the earlier output; ran is False when the result was served from the store."""
        entry = self.history.get(key)
        if entry is None:
            # A call never seen before changes what the later calls may produce.
            for other in self.history.values():
                other["identical_repeats"] = 0
            entry = {"result": None, "full": None, "rounds": [], "last_run": None, "identical_repeats": 0}
            self.history[key] = entry
        entry["result"] = copy.deepcopy(result)
        entry["full"] = copy.deepcopy(result if full is None else full)
        entry["rounds"].append(round_no)
        if ran:
            entry["last_run"] = round_no
        if identical:
            entry["identical_repeats"] += 1

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
