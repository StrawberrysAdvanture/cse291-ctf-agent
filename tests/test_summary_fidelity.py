"""Tests for scripts/summary_fidelity.py (recall of exact values in memory summaries) and
for the --early-calls option and raw-tail handling of scripts/paired_analysis.py.

The scripts are loaded from their files; the hand-made logs below follow the shape that
llm_ctf_solve.py writes, and one test runs the real loop to get a log from the harness.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS

from openai.types.chat import ChatCompletionMessage

from llm_ctf.ctflogging import status
from test_memory import FakeClient, run_conversation, tool_call_reply

status.set(quiet=True)

ROOT = Path(__file__).resolve().parent.parent


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sf = load_script("summary_fidelity")
pa = load_script("paired_analysis")

SYSTEM = ("You are a skilled cybersecurity professional and top CTF player. Your goal is to solve the CTF "
          "challenge and find the flag. The flag format for this CTF is flag{...}.\n\nYou have access to a "
          "Linux container environment.")
TASK = "The CTF challenge is a cryptography problem named \"demo\", worth 100 points."
FLAG = "flag{abc_123}"
URL = "http://10.0.0.5:8080/admin"
PATH = "/home/ctfplayer/ctf_files/secret.txt"


def assistant_msg(content, *commands):
    message = {"role": "assistant", "content": content}
    if commands:
        message["tool_calls"] = [{"id": str(i + 1), "type": "function",
                                  "function": {"name": "run_command", "arguments": json.dumps({"command": c})}}
                                 for i, c in enumerate(commands)]
    return message


def tool_msg(name, result, call_id="1"):
    return {"name": name, "role": "tool", "content": json.dumps(result), "tool_call_id": call_id}


def command_result(stdout, returncode=0):
    return {"stdout": stdout, "stderr": "", "returncode": returncode, "timed_out": False}


def entry(step, summary, summarizer, full_chars=400):
    return {"step": step, "summary": summary, "summarizer": summarizer, "summary_seconds": 0.1,
            "full_chars": full_chars, "summary_chars": len(summary or ""), "saved": []}


def make_log(messages, memory, memory_config, challenge="demo", solved=False, rounds=3, finish="max_rounds",
             debug_log=(), system=SYSTEM):
    return {
        "args": {"memory_config": memory_config, "model": "Qwen/Qwen2.5-7B-Instruct"},
        "messages": [["2026-01-01T00:00:00+00:00", m] for m in
                     [{"role": "system", "content": system}, {"role": "user", "content": TASK}] + messages],
        "challenge": {"category": "crypto", "challenge": challenge},
        "solved": solved,
        "rounds": rounds,
        "cost": 0,
        "debug_log": list(debug_log),
        "runtime": {"total": 10.0, "tools": 1.0, "model": 8.0},
        "finish_reason": finish,
        "prompt_tokens": [500] * rounds,
        "memory": memory,
        "memory_facts": None,
    }


def write_logs(directory, logs):
    directory.mkdir(parents=True, exist_ok=True)
    for name, log in logs.items():
        (directory / f"{name}.json").write_text(json.dumps(log))
    return directory


def handmade_log():
    messages = [
        # Action 1: two tool calls; the summary quotes the flag and the path but not the rest.
        assistant_msg("Look around.", "cat notes.txt", "curl -v http://10.0.0.5:8080/admin"),
        tool_msg("run_command", command_result(f"{FLAG}\n{PATH}\n0xdeadbeef\n"), "1"),
        tool_msg("run_command", command_result(f"Listening on {URL}\npassword=hunter22\n"), "2"),
        # Action 2: no tool call, so the loop asks the model to proceed.
        assistant_msg("Let me think about this first."),
        {"role": "user", "content": "Please proceed to the next step using your best judgment."},
        # Action 3: a tool error; the llm summarizer failed, so the entry is a truncate fallback.
        assistant_msg("Run nothing.", ""),
        tool_msg("run_command", {"error": "No command provided"}),
        # Action 4: still in the raw tail when the run ended (summary null).
        assistant_msg("List the files.", "ls"),
        tool_msg("run_command", command_result("a.txt\nb.txt\n")),
        # Action 5: the final action, which has no memory entry.
        assistant_msg("Check the flag.", "cat a.txt"),
        tool_msg("run_command", command_result("nothing here\n")),
    ]
    memory = [
        entry(1, f"Read notes.txt: it holds {FLAG} and names {PATH}. Started curl.", "llm"),
        entry(2, "Reasoning: Let me think about this first.\nNo tool was called.", "llm", full_chars=50),
        entry(3, "Called run_command(command='').\nrun_command returned error: No command provided.", "truncate", 90),
        {"step": 4, "summary": None, "summarizer": None, "summary_seconds": 0.0, "full_chars": 120,
         "summary_chars": 0, "saved": []},
    ]
    config = {"enabled": True, "summarizer": "llm", "raw_tail": 1, "flag_format": "flag{...}"}
    return make_log(messages, memory, config, rounds=5,
                    debug_log=["Memory: summarizer failed (RuntimeError: endpoint down); using truncation"])


def test_alignment_on_handmade_log(tmp_path):
    log = handmade_log()
    acts = sf.actions(log)
    assert [a.step for a in acts] == [1, 2, 3, 4, 5]
    assert [len(a.results) for a in acts] == [2, 0, 1, 1, 1]
    assert [len(a.tool_calls) for a in acts] == [2, 0, 1, 1, 1]
    assert "error: No command provided" in sf.raw_text(acts[2])
    assert sf.flag_format_of(log) == "flag{...}"
    assert sf.configured_summarizer(log) == "llm"

    logs = sf.load_logs(write_logs(tmp_path / "run", {"demo": log}))
    assert list(logs) == ["demo"]
    stats = sf.analyze(logs)

    assert stats["n_logs"] == 1 and stats["n_with_memory"] == 1
    assert stats["n_scored"] == 2            # actions 1 and 3
    assert stats["n_no_tool"] == 1           # action 2
    assert stats["n_raw_tail"] == 1          # action 4
    assert stats["n_final_without_entry"] == 1   # action 5
    assert stats["n_unaligned"] == 0

    by_kind = stats["by_kind"]
    assert by_kind[("llm", "flag")] == {"found": 1, "present": 1}
    assert by_kind[("llm", "url")] == {"found": 1, "present": 0}
    assert by_kind[("llm", "hostport")] == {"found": 1, "present": 0}
    assert by_kind[("llm", "address")] == {"found": 1, "present": 0}
    assert by_kind[("llm", "credential")] == {"found": 1, "present": 0}
    assert by_kind[("llm", "path")] == {"found": 1, "present": 1}   # the URL host is not a path
    assert not any(s == "truncate" for s, _ in by_kind)   # the error text holds no value

    worst = stats["worst"]
    assert (worst[0].challenge, worst[0].step, worst[0].summarizer) == ("demo", 1, "llm")
    assert ("url", URL) in worst[0].lost and ("address", "0xdeadbeef") in worst[0].lost
    assert ("flag", FLAG) in worst[0].present and ("path", PATH) in worst[0].present
    assert worst[0].recall == 2 / 6
    assert worst[0].compression == 400 / len(log["memory"][0]["summary"])
    assert len(worst) == 1                   # action 3 has no value to check

    assert stats["fallbacks"] == [("demo", 3)]
    assert stats["fallback_reasons"] == {"Memory: summarizer failed (RuntimeError: endpoint down); using truncation": 1}

    report = sf.render(stats, "run")
    assert report.startswith("# Summary fidelity: run\n")
    assert "Actions aligned: 2; entries skipped: raw tail (summary null) 1, no tool output 1; final actions without entry 1." in report
    assert "| llm | url | 1 | 0 | 0.0% |" in report
    assert "| llm | all kinds | 6 | 2 | 33.3% |" in report
    assert f"| demo | 1 | llm | 33% (2/6) | " in report and f"url: `{URL}`" in report
    assert "fell back to `truncate`" in report and "demo#3" in report
    assert "RuntimeError: endpoint down" in report


def test_flag_format_fallback_and_cap():
    log = handmade_log()
    log["args"]["memory_config"] = {}
    assert sf.flag_format_of(log) == "flag{...}"       # parsed from the system prompt
    log["args"]["memory_config"] = {"flag_format": "csawctf{...}"}
    assert sf.flag_format_of(log) == "csawctf{...}"
    assert sf.flag_format_of(make_log([], None, {}, system="No format here.")) is None
    assert sf.flag_format_of(make_log([], None, {}, system=SYSTEM.replace("flag{...}", "not provided"))) == "not provided"

    ten = " ".join(f"0x{i:08x}" for i in range(10))
    assert len(sf.make_extractor("flag{...}", 0).extract(ten)) == 10
    assert len(sf.make_extractor("flag{...}", 6).extract(ten)) == 6
    assert isinstance(sf.make_extractor(None, 0), sf.UncappedExtractor)


def test_verbatim_has_full_recall(tmp_path):
    output = f"{FLAG}\n{URL}\n{PATH}\n0xdeadbeef\n"
    full = ("Reasoning:\nRead it.\n\nTool call: run_command(command='cat notes.txt')\n\n"
            f"Result of run_command:\n  stdout: {output}\n  returncode: 0")
    messages = [assistant_msg("Read it.", "cat notes.txt"), tool_msg("run_command", command_result(output)),
                assistant_msg("Done.")]
    log = make_log(messages, [entry(1, full, "verbatim", len(full))],
                   {"enabled": True, "summarizer": "llm", "long_output_chars": 600, "flag_format": "flag{...}"})
    stats = sf.analyze(sf.load_logs(write_logs(tmp_path / "run", {"demo": log})))
    assert stats["worst"][0].recall == 1.0 and stats["worst"][0].lost == []
    assert stats["by_kind"][("verbatim", "url")] == {"found": 1, "present": 1}
    assert stats["verbatim"] == [1, 1]
    assert stats["fallbacks"] == []
    report = sf.render(stats, "run")
    assert "sanity check: 1/1 did" in report
    assert "No action fell back" in report


def test_baseline_logs_without_memory_are_counted(tmp_path):
    log = make_log([assistant_msg("Hi.")], None, {})
    stats = sf.analyze(sf.load_logs(write_logs(tmp_path / "run", {"base": log})))
    assert stats["n_logs"] == 1 and stats["n_with_memory"] == 0 and stats["n_scored"] == 0
    report = sf.render(stats, "run")
    assert "Logs: 1 (with memory entries: 0)" in report
    assert "no action had a value to check" in report


class FlagOnlyClient(FakeClient):
    """The llm summarizer copies the flag it saw but leaves the URL out."""

    def create(self, model, messages, **kwargs):
        if "maintain the working memory" in messages[-1]["content"]:
            self.summary_requests.append(messages)
            content = f"Ran a command; the output contained {FLAG}."
            message = ChatCompletionMessage(role="assistant", content=content)
            return NS(choices=[NS(message=message, finish_reason="stop")], usage=NS(prompt_tokens=100),
                      model_dump=lambda: {"content": content})
        return super().create(model, messages, **kwargs)


def test_on_a_log_written_by_the_harness(tmp_path):
    replies = [tool_call_reply("cat notes.txt", "Read the notes."), tool_call_reply("ls", "List."), "Done."]
    client = FlagOnlyClient(replies)
    _, log = run_conversation(tmp_path, {"enabled": True, "summarizer": "llm", "flag_format": "flag{...}"},
                              client, [f"{FLAG}\nListening on {URL}\n", "a.txt\nb.txt"], rounds=2)
    run_dir = write_logs(tmp_path / "run", {"demo": log})

    stats = sf.analyze(sf.load_logs(run_dir))
    assert stats["n_with_memory"] == 1 and stats["n_scored"] == 2 and stats["n_final_without_entry"] == 1
    first = next(r for r in stats["reports"] if r.step == 1)
    assert first.summarizer == "llm"
    assert ("flag", FLAG) in first.present
    assert ("url", URL) in first.lost
    assert stats["fallbacks"] == []

    out = tmp_path / "report.md"
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "summary_fidelity.py"), str(run_dir), "-o", str(out)],
                          capture_output=True, text=True, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    text = out.read_text()
    assert text.startswith("# Summary fidelity: run\n")
    assert proc.stdout.rstrip("\n") == text.rstrip("\n")
    assert f"url: `{URL}`" in text
    assert "| llm | flag | 1 | 1 | 100.0% |" in text


def minimal_log(solved, rounds, memory=None):
    return make_log([assistant_msg("Hi.")], memory, {}, solved=solved, rounds=rounds,
                    finish="solved" if solved else "max_rounds")


def test_paired_analysis_early_calls(tmp_path):
    raw_tail = {"step": 2, "summary": None, "summarizer": None, "summary_seconds": 0.0, "full_chars": 5000,
                "summary_chars": 0, "saved": []}
    a = sf.load_logs(write_logs(tmp_path / "a", {
        "c1": minimal_log(True, 2),
        "c2": minimal_log(True, 3),
    }))
    b = pa.load_logs(write_logs(tmp_path / "b", {
        "c1": minimal_log(True, 6, memory=[entry(1, "S" * 100, "llm", full_chars=1000), raw_tail]),
        "c2": minimal_log(True, 2, memory=[entry(1, "S" * 100, "llm", full_chars=1000)]),
    }))
    report = pa.analyze(a, b, ["a", "b"], early_calls=4)
    assert "| Solved | 2/2 (100.0%) | 2/2 (100.0%) |" in report
    assert "| Solved within the first 4 model calls | 2/2 (100.0%) | 1/2 (50.0%) |" in report
    assert "Early paired outcomes: both 1, only a 1, only b 0." in report
    # The raw-tail entry is not a summary: it is left out of the counts and the compression.
    assert "- Actions summarized: 2 (llm: 2)" in report
    assert "None" not in report.split("## Memory summaries")[1].split("## By category")[0]
    assert "Compression (action chars / summary chars): median 10.0x" in report

    report = pa.analyze(a, b, ["a", "b"], early_calls=6)
    assert "| Solved within the first 6 model calls | 2/2 (100.0%) | 2/2 (100.0%) |" in report
    assert "Early paired outcomes: both 2, only a 0, only b 0." in report
