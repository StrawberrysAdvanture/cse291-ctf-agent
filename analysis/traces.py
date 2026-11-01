"""Load attempt logs written by llm_ctf_solve.py into a flat, analysis-friendly form."""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class ToolEvent:
    round: int
    id: str
    name: str
    arguments: Any
    output: str            # JSON-encoded tool result, as the model saw it
    latency: float
    error: Optional[str]
    returncode: Optional[int]
    timed_out: bool


@dataclass
class Attempt:
    path: Path
    set: str               # pilot / development / heldout (the logs/phase1/<set>/ directory)
    attempt: int           # index passed to llm_ctf_solve.py -i
    challenge: str         # canonical name, e.g. 2018q-cry-babycrypto
    category: str
    log: dict
    events: list = field(default_factory=list)

    @property
    def rounds(self):
        return [r for r in self.log.get("round_log", []) if r.get("model_latency") is not None]


def load_attempt(path: Path, set_name: str) -> Attempt:
    log = json.loads(path.read_text())
    attempt = int(path.parent.name.rsplit("round", 1)[-1])
    chal = log["challenge"]
    # Pair tool results with calls by order: call IDs are written by the model and may repeat
    outputs = [m.get("content") or "" for _, m in log.get("messages", []) if m.get("role") == "tool"]
    a = Attempt(path, set_name, attempt, path.stem, chal["category"], log)
    calls = [(r, c) for r in log.get("round_log", []) for c in r.get("tool_calls", [])]
    for i, (r, c) in enumerate(calls):
        a.events.append(ToolEvent(
            round=r["round"], id=c["id"], name=c["name"], arguments=c["arguments"],
            output=outputs[i] if i < len(outputs) else "", latency=c["latency"], error=c["error"],
            returncode=c["returncode"], timed_out=c["timed_out"],
        ))
    return a


def load_attempts(logroot: Path):
    """Yield every complete attempt under logroot/<set>/<name>_round<i>/<challenge>.json."""
    for path in sorted(Path(logroot).glob("*/*_round*/*.json")):
        yield load_attempt(path, path.parent.parent.name)


def true_flag(dataset_dir: Path, attempt: Attempt) -> str:
    """Read the answer key from the dataset on the host; never from the trace."""
    chal_json = Path(dataset_dir) / attempt.log["challenge"]["path"] / "challenge.json"
    return json.loads(chal_json.read_text())["flag"]
