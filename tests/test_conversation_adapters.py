#!/usr/bin/env python3
"""Shared and native contract tests for Feature #24 conversation adapters."""
from __future__ import annotations

import io
import json
import signal
import sys
import tempfile
import unittest
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from conversation_adapters import (  # noqa: E402
    AdapterError,
    ClaudeAdapter,
    CodexAdapter,
    ConversationContext,
    OpenCodeAdapter,
    adapter_for,
)


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
            return OpenCodeAdapter(transport=native), native
        if harness == "claude":
            native = FakeClaudeRunner()
            return (
                ClaudeAdapter(
                    runner=native,
                    config_dir=self.claude_config,
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
        for harness in ("opencode", "claude", "codex"):
            with self.subTest(harness=harness):
                adapter, native = self.build(harness)
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
        events = list(adapter.stream(turn))
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

    def test_opencode_strips_the_selected_provider_prefix_from_model_id(
        self,
    ) -> None:
        adapter, native = self.build("opencode")
        context = ConversationContext(
            worktree=self.root,
            provider="openai",
            model="openai/gpt-5.6-terra-fast",
        )
        adapter.start(context, "hello")
        create = next(
            request
            for request in native.requests
            if request[:2] == ("POST", "/session")
        )
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
