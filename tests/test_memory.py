"""Tests for the action memory (llm_ctf/memory.py) and its use in the vLLM backend.

Runs the real conversation loop, backend, formatter and tools against a fake
OpenAI-compatible client and a fake container, so no GPU, model server or Docker
is needed:  python -m pytest tests/
"""
import json
from argparse import Namespace
from types import SimpleNamespace as NS

import pytest
import yaml
from openai.types.chat import ChatCompletionMessage

from llm_ctf.backends import VLLMBackend
from llm_ctf.conversation import CTFConversation
from llm_ctf.environment import CTFEnvironment
from llm_ctf.memory import ActionMemory, ActionRecord, TruncationSummarizer, shorten
from llm_ctf.prompts.prompts import PromptManager
from llm_ctf.tools import ToolCall, ToolResult
from llm_ctf.ctflogging import status

status.set(quiet=True)

MODEL = "Qwen/Qwen2.5-7B-Instruct"
BIG_OUTPUT = "BIGOUTPUT-START\n" + ("0123456789abcdef" * 1500) + "\nBIGOUTPUT-END flag-fragment: abc_123"


def tool_call_reply(command, thought):
    # What the model returns: generation stops at the </function_calls> stop sequence.
    return (f"{thought}\n\n<function_calls>\n<invoke>\n<tool_name>run_command</tool_name>\n"
            f"<call_id>1</call_id>\n<parameters>\n<command>{command}</command>\n</parameters>\n</invoke>\n")


class FakeClient:
    """Stands in for openai.OpenAI; records every request."""

    def __init__(self, agent_replies, summarizer_fails=False):
        self.agent_replies = list(agent_replies)
        self.summarizer_fails = summarizer_fails
        self.agent_requests = []
        self.summary_requests = []
        self.chat = NS(completions=NS(create=self.create))

    def create(self, model, messages, **kwargs):
        text = "\n".join(m["content"] if isinstance(m, dict) else m.content for m in messages)
        if "maintain the working memory" in messages[-1]["content"]:
            self.summary_requests.append(messages)
            if self.summarizer_fails:
                raise RuntimeError("summarizer endpoint down")
            content = f"SUMMARY-{len(self.summary_requests)}: ran a command; output ended with flag-fragment: abc_123"
        else:
            self.agent_requests.append(messages)
            content = self.agent_replies.pop(0) if self.agent_replies else "I am stuck."
        message = ChatCompletionMessage(role="assistant", content=content)
        return NS(choices=[NS(message=message, finish_reason="stop")],
                  usage=NS(prompt_tokens=len(text) // 4),
                  model_dump=lambda: {"content": content})


class FakeEnvironment(CTFEnvironment):
    """Real tools, but no Docker: run_command returns canned output."""

    def __init__(self, challenge, outputs):
        super().__init__(challenge, "ctfenv", "ctfnet")
        self.outputs = list(outputs)
        self.available_tools["run_command"].run_cmd = self.fake_run_cmd

    def fake_run_cmd(self, command, timeout):
        return {"stdout": self.outputs.pop(0), "stderr": "", "returncode": 0, "timed_out": False}

    def setup(self):
        pass

    def teardown(self, *exc):
        pass


def fake_challenge():
    return NS(category="crypto", category_friendly="cryptography", name="demo", points=100,
              description="Decrypt the message.", flag="flag{real}", flag_format="flag{...}",
              server_type=None, files=[], canonical_name="demo", challenge_info={"name": "demo"},
              start_challenge_container=lambda: None, stop_challenge_container=lambda: None)


def run_conversation(tmp_path, memory_config, client, outputs, rounds=2):
    # The loop runs while rounds <= max_rounds, so max_rounds=2 gives three model calls.
    config = yaml.safe_load(open("config/qwen_baseline.yaml"))
    challenge = fake_challenge()
    environment = FakeEnvironment(challenge, outputs)
    prompt_manager = PromptManager(config=config)
    args = Namespace(hints=[], debug=False, memory_config=memory_config, model=MODEL)
    VLLMBackend.client_setup = lambda self: setattr(self, "client", client)
    backend = VLLMBackend(prompt_manager.system_message(challenge), "", environment.available_tools,
                          prompt_manager, model=MODEL, args=args)
    backend.messages = []  # Backend keeps its default message list on the class; start fresh
    logfile = tmp_path / "log.json"
    with CTFConversation(environment, challenge, prompt_manager, backend, logfile,
                         max_rounds=rounds, max_cost=1, args=args) as convo:
        convo.run()
    return backend, json.loads(logfile.read_text())


REPLIES = [
    tool_call_reply("cat big.txt", "Let me read the file."),
    tool_call_reply("ls -la", "Now list the directory."),
    "I think the answer involves abc_123.",
]


def test_shorten_keeps_head_and_tail():
    text = "A" * 1000 + "B" * 1000
    out = shorten(text, 300)
    assert out.startswith("A") and out.endswith("B")
    assert "characters omitted" in out
    assert len(out) < 400
    assert shorten("short", 300) == "short"


def test_truncation_summary_is_bounded_and_keeps_command():
    tc = ToolCall.create_parsed("run_command", "1", {"command": "cat big.txt"})
    record = ActionRecord(step=1, thought="Read it.", tool_calls=[tc],
                          results=[ToolResult("run_command", "1", {"stdout": BIG_OUTPUT, "returncode": 0})])
    summary = TruncationSummarizer().summarize(record)
    assert "cat big.txt" in summary
    assert "returncode: 0" in summary
    assert "flag-fragment: abc_123" in summary   # the end of the output is kept
    assert len(summary) < 1000 < len(BIG_OUTPUT)


def test_memory_drops_oldest_summaries_when_over_budget():
    pm = PromptManager()
    memory = ActionMemory(TruncationSummarizer(), pm, max_memory_chars=500)
    for step in range(1, 21):
        memory.add(ActionRecord(step=step, thought=f"thought number {step} " + "x" * 80))
    block = memory.render()
    assert "### Action 20" in block
    assert "### Action 1\n" not in block
    assert "earlier action(s) are omitted" in block
    assert len(block) < 1200


def test_without_memory_the_full_history_is_sent(tmp_path):
    client = FakeClient(REPLIES)
    backend, log = run_conversation(tmp_path, {}, client, [BIG_OUTPUT, "a.txt\nb.txt"])
    assert backend.memory is None
    last = client.agent_requests[-1]
    assert any("BIGOUTPUT-START" in m["content"] for m in last if isinstance(m, dict))
    assert len(client.summary_requests) == 0
    assert log["memory"] is None
    assert len(log["prompt_tokens"]) == 3


def test_with_llm_memory_past_actions_are_summarized(tmp_path):
    client = FakeClient(REPLIES)
    backend, log = run_conversation(
        tmp_path, {"enabled": True, "summarizer": "llm", "max_input_chars": 4000}, client,
        [BIG_OUTPUT, "a.txt\nb.txt"])

    # Two actions were followed by another model call, so two were summarized.
    assert len(client.summary_requests) == 2
    summarizer_prompt = client.summary_requests[0][0]["content"]
    assert "cat big.txt" in summarizer_prompt
    assert "BIGOUTPUT-END flag-fragment: abc_123" in summarizer_prompt   # tail kept
    assert len(summarizer_prompt) < 4000 + 1500                          # input capped

    first, second, third = client.agent_requests
    system_len = len(first) - 1
    for request in (second, third):
        # setup messages + exactly one user message with task, memory and instruction
        assert len(request) == system_len + 1
        user = request[-1]["content"]
        assert user.startswith(first[-1]["content"])          # the task description
        assert "## Memory: summary of your previous actions" in user
        assert "BIGOUTPUT-START" not in user                     # raw output is not resent
    assert "SUMMARY-1" in second[-1]["content"]
    assert "SUMMARY-1" in third[-1]["content"] and "SUMMARY-2" in third[-1]["content"]
    assert third[-1]["content"].rstrip().endswith("Choose your next action.")

    # The log keeps the full transcript and records the summaries.
    assert [m["step"] for m in log["memory"]] == [1, 2]
    assert log["memory"][0]["full_chars"] > 10 * log["memory"][0]["summary_chars"]
    assert any("BIGOUTPUT-START" in json.dumps(m) for _, m in log["messages"])
    assert log["prompt_tokens"][1] < log["prompt_tokens"][0] + 500


def test_no_tool_call_turn_uses_keep_going_instruction(tmp_path):
    replies = ["Thinking about it without running anything.", tool_call_reply("id", "Check user.")]
    client = FakeClient(replies)
    run_conversation(tmp_path, {"enabled": True, "summarizer": "truncate"}, client, ["uid=1000"], rounds=1)
    user = client.agent_requests[1][-1]["content"]
    assert "No tool was called." in user
    assert user.rstrip().endswith("Please proceed to the next step using your best judgment.")


def test_summarizer_failure_falls_back_to_truncation(tmp_path):
    client = FakeClient(REPLIES, summarizer_fails=True)
    _, log = run_conversation(tmp_path, {"enabled": True, "summarizer": "llm"}, client,
                              [BIG_OUTPUT, "a.txt\nb.txt"])
    assert [m["summarizer"] for m in log["memory"]] == ["truncate", "truncate"]
    assert "cat big.txt" in client.agent_requests[1][-1]["content"]


def test_unknown_summarizer_is_rejected():
    with pytest.raises(ValueError):
        ActionMemory.from_config({"enabled": True, "summarizer": "magic"}, None, MODEL, PromptManager())


def test_vllm_context_length_error_ends_run_with_context_length(tmp_path):
    import httpx
    import openai

    class OverflowClient(FakeClient):
        def create(self, model, messages, **kwargs):
            if len(self.agent_requests) >= 1:
                request = httpx.Request("POST", "http://x/v1/chat/completions")
                response = httpx.Response(400, request=request)
                raise openai.BadRequestError(
                    "Error code: 400 - {'object': 'error', 'message': \"This model's maximum context "
                    "length is 4096 tokens. However, you requested 5000 tokens.\", 'code': 400}",
                    response=response, body=None)
            return super().create(model, messages, **kwargs)

    client = OverflowClient(REPLIES)
    _, log = run_conversation(tmp_path, {}, client, [BIG_OUTPUT])
    assert log["finish_reason"] == "context_length"
