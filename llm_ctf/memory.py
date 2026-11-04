"""Action memory: keep a short summary of each past action instead of the full transcript.

Without memory, every model call receives the whole conversation: each of the
model's earlier replies and every tool output in full. With memory enabled, a
model call receives only

    system prompt + task description + memory block + next instruction

where the memory block holds one entry per past action, so the prompt grows by
roughly one short entry per action instead of by the full tool output. The full
transcript is still written to the log.

An *action* is one model reply (its reasoning text plus any tool calls) together
with the results of those tool calls.

Configured by the ``memory`` section of the experiment config. The first group is
the initial design (v1); the second group adds options taken from the literature
review in ``docs/memory-literature.md`` (see ``docs/memory-experiments.md`` for the
source of each option and its status). Every option in the second group is off
by default, so a v1 config behaves exactly as before::

    memory:
      enabled: true
      summarizer: llm          # llm: ask the model; truncate: head+tail of outputs; mask: hide outputs
      max_summary_words: 80    # length limit given to the llm summarizer
      max_input_chars: 4000    # action text passed to the llm summarizer (head + tail kept)
      max_memory_chars: 4000   # size of the summarized-actions part of the memory block

      raw_tail: 0              # keep the newest N actions verbatim; summarize them only when they age out
      raw_tail_chars: 2500     # head+tail cap for a verbatim action
      long_output_chars: 0     # actions whose text is at most this long are kept verbatim instead of summarized (0: off)
      save_outputs: false      # write each tool output to a file in the container and cite the path
      output_dir: ~/.agent_outputs
      facts: false             # pinned block of exact values (flag-like strings, paths, URLs, host:port, addresses, credentials)
      max_facts_chars: 1200
      flag_format: null        # e.g. "flag{...}"; llm_ctf_solve.py fills this from the challenge
      summarizer_sees_task: false  # give the llm summarizer the challenge description and the facts block
      overflow: drop           # drop: forget the oldest summaries when over budget; merge: keep one line each
      max_earlier_chars: 800   # size of the merged one-line list
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .ctflogging import status
from .tools import ToolCall, ToolResult

DEFAULTS = {
    "enabled": False,
    "summarizer": "llm",
    "max_summary_words": 80,
    "max_input_chars": 4000,
    "max_memory_chars": 4000,
    "summary_max_tokens": 256,
    # Options from the literature review (docs/memory-experiments.md). All off by default.
    "raw_tail": 0,
    "raw_tail_chars": 2500,
    "long_output_chars": 0,
    "save_outputs": False,
    "output_dir": "~/.agent_outputs",
    "facts": False,
    "max_facts_chars": 1200,
    "flag_format": None,
    "summarizer_sees_task": False,
    "overflow": "drop",
    "max_earlier_chars": 800,
}

SUMMARIZER_NAMES = ("llm", "truncate", "mask")


def shorten(text: Any, limit: int) -> str:
    """Return text with at most ~limit characters, keeping the head and the tail."""
    text = "" if text is None else str(text)
    if len(text) <= limit:
        return text
    head = max(limit * 2 // 3, 1)
    tail = max(limit - head, 0)
    omitted = len(text) - head - tail
    return f"{text[:head]}\n[... {omitted} characters omitted ...]\n{text[-tail:] if tail else ''}"


def call_arguments(tool_call: ToolCall) -> dict:
    """Best-effort readable arguments of a tool call (parsed if available, else raw text)."""
    parsed = tool_call.function.parsed_arguments
    if parsed:
        return dict(parsed)
    raw = tool_call.function.arguments
    if raw is None:
        return {}
    # The XML formatter stores the unparsed <invoke> element; use its text.
    text = raw.get_text(" ", strip=True) if hasattr(raw, "get_text") else str(raw)
    return {"raw": text}


def describe_call(tool_call: ToolCall, limit: int = 200) -> str:
    args = ", ".join(f"{k}={shorten(v, limit)!r}" for k, v in call_arguments(tool_call).items())
    return f"{tool_call.name}({args})"


def result_text(tr: ToolResult) -> str:
    """The textual output of a tool result (stdout, stderr, error and other string fields)."""
    parts = []
    for key, value in tr.result.items():
        if isinstance(value, str) and value:
            parts.append(value if key in ("stdout", "stderr") else f"{key}: {value}")
    return "\n".join(parts)


@dataclass
class ActionRecord:
    """One model reply and the results of the tools it called."""
    step: int
    thought: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    results: List[ToolResult] = field(default_factory=list)
    summary: Optional[str] = None
    summarizer: Optional[str] = None
    summary_seconds: float = 0.0
    # (tool name, path in the container, number of lines) for outputs saved by save_outputs
    saved: List[Tuple[str, str, int]] = field(default_factory=list)
    # The facts block as it was before this action, given to a task-aware summarizer.
    facts_before: Optional[str] = None

    def render_full(self, max_chars: Optional[int] = None) -> str:
        """Plain-text rendering of the whole action, used as summarizer input and for raw entries.

        If max_chars is given, long values are shortened so the text stays near that size.
        """
        parts = []
        n_values = max(1, 1 + len(self.tool_calls) + sum(len(r.result) for r in self.results))
        per_value = max(200, (max_chars or 10**9) // n_values)
        if self.thought.strip():
            parts.append("Reasoning:\n" + shorten(self.thought.strip(), per_value))
        if not self.tool_calls and not self.results:
            parts.append("(No tool was called.)")
        for tc in self.tool_calls:
            parts.append(f"Tool call: {describe_call(tc, per_value)}")
        for tr in self.results:
            lines = [f"Result of {tr.name}:"]
            for key, value in tr.result.items():
                if value in (None, "", False) and key != "returncode":
                    continue
                lines.append(f"  {key}: {shorten(value, per_value)}")
            parts.append("\n".join(lines))
        return "\n\n".join(parts)

    def one_line(self, limit: int = 140) -> str:
        """A single line for the merged 'earlier actions' list: the call and the first line of its outcome."""
        if self.tool_calls:
            what = "; ".join(describe_call(tc, 60) for tc in self.tool_calls)
        else:
            what = "no tool called: " + " ".join(self.thought.split())[:60]
        outcome = ""
        for tr in self.results:
            text = result_text(tr).strip()
            if text:
                outcome = text.splitlines()[0]
                break
            if tr.result.get("returncode") not in (None, 0):
                outcome = f"returncode {tr.result['returncode']}"
        line = f"Action {self.step}: {what}"
        if outcome:
            line += f" -> {outcome}"
        return shorten(" ".join(line.split()), limit).replace("\n", " ")

    def saved_note(self) -> str:
        if not self.saved:
            return ""
        return "\n".join(f"Full {name} output saved in the container at {path} ({n} lines)."
                         for name, path, n in self.saved)

    def to_log(self) -> dict:
        return {
            "step": self.step,
            "summary": self.summary,
            "summarizer": self.summarizer,
            "summary_seconds": round(self.summary_seconds, 3),
            "full_chars": len(self.render_full()),
            "summary_chars": len(self.summary or ""),
            "saved": self.saved,
        }


# ----------------------------------------------------------------------------- summarizers

class TruncationSummarizer:
    """Summarizes without a model call: tool calls plus the start and end of each output."""
    NAME = "truncate"

    def __init__(self, chars_per_value: int = 300):
        self.chars_per_value = chars_per_value

    def summarize(self, record: ActionRecord, task: Optional[str] = None, facts: Optional[str] = None) -> str:
        parts = []
        if record.thought.strip():
            parts.append("Reasoning: " + shorten(" ".join(record.thought.split()), self.chars_per_value))
        if not record.tool_calls and not record.results:
            parts.append("No tool was called.")
        for tc in record.tool_calls:
            parts.append(f"Called {describe_call(tc)}.")
        for tr in record.results:
            fields = []
            for key, value in tr.result.items():
                if value in (None, "", False) and key != "returncode":
                    continue
                fields.append(f"{key}: {shorten(value, self.chars_per_value)}")
            parts.append(f"{tr.name} returned " + ("; ".join(fields) if fields else "nothing") + ".")
        return "\n".join(parts)


class MaskingSummarizer:
    """Observation masking (docs/memory-experiments.md, experiment 2): keeps the reasoning and the
    tool calls, replaces each output with a one-line placeholder. No model call.

    Deviation from pure masking: the return code, the timed-out flag and short tool *error*
    messages are kept, because they are the signal that an action failed.
    """
    NAME = "mask"

    def __init__(self, chars_per_value: int = 300):
        self.chars_per_value = chars_per_value

    def summarize(self, record: ActionRecord, task: Optional[str] = None, facts: Optional[str] = None) -> str:
        parts = []
        if record.thought.strip():
            parts.append("Reasoning: " + shorten(" ".join(record.thought.split()), self.chars_per_value))
        if not record.tool_calls and not record.results:
            parts.append("No tool was called.")
        for tc in record.tool_calls:
            parts.append(f"Called {describe_call(tc)}.")
        for tr in record.results:
            output = "\n".join(str(tr.result.get(k) or "") for k in ("stdout", "stderr")).strip()
            n_lines = output.count("\n") + 1 if output else 0
            status_bits = []
            if tr.result.get("returncode") is not None:
                status_bits.append(f"returncode {tr.result['returncode']}")
            if tr.result.get("timed_out"):
                status_bits.append("timed out")
            if tr.result.get("error"):
                status_bits.append("error: " + shorten(str(tr.result["error"]), 200).rstrip("."))
            if tr.result.get("note"):   # added by repeat detection (llm_ctf/repeats.py)
                status_bits.append("note: " + str(tr.result["note"]).rstrip("."))
            placeholder = (f"output omitted ({n_lines} lines, {len(output)} chars)" if output
                           else "no output")
            parts.append(f"{tr.name}: {placeholder}" + (f"; {', '.join(status_bits)}" if status_bits else "") + ".")
        return "\n".join(parts)


class LLMSummarizer:
    """Asks a model (by default the agent's own model) to summarize the action."""
    NAME = "llm"

    def __init__(self, client, model: str, prompt_manager, max_input_chars: int,
                 max_summary_words: int, max_tokens: int, fallback):
        self.client = client
        self.model = model
        self.prompt_manager = prompt_manager
        self.max_input_chars = max_input_chars
        self.max_summary_words = max_summary_words
        self.max_tokens = max_tokens
        self.fallback = fallback

    def summarize(self, record: ActionRecord, task: Optional[str] = None, facts: Optional[str] = None) -> str:
        action_text = record.render_full(self.max_input_chars)
        # render_full shortens each value; this is a final cap for many tool calls.
        action_text = shorten(action_text, self.max_input_chars)
        prompt = self.prompt_manager.render(
            "memory_summarize",
            action=action_text,
            max_words=self.max_summary_words,
            task=task or "",
            facts=facts or "",
        )
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=self.max_tokens,
            )
            summary = (response.choices[0].message.content or "").strip()
            if summary:
                return summary
            status.debug_message("Memory: summarizer returned an empty summary; using truncation")
        except Exception as e:  # never let summarization end the run
            status.debug_message(f"Memory: summarizer failed ({type(e).__name__}: {e}); using truncation")
        record.summarizer = self.fallback.NAME
        return self.fallback.summarize(record)


# ----------------------------------------------------------------------------- facts (experiment 5)

class FactExtractor:
    """Deterministic extraction of exact values from tool outputs (no model call).

    Kinds: flag (matches the challenge's flag format or a generic name{...} pattern),
    url, hostport, path, address (0x...), credential (password/user/key/token = value).
    """
    GENERIC_FLAG = r"\b[A-Za-z0-9_]{2,16}\{[^{}\s]{3,120}\}"
    PATTERNS = [
        ("url", re.compile(r"https?://[^\s'\"<>)\]]+")),
        ("hostport", re.compile(r"\b(?:\d{1,3}(?:\.\d{1,3}){3}|[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+):\d{2,5}\b")),
        # Absolute or ~/ paths; not preceded by a word character, a dot or another slash,
        # so the host part of a URL (http://10.0.0.5/...) is not taken for a path.
        ("path", re.compile(r"(?<![\w./])~?/(?:[\w.+-]+/)*[\w.+-]+")),
        ("address", re.compile(r"\b0x[0-9a-fA-F]{4,16}\b")),
        ("credential", re.compile(r"(?i)\b(?:password|passwd|passphrase|username|user|token|api[_-]?key|secret|key)\s*[:=]\s*\S{3,80}")),
    ]
    PER_KIND_PER_ACTION = 6

    def __init__(self, flag_format: Optional[str] = None):
        flag_patterns = [self.GENERIC_FLAG]
        if flag_format and "{" in flag_format:
            # "flag{...}" -> r"flag\{[^{}\s]{1,200}\}"
            prefix = re.escape(flag_format.split("{", 1)[0])
            flag_patterns.insert(0, prefix + r"\{[^{}\s]{1,200}\}")
        self.flag_regex = re.compile("|".join(flag_patterns))

    def extract(self, text: str) -> List[Tuple[str, str]]:
        found: List[Tuple[str, str]] = []
        if not text:
            return found
        seen = set()
        for value in self.flag_regex.findall(text):
            if value not in seen:
                seen.add(value)
                found.append(("flag", value))
        for kind, regex in self.PATTERNS:
            count = 0
            for value in regex.findall(text):
                value = value.rstrip(".,;:")
                if kind == "path" and value.count("/") == 0:
                    continue
                if value in seen:
                    continue
                seen.add(value)
                found.append((kind, value))
                count += 1
                if count >= self.PER_KIND_PER_ACTION:
                    break
        return found


class FactsBlock:
    """Ordered, de-duplicated facts with a character budget; the oldest are dropped when over budget.

    (The review suggests never evicting; a budget is still needed with a 4096-token context.)
    """

    def __init__(self, max_chars: int):
        self.max_chars = max_chars
        self.facts: Dict[Tuple[str, str], int] = {}   # (kind, value) -> step first seen
        self.dropped = 0

    def add(self, items: List[Tuple[str, str]], step: int) -> int:
        new = 0
        for item in items:
            if item not in self.facts:
                self.facts[item] = step
                new += 1
        self._trim()
        return new

    def _trim(self):
        while self.facts and sum(len(k) + len(v) + 6 for k, v in self.facts) > self.max_chars:
            oldest = next(iter(self.facts))
            del self.facts[oldest]
            self.dropped += 1

    def entries(self) -> List[Tuple[str, str]]:
        return list(self.facts.keys())

    def render(self) -> str:
        return "\n".join(f"- {kind}: {value}" for kind, value in self.facts)

    def to_log(self) -> dict:
        return {"facts": [{"kind": k, "value": v, "step": s} for (k, v), s in self.facts.items()],
                "dropped": self.dropped}


# ----------------------------------------------------------------------------- saved outputs (experiment 4)

class ContainerOutputStore:
    """Writes tool outputs to files inside the agent's container so the agent can re-read them."""

    def __init__(self, container_getter: Callable[[], Optional[str]], output_dir: str):
        self.container_getter = container_getter
        self.output_dir = output_dir

    def save(self, step: int, index: int, tr: ToolResult) -> Optional[Tuple[str, str, int]]:
        text = result_text(tr)
        container = self.container_getter()
        if not text or not container:
            return None
        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", tr.name)   # "[error]" would be a glob pattern
        path = f"{self.output_dir}/step_{step}_{index}_{safe_name}.txt"
        try:
            subprocess.run(
                ["docker", "exec", "-i", container, "bash", "-c",
                 f"mkdir -p {self.output_dir} && cat > {path}"],
                input=text.encode("utf-8", errors="replace"), capture_output=True, timeout=30, check=True)
        except Exception as e:
            status.debug_message(f"Memory: could not save output to {path}: {type(e).__name__}: {e}")
            return None
        return (tr.name, path, text.count("\n") + 1)


# ----------------------------------------------------------------------------- memory

class ActionMemory:
    """Ordered entries for past actions, rendered into the prompt of the next model call."""

    def __init__(self, summarizer, prompt_manager, max_memory_chars: int, *,
                 raw_tail: int = 0, raw_tail_chars: int = 2500, long_output_chars: int = 0,
                 output_store: Optional[ContainerOutputStore] = None,
                 facts: Optional[FactsBlock] = None, fact_extractor: Optional[FactExtractor] = None,
                 summarizer_sees_task: bool = False, overflow: str = "drop", max_earlier_chars: int = 800):
        self.summarizer = summarizer
        self.prompt_manager = prompt_manager
        self.max_memory_chars = max_memory_chars
        self.raw_tail = raw_tail
        self.raw_tail_chars = raw_tail_chars
        self.long_output_chars = long_output_chars
        self.output_store = output_store
        self.facts = facts
        self.fact_extractor = fact_extractor
        self.summarizer_sees_task = summarizer_sees_task
        self.overflow = overflow
        self.max_earlier_chars = max_earlier_chars
        self.records: List[ActionRecord] = []
        self.task: Optional[str] = None

    @classmethod
    def from_config(cls, config: Optional[dict], client, model: str, prompt_manager,
                    container_getter: Optional[Callable[[], Optional[str]]] = None) -> Optional["ActionMemory"]:
        """Build the memory from a config dict; returns None if memory is disabled."""
        if not config:
            return None
        cfg = {**DEFAULTS, **config}
        if not cfg["enabled"]:
            return None
        fallback = TruncationSummarizer()
        name = cfg["summarizer"]
        if name == "llm":
            summarizer = LLMSummarizer(
                client, cfg.get("summarizer_model") or model, prompt_manager,
                max_input_chars=int(cfg["max_input_chars"]),
                max_summary_words=int(cfg["max_summary_words"]),
                max_tokens=int(cfg["summary_max_tokens"]),
                fallback=fallback,
            )
        elif name == "truncate":
            summarizer = fallback
        elif name == "mask":
            summarizer = MaskingSummarizer()
        else:
            raise ValueError(f"Unknown memory summarizer {name!r}; use one of {SUMMARIZER_NAMES}")
        if cfg["overflow"] not in ("drop", "merge"):
            raise ValueError(f"Unknown memory overflow policy {cfg['overflow']!r}; use 'drop' or 'merge'")
        output_store = None
        if cfg["save_outputs"]:
            if container_getter is None:
                raise ValueError("memory.save_outputs needs a container; the backend did not provide one")
            output_store = ContainerOutputStore(container_getter, str(cfg["output_dir"]))
        facts = FactsBlock(int(cfg["max_facts_chars"])) if cfg["facts"] else None
        extractor = FactExtractor(cfg.get("flag_format")) if cfg["facts"] else None
        return cls(summarizer, prompt_manager, int(cfg["max_memory_chars"]),
                   raw_tail=int(cfg["raw_tail"]), raw_tail_chars=int(cfg["raw_tail_chars"]),
                   long_output_chars=int(cfg["long_output_chars"]), output_store=output_store,
                   facts=facts, fact_extractor=extractor,
                   summarizer_sees_task=bool(cfg["summarizer_sees_task"]),
                   overflow=cfg["overflow"], max_earlier_chars=int(cfg["max_earlier_chars"]))

    def set_task(self, task: Optional[str]):
        """The challenge description, given to the summarizer when summarizer_sees_task is on."""
        self.task = task

    # -- adding actions

    def add(self, record: ActionRecord) -> ActionRecord:
        """Record a finished action. It is summarized now, or later if it is inside the raw tail."""
        if self.output_store is not None:
            for i, tr in enumerate(record.results):
                saved = self.output_store.save(record.step, i, tr)
                if saved:
                    record.saved.append(saved)
        if self.facts is not None:
            # Captured before this action's values are added, so a task-aware summarizer is
            # not told to omit the very values the action produced.
            record.facts_before = self.facts.render()
            items = []
            for tr in record.results:
                items += self.fact_extractor.extract(result_text(tr))
            new = self.facts.add(items, record.step)
            if new:
                status.debug_message(f"Memory: {new} new fact(s) from action {record.step}")
        self.records.append(record)
        # Summarize every record that is now outside the raw tail and has no summary yet.
        cutoff = len(self.records) - self.raw_tail
        for r in self.records[:cutoff]:
            if r.summary is None:
                self._summarize(r)
        if record.summary is None:
            status.print(f"\n[Memory #{record.step}] kept in full (raw tail)", style="magenta bold")
        return record

    def _summarize(self, record: ActionRecord):
        start = time.time()
        full = record.render_full()
        if self.long_output_chars and len(full) <= self.long_output_chars:
            record.summarizer = "verbatim"
            record.summary = full
        else:
            record.summarizer = self.summarizer.NAME
            task = self.task if self.summarizer_sees_task else None
            facts = record.facts_before if (self.summarizer_sees_task and self.facts) else None
            record.summary = self.summarizer.summarize(record, task=task, facts=facts)
        record.summary_seconds = time.time() - start
        status.print(f"\n[Memory #{record.step}] {record.summarizer} summary, "
                     f"{len(full)} -> {len(record.summary)} chars", style="magenta bold")
        status.print(record.summary, width=status.WIDTH)

    # -- rendering

    def render(self) -> str:
        """The memory block for the next prompt; empty if there are no past actions."""
        if not self.records:
            return ""
        cutoff = len(self.records) - self.raw_tail
        summarized = self.records[:max(cutoff, 0)]
        raw = self.records[max(cutoff, 0):]

        # Newest summaries that fit the budget; the rest are merged into one-liners or dropped.
        kept: List[ActionRecord] = []
        used = 0
        for record in reversed(summarized):
            size = len(record.summary or "") + len(record.saved_note()) + 16
            if kept and used + size > self.max_memory_chars:
                break
            kept.append(record)
            used += size
        kept.reverse()
        overflowed = summarized[:len(summarized) - len(kept)]
        earlier: List[str] = []
        omitted = len(overflowed)
        if self.overflow == "merge" and overflowed:
            lines = [r.one_line() for r in overflowed]
            # Keep the newest one-liners that fit the earlier-actions budget.
            total = 0
            kept_lines: List[str] = []
            for line in reversed(lines):
                if kept_lines and total + len(line) + 3 > self.max_earlier_chars:
                    break
                kept_lines.append(line)
                total += len(line) + 3
            kept_lines.reverse()
            earlier = kept_lines
            omitted = len(lines) - len(kept_lines)

        def entry_text(r: ActionRecord, text: str) -> str:
            note = r.saved_note()
            return text + ("\n" + note if note else "")

        return self.prompt_manager.render(
            "memory",
            omitted=omitted,
            earlier=earlier,
            entries=[(r.step, entry_text(r, r.summary)) for r in kept],
            raw=[(r.step, entry_text(r, r.render_full(self.raw_tail_chars))) for r in raw],
            saved_dir=self.output_store.output_dir if self.output_store else "",
            facts=self.facts.entries() if self.facts else [],
        )

    def to_log(self) -> List[dict]:
        return [r.to_log() for r in self.records]

    def facts_log(self) -> Optional[dict]:
        return self.facts.to_log() if self.facts else None
