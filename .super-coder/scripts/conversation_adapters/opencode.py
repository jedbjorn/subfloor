#!/usr/bin/env python3
"""OpenCode server-backed conversation adapter."""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import queue
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import harness_versions
import opencode_config
import route_transport

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
    command_version,
    ensure_exact_session,
    ensure_nonempty_message,
    load_manifest,
    terminal_outcome,
)

ENGINE = Path(__file__).resolve().parents[2]
SERVER_ENDPOINT = "http://127.0.0.1:4096"
SERVER_LOG = ENGINE / "logs" / "opencode-server.log"
SERVER_STATE = ENGINE / "run" / "opencode-server.json"
SHELL_RUNTIME_DIR = ENGINE / "run" / "opencode-shells"
TURN_TIMEOUT_SECONDS = 5400.0
TURN_CLEANUP_TIMEOUT_SECONDS = 10.0
_SERVER_LOCK = threading.RLock()
_SERVER_PROCESS: subprocess.Popen | None = None
_SERVER_ENDPOINT = SERVER_ENDPOINT
_SERVER_PASSWORD: str | None = None
_SERVER_LOG_HANDLE = None
_CONTEXT_SERVERS: list[tuple[subprocess.Popen, Any]] = []

MAX_CONNECTED_PROVIDERS = 256
MAX_CONNECTED_MODELS = 2_000
MAX_NATIVE_OPTIONS = 256
MAX_NATIVE_IDENTIFIER_CHARS = 512


class _OpenCodeProjection:
    """Reduce one exact-session OpenCode typed-part stream."""

    def __init__(self) -> None:
        self.message_roles: dict[str, str] = {}
        self.part_kinds: dict[str, str] = {}
        self.part_messages: dict[str, str] = {}
        self.part_text: dict[str, str] = {}
        self.part_emitted: dict[str, str] = {}
        self.tool_states: dict[str, str] = {}
        self.run_started = False
        self.last_usage: tuple[tuple[str, int | float], ...] | None = None

    def _flush_text(self, part_id: str, native_type: str) -> list[NormalizedEvent]:
        kind = self.part_kinds.get(part_id)
        message_id = self.part_messages.get(part_id)
        if kind not in {"text", "reasoning"} or not message_id:
            return []
        if self.message_roles.get(message_id) != "assistant":
            return []
        text = self.part_text.get(part_id, "")
        emitted = self.part_emitted.get(part_id, "")
        if not text.startswith(emitted):
            raise AdapterError(
                "HARNESS_PROTOCOL_ERROR",
                f"OpenCode replaced emitted text for part {part_id}",
            )
        delta = text[len(emitted):]
        if not delta:
            return []
        self.part_emitted[part_id] = text
        return [
            NormalizedEvent(
                "assistant.delta",
                {
                    "text": delta,
                    "segment": "reasoning" if kind == "reasoning" else "answer",
                },
                native_type,
            )
        ]

    def _merge_full_text(self, part_id: str, text: str) -> None:
        current = self.part_text.get(part_id, "")
        if text.startswith(current):
            self.part_text[part_id] = text
            return
        if current.startswith(text):
            return
        raise AdapterError(
            "HARNESS_PROTOCOL_ERROR",
            f"OpenCode returned irreconcilable text for part {part_id}",
        )

    def _tool_events(
        self,
        tool_ref: str,
        name: str | None,
        status: str,
        native_type: str,
    ) -> list[NormalizedEvent]:
        previous = self.tool_states.get(tool_ref)
        if status in {"pending", "running"}:
            if previous is not None:
                return []
            self.tool_states[tool_ref] = "running"
            return [
                NormalizedEvent(
                    "tool.started",
                    {"tool_ref": tool_ref, "name": name},
                    native_type,
                )
            ]
        if status not in {"completed", "error"}:
            return []
        if previous in {"completed", "error"}:
            return []
        events = []
        if previous is None:
            events.append(
                NormalizedEvent(
                    "tool.started",
                    {"tool_ref": tool_ref, "name": name},
                    native_type,
                )
            )
        self.tool_states[tool_ref] = status
        events.append(
            NormalizedEvent(
                "tool.completed",
                {
                    "tool_ref": tool_ref,
                    "status": "failed" if status == "error" else "completed",
                },
                native_type,
            )
        )
        return events

    def normalize(self, raw: Mapping[str, Any]) -> list[NormalizedEvent]:
        event = OpenCodeAdapter._event_payload(raw)
        native_type = event.get("type")
        if not isinstance(native_type, str):
            return []
        props = event.get("properties")
        props = props if isinstance(props, dict) else {}

        if native_type == "session.status":
            status = props.get("status")
            status_type = status.get("type") if isinstance(status, dict) else status
            if status_type != "busy" or self.run_started:
                return []
            self.run_started = True
            return [
                NormalizedEvent(
                    "run.started", {"status": "running"}, native_type
                )
            ]
        if native_type == "session.idle":
            return [
                NormalizedEvent(
                    "run.completed", {"status": "completed"}, native_type
                )
            ]
        if native_type == "session.error":
            error = props.get("error")
            name = OpenCodeAdapter._error_name(error)
            interrupted = name == "MessageAbortedError"
            return [
                NormalizedEvent(
                    "run.interrupted" if interrupted else "run.failed",
                    {"error": name},
                    native_type,
                    "native" if interrupted else None,
                )
            ]
        if native_type == "message.updated":
            return self._message_updated(props, native_type)
        if native_type == "message.part.delta":
            return self._part_delta(props, native_type)
        if native_type == "message.part.updated":
            return self._part_updated(props, native_type)
        if native_type in {"permission.asked", "permission.v2.asked"}:
            return [
                NormalizedEvent(
                    "permission.requested",
                    {
                        "request_ref": props.get("id"),
                        "action": props.get("permission") or props.get("action"),
                        "resources": props.get("patterns") or props.get("resources", []),
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
            tool_ref = props.get("callID") or props.get("id")
            if not isinstance(tool_ref, str):
                return []
            return self._tool_events(
                tool_ref,
                props.get("tool") or ("bash" if props.get("command") else None),
                "running",
                native_type,
            )
        if native_type in {
            "session.next.tool.success",
            "session.next.tool.failed",
            "session.next.shell.ended",
        }:
            tool_ref = props.get("callID") or props.get("id")
            if not isinstance(tool_ref, str):
                return []
            return self._tool_events(
                tool_ref,
                None,
                "error" if native_type.endswith("failed") else "completed",
                native_type,
            )
        return []

    def _message_updated(
        self,
        props: Mapping[str, Any],
        native_type: str,
    ) -> list[NormalizedEvent]:
        info = props.get("info")
        if not isinstance(info, dict):
            return []
        message_id = info.get("id")
        role = info.get("role")
        events: list[NormalizedEvent] = []
        if isinstance(message_id, str) and isinstance(role, str):
            self.message_roles[message_id] = role
            for part_id, owner in tuple(self.part_messages.items()):
                if owner == message_id:
                    events.extend(self._flush_text(part_id, native_type))
        if role != "assistant":
            return events
        tokens = info.get("tokens")
        if not isinstance(tokens, dict):
            return events
        safe = {
            key: value
            for key, value in tokens.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        if not safe or not any(safe.values()):
            return events
        usage = tuple(sorted(safe.items()))
        if usage == self.last_usage:
            return events
        self.last_usage = usage
        events.append(NormalizedEvent("usage", {"tokens": safe}, native_type))
        return events

    def _part_delta(
        self,
        props: Mapping[str, Any],
        native_type: str,
    ) -> list[NormalizedEvent]:
        if props.get("field") != "text" or not isinstance(props.get("delta"), str):
            return []
        part_id = props.get("partID")
        if not isinstance(part_id, str):
            return []
        message_id = props.get("messageID")
        if isinstance(message_id, str):
            self.part_messages[part_id] = message_id
        self.part_text[part_id] = self.part_text.get(part_id, "") + props["delta"]
        return self._flush_text(part_id, native_type)

    def _part_updated(
        self,
        props: Mapping[str, Any],
        native_type: str,
    ) -> list[NormalizedEvent]:
        part = props.get("part")
        if not isinstance(part, dict):
            return []
        part_id = part.get("id")
        kind = part.get("type")
        if not isinstance(part_id, str) or not isinstance(kind, str):
            return []
        message_id = part.get("messageID")
        if isinstance(message_id, str):
            self.part_messages[part_id] = message_id
        if kind in {"text", "reasoning"}:
            self.part_kinds[part_id] = kind
            text = part.get("text")
            if isinstance(text, str):
                self._merge_full_text(part_id, text)
            return self._flush_text(part_id, native_type)
        if kind != "tool":
            return []
        state = part.get("state")
        status = state.get("status") if isinstance(state, dict) else None
        tool_ref = part.get("callID") or part_id
        if not isinstance(status, str) or not isinstance(tool_ref, str):
            return []
        name = part.get("tool")
        return self._tool_events(
            tool_ref,
            name if isinstance(name, str) else None,
            status,
            native_type,
        )

    def reconcile_response(self, response: Any) -> list[NormalizedEvent]:
        if not isinstance(response, dict):
            return []
        events: list[NormalizedEvent] = []
        info = response.get("info")
        if isinstance(info, dict):
            events.extend(self._message_updated({"info": info}, "message.response"))
        for part in response.get("parts") or []:
            if isinstance(part, dict):
                events.extend(
                    self._part_updated({"part": part}, "message.response")
                )
        return events


def _read_server_state() -> dict[str, Any] | None:
    """Return the recorded managed-server identity, if any is readable."""
    try:
        data = json.loads(SERVER_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("pid"), int)
        or isinstance(data["pid"], bool)
        or not isinstance(data.get("password"), str)
    ):
        return None
    return data


def _server_endpoint(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _server_port(state: Mapping[str, Any]) -> int:
    """Return a validated recorded port, accepting legacy fixed-port state."""
    port = state.get("port", 4096)
    if (
        isinstance(port, bool)
        or not isinstance(port, int)
        or not 1 <= port <= 65535
    ):
        return 4096
    return port


def _available_loopback_port() -> int:
    """Ask the kernel for a currently available private sidecar port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _write_server_state(pid: int, password: str, port: int) -> None:
    SERVER_STATE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(SERVER_STATE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"pid": pid, "password": password, "port": port}, handle)


def _clear_server_state() -> None:
    try:
        SERVER_STATE.unlink()
    except OSError:
        pass


def _pid_is_opencode_serve(pid: int) -> bool:
    """Best-effort identity check so only an opencode serve is ever reaped."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        cmdline = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace")
    except OSError:
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "args="],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode != 0:
            return False
        cmdline = result.stdout
    parts = cmdline.split()
    return "serve" in parts and any(
        os.path.basename(part) == "opencode" for part in parts
    )


def _reap_orphan_server(pid: int) -> None:
    """Terminate a recorded orphan that fails every health check."""
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if not _pid_is_opencode_serve(pid):
            return
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _server_healthy(endpoint: str, password: str | None) -> bool:
    try:
        health = UrlHttpTransport(
            endpoint,
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


def _server_version(endpoint: str, password: str | None) -> str | None:
    """Read the exact version of one healthy managed execution seat."""
    try:
        health = UrlHttpTransport(
            endpoint,
            password=password,
            timeout=1.0,
        ).request("GET", "/global/health")
    except AdapterError:
        return None
    version = health.get("version") if isinstance(health, dict) else None
    return version if isinstance(version, str) and version else None


def ensure_server(*, timeout: float = 10.0) -> tuple[str, str | None]:
    """Return the endpoint and credential for a healthy ``opencode serve``.

    Browser conversations are server-backed, so the engine owns the local
    sidecar lifecycle instead of requiring an operator to start it by hand.
    An already-running compatible server is reused. A server started here is
    loopback-only and protected with an in-memory Basic Auth password.
    """
    global _SERVER_PROCESS, _SERVER_ENDPOINT, _SERVER_PASSWORD
    global _SERVER_LOG_HANDLE
    with _SERVER_LOCK:
        configured_password = os.environ.get("OPENCODE_SERVER_PASSWORD")
        state = _read_server_state()
        state_pid = state["pid"] if state else None
        state_endpoint = (
            _server_endpoint(_server_port(state)) if state else None
        )
        candidates = []
        for endpoint, password in (
            (_SERVER_ENDPOINT, _SERVER_PASSWORD),
            (state_endpoint, state["password"] if state else None),
            (SERVER_ENDPOINT, configured_password),
            (SERVER_ENDPOINT, None),
        ):
            candidate = (endpoint, password)
            if endpoint is not None and candidate not in candidates:
                candidates.append(candidate)
        for endpoint, password in candidates:
            if _server_healthy(endpoint, password):
                first_orphan_adoption = bool(
                    state
                    and state_pid is not None
                    and endpoint == state_endpoint
                    and password == state["password"]
                    and _SERVER_PROCESS is None
                    and (_SERVER_ENDPOINT, _SERVER_PASSWORD)
                    != (state_endpoint, state["password"])
                )
                installed_version = (
                    harness_versions.probe("opencode")
                    if first_orphan_adoption
                    else None
                )
                server_version = (
                    _server_version(endpoint, password)
                    if installed_version is not None
                    else None
                )
                if (
                    installed_version is not None
                    and server_version is not None
                    and installed_version != server_version
                    and _pid_is_opencode_serve(state_pid)
                ):
                    # A healthy orphan is still the wrong managed-conversation
                    # execution seat after the CLI is upgraded. Rotate it only
                    # at first adoption, before this process can hand the
                    # endpoint to an active conversation.
                    _clear_server_state()
                    _reap_orphan_server(state_pid)
                    state_pid = None
                    break
                _SERVER_ENDPOINT = endpoint
                _SERVER_PASSWORD = password
                return endpoint, password

        if state_pid is not None:
            # A server this engine started outlived the process that spawned
            # it (restart, crash) and answers none of our passwords. Reap only
            # the verified orphan before starting an independently addressed
            # replacement.
            _clear_server_state()
            if _pid_is_opencode_serve(state_pid):
                _reap_orphan_server(state_pid)

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
        port = _available_loopback_port()
        endpoint = _server_endpoint(port)
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
                    str(port),
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
            if _server_healthy(endpoint, password):
                _SERVER_ENDPOINT = endpoint
                _SERVER_PASSWORD = password
                _write_server_state(_SERVER_PROCESS.pid, password, port)
                return endpoint, password
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


def start_context_server(
    context: ConversationContext,
    *,
    timeout: float = 10.0,
) -> tuple[subprocess.Popen, Any, str, str]:
    """Start one conversation server inside its canonical execution view."""
    binary = shutil.which("opencode")
    if not binary:
        raise AdapterError(
            "HARNESS_UNAVAILABLE",
            "opencode executable is not installed",
            retryable=True,
        )
    password = secrets.token_urlsafe(32)
    port = _available_loopback_port()
    endpoint = _server_endpoint(port)
    env = dict(context.env)
    env["OPENCODE_SERVER_PASSWORD"] = password
    env.setdefault("OPENCODE_SERVER_USERNAME", "opencode")
    env["OPENCODE_DISABLE_CLAUDE_CODE"] = "1"
    SERVER_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_handle = SERVER_LOG.open("a")
    command = context.execution_argv(
        [
            binary,
            "serve",
            "--hostname",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "WARN",
        ]
    )
    try:
        process = subprocess.Popen(
            command,
            cwd=context.checked_worktree(),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        log_handle.close()
        raise AdapterError(
            "HARNESS_UNAVAILABLE",
            f"could not start restricted opencode serve: {exc}",
            retryable=True,
        ) from exc
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        if _server_healthy(endpoint, password):
            with _SERVER_LOCK:
                _CONTEXT_SERVERS.append((process, log_handle))
            return process, log_handle, endpoint, password
        time.sleep(0.05)
    exit_code = process.poll()
    if exit_code is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    log_handle.close()
    detail = (
        f"exited with code {exit_code}"
        if exit_code is not None
        else f"did not become healthy within {timeout:g}s"
    )
    raise AdapterError(
        "HARNESS_UNAVAILABLE",
        f"restricted opencode serve {detail}",
        retryable=True,
    )


def stop_server() -> None:
    """Stop only the OpenCode server process this module started."""
    global _SERVER_PROCESS, _SERVER_ENDPOINT, _SERVER_PASSWORD
    global _SERVER_LOG_HANDLE
    with _SERVER_LOCK:
        process = _SERVER_PROCESS
        _SERVER_PROCESS = None
        _SERVER_ENDPOINT = SERVER_ENDPOINT
        _SERVER_PASSWORD = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        if process is not None:
            # Our own managed server is gone; drop its re-adoption record.
            # An adopted orphan (never in _SERVER_PROCESS) keeps its record.
            _clear_server_state()
        if _SERVER_LOG_HANDLE is not None:
            _SERVER_LOG_HANDLE.close()
            _SERVER_LOG_HANDLE = None


def stop_context_servers() -> None:
    """Stop restricted servers owned by this broker process."""
    with _SERVER_LOCK:
        servers = list(_CONTEXT_SERVERS)
        _CONTEXT_SERVERS.clear()
    for process, log_handle in servers:
        _stop_context_server(process, log_handle)


def _stop_context_server(process: subprocess.Popen, log_handle: Any) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    log_handle.close()


def provider_state() -> dict[str, Any]:
    """Return OpenCode's authoritative provider/model connection projection."""
    endpoint, password = ensure_server()
    transport = UrlHttpTransport(
        endpoint,
        password=password,
        timeout=10.0,
    )
    result = transport.request("GET", "/provider")
    if not isinstance(result, dict):
        raise AdapterError(
            "HARNESS_PROTOCOL_ERROR",
            "OpenCode provider response was not an object",
        )
    health = transport.request("GET", "/global/health")
    version = health.get("version") if isinstance(health, dict) else None
    return {**result, "_sc_cli_version": version}


def _native_projection_error(detail: str) -> AdapterError:
    return AdapterError(
        "HARNESS_PROTOCOL_ERROR",
        f"OpenCode provider projection is malformed: {detail}",
    )


def _exact_native_identifier(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_NATIVE_IDENTIFIER_CHARS
    ):
        raise _native_projection_error(f"invalid {field}")
    return value


def native_variant_ids(variants: Any) -> list[str]:
    """Project exact variant keys without interpreting provider payloads."""
    if variants is None:
        return []
    if not isinstance(variants, Mapping):
        raise _native_projection_error("variants must be an object")
    if len(variants) > MAX_NATIVE_OPTIONS:
        raise _native_projection_error("too many native options")
    return [
        _exact_native_identifier(option_id, "native option id")
        for option_id in variants
    ]


def connected_models(state: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Flatten models belonging to providers OpenCode reports as connected."""
    state = provider_state() if state is None else state
    if not isinstance(state, Mapping):
        raise _native_projection_error("response must be an object")
    connected_rows = state.get("connected")
    provider_rows = state.get("all")
    if not isinstance(connected_rows, list) or not isinstance(provider_rows, list):
        raise _native_projection_error("connected and all must be arrays")
    if (
        len(connected_rows) > MAX_CONNECTED_PROVIDERS
        or len(provider_rows) > MAX_CONNECTED_PROVIDERS
    ):
        raise _native_projection_error("too many providers")
    connected_list = [
        _exact_native_identifier(item, "connected provider id")
        for item in connected_rows
    ]
    if len(set(connected_list)) != len(connected_list):
        raise _native_projection_error("duplicate connected provider id")
    connected = set(connected_list)
    models: list[dict[str, Any]] = []
    seen_providers: set[str] = set()
    seen_routes: set[str] = set()
    model_rows_seen = 0
    for provider in provider_rows:
        if not isinstance(provider, Mapping):
            raise _native_projection_error("provider row must be an object")
        provider_id = _exact_native_identifier(provider.get("id"), "provider id")
        if provider_id not in connected:
            continue
        if provider_id in seen_providers:
            raise _native_projection_error("duplicate connected provider row")
        seen_providers.add(provider_id)
        provider_models = provider.get("models")
        if not isinstance(provider_models, Mapping):
            raise _native_projection_error("provider models must be an object")
        model_rows_seen += len(provider_models)
        if model_rows_seen > MAX_CONNECTED_MODELS:
            raise _native_projection_error("too many connected models")
        for model_id, model in provider_models.items():
            model_id = _exact_native_identifier(model_id, "model id")
            if not isinstance(model, Mapping):
                raise _native_projection_error("model row must be an object")
            status = model.get("status")
            if status not in (None, "active"):
                if not isinstance(status, str):
                    raise _native_projection_error("model status must be a string")
                continue
            native_option_ids = native_variant_ids(model.get("variants"))
            selector = f"{provider_id}/{model_id}"
            if selector in seen_routes:
                raise _native_projection_error("duplicate exact model route")
            seen_routes.add(selector)
            models.append(
                {
                    "id": selector,
                    "provider": provider_id,
                    "provider_model": model_id,
                    "name": (
                        model.get("name")
                        if isinstance(model.get("name"), str)
                        else model_id
                    ),
                    "family": (
                        model.get("family")
                        if isinstance(model.get("family"), str)
                        else None
                    ),
                    "release_date": (
                        model.get("release_date")
                        if isinstance(model.get("release_date"), str)
                        else ""
                    ),
                    "status": status or "active",
                    "native_option_ids": native_option_ids,
                    "native_default_option_id": None,
                    "supported_efforts": native_option_ids,
                    "default_effort": None,
                    "native_variant_ids": {
                        variant_id: variant_id for variant_id in native_option_ids
                    },
                    "selector_binding": {
                        "kind": "harness-live",
                        "selector": selector,
                    },
                    "adapter_metadata": {},
                    "cli_version": state.get("_sc_cli_version"),
                }
            )
    return models


atexit.register(stop_server)
atexit.register(stop_context_servers)


class OpenCodeAdapter(ConversationAdapter):
    harness = "opencode"

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        password: str | None = None,
        transport: HttpTransport | None = None,
        manifest: Mapping[str, Any] | None = None,
        shell_runtime_dir: Path = SHELL_RUNTIME_DIR,
    ) -> None:
        super().__init__(manifest or load_manifest(self.harness))
        self.shell_runtime_dir = shell_runtime_dir
        if transport is not None:
            self.transport = transport
        elif endpoint is not None:
            self.transport = UrlHttpTransport(
                endpoint,
                password=password,
                timeout=TURN_TIMEOUT_SECONDS,
            )
        else:
            # A conversation's canonical context decides whether server launch
            # is restricted.  Starting the global server here would happen
            # before that policy exists and create an unwrapped side channel.
            self.transport = None
        self._context_server: subprocess.Popen | None = None
        self._context_log = None
        self._context_prefix: tuple[str, ...] = ()

    def _ensure_global_transport(self) -> None:
        if self.transport is not None:
            return
        endpoint, password = ensure_server()
        self.transport = UrlHttpTransport(
            endpoint,
            password=password,
            timeout=TURN_TIMEOUT_SECONDS,
        )

    def _ensure_context_transport(self, context: ConversationContext) -> None:
        prefix = tuple(context.execution_prefix)
        if self._context_server is not None:
            if prefix != self._context_prefix:
                raise AdapterError(
                    "RESTRICTED_SHELL_VIEW_MISMATCH",
                    "OpenCode adapter cannot reuse a different shell execution view",
                )
            if self._context_server.poll() is None:
                return
            raise AdapterError(
                "HARNESS_UNAVAILABLE",
                "restricted OpenCode server exited",
                retryable=True,
            )
        if not prefix:
            self._ensure_global_transport()
            return
        process, log_handle, endpoint, password = start_context_server(context)
        self._context_server = process
        self._context_log = log_handle
        self._context_prefix = prefix
        self.transport = UrlHttpTransport(
            endpoint,
            password=password,
            timeout=TURN_TIMEOUT_SECONDS,
        )

    def probe(self) -> ProbeResult:
        launch = self.manifest["launch"][0]
        return self._probe_result(command_version([launch, "--version"]))

    def close(self) -> None:
        process = self._context_server
        log_handle = self._context_log
        self._context_server = None
        self._context_log = None
        self._context_prefix = ()
        if process is None or log_handle is None:
            return
        with _SERVER_LOCK:
            _CONTEXT_SERVERS[:] = [
                owned
                for owned in _CONTEXT_SERVERS
                if owned[0] is not process
            ]
        _stop_context_server(process, log_handle)

    @staticmethod
    def _bound_model(context: ConversationContext) -> str | None:
        binding = context.route_binding
        if isinstance(binding, Mapping):
            model = binding.get("requested_model")
            return model if isinstance(model, str) else None
        return context.model

    @classmethod
    def _route(cls, context: ConversationContext) -> tuple[str, str] | None:
        selected_model = cls._bound_model(context)
        if not selected_model:
            return None
        if isinstance(context.route_binding, Mapping):
            if "/" in selected_model:
                provider, model = selected_model.split("/", 1)
                if provider and model:
                    return provider, model
            raise AdapterError(
                "HARNESS_MODEL_ROUTE_INVALID",
                "Bound OpenCode models require an exact provider/model selector",
            )
        if context.provider:
            prefix = f"{context.provider}/"
            model = (
                selected_model.removeprefix(prefix)
                if selected_model.startswith(prefix)
                else selected_model
            )
            if model:
                return context.provider, model
        if "/" in selected_model:
            provider, model = selected_model.split("/", 1)
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
        try:
            route_transport.context_projection(context, self.harness)
        except (
            route_transport.route_bindings.RouteResolutionError,
            opencode_config.OpenCodeConfigError,
        ) as exc:
            raise AdapterError(
                getattr(exc, "code", "HARNESS_ROUTE_INVALID"), str(exc)
            ) from exc
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

        try:
            opencode_config.merge_json(
                worktree,
                {"shell": str(wrapper)},
                operation="set-shell-wrapper",
            )
        except opencode_config.OpenCodeConfigError as exc:
            raise AdapterError(
                exc.code, str(exc)
            ) from exc
        return wrapper

    def _prepare_live_route(self, context: ConversationContext) -> None:
        binding = context.route_binding
        if (
            not isinstance(binding, Mapping)
            or binding.get("harness") != self.harness
            or binding.get("control_state") != "controlled"
        ):
            return
        try:
            selection = route_transport.route_bindings.live_native_selection(
                dict(binding)
            )
            state = self.transport.request("GET", "/provider")
            models = connected_models(state)
            current = next(
                (
                    model
                    for model in models
                    if model.get("id") == selection["model_id"]
                ),
                None,
            )
            advertised = {
                selection["model_id"]: (
                    list(current.get("native_option_ids") or [])
                    if isinstance(current, Mapping)
                    else []
                )
            } if current is not None else {}
            route_transport.route_bindings.require_advertised_live_native(
                dict(binding), advertised
            )
            if (
                binding.get("contract_version")
                == route_transport.route_bindings.V2_CONTRACT_VERSION
            ):
                opencode_config.ensure_route_agent(
                    context.checked_worktree(),
                    binding,
                    context.binding_digest or "",
                )
        except route_transport.route_bindings.RouteResolutionError as exc:
            raise AdapterError(exc.code, exc.message) from exc
        except opencode_config.OpenCodeConfigError as exc:
            raise AdapterError(exc.code, str(exc)) from exc

    def _prompt(
        self,
        session_ref: str,
        context: ConversationContext,
        message: str,
    ) -> Any:
        body: dict[str, Any] = {
            "parts": [{"type": "text", "text": message}],
        }
        route = self._route(context)
        if route:
            body["model"] = {
                "providerID": route[0],
                "modelID": route[1],
            }
        if (
            isinstance(context.route_binding, Mapping)
            and context.route_binding.get("control_state") == "controlled"
        ):
            if (
                context.route_binding.get("contract_version")
                == route_transport.route_bindings.LIVE_NATIVE_CONTRACT_VERSION
            ):
                option_id = context.route_binding.get("native_option_id")
                if option_id is not None:
                    body["variant"] = option_id
            else:
                body["agent"] = opencode_config.route_agent_name(
                    context.binding_digest or ""
                )
        return self.transport.request(
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
            # The OpenCode server is shared across shells and turns, so it is
            # not a turn-owned process the active-chat reaper may terminate.
            process_ref=None,
            metadata={
                "context": context,
                "event_stream": event_stream,
                "message": message,
                "dispatch_pending": True,
                "interrupt_lock": threading.Lock(),
                "interrupt_done": threading.Event(),
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
        self._ensure_context_transport(context)
        worktree = context.checked_worktree()
        message = ensure_nonempty_message(message)
        self._prepare_shell_environment(context)
        self._prepare_live_route(context)
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
        self._ensure_context_transport(context)
        message = ensure_nonempty_message(message)
        self._prepare_shell_environment(context)
        if (
            isinstance(context.route_binding, Mapping)
            and context.route_binding.get("contract_version")
            == route_transport.route_bindings.LIVE_NATIVE_CONTRACT_VERSION
        ):
            self._prepare_live_route(context)
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
                if isinstance(value, str):
                    return value
            part = props.get("part")
            if isinstance(part, dict):
                value = part.get("sessionID") or part.get("sessionId")
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
        projection: _OpenCodeProjection | None = None,
    ) -> list[NormalizedEvent]:
        return (projection or _OpenCodeProjection()).normalize(raw)

    @staticmethod
    def _close_stream(native_stream: Any) -> None:
        close = getattr(native_stream, "close", None)
        if not callable(close):
            return
        try:
            close()
        except (OSError, RuntimeError, ValueError):
            pass

    def _abort_once(
        self,
        turn: NativeTurn,
        *,
        operator: bool = False,
    ) -> InterruptResult:
        with self._interrupt_lock(turn):
            if operator:
                turn.metadata["interrupt_requested"] = True
            if turn.metadata.get("abort_sent"):
                return InterruptResult(
                    bool(turn.metadata.get("interrupt_acknowledged"))
                )
            turn.metadata["abort_sent"] = True
            pre_dispatch = bool(turn.metadata.get("dispatch_pending"))
        try:
            result = self.transport.request(
                "POST",
                f"/session/{turn.session_ref}/abort",
                query=self._query(turn.worktree),
            )
            acknowledged = bool(result) or pre_dispatch
            with self._interrupt_lock(turn):
                if operator:
                    turn.metadata["interrupt_acknowledged"] = acknowledged
        finally:
            done = turn.metadata.get("interrupt_done")
            if isinstance(done, threading.Event):
                done.set()
        return InterruptResult(acknowledged)

    def _join_turn_workers(
        self,
        turn: NativeTurn,
        native_stream: Any,
        workers: tuple[threading.Thread, threading.Thread],
        stop: threading.Event,
        *,
        abort_prompt: bool,
    ) -> None:
        stop.set()
        if abort_prompt:
            try:
                self._abort_once(turn)
            except AdapterError:
                pass
        self._close_stream(native_stream)
        deadline = time.monotonic() + TURN_CLEANUP_TIMEOUT_SECONDS
        for worker in workers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            worker.join(remaining)
        live = [worker.name for worker in workers if worker.is_alive()]
        turn.metadata.pop("workers", None)
        if live:
            raise AdapterError(
                "HARNESS_CLEANUP_FAILED",
                "OpenCode turn workers did not stop: " + ", ".join(live),
            )

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
                turn.metadata["interrupt_evidence"] = "operator"
                interrupted_before_dispatch = True
            else:
                turn.metadata["dispatch_pending"] = False
        if interrupted_before_dispatch:
            self._close_stream(native_stream)
            yield NormalizedEvent(
                "run.interrupted",
                {"status": "cancelled"},
                "operator.interrupt",
                "operator",
            )
            return

        projection = _OpenCodeProjection()
        coordination: queue.Queue[tuple[str, Any]] = queue.Queue()
        stop = threading.Event()
        sse_entered = threading.Event()
        prompt_allowed = threading.Event()
        prompt_state: dict[str, Any] = {}

        def consume_sse() -> None:
            sse_entered.set()
            try:
                for raw in native_stream:
                    if stop.is_set():
                        break
                    coordination.put(("sse.raw", raw))
            except BaseException as exc:  # noqa: BLE001 - cross-thread handoff
                if not stop.is_set():
                    coordination.put(("sse.error", exc))
            finally:
                coordination.put(("sse.end", None))

        def dispatch_prompt() -> None:
            prompt_allowed.wait()
            if stop.is_set():
                return
            try:
                prompt_state["response"] = self._prompt(
                    turn.session_ref,
                    context,
                    message,
                )
                coordination.put(("prompt.done", None))
            except BaseException as exc:  # noqa: BLE001 - cross-thread handoff
                prompt_state["error"] = exc
                coordination.put(("prompt.error", exc))

        sse_worker = threading.Thread(
            target=consume_sse,
            name=f"opencode-turn-{turn.run_ref}-sse",
        )
        prompt_worker = threading.Thread(
            target=dispatch_prompt,
            name=f"opencode-turn-{turn.run_ref}-prompt",
        )
        workers = (sse_worker, prompt_worker)
        turn.metadata["workers"] = workers
        sse_worker.start()
        if not sse_entered.wait(1):
            stop.set()
            self._close_stream(native_stream)
            sse_worker.join(TURN_CLEANUP_TIMEOUT_SECONDS)
            turn.metadata.pop("workers", None)
            raise AdapterError(
                "HARNESS_CLEANUP_FAILED",
                "OpenCode SSE consumer did not enter its read loop",
            )
        prompt_worker.start()
        prompt_allowed.set()

        observed_activity = False
        prompt_done = False
        terminal: NormalizedEvent | None = None
        failure: BaseException | None = None
        sse_ended = False
        try:
            while terminal is None and failure is None:
                kind, value = coordination.get()
                if kind == "prompt.done":
                    prompt_done = True
                    continue
                if kind == "prompt.error":
                    failure = value
                    continue
                if kind == "sse.error":
                    failure = value
                    continue
                if kind == "sse.end":
                    sse_ended = True
                    if terminal is None:
                        if not observed_activity:
                            prompt_worker.join(
                                min(0.2, TURN_CLEANUP_TIMEOUT_SECONDS)
                            )
                        if prompt_state.get("error") is not None:
                            failure = prompt_state["error"]
                        elif not observed_activity and "response" in prompt_state:
                            failure = AdapterError(
                                "HARNESS_SUBMISSION_UNOBSERVED",
                                "OpenCode accepted the synchronous prompt request "
                                "but reported no activity or terminal event for "
                                f"{turn.session_ref}",
                            )
                        else:
                            failure = AdapterError(
                                "HARNESS_STREAM_LOST",
                                "OpenCode event stream ended without a terminal event",
                                retryable=True,
                            )
                    continue
                if kind != "sse.raw" or not isinstance(value, Mapping):
                    continue
                event_session = self._session_of(value)
                if event_session and event_session != turn.session_ref:
                    continue
                for event in self._normalize(value, projection):
                    if event.type == "run.completed" and not observed_activity:
                        continue
                    if event.type == "run.completed":
                        done = turn.metadata.get("interrupt_done")
                        with self._interrupt_lock(turn):
                            interrupt_pending = bool(
                                turn.metadata.get("interrupt_requested")
                                and not turn.metadata.get(
                                    "interrupt_acknowledged"
                                )
                            )
                        if interrupt_pending and isinstance(
                            done, threading.Event
                        ):
                            done.wait(TURN_CLEANUP_TIMEOUT_SECONDS)
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
                        if (
                            prompt_state.get("error") is None
                            and prompt_worker.is_alive()
                        ):
                            prompt_worker.join(
                                min(0.2, TURN_CLEANUP_TIMEOUT_SECONDS)
                            )
                        if prompt_state.get("error") is not None:
                            failure = prompt_state["error"]
                        else:
                            terminal = event
                        break
                    yield event
        finally:
            if terminal is not None and prompt_worker.is_alive():
                prompt_worker.join(min(0.2, TURN_CLEANUP_TIMEOUT_SECONDS))
            self._join_turn_workers(
                turn,
                native_stream,
                workers,
                stop,
                abort_prompt=(
                    prompt_worker.is_alive()
                    or (
                        failure is not None
                        and not (
                            isinstance(failure, AdapterError)
                            and isinstance(
                                failure.__cause__, urllib.error.HTTPError
                            )
                        )
                    )
                    or (failure is None and terminal is None)
                ),
            )

        if failure is not None:
            if isinstance(failure, AdapterError):
                if isinstance(failure.__cause__, urllib.error.HTTPError):
                    raise AdapterError(
                        "HARNESS_SUBMISSION_FAILED",
                        failure.detail,
                        retryable=failure.retryable,
                    ) from failure
                raise failure
            raise failure
        if terminal is None:
            if sse_ended and not observed_activity and prompt_done:
                raise AdapterError(
                    "HARNESS_SUBMISSION_UNOBSERVED",
                    "OpenCode accepted the synchronous prompt request but "
                    f"reported no activity or terminal event for {turn.session_ref}",
                )
            raise AdapterError(
                "HARNESS_STREAM_LOST",
                "OpenCode turn ended without a terminal event",
                retryable=True,
            )
        for event in projection.reconcile_response(prompt_state.get("response")):
            yield event
        turn.metadata["terminal"] = terminal.type
        if terminal.interrupt_evidence:
            turn.metadata["interrupt_evidence"] = terminal.interrupt_evidence
        yield terminal

    def interrupt(self, turn: NativeTurn) -> InterruptResult:
        return self._abort_once(turn, operator=True)

    def inspect(
        self,
        session_ref: str,
        context: ConversationContext,
    ) -> SessionInspection:
        self._ensure_context_transport(context)
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
            outcome = terminal_outcome(terminal)
            return ReconcileResult(
                outcome,
                True,
                f"terminal {terminal} was observed on the native stream",
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
