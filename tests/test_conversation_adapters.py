#!/usr/bin/env python3
"""Shared and native contract tests for Feature #24 conversation adapters."""
from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from conversation_adapters import (  # noqa: E402
    AdapterError,
    ClaudeAdapter,
    CodexAdapter,
    ConversationContext,
    KimiAdapter,
    NativeTurn,
    OpenCodeAdapter,
    adapter_for,
)

KIMI_FIXTURES = ROOT / "tests" / "fixtures" / "conversations" / "kimi"


class FakeOpenCode:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict, Any]] = []
        self.session_ref = "ses_exact"
        self.status = "idle"
        self.exists = True
        self.stream_calls: list[tuple[str, dict]] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        self.requests.append((method, path, dict(query or {}), body))
        if path == "/global/health":
            return {"healthy": True, "version": "1.18.9"}
        if method == "POST" and path == "/session":
            return {"id": self.session_ref, "title": "test"}
        if path.endswith("/message"):
            self.status = "busy"
            return {
                "info": {
                    "role": "assistant",
                    "sessionID": self.session_ref,
                },
                "parts": [{"type": "text", "text": "hello"}],
            }
        if path.endswith("/abort"):
            self.status = "idle"
            return True
        if path == "/session/status":
            return {self.session_ref: {"type": self.status}}
        if method == "GET" and path == f"/session/{self.session_ref}":
            if not self.exists:
                raise AdapterError("HARNESS_SESSION_LOST", "missing")
            return {"id": self.session_ref, "title": "test"}
        if method == "PATCH" and path == f"/session/{self.session_ref}":
            return {"id": self.session_ref, "title": "test"}
        raise AssertionError(f"unexpected OpenCode request: {method} {path}")

    def stream(
        self,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
    ) -> Iterable[Mapping[str, Any]]:
        self.stream_calls.append((path, dict(query or {})))
        return iter(
            [
                {
                    "type": "message.part.delta",
                    "properties": {
                        "sessionID": "ses_other",
                        "field": "text",
                        "delta": "wrong",
                    },
                },
                {
                    "type": "session.idle",
                    "properties": {"sessionID": self.session_ref},
                },
                {
                    "type": "session.status",
                    "properties": {
                        "sessionID": self.session_ref,
                        "status": {"type": "busy"},
                    },
                },
                {
                    "type": "message.part.delta",
                    "properties": {
                        "sessionID": self.session_ref,
                        "field": "reasoning",
                        "delta": "secret reasoning",
                    },
                },
                {
                    "type": "message.part.delta",
                    "properties": {
                        "sessionID": self.session_ref,
                        "field": "text",
                        "delta": "hello",
                    },
                },
                {
                    "type": "session.next.tool.called",
                    "properties": {
                        "sessionID": self.session_ref,
                        "id": "tool-1",
                        "tool": "bash",
                    },
                },
                {
                    "type": "session.next.tool.success",
                    "properties": {
                        "sessionID": self.session_ref,
                        "id": "tool-1",
                    },
                },
                {
                    "type": "permission.v2.asked",
                    "properties": {
                        "sessionID": self.session_ref,
                        "id": "per-1",
                        "action": "bash",
                        "resources": ["git status"],
                    },
                },
                {
                    "type": "session.idle",
                    "properties": {"sessionID": self.session_ref},
                },
            ]
        )


class FakeClaudeProcess:
    def __init__(self, session_ref: str) -> None:
        rows = [
            {
                "type": "system",
                "subtype": "init",
                "session_id": session_ref,
            },
            {
                "type": "stream_event",
                "session_id": session_ref,
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "thinking_delta", "thinking": "secret"},
                },
            },
            {
                "type": "stream_event",
                "session_id": session_ref,
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "hello"},
                },
            },
            {
                "type": "stream_event",
                "session_id": session_ref,
                "event": {
                    "type": "content_block_start",
                    "content_block": {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Bash",
                    },
                },
            },
            {
                "type": "user",
                "session_id": session_ref,
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "is_error": False,
                        }
                    ]
                },
            },
            {
                "type": "result",
                "subtype": "success",
                "session_id": session_ref,
                "result": "done",
                "usage": {"input_tokens": 10, "output_tokens": 2},
            },
        ]
        self.stdout = io.StringIO(
            "".join(json.dumps(row) + "\n" for row in rows)
        )
        self.stderr = io.StringIO()
        self.pid = 4321
        self.returncode: int | None = None
        self.signals: list[int] = []

    def poll(self) -> int | None:
        return self.returncode

    def wait(self) -> int:
        self.returncode = 0
        return 0

    def send_signal(self, value: int) -> None:
        self.signals.append(value)


class FakeClaudeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path, dict[str, str]]] = []
        self.processes: list[FakeClaudeProcess] = []

    def spawn(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
    ) -> FakeClaudeProcess:
        flag = "--resume" if "--resume" in argv else "--session-id"
        session_ref = argv[argv.index(flag) + 1]
        process = FakeClaudeProcess(session_ref)
        self.calls.append((list(argv), cwd, dict(env)))
        self.processes.append(process)
        return process


class FakeKimiProcess:
    def __init__(
        self,
        stdout_lines: list[Any],
        *,
        wait_code: int = 0,
        exit_before_identity: bool = False,
        stderr: str = "",
        cancel_wire: Path | None = None,
        block_after_stdout: bool = False,
    ) -> None:
        encoded = [
            line if isinstance(line, str) else json.dumps(line)
            for line in stdout_lines
        ]
        self.stdout_released = threading.Event()
        self.stdout_blocked = threading.Event()
        self.stdout = (
            BlockingKimiStdout(
                encoded,
                self.stdout_blocked,
                self.stdout_released,
            )
            if block_after_stdout
            else io.StringIO("".join(line + "\n" for line in encoded))
        )
        self.stderr = io.StringIO(stderr)
        self.pid = 9876
        self.returncode: int | None = (
            wait_code if exit_before_identity else None
        )
        self.wait_code = wait_code
        self.cancel_wire = cancel_wire
        self.signals: list[int] = []
        self.terminated = False
        self.killed = False
        self.waited = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.waited = True
        if self.returncode is None:
            self.returncode = self.wait_code
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -signal.SIGTERM
        self.stdout_released.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -signal.SIGKILL
        self.stdout_released.set()

    def send_signal(self, value: int) -> None:
        self.signals.append(value)
        if value == signal.SIGINT and self.cancel_wire is not None:
            with self.cancel_wire.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps({"type": "turn.cancel", "time": 9000}) + "\n"
                )
            self.returncode = -signal.SIGINT
            self.stdout_released.set()


class BlockingKimiStdout:
    def __init__(
        self,
        lines: list[str],
        blocked: threading.Event,
        released: threading.Event,
    ) -> None:
        self.lines = lines
        self.blocked = blocked
        self.released = released

    def __iter__(self) -> Iterator[str]:
        yield from (line + "\n" for line in self.lines)
        self.blocked.set()
        if not self.released.wait(2.0):
            raise AssertionError("Kimi stdout remained blocked")


class FakeKimiRunner:
    def __init__(
        self,
        sessions_root: Path | None,
        worktree: Path,
    ) -> None:
        self.sessions_root = sessions_root
        self.worktree = worktree
        self.calls: list[tuple[list[str], Path, dict[str, str]]] = []
        self.processes: list[FakeKimiProcess] = []
        self.plans: list[dict[str, Any]] = []
        self.serial = 0

    def queue(self, **plan: Any) -> None:
        self.plans.append(plan)

    def root_for(self, env: Mapping[str, str], cwd: Path) -> Path:
        if self.sessions_root is not None:
            return self.sessions_root
        configured = env.get("KIMI_CODE_HOME", "").strip()
        merged_home = Path(env.get("HOME", str(Path.home())))
        if configured == "~":
            data_root = merged_home
        elif configured.startswith("~/"):
            data_root = merged_home / configured[2:]
        elif configured:
            data_root = Path(configured)
            if not data_root.is_absolute():
                data_root = cwd / data_root
        else:
            data_root = merged_home / ".kimi-code"
        return data_root / "sessions"

    def session_ref(self) -> str:
        self.serial += 1
        return (
            "session_00000000-0000-4000-8000-"
            f"{self.serial:012d}"
        )

    def write_session(
        self,
        root: Path,
        session_ref: str,
        worktree: Path,
        message: str,
        *,
        prompt_time: int,
        malformed_prompt: bool = False,
        after_prompt: list[dict[str, Any]] | None = None,
        directory: str = "wd_test",
        append: bool = False,
    ) -> tuple[Path, Path]:
        session = root / directory / session_ref
        wire = session / "agents" / "main" / "wire.jsonl"
        wire.parent.mkdir(parents=True, exist_ok=True)
        (session / "state.json").write_text(
            json.dumps({"workDir": str(worktree)}),
            encoding="utf-8",
        )
        prompt = (
            {"type": "turn.prompt", "input": [{"type": "text", "text": message}]}
            if malformed_prompt
            else {
                "type": "turn.prompt",
                "input": [{"type": "text", "text": message}],
                "origin": {"kind": "user"},
                "time": prompt_time,
            }
        )
        mode = "a" if append else "w"
        with wire.open(mode, encoding="utf-8") as stream:
            for row in [prompt, *(after_prompt or [])]:
                stream.write(json.dumps(row) + "\n")
        return session, wire

    def spawn(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
    ) -> FakeKimiProcess:
        plan = self.plans.pop(0) if self.plans else {}
        root = self.root_for(env, cwd)
        message = argv[argv.index("-p") + 1]
        resume = "-S" in argv
        session_ref = (
            argv[argv.index("-S") + 1]
            if resume
            else plan.get("session_ref", self.session_ref())
        )
        prompt_time = plan.get("prompt_time", 1000 + len(self.calls))
        wire: Path | None = None
        if plan.get("write_identity", True):
            worktrees = plan.get("candidate_worktrees", [cwd])
            for index, stored_worktree in enumerate(worktrees):
                candidate_ref = (
                    session_ref if index == 0 else self.session_ref()
                )
                _session, candidate_wire = self.write_session(
                    root,
                    candidate_ref,
                    stored_worktree,
                    message,
                    prompt_time=prompt_time,
                    malformed_prompt=plan.get("malformed_prompt", False),
                    after_prompt=plan.get("after_prompt"),
                    directory=f"wd_test_{index}",
                    append=resume and index == 0,
                )
                if index == 0:
                    wire = candidate_wire
        hint_ref = plan.get("hint_ref", session_ref)
        stdout_lines = plan.get(
            "stdout_lines",
            [
                {
                    "role": "assistant",
                    "content": "hello",
                    "tool_calls": [
                        {
                            "id": "tool-default",
                            "function": {"name": "Shell"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "tool-default"},
                {
                    "role": "meta",
                    "type": "session.resume_hint",
                    "session_id": hint_ref,
                },
            ],
        )
        process = FakeKimiProcess(
            stdout_lines,
            wait_code=plan.get("wait_code", 0),
            exit_before_identity=plan.get("exit_before_identity", False),
            stderr=plan.get("stderr", ""),
            cancel_wire=wire,
            block_after_stdout=plan.get("block_after_stdout", False),
        )
        self.calls.append((list(argv), cwd, dict(env)))
        self.processes.append(process)
        return process


class FakeCodexRpc:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.notifications_sent: list[tuple[str, dict[str, Any]]] = []
        self.session_ref = "codex-thread-exact"
        self.run_ref = "codex-turn-1"
        self.turn_number = 0
        self.read_status = "completed"
        self.resume_ref_override: str | None = None
        self.closed = False

    def request(self, method: str, params: Mapping[str, Any]) -> Any:
        self.requests.append((method, dict(params)))
        if method == "thread/start":
            return {
                "thread": {
                    "id": self.session_ref,
                    "cwd": params["cwd"],
                }
            }
        if method == "thread/resume":
            return {
                "thread": {
                    "id": self.resume_ref_override or params["threadId"],
                    "cwd": params["cwd"],
                }
            }
        if method == "turn/start":
            self.turn_number += 1
            self.run_ref = f"codex-turn-{self.turn_number}"
            return {"turn": {"id": self.run_ref, "status": "inProgress"}}
        if method == "turn/interrupt":
            return {}
        if method == "thread/read":
            return {
                "thread": {
                    "id": params["threadId"],
                    "cwd": str(WORKTREE),
                    "turns": [
                        {"id": self.run_ref, "status": self.read_status}
                    ],
                }
            }
        raise AssertionError(f"unexpected Codex request: {method}")

    def notify(self, method: str, params: Mapping[str, Any]) -> None:
        self.notifications_sent.append((method, dict(params)))

    def notifications(self) -> Iterable[Mapping[str, Any]]:
        return iter(
            [
                {
                    "method": "item/agentMessage/delta",
                    "params": {
                        "threadId": "other-thread",
                        "turnId": self.run_ref,
                        "delta": "wrong",
                    },
                },
                {
                    "method": "thread/started",
                    "params": {"thread": {"id": self.session_ref}},
                },
                {
                    "method": "turn/started",
                    "params": {
                        "threadId": self.session_ref,
                        "turn": {
                            "id": self.run_ref,
                            "status": "inProgress",
                        },
                    },
                },
                {
                    "method": "item/agentMessage/delta",
                    "params": {
                        "threadId": self.session_ref,
                        "turnId": self.run_ref,
                        "delta": "hello",
                    },
                },
                {
                    "method": "item/started",
                    "params": {
                        "threadId": self.session_ref,
                        "turnId": self.run_ref,
                        "item": {"id": "tool-1", "type": "commandExecution"},
                    },
                },
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": self.session_ref,
                        "turnId": self.run_ref,
                        "item": {
                            "id": "tool-1",
                            "type": "commandExecution",
                            "status": "completed",
                        },
                    },
                },
                {
                    "id": 91,
                    "method": "item/permissions/requestApproval",
                    "params": {
                        "threadId": self.session_ref,
                        "turnId": self.run_ref,
                    },
                },
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": self.session_ref,
                        "turn": {
                            "id": self.run_ref,
                            "status": "completed",
                        },
                    },
                },
            ]
        )

    def close(self) -> None:
        self.closed = True


WORKTREE = Path("/")


class ConversationAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        global WORKTREE
        WORKTREE = self.root
        self.context = ConversationContext(
            worktree=self.root,
            provider="openrouter",
            model="test-model",
            effort="high",
        )
        self.claude_config = self.root / "claude-config"
        self.kimi_sessions = self.root / "kimi-sessions"
        self.kimi_runner_serial = 0

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_claude_session(
        self,
        adapter: ClaudeAdapter,
        session_ref: str,
        *,
        terminal: bool = True,
    ) -> None:
        path = adapter._session_path(session_ref, self.root)
        path.parent.mkdir(parents=True)
        rows = [
            {
                "type": "system",
                "subtype": "init",
                "session_id": session_ref,
                "cwd": str(self.root),
            }
        ]
        if terminal:
            rows.append(
                {
                    "type": "result",
                    "subtype": "success",
                    "session_id": session_ref,
                    "cwd": str(self.root),
                    "result": "done",
                }
            )
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def build(self, harness: str):
        if harness == "opencode":
            native = FakeOpenCode()
            return (
                OpenCodeAdapter(
                    transport=native,
                    shell_runtime_dir=self.root / "opencode-shells",
                ),
                native,
            )
        if harness == "claude":
            native = FakeClaudeRunner()
            return (
                ClaudeAdapter(
                    runner=native,
                    config_dir=self.claude_config,
                ),
                native,
            )
        if harness == "kimi":
            self.kimi_runner_serial += 1
            sessions_root = (
                self.kimi_sessions / f"runner-{self.kimi_runner_serial}"
            )
            native = FakeKimiRunner(sessions_root, self.root)
            return (
                KimiAdapter(
                    runner=native,
                    sessions_root=sessions_root,
                    identity_timeout=0.1,
                ),
                native,
            )
        native = FakeCodexRpc()
        return CodexAdapter(rpc=native), native

    def prepare_resume(self, harness: str, adapter, session_ref: str) -> None:
        if harness == "claude":
            self.write_claude_session(adapter, session_ref)

    def test_claude_session_path_matches_native_project_encoding(self) -> None:
        adapter = ClaudeAdapter(config_dir=self.claude_config)
        worktree = Path("/home/j3d1/Repos/dos_app/.sc-worktrees/pln1")
        self.assertEqual(
            adapter._session_path(
                "b6321ad5-9363-4529-980d-93a959000968",
                worktree,
            ),
            self.claude_config
            / "projects"
            / "-home-j3d1-Repos-dos-app--sc-worktrees-pln1"
            / "b6321ad5-9363-4529-980d-93a959000968.jsonl",
        )

    def test_identical_contract_start_stream_interrupt_resume_reconcile(
        self,
    ) -> None:
        for harness in ("opencode", "claude", "codex", "kimi"):
            with self.subTest(harness=harness):
                adapter, _native = self.build(harness)
                turn = adapter.start(self.context, "first")
                self.assertEqual(turn.harness, harness)
                self.assertEqual(turn.worktree, self.root)
                self.assertTrue(turn.session_ref)
                self.assertTrue(turn.run_ref)

                events = list(adapter.stream(turn))
                types = [event.type for event in events]
                self.assertIn("session.started", types)
                self.assertIn("run.started", types)
                self.assertIn("assistant.delta", types)
                self.assertIn("tool.started", types)
                self.assertIn("tool.completed", types)
                self.assertIn("run.completed", types)
                self.assertNotIn("secret reasoning", repr(events))
                self.assertEqual(
                    adapter.reconcile(turn, self.context).outcome,
                    "succeeded",
                )

                self.prepare_resume(harness, adapter, turn.session_ref)
                resumed = adapter.resume(
                    turn.session_ref,
                    self.context,
                    "second",
                )
                self.assertEqual(resumed.session_ref, turn.session_ref)
                self.assertNotEqual(resumed.run_ref, turn.run_ref)
                self.assertTrue(adapter.interrupt(resumed).acknowledged)

    def test_native_permission_translation_has_no_shared_sandbox_flag(
        self,
    ) -> None:
        opencode, opencode_native = self.build("opencode")
        opencode.start(self.context, "work")
        create = next(
            request
            for request in opencode_native.requests
            if request[:2] == ("POST", "/session")
        )
        self.assertNotIn("sandbox", create[3])
        self.assertEqual(
            create[3]["permission"],
            [{"permission": "*", "pattern": "*", "action": "allow"}],
        )

        claude, claude_native = self.build("claude")
        claude.start(self.context, "work")
        claude_argv = claude_native.calls[-1][0]
        self.assertIn("--dangerously-skip-permissions", claude_argv)
        self.assertNotIn("--sandbox", claude_argv)
        with self.assertRaisesRegex(
            AdapterError,
            "HARNESS_PERMISSION_RESPONSE_UNSUPPORTED",
        ):
            claude.start(
                ConversationContext(
                    worktree=self.root,
                    permission_mode="interactive",
                ),
                "work",
            )

        codex, codex_native = self.build("codex")
        codex.start(self.context, "work")
        thread_params = next(
            params
            for method, params in codex_native.requests
            if method == "thread/start"
        )
        self.assertEqual(thread_params["approvalPolicy"], "never")
        self.assertEqual(thread_params["sandbox"], "danger-full-access")
        self.assertNotIn("--sandbox", repr(codex_native.requests))

        kimi, kimi_native = self.build("kimi")
        kimi.start(self.context, "work")
        kimi_argv, _cwd, kimi_env = kimi_native.calls[-1]
        self.assertNotIn("--yolo", kimi_argv)
        self.assertNotIn("--auto", kimi_argv)
        self.assertEqual(kimi_env["KIMI_MODEL_THINKING_EFFORT"], "high")
        self.assertIn("-m", kimi_argv)
        with self.assertRaisesRegex(
            AdapterError,
            "HARNESS_PERMISSION_RESPONSE_UNSUPPORTED",
        ):
            kimi.start(
                ConversationContext(
                    worktree=self.root,
                    permission_mode="interactive",
                ),
                "work",
            )
        no_effort_root = self.kimi_sessions / "no-effort"
        no_effort_native = FakeKimiRunner(no_effort_root, self.root)
        no_effort = KimiAdapter(
            runner=no_effort_native,
            sessions_root=no_effort_root,
            identity_timeout=0.1,
        )
        no_effort.start(
            ConversationContext(
                worktree=self.root,
                env={"KIMI_MODEL_THINKING_EFFORT": "ambient"},
            ),
            "work",
        )
        self.assertNotIn(
            "KIMI_MODEL_THINKING_EFFORT",
            no_effort_native.calls[-1][2],
        )

    def test_opencode_exact_resources_filtering_and_unknown_recovery(
        self,
    ) -> None:
        adapter, native = self.build("opencode")
        result = adapter.probe()
        self.assertEqual(result.version, "1.18.9")
        turn = adapter.start(self.context, "hello")
        self.assertEqual(
            native.stream_calls,
            [("/event", {"directory": str(self.root)})],
            "the SSE subscription must open before prompt dispatch",
        )
        self.assertFalse(
            any(request[1].endswith("/message") for request in native.requests),
            "message dispatch must wait until NativeTurn can be persisted",
        )
        events = list(adapter.stream(turn))
        prompt = next(
            request
            for request in native.requests
            if request[1].endswith("/message")
        )
        self.assertEqual(prompt[2]["directory"], str(self.root))
        self.assertNotIn(
            "messageID",
            prompt[3],
            "OpenCode must generate its own ordered native message id",
        )
        self.assertEqual(
            prompt[3]["model"],
            {"providerID": "openrouter", "modelID": "test-model"},
        )
        self.assertNotIn("wrong", repr(events))
        self.assertIn("permission.requested", [event.type for event in events])
        self.assertEqual(
            [event.type for event in events].count("run.completed"),
            1,
            "the pre-dispatch idle event must not terminate the new turn",
        )

        fresh = adapter.resume(turn.session_ref, self.context, "again")
        self.assertFalse(
            any(request[0] == "PATCH" for request in native.requests),
            "resume must reuse persisted permissions, not duplicate them",
        )
        native.status = "busy"
        recovered = adapter.reconcile(fresh, self.context)
        self.assertEqual(recovered.outcome, "running")
        self.assertTrue(recovered.proven)
        native.status = "idle"
        recovered = adapter.reconcile(fresh, self.context)
        self.assertEqual(recovered.outcome, "unknown")
        self.assertFalse(recovered.proven)

    def test_opencode_shell_tools_use_the_conversation_launch_identity(
        self,
    ) -> None:
        native = FakeOpenCode()
        adapter = OpenCodeAdapter(
            transport=native,
            shell_runtime_dir=self.root / "runtime-shells",
        )
        config = self.root / "opencode.json"
        config.write_text('{"permission":{"*":"allow"}}\n')
        context = ConversationContext(
            worktree=self.root,
            provider="openai",
            model="gpt-test",
            env={
                "PATH": os.environ["PATH"],
                "SC_API_BASE": "http://127.0.0.1:9911",
                "SC_API_TOKEN": "reviewer-token",
                "SC_ROOT": "/target/fork",
                "SC_SHELL_FLAVOR": "reviewer",
                "UNRELATED_SECRET": "must-not-persist",
            },
        )

        adapter.start(context, "review")

        configured = json.loads(config.read_text())
        self.assertEqual(configured["permission"], {"*": "allow"})
        wrapper = Path(configured["shell"])
        self.assertEqual(wrapper.stat().st_mode & 0o777, 0o700)
        self.assertNotIn("reviewer-token", config.read_text())
        self.assertNotIn("UNRELATED_SECRET", wrapper.read_text())
        result = subprocess.run(
            [
                str(wrapper),
                "-lc",
                "printf '%s\\n%s\\n%s' \"$SC_API_BASE\" \"$SC_API_TOKEN\" \"$SC_SHELL_FLAVOR\"",
            ],
            env={
                **os.environ,
                "SC_API_BASE": "http://127.0.0.1:8837",
                "SC_API_TOKEN": "wrong-parent-token",
                "SC_SHELL_FLAVOR": "dev",
            },
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(
            result.stdout.splitlines(),
            [
                "http://127.0.0.1:9911",
                "reviewer-token",
                "reviewer",
            ],
        )

    def test_opencode_strips_the_selected_provider_prefix_from_model_id(
        self,
    ) -> None:
        adapter, native = self.build("opencode")
        context = ConversationContext(
            worktree=self.root,
            provider="openai",
            model="openai/gpt-5.6-terra-fast",
        )
        turn = adapter.start(context, "hello")
        create = next(
            request
            for request in native.requests
            if request[:2] == ("POST", "/session")
        )
        list(adapter.stream(turn))
        prompt = next(
            request
            for request in native.requests
            if request[1].endswith("/message")
        )
        self.assertEqual(
            create[3]["model"],
            {"providerID": "openai", "id": "gpt-5.6-terra-fast"},
        )
        self.assertEqual(
            prompt[3]["model"],
            {"providerID": "openai", "modelID": "gpt-5.6-terra-fast"},
        )

    def test_opencode_default_transport_uses_decided_turn_ceiling(
        self,
    ) -> None:
        with mock.patch(
            "conversation_adapters.opencode.ensure_server",
            return_value="test-password",
        ):
            adapter = OpenCodeAdapter(endpoint="http://127.0.0.1:1")
        self.assertEqual(adapter.transport.timeout, 5400.0)

    def test_opencode_stop_before_dispatch_never_sends_the_prompt(
        self,
    ) -> None:
        adapter, native = self.build("opencode")
        turn = adapter.start(self.context, "must not dispatch")
        self.assertTrue(adapter.interrupt(turn).acknowledged)

        events = list(adapter.stream(turn))

        self.assertEqual(events[-1].type, "run.interrupted")
        self.assertFalse(
            any(request[1].endswith("/message") for request in native.requests)
        )

    def test_claude_uses_exact_start_and_resume_flags_and_sigint(
        self,
    ) -> None:
        adapter, runner = self.build("claude")
        turn = adapter.start(self.context, "hello")
        argv = runner.calls[-1][0]
        self.assertIn("--session-id", argv)
        self.assertNotIn("--resume", argv)
        self.assertIn("--include-partial-messages", argv)
        self.write_claude_session(adapter, turn.session_ref)
        resumed = adapter.resume(turn.session_ref, self.context, "again")
        argv = runner.calls[-1][0]
        self.assertIn("--resume", argv)
        self.assertNotIn("--session-id", argv)
        self.assertTrue(adapter.interrupt(resumed).acknowledged)
        self.assertEqual(runner.processes[-1].signals, [signal.SIGINT])

    def test_kimi_discovers_native_identity_before_stream_and_resumes_exactly(
        self,
    ) -> None:
        adapter, runner = self.build("kimi")
        captured_ref = "session_11111111-1111-4111-8111-111111111111"
        runner.queue(
            session_ref=captured_ref,
            stdout_lines=(
                KIMI_FIXTURES / "new-stream.jsonl"
            ).read_text().splitlines(),
        )
        first = adapter.start(self.context, "first")
        self.assertEqual(first.session_ref, captured_ref)
        self.assertRegex(
            first.session_ref,
            r"^session_[0-9a-f-]{36}$",
        )
        self.assertRegex(first.run_ref, r"^kimi-\d+-\d+$")
        self.assertIsNone(first.opaque.poll())
        wire = Path(first.metadata["wire_path"])
        with wire.open("rb") as stream:
            stream.seek(first.metadata["prompt_offset"])
            prompt = json.loads(stream.readline())
        self.assertEqual(prompt["time"], first.metadata["prompt_time"])
        self.assertEqual(prompt["input"][0]["text"], "first")
        list(adapter.stream(first))

        runner.queue(
            prompt_time=first.metadata["prompt_time"],
            stdout_lines=(
                KIMI_FIXTURES / "resume-stream.jsonl"
            ).read_text().splitlines(),
        )
        resumed = adapter.resume(first.session_ref, self.context, "second")
        argv = runner.calls[-1][0]
        self.assertEqual(
            argv[argv.index("-S") + 1],
            first.session_ref,
        )
        self.assertEqual(resumed.session_ref, first.session_ref)
        self.assertNotEqual(resumed.run_ref, first.run_ref)
        self.assertEqual(
            resumed.metadata["prompt_time"],
            first.metadata["prompt_time"],
            "the binary offset must distinguish same-millisecond prompts",
        )
        self.assertGreater(
            resumed.metadata["prompt_offset"],
            first.metadata["prompt_offset"],
        )
        self.assertEqual(
            list(adapter.stream(resumed))[-1].type,
            "run.completed",
        )

    def test_kimi_new_session_discovery_filters_and_reaps_failures(
        self,
    ) -> None:
        adapter, runner = self.build("kimi")
        preexisting = (
            "session_aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        )
        runner.write_session(
            runner.sessions_root,
            preexisting,
            self.root,
            "target",
            prompt_time=50,
            directory="wd_preexisting",
        )
        other = self.root / "other"
        other.mkdir()
        runner.queue(candidate_worktrees=[other, self.root])
        turn = adapter.start(self.context, "target")
        self.assertNotEqual(turn.session_ref, preexisting)
        self.assertEqual(
            json.loads(
                (Path(turn.metadata["session_path"]) / "state.json").read_text()
            )["workDir"],
            str(self.root),
        )

        ambiguous, ambiguous_runner = self.build("kimi")
        ambiguous_runner.queue(candidate_worktrees=[self.root, self.root])
        with self.assertRaisesRegex(
            AdapterError,
            "HARNESS_SESSION_DISCOVERY_FAILED",
        ):
            ambiguous.start(self.context, "ambiguous")
        self.assertTrue(ambiguous_runner.processes[-1].terminated)
        self.assertTrue(ambiguous_runner.processes[-1].waited)

        timeout_runner = FakeKimiRunner(self.kimi_sessions / "timeout", self.root)
        timeout_runner.queue(write_identity=False)
        timeout_adapter = KimiAdapter(
            runner=timeout_runner,
            sessions_root=self.kimi_sessions / "timeout",
            identity_timeout=0.03,
        )
        with self.assertRaisesRegex(
            AdapterError,
            "HARNESS_SESSION_DISCOVERY_FAILED",
        ):
            timeout_adapter.start(self.context, "missing")
        self.assertTrue(timeout_runner.processes[-1].terminated)
        self.assertTrue(timeout_runner.processes[-1].waited)

        malformed_runner = FakeKimiRunner(
            self.kimi_sessions / "malformed",
            self.root,
        )
        malformed_runner.queue(
            malformed_prompt=True,
            exit_before_identity=True,
            wait_code=1,
        )
        malformed_adapter = KimiAdapter(
            runner=malformed_runner,
            sessions_root=self.kimi_sessions / "malformed",
            identity_timeout=0.03,
        )
        with self.assertRaisesRegex(
            AdapterError,
            "malformed turn.prompt",
        ):
            malformed_adapter.start(self.context, "malformed")
        self.assertTrue(malformed_runner.processes[-1].waited)

    def test_kimi_store_root_commands_and_resume_validation(self) -> None:
        data_home = self.root / "kimi-home"
        runner = FakeKimiRunner(None, self.root)
        adapter = KimiAdapter(runner=runner, identity_timeout=0.1)
        context = ConversationContext(
            worktree=self.root,
            env={"KIMI_CODE_HOME": str(data_home)},
        )
        turn = adapter.start(context, "root")
        self.assertEqual(
            Path(turn.metadata["session_path"]).parents[1],
            data_home / "sessions",
        )
        self.assertNotIn("--yolo", runner.calls[-1][0])
        self.assertNotIn("--auto", runner.calls[-1][0])

        merged_home = self.root / "merged-home"
        home_runner = FakeKimiRunner(None, self.root)
        home_adapter = KimiAdapter(
            runner=home_runner,
            identity_timeout=0.1,
        )
        home_turn = home_adapter.start(
            ConversationContext(
                worktree=self.root,
                env={"HOME": str(merged_home), "KIMI_CODE_HOME": ""},
            ),
            "merged home",
        )
        self.assertEqual(
            Path(home_turn.metadata["session_path"]).parents[1],
            merged_home / ".kimi-code" / "sessions",
        )
        self.assertNotIn("KIMI_CODE_HOME", home_runner.calls[-1][2])

        before = len(runner.calls)
        with self.assertRaisesRegex(AdapterError, "HARNESS_SESSION_LOST"):
            adapter.resume("not-a-session", context, "again")
        with self.assertRaisesRegex(AdapterError, "HARNESS_SESSION_LOST"):
            adapter.resume(
                "session_ffffffff-ffff-4fff-8fff-ffffffffffff",
                context,
                "again",
            )
        self.assertEqual(
            len(runner.calls),
            before,
            "invalid and missing refs must fail before spawning",
        )

        wrong = self.root / "wrong-worktree"
        wrong.mkdir()
        with self.assertRaisesRegex(
            AdapterError,
            "HARNESS_WORKTREE_MISMATCH",
        ):
            adapter.resume(
                turn.session_ref,
                ConversationContext(
                    worktree=wrong,
                    env={"KIMI_CODE_HOME": str(data_home)},
                ),
                "again",
            )
        self.assertEqual(len(runner.calls), before)

    def test_kimi_stream_normalizes_live_capture_and_exact_slice_usage(
        self,
    ) -> None:
        adapter, runner = self.build("kimi")
        captured = (
            KIMI_FIXTURES / "new-stream.jsonl"
        ).read_text().splitlines()
        runner.queue(
            session_ref="session_11111111-1111-4111-8111-111111111111",
            stdout_lines=[
                "raw subprocess output",
                {"unrecognized": True},
                {
                    "role": "assistant",
                    "content": "chunk",
                    "tool_calls": [
                        {
                            "id": "tool-1",
                            "function": {"name": "Shell"},
                        },
                        {
                            "id": "tool-2",
                            "function": {"name": "Read"},
                        },
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "tool-1",
                    "content": "done",
                },
                *captured,
            ],
            after_prompt=[
                {
                    "type": "usage.record",
                    "usageScope": "turn",
                    "usage": {
                        "inputOther": 10,
                        "output": 2,
                        "inputCacheRead": 3,
                    },
                },
                {
                    "type": "usage.record",
                    "usageScope": "turn",
                    "usage": {
                        "inputOther": 5,
                        "inputCacheCreation": 7,
                    },
                },
                {
                    "type": "turn.prompt",
                    "input": [{"type": "text", "text": "later"}],
                    "time": 9999,
                },
                {
                    "type": "usage.record",
                    "usageScope": "turn",
                    "usage": {"output": 999},
                },
            ],
        )
        turn = adapter.start(self.context, "normalize")
        events = list(adapter.stream(turn))
        self.assertEqual(
            [event.type for event in events],
            [
                "session.started",
                "run.started",
                "assistant.delta",
                "tool.started",
                "tool.started",
                "tool.completed",
                "assistant.delta",
                "usage",
                "run.completed",
            ],
        )
        self.assertEqual(events[2].payload["text"], "chunk")
        self.assertEqual(events[3].payload["tool_ref"], "tool-1")
        self.assertEqual(events[4].payload["tool_ref"], "tool-2")
        usage = next(event for event in events if event.type == "usage")
        self.assertEqual(
            usage.payload["tokens"],
            {
                "input_tokens": 15,
                "output_tokens": 2,
                "cache_read_tokens": 3,
                "cache_write_tokens": 7,
            },
        )
        self.assertNotIn("999", repr(events))

        missing_usage, _missing_runner = self.build("kimi")
        completed = list(missing_usage.stream(
            missing_usage.start(self.context, "no usage")
        ))
        self.assertEqual(
            [event.type for event in completed if event.type == "usage"],
            [],
        )
        self.assertEqual(completed[-1].type, "run.completed")

        failed, failed_runner = self.build("kimi")
        failed_runner.queue(
            wait_code=3,
            stderr="native failure detail" + ("x" * 20000),
        )
        failed_events = list(failed.stream(
            failed.start(self.context, "fail")
        ))
        self.assertEqual(failed_events[-1].type, "run.failed")
        self.assertEqual(failed_events[-1].payload["exit_code"], 3)
        self.assertEqual(
            len(failed_events[-1].payload["error"]),
            16384,
        )
        self.assertTrue(
            failed_events[-1].payload["error"].startswith(
                "native failure detail"
            )
        )

    def test_kimi_durable_usage_completes_when_child_holds_stdout_open(
        self,
    ) -> None:
        adapter, runner = self.build("kimi")
        runner.queue(
            stdout_lines=[
                {"role": "assistant", "content": "server started"},
            ],
            after_prompt=[
                {
                    "type": "usage.record",
                    "usageScope": "turn",
                    "usage": {"inputOther": 10, "output": 2},
                },
            ],
            block_after_stdout=True,
        )

        turn = adapter.start(self.context, "start server")
        events = list(adapter.stream(turn))

        self.assertTrue(turn.opaque.stdout_blocked.is_set())
        self.assertTrue(turn.opaque.terminated)
        self.assertEqual(
            [event.type for event in events],
            [
                "session.started",
                "run.started",
                "assistant.delta",
                "usage",
                "run.completed",
            ],
        )
        self.assertEqual(events[-1].native_type, "usage.record")

    def test_kimi_default_runner_owns_and_cleans_up_process_group(self) -> None:
        adapter = KimiAdapter()
        self.assertTrue(adapter.runner.start_new_session)
        process = FakeKimiProcess([])
        process._sc_conversation_process_group = 4321

        with mock.patch(
            "conversation_adapters.kimi.os.killpg"
        ) as kill_process_group:
            adapter._cleanup_process(process, 0.1)

        self.assertEqual(
            kill_process_group.call_args_list,
            [
                mock.call(4321, signal.SIGTERM),
                mock.call(4321, 0),
                mock.call(4321, signal.SIGKILL),
            ],
        )

    def test_kimi_identity_mismatch_blocks_usage_reconciliation(self) -> None:
        adapter, runner = self.build("kimi")
        runner.queue(
            hint_ref="session_ffffffff-ffff-4fff-8fff-ffffffffffff",
            after_prompt=[
                {
                    "type": "usage.record",
                    "usageScope": "turn",
                    "usage": {"inputOther": 10, "output": 2},
                }
            ],
        )
        turn = adapter.start(self.context, "mismatch")
        with self.assertRaisesRegex(
            AdapterError,
            "HARNESS_SESSION_MISMATCH",
        ):
            list(adapter.stream(turn))
        result = adapter.reconcile(turn, self.context)
        self.assertEqual(result.outcome, "unknown")
        self.assertFalse(result.proven)
        self.assertTrue(turn.metadata["identity_mismatch"])

    def test_kimi_recovered_turn_rebuilds_exact_usage_slice(self) -> None:
        adapter, runner = self.build("kimi")
        session_ref = "session_dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        session, wire = runner.write_session(
            runner.sessions_root,
            session_ref,
            self.root,
            "recovered",
            prompt_time=8000,
            after_prompt=[
                {
                    "type": "usage.record",
                    "usageScope": "turn",
                    "usage": {"inputOther": 4, "output": 2},
                },
                {
                    "type": "turn.prompt",
                    "input": [{"type": "text", "text": "later"}],
                    "time": 8001,
                },
                {"type": "turn.cancel", "time": 8002},
            ],
            directory="wd_recovered_usage",
        )
        recovered = NativeTurn(
            "kimi",
            session_ref,
            "kimi-8000-0",
            self.root,
            metadata={"recovered": True},
        )

        result = adapter.reconcile(recovered, self.context)

        self.assertEqual(result.outcome, "succeeded")
        self.assertTrue(result.proven)
        self.assertEqual(recovered.metadata["session_path"], str(session))
        self.assertEqual(recovered.metadata["wire_path"], str(wire))
        self.assertEqual(recovered.metadata["prompt_time"], 8000)
        self.assertEqual(recovered.metadata["prompt_offset"], 0)
        self.assertNotEqual(
            result.outcome,
            "cancelled",
            "a later turn.cancel must not terminate the recovered run",
        )

    def test_kimi_recovered_turn_rebuilds_exact_cancel_slice(self) -> None:
        adapter, runner = self.build("kimi")
        session_ref = "session_eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
        session, wire = runner.write_session(
            runner.sessions_root,
            session_ref,
            self.root,
            "interrupted",
            prompt_time=8100,
            after_prompt=[
                {"type": "turn.cancel", "time": 8101},
                {
                    "type": "turn.prompt",
                    "input": [{"type": "text", "text": "later"}],
                    "time": 8102,
                },
                {
                    "type": "usage.record",
                    "usageScope": "turn",
                    "usage": {"inputOther": 99},
                },
            ],
            directory="wd_recovered_cancel",
        )
        recovered = NativeTurn(
            "kimi",
            session_ref,
            "kimi-8100-0",
            self.root,
            metadata={"recovered": True},
        )

        result = adapter.reconcile(recovered, self.context)

        self.assertEqual(result.outcome, "cancelled")
        self.assertTrue(result.proven)
        self.assertEqual(recovered.metadata["session_path"], str(session))
        self.assertEqual(recovered.metadata["wire_path"], str(wire))
        self.assertEqual(recovered.metadata["prompt_time"], 8100)
        self.assertEqual(recovered.metadata["prompt_offset"], 0)
        self.assertNotEqual(
            result.outcome,
            "succeeded",
            "a later usage record must not complete the recovered run",
        )

    def test_kimi_inspect_and_reconcile_use_only_main_exact_run_slice(
        self,
    ) -> None:
        adapter, runner = self.build("kimi")
        runner.queue(
            prompt_time=7000,
            after_prompt=[
                {
                    "type": "config.update",
                    "modelAlias": "kimi-code/k3",
                    "thinkingEffort": "high",
                },
                {
                    "type": "turn.prompt",
                    "input": [{"type": "text", "text": "later"}],
                    "time": 7001,
                },
                {
                    "type": "usage.record",
                    "usageScope": "turn",
                    "usage": {"inputOther": 99},
                },
            ],
        )
        turn = adapter.start(self.context, "current")
        session = Path(turn.metadata["session_path"])
        subagent = session / "agents" / "agent-0" / "wire.jsonl"
        subagent.parent.mkdir(parents=True)
        subagent.write_text(
            json.dumps({"type": "turn.cancel", "time": 7002}) + "\n",
            encoding="utf-8",
        )
        turn.opaque.returncode = 0
        result = adapter.reconcile(turn, self.context)
        self.assertEqual(result.outcome, "unknown")
        self.assertFalse(result.proven)

        inspection = adapter.inspect(turn.session_ref, self.context)
        self.assertEqual(inspection.state, "idle")
        self.assertEqual(inspection.metadata["model"], "kimi-code/k3")
        self.assertEqual(inspection.metadata["effort"], "high")
        self.assertEqual(inspection.metadata["last_prompt"], "later")

        captured = (
            KIMI_FIXTURES / "interrupted-main-wire.jsonl"
        ).read_bytes()
        captured_ref = "session_cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        captured_session = (
            runner.sessions_root / "wd_capture" / captured_ref
        )
        captured_wire = (
            captured_session / "agents" / "main" / "wire.jsonl"
        )
        captured_wire.parent.mkdir(parents=True)
        (captured_session / "state.json").write_text(
            json.dumps({"workDir": str(self.root)}),
            encoding="utf-8",
        )
        captured_wire.write_bytes(captured)
        marker = (
            b'{"type":"turn.prompt","input":[{"type":"text","text":'
            b'"Use the shell tool to run sleep 120'
        )
        offset = captured.index(marker)
        captured_turn = NativeTurn(
            "kimi",
            captured_ref,
            f"kimi-1785365164354-{offset}",
            self.root,
            metadata={
                "wire_path": str(captured_wire),
                "prompt_time": 1785365164354,
                "prompt_offset": offset,
            },
            opaque=FakeKimiProcess([], exit_before_identity=True),
        )
        interrupted = adapter.reconcile(captured_turn, self.context)
        self.assertEqual(interrupted.outcome, "cancelled")
        self.assertTrue(interrupted.proven)

    def test_kimi_sigint_and_run_cancel_normalize_to_interrupted(self) -> None:
        adapter, _runner = self.build("kimi")
        turn = adapter.start(self.context, "interrupt")
        self.assertTrue(adapter.interrupt(turn).acknowledged)
        self.assertEqual(turn.opaque.signals, [signal.SIGINT])
        events = list(adapter.stream(turn))
        self.assertEqual(events[-1].type, "run.interrupted")
        self.assertEqual(adapter.reconcile(turn, self.context).outcome, "cancelled")
        self.assertFalse(adapter.interrupt(turn).acknowledged)

    def test_codex_uses_exact_rpc_methods_and_read_reconciliation(
        self,
    ) -> None:
        adapter, rpc = self.build("codex")
        turn = adapter.start(self.context, "hello")
        self.assertEqual(
            [method for method, _params in rpc.requests[:2]],
            ["thread/start", "turn/start"],
        )
        events = list(adapter.stream(turn))
        self.assertIn(
            "permission.requested",
            [event.type for event in events],
        )
        resumed = adapter.resume(turn.session_ref, self.context, "again")
        self.assertIn("thread/resume", [method for method, _ in rpc.requests])
        rpc.read_status = "inProgress"
        result = adapter.reconcile(resumed, self.context)
        self.assertEqual(result.outcome, "running")
        self.assertTrue(result.proven)
        adapter.close()
        self.assertTrue(rpc.closed)

    def test_worktree_and_registry_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            AdapterError,
            "HARNESS_WORKTREE_MISSING",
        ):
            ConversationContext(
                worktree=self.root / "missing"
            ).checked_worktree()
        with self.assertRaisesRegex(
            AdapterError,
            "HARNESS_WORKTREE_MISMATCH",
        ):
            ConversationContext(worktree=Path("relative")).checked_worktree()
        with self.assertRaisesRegex(
            AdapterError,
            "HARNESS_CONVERSATION_UNSUPPORTED",
        ):
            adapter_for("vibe")

    def test_exact_resume_fails_closed_on_lost_or_mismatched_session(
        self,
    ) -> None:
        opencode, opencode_native = self.build("opencode")
        opencode_native.exists = False
        with self.assertRaisesRegex(AdapterError, "HARNESS_SESSION_LOST"):
            opencode.resume(
                opencode_native.session_ref,
                self.context,
                "again",
            )

        claude, _runner = self.build("claude")
        started = claude.start(self.context, "first")
        self.write_claude_session(claude, started.session_ref)
        resumed = claude.resume(started.session_ref, self.context, "again")
        resumed.opaque.stdout = io.StringIO(
            json.dumps(
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": str(
                        "00000000-0000-0000-0000-000000000000"
                    ),
                }
            )
            + "\n"
        )
        with self.assertRaisesRegex(
            AdapterError,
            "HARNESS_SESSION_MISMATCH",
        ):
            list(claude.stream(resumed))

        codex, rpc = self.build("codex")
        first = codex.start(self.context, "first")
        rpc.resume_ref_override = "wrong-thread"
        with self.assertRaisesRegex(
            AdapterError,
            "HARNESS_SESSION_MISMATCH",
        ):
            codex.resume(first.session_ref, self.context, "again")


if __name__ == "__main__":
    unittest.main()
