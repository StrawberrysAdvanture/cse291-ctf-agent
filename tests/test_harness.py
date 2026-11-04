"""Tests for the harness options in llm_ctf/conversation.py: repeat detection
(llm_ctf/repeats.py, issue I1) and the nudge after a reply without a tool call (issue I9).

Uses the fakes from test_memory.py, so no GPU, model server or Docker is needed:
python -m pytest tests/
"""
import json

import pytest

from llm_ctf.formatters.xml import XMLFormatter
from llm_ctf.prompts.prompts import PromptManager
from llm_ctf.repeats import RepeatDetector, call_key
from llm_ctf.tokens import estimate_tokens
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


LS_THRICE = LS_TWICE + [tool_call_reply("ls", "And once more.")]


def test_skip_identical_stops_rerunning_after_an_identical_repeat(tmp_path):
    client = SnapshotClient(LS_THRICE)
    # Only two outputs: the third call must be served from the store, or run_cmd fails.
    _, log = run_conversation(tmp_path, {}, client, ["same", "same"], rounds=2, repeat_detection="skip_identical")
    first, second, third = tool_messages(log)
    assert "note" not in first
    assert second["note"] == "Same call as in round 1; the output is identical."
    assert third["stdout"] == "same"
    assert third["note"] == ("Same call as in round 2; it has produced identical output 2 times, "
                             "so it was not run again. This is the result from round 2.")
    assert [r["action"] for r in log["repeat_detection"]["repeats"]] == ["note", "skip"]
    assert log["repeat_detection"]["mode"] == "skip_identical"
    assert len(FakeEnvironment.last.commands) == 2


def test_skip_identical_names_the_round_that_ran_and_resets_on_a_new_call(tmp_path):
    run = tool_call_reply("python3 s.py", "Run it.")
    client = SnapshotClient([run, run, run, run, tool_call_reply("sed -i s/a/b/ s.py", "Edit."), run])
    _, log = run_conversation(tmp_path, {}, client, ["err", "err", "edited", "FLAG"], rounds=5,
                              repeat_detection="skip_identical")
    results = tool_messages(log)
    assert [r["stdout"] for r in results] == ["err", "err", "err", "err", "edited", "FLAG"]
    assert results[2]["note"].endswith("This is the result from round 2.")
    assert results[3]["note"].endswith("This is the result from round 2.")   # round 3 ran nothing
    assert results[3]["note"].startswith("Same call as in round 3; it has produced identical output 2 times")
    assert results[5]["note"] == "Same call as in round 4; the output differs."  # ran again after the edit
    assert [r["action"] for r in log["repeat_detection"]["repeats"]] == ["note", "skip", "skip", "note"]
    assert [c for c, _ in FakeEnvironment.last.commands] == ["python3 s.py"] * 2 + ["sed -i s/a/b/ s.py", "python3 s.py"]


def test_skip_identical_keeps_running_calls_whose_output_changes(tmp_path):
    client = SnapshotClient(LS_THRICE)
    _, log = run_conversation(tmp_path, {}, client, ["a", "b", "c"], rounds=2, repeat_detection="skip_identical")
    assert [r["stdout"] for r in tool_messages(log)] == ["a", "b", "c"]
    assert [r["action"] for r in log["repeat_detection"]["repeats"]] == ["note", "note"]
    assert tool_messages(log)[2]["note"] == "Same call as in round 2; the output differs."


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
    assert log["tool_call_parsing"] == {"lenient": False, "stop_after_first_call": False, "dropped_invokes": 0}


def test_lenient_drops_invoke_fragment_cut_off_by_the_token_cap(tmp_path):
    # Issue I13: a batch of invokes that hits the token cap mid-tag. Once the closing tags are
    # re-added, the fragment parses as an invoke with an empty tool name; it must be dropped,
    # not run as "Unknown tool".
    truncated = tool_call_reply("ls", "List.") + "\nStep 2: try again\n<invoke>\n<tool_name"
    client = LenientClient([truncated, tool_call_reply("id", "...")], finish_reason="length")
    _, log = run_conversation(tmp_path, {}, client, ["a.txt", "uid=0"], rounds=1,
                              args_extra={"lenient_tool_calls": True})
    first_reply = [m for _, m in log["messages"] if m.get("role") == "assistant"][0]
    assert len(first_reply["tool_calls"]) == 1                    # the complete invoke only
    results = tool_messages(log)
    assert [r.get("stdout") for r in results] == ["a.txt", "uid=0"]
    assert not any("Unknown tool" in r.get("error", "") for r in results)
    assert log["tool_call_parsing"]["dropped_invokes"] == 1


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


# ----------------------------------------------------------------------------- programs named as tools (issue I11)

def test_program_named_as_tool_is_unknown_by_default(tmp_path):
    client = LenientClient([invoke_reply("python3", {"command": "/home/x.py"}), tool_call_reply("id", "...")])
    _, log = run_conversation(tmp_path, {}, client, ["uid=0"], rounds=1)
    assert tool_messages(log)[0] == {"error": "Unknown tool python3"}
    assert log["rerouted_calls"] == []


def test_lenient_runs_program_named_as_tool_through_run_command(tmp_path):
    client = LenientClient([invoke_reply("python3", {"command": "/home/x.py", "timeout": "30"}),
                            tool_call_reply("id", "...")])
    _, log = run_conversation(tmp_path, {}, client, ["ran", "uid=0"], rounds=1,
                              args_extra={"lenient_tool_calls": True})
    first = tool_messages(log)[0]
    assert first["stdout"] == "ran"
    assert first["note"] == ("There is no tool named 'python3'; the harness ran `python3 /home/x.py` "
                             "with run_command instead.")
    assert FakeEnvironment.last.commands == [("python3 /home/x.py", 30.0), ("id", 10.0)]
    assert log["rerouted_calls"] == [{"round": 1, "tool_name": "python3", "command": "python3 /home/x.py"}]
    # The log and the memory see the call that ran, not the name the model wrote.
    logged = [m for _, m in log["messages"] if m.get("role") == "assistant"][0]["tool_calls"][0]
    function = logged.get("function", logged)
    arguments = function["arguments"]
    arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
    assert function["name"] == "run_command" and arguments["command"] == "python3 /home/x.py"


def test_lenient_reroute_of_a_repeated_call_keeps_both_notes(tmp_path):
    call = invoke_reply("python3", {"command": "/home/x.py"})
    client = LenientClient([call, call, call, tool_call_reply("id", "...")])
    _, log = run_conversation(tmp_path, {}, client, ["same", "same", "uid=0"], rounds=3,
                              args_extra={"lenient_tool_calls": True}, repeat_detection="skip_identical")
    first, second, third, _ = tool_messages(log)
    assert first["note"].startswith("There is no tool named 'python3'")
    assert second["note"] == ("There is no tool named 'python3'; the harness ran `python3 /home/x.py` with "
                              "run_command instead. Same call as in round 1; the output is identical.")
    assert third["stdout"] == "same" and "was not run again" in third["note"] and "python3" in third["note"]
    assert len(FakeEnvironment.last.commands) == 3


@pytest.mark.parametrize("tool, params, expected", [
    ("cat", {"path": "/etc/passwd"}, ("cat /etc/passwd", 10.0)),
    ("tshark", {"command": "tshark -r x.pcap -Y 'tcp'"}, ("tshark -r x.pcap -Y 'tcp'", 10.0)),   # not doubled
    ("python3", {"args": "-c 'print(1)'"}, ("python3 -c 'print(1)'", 10.0)),
    ("python3", {"file": "solve.py", "args": "--fast"}, ("python3 solve.py --fast", 10.0)),      # all parameters, file first
    ("python3", {"command": "sudo python3 x.py"}, ("sudo python3 x.py", 10.0)),                 # program already present
    ("bash", {"command": "ls -la"}, ("ls -la", 10.0)),                                          # shells run the command as written
    ("python3", {"command": "x.py", "timeout": "30s"}, ("python3 x.py", 10.0)),                 # unparseable timeout dropped
])
def test_lenient_reroute_builds_the_command(tmp_path, tool, params, expected):
    client = LenientClient([invoke_reply(tool, params), tool_call_reply("id", "...")])
    run_conversation(tmp_path, {}, client, ["out", "uid=0"], rounds=1, args_extra={"lenient_tool_calls": True})
    assert FakeEnvironment.last.commands[0] == expected


def test_lenient_reroute_accepts_schema_style_parameters(tmp_path):
    reply = ("Run.\n\n<function_calls>\n<invoke>\n<tool_name>python3</tool_name>\n<call_id>1</call_id>\n"
             "<parameters>\n<parameter>\n<name>command</name>\n<type>string</type>\n<value>x.py</value>\n"
             "</parameter>\n</parameters>\n</invoke>\n")
    client = LenientClient([reply, tool_call_reply("id", "...")])
    run_conversation(tmp_path, {}, client, ["out", "uid=0"], rounds=1, args_extra={"lenient_tool_calls": True})
    assert FakeEnvironment.last.commands[0] == ("python3 x.py", 10.0)


def test_lenient_reroute_protects_tool_names_whatever_the_category_offers():
    # `dis` is a truncated disassemble_function even where that tool is not offered.
    from bs4 import BeautifulSoup
    tools = {"run_command": FakeEnvironment(fake_challenge(), []).available_tools["run_command"]}
    formatter = XMLFormatter(tools, PromptManager())
    formatter.lenient = True
    for name in ("dis", "run", "run_commands", "Check_Flag", "createfil"):
        invocation = BeautifulSoup(f"<invoke><tool_name>{name}</tool_name><parameters><path>/x</path></parameters></invoke>", "lxml").find("invoke")
        assert formatter.reroute_unknown_call(ToolCall.create_unparsed(name, "1", invocation), tools) is None, name
    invocation = BeautifulSoup("<invoke><tool_name>objdump</tool_name><parameters><path>/x</path></parameters></invoke>", "lxml").find("invoke")
    assert formatter.reroute_unknown_call(ToolCall.create_unparsed("objdump", "1", invocation), tools).parsed_arguments["command"] == "objdump /x"


@pytest.mark.parametrize("tool, params", [
    ("run", {"command": "ls"}),            # a truncated run_command, not a program
    ("dis", {"path": "~/ctf_files/bin"}),  # a truncated disassemble_function
    ("ls", {"flags": "-la"}),              # no parameter that could hold the arguments
    ("rm -rf", {"command": "/"}),          # not a program name
])
def test_lenient_reroute_leaves_other_unknown_tools_alone(tmp_path, tool, params):
    client = LenientClient([invoke_reply(tool, params), tool_call_reply("id", "...")])
    _, log = run_conversation(tmp_path, {}, client, ["uid=0"], rounds=1, args_extra={"lenient_tool_calls": True})
    assert tool_messages(log)[0] == {"error": f"Unknown tool {tool}"}
    assert FakeEnvironment.last.commands == [("id", 10.0)]


# ----------------------------------------------------------------------------- output cap (issue I12)

NO_CAP = {"max_output_chars": 0, "max_output_tokens": 0, "max_reply_output_tokens": 0,
          "outputs_capped": 0, "replies_capped": 0}
DENSE = "\\x8f\\x00\\x3a\\x7f" * 1250      # 20000 characters of escaped bytes, as run_command returns them


def two_calls_reply(c1, c2):
    inv = lambda i, c: (f"<invoke>\n<tool_name>run_command</tool_name>\n<call_id>{i}</call_id>\n"
                        f"<parameters>\n<command>{c}</command>\n</parameters>\n</invoke>\n")
    return "Two things.\n\n<function_calls>\n" + inv(1, c1) + inv(2, c2)


def test_output_cap_off_by_default(tmp_path):
    big = "y" * 50000
    client = SnapshotClient([tool_call_reply("cat big", "Read it."), tool_call_reply("id", "...")])
    _, log = run_conversation(tmp_path, {}, client, [big, "u"], rounds=1)
    assert tool_messages(log)[0]["stdout"] == big
    assert log["output_cap"] == NO_CAP


def test_estimate_tokens_counts_dense_text_higher():
    prose = ("the quick brown fox jumps over the lazy dog " * 100)[:4000]
    assert 1300 <= estimate_tokens(prose) <= 1600          # about 2.8 characters per token
    assert estimate_tokens(DENSE[:4000]) >= 3900            # about 1.0
    assert estimate_tokens("") == 0
    assert estimate_tokens(12345) >= 1


def test_token_cap_shortens_dense_output_more_than_the_char_cap_would(tmp_path):
    client = SnapshotClient([tool_call_reply("xxd f", "Dump."), tool_call_reply("id", "...")])
    _, log = run_conversation(tmp_path, {}, client, [DENSE, "u"], rounds=1,
                              max_output_chars=30000, max_output_tokens=1000)
    out = tool_messages(log)[0]["stdout"]
    assert "characters omitted by the harness (output limit)" in out
    assert estimate_tokens(out) <= 1000
    assert len(out) < 2000                                  # the char cap alone would have kept all 20000
    assert out.startswith("\\x8f") and out.endswith("\\x7f")
    assert log["output_cap"] == {**NO_CAP, "max_output_chars": 30000, "max_output_tokens": 1000, "outputs_capped": 1}


def test_reply_cap_bounds_all_outputs_of_one_reply_together(tmp_path):
    big = "lorem ipsum dolor sit amet consectetur " * 300     # about 4,200 estimated tokens each
    client = SnapshotClient([two_calls_reply("cat a", "cat b"), tool_call_reply("id", "...")])
    _, log = run_conversation(tmp_path, {}, client, [big, big, "u"], rounds=1,
                              max_output_tokens=8000, max_reply_output_tokens=6000)
    a, b, last = tool_messages(log)
    sizes = [estimate_tokens(a["stdout"]), estimate_tokens(b["stdout"])]
    assert sum(sizes) <= 6000 and max(sizes) <= 3000 and min(sizes) >= 2500   # an equal share each
    assert all("per-reply output limit" in r["stdout"] for r in (a, b))
    assert last["stdout"] == "u"
    assert log["output_cap"]["replies_capped"] == 1 and log["output_cap"]["outputs_capped"] == 0


def test_reply_cap_cuts_from_the_original_and_leaves_small_outputs_alone(tmp_path):
    small = "lorem ipsum dolor sit amet consectetur " * 50   # about 700 tokens
    client = SnapshotClient([two_calls_reply("xxd a", "cat b"), tool_call_reply("id", "...")])
    _, log = run_conversation(tmp_path, {}, client, [DENSE, small, "u"], rounds=1,
                              max_output_tokens=8000, max_reply_output_tokens=3000)
    a, b, _ = tool_messages(log)
    assert estimate_tokens(a["stdout"]) + estimate_tokens(b["stdout"]) <= 3000
    assert b["stdout"] == small                                           # under the level, untouched
    assert "of 20000 characters omitted by the harness (per-reply output limit)" in a["stdout"]   # counted from the original, not from the 8000-token cut
    assert a["stdout"].startswith("\\x8f") and a["stdout"].endswith("\\x7f")
    assert log["output_cap"] == {**NO_CAP, "max_output_tokens": 8000, "max_reply_output_tokens": 3000,
                                 "outputs_capped": 1, "replies_capped": 1}


def test_reply_cap_shares_the_budget_between_equal_outputs(tmp_path):
    inv = lambda i: (f"<invoke>\n<tool_name>run_command</tool_name>\n<call_id>{i}</call_id>\n"
                     f"<parameters>\n<command>xxd {i}</command>\n</parameters>\n</invoke>\n")
    reply = "Four dumps.\n\n<function_calls>\n" + "".join(inv(i) for i in range(4))
    client = SnapshotClient([reply, tool_call_reply("id", "...")])
    _, log = run_conversation(tmp_path, {}, client, [DENSE] * 4 + ["u"], rounds=1,
                              max_output_tokens=3000, max_reply_output_tokens=6000)
    outs = [r["stdout"] for r in tool_messages(log)[:4]]
    sizes = [estimate_tokens(o) for o in outs]
    assert sum(sizes) <= 6000 and max(sizes) <= 1500 and min(sizes) >= 1100
    assert all("of 20000 characters omitted" in o for o in outs)


def test_token_cap_note_counts_from_the_original_when_both_caps_apply(tmp_path):
    big = DENSE * 3                                                       # 60,000 characters
    client = SnapshotClient([tool_call_reply("xxd f", "Dump."), tool_call_reply("id", "...")])
    _, log = run_conversation(tmp_path, {}, client, [big, "u"], rounds=1,
                              max_output_chars=20000, max_output_tokens=3000)
    out = tool_messages(log)[0]["stdout"]
    assert "of 60000 characters omitted" in out
    assert estimate_tokens(out) <= 3000 and len(out) <= 20000


def test_shorten_tokens_never_goes_below_the_floor_but_always_terminates():
    from llm_ctf.tokens import shorten_tokens, MIN_KEEP_TOKENS
    for limit in (1, 40, 80, 120):
        out = shorten_tokens(DENSE, limit, "x")
        assert estimate_tokens(out) <= MIN_KEEP_TOKENS
        assert "omitted by the harness (x)" in out
    assert shorten_tokens("short", 500, "x") == "short"


def test_identical_is_judged_on_the_full_output_not_the_capped_one(tmp_path):
    a = "H" * 3000 + "MIDDLE-A" * 100 + "T" * 3000
    b = "H" * 3000 + "MIDDLE-B" * 100 + "T" * 3000
    client = SnapshotClient(LS_THRICE)
    _, log = run_conversation(tmp_path, {}, client, [a, b, b], rounds=2,
                              repeat_detection="skip_identical", max_output_chars=2000)
    results = tool_messages(log)
    assert results[1]["note"] == "Same call as in round 1; the output differs."     # the capped texts are equal
    assert results[2]["note"] == "Same call as in round 2; the output is identical."
    assert len(FakeEnvironment.last.commands) == 3


def test_reply_cap_saves_the_full_output_without_notes(tmp_path, monkeypatch):
    import llm_ctf.memory
    from types import SimpleNamespace
    saved = []

    def fake_run(cmd, input=None, **kwargs):
        saved.append(input)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(llm_ctf.memory, "subprocess", SimpleNamespace(run=fake_run))
    monkeypatch.setattr(FakeEnvironment, "container", "ctr-1", raising=False)
    big = "lorem ipsum dolor sit amet consectetur " * 300
    client = SnapshotClient(LS_TWICE + [tool_call_reply("id", "...")])
    _, log = run_conversation(tmp_path, {"enabled": True, "summarizer": "truncate", "save_outputs": True},
                              client, [big, big, "u"], rounds=2, repeat_detection="note", max_reply_output_tokens=2000)
    assert tool_messages(log)[1]["note"] == "Same call as in round 1; the output is identical."
    assert saved[0] == big.encode() and saved[1] == big.encode()       # neither the cut nor the note


def test_output_cap_keeps_head_and_tail_and_notes_the_cut(tmp_path):
    big = "H" * 30000 + "T" * 20000
    client = SnapshotClient([tool_call_reply("cat big", "Read it."), tool_call_reply("id", "...")])
    _, log = run_conversation(tmp_path, {}, client, [big, "u"], rounds=1, max_output_chars=3000)
    out = tool_messages(log)[0]["stdout"]
    assert out.startswith("H" * 2000) and out.endswith("T" * 1000)
    assert "47000 of 50000 characters omitted by the harness" in out
    assert len(out) < 3300
    assert log["output_cap"] == {**NO_CAP, "max_output_chars": 3000, "outputs_capped": 1}
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
