"""Native hook protocol, safe transport, and version-keyed degradation."""
from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import run_seat
import seat_conversion
import seat_convert


@pytest.fixture
def gui(monkeypatch):
    monkeypatch.setenv("SC_SEAT", "gui")
    monkeypatch.setenv("SC_SHELL_FLAVOR", "dev")


@pytest.mark.parametrize("ms,seconds", [(1, 1), (999, 1), (1000, 1), (1001, 2), (120000, 120)])
def test_timeout_ceiling(ms, seconds):
    assert seat_convert.timeout_seconds(ms) == seconds


@pytest.mark.parametrize("timeout", [0, -1, None, True, False, "1000", 1.5])
def test_invalid_timeout_refuses_before_start(gui, tmp_path, timeout):
    event = {"tool_name": "Bash", "cwd": str(tmp_path),
             "tool_input": {"command": "true", "run_in_background": True, "timeout": timeout}}
    result = seat_convert.convert(event, "rewrite")["hookSpecificOutput"]
    assert result["permissionDecision"] == "deny"
    assert "positive integer" in result["permissionDecisionReason"]


def test_bridge_preserves_program_cwd_and_timeout_as_data(gui, tmp_path):
    cwd = tmp_path / "cwd ' $(touch WRONG)"
    cwd.mkdir()
    original = "printf '%s\\n' '$(touch WRONG)'\nprintf 'second'; true"
    event = {"tool_name": "Bash", "cwd": str(cwd), "tool_input": {
        "command": original, "run_in_background": True, "timeout": 1, "description": "test"}}
    result = seat_convert.convert(event, "rewrite")["hookSpecificOutput"]["updatedInput"]
    assert result["run_in_background"] is False
    assert result["timeout"] == 120000
    assert original not in result["command"]
    argv = shlex.split(result["command"])
    payload = json.loads(base64.urlsafe_b64decode(argv[-1]))
    assert payload["command"] == original
    with mock.patch.object(seat_convert.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as start:
        assert seat_convert.bridge(argv[-1]) == 0
    called = start.call_args
    assert called.kwargs == {"cwd": str(cwd), "shell": False, "check": False}
    command = called.args[0]
    assert command[-2:] == ["-c", original]
    assert command[command.index("--timeout") + 1] == "1"
    assert "/" not in command[command.index("--label") + 1]


def test_absent_timeout_is_omitted_and_start_failure_not_announced(gui, tmp_path, capsys):
    payload = {"v": 1, "command": "true", "cwd": str(tmp_path), "bash": "/bin/bash"}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    with mock.patch.object(seat_convert.subprocess, "run", return_value=subprocess.CompletedProcess([], 7)) as start:
        assert seat_convert.bridge(encoded) == 7
    assert "--timeout" not in start.call_args.args[0]
    assert "completion wakes" not in capsys.readouterr().out


def test_real_wrapper_executes_original_once_after_argv_dispatch(gui, tmp_path, monkeypatch):
    """Instrument the sc boundary; this is transport proof, not native A6."""
    root = tmp_path / "engine with ' quotes λ"
    scripts = root / ".super-coder" / "scripts"
    scripts.mkdir(parents=True)
    bridge = scripts / "seat_convert.py"
    bridge.write_text((SCRIPTS / "seat_convert.py").read_text())
    (scripts / "cli_entry.py").write_text((SCRIPTS / "cli_entry.py").read_text())
    launcher = root / "sc"
    launcher.write_text('''#!/usr/bin/env python3
import json,os,subprocess,sys
from pathlib import Path
args=sys.argv[1:]
Path("dispatch.json").write_text(json.dumps({"argv":args,"cwd":os.getcwd(),
    "premature":any(Path(p).exists() for p in ("once","dollar","backtick","result"))}))
raise SystemExit(subprocess.run(args[args.index("--")+1:],shell=False,check=False).returncode)
''')
    launcher.chmod(0o755)
    cwd = root / "cwd ' \" $(touch WRONG) `touch ALSO_WRONG`\nUnicode λ"
    cwd.mkdir()
    original = '''printf x >> once
printf '%s\\n' "double quote: $SEAT_TEST_VALUE" 'single quote: λ' \\
  "$(printf x >> dollar; printf substitution)" "`printf x >> backtick; printf tick`" \\
  | tr a-z A-Z > result
'''
    monkeypatch.setattr(seat_convert, "__file__", str(bridge))
    event = {"tool_name": "Bash", "cwd": str(cwd), "tool_input": {
        "command": original, "run_in_background": True, "timeout": 1001}}
    updated = seat_convert.convert(event, "rewrite")["hookSpecificOutput"]["updatedInput"]
    assert list(cwd.iterdir()) == []
    parsed = subprocess.run(["bash", "-n", "-c", updated["command"]], cwd=tmp_path,
                            capture_output=True, text=True, check=False)
    assert parsed.returncode == 0
    assert list(cwd.iterdir()) == []
    executed = subprocess.run(["bash", "-c", updated["command"]], cwd=tmp_path,
                              env={**os.environ, "SEAT_TEST_VALUE": "expanded λ"},
                              capture_output=True, text=True, check=False, timeout=10)
    assert executed.returncode == 0, executed.stderr
    dispatch = json.loads((cwd / "dispatch.json").read_text())
    assert dispatch["premature"] is False
    assert dispatch["cwd"] == str(cwd)
    assert dispatch["argv"][-2:] == ["-c", original]
    assert dispatch["argv"][dispatch["argv"].index("--timeout") + 1] == "2"
    for marker in ("once", "dollar", "backtick"):
        assert (cwd / marker).read_text() == "x"
    assert (cwd / "result").read_text() == "DOUBLE QUOTE: EXPANDED λ\nSINGLE QUOTE: λ\nSUBSTITUTION\nTICK\n"
    assert not (tmp_path / "WRONG").exists()
    assert not (tmp_path / "ALSO_WRONG").exists()
    assert "End your turn; the completion wakes you." in executed.stdout


@pytest.mark.parametrize("payload", [None, [], {"v": True}, {"v": 0},
    {"v": 1, "command": "true", "cwd": ".", "bash": "/bin/bash"},
    {"v": 1, "command": "true\0bad", "cwd": "/", "bash": "/bin/bash"},
])
def test_malformed_payload_never_invokes_job(payload):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    with mock.patch.object(seat_convert.subprocess, "run") as launch:
        assert seat_convert.bridge(encoded) == 2
    launch.assert_not_called()


@pytest.mark.parametrize("value", [0, -1, None, True, False, "1000", 1.5])
def test_bridge_revalidates_timeout_before_job(tmp_path, value):
    payload = {"v": 1, "command": "true", "cwd": str(tmp_path), "bash": "/bin/bash", "timeout": value}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    with mock.patch.object(seat_convert.subprocess, "run") as launch:
        assert seat_convert.bridge(encoded) == 2
    launch.assert_not_called()


def test_bad_encoding_and_non_directory_cwd_refuse_before_job(tmp_path):
    file = tmp_path / "file"
    file.touch()
    payload = {"v": 1, "command": "true", "cwd": str(file), "bash": "/bin/bash"}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    with mock.patch.object(seat_convert.subprocess, "run") as launch:
        assert seat_convert.bridge("not!base64") == 2
        assert seat_convert.bridge(encoded) == 2
        payload["cwd"] = str(tmp_path / "absent")
        assert seat_convert.bridge(base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()) == 2
    launch.assert_not_called()


@pytest.mark.parametrize("name", ["Monitor", "CronCreate", "ScheduleWakeup"])
def test_schedulers_have_engine_equivalent(gui, name):
    assert "sc job start --until" in seat_convert.convert({"tool_name": name}, "rewrite")["hookSpecificOutput"]["permissionDecisionReason"]


def test_agent_tiers_and_foreground_passthrough(gui):
    event = {"tool_name": "Agent", "tool_input": {"prompt": "hello", "run_in_background": True}}
    assert seat_convert.convert(event, "rewrite")["hookSpecificOutput"]["updatedInput"] == {"prompt": "hello", "run_in_background": False}
    assert seat_convert.convert(event, "deny-only")["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert seat_convert.convert(event, "disarmed") is None
    event["tool_input"]["run_in_background"] = False
    assert seat_convert.convert(event, "rewrite") is None


@pytest.mark.parametrize("seat,flavor", [("tui", "dev"), ("gui", "admin"), ("", "dev")])
def test_hook_exemptions_exit_without_reading_input(seat, flavor):
    result = subprocess.run(["bash", str(SCRIPTS / "seat-convert-hook.sh")], input="not JSON",
                            env={**os.environ, "SC_SEAT": seat, "SC_SHELL_FLAVOR": flavor},
                            capture_output=True, text=True, timeout=3, check=False)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


STUB = '''#!/usr/bin/env python3
import json,sys,subprocess,os,time
from pathlib import Path
if "--version" in sys.argv:
    print("2.1.289 (Claude Code)"); sys.exit(0)
count=Path(os.environ["PROBE_COUNT"])
count.write_text(str(int(count.read_text())+1) if count.exists() else "1")
mode=os.environ["PROBE_MODE"]
if mode == "hang": time.sleep(10)
if mode == "flood":
    sys.stdout.write("x" * 1048576); sys.stdout.flush()
    sys.stderr.write("y" * 1048576); sys.stderr.flush()
    time.sleep(10)
settings=json.loads(Path(sys.argv[sys.argv.index("--settings")+1]).read_text())
hook=settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
for command in ("printf SC_PROBE_ORIGINAL","printf SC_PROBE_DENIED"):
    event={"tool_name":"Bash","tool_input":{"command":command,"run_in_background":command.endswith("ORIGINAL")}}
    response={}
    if mode != "none":
        result=subprocess.run(hook,input=json.dumps(event),capture_output=True,text=True,shell=True)
        response=json.loads(result.stdout)["hookSpecificOutput"]
    if response.get("permissionDecision") == "deny":
        output=response["permissionDecisionReason"]
    else:
        effective=response.get("updatedInput",{}).get("command",command) if mode in ("all", "partial", "error", "linger") else command
        output=effective.removeprefix("printf ")
    print(json.dumps({"type":"user","message":{"content":[{"type":"tool_result","content":output}]}}), flush=True)
    if mode == "partial":
        child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"])
        print("partial probe stderr child="+str(child.pid), file=sys.stderr, flush=True)
        time.sleep(10)
    if mode == "error":
        print("native error after first result", file=sys.stderr, flush=True)
        sys.exit(3)
if mode == "linger":
    subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
'''


@pytest.fixture
def installed(tmp_path, monkeypatch):
    binary = tmp_path / "claude"
    binary.write_text(STUB)
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("PROBE_COUNT", str(tmp_path / "count"))
    monkeypatch.setattr(seat_conversion, "EVIDENCE_DIR", tmp_path / "evidence")
    return binary, tmp_path / "count"


@pytest.mark.parametrize("mode,tier", [("all", "rewrite"), ("deny", "deny-only"), ("none", "disarmed")])
def test_once_per_install_with_native_protocol_stubs(installed, monkeypatch, mode, tier):
    binary, count = installed
    monkeypatch.setenv("PROBE_MODE", mode)
    first = seat_conversion.ensure()
    assert first["tier"] == tier
    assert seat_conversion.ensure() == first
    assert count.read_text() == "1"
    assert seat_conversion.current() == first
    monkeypatch.setenv("SC_SEAT_CONVERSION_EVIDENCE", first["evidence_path"])
    assert seat_convert.evidence_tier() == tier
    binary.write_text(STUB + "\n# new installation\n")
    assert seat_conversion.current()["tier"] == "disarmed"
    assert seat_conversion.ensure()["tier"] == tier
    assert count.read_text() == "2"
    seat_conversion.ensure(force=True)
    assert count.read_text() == "3"


def test_timeout_is_durable_disarmed_and_visible_in_boot(installed, monkeypatch):
    _, count = installed
    monkeypatch.setenv("PROBE_MODE", "hang")
    monkeypatch.setattr(seat_conversion.harness_versions, "TIMEOUT", .2)
    result = seat_conversion.ensure()
    assert result["tier"] == "disarmed"
    assert "exceeded" in result["error"]
    assert seat_conversion.ensure() == result
    assert count.read_text() == "1"
    assert "disarmed" in run_seat.boot_block("dev", "gui", result)
    assert "Start long work with `sc job` yourself" in run_seat.boot_block("dev", "gui", result)


def test_tool_result_parser_does_not_trust_assistant_claim():
    transcript = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "SC_PROBE_REWRITTEN"}]}})
    assert seat_conversion._tool_results(transcript) == []


def test_partial_timeout_retains_observations_and_kills_group(installed, monkeypatch):
    _, count = installed
    monkeypatch.setenv("PROBE_MODE", "partial")
    monkeypatch.setattr(seat_conversion.harness_versions, "TIMEOUT", .5)
    result = seat_conversion.ensure()
    assert result["tier"] == "disarmed"
    assert result["hook_fired"] is True
    assert result["rewrite_honored"] is True
    assert result["deny_honored"] is None
    root = Path(result["artifact_directory"])
    assert root.stat().st_mode & 0o077 == 0
    assert "SC_PROBE_REWRITTEN" in (root / "stdout.jsonl").read_text()
    assert "partial probe stderr" in (root / "stderr.log").read_text()
    assert "SC_PROBE_ORIGINAL" in (root / "hook-calls.jsonl").read_text()
    receipt = json.loads((root / "receipt.json").read_text())
    assert receipt["capture"]["timed_out"] is True
    assert receipt["process"]["start_ticks"] > 0
    assert receipt["timings"]["hook_seconds"]
    assert receipt["cleanup"]["signal"] == "SIGKILL"
    assert len(receipt["cleanup"]["before"]["members"]) >= 2
    for member in receipt["cleanup"]["before"]["members"]:
        current = seat_conversion._process_identity(member["pid"])
        assert current is None or current["start_ticks"] != member["start_ticks"] or current["state"] == "Z"
    assert receipt["timings"]["total_seconds"] < 2
    assert seat_conversion.ensure() == result
    assert count.read_text() == "1"
    assert seat_conversion.ensure(force=True)["artifact_directory"] != str(root)
    assert count.read_text() == "2"
    assert (root / "receipt.json").exists()


def test_probe_error_retains_partial_facts(installed, monkeypatch):
    monkeypatch.setenv("PROBE_MODE", "error")
    result = seat_conversion.ensure()
    assert result["tier"] == "disarmed"
    assert result["error"] == "Probe exited 3"
    assert result["rewrite_honored"] is True
    assert result["deny_honored"] is None
    root = Path(result["artifact_directory"])
    assert "native error" in (root / "stderr.log").read_text()
    assert json.loads((root / "receipt.json").read_text())["returncode"] == 3


def test_probe_output_retention_is_bounded(installed, monkeypatch):
    monkeypatch.setenv("PROBE_MODE", "flood")
    monkeypatch.setattr(seat_conversion.harness_versions, "TIMEOUT", .5)
    result = seat_conversion.ensure()
    assert result["tier"] == "disarmed"
    assert result["hook_fired"] is None
    root = Path(result["artifact_directory"])
    for name in ("stdout.jsonl", "stderr.log"):
        assert (root / name).stat().st_size == seat_conversion.OUTPUT_LIMIT
    receipt = json.loads((root / "receipt.json").read_text())
    assert receipt["capture"]["bytes"] == {"stdout": 1048576, "stderr": 1048576}


def test_unreadable_processes_are_not_reported_absent(installed, monkeypatch):
    monkeypatch.setenv("PROBE_MODE", "all")
    monkeypatch.setattr(seat_conversion, "_group_snapshot", lambda pgid: {"members": [], "unreadable_count": 1})
    result = seat_conversion.ensure()
    receipt = json.loads((Path(result["artifact_directory"]) / "receipt.json").read_text())
    assert receipt["cleanup"]["status"] == "indeterminate"


def test_spawn_failure_retains_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(seat_conversion, "EVIDENCE_DIR", tmp_path)
    result = seat_conversion.native_probe(str(tmp_path / "missing"), timeout=.5)
    assert result["error"] == "Conversion probe failed: FileNotFoundError"
    assert result["hook_fired"] is None
    receipt = json.loads((Path(result["artifact_directory"]) / "receipt.json").read_text())
    assert receipt["observations"] == result


def test_completed_probe_cleans_remaining_group_member(installed, monkeypatch):
    monkeypatch.setenv("PROBE_MODE", "linger")
    result = seat_conversion.ensure()
    assert result["tier"] == "rewrite"
    receipt = json.loads((Path(result["artifact_directory"]) / "receipt.json").read_text())
    assert receipt["returncode"] == 0
    assert receipt["cleanup"]["signal"] == "SIGKILL"
    assert receipt["cleanup"]["before"]["members"]
    assert all(member["state"] == "Z" for member in receipt["cleanup"]["after"]["members"])
