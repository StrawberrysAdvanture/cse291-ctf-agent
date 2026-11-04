"""Tests for the Ghidra wrapper in llm_ctf/tools/tools.py (issue I14: time limit).

Uses a stand-in for analyzeHeadless, so Ghidra and Java are not needed:
python -m pytest tests/
"""
import stat
import time
from types import SimpleNamespace as NS

import pytest

import llm_ctf.tools.tools as tools
from llm_ctf.ctflogging import status

status.set(quiet=True)


def fake_ghidra(tmp_path, body):
    """A script that takes analyzeHeadless's arguments; the output path is the last one."""
    script = tmp_path / "analyzeHeadless"
    script.write_text("#!/bin/bash\nout=\"${@: -1}\"\n" + body + "\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def dead(pid):
    """True once the process is gone or is a zombie (killed but not yet reaped by init)."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] == "Z"
    except FileNotFoundError:
        return True


def challenge_with_binary(tmp_path):
    chal_dir = tmp_path / "challenge"
    (chal_dir / "bin").mkdir(parents=True)
    (chal_dir / "bin" / "prog").write_bytes(b"\x7fELF")
    return NS(challenge_dir=chal_dir, category="rev")


def test_ghidra_output_is_returned_when_the_script_finishes(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "GHIDRA", fake_ghidra(tmp_path, 'echo \'{"functions": {"main": "int main(){}"}, "addresses": {}}\' > "$out"'))
    output = tmp_path / "out" / "prog.decomp.json"
    assert tools.run_ghidra_script(challenge_with_binary(tmp_path), "prog", "DecompileToJson.java", output, timeout=30) is None
    assert "main" in output.read_text()


def test_ghidra_is_killed_after_the_timeout(tmp_path, monkeypatch):
    # The stand-in starts a child that sleeps, like analyzeHeadless starting java; both must die.
    pidfile = tmp_path / "child.pid"
    monkeypatch.setattr(tools, "GHIDRA", fake_ghidra(tmp_path, f'sleep 300 & echo $! > "{pidfile}"; wait'))
    output = tmp_path / "out" / "prog.decomp.json"
    reason = tools.run_ghidra_script(challenge_with_binary(tmp_path), "prog", "DecompileToJson.java", output, timeout=1)
    assert reason == "Ghidra timed out after 1 seconds"
    assert not output.exists()
    child = int(pidfile.read_text())
    for _ in range(50):              # the sleeping child was killed with the group
        if dead(child):
            break
        time.sleep(0.1)
    assert dead(child)


def test_ghidra_failure_without_output_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "GHIDRA", fake_ghidra(tmp_path, "exit 3"))
    output = tmp_path / "out" / "prog.disas.json"
    reason = tools.run_ghidra_script(challenge_with_binary(tmp_path), "prog", "DisassembleToJson.java", output, timeout=30)
    assert reason == "Ghidra exited with status 3 without writing its output"


def test_missing_binary_is_reported_without_running_ghidra(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "GHIDRA", fake_ghidra(tmp_path, "touch \"$out\""))
    output = tmp_path / "out" / "other.decomp.json"
    reason = tools.run_ghidra_script(challenge_with_binary(tmp_path), "other", "DecompileToJson.java", output, timeout=30)
    assert reason == "binary not found in the challenge files"
    assert not output.exists()


def test_decompile_tool_reports_the_timeout_to_the_model(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "GHIDRA", fake_ghidra(tmp_path, "sleep 300"))
    monkeypatch.setattr(tools, "GHIDRA_TIMEOUT", 1.0)
    monkeypatch.setattr(tools, "SCRIPT_DIR", tmp_path)        # keep the decomp cache out of the repo
    challenge = challenge_with_binary(tmp_path)
    tool = tools.Decompile(NS(challenge=challenge))
    result = tool(path="~/ctf_files/prog", function="main")
    assert result == {"error": "Decompilation for ~/ctf_files/prog not available: Ghidra timed out after 1 seconds"}
