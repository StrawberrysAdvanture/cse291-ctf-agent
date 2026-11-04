"""Tests for the memory options added from the literature review (llm_ctf/memory.py):
raw tail, long-output threshold, masking summarizer, saved outputs, facts block,
summarizer that sees the task, and the merge overflow policy.

Like tests/test_memory.py, the loop tests run the real conversation loop, backend,
formatter and tools against a fake OpenAI-compatible client and a fake container.
Docker is never called: subprocess is replaced inside llm_ctf.memory where needed.
"""
import json
from types import SimpleNamespace

import pytest
import yaml

import llm_ctf.memory
from llm_ctf.ctflogging import status
from llm_ctf.memory import (ActionMemory, ActionRecord, ContainerOutputStore, FactExtractor, FactsBlock,
                            LLMSummarizer, MaskingSummarizer, TruncationSummarizer)
from llm_ctf.prompts.prompts import PromptManager
from llm_ctf.tools import ToolCall, ToolResult
from test_memory import (BIG_OUTPUT, MODEL, REPLIES, FakeClient, FakeEnvironment, fake_challenge,
                         run_conversation, tool_call_reply)

status.set(quiet=True)

# An output with one value of every kind the fact extractor knows; longer than the
# long_output_chars threshold of 600 used below, so it is summarized rather than kept.
FACT_OUTPUT = ("Listening on http://10.0.0.5:8080/admin\nflag{abc_123}\n/home/ctfplayer/ctf_files/secret.txt\n"
               "0xdeadbeef\npassword=hunter22\n" + "x" * 900)
SAVED_PATH = "~/.agent_outputs/step_1_0_run_command.txt"


def command_record(step, stdout="", thought="", command=None, result=None):
    """An ActionRecord for one run_command call (or for no call at all when command is None)."""
    record = ActionRecord(step=step, thought=thought)
    if command is not None:
        record.tool_calls = [ToolCall.create_parsed("run_command", "1", {"command": command})]
        if result is None:
            result = {"stdout": stdout, "stderr": "", "returncode": 0, "timed_out": False}
        record.results = [ToolResult("run_command", "1", result)]
    return record


def user_messages(client):
    """The final user message of every agent request (task + memory + instruction)."""
    return [request[-1]["content"] for request in client.agent_requests]


def patch_subprocess(monkeypatch, run):
    monkeypatch.setattr(llm_ctf.memory, "subprocess", SimpleNamespace(run=run))


# ----------------------------------------------------------------------------- raw tail (experiment 1)

def test_raw_tail_shows_newest_action_in_full_then_summarizes_it(tmp_path):
    client = FakeClient(REPLIES)
    _, log = run_conversation(tmp_path, {"enabled": True, "summarizer": "llm", "raw_tail": 1}, client,
                              [BIG_OUTPUT, "a.txt\nb.txt"], rounds=2)
    _, second, third = user_messages(client)

    # Second call: action 1 is in the raw tail, shown in full but shortened to raw_tail_chars.
    assert "### Action 1 (most recent, shown in full" in second
    assert "BIGOUTPUT-START" in second and "BIGOUTPUT-END" in second
    assert "characters omitted" in second
    assert "SUMMARY-" not in second

    # Third call: action 1 aged out of the tail and was summarized; action 2 is now the raw one.
    assert "### Action 1\nSUMMARY-1" in third
    assert "### Action 2 (most recent" in third and "a.txt" in third
    assert "BIGOUTPUT-START" not in third

    # Each action is summarized at most once, when it leaves the tail.
    assert len(client.summary_requests) == 1
    assert "cat big.txt" in client.summary_requests[0][0]["content"]
    assert [m["summarizer"] for m in log["memory"]] == ["llm", None]
    assert log["memory"][1]["summary"] is None and log["memory"][1]["summary_chars"] == 0


def test_raw_tail_unit():
    memory = ActionMemory(TruncationSummarizer(), PromptManager(), 4000, raw_tail=2)
    memory.add(command_record(1, "one", thought="think 1", command="cmd1"))
    memory.add(command_record(2, "two", thought="think 2", command="cmd2"))
    memory.add(command_record(3, "Z" * 10000, thought="think 3", command="cmd3"))
    assert [r.summary is not None for r in memory.records] == [True, False, False]
    block = memory.render()
    assert block.count("### Action 1\n") == 1
    assert block.count("(most recent") == 2
    assert "cmd3" in block and "ZZZZ" in block
    # The 10 kB output is shortened to the raw_tail_chars budget.
    assert len(block) < 3000


# ----------------------------------------------------------------------------- masking (experiment 2)

def test_mask_summarizer_through_the_loop(tmp_path):
    client = FakeClient(REPLIES)
    _, log = run_conversation(tmp_path, {"enabled": True, "summarizer": "mask"}, client,
                              [BIG_OUTPUT, "a.txt\nb.txt"], rounds=2)
    assert client.summary_requests == []
    second = user_messages(client)[1]
    assert "output omitted (" in second and "lines" in second
    assert "returncode 0" in second
    assert "cat big.txt" in second
    assert "BIGOUTPUT" not in second
    assert [m["summarizer"] for m in log["memory"]] == ["mask", "mask"]


def test_mask_summarizer_keeps_failure_signals():
    masker = MaskingSummarizer()
    failed = command_record(1, command="ls", result={"stdout": "", "stderr": "oops", "returncode": 1})
    assert "run_command: output omitted (1 lines, 4 chars); returncode 1." in masker.summarize(failed)
    assert "oops" not in masker.summarize(failed)

    timed_out = command_record(1, command="sleep 99",
                               result={"stdout": "", "stderr": "", "returncode": None, "timed_out": True})
    assert "run_command: no output; timed out." in masker.summarize(timed_out)

    error = command_record(1, command="", result={"error": "No command provided."})
    assert "run_command: no output; error: No command provided." in masker.summarize(error)

    long_error = command_record(1, command="x", result={"error": "E" * 500})
    summary = masker.summarize(long_error)
    assert "characters omitted" in summary and len(summary) < 400

    assert "No tool was called." in masker.summarize(ActionRecord(step=1, thought="hmm"))


# ----------------------------------------------------------------------------- long-output threshold (experiment 3)

@pytest.mark.parametrize("summarizer", ["llm", "truncate"])
def test_long_output_threshold(tmp_path, summarizer):
    client = FakeClient(REPLIES)
    _, log = run_conversation(tmp_path, {"enabled": True, "summarizer": summarizer, "long_output_chars": 600},
                              client, [FACT_OUTPUT, "a.txt\nb.txt"], rounds=2)
    # Action 1 is above the threshold and summarized; action 2 is short and kept verbatim.
    assert [m["summarizer"] for m in log["memory"]] == [summarizer, "verbatim"]
    assert len(client.summary_requests) == (1 if summarizer == "llm" else 0)
    assert log["memory"][1]["summary_chars"] == log["memory"][1]["full_chars"]
    third = user_messages(client)[2]
    assert "### Action 2\n" in third
    assert "Result of run_command:" in third.split("### Action 2\n", 1)[1]
    assert "a.txt" in third


# ----------------------------------------------------------------------------- saved outputs (experiment 4)

def test_save_outputs_runs_docker_exec_and_cites_path(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))

    patch_subprocess(monkeypatch, fake_run)
    store = ContainerOutputStore(lambda: "ctr-1", "~/.agent_outputs")
    memory = ActionMemory(TruncationSummarizer(), PromptManager(), 4000, output_store=store)
    record = memory.add(command_record(1, "hello\nworld", thought="read", command="cat f"))

    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert cmd == ["docker", "exec", "-i", "ctr-1", "bash", "-c",
                   f"mkdir -p ~/.agent_outputs && cat > {SAVED_PATH}"]
    assert kwargs["input"] == b"hello\nworld"
    assert kwargs["check"] is True and kwargs["timeout"] == 30
    assert record.saved == [("run_command", SAVED_PATH, 2)]

    block = memory.render()
    assert f"Full run_command output saved in the container at {SAVED_PATH} (2 lines)." in block
    assert "`~/.agent_outputs`" in block
    assert json.loads(json.dumps(memory.to_log()))[0]["saved"] == [["run_command", SAVED_PATH, 2]]


def test_save_outputs_failure_is_ignored(monkeypatch):
    calls = []

    def failing_run(cmd, **kwargs):
        calls.append(cmd)
        raise RuntimeError("docker is not running")

    patch_subprocess(monkeypatch, failing_run)
    before = len(status.debug_log)
    store = ContainerOutputStore(lambda: "ctr-1", "~/.agent_outputs")
    memory = ActionMemory(TruncationSummarizer(), PromptManager(), 4000, output_store=store)
    record = memory.add(command_record(1, "hello", command="cat f"))
    assert len(calls) == 1
    assert record.saved == []
    assert record.saved_note() == ""
    assert "Full run_command output saved" not in memory.render()
    assert "could not save output" in "".join(status.debug_log[before:])

    # No container yet, or nothing to save: docker is not called at all.
    store = ContainerOutputStore(lambda: None, "~/.agent_outputs")
    memory = ActionMemory(TruncationSummarizer(), PromptManager(), 4000, output_store=store)
    memory.add(command_record(1, "hello", command="cat f"))
    store = ContainerOutputStore(lambda: "ctr-1", "~/.agent_outputs")
    memory = ActionMemory(TruncationSummarizer(), PromptManager(), 4000, output_store=store)
    memory.add(command_record(2, "", command="true"))
    assert len(calls) == 1


def test_save_outputs_through_the_loop(tmp_path, monkeypatch):
    calls = []
    patch_subprocess(monkeypatch, lambda cmd, **kwargs: calls.append(cmd))
    monkeypatch.setattr(FakeEnvironment, "container", "ctr-123", raising=False)
    client = FakeClient(REPLIES)
    _, log = run_conversation(tmp_path, {"enabled": True, "summarizer": "truncate", "save_outputs": True},
                              client, [BIG_OUTPUT, "a.txt\nb.txt"], rounds=2)
    assert log["memory"][0]["saved"] == [["run_command", SAVED_PATH, BIG_OUTPUT.count("\n") + 1]]
    assert calls[0][:4] == ["docker", "exec", "-i", "ctr-123"]
    second = user_messages(client)[1]
    assert f"saved in the container at {SAVED_PATH}" in second
    assert "`~/.agent_outputs`" in second


# ----------------------------------------------------------------------------- facts (experiment 5)

def test_fact_extractor_patterns():
    extractor = FactExtractor("flag{...}")
    text = ("flag{abc_123} and csawctf{x_y} again flag{abc_123}.\n"
            "See http://10.0.0.5:8080/admin. and https://example.com/a/b, host 10.0.0.5:1337 and "
            "example.com:443 and localhost:1234\n"
            "paths /home/ctfplayer/ctf_files/secret.txt ~/ctf_files/x.bin ~ alone /tmp/a. end\n"
            "addresses 0xdeadbeef 0x401000 0x1\n"
            "password=hunter22 user: bob key = abc\n")
    found = extractor.extract(text)
    for item in [("flag", "flag{abc_123}"), ("flag", "csawctf{x_y}"),
                 ("url", "http://10.0.0.5:8080/admin"), ("url", "https://example.com/a/b"),
                 ("hostport", "10.0.0.5:8080"), ("hostport", "10.0.0.5:1337"), ("hostport", "example.com:443"),
                 ("path", "/home/ctfplayer/ctf_files/secret.txt"), ("path", "/tmp/a"),
                 ("address", "0xdeadbeef"), ("address", "0x401000"),
                 ("credential", "password=hunter22"), ("credential", "user: bob")]:
        assert item in found, item
    assert found.count(("flag", "flag{abc_123}")) == 1          # duplicates once
    assert ("path", "/tmp/a.") not in found                     # trailing punctuation stripped
    assert ("path", "~") not in found                           # ~ without a slash is not a path
    assert ("path", "~/ctf_files/x.bin") in found               # ~/ paths keep the ~
    assert ("address", "0x1") not in found                      # too short
    assert not any(v == "localhost:1234" for _, v in found)     # not matched by the hostport pattern
    assert ("path", "/10.0.0.5") not in found                   # a URL host is not a path

    ten = " ".join(f"0x{i:08x}" for i in range(10))
    addresses = [v for k, v in extractor.extract(ten) if k == "address"]
    assert len(addresses) == FactExtractor.PER_KIND_PER_ACTION == 6

    # Without a usable flag format only the generic name{...} pattern is used.
    for flag_format in ("not provided", None):
        found = FactExtractor(flag_format).extract(text)
        assert ("flag", "flag{abc_123}") in found and ("flag", "csawctf{x_y}") in found
    assert FactExtractor("flag{...}").extract("") == []


def test_facts_block_dedupes_and_evicts_oldest():
    block = FactsBlock(60)
    for i in range(10):
        assert block.add([("path", f"/tmp/f{i}")], step=i + 1) == 1
    assert block.entries() == [("path", "/tmp/f7"), ("path", "/tmp/f8"), ("path", "/tmp/f9")]
    assert block.dropped == 7
    assert block.add([("path", "/tmp/f9")], step=11) == 0
    assert block.render() == "- path: /tmp/f7\n- path: /tmp/f8\n- path: /tmp/f9"
    assert block.to_log() == {
        "facts": [{"kind": "path", "value": "/tmp/f7", "step": 8},
                  {"kind": "path", "value": "/tmp/f8", "step": 9},
                  {"kind": "path", "value": "/tmp/f9", "step": 10}],
        "dropped": 7,
    }


def test_facts_rendered_at_end_of_memory_and_logged(tmp_path):
    client = FakeClient(REPLIES)
    _, log = run_conversation(tmp_path, {"enabled": True, "summarizer": "truncate", "facts": True,
                                         "flag_format": "flag{...}"},
                              client, [FACT_OUTPUT, "a.txt\nb.txt"], rounds=2)
    third = user_messages(client)[2]
    facts_at = third.index("### Facts collected so far")
    assert facts_at > third.rindex("### Action ")
    assert facts_at < third.index("Choose your next action.")
    assert "- flag: `flag{abc_123}`" in third
    assert "- url: `http://10.0.0.5:8080/admin`" in third
    assert {"kind": "flag", "value": "flag{abc_123}", "step": 1} in log["memory_facts"]["facts"]
    assert log["memory_facts"]["dropped"] == 0

    client = FakeClient(REPLIES)
    _, log = run_conversation(tmp_path, {"enabled": True, "summarizer": "truncate"}, client,
                              [FACT_OUTPUT, "a.txt\nb.txt"], rounds=2)
    assert log["memory_facts"] is None
    assert "Facts collected" not in user_messages(client)[2]


# ----------------------------------------------------------------------------- summarizer sees the task (experiment 6)

def test_summarizer_sees_task_and_facts(tmp_path):
    client = FakeClient(REPLIES)
    run_conversation(tmp_path, {"enabled": True, "summarizer": "llm", "summarizer_sees_task": True,
                                "facts": True, "flag_format": "flag{...}"},
                     client, [FACT_OUTPUT, "a.txt\nb.txt"], rounds=2)
    # Action 1 is summarized before any facts exist; action 2's summarizer sees action 1's facts.
    first, second = (r[0]["content"] for r in client.summary_requests[:2])
    assert "<challenge>" in first and "Decrypt the message." in first
    assert "established or ruled out" in first
    assert "Facts already recorded in memory" not in first      # no facts before action 1
    assert "Facts already recorded in memory" in second
    assert "- flag: flag{abc_123}" in second                     # recorded from action 1

    client = FakeClient(REPLIES)
    run_conversation(tmp_path, {"enabled": True, "summarizer": "llm", "facts": True, "flag_format": "flag{...}"},
                     client, [FACT_OUTPUT, "a.txt\nb.txt"], rounds=2)
    prompt = client.summary_requests[0][0]["content"]
    for text in ("<challenge>", "Decrypt the message.", "Facts already recorded in memory",
                 "- flag: flag{abc_123}", "established or ruled out"):
        assert text not in prompt


# ----------------------------------------------------------------------------- merge overflow (experiment 7)

def eight_actions(**kwargs):
    memory = ActionMemory(TruncationSummarizer(), PromptManager(), 300, **kwargs)
    for i in range(1, 9):
        memory.add(command_record(i, f"out{i} line1\nline2", thought="t" * 150, command=f"cmd{i}"))
    return memory


def test_merge_overflow():
    block = eight_actions(overflow="merge", max_earlier_chars=200).render()
    assert "### Earlier actions (one line each)" in block
    for i in (5, 6, 7):
        assert f"- Action {i}: run_command(command='cmd{i}') -> out{i} line1" in block
    assert "### Action 8\n" in block
    assert "(4 earlier action(s) are omitted" in block
    assert "Action 1:" not in block and "Action 4:" not in block

    block = eight_actions(overflow="merge", max_earlier_chars=10 ** 6).render()
    assert "omitted" not in block
    for i in range(1, 8):
        assert f"- Action {i}: run_command(command='cmd{i}')" in block

    block = eight_actions(overflow="drop").render()
    assert "Earlier actions" not in block
    assert "(7 earlier action(s) are omitted" in block
    assert "### Action 8\n" in block


def test_one_line():
    line = command_record(1, "a\nb", thought="look", command="cat x").one_line()
    assert line == "Action 1: run_command(command='cat x') -> a"
    failed = command_record(1, command="cat x", result={"stdout": "", "stderr": "", "returncode": 1}).one_line()
    assert failed == "Action 1: run_command(command='cat x') -> returncode 1"
    thought_only = ActionRecord(step=3, thought="I will think about this\nfor a while " + "y" * 100).one_line()
    assert thought_only.startswith("Action 3: no tool called: I will think about this for a while")
    assert "\n" not in thought_only


# ----------------------------------------------------------------------------- configuration

def test_from_config_validation():
    pm = PromptManager()
    with pytest.raises(ValueError):
        ActionMemory.from_config({"enabled": True, "overflow": "squash"}, None, MODEL, pm)
    with pytest.raises(ValueError):
        ActionMemory.from_config({"enabled": True, "save_outputs": True}, None, MODEL, pm, container_getter=None)
    masked = ActionMemory.from_config({"enabled": True, "summarizer": "mask"}, None, MODEL, pm)
    assert isinstance(masked.summarizer, MaskingSummarizer)
    assert ActionMemory.from_config({}, None, MODEL, pm) is None
    assert ActionMemory.from_config({"enabled": False, "summarizer": "llm"}, None, MODEL, pm) is None

    config = yaml.safe_load(open("config/qwen_memory_v2.yaml"))["memory"]
    memory = ActionMemory.from_config(config, FakeClient([]), MODEL, pm, container_getter=lambda: "ctr")
    assert isinstance(memory.summarizer, LLMSummarizer)
    assert memory.raw_tail == 1
    assert memory.long_output_chars == 600
    assert memory.overflow == "merge"
    assert memory.output_store.output_dir == "~/.agent_outputs"
    assert isinstance(memory.facts, FactsBlock) and isinstance(memory.fact_extractor, FactExtractor)
    assert memory.summarizer_sees_task is True


def test_memory_v2_config_end_to_end(tmp_path, monkeypatch):
    patch_subprocess(monkeypatch, lambda cmd, **kwargs: None)
    monkeypatch.setattr(FakeEnvironment, "container", "ctr-123", raising=False)
    config = dict(yaml.safe_load(open("config/qwen_memory_v2.yaml"))["memory"])
    config["flag_format"] = fake_challenge().flag_format
    client = FakeClient(REPLIES)
    _, log = run_conversation(tmp_path, config, client, [FACT_OUTPUT, "a.txt\nb.txt"], rounds=2)
    assert log["finish_reason"] == "max_rounds"

    first = client.agent_requests[0]
    system_len = len(first) - 1
    for request in client.agent_requests[1:]:
        assert len(request) == system_len + 1
        assert request[-1]["content"].startswith(first[-1]["content"])

    last = user_messages(client)[-1]
    assert "## Memory" in last
    assert "### Action 1\nSUMMARY-1" in last
    assert f"saved in the container at {SAVED_PATH}" in last
    assert "### Action 2 (most recent" in last and "a.txt" in last
    assert "### Facts collected so far" in last and "- flag: `flag{abc_123}`" in last
    assert "`~/.agent_outputs`" in last
    assert last.rstrip().endswith("Choose your next action.")

    assert len(client.summary_requests) == 1
    assert "<challenge>" in client.summary_requests[0][0]["content"]
    assert [m["summarizer"] for m in log["memory"]] == ["llm", None]
    assert isinstance(log["memory_facts"], dict) and log["memory_facts"]["facts"]
