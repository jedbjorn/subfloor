"""Claude source-driver evidence; no native inference or live engine rows."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".super-coder/scripts"
sys.path.insert(0, str(SCRIPTS))
from conversation_adapters import claude_runtime as runtime
from conversation_runtime_contract import (
    ExecutableBinding,
    NativeControl,
    NativeReference,
    NativeSubmission,
    ProcessIdentity,
    RuntimeContext,
    RuntimeContractError,
    RuntimeIdentity,
)

ASSETS = SCRIPTS.parent / "assets/runtime/claude"
DEADLINE = lambda: time.monotonic() + 1


@pytest.fixture
def seat(tmp_path):
    worktree = tmp_path / "work"
    worktree.mkdir()
    boot = "Canonical instructions; no auto-memory."
    (worktree / "CLAUDE.md").write_text(boot)
    executable = tmp_path / "native"
    executable.write_text("captured test executable; never executed")
    state = tmp_path / "generation"
    context = RuntimeContext("gen", "chat", 1, 1, "claude", state, worktree,
        ExecutableBinding(executable, hashlib.sha256(executable.read_bytes()).hexdigest(), "test-version"),
        runtime.REVISION, hashlib.sha256(boot.encode()).hexdigest(), "canonical-policy", "unrestricted",
        boot_content=boot, env={"HOME": str(tmp_path), "PATH": os.environ["PATH"]},
        controller_endpoint=tmp_path / "controller.sock")
    driver = runtime.ClaudeRuntimeDriver()
    events = []
    driver._context, driver._emit = context, events.append
    driver._identity = RuntimeIdentity("00000000-0000-4000-a000-000000000000")
    with driver._condition:
        yield driver, context, events


def hook(seat, name, **fields):
    driver, context, _ = seat
    driver.asset({"kind": "hook", "event": {"hook_event_name": name,
        "session_id": driver._identity.root_id, "cwd": str(context.worktree), **fields}},
        peer=ProcessIdentity(999999999, 10), deadline=DEADLINE())


def prepared_assets(tmp_path, monkeypatch):
    assets = tmp_path / "assets"
    (assets / "node_modules/@modelcontextprotocol/sdk").mkdir(parents=True)
    for original in runtime.ASSETS.iterdir():
        if original.is_file():
            (assets / original.name).write_bytes(original.read_bytes())
    (assets / "node_modules/@modelcontextprotocol/sdk/package.json").write_text("{}")
    monkeypatch.setattr(runtime, "ASSETS", assets)
    monkeypatch.setattr(runtime.shutil, "which", lambda *args, **kwargs: "/captured/node")


def request(identifier="request-1"):
    return NativeSubmission(identifier, 1, "payload-sha", "User input, exactly as supplied.")


def channel_prompt(seat, identifier="request-1", **attrs):
    attributes = {"source": runtime.CHANNEL, "generation_id": "gen", "conversation_id": "chat",
        "request_id": identifier, "payload_digest": "payload-sha", **attrs}
    return "<channel " + " ".join(f'{k}="{v}"' for k, v in attributes.items()) + ">user text</channel>"


def make_ready(seat):
    driver, _, _ = seat
    driver._ready, driver._primary_state = True, "idle"
    driver._channel_peer = ProcessIdentity(999999999, 10)


def startup_dialog():
    # Exact installed 2.1.287 DevChannelsDialog/ax ConfirmCancel text;
    # source fixture only, actual native phase acceptance remains separate.
    return ("WARNING: Loading development channels\n"
        "--dangerously-load-development-channels is for local channel development only. "
        "Do not use this option to run channels you have downloaded off the internet.\n"
        "Please use --channels to run a list of approved channels.\n"
        "Channels: server:subfloor_runtime\n"
        "y. I am using this for local development\nn. Exit\nEnter y/n: ")


@pytest.fixture
def startup_seat(seat, tmp_path, monkeypatch):
    _, context, _ = seat
    prepared_assets(tmp_path, monkeypatch)
    context.executable.path.write_text("#!/usr/bin/python3\nimport sys,time\n"
        + f"print({startup_dialog()!r},end='',flush=True)\n"
        + "sys.stdin.readline()\nprint('fixture confirmation consumed',flush=True)\ntime.sleep(5)\n")
    context.executable.path.chmod(0o700)
    context = replace(context, executable=replace(context.executable,
        sha256=hashlib.sha256(context.executable.path.read_bytes()).hexdigest()))
    driver, events = runtime.ClaudeRuntimeDriver(), []
    started = time.monotonic()
    result = driver.start(context, events.append, deadline=started + 3)
    try:
        assert result.state == "needs_consent" and result.setup and time.monotonic() - started < 1
        assert not driver._ready
        yield driver, context, events, result.setup
    finally:
        driver.cleanup(deadline=time.monotonic() + 2)
        assert driver._process.poll() is not None


def enable_command(setup, **changes):
    return NativeControl("enable", 1, "digest", "enable_local_channel",
        options={"setup_id": setup.setup_id, "configuration_sha256": setup.configuration_sha256}, **changes)


def test_finite_startup_returns_early_and_explicit_confirmation_is_not_readiness(startup_seat):
    driver, _, events, setup = startup_seat
    assert setup.phase == "local_channel_development_consent"
    assert any(event.kind == "runtime.setup" and not event.partial for event in events)
    assert driver.control(enable_command(setup), deadline=DEADLINE()).state == "written"
    assert not driver._ready and not [event for event in events if event.kind == "runtime.ready"]
    assert driver.control(replace(enable_command(setup), control_id="another"), deadline=DEADLINE()).state == "rejected"


@pytest.mark.parametrize("mutation", ["stale_id", "wrong_digest", "changed_phase", "wrong_root", "changed_configuration", "changed_binary", "changed_boot", "expired"])
def test_startup_confirmation_revalidates_only_captured_phase_and_writes_nothing(startup_seat, monkeypatch, mutation):
    driver, context, events, setup = startup_seat
    command = enable_command(setup)
    if mutation == "stale_id":
        command = replace(command, options=dict(command.options) | {"setup_id": "stale"})
    elif mutation == "wrong_digest":
        command = replace(command, options=dict(command.options) | {"configuration_sha256": "a" * 64})
    elif mutation == "wrong_root":
        command = replace(command, target=NativeReference("other-root"))
    elif mutation == "changed_phase":
        with driver._condition:
            driver._observe_startup(b"\nA different startup choice\nEnter y/n: ")
        assert any(event.kind == "runtime.setup" and event.partial and event.freshness == "stale" for event in events)
    elif mutation == "changed_configuration":
        (context.state_root / "claude-runtime-settings.json").write_text("different settings")
    elif mutation == "changed_binary":
        context.executable.path.write_text("changed native executable")
    elif mutation == "changed_boot":
        (context.worktree / "CLAUDE.md").write_text("different canonical boot")
    writes = []
    monkeypatch.setattr(runtime.os, "write", lambda fd, data: writes.append(data) or len(data))
    result = driver.control(command, deadline=time.monotonic() - 1 if mutation == "expired" else DEADLINE())
    assert result.state in {"not_written", "rejected"} and writes == [] and not driver._startup_confirmed


def test_partial_startup_keyboard_write_is_unknown_and_never_replayed(startup_seat, monkeypatch):
    driver, _, _, setup = startup_seat
    writes = []
    monkeypatch.setattr(runtime.os, "write", lambda fd, data: writes.append(data) or 1)
    assert driver.control(enable_command(setup), deadline=DEADLINE()).state == "unknown"
    assert driver.control(replace(enable_command(setup), control_id="retry"), deadline=DEADLINE()).state == "rejected"
    assert writes == [b"y\r"] and not driver._ready


def test_close_withdraws_setup_and_late_channel_cannot_start_readiness(startup_seat):
    driver, context, events, setup = startup_seat
    driver._context = replace(context, capability_evidence={"submission": "compatible"})
    driver.cleanup(deadline=DEADLINE())
    assert driver._startup_setup is None
    assert driver.control(enable_command(setup), deadline=DEADLINE()).state == "rejected"
    transcript = Path(context.env["HOME"]) / ".claude/projects/exact" / (driver._identity.root_id + ".jsonl")
    driver.asset({"kind": "hook", "event": {"hook_event_name": "SessionStart",
        "session_id": driver._identity.root_id, "cwd": str(context.worktree), "transcript_path": str(transcript)}},
        peer=ProcessIdentity(999999999, 10), deadline=DEADLINE())
    driver.asset({"kind": "channel.ready"}, peer=ProcessIdentity(999999999, 10), deadline=DEADLINE())
    assert not driver._notifications and not driver._readiness_queued and not driver._ready
    assert not [event for event in events if event.kind == "runtime.ready"]


@pytest.mark.parametrize("replacement", ["Unknown warning", "Channels: server:other", "y. Other choice", "Enter y/n: n"])
def test_unrecognized_startup_text_never_creates_eligible_setup(seat, replacement):
    driver, _, events = seat
    driver._configuration_sha256 = "a" * 64
    text = startup_dialog()
    original = {"Unknown warning": "WARNING: Loading development channels", "Channels: server:other": "Channels: server:subfloor_runtime",
                "y. Other choice": "y. I am using this for local development", "Enter y/n: n": "Enter y/n: "}[replacement]
    driver._observe_startup(text.replace(original, replacement).encode())
    assert driver._startup_setup is None and not [event for event in events if event.kind == "runtime.setup"]


def test_boot_keeps_canonical_permissions_mcp_discovery_and_native_route(seat, tmp_path, monkeypatch):
    driver, context, _ = seat
    prepared_assets(tmp_path, monkeypatch)
    mcp = tmp_path / "managed.json"
    mcp.write_text('{"mcpServers":{"managed":{}}}')
    context = replace(context, managed_mcp_files=(mcp,), env=dict(context.env) | {
        "ANTHROPIC_API_KEY": "never-forward", "CLAUDE_CODE_SIMPLE": "1"})
    argv, env = driver._prepare(context)
    assert argv[0] == str(context.executable.path)
    assert "--dangerously-skip-permissions" in argv and str(mcp) in argv
    assert "--ax-screen-reader" in argv
    assert not {"--print", "--bare", "--restricted", "--setting-sources"} & set(argv)
    assert "ANTHROPIC_API_KEY" not in env and "CLAUDE_CODE_SIMPLE" not in env
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    settings = json.loads((context.state_root / "claude-runtime-settings.json").read_text())
    assert settings["permissions"]["deny"] == ["CronCreate"]
    assert settings["autoMemoryEnabled"] is False
    assert (context.state_root / "claude-runtime-settings.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("failure", ["boot", "executable", "permission", "provider"])
def test_boot_refuses_changed_or_unsupported_identity(seat, tmp_path, monkeypatch, failure):
    driver, context, _ = seat
    prepared_assets(tmp_path, monkeypatch)
    if failure == "boot":
        (context.worktree / "CLAUDE.md").write_text("different")
    elif failure == "executable":
        context.executable.path.write_text("different")
    elif failure == "permission":
        context = replace(context, permission_mode="unknown-mode")
    else:
        context = replace(context, provider="paid-api")
    with pytest.raises(RuntimeContractError):
        driver._prepare(context)


def test_native_initialization_does_not_equal_consent_or_ready(seat):
    driver, context, events = seat
    driver._context = replace(context, capability_evidence={"submission": "compatible"})
    transcript = Path(context.env["HOME"]) / ".claude/projects/exact" / (driver._identity.root_id + ".jsonl")
    hook(seat, "SessionStart", transcript_path=str(transcript))
    driver.asset({"kind": "channel.ready"}, peer=ProcessIdentity(999999999, 10), deadline=DEADLINE())
    assert not driver._ready
    assert driver.submit(request(), deadline=DEADLINE()).state == "not_written"
    assert len(driver._notifications) == 1
    nonce = driver._readiness_nonce
    hook(seat, "UserPromptSubmit", prompt_id="ready-prompt",
         prompt=channel_prompt(seat, nonce, readiness_nonce=nonce))
    driver._transcript_record({"sessionId": driver._identity.root_id, "type": "assistant",
        "promptId": "ready-prompt", "message": {"id": "observed-native-message",
        "content": [{"type": "text", "text": nonce}]}})
    hook(seat, "Stop", prompt_id="ready-prompt", background_tasks=[], session_crons=[])
    assert not driver._ready  # Pre-terminal Stop can be blocked by another hook.
    driver._transcript_record({"sessionId": driver._identity.root_id, "type": "system",
        "promptId": "ready-prompt", "subtype": "turn_duration"})
    assert driver._ready
    ready = [event for event in events if event.kind == "runtime.ready"]
    assert ready[0].data["inference_turns"] == 1
    assert ready[0].data["observed_model_message_ids"] == 1
    assert not [event for event in events if event.kind == "output.final"]


@pytest.mark.parametrize("failure", ["StopFailure", "failed_terminal", "missing_reply", "wrong_reply", "wrong_terminal_source"])
def test_failed_or_unanswered_challenge_never_certifies_readiness(seat, failure):
    driver, context, events = seat
    driver._context = replace(context, capability_evidence={"submission": "compatible"})
    transcript = Path(context.env["HOME"]) / ".claude/projects/exact" / (driver._identity.root_id + ".jsonl")
    hook(seat, "SessionStart", transcript_path=str(transcript))
    driver.asset({"kind": "channel.ready"}, peer=ProcessIdentity(999999999, 10), deadline=DEADLINE())
    nonce = driver._readiness_nonce
    hook(seat, "UserPromptSubmit", prompt_id="challenge", prompt=channel_prompt(seat, nonce, readiness_nonce=nonce))
    if failure != "missing_reply":
        driver._transcript_record({"sessionId": driver._identity.root_id, "type": "assistant", "promptId": "challenge",
            "message": {"id": "observed-message", "content": [{"type": "text", "text": "different" if failure == "wrong_reply" else nonce}]}})
    if failure == "StopFailure":
        hook(seat, "StopFailure", prompt_id="challenge")
    elif failure == "failed_terminal":
        driver._terminal("challenge", "failed", "claude:owned-transcript-turn_duration")
    elif failure == "wrong_terminal_source":
        driver._terminal("challenge", "completed", "unattributed-terminal")
    else:
        driver._transcript_record({"sessionId": driver._identity.root_id, "type": "system",
            "promptId": "challenge", "subtype": "turn_duration"})
    assert not driver._ready and not [event for event in events if event.kind == "runtime.ready"]
    assert driver.submit(request(), deadline=DEADLINE()).state == "not_written"
    assert driver.cleanup(deadline=DEADLINE()).outcome in {"complete", "pending"}


@pytest.mark.parametrize("invalid", [None, [None], [{"unexpected": "unparseable"}]])
@pytest.mark.parametrize("native_tool", ["TaskStop", "CronDelete"])
def test_partial_inventory_cannot_complete_native_stop_or_delete(seat, invalid, native_tool):
    driver, context, events = seat
    make_ready(seat)
    driver._context = replace(context, capability_evidence={"stop_work": "compatible", "automation": "compatible"})
    if native_tool == "TaskStop":
        hook(seat, "PostToolUse", tool_name="Bash", tool_use_id="launch", tool_input={},
            tool_response={"backgroundTaskId": "owned"})
        action, args, response = "stop_work", {"task_id": "owned"}, {"task_id": "owned", "task_type": "local_bash"}
    else:
        hook(seat, "PostToolUse", tool_name="CronCreate", tool_use_id="launch",
            tool_input={"durable": False}, tool_response={"id": "owned", "durable": False, "recurring": True})
        action, args, response = "stop_automation", {"id": "owned"}, {"id": "owned"}
    driver.control(NativeControl("control", 1, "sha", action,
        NativeReference(root_id=driver._identity.root_id, work_id="owned")), deadline=DEADLINE())
    hook(seat, "UserPromptSubmit", prompt_id="current", prompt=channel_prompt(seat, "none", control_id="control"))
    hook(seat, "PostToolUse", prompt_id="current", tool_name=native_tool, tool_use_id="stop-tool",
        tool_input=args, tool_response=response)
    hook(seat, "Stop", prompt_id="current", background_tasks=invalid, session_crons=[])
    assert driver.inventory(deadline=DEADLINE()).partial
    assert not [event for event in events if event.kind in {"work.terminal", "control.outcome"}]
    assert "control" in driver._pending_results and driver._work["owned"].state in {"running", "scheduled"}
    if native_tool == "CronDelete":
        assert "owned" in driver._definitions
    hook(seat, "Stop", prompt_id="current", background_tasks=[], session_crons=[])
    assert [event for event in events if event.kind == "control.outcome"][-1].data["outcome"] == "complete"
    assert "control" not in driver._pending_results
    if native_tool == "CronDelete":
        assert "owned" not in driver._definitions


def test_acks_are_not_processing_busy_queue_is_retained_and_no_blind_replay(seat):
    driver, _, events = seat
    make_ready(seat)
    first = request()
    assert driver.submit(first, deadline=DEADLINE()).state == "unknown"
    assert driver.submit(first, deadline=DEADLINE()).state == "unknown"
    assert len(driver._notifications) == 1
    driver.asset({"kind": "channel.sent", "sequence": 1}, peer=driver._channel_peer, deadline=DEADLINE())
    assert not [event for event in events if event.kind == "activity.processed"]
    assert driver.submit(request("request-2"), deadline=DEADLINE()).state == "not_written"
    hook(seat, "UserPromptSubmit", prompt_id="a", prompt=channel_prompt(seat))
    assert next(event for event in events if event.kind == "activity.processed").request_id == first.request_id
    driver._terminal("a", "completed", "test:exact-terminal")
    assert driver.submit(request("request-2"), deadline=DEADLINE()).state == "unknown"


def test_autonomous_turn_survives_old_terminal_and_excludes_private_transcript(seat):
    driver, _, events = seat
    driver._prompt("a", "earlier", "test:native")
    driver._prompt("b", "<task-notification><task-id>task</task-id><status>completed</status></task-notification>", "test:native")
    driver._terminal("a", "completed", "test:old-terminal")
    assert driver.inventory(deadline=DEADLINE()).primary.activity_id == "b"
    driver._transcript_record({"sessionId": driver._identity.root_id, "uuid": "message", "promptId": "b",
        "type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "private sentinel"},
        {"type": "text", "text": "Visible answer"}]}})
    driver._transcript_record({"sessionId": driver._identity.root_id, "uuid": "summary", "parentUuid": "message",
        "type": "system", "subtype": "stop_hook_summary", "preventedContinuation": False})
    assert driver._primary == "b"
    driver._transcript_record({"sessionId": driver._identity.root_id, "parentUuid": "summary", "type": "system", "subtype": "turn_duration"})
    assert driver._primary_state == "idle"
    assert "private sentinel" not in str(events)
    assert next(event for event in events if event.kind == "output.final").source == "native_completion"


def test_user_narration_and_stale_snapshot_cannot_prove_task_stop(seat):
    driver, context, events = seat
    make_ready(seat)
    driver._context = replace(context, capability_evidence={"stop_work": "compatible"})
    hook(seat, "PostToolUse", prompt_id="a", tool_name="Bash", tool_use_id="tool-a",
         tool_input={"command": "finite"}, tool_response={"backgroundTaskId": "task-1"})
    target = NativeReference(root_id=driver._identity.root_id, work_id="task-1")
    command = NativeControl("stop-1", 2, "sha", "stop_work", target)
    assert driver.control(command, deadline=DEADLINE()).state == "unknown"
    hook(seat, "UserPromptSubmit", prompt_id="control-prompt", prompt=channel_prompt(seat, "none", control_id="stop-1"))
    hook(seat, "PostToolUse", prompt_id="control-prompt", tool_name="TaskStop", tool_use_id="tool-stop",
         tool_input={"task_id": "task-1"}, tool_response={"task_id": "task-1", "task_type": "local_bash"})
    assert not [event for event in events if event.kind == "control.outcome"]
    hook(seat, "Stop", prompt_id="control-prompt", background_tasks=[], session_crons=[])
    outcome = [event for event in events if event.kind == "control.outcome"][-1]
    assert outcome.data == {"outcome": "complete", "native_outcome": "native_stopped", "os_verified": False}
    hook(seat, "SubagentStop", background_tasks=[{"id": "task-1", "type": "shell", "status": "running"}], session_crons=[])
    assert driver.inventory(deadline=DEADLINE()).partial
    assert driver._work["task-1"].state == "unknown_conflicting_snapshot"


def test_stale_empty_inventory_is_not_terminal_and_task_id_is_not_pid(seat):
    driver, _, events = seat
    hook(seat, "PostToolUse", tool_name="Bash", tool_use_id="task-tool", tool_input={"command": "finite"},
         tool_response={"backgroundTaskId": "12345"})
    hook(seat, "Stop", background_tasks=[], session_crons=[])
    work = driver.inventory(deadline=DEADLINE()).work[0]
    assert work.state == "absent_from_snapshot" and work.reference.os_process is None
    assert not [event for event in events if event.kind == "work.terminal"]


def test_wrong_session_configuration_change_and_unproven_controls_refuse(seat):
    driver, context, _ = seat
    make_ready(seat)
    target = NativeReference(root_id=driver._identity.root_id, activity_id="a")
    assert driver.control(NativeControl("stop", 1, "sha", "stop_reply", target, "a"), deadline=DEADLINE()).state == "unsupported"
    hook(seat, "ConfigChange")
    assert driver.submit(request(), deadline=DEADLINE()).state == "not_written"
    assert driver.inventory(deadline=DEADLINE()).capabilities["submission"] == "inconclusive"
    driver.asset({"kind": "hook", "event": {"session_id": "another-root", "cwd": str(context.worktree)}},
        peer=ProcessIdentity(999999999, 10), deadline=DEADLINE())
    assert driver._lost


def test_close_fences_queued_channel_work_and_retains_definition_obligations(seat, monkeypatch):
    driver, _, _ = seat
    make_ready(seat)
    driver.submit(request(), deadline=DEADLINE())
    hook(seat, "PostToolUse", tool_name="CronCreate", tool_use_id="cron-tool",
         tool_input={"durable": True}, tool_response={"id": "unsafe-cron", "durable": True})
    monkeypatch.setattr(runtime, "process_identity", lambda pid: None)
    outcome = driver.cleanup(deadline=DEADLINE())
    assert outcome.outcome == "pending" and outcome.unresolved_definitions == ("unsafe-cron",)
    assert not driver._notifications
    assert driver.submit(request("later"), deadline=DEADLINE()).state == "not_written"


@pytest.mark.parametrize("durable", [None, True, "false", 0])
def test_real_hook_rejects_missing_true_and_non_boolean_durable(tmp_path, durable):
    args = {} if durable is None else {"durable": durable}
    event = {"hook_event_name": "PreToolUse", "tool_name": "CronCreate", "tool_input": args}
    result = subprocess.run([sys.executable, str(ASSETS / "hook.py"), "--event", "PreToolUse"],
        input=json.dumps(event), capture_output=True, text=True, timeout=3, check=False,
        env={"SC_F89_SCHEDULING": "1"})
    assert result.returncode == 0
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_ingress_failure_blocks_pretool_and_does_not_print_raw_event(tmp_path):
    event = {"hook_event_name": "PreToolUse", "tool_name": "CronCreate",
        "tool_input": {"durable": False, "prompt": "private input sentinel"}}
    result = subprocess.run([sys.executable, str(ASSETS / "hook.py"), "--event", "PreToolUse"],
        input=json.dumps(event), capture_output=True, text=True, timeout=3, check=False,
        env={"SC_F89_SCHEDULING": "1", "SC_F89_CONTROLLER_ENDPOINT": str(tmp_path / "missing"), "SC_F89_GENERATION_ID": "gen"})
    assert result.returncode == 2
    assert "private input sentinel" not in result.stderr + result.stdout


def test_hook_private_wire_carries_owned_record_without_api_or_credentials(tmp_path):
    path = tmp_path / "controller.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    frames = []
    def receive():
        with server.accept()[0] as client:
            line = b""
            while not line.endswith(b"\n"):
                line += client.recv(8192)
            frames.append(json.loads(line))
            client.sendall(b'{"ok":true,"result":{"accepted":true}}\n')
    thread = threading.Thread(target=receive)
    thread.start()
    try:
        result = subprocess.run([sys.executable, str(ASSETS / "hook.py"), "--event", "Stop"],
            input='{"hook_event_name":"Stop","session_id":"root"}', capture_output=True, text=True, timeout=3, check=False,
            env={"SC_F89_CONTROLLER_ENDPOINT": str(path), "SC_F89_GENERATION_ID": "gen"})
        assert result.returncode == 0
        thread.join(timeout=1)
        assert frames[0]["contract"] == "f89-native-runtime-v1"
        assert frames[0]["op"] == "asset" and "env" not in frames[0]
        assert frames[0]["payload"]["event"]["session_id"] == "root"
    finally:
        server.close()


def test_old_turn_snapshot_cannot_complete_later_task_control(seat):
    driver, context, events = seat
    make_ready(seat)
    driver._context = replace(context, capability_evidence={"stop_work": "compatible"})
    hook(seat, "PostToolUse", tool_name="Bash", tool_use_id="launch", tool_input={},
         tool_response={"backgroundTaskId": "task"})
    driver.control(NativeControl("control", 1, "sha", "stop_work",
        NativeReference(root_id=driver._identity.root_id, work_id="task")), deadline=DEADLINE())
    hook(seat, "UserPromptSubmit", prompt_id="current", prompt=channel_prompt(seat, "none", control_id="control"))
    hook(seat, "PostToolUse", prompt_id="current", tool_name="TaskStop", tool_use_id="stop-tool",
         tool_input={"task_id": "task"}, tool_response={"task_id": "task", "task_type": "local_bash"})
    hook(seat, "Stop", prompt_id="old", background_tasks=[], session_crons=[])
    assert not [event for event in events if event.kind == "control.outcome"]
    hook(seat, "Stop", prompt_id="current", background_tasks=[], session_crons=[])
    assert [event for event in events if event.kind == "control.outcome"][-1].control_id == "control"


def test_control_transport_write_blocks_a_second_primary_dispatch_until_processed(seat):
    driver, context, _ = seat
    make_ready(seat)
    driver._context = replace(context, capability_evidence={"stop_work": "compatible"})
    hook(seat, "PostToolUse", tool_name="Bash", tool_use_id="launch", tool_input={},
         tool_response={"backgroundTaskId": "task"})
    driver.control(NativeControl("control", 1, "sha", "stop_work",
        NativeReference(root_id=driver._identity.root_id, work_id="task")), deadline=DEADLINE())
    driver.asset({"kind": "channel.sent", "sequence": 1}, peer=driver._channel_peer, deadline=DEADLINE())
    assert not driver._notifications
    assert driver.submit(request(), deadline=DEADLINE()).state == "not_written"


def test_owned_channel_close_rechecks_process_identity_and_leaves_other_process(seat):
    driver, _, _ = seat
    # Finite local test children only; no Claude process or engine service.
    children = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"]) for _ in range(3)]
    root, channel, other = children
    try:
        root_identity = runtime.process_identity(root.pid)
        channel_identity = runtime.process_identity(channel.pid)
        assert root_identity and channel_identity
        driver._process = root
        driver._identity = RuntimeIdentity(driver._identity.root_id, process=root_identity)
        driver._channel_peer = channel_identity
        result = driver.cleanup(deadline=time.monotonic() + 2)
        root.wait(timeout=1)
        channel.wait(timeout=1)
        assert other.poll() is None
        assert result.outcome == "complete"  # Native obligations only, not unit proof.
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=1)


def test_close_never_signals_reused_channel_pid(seat, monkeypatch):
    driver, _, _ = seat
    driver._channel_peer = ProcessIdentity(111111111, 20)
    monkeypatch.setattr(runtime, "process_identity", lambda pid: ProcessIdentity(pid, 21))
    calls = []
    monkeypatch.setattr(runtime.os, "kill", lambda *args: calls.append(args))
    driver.cleanup(deadline=DEADLINE())
    assert calls == []
