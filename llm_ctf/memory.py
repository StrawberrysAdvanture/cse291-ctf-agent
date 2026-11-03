"""Action memory: keep a short summary of each past action instead of the full transcript.

Without memory, every model call receives the whole conversation: each of the
model's earlier replies and every tool output in full. With memory enabled, a
model call receives only

    system prompt + task description + summaries of past actions + next instruction

so the prompt grows by roughly one short summary per action instead of by the
full tool output. The full transcript is still written to the log.

An *action* is one model reply (its reasoning text plus any tool calls) together
with the results of those tool calls.

Configured by the ``memory`` section of the experiment config::

    memory:
      enabled: true
      summarizer: llm          # llm: ask the model to summarize; truncate: no model call
      max_summary_words: 80    # length limit given to the llm summarizer
      max_input_chars: 4000    # action text passed to the llm summarizer (head + tail kept)
      max_memory_chars: 4000   # total size of the memory block; the oldest summaries are dropped first
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, List, Optional

from .ctflogging import status
from .tools import ToolCall, ToolResult

DEFAULTS = {
    "enabled": False,
    "summarizer": "llm",
    "max_summary_words": 80,
    "max_input_chars": 4000,
    "max_memory_chars": 4000,
    "summary_max_tokens": 256,
}


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

    def render_full(self, max_chars: Optional[int] = None) -> str:
        """Plain-text rendering of the whole action, used as summarizer input.

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
            args = ", ".join(f"{k}={shorten(v, per_value)!r}" for k, v in call_arguments(tc).items())
            parts.append(f"Tool call: {tc.name}({args})")
        for tr in self.results:
            lines = [f"Result of {tr.name}:"]
            for key, value in tr.result.items():
                if value in (None, "", False) and key != "returncode":
                    continue
                lines.append(f"  {key}: {shorten(value, per_value)}")
            parts.append("\n".join(lines))
        return "\n\n".join(parts)

    def to_log(self) -> dict:
        return {
            "step": self.step,
            "summary": self.summary,
            "summarizer": self.summarizer,
            "summary_seconds": round(self.summary_seconds, 3),
            "full_chars": len(self.render_full()),
            "summary_chars": len(self.summary or ""),
        }


class TruncationSummarizer:
    """Summarizes without a model call: tool calls plus the start and end of each output."""
    NAME = "truncate"

    def __init__(self, chars_per_value: int = 300):
        self.chars_per_value = chars_per_value

    def summarize(self, record: ActionRecord) -> str:
        parts = []
        if record.thought.strip():
            parts.append("Reasoning: " + shorten(" ".join(record.thought.split()), self.chars_per_value))
        if not record.tool_calls and not record.results:
            parts.append("No tool was called.")
        for tc in record.tool_calls:
            args = ", ".join(f"{k}={shorten(v, 200)!r}" for k, v in call_arguments(tc).items())
            parts.append(f"Called {tc.name}({args}).")
        for tr in record.results:
            fields = []
            for key, value in tr.result.items():
                if value in (None, "", False) and key != "returncode":
                    continue
                fields.append(f"{key}: {shorten(value, self.chars_per_value)}")
            parts.append(f"{tr.name} returned " + ("; ".join(fields) if fields else "nothing") + ".")
        return "\n".join(parts)


class LLMSummarizer:
    """Asks a model (by default the agent's own model) to summarize the action."""
    NAME = "llm"

    def __init__(self, client, model: str, prompt_manager, max_input_chars: int,
                 max_summary_words: int, max_tokens: int, fallback: TruncationSummarizer):
        self.client = client
        self.model = model
        self.prompt_manager = prompt_manager
        self.max_input_chars = max_input_chars
        self.max_summary_words = max_summary_words
        self.max_tokens = max_tokens
        self.fallback = fallback

    def summarize(self, record: ActionRecord) -> str:
        action_text = record.render_full(self.max_input_chars)
        # render_full shortens each value; this is a final cap for many tool calls.
        action_text = shorten(action_text, self.max_input_chars)
        prompt = self.prompt_manager.render(
            "memory_summarize",
            action=action_text,
            max_words=self.max_summary_words,
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
        record.summarizer = TruncationSummarizer.NAME
        return self.fallback.summarize(record)


class ActionMemory:
    """Ordered summaries of past actions, rendered into the prompt of the next model call."""

    def __init__(self, summarizer, prompt_manager, max_memory_chars: int):
        self.summarizer = summarizer
        self.prompt_manager = prompt_manager
        self.max_memory_chars = max_memory_chars
        self.records: List[ActionRecord] = []

    @classmethod
    def from_config(cls, config: Optional[dict], client, model: str, prompt_manager) -> Optional["ActionMemory"]:
        """Build the memory from a config dict; returns None if memory is disabled."""
        if not config:
            return None
        cfg = {**DEFAULTS, **config}
        if not cfg["enabled"]:
            return None
        fallback = TruncationSummarizer()
        if cfg["summarizer"] == "llm":
            summarizer = LLMSummarizer(
                client, cfg.get("summarizer_model") or model, prompt_manager,
                max_input_chars=int(cfg["max_input_chars"]),
                max_summary_words=int(cfg["max_summary_words"]),
                max_tokens=int(cfg["summary_max_tokens"]),
                fallback=fallback,
            )
        elif cfg["summarizer"] == "truncate":
            summarizer = fallback
        else:
            raise ValueError(f"Unknown memory summarizer {cfg['summarizer']!r}; use 'llm' or 'truncate'")
        return cls(summarizer, prompt_manager, int(cfg["max_memory_chars"]))

    def add(self, record: ActionRecord) -> ActionRecord:
        start = time.time()
        record.summarizer = self.summarizer.NAME
        record.summary = self.summarizer.summarize(record)
        record.summary_seconds = time.time() - start
        self.records.append(record)
        status.print(f"\n[Memory #{record.step}] {record.summarizer} summary, "
                     f"{len(record.render_full())} -> {len(record.summary)} chars", style="magenta bold")
        status.print(record.summary, width=status.WIDTH)
        return record

    def render(self) -> str:
        """The memory block for the next prompt; empty if there are no past actions.

        Keeps the newest summaries that fit in max_memory_chars and notes how many were dropped.
        """
        if not self.records:
            return ""
        kept: List[ActionRecord] = []
        used = 0
        for record in reversed(self.records):
            size = len(record.summary or "") + 16
            if kept and used + size > self.max_memory_chars:
                break
            kept.append(record)
            used += size
        kept.reverse()
        return self.prompt_manager.render(
            "memory",
            entries=[(r.step, r.summary) for r in kept],
            omitted=len(self.records) - len(kept),
        )

    def to_log(self) -> List[dict]:
        return [r.to_log() for r in self.records]
