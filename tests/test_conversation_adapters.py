#!/usr/bin/env python3
"""Shared and native contract tests for Feature #24 conversation adapters."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import replace
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
    JsonLineRpcProcess,
    KimiAdapter,
    NativeTurn,
    NormalizedEvent,
    OpenCodeAdapter,
    ReconcileResult,
    adapter_for,
)
from conversation_adapters import base as base_adapter
from conversation_adapters import codex as codex_adapter
from conversation_adapters import opencode as opencode_adapter
from conversation_adapters.base import SubprocessRunner

KIMI_FIXTURES = ROOT / "tests" / "fixtures" / "conversations" / "kimi"
KIMI_V2_STATE = KIMI_FIXTURES / "state-v2.json"
OPENCODE_FIXTURES = ROOT / "tests" / "fixtures" / "conversations" / "opencode"
OPENCODE_TYPED_TURN = OPENCODE_FIXTURES / "1.18.23-typed-turn.json"


def v2_context(
    root: Path,
    harness: str,
    *,
    provider: str | None = "openrouter",
    model: str = "test-model",
    effort: str = "high",
    env: Mapping[str, str] | None = None,
) -> ConversationContext:
    requested_model = (
        f"{provider}/{model}" if harness == "opencode" and provider else model
    )
    adapter_metadata = {}
    native_variant_id = None
    if harness == "opencode":
        native_variant_id = effort
        adapter_metadata = {
            "compatibility_manifest": "opencode-1.18.9-v1",
            "provider_family": "openai-ai-sdk",
            "variant_options": {"reasoningEffort": effort},
        }
    binding = {
        "contract_version": 2,
        "control_state": "controlled",
        "harness": harness,
        "requested_model": requested_model,
        "provider_model": model,
        "requested_effort": effort,
        "effective_effort": effort,
        "native_variant_id": native_variant_id,
        "transport": {
            "claude": "claude-effort-argument",
            "codex": "codex-reasoning-config",
            "kimi": "kimi-effort-environment",
            "opencode": "opencode-route-agent",
        }[harness],
        "catalogue_generation": "1" * 32,
        "evidence_digest": "2" * 64,
        "selector_binding": {"kind": "exact-test-route"},
        "adapter_metadata": adapter_metadata,
    }
    digest = hashlib.sha256(
        json.dumps(
            binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    return ConversationContext(
        worktree=root,
        provider=provider,
        model=requested_model,
        effort=effort,
        env=env or {},
        route_binding=binding,
        binding_digest=digest,
    )


def v3_opencode_context(
    root: Path,
    *,
    provider: str = "openai",
    model: str = "gpt-test",
    variant: str | None = "MAX.Future",
) -> ConversationContext:
    requested_model = f"{provider}/{model}"
    binding, digest = (
        opencode_adapter.route_transport.route_bindings.live_native_v3_binding(
            "opencode",
            requested_model,
            model,
            variant,
        )
    )
    return ConversationContext(
        worktree=root,
        provider=provider,
        model=requested_model,
        effort=variant,
        env={},
        route_binding=binding,
        binding_digest=digest,
    )


def kimi_step_event(
    event_type: str,
    *,
    finish_reason: str | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {"type": event_type}
    if finish_reason is not None:
        event["finishReason"] = finish_reason
    return {"type": "context.append_loop_event", "event": event}


class FakeOpenCode:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict, Any]] = []
        self.session_ref = "ses_exact"
        self.status = "idle"
        self.exists = True
        self.stream_calls: list[tuple[str, dict]] = []
        self.provider_state = {
            "connected": ["openai", "openrouter"],
            "all": [
                {
                    "id": "openai",
                    "models": {
                        "gpt-test": {
                            "variants": {
                                "high": {"reasoningEffort": "high"}
                            }
                        }
                    },
                },
                {
                    "id": "openrouter",
                    "models": {
                        "test-model": {
                            "variants": {
                                "high": {"reasoningEffort": "high"}
                            }
                        }
                    },
                },
            ],
        }

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
        if method == "GET" and path == "/provider":
            return self.provider_state
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
                    "type": "message.updated",
                    "properties": {
                        "sessionID": self.session_ref,
                        "info": {
                            "id": "msg-assistant",
                            "sessionID": self.session_ref,
                            "role": "assistant",
                        },
                    },
                },
                {
                    "type": "message.part.updated",
                    "properties": {
                        "sessionID": self.session_ref,
                        "part": {
                            "id": "reasoning-1",
                            "messageID": "msg-assistant",
                            "sessionID": self.session_ref,
                            "type": "reasoning",
                            "text": "",
                        },
                    },
                },
                {
                    "type": "message.part.delta",
                    "properties": {
                        "sessionID": self.session_ref,
                        "messageID": "msg-assistant",
                        "partID": "reasoning-1",
                        "field": "text",
                        "delta": "secret reasoning",
                    },
                },
                {
                    "type": "message.part.updated",
                    "properties": {
                        "sessionID": self.session_ref,
                        "part": {
                            "id": "text-1",
                            "messageID": "msg-assistant",
                            "sessionID": self.session_ref,
                            "type": "text",
                            "text": "",
                        },
                    },
                },
                {
                    "type": "message.part.delta",
                    "properties": {
                        "sessionID": self.session_ref,
                        "messageID": "msg-assistant",
                        "partID": "text-1",
                        "field": "text",
                        "delta": "hello",
                    },
                },
                {
                    "type": "message.part.updated",
                    "properties": {
                        "sessionID": self.session_ref,
                        "part": {
                            "id": "tool-1",
                            "messageID": "msg-assistant",
                            "sessionID": self.session_ref,
                            "type": "tool",
                            "callID": "call-1",
                            "tool": "bash",
                            "state": {"status": "running", "input": {}},
                        },
                    },
                },
                {
                    "type": "message.part.updated",
                    "properties": {
                        "sessionID": self.session_ref,
                        "part": {
                            "id": "tool-1",
                            "messageID": "msg-assistant",
                            "sessionID": self.session_ref,
                            "type": "tool",
                            "callID": "call-1",
                            "tool": "bash",
                            "state": {"status": "completed", "output": "ok"},
                        },
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


class SynchronizedTypedOpenCode(FakeOpenCode):
    """Replay 1.18.23 typed SSE while the synchronous prompt is blocked."""

    def __init__(self) -> None:
        super().__init__()
        fixture = json.loads(OPENCODE_TYPED_TURN.read_text())
        self.session_ref = fixture["session_ref"]
        self.events = fixture["events"]
        self.response = fixture["response"]
        self.message_started = threading.Event()
        self.progress_sent = threading.Event()
        self.release_message = threading.Event()

    def request(self, method, path, *, query=None, body=None):
        if method == "POST" and path.endswith("/message"):
            self.requests.append((method, path, dict(query or {}), body))
            self.message_started.set()
            if not self.release_message.wait(2):
                raise AssertionError("test did not release synchronous prompt")
            return self.response
        return super().request(method, path, query=query, body=body)

    def stream(self, path, *, query=None):
        self.stream_calls.append((path, dict(query or {})))

        def replay():
            yield self.events[0]
            if not self.message_started.wait(2):
                raise AssertionError("SSE consumer started no prompt worker")
            for event in self.events[1:-1]:
                yield event
            self.progress_sent.set()
            if not self.release_message.wait(2):
                raise AssertionError("test did not release terminal SSE")
            yield self.events[-1]

        return replay()


class CloseableBlockingStream:
    def __init__(self, session_ref: str) -> None:
        self.session_ref = session_ref
        self.closed = threading.Event()
        self.started = threading.Event()
        self._first = True

    def __iter__(self):
        return self

    def __next__(self):
        self.started.set()
        if self._first:
            self._first = False
            return {
                "type": "session.status",
                "properties": {
                    "sessionID": self.session_ref,
                    "status": {"type": "busy"},
                },
            }
        self.closed.wait(2)
        raise StopIteration

    def close(self) -> None:
        self.closed.set()


class FailingPromptOpenCode(FakeOpenCode):
    def __init__(self) -> None:
        super().__init__()
        self.event_stream = CloseableBlockingStream(self.session_ref)
        self.abort_count = 0

    def request(self, method, path, *, query=None, body=None):
        if method == "POST" and path.endswith("/message"):
            self.requests.append((method, path, dict(query or {}), body))
            raise AdapterError(
                "HARNESS_UNAVAILABLE",
                "POST /message failed: connection reset",
                retryable=True,
            )
        if method == "POST" and path.endswith("/abort"):
            self.requests.append((method, path, dict(query or {}), body))
            self.abort_count += 1
            return True
        return super().request(method, path, query=query, body=body)

    def stream(self, path, *, query=None):
        self.stream_calls.append((path, dict(query or {})))
        return self.event_stream


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
        state_schema: str = "legacy",
    ) -> tuple[Path, Path]:
        session = root / directory / session_ref
        wire = session / "agents" / "main" / "wire.jsonl"
        wire.parent.mkdir(parents=True, exist_ok=True)
        if state_schema == "legacy":
            state = {"workDir": str(worktree)}
        elif state_schema == "v2":
            state = json.loads(KIMI_V2_STATE.read_text())
            state["id"] = session_ref
            state["cwd"] = str(worktree)
            state["agents"]["main"]["homedir"] = str(wire.parent)
        else:
            raise AssertionError(f"unsupported Kimi state schema: {state_schema}")
        (session / "state.json").write_text(
            json.dumps(state),
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
                    state_schema=plan.get("state_schema", "legacy"),
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
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "threadId": self.session_ref,
                        "turnId": self.run_ref,
                        "tokenUsage": {
                            "last": {
                                "inputTokens": 12000,
                                "cachedInputTokens": 9000,
                                "outputTokens": 345,
                                "reasoningOutputTokens": 200,
                                "totalTokens": 12345,
                            },
                            "total": {"totalTokens": 98765},
                            "modelContextWindow": 258400,
                        },
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
        self.linked_vm = mock.patch(
            "run.linked_vm_configured", return_value=True
        )
        self.linked_vm.start()
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
        self.linked_vm.stop()
        self.temp.cleanup()

    def test_declared_compatibility_ranges_enforce_floor_and_flag_newer(self) -> None:
        cases = {
            "claude": ("2.1.219", "2.1.220", "2.1.223", "2.2.0"),
            "codex": ("0.144.999", "0.145.0", "0.147.0", "0.148.0"),
            "opencode": ("1.18.8", "1.18.9", "1.18.13", "1.19.0"),
            "kimi": ("0.29.999", "0.30.0", "0.33.0", "0.34.0"),
        }
        current_observed = {
            "claude": "2.1.223 (Claude Code)",
            "codex": "codex-cli 0.147.0",
            "opencode": "1.18.13",
            "kimi": "0.33.0",
        }
        for harness, (below, lower, current, upper) in cases.items():
            with self.subTest(harness=harness):
                adapter, _native = self.build(harness)
                lower_result = adapter._probe_result(lower)
                current_result = adapter._probe_result(current_observed[harness])
                self.assertEqual(lower_result.minimum_version, lower)
                self.assertEqual(current_result.version, current)
                self.assertEqual(
                    current_result.compatibility,
                    (
                        "verified"
                        if current
                        == adapter.manifest["conversation"]["verified_cli_version"]
                        else "supported"
                    ),
                )
                self.assertEqual(current_result.maximum_version_exclusive, upper)
                with self.assertRaisesRegex(
                    AdapterError,
                    rf"{harness} {re.escape(below)} is older than required {re.escape(lower)}",
                ):
                    adapter._probe_result(below)
                upper_result = adapter._probe_result(upper)
                self.assertEqual(upper_result.version, upper)
                self.assertEqual(upper_result.compatibility, "newer-unverified")
                self.assertEqual(upper_result.maximum_version_exclusive, upper)

    def test_same_core_non_tokens_are_not_verified(self) -> None:
        adapter, _native = self.build("codex")
        for observed in (
            "codex-cli 0.147.0dev", "codex-cli 0.147.0.1",
            "codex-cli 0.147.0_dev", "codex-cli 0.147.0~dev",
            "codex-cli 0.147.0/dev", "codex-cli 0.147.0:dev",
        ):
            with self.subTest(observed=observed):
                result = adapter._probe_result(observed)
                self.assertEqual(result.version, observed)
                self.assertEqual(result.compatibility, "non-semver")
                self.assertEqual(result.verified_version, "0.147.0")

    def test_custom_current_core_is_not_verified(self) -> None:
        adapter, _native = self.build("codex")
        for observed in (
            "codex-cli 0.147.0(dev)",
            "codex-cli 0.147.0 custom-build",
            "wrapper 0.147.0 (not-the-canary)",
        ):
            with self.subTest(observed=observed):
                result = adapter._probe_result(observed)
                self.assertEqual(result.version, observed)
                self.assertEqual(result.compatibility, "custom-unverified")
                self.assertEqual(result.verified_version, "0.147.0")

    def test_verified_probe_result_names_missing_manifest_keys(self) -> None:
        adapter, _native = self.build("claude")
        result = adapter._probe_result("2.1.223 (Claude Code)")
        self.assertEqual(result.compatibility, "verified")
        self.assertEqual(result.verified_version, "2.1.223")
        self.assertEqual(result.maximum_version_exclusive, "2.2.0")

        manifest = json.loads(
            (ROOT / ".super-coder" / "adapters" / "claude" / "adapter.json").read_text()
        )
        del manifest["conversation"]["maximum_cli_version_exclusive"]
        invalid = ClaudeAdapter(runner=FakeClaudeRunner(), manifest=manifest)
        with self.assertRaisesRegex(
            AdapterError,
            "HARNESS_MANIFEST_INVALID: harness compatibility manifest is missing "
            "maximum_cli_version_exclusive: claude",
        ):
            invalid._probe_result("2.1.222")

        missing_verified = json.loads(json.dumps(manifest))
        missing_verified["conversation"]["maximum_cli_version_exclusive"] = "2.2.0"
        del missing_verified["conversation"]["verified_cli_version"]
        invalid = ClaudeAdapter(
            runner=FakeClaudeRunner(), manifest=missing_verified
        )
        with self.assertRaisesRegex(
            AdapterError,
            "HARNESS_MANIFEST_INVALID: harness compatibility manifest is missing "
            "verified_cli_version: claude",
        ):
            invalid._probe_result("2.1.222")

        manifest_root = self.root / "adapters"
        (manifest_root / "claude").mkdir(parents=True)
        (manifest_root / "claude" / "adapter.json").write_text(
            json.dumps(manifest)
        )
        with (
            mock.patch.object(base_adapter, "ADAPTERS", manifest_root),
            self.assertRaisesRegex(
                AdapterError,
                "HARNESS_MANIFEST_INVALID: harness compatibility manifest is missing "
                "maximum_cli_version_exclusive: claude",
            ),
        ):
            base_adapter.load_manifest("claude")

    def test_successful_non_semver_probe_preserves_manifest_validation(self) -> None:
        adapter, _native = self.build("codex")
        with mock.patch.object(
            base_adapter.subprocess,
            "run",
            return_value=mock.Mock(
                returncode=0, stdout="codex dev-build\n", stderr=""
            ),
        ):
            result = adapter.probe()

        self.assertEqual(result.version, "codex dev-build")
        self.assertEqual(result.compatibility, "non-semver")
        self.assertEqual(result.verified_version, "0.147.0")

    def write_claude_session(
        self,
        adapter: ClaudeAdapter,
        session_ref: str,
        *,
        terminal: bool = True,
        stored_cwds: list[Path] | None = None,
    ) -> None:
        path = adapter._session_path(session_ref, self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        cwd_rows = stored_cwds or [self.root]
        rows = [
            {
                "type": "system",
                "subtype": "init",
                "session_id": session_ref,
                "cwd": str(cwd_rows[0]),
            }
        ]
        middle_cwds = cwd_rows[1:-1] if terminal else cwd_rows[1:]
        rows.extend(
            {
                "type": "assistant",
                "session_id": session_ref,
                "cwd": str(stored_cwd),
                "message": {"content": "working"},
            }
            for stored_cwd in middle_cwds
        )
        if terminal:
            rows.append(
                {
                    "type": "result",
                    "subtype": "success",
                    "session_id": session_ref,
                    "cwd": str(cwd_rows[-1]),
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

    def test_claude_success_result_prose_cannot_assert_interruption(self) -> None:
        adapter, _runner = self.build("claude")

        events = adapter._normalize(
            {
                "type": "result",
                "subtype": "success",
                "result": "I was mid-orientation when you first interrupted.",
            }
        )

        self.assertEqual(events[-1].type, "run.completed")
        self.assertEqual(events[-1].payload["status"], "completed")

    def test_claude_resume_preamble_result_is_not_terminal(self) -> None:
        """#1497: on ``--resume`` Claude first flushes a pending background-task
        notification as its own turn and emits a zero-turn ``result`` for it
        before the prompt's turn. Only the prompt's ``result`` ends the run."""
        adapter, _runner = self.build("claude")
        preamble = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "num_turns": 0,
            "stop_reason": None,
            "duration_api_ms": 0,
            "result": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
        self.assertEqual(adapter._normalize(preamble), [])
        failed = adapter._normalize(
            {**preamble, "is_error": True, "subtype": "error_during_execution"}
        )
        self.assertEqual(failed[-1].type, "run.failed")

        session_ref = "11111111-2222-4333-8444-555555555555"
        rows = [
            {
                "type": "system",
                "subtype": "task_notification",
                "session_id": session_ref,
                "status": "stopped",
            },
            {"type": "system", "subtype": "init", "session_id": session_ref},
            {**preamble, "session_id": session_ref},
            {"type": "system", "subtype": "init", "session_id": session_ref},
            {
                "type": "stream_event",
                "session_id": session_ref,
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "hello"},
                },
            },
            {
                "type": "result",
                "subtype": "success",
                "session_id": session_ref,
                "result": "hello",
                "num_turns": 1,
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 2},
            },
        ]
        process = FakeClaudeProcess(session_ref)
        process.stdout = io.StringIO(
            "".join(json.dumps(row) + "\n" for row in rows)
        )
        turn = NativeTurn(
            harness="claude",
            session_ref=session_ref,
            run_ref="claude-test",
            worktree=self.root,
            process_ref=str(process.pid),
            metadata={"resumed": True},
            opaque=process,
        )

        events = list(adapter.stream(turn))

        terminals = [
            event for event in events if event.type in base_adapter.TERMINAL_EVENTS
        ]
        self.assertEqual(len(terminals), 1)
        self.assertIs(terminals[0], events[-1])
        self.assertEqual(events[-1].type, "run.completed")
        self.assertEqual(events[-1].payload["result"], "hello")
        usage = [event for event in events if event.type == "usage"]
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0].payload["tokens"]["input_tokens"], 10)
        self.assertIn(
            "hello",
            [
                event.payload.get("text")
                for event in events
                if event.type == "assistant.delta"
            ],
        )

    def test_interrupted_events_require_structured_evidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "structured native or operator"):
            NormalizedEvent("run.interrupted", {"status": "interrupted"})

    def test_cancelled_reconciliation_requires_structured_evidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "structured native or operator"):
            ReconcileResult("cancelled", True, "cancelled without provenance")

        result = ReconcileResult(
            "cancelled",
            True,
            "operator interrupt was durably recorded",
            "operator",
        )
        self.assertEqual(result.interrupt_evidence, "operator")

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
                if harness == "opencode":
                    reasoning = [
                        event for event in events
                        if event.type == "assistant.delta"
                        and event.payload.get("segment") == "reasoning"
                    ]
                    answers = [
                        event for event in events
                        if event.type == "assistant.delta"
                        and event.payload.get("segment") != "reasoning"
                    ]
                    self.assertEqual(
                        [event.payload["text"] for event in reasoning],
                        ["secret reasoning"],
                    )
                    self.assertNotIn("secret reasoning", repr(answers))
                else:
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

    def test_v2_binding_drives_each_native_effort_transport_exactly_once(self):
        claude, claude_native = self.build("claude")
        claude_context = replace(
            v2_context(self.root, "claude"), model=None, effort=None
        )
        claude.start(claude_context, "work")
        claude_argv = claude_native.calls[-1][0]
        self.assertEqual(claude_argv.count("--effort"), 1)
        self.assertEqual(
            claude_argv[claude_argv.index("--effort") + 1], "high"
        )
        self.assertEqual(
            claude_argv[claude_argv.index("--model") + 1], "test-model"
        )

        codex, codex_native = self.build("codex")
        codex_context = replace(
            v2_context(self.root, "codex"), model=None, effort=None
        )
        codex.start(codex_context, "work")
        turn_params = next(
            params for method, params in codex_native.requests
            if method == "turn/start"
        )
        self.assertEqual(turn_params["effort"], "high")
        self.assertEqual(turn_params["model"], "test-model")

        kimi, kimi_native = self.build("kimi")
        kimi_context = replace(
            v2_context(
                self.root,
                "kimi",
                env={"KIMI_MODEL_THINKING_EFFORT": "ambient"},
            ),
            model=None,
            effort=None,
        )
        kimi.start(
            kimi_context,
            "work",
        )
        kimi_argv, _cwd, kimi_env = kimi_native.calls[-1]
        self.assertEqual(kimi_env["KIMI_MODEL_THINKING_EFFORT"], "high")
        self.assertEqual(kimi_argv.count("-m"), 1)
        self.assertEqual(kimi_argv[kimi_argv.index("-m") + 1], "test-model")

    def test_opencode_v2_binding_uses_full_agent_on_start_resume_and_prompt(self):
        native = FakeOpenCode()
        adapter = OpenCodeAdapter(
            transport=native,
            shell_runtime_dir=self.root / "runtime-shells",
        )
        context = replace(
            v2_context(
                self.root, "opencode", provider="openai", model="gpt-test"
            ),
            provider="stale-provider",
            model=None,
            effort=None,
        )
        expected_agent = f"sc-route-{context.binding_digest}"

        first = adapter.start(context, "first")
        list(adapter.stream(first))
        resumed = adapter.resume(first.session_ref, context, "second")
        list(adapter.stream(resumed))

        prompts = [
            request for request in native.requests
            if request[1].endswith("/message")
        ]
        self.assertEqual(len(prompts), 2)
        self.assertEqual(
            [request[3]["agent"] for request in prompts],
            [expected_agent, expected_agent],
        )
        self.assertEqual(
            [request[3]["model"] for request in prompts],
            [
                {"providerID": "openai", "modelID": "gpt-test"},
                {"providerID": "openai", "modelID": "gpt-test"},
            ],
        )
        configured = json.loads((self.root / "opencode.json").read_text())
        self.assertEqual(configured["agent"][expected_agent], {
            "mode": "primary",
            "model": "openai/gpt-test",
            "reasoningEffort": "high",
        })
        self.assertIn("shell", configured)

    def test_opencode_v3_sends_exact_model_and_variant_on_start_and_resume(self):
        native = FakeOpenCode()
        native.provider_state["all"][0]["models"]["gpt-test"]["variants"] = {
            "MAX.Future": {
                "reasoningEffort": "ignored-by-super-coder",
                "futureNativeKey": {"enabled": True},
            },
        }
        adapter = OpenCodeAdapter(
            transport=native,
            shell_runtime_dir=self.root / "runtime-shells",
        )
        context = v3_opencode_context(self.root)

        with mock.patch.object(
            opencode_adapter.opencode_config,
            "ensure_route_agent",
        ) as ensure_route_agent:
            first = adapter.start(context, "first")
            list(adapter.stream(first))
            later = adapter.resume(first.session_ref, context, "later")
            list(adapter.stream(later))
            resumed = adapter.resume(first.session_ref, context, "resumed")
            list(adapter.stream(resumed))

        ensure_route_agent.assert_not_called()

        prompts = [
            request for request in native.requests
            if request[1].endswith("/message")
        ]
        self.assertEqual([request[3] for request in prompts], [
            {
                "parts": [{"type": "text", "text": "first"}],
                "model": {"providerID": "openai", "modelID": "gpt-test"},
                "variant": "MAX.Future",
            },
            {
                "parts": [{"type": "text", "text": "later"}],
                "model": {"providerID": "openai", "modelID": "gpt-test"},
                "variant": "MAX.Future",
            },
            {
                "parts": [{"type": "text", "text": "resumed"}],
                "model": {"providerID": "openai", "modelID": "gpt-test"},
                "variant": "MAX.Future",
            },
        ])
        self.assertEqual(
            len([request for request in native.requests if request[:2] == (
                "GET", "/provider"
            )]),
            3,
        )
        self.assertEqual(
            len([request for request in native.requests if request[:2] == (
                "POST", "/session"
            )]),
            1,
        )
        configured = json.loads((self.root / "opencode.json").read_text())
        self.assertNotIn("agent", configured)

    def test_opencode_v3_harness_default_omits_variant_on_every_prompt(self):
        native = FakeOpenCode()
        adapter = OpenCodeAdapter(
            transport=native,
            shell_runtime_dir=self.root / "runtime-shells",
        )
        context = v3_opencode_context(self.root, variant=None)

        first = adapter.start(context, "first")
        list(adapter.stream(first))
        resumed = adapter.resume(first.session_ref, context, "second")
        list(adapter.stream(resumed))

        prompts = [
            request[3] for request in native.requests
            if request[1].endswith("/message")
        ]
        self.assertEqual(len(prompts), 2)
        self.assertEqual(
            [prompt["model"] for prompt in prompts],
            [
                {"providerID": "openai", "modelID": "gpt-test"},
                {"providerID": "openai", "modelID": "gpt-test"},
            ],
        )
        self.assertEqual(
            [("variant" in prompt, "agent" in prompt) for prompt in prompts],
            [(False, False), (False, False)],
        )

    def test_opencode_v3_disappeared_variant_refuses_resumed_prompt(self):
        native = FakeOpenCode()
        native.provider_state["all"][0]["models"]["gpt-test"]["variants"] = {
            "MAX.Future": {},
        }
        adapter = OpenCodeAdapter(
            transport=native,
            shell_runtime_dir=self.root / "runtime-shells",
        )
        context = v3_opencode_context(self.root)

        first = adapter.start(context, "first")
        list(adapter.stream(first))
        native.provider_state["all"][0]["models"]["gpt-test"]["variants"] = {}

        original_binding = dict(context.route_binding)
        with mock.patch.object(
            opencode_adapter.opencode_config,
            "ensure_route_agent",
        ) as ensure_route_agent, self.assertRaisesRegex(
            AdapterError, "native_route_unavailable"
        ):
            adapter.resume(first.session_ref, context, "must not dispatch")
        ensure_route_agent.assert_not_called()
        self.assertEqual(context.route_binding, original_binding)

        self.assertEqual(
            len([
                request for request in native.requests
                if request[1].endswith("/message")
            ]),
            1,
        )
        self.assertEqual(
            len([request for request in native.requests if request[:2] == (
                "GET", f"/session/{native.session_ref}"
            )]),
            0,
        )
        configured = json.loads((self.root / "opencode.json").read_text())
        self.assertNotIn("agent", configured)

    def test_opencode_v2_refuses_stale_route_agent_without_live_translation(self):
        native = FakeOpenCode()
        native.provider_state["all"][0]["models"]["gpt-test"]["variants"][
            "high"
        ] = {
            "reasoningEffort": "high",
            "futureNativeKey": {"enabled": True},
        }
        adapter = OpenCodeAdapter(
            transport=native,
            shell_runtime_dir=self.root / "runtime-shells",
        )
        context = v2_context(
            self.root, "opencode", provider="openai", model="gpt-test"
        )
        agent = f"sc-route-{context.binding_digest}"
        (self.root / "opencode.json").write_text(json.dumps({
            "agent": {agent: {
                "mode": "primary",
                "model": "openai/gpt-test",
                "reasoningEffort": "low",
            }}
        }))

        with self.assertRaisesRegex(AdapterError, "HARNESS_CONFIG_INVALID"):
            adapter.start(context, "must not dispatch")

        configured = json.loads((self.root / "opencode.json").read_text())
        self.assertEqual(configured["agent"][agent], {
            "mode": "primary",
            "model": "openai/gpt-test",
            "reasoningEffort": "low",
        })
        self.assertIn("shell", configured)
        self.assertEqual(
            [(method, path) for method, path, _query, _body in native.requests],
            [("GET", "/provider")],
        )

    def test_opencode_v3_concurrent_sessions_keep_exact_variants_without_agents(self):
        routes = (
            ("glm-5.2", "MaX.Future"),
            ("gemma4:31b", "Case/Sensitive"),
        )
        barrier = threading.Barrier(len(routes) + 1)
        results: list[tuple[FakeOpenCode, str, str]] = []
        errors: list[Exception] = []

        def run(model: str, variant: str) -> None:
            native = FakeOpenCode()
            native.session_ref = f"ses-{model}"
            native.provider_state["all"][0]["models"] = {
                model: {"variants": {variant: object()}},
            }
            adapter = OpenCodeAdapter(
                transport=native,
                shell_runtime_dir=self.root / "runtime-shells",
            )
            try:
                barrier.wait(timeout=2)
                turn = adapter.start(
                    v3_opencode_context(self.root, model=model, variant=variant),
                    f"prompt-{model}",
                )
                list(adapter.stream(turn))
                results.append((native, model, variant))
            except Exception as exc:  # noqa: BLE001 - captured for assertion
                errors.append(exc)

        workers = [threading.Thread(target=run, args=route) for route in routes]
        for worker in workers:
            worker.start()
        barrier.wait(timeout=2)
        for worker in workers:
            worker.join(timeout=2)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        for native, model, variant in results:
            prompts = [
                request[3] for request in native.requests
                if request[1].endswith("/message")
            ]
            self.assertEqual(prompts, [{
                "parts": [{"type": "text", "text": f"prompt-{model}"}],
                "model": {"providerID": "openai", "modelID": model},
                "variant": variant,
            }])
        configured = json.loads((self.root / "opencode.json").read_text())
        self.assertNotIn("agent", configured)

    def test_opencode_missing_live_option_refuses_before_session_or_prompt(self):
        native = FakeOpenCode()
        native.provider_state["all"][0]["models"]["gpt-test"]["variants"] = {
            "low": {"reasoningEffort": "low"},
        }
        adapter = OpenCodeAdapter(
            transport=native,
            shell_runtime_dir=self.root / "runtime-shells",
        )
        context = v2_context(
            self.root, "opencode", provider="openai", model="gpt-test"
        )

        with self.assertRaisesRegex(AdapterError, "native_route_unavailable"):
            adapter.start(context, "must not dispatch")

        self.assertEqual(
            [(method, path) for method, path, _query, _body in native.requests],
            [("GET", "/provider")],
        )

    def test_opencode_transport_rejection_is_one_terminal_dispatch(self):
        native = FakeOpenCode()
        native.provider_state["all"][0]["models"]["gpt-test"]["variants"] = {
            "MAX.Future": {},
        }
        native.stream = mock.Mock(return_value=iter([
            {
                "type": "session.status",
                "properties": {
                    "sessionID": native.session_ref,
                    "status": {"type": "busy"},
                },
            },
            {
                "type": "session.error",
                "properties": {
                    "sessionID": native.session_ref,
                    "error": {"name": "UnsupportedVariantError"},
                },
            },
        ]))
        adapter = OpenCodeAdapter(
            transport=native,
            shell_runtime_dir=self.root / "runtime-shells",
        )
        context = v3_opencode_context(self.root)

        turn = adapter.start(context, "dispatch once")
        events = list(adapter.stream(turn))
        prompts = [
            request for request in native.requests
            if request[1].endswith("/message")
        ]

        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0][3], {
            "parts": [{"type": "text", "text": "dispatch once"}],
            "model": {"providerID": "openai", "modelID": "gpt-test"},
            "variant": "MAX.Future",
        })
        self.assertEqual(events[-1].type, "run.failed")
        self.assertEqual(events[-1].payload["error"], "UnsupportedVariantError")
        self.assertEqual(
            len([request for request in native.requests if request[:2] == (
                "POST", "/session"
            )]),
            1,
        )

    def test_opencode_streams_typed_progress_before_sync_response(self):
        native = SynchronizedTypedOpenCode()
        adapter = OpenCodeAdapter(
            transport=native,
            shell_runtime_dir=self.root / "runtime-shells",
        )
        turn = adapter.start(self.context, "typed progress")
        events: list[NormalizedEvent] = []
        progress_observed = threading.Event()
        errors: list[BaseException] = []

        def consume() -> None:
            try:
                for event in adapter.stream(turn):
                    events.append(event)
                    if event.type == "tool.completed":
                        progress_observed.set()
            except BaseException as exc:  # noqa: BLE001 - asserted below
                errors.append(exc)

        worker = threading.Thread(target=consume)
        worker.start()
        self.assertTrue(native.message_started.wait(1))
        self.assertTrue(native.progress_sent.wait(1))
        self.assertTrue(
            progress_observed.wait(1),
            "typed progress remained buffered behind synchronous /message",
        )
        self.assertTrue(worker.is_alive())

        progress = list(events)
        self.assertEqual(
            [event.type for event in progress].count("run.started"),
            1,
        )
        self.assertEqual(
            [
                (event.payload["text"], event.payload.get("segment"))
                for event in progress
                if event.type == "assistant.delta"
            ],
            [
                ("think", "reasoning"),
                ("ing", "reasoning"),
                ("OK", "answer"),
            ],
        )
        self.assertNotIn("must not project", repr(progress))
        self.assertEqual(
            [event.type for event in progress].count("tool.started"),
            1,
        )
        self.assertEqual(
            [event.type for event in progress].count("tool.completed"),
            1,
        )
        usage = [event for event in progress if event.type == "usage"]
        self.assertEqual(len(usage), 1)
        self.assertEqual(
            usage[0].payload["tokens"],
            {"input": 8602, "output": 31, "reasoning": 0},
        )

        native.release_message.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(events[-1].type, "run.completed")
        self.assertEqual(
            [event.type for event in events].count("run.completed"),
            1,
        )
        self.assertEqual(
            len([
                request for request in native.requests
                if request[1].endswith("/message")
            ]),
            1,
        )

    def test_opencode_prompt_failure_closes_stream_and_joins_workers(self):
        native = FailingPromptOpenCode()
        adapter = OpenCodeAdapter(
            transport=native,
            shell_runtime_dir=self.root / "runtime-shells",
        )
        turn = adapter.start(self.context, "fail once")

        with self.assertRaises(AdapterError) as caught:
            list(adapter.stream(turn))

        self.assertEqual(caught.exception.code, "HARNESS_UNAVAILABLE")
        self.assertTrue(native.event_stream.started.is_set())
        self.assertTrue(native.event_stream.closed.is_set())
        self.assertEqual(native.abort_count, 1)
        self.assertEqual(
            [
                thread.name for thread in threading.enumerate()
                if turn.run_ref in thread.name
            ],
            [],
        )

    def test_opencode_repeated_interrupt_aborts_exact_session_once(self):
        adapter, native = self.build("opencode")
        turn = adapter.start(self.context, "interrupt once")

        self.assertTrue(adapter.interrupt(turn).acknowledged)
        self.assertTrue(adapter.interrupt(turn).acknowledged)

        aborts = [
            request for request in native.requests
            if request[:2] == (
                "POST", f"/session/{native.session_ref}/abort"
            )
        ]
        self.assertEqual(len(aborts), 1)

    def test_opencode_typed_tool_error_is_one_failed_lifecycle(self):
        adapter, _native = self.build("opencode")
        projection = opencode_adapter._OpenCodeProjection()
        raw = {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "ses_exact",
                "part": {
                    "id": "prt_failed",
                    "messageID": "msg_assistant",
                    "sessionID": "ses_exact",
                    "type": "tool",
                    "callID": "call_failed",
                    "tool": "bash",
                    "state": {"status": "error", "error": "denied"},
                },
            },
        }

        events = adapter._normalize(raw, projection)

        self.assertEqual([event.type for event in events], [
            "tool.started", "tool.completed"
        ])
        self.assertEqual(events[0].payload, {
            "tool_ref": "call_failed", "name": "bash"
        })
        self.assertEqual(events[1].payload, {
            "tool_ref": "call_failed", "status": "failed"
        })
        self.assertEqual(adapter._normalize(raw, projection), [])

    def test_opencode_rejects_irreconcilable_completed_part_text(self):
        adapter, _native = self.build("opencode")
        projection = opencode_adapter._OpenCodeProjection()
        adapter._normalize({
            "type": "message.updated",
            "properties": {
                "sessionID": "ses_exact",
                "info": {
                    "id": "msg_assistant",
                    "sessionID": "ses_exact",
                    "role": "assistant",
                },
            },
        }, projection)
        adapter._normalize({
            "type": "message.part.updated",
            "properties": {
                "sessionID": "ses_exact",
                "part": {
                    "id": "prt_answer",
                    "messageID": "msg_assistant",
                    "sessionID": "ses_exact",
                    "type": "text",
                    "text": "first",
                },
            },
        }, projection)

        with self.assertRaisesRegex(
            AdapterError, "irreconcilable text for part prt_answer"
        ):
            adapter._normalize({
                "type": "message.part.updated",
                "properties": {
                    "sessionID": "ses_exact",
                    "part": {
                        "id": "prt_answer",
                        "messageID": "msg_assistant",
                        "sessionID": "ses_exact",
                        "type": "text",
                        "text": "replacement",
                    },
                },
            }, projection)

    def test_opencode_exact_resources_filtering_and_unknown_recovery(
        self,
    ) -> None:
        adapter, native = self.build("opencode")
        with mock.patch.object(
            opencode_adapter,
            "command_version",
            return_value="1.18.9",
        ):
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

    def test_opencode_submitted_first_turn_without_activity_fails_precisely(
        self,
    ) -> None:
        class IdleOnlyOpenCode(FakeOpenCode):
            def stream(self, path, *, query=None):
                self.stream_calls.append((path, dict(query or {})))
                return iter(({
                    "type": "session.idle",
                    "properties": {"sessionID": self.session_ref},
                },))

        native = IdleOnlyOpenCode()
        adapter = OpenCodeAdapter(
            transport=native,
            shell_runtime_dir=self.root / "runtime-shells",
        )

        turn = adapter.start(self.context, "dispatch exactly once")
        with self.assertRaises(AdapterError) as caught:
            list(adapter.stream(turn))

        self.assertEqual(caught.exception.code, "HARNESS_SUBMISSION_UNOBSERVED")
        self.assertEqual(
            caught.exception.detail,
            "OpenCode accepted the synchronous prompt request but reported no "
            f"activity or terminal event for {native.session_ref}",
        )
        self.assertEqual(
            len([
                request for request in native.requests
                if request[1].endswith("/message")
            ]),
            1,
        )
        self.assertEqual(
            len([
                request for request in native.requests
                if request[:2] == ("POST", "/session")
            ]),
            1,
        )

    def test_opencode_ambiguous_prompt_connection_loss_stays_reconcilable(
        self,
    ) -> None:
        class ConnectionLostOpenCode(FakeOpenCode):
            def request(self, method, path, *, query=None, body=None):
                if method == "POST" and path.endswith("/message"):
                    self.requests.append((method, path, dict(query or {}), body))
                    try:
                        raise OSError("connection reset")
                    except OSError as cause:
                        raise AdapterError(
                            "HARNESS_UNAVAILABLE",
                            f"POST {path} failed: connection reset",
                            retryable=True,
                        ) from cause
                return super().request(
                    method,
                    path,
                    query=query,
                    body=body,
                )

        native = ConnectionLostOpenCode()
        adapter = OpenCodeAdapter(
            transport=native,
            shell_runtime_dir=self.root / "runtime-shells",
        )

        turn = adapter.start(self.context, "dispatch exactly once")
        with self.assertRaises(AdapterError) as caught:
            list(adapter.stream(turn))

        self.assertEqual(caught.exception.code, "HARNESS_UNAVAILABLE")
        self.assertEqual(
            caught.exception.detail,
            f"POST /session/{native.session_ref}/message failed: connection reset",
        )
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(
            len([
                request for request in native.requests
                if request[1].endswith("/message")
            ]),
            1,
        )

    def test_opencode_shared_server_is_not_the_turn_process(self) -> None:
        adapter, _native = self.build("opencode")
        shared_server = mock.Mock(pid=4242)
        shared_server.poll.return_value = None

        with mock.patch.object(
            opencode_adapter,
            "_SERVER_PROCESS",
            shared_server,
        ):
            turn = adapter.start(self.context, "hello")

        self.assertIsNone(turn.process_ref)
        self.assertEqual(shared_server.poll.call_count, 0)

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
        injection = [
            "--mcp-config",
            (
                '{"mcpServers":{"windows-mcp":{"type":"http",'
                '"url":"http://127.0.0.1:18000/mcp"}}}'
            ),
        ]
        self.assertEqual(argv[1:3], injection)
        self.assertEqual(argv.count("--mcp-config"), 1)
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

    def test_native_processes_receive_parent_owned_execution_prefix(self) -> None:
        context = replace(
            self.context,
            execution_prefix=("view-helper", "--"),
        )

        claude, claude_runner = self.build("claude")
        claude.start(context, "contained")
        self.assertEqual(claude_runner.calls[-1][0][:2], ["view-helper", "--"])

        kimi, kimi_runner = self.build("kimi")
        kimi.start(context, "contained")
        self.assertEqual(kimi_runner.calls[-1][0][:2], ["view-helper", "--"])

        with mock.patch.object(codex_adapter, "JsonLineRpcProcess") as rpc_process:
            codex = CodexAdapter()
            codex._transport(context)
        self.assertEqual(
            rpc_process.call_args.kwargs["argv"][:2],
            ["view-helper", "--"],
        )

        for surface in ("browser", "sprint"):
            with self.subTest(surface=surface):
                restricted = replace(
                    context,
                    env={**context.env, "SC_CONVERSATION_SURFACE": surface},
                )
                restricted_server = mock.Mock()
                restricted_server.poll.return_value = None
                restricted_log = mock.Mock()
                native = FakeOpenCode()
                with mock.patch.object(
                    opencode_adapter,
                    "ensure_server",
                ) as ensure_server, mock.patch.object(
                    opencode_adapter,
                    "start_context_server",
                    return_value=(
                        restricted_server,
                        restricted_log,
                        "http://127.0.0.1:12345",
                        "password",
                    ),
                ) as start_context_server, mock.patch.object(
                    opencode_adapter,
                    "UrlHttpTransport",
                    return_value=native,
                ):
                    opencode = OpenCodeAdapter()
                    ensure_server.assert_not_called()
                    opencode.start(restricted, "contained")
                    ensure_server.assert_not_called()
                    opencode.close()
                start_context_server.assert_called_once_with(restricted)
                restricted_server.terminate.assert_called_once_with()
                restricted_log.close.assert_called_once_with()

    def test_claude_resume_accepts_resolved_worktree_descendants(self) -> None:
        adapter, runner = self.build("claude")
        session_ref = "11111111-1111-4111-8111-111111111111"
        descendant = self.root / "project" / "src"
        descendant.mkdir(parents=True)
        self.write_claude_session(
            adapter,
            session_ref,
            stored_cwds=[self.root, descendant, self.root],
        )

        inspection = adapter.inspect(session_ref, self.context)
        resumed = adapter.resume(session_ref, self.context, "again")
        argv, cwd, _env = runner.calls[-1]

        self.assertTrue(inspection.exists)
        self.assertEqual(inspection.state, "idle")
        self.assertEqual(resumed.session_ref, session_ref)
        self.assertEqual(cwd, self.root)
        self.assertEqual(argv[argv.index("--resume") + 1], session_ref)
        self.assertNotIn("--session-id", argv)

    def test_claude_resume_rejects_resolved_paths_outside_worktree(self) -> None:
        adapter, runner = self.build("claude")
        with (
            tempfile.TemporaryDirectory(
                prefix=f"{self.root.name}-",
                dir=self.root.parent,
            ) as prefix_sibling_dir,
            tempfile.TemporaryDirectory(
                prefix="outside-",
                dir=self.root.parent,
            ) as unrelated_dir,
        ):
            prefix_sibling = Path(prefix_sibling_dir).resolve()
            unrelated = Path(unrelated_dir).resolve()
            external_target = unrelated / "target"
            external_target.mkdir()
            symlink_escape = self.root / "escape"
            symlink_escape.symlink_to(external_target, target_is_directory=True)
            rejected = {
                "parent": self.root.parent,
                "prefix-sharing sibling": prefix_sibling,
                "unrelated": unrelated,
                "symlink escape": symlink_escape,
            }

            for index, (label, stored_cwd) in enumerate(rejected.items(), 1):
                with self.subTest(path_kind=label):
                    session_ref = (
                        f"00000000-0000-4000-8000-{index:012d}"
                    )
                    self.write_claude_session(
                        adapter,
                        session_ref,
                        stored_cwds=[self.root, stored_cwd, self.root],
                    )
                    spawn_count = len(runner.calls)

                    with self.assertRaisesRegex(
                        AdapterError,
                        "HARNESS_WORKTREE_MISMATCH",
                    ):
                        adapter.resume(session_ref, self.context, "again")

                    self.assertEqual(len(runner.calls), spawn_count)

    def test_codex_app_server_receives_managed_mcp_before_subcommand(self) -> None:
        native = FakeCodexRpc()
        with mock.patch.object(
            codex_adapter,
            "JsonLineRpcProcess",
            return_value=native,
        ) as process:
            adapter = CodexAdapter()
            adapter.start(self.context, "hello")

        self.assertEqual(
            process.call_args.kwargs["argv"],
            [
                "codex",
                "-c",
                'mcp_servers.windows-mcp.url="http://127.0.0.1:18000/mcp"',
                "app-server",
                "--stdio",
            ],
        )
        self.assertEqual(process.call_args.kwargs["cwd"], self.root)

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

    def test_kimi_state_accepts_legacy_v2_and_equal_dual_fields(self) -> None:
        session = self.root / "kimi-state-valid"
        session.mkdir()
        state_path = session / "state.json"
        v2_state = json.loads(KIMI_V2_STATE.read_text())
        v2_state["cwd"] = str(self.root)
        valid_states = {
            "legacy": {"workDir": str(self.root)},
            "v2": v2_state,
            "equal dual": {
                "workDir": str(self.root),
                "cwd": str(self.root / "synthetic" / ".."),
            },
        }

        for label, state in valid_states.items():
            with self.subTest(label=label):
                state_path.write_text(json.dumps(state), encoding="utf-8")
                self.assertEqual(
                    KimiAdapter._state_worktree(session),
                    self.root,
                )

    def test_kimi_state_rejects_invalid_or_conflicting_fields(self) -> None:
        session = self.root / "kimi-state-invalid"
        session.mkdir()
        state_path = session / "state.json"
        invalid_states = {
            "missing": {},
            "legacy non-string": {"workDir": 7},
            "v2 empty": {"cwd": ""},
            "v2 relative": {"cwd": "relative/worktree"},
            "legacy invalid alongside v2": {
                "workDir": None,
                "cwd": str(self.root),
            },
            "v2 invalid alongside legacy": {
                "workDir": str(self.root),
                "cwd": [],
            },
            "conflicting": {
                "workDir": str(self.root),
                "cwd": str(self.root / "other"),
            },
            "unresolvable": {"cwd": "/\0"},
        }

        for label, state in invalid_states.items():
            with self.subTest(label=label):
                state_path.write_text(json.dumps(state), encoding="utf-8")
                with self.assertRaises(AdapterError) as raised:
                    KimiAdapter._state_worktree(session)
                self.assertEqual(
                    raised.exception.code,
                    "HARNESS_SESSION_INSPECTION_FAILED",
                )

    def test_kimi_state_rejects_malformed_or_unreadable_json(self) -> None:
        malformed = self.root / "kimi-state-malformed"
        malformed.mkdir()
        (malformed / "state.json").write_text("{", encoding="utf-8")
        with self.assertRaises(AdapterError) as malformed_error:
            KimiAdapter._state_worktree(malformed)
        self.assertEqual(
            malformed_error.exception.code,
            "HARNESS_SESSION_INSPECTION_FAILED",
        )

        unreadable = self.root / "kimi-state-unreadable"
        (unreadable / "state.json").mkdir(parents=True)
        with self.assertRaises(AdapterError) as unreadable_error:
            KimiAdapter._state_worktree(unreadable)
        self.assertEqual(
            unreadable_error.exception.code,
            "HARNESS_SESSION_INSPECTION_FAILED",
        )

    def test_kimi_v2_state_supports_full_turn_lifecycle_and_exact_slice(
        self,
    ) -> None:
        adapter, runner = self.build("kimi")
        runner.queue(
            state_schema="v2",
            prompt_time=6000,
            after_prompt=[
                kimi_step_event("step.end", finish_reason="end_turn"),
                {
                    "type": "usage.record",
                    "usageScope": "turn",
                    "usage": {"inputOther": 4, "output": 2},
                },
                {
                    "type": "turn.prompt",
                    "input": [{"type": "text", "text": "later"}],
                    "origin": {"kind": "user"},
                    "time": 6001,
                },
                {"type": "turn.cancel", "time": 6002},
            ],
        )

        turn = adapter.start(self.context, "v2 session")

        state = json.loads(
            (Path(turn.metadata["session_path"]) / "state.json").read_text()
        )
        self.assertEqual(
            set(state),
            {
                "id",
                "version",
                "cwd",
                "createdAt",
                "updatedAt",
                "archived",
                "agents",
                "custom",
            },
        )
        self.assertEqual(state["version"], 2)
        self.assertEqual(state["cwd"], str(self.root))
        self.assertNotIn("workDir", state)
        self.assertEqual(turn.worktree, self.root)
        self.assertEqual(
            Path(turn.metadata["wire_path"]),
            Path(turn.metadata["session_path"])
            / "agents"
            / "main"
            / "wire.jsonl",
        )

        turn.opaque.returncode = 0
        result = adapter.reconcile(turn, self.context)
        self.assertEqual(result.outcome, "succeeded")
        self.assertTrue(result.proven)
        self.assertNotEqual(
            result.outcome,
            "cancelled",
            "a later prompt's cancellation must not enter the exact run slice",
        )
        inspection = adapter.inspect(turn.session_ref, self.context)
        self.assertTrue(inspection.exists)
        self.assertEqual(inspection.state, "idle")
        self.assertEqual(inspection.metadata["last_prompt"], "later")

        runner.queue(state_schema="v2", prompt_time=6003)
        resumed = adapter.resume(turn.session_ref, self.context, "resume v2")
        self.assertEqual(resumed.session_ref, turn.session_ref)
        self.assertGreater(
            resumed.metadata["prompt_offset"],
            turn.metadata["prompt_offset"],
        )
        acknowledged = adapter.interrupt(resumed)
        self.assertTrue(acknowledged.acknowledged)
        self.assertEqual(resumed.opaque.signals, [signal.SIGINT])
        interrupted = adapter.reconcile(resumed, self.context)
        self.assertEqual(interrupted.outcome, "cancelled")
        self.assertTrue(interrupted.proven)
        self.assertEqual(
            adapter.inspect(resumed.session_ref, self.context).state,
            "idle",
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
                kimi_step_event("step.end", finish_reason="end_turn"),
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
                kimi_step_event("step.end", finish_reason="end_turn"),
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

    def test_kimi_tool_usage_does_not_complete_before_follow_up_answer(
        self,
    ) -> None:
        adapter, runner = self.build("kimi")
        runner.queue(
            stdout_lines=[
                {
                    "role": "assistant",
                    "content": "Running the repo-map queries now.",
                    "tool_calls": [
                        {
                            "id": "tool-map",
                            "function": {"name": "Bash"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "tool-map"},
                {"role": "assistant", "content": "Map loaded."},
            ],
            after_prompt=[
                kimi_step_event("step.end", finish_reason="tool_use"),
                {
                    "type": "usage.record",
                    "usageScope": "turn",
                    "usage": {"inputOther": 10, "output": 2},
                },
                kimi_step_event("step.begin"),
            ],
            block_after_stdout=True,
        )

        turn = adapter.start(self.context, "continue")
        events: list[Any] = []
        errors: list[BaseException] = []

        def consume() -> None:
            try:
                events.extend(adapter.stream(turn))
            except BaseException as exc:  # noqa: BLE001 - cross-thread test capture
                errors.append(exc)

        worker = threading.Thread(target=consume)
        worker.start()
        self.assertTrue(turn.opaque.stdout_blocked.wait(1.0))
        self.assertFalse(
            turn.opaque.stdout_released.wait(0.25),
            "tool-use usage must not terminate the persistent Kimi child",
        )
        self.assertFalse(turn.opaque.terminated)
        self.assertEqual(
            adapter.inspect(turn.session_ref, self.context).state,
            "unknown",
        )

        wire = Path(turn.metadata["wire_path"])
        with wire.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(
                kimi_step_event("step.end", finish_reason="end_turn")
            ) + "\n")
        self.assertFalse(
            turn.opaque.stdout_released.wait(0.25),
            "end_turn without its usage must not terminate Kimi",
        )
        with wire.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({
                "type": "usage.record",
                "usageScope": "turn",
                "usage": {"inputOther": 5, "output": 3},
            }) + "\n")

        worker.join(2.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(turn.opaque.terminated)
        self.assertEqual(
            [event.type for event in events],
            [
                "session.started",
                "run.started",
                "assistant.delta",
                "tool.started",
                "tool.completed",
                "assistant.delta",
                "usage",
                "run.completed",
            ],
        )
        self.assertEqual(
            next(event for event in events if event.type == "usage").payload,
            {
                "tokens": {
                    "input_tokens": 15,
                    "output_tokens": 5,
                }
            },
        )
        self.assertEqual(
            adapter.inspect(turn.session_ref, self.context).state,
            "idle",
        )

    def test_kimi_default_runner_owns_and_cleans_up_process_group(self) -> None:
        adapter = KimiAdapter()
        self.assertTrue(adapter.runner.start_new_session)
        self.assertTrue(ClaudeAdapter().runner.start_new_session)
        process = FakeKimiProcess([])
        process._sc_conversation_process_group = 4321

        with mock.patch(
            "conversation_adapters.base.os.killpg"
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

    def test_codex_app_server_owns_a_process_group(self) -> None:
        process = mock.Mock()
        process.pid = 4321
        process.stdin = io.StringIO()
        process.stdout = io.StringIO()
        process.stderr = io.StringIO()
        with (
            mock.patch(
                "conversation_adapters.codex.subprocess.Popen",
                return_value=process,
            ) as spawn,
            mock.patch.object(JsonLineRpcProcess, "request", return_value={}),
            mock.patch.object(JsonLineRpcProcess, "notify"),
        ):
            rpc = JsonLineRpcProcess(cwd=self.root, env={})

        self.assertTrue(spawn.call_args.kwargs["start_new_session"])
        self.assertEqual(
            rpc.process._sc_conversation_process_group,
            process.pid,
        )

    def test_codex_initialize_timeout_cleans_up_process_group(self) -> None:
        process = mock.Mock()
        process.pid = 4321
        process.stdin = io.StringIO()
        process.stdout = io.StringIO()
        process.stderr = io.StringIO()
        process.poll.return_value = None
        timeout = AdapterError(
            "HARNESS_TIMEOUT",
            "Codex request timed out: initialize",
            retryable=True,
        )
        with (
            mock.patch(
                "conversation_adapters.codex.subprocess.Popen",
                return_value=process,
            ),
            mock.patch.object(
                JsonLineRpcProcess,
                "request",
                side_effect=timeout,
            ),
            mock.patch(
                "conversation_adapters.codex.cleanup_owned_process"
            ) as cleanup,
        ):
            with self.assertRaisesRegex(
                AdapterError,
                "Codex request timed out: initialize",
            ):
                JsonLineRpcProcess(cwd=self.root, env={})

        cleanup.assert_called_once_with(process, 5.0)

    def test_shared_runner_reaps_group_after_leader_exit(self) -> None:
        process = mock.Mock()
        process.pid = 4321
        process.stderr = io.StringIO()
        process.wait.return_value = 0
        cleaned = threading.Event()
        with (
            mock.patch(
                "conversation_adapters.base.subprocess.Popen",
                return_value=process,
            ) as spawn,
            mock.patch(
                "conversation_adapters.base.cleanup_owned_process",
                side_effect=lambda _process, _timeout: cleaned.set(),
            ) as cleanup,
        ):
            returned = SubprocessRunner().spawn(
                ["harness"],
                cwd=self.root,
                env={},
            )
            self.assertTrue(cleaned.wait(1))

        self.assertIs(returned, process)
        self.assertTrue(spawn.call_args.kwargs["start_new_session"])
        self.assertEqual(process._sc_conversation_process_group, process.pid)
        cleanup.assert_called_once_with(process, 1.0)

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
                kimi_step_event("step.end", finish_reason="end_turn"),
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

    def test_kimi_recovered_tool_step_is_not_terminal_completion(self) -> None:
        adapter, runner = self.build("kimi")
        session_ref = "session_abababab-abab-4bab-8bab-abababababab"
        _session, _wire = runner.write_session(
            runner.sessions_root,
            session_ref,
            self.root,
            "recover tool step",
            prompt_time=8050,
            after_prompt=[
                kimi_step_event("step.end", finish_reason="tool_use"),
                {
                    "type": "usage.record",
                    "usageScope": "turn",
                    "usage": {"inputOther": 4, "output": 2},
                },
                kimi_step_event("step.begin"),
            ],
            directory="wd_recovered_tool_step",
        )
        recovered = NativeTurn(
            "kimi",
            session_ref,
            "kimi-8050-0",
            self.root,
            metadata={"recovered": True},
        )

        result = adapter.reconcile(recovered, self.context)

        self.assertEqual(result.outcome, "unknown")
        self.assertFalse(result.proven)
        self.assertEqual(
            adapter.inspect(session_ref, self.context).state,
            "unknown",
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
                kimi_step_event("step.end", finish_reason="end_turn"),
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
        usage = next(event for event in events if event.type == "usage")
        self.assertEqual(
            usage.payload,
            {
                "tokens": {
                    "inputTokens": 12000,
                    "cachedInputTokens": 9000,
                    "outputTokens": 345,
                    "reasoningOutputTokens": 200,
                    "totalTokens": 12345,
                }
            },
        )
        self.assertNotIn("98765", repr(usage.payload))
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
