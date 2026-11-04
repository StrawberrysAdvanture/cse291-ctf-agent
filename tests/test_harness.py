"""Tests for the harness options in llm_ctf/conversation.py: repeat detection
(llm_ctf/repeats.py, issue I1) and the nudge after a reply without a tool call (issue I9).

Uses the fakes from test_memory.py, so no GPU, model server or Docker is needed:
python -m pytest tests/
"""
import json

import pytest

from llm_ctf.prompts.prompts import PromptManager
from llm_ctf.repeats import RepeatDetector, call_key
from llm_ctf.tools import ToolCall

from test_memory import (FakeClient, FakeEnvironment, fake_challenge, run_conversation,
                         tool_call_reply, MODEL)

KEEP_GOING = "Please proceed to the next step using your best judgment."
TOOL_NAMES = ", ".join(FakeEnvironment(fake_challenge(), []).available_tools)


class SnapshotClient(FakeClient):
    """FakeClient stores the message list it was given; without memory that is the backend's
    live list, which grows after the request. This keeps a copy of each request as it was sent."""

    def create(self, model, messages, **kwargs):
        return super().create(model, list(messages), **kwargs)

# What the no_tool_call prompt shows with the XML formatter (rendered through macros.jinja).
EXAMPLE_BLOCK = (
    "```xml\n"
    "<function_calls>\n"
    "<invoke>\n"
    "<tool_name>run_command</tool_name>\n"
    "<call_id>1</call_id>\n"
    "<parameters>\n"
    "<command>ls ~/ctf_files</command>\n"
    "</parameters>\n"
    "</invoke>\n"
    "</function_calls>\n"
    "\n"
    "```\n"
)


def invoke_reply(tool, params, thought="Let me try this."):
    body = "\n".join(f"<{k}>{v}</{k}>" for k, v in params.items())
    return (f"{thought}\n\n<function_calls>\n<invoke>\n<tool_name>{tool}</tool_name>\n"
            f"<call_id>1</call_id>\n<parameters>\n{body}\n</parameters>\n</invoke>\n")


def tool_messages(log):
    return [json.loads(m["content"]) for _, m in log["messages"] if m.get("role") == "tool"]


LS_TWICE = [tool_call_reply("ls", "List the files."), tool_call_reply("ls", "List them again.")]


# ----------------------------------------------------------------------------- repeat detection

def test_repeat_detection_off_is_unchanged_but_counted(tmp_path):
    client = SnapshotClient(LS_TWICE)
    # max_rounds=3: four model calls; the third reply is prose, so the fourth request carries the keep-going text.
    _, log = run_conversation(tmp_path, {}, client, ["a", "b"], rounds=3)
    results = tool_messages(log)
    assert [r["stdout"] for r in results] == ["a", "b"]      # both calls ran
    assert all("note" not in r for r in results)
    assert log["repeat_detection"] == {
        "mode": "off", "count": 1,
        "repeats": [{"round": 2, "earlier_rounds": [1], "tool": "run_command",
                     "arguments": {"command": "ls", "timeout": 10.0}, "action": "none", "identical": False}],
    }
    assert client.agent_requests[3][-1]["content"] == KEEP_GOING


def test_repeat_note_identical_output(tmp_path):
    client = SnapshotClient(LS_TWICE)
    _, log = run_conversation(tmp_path, {}, client, ["same", "same"], repeat_detection="note")
    first, second = tool_messages(log)
    assert "note" not in first
    assert second["stdout"] == "same"                        # the call was run again
    assert second["note"] == "Same call as in round 1; the output is identical."
    # The note is the last element of the result the model sees.
    after_repeat = client.agent_requests[2][-1]["content"]
    assert "<note>Same call as in round 1; the output is identical.</note>\n</result>" in after_repeat
    repeat = log["repeat_detection"]["repeats"][0]
    assert repeat["identical"] is True and repeat["action"] == "note"
    assert log["repeat_detection"]["mode"] == "note"


def test_repeat_note_different_output(tmp_path):
    client = SnapshotClient(LS_TWICE)
    _, log = run_conversation(tmp_path, {}, client, ["one", "two"], repeat_detection="note")
    first, second = tool_messages(log)
    assert first["stdout"] == "one" and second["stdout"] == "two"
    assert second["note"] == "Same call as in round 1; the output differs."
    assert log["repeat_detection"]["repeats"][0]["identical"] is False


def test_repeat_skip_returns_earlier_result_without_running(tmp_path):
    client = SnapshotClient(LS_TWICE)
    # Only one output is available: a second run would fail with an error result.
    _, log = run_conversation(tmp_path, {}, client, ["only-once"], repeat_detection="skip")
    first, second = tool_messages(log)
    assert first == {"stdout": "only-once", "stderr": "", "returncode": 0, "timed_out": False}
    assert second["stdout"] == "only-once"
    assert second["note"].startswith("Same call as in round 1; it was not run again")
    repeat = log["repeat_detection"]["repeats"][0]
    assert repeat["identical"] is None and repeat["action"] == "skip"
    assert repeat["earlier_rounds"] == [1] and repeat["round"] == 2


def test_repeat_key_ignores_order_and_fills_defaults():
    tool = FakeEnvironment(fake_challenge(), []).available_tools["run_command"]
    short = call_key(tool, ToolCall.create_parsed("run_command", "1", {"command": "ls"}))
    full = call_key(tool, ToolCall.create_parsed("run_command", "2", {"timeout": 10.0, "command": "ls"}))
    other = call_key(tool, ToolCall.create_parsed("run_command", "3", {"command": "ls -la"}))
    assert short == full
    assert short != other
    with pytest.raises(ValueError):
        RepeatDetector("bogus")


def test_repeated_wrong_flag_is_a_repeat(tmp_path):
    client = SnapshotClient([invoke_reply("check_flag", {"flag": "flag{wrong}"}),
                         invoke_reply("check_flag", {"flag": "flag{wrong}"})])
    _, log = run_conversation(tmp_path, {}, client, [], repeat_detection="note")
    assert log["solved"] is False
    first, second = tool_messages(log)
    assert first == {"correct": False}
    assert second == {"correct": False, "note": "Same call as in round 1; the output is identical."}


@pytest.mark.parametrize("summarizer", ["truncate", "mask"])
def test_repeat_note_reaches_memory_summary(tmp_path, summarizer):
    client = SnapshotClient(LS_TWICE)
    run_conversation(tmp_path, {"enabled": True, "summarizer": summarizer}, client, ["same", "same"],
                     repeat_detection="note")
    user = client.agent_requests[2][-1]["content"]
    assert "### Action 2" in user
    assert "note: Same call as in round 1; the output is identical." in user


# ----------------------------------------------------------------------------- no-tool-call nudge

PROSE_THEN_TOOL = ["Just thinking.", tool_call_reply("id", "Check the user.")]


def test_no_tool_nudge_off_sends_keep_going_without_memory(tmp_path):
    client = SnapshotClient(PROSE_THEN_TOOL)
    _, log = run_conversation(tmp_path, {}, client, ["uid=1000"], rounds=1)
    assert client.agent_requests[1][-1]["content"] == KEEP_GOING
    assert log["no_tool_call"] == {"nudge": False, "count": 1, "rounds": [1], "nudges_sent": 1}


def test_no_tool_nudge_sends_format_example(tmp_path):
    client = SnapshotClient(PROSE_THEN_TOOL)
    _, log = run_conversation(tmp_path, {}, client, ["uid=1000"], rounds=1, no_tool_nudge=True)
    message = client.agent_requests[1][-1]["content"]
    assert "did not call any tool" in message
    assert EXAMPLE_BLOCK in message
    assert "Please proceed" not in message
    assert message.endswith(f"Available tools: {TOOL_NAMES}.")
    assert log["no_tool_call"] == {"nudge": True, "count": 1, "rounds": [1], "nudges_sent": 1}
    assert any(m.get("role") == "user" and m.get("content") == message for _, m in log["messages"])


def test_no_tool_nudge_example_parses(tmp_path):
    client = SnapshotClient(PROSE_THEN_TOOL)
    backend, _ = run_conversation(tmp_path, {}, client, ["uid=1000"], rounds=1, no_tool_nudge=True)
    message = client.agent_requests[1][-1]["content"]
    calls = backend.formatter.extract_tool_calls(message)
    assert len(calls) == 1 and calls[0].name == "run_command"
    parsed = backend.formatter.extract_params(backend.tools["run_command"], calls[0])
    assert parsed.parsed_arguments["command"] == "ls ~/ctf_files"


def test_no_tool_nudge_with_memory_is_placed_after_memory_block(tmp_path):
    client = SnapshotClient(PROSE_THEN_TOOL)
    run_conversation(tmp_path, {"enabled": True, "summarizer": "truncate"}, client, ["uid=1000"],
                     rounds=1, no_tool_nudge=True)
    task = client.agent_requests[0][-1]["content"]
    user = client.agent_requests[1][-1]["content"]
    assert user.startswith(task)
    assert "## Memory: summary of your previous actions" in user
    assert "No tool was called." in user
    assert user.index("did not call any tool") > user.index("No tool was called.")
    assert user.rstrip().endswith(f"Available tools: {TOOL_NAMES}.")
    assert "Choose your next action." not in user


def test_no_tool_nudge_without_formatter_uses_generic_text():
    text = PromptManager().no_tool_call(example=None, formatter=None, tool_names=["run_command"])
    assert "did not call any tool" in text
    assert "function-calling interface" in text
    assert "Available tools: run_command." in text
    assert "```" not in text


def test_config_override_of_no_tool_call_prompt():
    pm = PromptManager(config={"prompts": {"no_tool_call": "CUSTOM {{ tool_names|join(',') }}"}})
    assert pm.no_tool_call(example=None, formatter=None, tool_names=["a", "b"]) == "CUSTOM a,b"


# ----------------------------------------------------------------------------- lenient tool calls (issue I11)

BARE_INVOKE = ("Let me look at the files.\n\n```xml\n<invoke>\n<tool_name>run_command</tool_name>\n"
               "<call_id>1</call_id>\n<parameters>\n<command>ls ~/ctf_files</command>\n</parameters>\n</invoke>\n```\n")
SCHEMA_STYLE = ("Check the file.\n\n<function_calls>\n<invoke>\n<tool_name>run_command</tool_name>\n<call_id>1</call_id>\n"
                "<parameters>\n<parameter>\n<name>command</name>\n<type>string</type>\n<value>file x.bin</value>\n"
                "</parameter>\n</parameters>\n</invoke>\n")


class LenientClient(SnapshotClient):
    """Records the request keyword arguments and can report a different finish_reason."""

    def __init__(self, replies, finish_reason="stop", **kw):
        super().__init__(replies, **kw)
        self.finish_reason = finish_reason
        self.kwargs = []

    def create(self, model, messages, **kwargs):
        self.kwargs.append(kwargs)
        response = super().create(model, messages, **kwargs)
        response.choices[0].finish_reason = self.finish_reason
        return response


def test_bare_invoke_is_ignored_by_default(tmp_path):
    client = LenientClient([BARE_INVOKE, tool_call_reply("id", "...")])
    _, log = run_conversation(tmp_path, {}, client, ["should not be consumed"], rounds=1)
    first_reply = [m for _, m in log["messages"] if m.get("role") == "assistant"][0]
    assert not first_reply.get("tool_calls")
    assert client.agent_requests[1][-1]["content"] == KEEP_GOING   # treated as a reply without a tool call
    assert "</invoke>" not in client.kwargs[0]["stop"]
    assert log["tool_call_parsing"] == {"lenient": False, "stop_after_first_call": False}


def test_lenient_accepts_bare_invoke_and_keeps_the_thought(tmp_path):
    client = LenientClient([BARE_INVOKE, tool_call_reply("id", "...")])
    _, log = run_conversation(tmp_path, {}, client, ["a.txt"], rounds=1,
                              args_extra={"lenient_tool_calls": True})
    assert tool_messages(log)[0]["stdout"] == "a.txt"
    assert log["tool_call_parsing"]["lenient"] is True
    assert log["no_tool_call"]["count"] == 0


def test_lenient_accepts_bare_invoke_cut_off_by_the_token_cap(tmp_path):
    # Without the wrapper the </function_calls> stop sequence never fires, so the reply ends with
    # finish_reason "length"; lenient parsing must still run the call.
    client = LenientClient([BARE_INVOKE.replace("</invoke>\n```\n", ""), tool_call_reply("id", "...")], finish_reason="length")
    _, log = run_conversation(tmp_path, {}, client, ["a.txt"], rounds=1, args_extra={"lenient_tool_calls": True})
    assert tool_messages(log)[0]["stdout"] == "a.txt"


def test_lenient_accepts_schema_style_parameters(tmp_path):
    client = LenientClient([SCHEMA_STYLE, tool_call_reply("id", "...")])
    _, log = run_conversation(tmp_path, {}, client, ["x.bin: data"], rounds=1)
    assert "Missing required arguments" in tool_messages(log)[0]["error"]   # default behaviour

    client = LenientClient([SCHEMA_STYLE, tool_call_reply("id", "...")])
    _, log = run_conversation(tmp_path, {}, client, ["x.bin: data"], rounds=1, args_extra={"lenient_tool_calls": True})
    assert tool_messages(log)[0]["stdout"] == "x.bin: data"


def test_stop_after_first_call_adds_stop_sequence_and_closes_tags(tmp_path):
    # vLLM strips the matched stop sequence, so the reply ends right before </invoke>.
    cut = tool_call_reply("ls", "List.").replace("</invoke>\n", "")
    client = LenientClient([cut, tool_call_reply("id", "...")])
    _, log = run_conversation(tmp_path, {}, client, ["a.txt"], rounds=1, args_extra={"stop_after_first_call": True})
    assert "</invoke>" in client.kwargs[0]["stop"] and "</function_calls>" in client.kwargs[0]["stop"]
    assert tool_messages(log)[0]["stdout"] == "a.txt"
    assert log["tool_call_parsing"]["stop_after_first_call"] is True


# ----------------------------------------------------------------------------- output cap (issue I12)

def test_output_cap_off_by_default(tmp_path):
    big = "y" * 50000
    client = SnapshotClient([tool_call_reply("cat big", "Read it."), tool_call_reply("id", "...")])
    _, log = run_conversation(tmp_path, {}, client, [big, "u"], rounds=1)
    assert tool_messages(log)[0]["stdout"] == big
    assert log["output_cap"] == {"max_output_chars": 0, "outputs_capped": 0}


def test_output_cap_keeps_head_and_tail_and_notes_the_cut(tmp_path):
    big = "H" * 30000 + "T" * 20000
    client = SnapshotClient([tool_call_reply("cat big", "Read it."), tool_call_reply("id", "...")])
    _, log = run_conversation(tmp_path, {}, client, [big, "u"], rounds=1, max_output_chars=3000)
    out = tool_messages(log)[0]["stdout"]
    assert out.startswith("H" * 2000) and out.endswith("T" * 1000)
    assert "47000 of 50000 characters omitted by the harness" in out
    assert len(out) < 3300
    assert log["output_cap"] == {"max_output_chars": 3000, "outputs_capped": 1}
    assert tool_messages(log)[1]["stdout"] == "u"   # short outputs untouched
    # The model saw the capped text, not the full output.
    assert not any("H" * 2500 in str(m) for m in client.agent_requests[1])


def test_output_cap_lets_memory_save_the_full_output(tmp_path, monkeypatch):
    import llm_ctf.memory
    from types import SimpleNamespace
    saved = {}

    def fake_run(cmd, input=None, **kwargs):
        saved["bytes"] = input
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(llm_ctf.memory, "subprocess", SimpleNamespace(run=fake_run))
    monkeypatch.setattr(FakeEnvironment, "container", "ctr-1", raising=False)
    big = "z" * 50000
    client = SnapshotClient([tool_call_reply("cat big", "Read it."), tool_call_reply("id", "...")])
    run_conversation(tmp_path, {"enabled": True, "summarizer": "truncate", "save_outputs": True},
                     client, [big, "u"], rounds=1, max_output_chars=3000)
    assert saved["bytes"] == big.encode()
