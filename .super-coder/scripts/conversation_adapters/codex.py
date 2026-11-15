#!/usr/bin/env python3
"""Codex app-server conversation adapter and JSONL-RPC transport."""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
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
    compaction_event,
    ensure_exact_session,
    ensure_nonempty_message,
    load_manifest,
    managed_mcp_launch_args,
    merged_env,
    terminal_outcome,
    token_count,
    usage_event,
)

# The Codex config key for the token count at which it compacts a thread.
# The engine states it twice, and both were accepted by codex-cli 0.167.1:
# as a ``-c`` override on the app-server launch (``config/read`` then reports
# it with origin ``sessionFlags``) and in the ``config`` object of
# ``thread/start`` / ``thread/resume`` (parsed as thread configuration: a
# non-integer value is refused with "failed to load configuration").
AUTO_COMPACT_KEY = "model_auto_compact_token_limit"
COMPACTION_ITEM = "contextCompaction"
# The manual request (0.167.1): ``thread/compact/start`` on a resumed thread
# answers ``{}`` at once, then the app-server runs the compaction as a turn of
# its own: ``turn/started`` with a new turn id, the ``contextCompaction``
# item, ``turn/completed``. The waits for that turn are bounded.
COMPACT_METHOD = "thread/compact/start"
COMPACT_START_SECONDS = 30.0
COMPACT_WAIT_SECONDS = 300.0
COMPACT_POLL_SECONDS = 0.25


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
        try:
            self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "super-coder",
                        "title": "Subfloor conversation broker",
                        "version": "1",
                    },
                    "capabilities": {"experimentalApi": False},
                },
            )
            self.notify("initialized", {})
        except BaseException:
            self.close()
            raise

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

    def next_notification(self, timeout: float) -> Mapping[str, Any] | None:
        """The next notification, or None when ``timeout`` passes without one."""
        try:
            message = self._notifications.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None
        if message is None:
            # Keep the end marker for any later reader of the stream.
            self._notifications.put(None)
            raise AdapterError(
                "HARNESS_UNAVAILABLE",
                "Codex app-server stream closed",
                retryable=True,
            )
        if isinstance(message, AdapterError):
            raise message
        return message

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        cleanup_owned_process(self.process, 5.0)


class CodexAdapter(ConversationAdapter):
    harness = "codex"
    manual_compaction = True
    # ``model_auto_compact_token_limit`` on the launch and the thread config.
    compact_window_native = True

    def __init__(
        self,
        *,
        rpc: RpcTransport | None = None,
        manifest: Mapping[str, Any] | None = None,
        compact_start_seconds: float = COMPACT_START_SECONDS,
        compact_wait_seconds: float = COMPACT_WAIT_SECONDS,
    ) -> None:
        super().__init__(manifest or load_manifest(self.harness))
        self._rpc = rpc
        self.compact_start_seconds = compact_start_seconds
        self.compact_wait_seconds = compact_wait_seconds
        self._notification_source: Iterator[Mapping[str, Any]] | None = None
        # Set by the caller that asked Codex to compact (the manual request);
        # the next compaction recorded is then ``manual``. The item itself
        # carries no trigger, so everything else is ``auto``.
        self.compaction_requested = False
        # (size, total, estimate) of the latest token-usage updates: ``size``
        # is the meter reading, ``total`` adds that request's output, and
        # ``estimate`` marks Codex's post-compaction figure (no input side).
        self._reading: tuple[int, int, bool] | None = None
        self._compaction: dict[str, Any] | None = None

    @staticmethod
    def _compact_launch_args(context: ConversationContext) -> list[str]:
        window = context.effective_compact_window()
        if window is None:
            return []
        return ["-c", f"{AUTO_COMPACT_KEY}={window}"]

    def _seed_reading(self, context: ConversationContext) -> None:
        """Start from the chat's last recorded size, when one is recorded."""
        size = token_count(context.context_tokens)
        self._reading = (size, size, False) if size else None
        self._compaction = None

    def _transport(self, context: ConversationContext) -> RpcTransport:
        if self._rpc is None:
            launch = self.manifest["launch"][0]
            self._rpc = JsonLineRpcProcess(
                argv=[
                    launch,
                    *managed_mcp_launch_args(self.manifest, context),
                    *self._compact_launch_args(context),
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
        window = context.effective_compact_window()
        if window is not None:
            params["config"] = {AUTO_COMPACT_KEY: window}
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
        # Model default is a reserved binding value, not a native effort name.
        if effort and effort != route_transport.route_bindings.DEFAULT_EFFORT:
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
        self._reading = self._compaction = None
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
        rpc = self._resume_thread(session_ref, context)
        return self._start_turn(
            rpc,
            session_ref,
            context,
            message,
            resumed=True,
        )

    def _resume_thread(
        self,
        session_ref: str,
        context: ConversationContext,
    ) -> RpcTransport:
        """Load the exact stored thread into this app-server."""
        rpc = self._transport(context)
        self._seed_reading(context)
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
        return rpc

    def compact(
        self,
        session_ref: str,
        context: ConversationContext,
    ) -> NativeTurn:
        """Resume the thread, then ask the app-server to compact it."""
        rpc = self._resume_thread(session_ref, context)
        # The item carries no trigger: this flag is what makes the next
        # compaction ``manual``.
        self.compaction_requested = True
        try:
            rpc.request(COMPACT_METHOD, {"threadId": session_ref})
        except AdapterError as exc:
            self.compaction_requested = False
            if exc.code == "HARNESS_PROTOCOL_ERROR":
                raise AdapterError("HARNESS_COMPACT_REFUSED", exc.detail) from exc
            raise
        process = getattr(rpc, "process", None)
        pid = getattr(process, "pid", None)
        # The compaction's turn id is not known until ``turn/started``.
        run_ref = f"compact-{uuid.uuid4()}"
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
            metadata={"resumed": True, "compaction": True},
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

    @staticmethod
    def _usage_reading(
        source: Mapping[str, Any],
    ) -> tuple[int, int, bool] | None:
        """(size, total, estimate) of one token-usage breakdown.

        ``inputTokens`` already counts the cached part, so it alone is the
        input side of the request. Right after a compaction Codex reports no
        input side at all and puts its estimate of the new context in
        ``totalTokens``; that figure is then the reading.
        """

        def read(camel: str, snake: str) -> int | None:
            value = token_count(source.get(camel))
            return token_count(source.get(snake)) if value is None else value

        input_side = read("inputTokens", "input_tokens")
        total = read("totalTokens", "total_tokens")
        if input_side:
            return input_side, max(input_side, total or 0), False
        if total:
            return total, total, True
        return None

    def _open_compaction(self, ref: str | None) -> dict[str, Any]:
        """Begin tracking one ``contextCompaction`` item.

        The item carries no counts; they are read from the token-usage
        updates around it. The app-server order is: the last request's
        usage, ``item/started``, a usage update with no input side whose
        total is the reduced context (sometimes sent twice), then
        ``item/completed``. So the size before is the last reading at
        ``item/started``, fixed here, and the size after is the zero-input
        update that arrives while the item is open.
        """
        latest = self._reading
        self._compaction = {
            "ref": ref,
            "before": latest[1] if latest else None,
            "after": None,
            "completed": False,
        }
        return self._compaction

    def _emit_compaction(self, native_type: str) -> list[NormalizedEvent]:
        compaction, self._compaction = self._compaction, None
        if compaction is None:
            return []
        trigger = "manual" if self.compaction_requested else "auto"
        self.compaction_requested = False
        return [
            compaction_event(
                trigger=trigger,
                tokens_before=compaction["before"],
                tokens_after=compaction["after"],
                compaction_ref=compaction["ref"],
                native_type=native_type,
            )
        ]

    def _flush_compaction(self, native_type: str) -> list[NormalizedEvent]:
        """Record a finished compaction whose ``after`` never arrived."""
        if self._compaction is None or not self._compaction["completed"]:
            return []
        return self._emit_compaction(native_type)

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
        if method == "thread/compacted":
            # Deprecated in the installed protocol. The ``contextCompaction``
            # item is the one signal; reading both would record it twice.
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
            if kind == COMPACTION_ITEM:
                # Not a tool: the compaction is recorded once, on completion.
                flushed = self._flush_compaction(method)
                self._open_compaction(ref)
                return flushed
            return [
                *self._flush_compaction(method),
                NormalizedEvent(
                    "tool.started",
                    {"tool_ref": ref, "name": kind},
                    method,
                ),
            ]
        if method == "item/completed":
            kind, ref = self._item_kind(params)
            if kind in {None, "agentMessage", "reasoning"}:
                return []
            if kind == COMPACTION_ITEM:
                compaction = self._compaction
                if compaction is None or compaction["completed"]:
                    flushed = self._flush_compaction(method)
                    compaction = self._open_compaction(ref)
                else:
                    flushed = []
                compaction["completed"] = True
                if compaction["after"] is None:
                    # No size arrived while the item was open; the next
                    # token-usage update is the first reading after it.
                    return flushed
                return [*flushed, *self._emit_compaction(method)]
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
        # Receive-side tolerance, deliberately wider than the newest CLI:
        # 0.155.1's ServerNotification schema carries only
        # `thread/tokenUsage/updated`, but older builds inside the supported
        # window emit the `turn/` spelling, so both stay mapped. Usage is
        # advisory, so an unmatched spelling costs a metric, not a turn.
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
            if not usage:
                return []
            reading = self._usage_reading(source)
            events: list[NormalizedEvent] = []
            if reading is not None:
                self._reading = reading
                compaction = self._compaction
                if compaction is not None and compaction["completed"]:
                    compaction["after"] = reading[0]
                    events.extend(self._emit_compaction(method))
                elif compaction is not None and reading[2]:
                    # The zero-input update inside the item. A repeat of it
                    # only restates the same figure: one marker, no more.
                    compaction["after"] = reading[0]
            events.append(
                usage_event(
                    usage,
                    method,
                    scope="request",
                    context_tokens=reading[0] if reading else None,
                    context_window=(
                        token_usage.get("modelContextWindow")
                        if isinstance(token_usage, dict)
                        else None
                    ),
                )
            )
            return events
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
                *self._flush_compaction(method),
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
        if turn.metadata.get("compaction"):
            yield from self._stream_compaction(turn)
            return
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

    def _next_notification(
        self,
        deadline: float,
        stopped: Any = None,
    ) -> Mapping[str, Any] | None:
        """One notification, or None once ``deadline`` passes, none remain,
        or ``stopped()`` turns true. The wait is taken in short slices so a
        Stop is seen promptly."""
        pull = getattr(self._rpc, "next_notification", None)
        if callable(pull):
            while True:
                if stopped is not None and stopped():
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                raw = pull(min(remaining, COMPACT_POLL_SECONDS))
                if raw is not None:
                    return raw
        if stopped is not None and stopped():
            return None
        if self._notification_source is None:
            self._notification_source = iter(self._rpc.notifications())
        return next(self._notification_source, None)

    def _stream_compaction(self, turn: NativeTurn) -> Iterator[NormalizedEvent]:
        """Follow a requested compaction to its terminal, within a bound.

        The request was accepted before this runs. The app-server then starts
        a turn of its own for the compaction; its id is adopted from
        ``turn/started`` so the exact-turn checks apply from there on. A
        thread that never starts that turn, or a turn that outlives the wait,
        ends the run without a compaction: the caller reports that the
        harness did not say it compacted. A Stop before the turn id is known
        cannot be sent to Codex (there is no turn to name), so it ends the
        wait here and the run is interrupted; the app-server is closed with
        the run. Codex states no token usage for the compaction request: the
        updates around it are the last turn's, replayed, and its estimate of
        the new context, so none is passed on as this run's usage.
        """
        deadline = time.monotonic() + self.compact_start_seconds
        adopted = False

        def stopped() -> bool:
            return not adopted and bool(turn.metadata.get("interrupt_requested"))

        while True:
            raw = self._next_notification(deadline, stopped)
            if raw is None:
                break
            params = raw.get("params")
            if not isinstance(params, dict):
                continue
            if adopted:
                if not self._matches(turn, params):
                    continue
            else:
                thread_ref = params.get("threadId")
                if thread_ref is not None and thread_ref != turn.session_ref:
                    continue
                native = params.get("turn")
                native_ref = native.get("id") if isinstance(native, dict) else None
                if (
                    raw.get("method") == "turn/started"
                    and isinstance(native_ref, str)
                    and native_ref
                ):
                    adopted = True
                    # The broker persists the changed reference, so recovery
                    # and Stop name the real turn from here on.
                    turn.run_ref = native_ref
                    turn.metadata["adopted"] = True
                    deadline = time.monotonic() + self.compact_wait_seconds
                    if turn.metadata.get("interrupt_requested"):
                        # Stop arrived as the turn started: pass it on now.
                        try:
                            self._rpc.request(
                                "turn/interrupt",
                                {"threadId": turn.session_ref, "turnId": native_ref},
                            )
                        except AdapterError:
                            pass
            for event in self._normalize(raw):
                if event.type == "usage":
                    continue
                if event.type in TERMINAL_EVENTS:
                    turn.metadata["terminal"] = event.type
                    if event.interrupt_evidence:
                        turn.metadata["interrupt_evidence"] = (
                            event.interrupt_evidence
                        )
                yield event
                if event.type in TERMINAL_EVENTS:
                    return
        # The tag is cleared only after a finished compaction is flushed, so
        # that marker is still ``manual``.
        yield from self._flush_compaction("compaction.wait")
        self.compaction_requested = False
        if stopped():
            turn.metadata["terminal"] = "run.interrupted"
            turn.metadata["interrupt_evidence"] = "operator"
            yield NormalizedEvent(
                "run.interrupted",
                {"status": "cancelled"},
                "operator.interrupt",
                "operator",
            )
            return
        turn.metadata["terminal"] = "run.completed"
        yield NormalizedEvent(
            "run.completed",
            {"status": "completed", "compaction_wait": "elapsed"},
            "compaction.wait",
        )

    def interrupt(self, turn: NativeTurn) -> InterruptResult:
        if self._rpc is None:
            return InterruptResult(False, "Codex app-server is not connected")
        if turn.metadata.get("compaction") and not turn.metadata.get("adopted"):
            # A requested compaction has no turn id until Codex starts it.
            # The stream sees this flag and ends the wait.
            turn.metadata["interrupt_requested"] = True
            return InterruptResult(True)
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
