#!/usr/bin/env python3
"""Codex app-server conversation adapter and JSONL-RPC transport."""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Protocol

import route_transport

from .base import (
    TERMINAL_EVENTS,
    AdapterError,
    ConversationAdapter,
    ConversationContext,
    InterruptResult,
    NativeTurn,
    NormalizedEvent,
    ProbeResult,
    ReconcileResult,
    SessionInspection,
    cleanup_owned_process,
    command_version,
    ensure_exact_session,
    ensure_nonempty_message,
    load_manifest,
    managed_mcp_launch_args,
    merged_env,
    terminal_outcome,
)


class RpcTransport(Protocol):
    def request(self, method: str, params: Mapping[str, Any]) -> Any: ...

    def notify(self, method: str, params: Mapping[str, Any]) -> None: ...

    def notifications(self) -> Iterable[Mapping[str, Any]]: ...

    def close(self) -> None: ...


class JsonLineRpcProcess:
    """Thread-safe request/notification client for `codex app-server` stdio."""

    def __init__(
        self,
        *,
        argv: list[str] | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.timeout = timeout
        self.process = subprocess.Popen(
            argv or ["codex", "app-server", "--stdio"],
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        self.process.__dict__["_sc_conversation_process_group"] = self.process.pid
        self._write_lock = threading.Lock()
        self._pending: dict[int, queue.Queue[Any]] = {}
        self._pending_lock = threading.Lock()
        self._notifications: queue.Queue[Any] = queue.Queue()
        self._next_id = 1
        self.stderr: list[str] = []
        self._stderr_reader = threading.Thread(
            target=self._drain_stderr,
            name="codex-app-server-stderr",
            daemon=True,
        )
        self._stderr_reader.start()
        self._reader = threading.Thread(
            target=self._read_loop,
            name="codex-app-server-reader",
            daemon=True,
        )
        self._reader.start()
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "super-coder",
                    "title": "super-coder conversation broker",
                    "version": "1",
                },
                "capabilities": {"experimentalApi": False},
            },
        )
        self.notify("initialized", {})

    def _drain_stderr(self) -> None:
        stderr = self.process.stderr
        if stderr is None:
            return
        remaining = 16384
        for line in stderr:
            if remaining <= 0:
                continue
            chunk = line[:remaining]
            self.stderr.append(chunk)
            remaining -= len(chunk)

    def _write(self, message: Mapping[str, Any]) -> None:
        stdin = self.process.stdin
        if stdin is None:
            raise AdapterError(
                "HARNESS_UNAVAILABLE",
                "Codex app-server stdin is closed",
                retryable=True,
            )
        encoded = json.dumps(message, separators=(",", ":"))
        with self._write_lock:
            try:
                stdin.write(encoded + "\n")
                stdin.flush()
            except OSError as exc:
                raise AdapterError(
                    "HARNESS_UNAVAILABLE",
                    f"Codex app-server write failed: {exc}",
                    retryable=True,
                ) from exc

    def _read_loop(self) -> None:
        stdout = self.process.stdout
        if stdout is None:
            self._notifications.put(None)
            return
        for line in stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self._notifications.put(
                    AdapterError(
                        "HARNESS_PROTOCOL_ERROR",
                        "Codex app-server emitted invalid JSON",
                    )
                )
                continue
            if not isinstance(message, dict):
                continue
            request_id = message.get("id")
            if request_id is not None and "method" not in message:
                with self._pending_lock:
                    waiter = self._pending.get(request_id)
                if waiter:
                    waiter.put(message)
                continue
            self._notifications.put(message)
        error = AdapterError(
            "HARNESS_UNAVAILABLE",
            "Codex app-server stream closed",
            retryable=True,
        )
        with self._pending_lock:
            waiters = list(self._pending.values())
        for waiter in waiters:
            waiter.put(error)
        self._notifications.put(None)

    def request(self, method: str, params: Mapping[str, Any]) -> Any:
        with self._pending_lock:
            request_id = self._next_id
            self._next_id += 1
            waiter: queue.Queue[Any] = queue.Queue(maxsize=1)
            self._pending[request_id] = waiter
        try:
            self._write({"id": request_id, "method": method, "params": params})
            try:
                response = waiter.get(timeout=self.timeout)
            except queue.Empty as exc:
                raise AdapterError(
                    "HARNESS_TIMEOUT",
                    f"Codex request timed out: {method}",
                    retryable=True,
                ) from exc
            if isinstance(response, AdapterError):
                raise response
            if "error" in response:
                raise AdapterError(
                    "HARNESS_PROTOCOL_ERROR",
                    f"Codex {method} failed: {response['error']}",
                )
            return response.get("result")
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def notify(self, method: str, params: Mapping[str, Any]) -> None:
        self._write({"method": method, "params": params})

    def notifications(self) -> Iterator[Mapping[str, Any]]:
        while True:
            message = self._notifications.get()
            if message is None:
                return
            if isinstance(message, AdapterError):
                raise message
            yield message

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        cleanup_owned_process(self.process, 5.0)


class CodexAdapter(ConversationAdapter):
    harness = "codex"

    def __init__(
        self,
        *,
        rpc: RpcTransport | None = None,
        manifest: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(manifest or load_manifest(self.harness))
        self._rpc = rpc

    def _transport(self, context: ConversationContext) -> RpcTransport:
        if self._rpc is None:
            launch = self.manifest["launch"][0]
            self._rpc = JsonLineRpcProcess(
                argv=[
                    launch,
                    *managed_mcp_launch_args(self.manifest),
                    "app-server",
                    "--stdio",
                ],
                cwd=context.checked_worktree(),
                env=merged_env(self.manifest, context),
            )
        return self._rpc

    def close(self) -> None:
        if self._rpc is not None:
            self._rpc.close()
            self._rpc = None

    def probe(self) -> ProbeResult:
        launch = self.manifest["launch"][0]
        return self._probe_result(command_version([launch, "--version"]))

    @staticmethod
    def _thread_params(context: ConversationContext) -> dict[str, Any]:
        try:
            projection = route_transport.context_projection(context, "codex")
        except route_transport.route_bindings.RouteResolutionError as exc:
            raise AdapterError(
                getattr(exc, "code", "HARNESS_ROUTE_INVALID"), str(exc)
            ) from exc
        model = projection.model if projection is not None else context.model
        params: dict[str, Any] = {
            "cwd": str(context.checked_worktree()),
        }
        if model:
            params["model"] = model
        if context.provider:
            params["modelProvider"] = context.provider
        if context.permission_mode == "unrestricted":
            params.update(
                {
                    "approvalPolicy": "never",
                    "sandbox": "danger-full-access",
                }
            )
        return params

    @staticmethod
    def _turn_params(
        session_ref: str,
        context: ConversationContext,
        message: str,
    ) -> dict[str, Any]:
        try:
            projection = route_transport.context_projection(context, "codex")
        except route_transport.route_bindings.RouteResolutionError as exc:
            raise AdapterError(
                getattr(exc, "code", "HARNESS_ROUTE_INVALID"), str(exc)
            ) from exc
        model = projection.model if projection is not None else context.model
        effort = projection.effort if projection is not None else context.effort
        params: dict[str, Any] = {
            "threadId": session_ref,
            "input": [{"type": "text", "text": message}],
            "cwd": str(context.checked_worktree()),
            "clientUserMessageId": str(uuid.uuid4()),
        }
        if model:
            params["model"] = model
        if effort:
            params["effort"] = effort
        if context.permission_mode == "unrestricted":
            params["approvalPolicy"] = "never"
        return params

    def _start_turn(
        self,
        rpc: RpcTransport,
        session_ref: str,
        context: ConversationContext,
        message: str,
        *,
        resumed: bool,
    ) -> NativeTurn:
        started = rpc.request(
            "turn/start",
            self._turn_params(session_ref, context, message),
        )
        turn_data = started.get("turn") if isinstance(started, dict) else None
        run_ref = turn_data.get("id") if isinstance(turn_data, dict) else None
        if not isinstance(run_ref, str) or not run_ref:
            raise AdapterError(
                "HARNESS_PROTOCOL_ERROR",
                "Codex turn/start returned no turn id",
            )
        process = getattr(rpc, "process", None)
        pid = getattr(process, "pid", None)
        return NativeTurn(
            harness=self.harness,
            session_ref=session_ref,
            run_ref=run_ref,
            worktree=context.checked_worktree(),
            process_ref=(
                str(pid)
                if isinstance(pid, int) and pid > 0
                else f"app-server:{run_ref}"
            ),
            metadata={"resumed": resumed},
        )

    def start(self, context: ConversationContext, message: str) -> NativeTurn:
        message = ensure_nonempty_message(message)
        rpc = self._transport(context)
        started = rpc.request("thread/start", self._thread_params(context))
        thread = started.get("thread") if isinstance(started, dict) else None
        session_ref = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(session_ref, str) or not session_ref:
            raise AdapterError(
                "HARNESS_PROTOCOL_ERROR",
                "Codex thread/start returned no thread id",
            )
        return self._start_turn(
            rpc,
            session_ref,
            context,
            message,
            resumed=False,
        )

    def resume(
        self,
        session_ref: str,
        context: ConversationContext,
        message: str,
    ) -> NativeTurn:
        message = ensure_nonempty_message(message)
        rpc = self._transport(context)
        params = self._thread_params(context)
        params["threadId"] = session_ref
        try:
            resumed = rpc.request("thread/resume", params)
        except AdapterError as exc:
            if exc.code == "HARNESS_PROTOCOL_ERROR":
                raise AdapterError(
                    "HARNESS_SESSION_LOST",
                    f"Codex could not resume thread {session_ref}",
                ) from exc
            raise
        thread = resumed.get("thread") if isinstance(resumed, dict) else None
        actual = thread.get("id") if isinstance(thread, dict) else None
        ensure_exact_session(session_ref, actual)
        stored_cwd = thread.get("cwd") if isinstance(thread, dict) else None
        if stored_cwd is not None and Path(stored_cwd).resolve() != (
            context.checked_worktree()
        ):
            raise AdapterError(
                "HARNESS_WORKTREE_MISMATCH",
                "Codex thread belongs to a different worktree",
            )
        return self._start_turn(
            rpc,
            session_ref,
            context,
            message,
            resumed=True,
        )

    @staticmethod
    def _matches(turn: NativeTurn, params: Mapping[str, Any]) -> bool:
        thread_ref = params.get("threadId")
        turn_ref = params.get("turnId")
        nested_thread = params.get("thread")
        if isinstance(nested_thread, dict):
            nested_thread_ref = nested_thread.get("id")
            if (
                nested_thread_ref is not None
                and nested_thread_ref != turn.session_ref
            ):
                return False
        if thread_ref is not None and thread_ref != turn.session_ref:
            return False
        if turn_ref is not None and turn_ref != turn.run_ref:
            return False
        nested = params.get("turn")
        if isinstance(nested, dict):
            nested_ref = nested.get("id")
            if nested_ref is not None and nested_ref != turn.run_ref:
                return False
        return True

    @staticmethod
    def _item_kind(params: Mapping[str, Any]) -> tuple[str | None, str | None]:
        item = params.get("item")
        if not isinstance(item, dict):
            return None, None
        kind = item.get("type")
        ref = item.get("id")
        return (
            kind if isinstance(kind, str) else None,
            ref if isinstance(ref, str) else None,
        )

    def _normalize(
        self,
        raw: Mapping[str, Any],
    ) -> list[NormalizedEvent]:
        method = raw.get("method")
        params = raw.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            return []
        if method == "thread/started":
            return []
        if method == "turn/started":
            return [
                NormalizedEvent(
                    "run.started",
                    {"status": "running"},
                    method,
                )
            ]
        if method == "item/agentMessage/delta":
            delta = params.get("delta")
            if isinstance(delta, str):
                return [
                    NormalizedEvent(
                        "assistant.delta",
                        {"text": delta},
                        method,
                    )
                ]
            return []
        if method == "item/started":
            kind, ref = self._item_kind(params)
            if kind in {None, "agentMessage", "reasoning"}:
                return []
            return [
                NormalizedEvent(
                    "tool.started",
                    {"tool_ref": ref, "name": kind},
                    method,
                )
            ]
        if method == "item/completed":
            kind, ref = self._item_kind(params)
            if kind in {None, "agentMessage", "reasoning"}:
                return []
            item = params.get("item")
            status = item.get("status") if isinstance(item, dict) else None
            return [
                NormalizedEvent(
                    "tool.completed",
                    {
                        "tool_ref": ref,
                        "name": kind,
                        "status": status or "completed",
                    },
                    method,
                )
            ]
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
            "applyPatchApproval",
            "execCommandApproval",
        }:
            return [
                NormalizedEvent(
                    "permission.requested",
                    {
                        "request_ref": raw.get("id"),
                        "kind": method,
                    },
                    method,
                )
            ]
        if method in {
            "item/tool/requestUserInput",
            "mcpServer/elicitation/request",
        }:
            return [
                NormalizedEvent(
                    "input.requested",
                    {
                        "request_ref": raw.get("id"),
                        "kind": method,
                    },
                    method,
                )
            ]
        if method in {
            "thread/tokenUsage/updated",
            "turn/tokenUsage/updated",
        }:
            token_usage = params.get("tokenUsage")
            last = token_usage.get("last") if isinstance(token_usage, dict) else None
            source = last if isinstance(last, dict) else params
            usage = {
                key: value
                for key, value in source.items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            }
            return [
                NormalizedEvent("usage", {"tokens": usage}, method)
            ] if usage else []
        if method == "turn/completed":
            turn = params.get("turn")
            status = turn.get("status") if isinstance(turn, dict) else None
            event_type = {
                "completed": "run.completed",
                "interrupted": "run.interrupted",
                "failed": "run.failed",
            }.get(status, "run.failed")
            error = turn.get("error") if isinstance(turn, dict) else None
            return [
                NormalizedEvent(
                    event_type,
                    {"status": status or "failed", "error": error},
                    method,
                    "native" if event_type == "run.interrupted" else None,
                )
            ]
        return []

    def stream(self, turn: NativeTurn) -> Iterator[NormalizedEvent]:
        if self._rpc is None:
            raise AdapterError(
                "HARNESS_UNAVAILABLE",
                "Codex app-server is not connected",
                retryable=True,
            )
        yield NormalizedEvent(
            "session.started",
            {
                "session_ref": turn.session_ref,
                "resumed": bool(turn.metadata.get("resumed")),
            },
            "thread.start-or-resume",
        )
        for raw in self._rpc.notifications():
            params = raw.get("params")
            if isinstance(params, dict) and not self._matches(turn, params):
                continue
            for event in self._normalize(raw):
                session = event.payload.get("session_ref")
                if session:
                    ensure_exact_session(turn.session_ref, session)
                if event.type in TERMINAL_EVENTS:
                    turn.metadata["terminal"] = event.type
                    if event.interrupt_evidence:
                        turn.metadata["interrupt_evidence"] = (
                            event.interrupt_evidence
                        )
                yield event
                if event.type in TERMINAL_EVENTS:
                    return

    def interrupt(self, turn: NativeTurn) -> InterruptResult:
        if self._rpc is None:
            return InterruptResult(False, "Codex app-server is not connected")
        self._rpc.request(
            "turn/interrupt",
            {"threadId": turn.session_ref, "turnId": turn.run_ref},
        )
        return InterruptResult(True)

    def inspect(
        self,
        session_ref: str,
        context: ConversationContext,
    ) -> SessionInspection:
        rpc = self._transport(context)
        try:
            result = rpc.request(
                "thread/read",
                {"threadId": session_ref, "includeTurns": True},
            )
        except AdapterError as exc:
            if exc.code in {
                "HARNESS_PROTOCOL_ERROR",
                "HARNESS_SESSION_LOST",
            }:
                return SessionInspection(session_ref, False, "missing")
            raise
        thread = result.get("thread") if isinstance(result, dict) else None
        actual = thread.get("id") if isinstance(thread, dict) else None
        ensure_exact_session(session_ref, actual)
        worktree = context.checked_worktree()
        stored_cwd = thread.get("cwd") if isinstance(thread, dict) else None
        if stored_cwd is not None and Path(stored_cwd).resolve() != worktree:
            raise AdapterError(
                "HARNESS_WORKTREE_MISMATCH",
                "Codex thread belongs to a different worktree",
            )
        turns = thread.get("turns", []) if isinstance(thread, dict) else []
        state = "idle"
        if turns and isinstance(turns[-1], dict):
            state = str(turns[-1].get("status") or "unknown")
        return SessionInspection(
            session_ref,
            True,
            state,
            worktree,
            {"turns": turns},
        )

    def reconcile(
        self,
        turn: NativeTurn,
        context: ConversationContext,
    ) -> ReconcileResult:
        terminal = turn.metadata.get("terminal")
        if terminal in TERMINAL_EVENTS:
            outcome = terminal_outcome(terminal)
            return ReconcileResult(
                outcome,
                True,
                f"terminal {terminal} was observed from app-server",
                (
                    turn.metadata.get("interrupt_evidence")
                    if outcome == "cancelled"
                    else None
                ),
            )
        inspection = self.inspect(turn.session_ref, context)
        if not inspection.exists:
            return ReconcileResult(
                "unknown",
                False,
                "Codex thread is missing",
            )
        turns = inspection.metadata.get("turns")
        if isinstance(turns, list):
            for native_turn in turns:
                if not isinstance(native_turn, dict):
                    continue
                if native_turn.get("id") != turn.run_ref:
                    continue
                status = native_turn.get("status")
                if status == "inProgress":
                    return ReconcileResult(
                        "running",
                        True,
                        "Codex thread/read reports the exact turn in progress",
                    )
                outcome = {
                    "completed": "succeeded",
                    "failed": "failed",
                    "interrupted": "cancelled",
                }.get(status)
                if outcome:
                    return ReconcileResult(
                        outcome,
                        True,
                        f"Codex thread/read reports {status}",
                        "native" if outcome == "cancelled" else None,
                    )
        return ReconcileResult(
            "unknown",
            False,
            "Codex thread exists but the run outcome is not provable",
        )
