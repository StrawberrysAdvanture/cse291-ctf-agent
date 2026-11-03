# Action memory (initial version)

Without memory, every model call receives the entire conversation: each of the model's earlier replies and every tool output in full. With the 7B baseline's 4096-token context, a few large outputs (a hexdump, a decompiled function, a directory listing) fill the window.

With **action memory**, each past action (one model reply plus the results of the tools it called) is replaced by a short summary. Each model call then receives:

```
[system prompt + tool descriptions]
[user]  challenge description
        ## Memory: summary of your previous actions
        ### Action 1
        <summary>
        ### Action 2
        <summary>
        ...
        Choose your next action.          (or "Please proceed ..." after a reply without a tool call)
```

The prompt grows by roughly one summary per action instead of by the full output. The conversation log still has the complete transcript, now including tool outputs, so summaries can be checked against the originals.

## Usage

Enable memory in the config (`config/qwen_memory.yaml` is the baseline config with memory on):

```bash
python llm_ctf_solve.py --challenge <name> --config config/qwen_memory.yaml
```

Or turn it on for any config from the command line:

```bash
python llm_ctf_solve.py --challenge <name> --config config/qwen_baseline.yaml --memory llm
python llm_ctf_solve.py --challenge <name> --config config/qwen_baseline.yaml --memory truncate
```

On AWS:

```bash
./aws/ctf-aws.sh launch --gpu-mem 24 --config config/qwen_memory.yaml --challenges all
```

Memory is currently implemented only for the `vllm` backend.

## Settings

```yaml
memory:
  enabled: true
  summarizer: llm          # llm | truncate
  max_summary_words: 80    # length limit stated in the summarizer prompt
  max_input_chars: 4000    # action text sent to the summarizer; long values keep their start and end
  max_memory_chars: 4000   # size of the memory block; the oldest summaries are dropped first
  summary_max_tokens: 256  # generation limit for one summary
  # summarizer_model: ...  # default: the agent's own model, on the same vLLM server
```

The defaults are sized for a 4096-token context. The system prompt with tool descriptions is about 850 tokens, the challenge description 150–300, and 1024 tokens are reserved for the reply. Raise both character limits for models with longer contexts.

### Summarizers

| Summarizer | How it works | Cost |
|---|---|---|
| `llm` | The model is asked (prompt: `prompts/templates/default/memory_summarize.md.jinja2`) to summarize the action in ≤ `max_summary_words` words. It must copy exact values (paths, addresses, keys, errors, flag-like strings) and must not suggest next steps. | One extra model call per action |
| `truncate` | No model call: the reasoning, the tool calls, and the start and end of each output. | None |

If an `llm` summary fails (request error or empty reply), that action falls back to `truncate`, and the log records which summarizer produced each summary.

The prompts are templates (`memory.md.jinja2`, `memory_summarize.md.jinja2`, `memory_next.md.jinja2`). Like the other prompts, a config can override them under `prompts:`.

## What is logged

Each conversation log (`<challenge>.json`) now also contains:

- `prompt_tokens`: the prompt size of every model call, as reported by vLLM. This is also recorded without memory, so runs can be compared.
- `memory`: one entry per summarized action, with `step`, `summary`, `summarizer`, `summary_seconds`, `full_chars` and `summary_chars`. The value is `null` when memory is off.
- Tool results and user messages in `messages`. The vLLM backend previously logged only the model's replies.

## Known limitations of this first version

- **Everything is summarized, including the most recent action.** The model never sees the latest output verbatim, so if the summary drops a needed detail, the model must re-run the command. Keeping the last *k* actions in full is the obvious next option to try.
- The summary of the final action is never generated, because no model call follows it.
- Summaries are not merged, so long runs drop their oldest summaries once `max_memory_chars` is reached.
- With `llm`, summarization roughly doubles the number of model calls; `summary_seconds` in the log measures the added time.

## Tests

```bash
python -m pytest tests/
```

The tests run the real conversation loop, backend, formatter and tools against a fake model client and a fake container (no GPU, server or Docker needed). They check that past outputs are replaced by summaries, that the request stays at one user message, that summarizer failures fall back to truncation, and that the baseline (memory off) still sends the full history.
