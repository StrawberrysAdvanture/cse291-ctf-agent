"""Token estimates and output shortening without a tokenizer (issue I12).

The harness caps tool outputs before they reach the model, but it has no tokenizer: the
model runs behind an OpenAI-compatible server. In the harness-fixes run two
20,000-character outputs of escaped binary data overran the 32k context on the next call,
which a character cap cannot see. The estimate interpolates between a "prose" and a
"dense" rate by the share of each line that is made of words (runs of three or more
letters) and spaces.

The rates were fitted to the growth of vLLM's reported prompt size between consecutive
model calls over the 163 steps of the harness-fixes record (`logs/runs/20261104-050449-2c0c-
harness-32k-3r`): English prose tokenizes at about 4 characters per token, but tool output
(assembly listings, hex dumps, escaped bytes, logs) at 1.1 to 2.4, so the constants are set
so that the estimate is never below the measured count on the large steps (over 3,000
tokens) and about 35% above it on a typical step. Pessimistic is the safe side for a cap.
"""
import re

PROSE_CHARS_PER_TOKEN = 2.8
DENSE_CHARS_PER_TOKEN = 1.0
# A shortened value is never smaller than this, note included (the note alone is about 70
# tokens); shorten_tokens treats any smaller limit as this value.
MIN_KEEP_TOKENS = 120
_WORDY = re.compile(r"[A-Za-z]{3,}| ")


def chars_per_token(text: str) -> float:
    """Estimated characters per token for text, between the dense and the prose rate."""
    if not text:
        return PROSE_CHARS_PER_TOKEN
    wordy = sum(len(m) for m in _WORDY.findall(text))
    share = min(1.0, wordy / len(text))
    return DENSE_CHARS_PER_TOKEN + (PROSE_CHARS_PER_TOKEN - DENSE_CHARS_PER_TOKEN) * share


def estimate_tokens(text) -> int:
    """Rough token count of text (non-strings are counted through their str())."""
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return 0
    total = 0.0
    for line in text.split("\n"):
        if line:
            total += len(line) / chars_per_token(line)
        total += 0.5      # the newline, often merged with a neighbouring token
    return int(total) + 1


def result_tokens(result: dict) -> int:
    """Estimated tokens of a tool result's string fields."""
    return sum(estimate_tokens(v) for v in result.values() if isinstance(v, str))


def omission_note(omitted: int, total: int, reason: str) -> str:
    return (f"\n[... {omitted} of {total} characters omitted by the harness ({reason}); "
            "re-run the command through head, tail, sed -n or grep to see other parts ...]\n")


def shorten_chars(value: str, limit: int, reason: str) -> str:
    """Keep the first two thirds and the last third of `limit` characters with a note between."""
    if len(value) <= limit:
        return value
    head = limit * 2 // 3
    tail = limit - head
    return value[:head] + omission_note(len(value) - limit, len(value), reason) + value[-tail:]


def shorten_tokens(value: str, limit: int, reason: str, max_chars: int = 0) -> str:
    """Shorten value until its estimated token count (note included) is at most
    max(limit, MIN_KEEP_TOKENS), and its length at most max_chars when that is set.
    Always cuts from the original text, so the note's counts are right."""
    limit = max(int(limit), MIN_KEEP_TOKENS)
    if estimate_tokens(value) <= limit and not (max_chars and len(value) > max_chars):
        return value
    note_tokens = estimate_tokens(omission_note(len(value), len(value), reason))
    chars = int(max(limit - note_tokens, 16) * chars_per_token(value))
    if max_chars:
        chars = min(chars, max_chars)
    shortened = shorten_chars(value, max(chars, 16), reason)
    # The kept parts can be denser than the whole; tighten until the estimate fits.
    for _ in range(12):
        if estimate_tokens(shortened) <= limit or chars <= 16:
            break
        chars = max(16, int(chars * 0.8))
        shortened = shorten_chars(value, chars, reason)
    return shortened
