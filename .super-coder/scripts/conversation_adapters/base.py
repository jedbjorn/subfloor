#!/usr/bin/env python3
"""Harness-neutral browser conversation adapter contract.

Adapters translate native session and run protocols into a deliberately small
event vocabulary. They never own durable queue state; the conversation broker
persists the opaque references returned here.
"""
from __future__ import annotations

import abc
import base64
import json
import os
import re
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Protocol


SCRIPTS = Path(__file__).resolve().parents[1]
ENGINE = SCRIPTS.parent
ADAPTERS = ENGINE / "adapters"

NORMALIZED_EVENTS = frozenset(
    {
        "session.started",
        "run.started",
        "assistant.delta",
        "tool.started",
        "tool.completed",
        "permission.requested",
        "input.requested",
        "usage",
        "run.completed",
        "run.failed",
        "run.interrupted",
    }
)
TERMINAL_EVENTS = frozenset(
    {"run.completed", "run.failed", "run.interrupted"}
)
RECONCILE_OUTCOMES = frozenset(
    {"running", "succeeded", "failed", "cancelled", "unknown"}
)
PERMISSION_MODES = frozenset({"unrestricted", "interactive"})


class AdapterError(RuntimeError):
    """Stable adapter error exposed to the broker."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.detail = detail
        self.retryable = retryable
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class AdapterCapabilities:
    exact_session_resume: bool
    structured_streaming: bool
    interruption: bool
    interactive_permission_response: bool
    server_backed: bool
    session_inspection: bool

    @classmethod
    def from_manifest(cls, manifest: Mapping[str, Any]) -> "AdapterCapabilities":
        raw = manifest["conversation"]["capabilities"]
        return cls(**{name: bool(raw[name]) for name in cls.__annotations__})


@dataclass(frozen=True)
class ProbeResult:
    harness: str
    version: str
    minimum_version: str
    capabilities: AdapterCapabilities


@dataclass(frozen=True)
class ConversationContext:
    """Resolved route and immutable work surface for one conversation."""

    worktree: Path
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    permission_mode: str = "unrestricted"
    title: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)

    def checked_worktree(self) -> Path:
        if self.permission_mode not in PERMISSION_MODES:
            raise AdapterError(
                "HARNESS_PERMISSION_POLICY_INVALID",
                f"unsupported permission mode: {self.permission_mode}",
            )
        if not self.worktree.is_absolute():
            raise AdapterError(
                "HARNESS_WORKTREE_MISMATCH",
                f"worktree must be absolute: {self.worktree}",
            )
        try:
            resolved = self.worktree.resolve(strict=True)
        except OSError as exc:
            raise AdapterError(
                "HARNESS_WORKTREE_MISSING",
                f"worktree is unavailable: {self.worktree}",
            ) from exc
        if not resolved.is_dir():
            raise AdapterError(
                "HARNESS_WORKTREE_MISSING",
                f"worktree is not a directory: {resolved}",
            )
        return resolved


@dataclass
class NativeTurn:
    harness: str
    session_ref: str
    run_ref: str
    worktree: Path
    process_ref: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    opaque: Any = field(default=None, repr=False)


@dataclass(frozen=True)
class NormalizedEvent:
    type: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    native_type: str | None = None

    def __post_init__(self) -> None:
        if self.type not in NORMALIZED_EVENTS:
            raise ValueError(f"unknown normalized conversation event: {self.type}")


@dataclass(frozen=True)
class InterruptResult:
    acknowledged: bool
    detail: str | None = None


@dataclass(frozen=True)
class SessionInspection:
    session_ref: str
    exists: bool
    state: str
    worktree: Path | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReconcileResult:
    outcome: str
    proven: bool
    detail: str

    def __post_init__(self) -> None:
        if self.outcome not in RECONCILE_OUTCOMES:
            raise ValueError(f"unknown reconciliation outcome: {self.outcome}")
        if self.outcome == "unknown" and self.proven:
            raise ValueError("an unknown adapter outcome cannot be proven")


class ConversationAdapter(abc.ABC):
    """The only harness surface the broker consumes."""

    harness: str

    def __init__(self, manifest: Mapping[str, Any]) -> None:
        self.manifest = dict(manifest)
        self.capabilities = AdapterCapabilities.from_manifest(manifest)

    @abc.abstractmethod
    def probe(self) -> ProbeResult:
        raise NotImplementedError

    @abc.abstractmethod
    def start(self, context: ConversationContext, message: str) -> NativeTurn:
        raise NotImplementedError

    @abc.abstractmethod
    def resume(
        self,
        session_ref: str,
        context: ConversationContext,
        message: str,
    ) -> NativeTurn:
        raise NotImplementedError

    @abc.abstractmethod
    def stream(self, turn: NativeTurn) -> Iterator[NormalizedEvent]:
        raise NotImplementedError

    @abc.abstractmethod
    def interrupt(self, turn: NativeTurn) -> InterruptResult:
        raise NotImplementedError

    @abc.abstractmethod
    def inspect(
        self,
        session_ref: str,
        context: ConversationContext,
    ) -> SessionInspection:
        raise NotImplementedError

    @abc.abstractmethod
    def reconcile(
        self,
        turn: NativeTurn,
        context: ConversationContext,
    ) -> ReconcileResult:
        raise NotImplementedError

    def _probe_result(self, version: str) -> ProbeResult:
        minimum = self.manifest["conversation"]["minimum_cli_version"]
        if version_tuple(version) < version_tuple(minimum):
            raise AdapterError(
                "HARNESS_VERSION_UNSUPPORTED",
                f"{self.harness} {version} is older than required {minimum}",
            )
        return ProbeResult(
            harness=self.harness,
            version=version,
            minimum_version=minimum,
            capabilities=self.capabilities,
        )


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any: ...

    def stream(
        self,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
    ) -> Iterable[Mapping[str, Any]]: ...


class UrlHttpTransport:
    """Small JSON + SSE transport for the loopback OpenCode server."""

    def __init__(
        self,
        endpoint: str,
        *,
        username: str = "opencode",
        password: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.headers = {"Accept": "application/json"}
        if password is not None:
            token = base64.b64encode(
                f"{username}:{password}".encode()
            ).decode()
            self.headers["Authorization"] = f"Basic {token}"

    def _url(
        self,
        path: str,
        query: Mapping[str, str] | None,
    ) -> str:
        suffix = urllib.parse.urlencode(query or {})
        return f"{self.endpoint}{path}" + (f"?{suffix}" if suffix else "")

    def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        data = None if body is None else json.dumps(body).encode()
        headers = dict(self.headers)
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self._url(path, query),
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            code = (
                "HARNESS_SESSION_LOST"
                if exc.code == 404
                else "HARNESS_PROTOCOL_ERROR"
            )
            raise AdapterError(
                code,
                f"{method} {path} returned HTTP {exc.code}",
                retryable=exc.code >= 500,
            ) from exc
        except OSError as exc:
            raise AdapterError(
                "HARNESS_UNAVAILABLE",
                f"{method} {path} failed: {exc}",
                retryable=True,
            ) from exc
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AdapterError(
                "HARNESS_PROTOCOL_ERROR",
                f"{method} {path} returned invalid JSON",
            ) from exc

    def stream(
        self,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
    ) -> Iterable[Mapping[str, Any]]:
        headers = dict(self.headers)
        headers["Accept"] = "text/event-stream"
        request = urllib.request.Request(
            self._url(path, query),
            headers=headers,
        )
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout)
        except OSError as exc:
            raise AdapterError(
                "HARNESS_UNAVAILABLE",
                f"GET {path} stream failed: {exc}",
                retryable=True,
            ) from exc

        return self._iter_sse(response)

    @staticmethod
    def _iter_sse(response: Any) -> Iterator[Mapping[str, Any]]:
        data: list[str] = []
        try:
            for raw_line in response:
                line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("data:"):
                    data.append(line[5:].lstrip())
                    continue
                if line or not data:
                    continue
                encoded = "\n".join(data)
                data.clear()
                try:
                    value = json.loads(encoded)
                except json.JSONDecodeError as exc:
                    raise AdapterError(
                        "HARNESS_PROTOCOL_ERROR",
                        "SSE event contained invalid JSON",
                    ) from exc
                if isinstance(value, dict):
                    yield value
        finally:
            response.close()


class ProcessRunner(Protocol):
    def spawn(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
    ) -> Any: ...


class SubprocessRunner:
    def spawn(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
    ) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        captured: list[str] = []

        def drain_stderr() -> None:
            stderr = process.stderr
            if stderr is None:
                return
            remaining = 16384
            for line in stderr:
                if remaining <= 0:
                    continue
                chunk = line[:remaining]
                captured.append(chunk)
                remaining -= len(chunk)

        setattr(process, "_sc_conversation_stderr", captured)
        threading.Thread(
            target=drain_stderr,
            name="conversation-stderr-drain",
            daemon=True,
        ).start()
        return process


def load_manifest(harness: str) -> dict[str, Any]:
    path = ADAPTERS / harness / "adapter.json"
    if not path.is_file():
        raise AdapterError(
            "HARNESS_CONVERSATION_UNSUPPORTED",
            f"harness has no adapter manifest: {harness}",
        )
    try:
        manifest = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterError(
            "HARNESS_MANIFEST_INVALID",
            f"cannot read adapter manifest for {harness}",
        ) from exc
    conversation = manifest.get("conversation") or {}
    if conversation.get("contract_version") != 1:
        raise AdapterError(
            "HARNESS_MANIFEST_INVALID",
            f"harness has no supported conversation contract: {harness}",
        )
    declared_events = frozenset(conversation.get("normalized_events") or ())
    if declared_events != NORMALIZED_EVENTS:
        raise AdapterError(
            "HARNESS_MANIFEST_INVALID",
            f"harness normalized event vocabulary is incomplete: {harness}",
        )
    if not conversation.get("capabilities", {}).get("exact_session_resume"):
        raise AdapterError(
            "HARNESS_RESUME_UNSUPPORTED",
            f"harness cannot resume an exact session: {harness}",
        )
    return manifest


def command_version(argv: list[str]) -> str:
    try:
        result = subprocess.run(
            argv,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AdapterError(
            "HARNESS_UNAVAILABLE",
            f"cannot probe {' '.join(argv)}: {exc}",
            retryable=True,
        ) from exc
    match = re.search(r"\d+\.\d+\.\d+", result.stdout + result.stderr)
    if not match:
        raise AdapterError(
            "HARNESS_PROTOCOL_ERROR",
            f"version probe returned no semantic version: {' '.join(argv)}",
        )
    return match.group(0)


def version_tuple(version: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        raise AdapterError(
            "HARNESS_PROTOCOL_ERROR",
            f"invalid harness version: {version}",
        )
    return tuple(int(part) for part in match.groups())


def ensure_nonempty_message(message: str) -> str:
    if not isinstance(message, str) or not message.strip():
        raise AdapterError(
            "HARNESS_MESSAGE_INVALID",
            "conversation message must contain text",
        )
    return message


def ensure_exact_session(expected: str, actual: Any) -> str:
    if not isinstance(actual, str) or not actual:
        raise AdapterError(
            "HARNESS_PROTOCOL_ERROR",
            "harness returned no native session reference",
        )
    if actual != expected:
        raise AdapterError(
            "HARNESS_SESSION_MISMATCH",
            f"requested session {expected}, harness returned {actual}",
        )
    return actual


def merged_env(
    manifest: Mapping[str, Any],
    context: ConversationContext,
) -> dict[str, str]:
    return {
        **os.environ,
        **{str(k): str(v) for k, v in manifest.get("env", {}).items()},
        **{str(k): str(v) for k, v in context.env.items()},
    }


def terminal_outcome(event_type: str) -> str:
    return {
        "run.completed": "succeeded",
        "run.failed": "failed",
        "run.interrupted": "cancelled",
    }[event_type]
