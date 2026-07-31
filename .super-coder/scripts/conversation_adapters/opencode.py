#!/usr/bin/env python3
"""OpenCode server-backed conversation adapter."""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import secrets
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from .base import (
    TERMINAL_EVENTS,
    AdapterError,
    ConversationAdapter,
    ConversationContext,
    HttpTransport,
    InterruptResult,
    NativeTurn,
    NormalizedEvent,
    ProbeResult,
    ReconcileResult,
    SessionInspection,
    UrlHttpTransport,
    ensure_exact_session,
    ensure_nonempty_message,
    load_manifest,
    terminal_outcome,
)

ENGINE = Path(__file__).resolve().parents[2]
SERVER_ENDPOINT = "http://127.0.0.1:4096"
SERVER_LOG = ENGINE / "logs" / "opencode-server.log"
SHELL_RUNTIME_DIR = ENGINE / "run" / "opencode-shells"
TURN_TIMEOUT_SECONDS = 5400.0
_SERVER_LOCK = threading.RLock()
_SERVER_PROCESS: subprocess.Popen | None = None
_SERVER_PASSWORD: str | None = None
_SERVER_LOG_HANDLE = None


def _server_healthy(password: str | None) -> bool:
    try:
        health = UrlHttpTransport(
            SERVER_ENDPOINT,
            password=password,
            timeout=1.0,
        ).request("GET", "/global/health")
    except AdapterError:
        return False
    return bool(
        isinstance(health, dict)
        and health.get("healthy")
        and isinstance(health.get("version"), str)
    )


def ensure_server(*, timeout: float = 10.0) -> str | None:
    """Return the credential for a healthy loopback ``opencode serve``.

    Browser conversations are server-backed, so the engine owns the local
    sidecar lifecycle instead of requiring an operator to start it by hand.
    An already-running compatible server is reused. A server started here is
    loopback-only and protected with an in-memory Basic Auth password.
    """
    global _SERVER_PROCESS, _SERVER_PASSWORD, _SERVER_LOG_HANDLE
    with _SERVER_LOCK:
        configured_password = os.environ.get("OPENCODE_SERVER_PASSWORD")
        candidate_passwords = []
        for password in (_SERVER_PASSWORD, configured_password, None):
            if password not in candidate_passwords:
                candidate_passwords.append(password)
        for password in candidate_passwords:
            if _server_healthy(password):
                _SERVER_PASSWORD = password
                return password

        if _SERVER_PROCESS is not None and _SERVER_PROCESS.poll() is None:
            raise AdapterError(
                "HARNESS_UNAVAILABLE",
                "managed OpenCode server is running but failed its health check",
                retryable=True,
            )

        binary = shutil.which("opencode")
        if not binary:
            raise AdapterError(
                "HARNESS_UNAVAILABLE",
                "opencode executable is not installed",
                retryable=True,
            )

        password = configured_password or secrets.token_urlsafe(32)
        env = dict(os.environ)
        env["OPENCODE_SERVER_PASSWORD"] = password
        env.setdefault("OPENCODE_SERVER_USERNAME", "opencode")
        env["OPENCODE_DISABLE_CLAUDE_CODE"] = "1"
        SERVER_LOG.parent.mkdir(parents=True, exist_ok=True)
        _SERVER_LOG_HANDLE = SERVER_LOG.open("a")
        try:
            _SERVER_PROCESS = subprocess.Popen(
                [
                    binary,
                    "serve",
                    "--hostname",
                    "127.0.0.1",
                    "--port",
                    "4096",
                    "--log-level",
                    "WARN",
                ],
                stdin=subprocess.DEVNULL,
                stdout=_SERVER_LOG_HANDLE,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            _SERVER_LOG_HANDLE.close()
            _SERVER_LOG_HANDLE = None
            raise AdapterError(
                "HARNESS_UNAVAILABLE",
                f"could not start opencode serve: {exc}",
                retryable=True,
            ) from exc

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _SERVER_PROCESS.poll() is not None:
                break
            if _server_healthy(password):
                _SERVER_PASSWORD = password
                return password
            time.sleep(0.05)

        exit_code = _SERVER_PROCESS.poll()
        stop_server()
        detail = (
            f"exited with code {exit_code}"
            if exit_code is not None
            else f"did not become healthy within {timeout:g}s"
        )
        raise AdapterError(
            "HARNESS_UNAVAILABLE",
            f"opencode serve {detail}; see {SERVER_LOG}",
            retryable=True,
        )


def stop_server() -> None:
    """Stop only the OpenCode server process this module started."""
    global _SERVER_PROCESS, _SERVER_PASSWORD, _SERVER_LOG_HANDLE
    with _SERVER_LOCK:
        process = _SERVER_PROCESS
        _SERVER_PROCESS = None
        _SERVER_PASSWORD = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        if _SERVER_LOG_HANDLE is not None:
            _SERVER_LOG_HANDLE.close()
            _SERVER_LOG_HANDLE = None


def provider_state() -> dict[str, Any]:
    """Return OpenCode's authoritative provider/model connection projection."""
    password = ensure_server()
    result = UrlHttpTransport(
        SERVER_ENDPOINT,
        password=password,
        timeout=10.0,
    ).request("GET", "/provider")
    if not isinstance(result, dict):
        raise AdapterError(
            "HARNESS_PROTOCOL_ERROR",
            "OpenCode provider response was not an object",
        )
    return result


def connected_models(state: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Flatten models belonging to providers OpenCode reports as connected."""
    state = state or provider_state()
    connected = {
        item for item in (state.get("connected") or []) if isinstance(item, str)
    }
    models: list[dict[str, Any]] = []
    for provider in state.get("all") or []:
        if not isinstance(provider, dict) or provider.get("id") not in connected:
            continue
        provider_id = provider["id"]
        for model_id, model in (provider.get("models") or {}).items():
            if not isinstance(model_id, str) or not isinstance(model, dict):
                continue
            status = model.get("status")
            if status not in (None, "active"):
                continue
            models.append(
                {
                    "id": f"{provider_id}/{model_id}",
                    "provider": provider_id,
                    "provider_model": model_id,
                    "name": model.get("name") or model_id,
                    "family": model.get("family"),
                    "release_date": model.get("release_date") or "",
                    "status": status or "active",
                }
            )
    return sorted(models, key=lambda item: (item["provider"], item["id"]))


atexit.register(stop_server)


class OpenCodeAdapter(ConversationAdapter):
    harness = "opencode"

    def __init__(
        self,
        *,
        endpoint: str = SERVER_ENDPOINT,
        password: str | None = None,
        transport: HttpTransport | None = None,
        manifest: Mapping[str, Any] | None = None,
        shell_runtime_dir: Path = SHELL_RUNTIME_DIR,
    ) -> None:
        super().__init__(manifest or load_manifest(self.harness))
        self.shell_runtime_dir = shell_runtime_dir
        if transport is not None:
            self.transport = transport
        else:
            self.transport = UrlHttpTransport(
                endpoint,
                password=password if password is not None else ensure_server(),
                timeout=TURN_TIMEOUT_SECONDS,
            )

    def probe(self) -> ProbeResult:
        health = self.transport.request("GET", "/global/health")
        if not isinstance(health, dict) or not health.get("healthy"):
            raise AdapterError(
                "HARNESS_UNAVAILABLE",
                "OpenCode server health check failed",
                retryable=True,
            )
        version = health.get("version")
        if not isinstance(version, str):
            raise AdapterError(
                "HARNESS_PROTOCOL_ERROR",
                "OpenCode health response omitted version",
            )
        return self._probe_result(version)

    @staticmethod
    def _route(context: ConversationContext) -> tuple[str, str] | None:
        if not context.model:
            return None
        if context.provider:
            prefix = f"{context.provider}/"
            model = (
                context.model.removeprefix(prefix)
                if context.model.startswith(prefix)
                else context.model
            )
            if model:
                return context.provider, model
        if "/" in context.model:
            provider, model = context.model.split("/", 1)
            if provider and model:
                return provider, model
        raise AdapterError(
            "HARNESS_MODEL_ROUTE_INVALID",
            "OpenCode models require provider plus model",
        )

    @staticmethod
    def _query(worktree: Path) -> dict[str, str]:
        return {"directory": str(worktree)}

    @staticmethod
    def _permission_rules(
        context: ConversationContext,
    ) -> list[dict[str, str]] | None:
        if context.permission_mode != "unrestricted":
            return None
        return [
            {
                "permission": "*",
                "pattern": "*",
                "action": "allow",
            }
        ]

    def _prepare_shell_environment(
        self,
        context: ConversationContext,
    ) -> Path:
        """Bind OpenCode shell tools to this conversation's launch identity.

        ``opencode serve`` is one long-lived process, so every shell tool it
        starts otherwise inherits the server's environment rather than the
        target shell's canonical ``LaunchPlan.env``. A server started from an
        already-wired shell could therefore route ``sc mem`` to that unrelated
        install and identity.

        OpenCode's project config supports an exact shell executable. Point it
        at an owner-only runtime wrapper which restores the canonical SC_*
        contract and PATH before delegating to bash. The project config stores
        only the wrapper path; the API key remains under the gitignored engine
        runtime directory.
        """
        worktree = context.checked_worktree()
        identity = hashlib.sha256(str(worktree).encode()).hexdigest()[:20]
        self.shell_runtime_dir.mkdir(parents=True, exist_ok=True)
        wrapper = self.shell_runtime_dir / f"{identity}.sh"
        exported = {
            key: str(value)
            for key, value in context.env.items()
            if key == "PATH" or key.startswith("SC_")
        }
        lines = ["#!/bin/sh"]
        lines.extend(
            f"export {key}={shlex.quote(value)}"
            for key, value in sorted(exported.items())
        )
        lines.append('exec /bin/bash "$@"')
        temporary = wrapper.with_name(
            f".{wrapper.name}.{secrets.token_hex(6)}.tmp"
        )
        temporary.write_text("\n".join(lines) + "\n")
        temporary.chmod(0o700)
        os.replace(temporary, wrapper)

        config_path = worktree / "opencode.json"
        try:
            config = json.loads(config_path.read_text())
        except FileNotFoundError:
            config = {}
        except (OSError, json.JSONDecodeError) as exc:
            raise AdapterError(
                "HARNESS_CONFIG_INVALID",
                f"cannot prepare OpenCode shell config: {exc}",
            ) from exc
        if not isinstance(config, dict):
            raise AdapterError(
                "HARNESS_CONFIG_INVALID",
                "OpenCode project config must be a JSON object",
            )
        config["shell"] = str(wrapper)
        config_path.write_text(json.dumps(config, indent=2) + "\n")
        return wrapper

    def _prompt(
        self,
        session_ref: str,
        context: ConversationContext,
        message: str,
    ) -> None:
        body: dict[str, Any] = {
            "parts": [{"type": "text", "text": message}],
        }
        route = self._route(context)
        if route:
            body["model"] = {
                "providerID": route[0],
                "modelID": route[1],
            }
        self.transport.request(
            "POST",
            f"/session/{session_ref}/message",
            query=self._query(context.checked_worktree()),
            body=body,
        )

    def _turn(
        self,
        session_ref: str,
        context: ConversationContext,
        message: str,
        *,
        resumed: bool,
    ) -> NativeTurn:
        worktree = context.checked_worktree()
        run_ref = f"msg_{uuid.uuid4().hex}"
        # `/event` is live rather than replayable. Open the subscription before
        # the synchronous message dispatch so all native activity is buffered
        # while `/message` waits for the completed assistant response.
        #
        # Do not use `/prompt_async`: OpenCode can lose async session state
        # after the first turn, accept later user messages without running
        # inference, and report those turns idle.
        event_stream = iter(
            self.transport.stream(
                "/event",
                query=self._query(worktree),
            )
        )
        turn = NativeTurn(
            harness=self.harness,
            session_ref=session_ref,
            run_ref=run_ref,
            worktree=worktree,
            metadata={
                "context": context,
                "event_stream": event_stream,
                "message": message,
                "dispatch_pending": True,
                "interrupt_lock": threading.Lock(),
                "resumed": resumed,
            },
        )
        return turn

    @staticmethod
    def _interrupt_lock(turn: NativeTurn):
        lock = turn.metadata.get("interrupt_lock")
        if lock is None:
            lock = threading.Lock()
            turn.metadata["interrupt_lock"] = lock
        return lock

    def start(self, context: ConversationContext, message: str) -> NativeTurn:
        worktree = context.checked_worktree()
        message = ensure_nonempty_message(message)
        self._prepare_shell_environment(context)
        body: dict[str, Any] = {}
        if context.title:
            body["title"] = context.title
        route = self._route(context)
        if route:
            body["model"] = {
                "providerID": route[0],
                "id": route[1],
            }
        permission = self._permission_rules(context)
        if permission:
            body["permission"] = permission
        created = self.transport.request(
            "POST",
            "/session",
            query=self._query(worktree),
            body=body,
        )
        session_ref = created.get("id") if isinstance(created, dict) else None
        if not isinstance(session_ref, str) or not session_ref:
            raise AdapterError(
                "HARNESS_PROTOCOL_ERROR",
                "OpenCode session.create returned no id",
            )
        return self._turn(session_ref, context, message, resumed=False)

    def resume(
        self,
        session_ref: str,
        context: ConversationContext,
        message: str,
    ) -> NativeTurn:
        message = ensure_nonempty_message(message)
        self._prepare_shell_environment(context)
        inspected = self.inspect(session_ref, context)
        if not inspected.exists:
            raise AdapterError(
                "HARNESS_SESSION_LOST",
                f"OpenCode session does not exist: {session_ref}",
            )
        # Session permissions are persisted from create. OpenCode's session
        # PATCH contract is for mutable metadata such as title; repeatedly
        # sending permission rules duplicates them in native session state.
        return self._turn(session_ref, context, message, resumed=True)

    @staticmethod
    def _event_payload(raw: Mapping[str, Any]) -> Mapping[str, Any]:
        payload = raw.get("payload")
        return payload if isinstance(payload, dict) else raw

    @classmethod
    def _session_of(cls, raw: Mapping[str, Any]) -> str | None:
        event = cls._event_payload(raw)
        props = event.get("properties")
        if isinstance(props, dict):
            value = props.get("sessionID") or props.get("sessionId")
            if isinstance(value, str):
                return value
            info = props.get("info")
            if isinstance(info, dict):
                value = info.get("sessionID") or info.get("sessionId")
                return value if isinstance(value, str) else None
        return None

    @staticmethod
    def _error_name(error: Any) -> str:
        if isinstance(error, dict):
            for field in ("name", "type", "code"):
                value = error.get(field)
                if isinstance(value, str):
                    return value
            nested = error.get("data")
            if isinstance(nested, dict):
                return OpenCodeAdapter._error_name(nested)
        return type(error).__name__

    def _normalize(
        self,
        raw: Mapping[str, Any],
    ) -> list[NormalizedEvent]:
        event = self._event_payload(raw)
        native_type = event.get("type")
        if not isinstance(native_type, str):
            return []
        props = event.get("properties")
        props = props if isinstance(props, dict) else {}

        if native_type == "session.status":
            status = props.get("status")
            status_type = (
                status.get("type") if isinstance(status, dict) else status
            )
            if status_type == "busy":
                return [
                    NormalizedEvent(
                        "run.started",
                        {"status": "running"},
                        native_type,
                    )
                ]
            return []
        if native_type == "session.idle":
            return [
                NormalizedEvent(
                    "run.completed",
                    {"status": "completed"},
                    native_type,
                )
            ]
        if native_type == "session.error":
            error = props.get("error")
            name = self._error_name(error)
            interrupted = name == "MessageAbortedError"
            return [
                NormalizedEvent(
                    "run.interrupted" if interrupted else "run.failed",
                    {"error": name},
                    native_type,
                    "native" if interrupted else None,
                )
            ]
        if native_type == "message.part.delta":
            if props.get("field") == "text" and isinstance(
                props.get("delta"), str
            ):
                return [
                    NormalizedEvent(
                        "assistant.delta",
                        {"text": props["delta"]},
                        native_type,
                    )
                ]
            return []
        if native_type in {
            "permission.asked",
            "permission.v2.asked",
        }:
            return [
                NormalizedEvent(
                    "permission.requested",
                    {
                        "request_ref": props.get("id"),
                        "action": props.get("action"),
                        "resources": props.get("resources", []),
                    },
                    native_type,
                )
            ]
        if native_type in {"question.asked", "question.v2.asked"}:
            return [
                NormalizedEvent(
                    "input.requested",
                    {
                        "request_ref": props.get("id"),
                        "questions": props.get("questions", []),
                    },
                    native_type,
                )
            ]
        if native_type in {
            "session.next.tool.called",
            "session.next.shell.started",
        }:
            return [
                NormalizedEvent(
                    "tool.started",
                    {
                        "tool_ref": props.get("id"),
                        "name": props.get("tool") or props.get("command"),
                    },
                    native_type,
                )
            ]
        if native_type in {
            "session.next.tool.success",
            "session.next.tool.failed",
            "session.next.shell.ended",
        }:
            return [
                NormalizedEvent(
                    "tool.completed",
                    {
                        "tool_ref": props.get("id"),
                        "status": (
                            "failed" if native_type.endswith("failed") else "completed"
                        ),
                    },
                    native_type,
                )
            ]
        if native_type == "message.updated":
            info = props.get("info")
            if isinstance(info, dict) and info.get("role") == "assistant":
                tokens = info.get("tokens")
                if isinstance(tokens, dict):
                    safe = {
                        key: value
                        for key, value in tokens.items()
                        if isinstance(value, (int, float))
                    }
                    if safe:
                        return [
                            NormalizedEvent(
                                "usage",
                                {"tokens": safe},
                                native_type,
                            )
                        ]
        return []

    def stream(self, turn: NativeTurn) -> Iterator[NormalizedEvent]:
        native_stream = turn.metadata.get("event_stream")
        if native_stream is None:
            raise AdapterError(
                "HARNESS_PROTOCOL_ERROR",
                "OpenCode turn has no pre-dispatch event subscription",
            )
        yield NormalizedEvent(
            "session.started",
            {
                "session_ref": turn.session_ref,
                "resumed": bool(turn.metadata.get("resumed")),
            },
            "session.create-or-resume",
        )
        context = turn.metadata.pop("context", None)
        message = turn.metadata.pop("message", None)
        if not isinstance(context, ConversationContext) or not isinstance(
            message, str
        ):
            raise AdapterError(
                "HARNESS_PROTOCOL_ERROR",
                "OpenCode turn has no pending synchronous message dispatch",
            )
        interrupted_before_dispatch = False
        with self._interrupt_lock(turn):
            if turn.metadata.get("interrupt_requested"):
                turn.metadata["dispatch_pending"] = False
                turn.metadata["terminal"] = "run.interrupted"
                interrupted_before_dispatch = True
            else:
                turn.metadata["dispatch_pending"] = False
        if interrupted_before_dispatch:
            yield NormalizedEvent(
                "run.interrupted",
                {"status": "cancelled"},
                "operator.interrupt",
                "operator",
            )
            return
        self._prompt(turn.session_ref, context, message)
        observed_activity = False
        for raw in native_stream:
            event_session = self._session_of(raw)
            if event_session and event_session != turn.session_ref:
                continue
            for event in self._normalize(raw):
                # Opening `/event` for an existing session can enqueue its
                # current idle state before the message is dispatched. That
                # idle belongs to the previous turn; accepting it would mark
                # the new message complete without ever generating a reply.
                if event.type == "run.completed" and not observed_activity:
                    continue
                if event.type == "run.completed":
                    with self._interrupt_lock(turn):
                        if turn.metadata.get("interrupt_acknowledged"):
                            event = NormalizedEvent(
                                "run.interrupted",
                                {"status": "cancelled"},
                                event.native_type,
                                "operator",
                            )
                if event.type in {
                    "run.started",
                    "assistant.delta",
                    "tool.started",
                    "tool.completed",
                    "permission.requested",
                    "input.requested",
                }:
                    observed_activity = True
                if event.type in TERMINAL_EVENTS:
                    turn.metadata["terminal"] = event.type
                yield event
                if event.type in TERMINAL_EVENTS:
                    return

    def interrupt(self, turn: NativeTurn) -> InterruptResult:
        with self._interrupt_lock(turn):
            turn.metadata["interrupt_requested"] = True
            result = self.transport.request(
                "POST",
                f"/session/{turn.session_ref}/abort",
                query=self._query(turn.worktree),
            )
            acknowledged = bool(result) or bool(
                turn.metadata.get("dispatch_pending")
            )
            turn.metadata["interrupt_acknowledged"] = acknowledged
        return InterruptResult(acknowledged)

    def inspect(
        self,
        session_ref: str,
        context: ConversationContext,
    ) -> SessionInspection:
        worktree = context.checked_worktree()
        try:
            session = self.transport.request(
                "GET",
                f"/session/{session_ref}",
                query=self._query(worktree),
            )
        except AdapterError as exc:
            if exc.code == "HARNESS_SESSION_LOST":
                return SessionInspection(session_ref, False, "missing")
            raise
        actual = session.get("id") if isinstance(session, dict) else None
        ensure_exact_session(session_ref, actual)
        status: Any = None
        statuses = self.transport.request(
            "GET",
            "/session/status",
            query=self._query(worktree),
        )
        if isinstance(statuses, dict):
            status = statuses.get(session_ref)
        state = (
            status.get("type")
            if isinstance(status, dict)
            else status if isinstance(status, str) else "idle"
        )
        return SessionInspection(
            session_ref,
            True,
            str(state),
            worktree,
            {"title": session.get("title") if isinstance(session, dict) else None},
        )

    def reconcile(
        self,
        turn: NativeTurn,
        context: ConversationContext,
    ) -> ReconcileResult:
        terminal = turn.metadata.get("terminal")
        if terminal in TERMINAL_EVENTS:
            return ReconcileResult(
                terminal_outcome(terminal),
                True,
                f"terminal {terminal} was observed on the native stream",
            )
        inspection = self.inspect(turn.session_ref, context)
        if not inspection.exists:
            return ReconcileResult(
                "unknown",
                False,
                "native OpenCode session is missing",
            )
        if inspection.state == "busy":
            return ReconcileResult(
                "running",
                True,
                "OpenCode reports the exact session busy",
            )
        return ReconcileResult(
            "unknown",
            False,
            "OpenCode session is idle without a recorded terminal event",
        )
