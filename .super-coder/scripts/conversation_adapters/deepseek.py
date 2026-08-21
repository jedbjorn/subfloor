#!/usr/bin/env python3
"""DeepSeek isolated-carrier browser conversation adapter."""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import queue
import re
import signal
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol

import deepseek_runtime
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
    checked_version_compatibility,
    cleanup_owned_process,
    ensure_exact_session,
    ensure_nonempty_message,
    load_manifest,
)

WORKER = Path(__file__).resolve().parents[1] / "deepseek_carrier_worker.py"
SESSION_REF = re.compile(r"^deepseek-[0-9a-f]{32}$")
RUN_REF_PREFIX = "deepseek-run-v1:"
MAX_NATIVE_BYTES = 8192
MAX_UNKNOWN_EVENTS = 8
DEFAULT_STREAM_INACTIVITY_SECONDS = 30.0
DEFAULT_SILENT_PROBE_LIMIT = 2
SENSITIVE_KEY = re.compile(r"(?:key|token|secret|password|credential|authorization)", re.I)
NATIVE_ERROR_CODES = frozenset(
    {
        "ABORTED",
        "AUTH",
        "CONTEXT_WINDOW_EXCEEDED",
        "EMPTY_RESPONSE",
        "INVALID_CREDENTIAL",
        "INVALID_REQUEST",
        "MALFORMED_RESPONSE",
        "MISSING_CREDENTIAL",
        "PI_AI_ERROR",
        "QUOTA",
        "RATE_LIMIT",
        "SERVER",
        "STREAM_CLOSED",
        "TIMEOUT",
        "TRANSPORT",
        "UNSUPPORTED_CONTENT",
    }
)
NATIVE_HTTP_ERROR_CODE = re.compile(r"^HTTP_[1-5][0-9]{2}$")
NATIVE_HTTP_STATUS = re.compile(
    r"(?:\bHTTP(?:\s+status)?[\s:_-]*([1-5][0-9]{2})\b|"
    r"\b([1-5][0-9]{2})\s+status\s+code\b)",
    re.I,
)
_CARRIER_STREAM_END = object()


class DeepSeekTransport(Protocol):
    process: Any

    def request(self, method: str, params: Mapping[str, Any]) -> Any: ...

    def poll_notification(self, timeout: float) -> Mapping[str, Any] | None: ...

    def close(self) -> None: ...


class DeepSeekCarrierProcess:
    """Thread-safe controller for the isolated SDK worker process."""

    def __init__(
        self,
        *,
        argv: list[str],
        cwd: Path,
        env: Mapping[str, str],
        timeout: float = 30.0,
    ) -> None:
        self.timeout = timeout
        self.process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=dict(env),
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
        threading.Thread(
            target=self._drain_stderr,
            name="deepseek-carrier-stderr",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._read_loop,
            name="deepseek-carrier-reader",
            daemon=True,
        ).start()

    def _drain_stderr(self) -> None:
        stderr = self.process.stderr
        if stderr is None:
            return
        remaining = 16384
        for line in stderr:
            if remaining <= 0:
                continue
            chunk = deepseek_runtime.sanitize_diagnostic(line, limit=remaining)
            self.stderr.append(chunk)
            remaining -= len(chunk)

    def _write(self, message: Mapping[str, Any]) -> None:
        stdin = self.process.stdin
        if stdin is None:
            raise AdapterError(
                "HARNESS_UNAVAILABLE",
                "DeepSeek carrier stdin is closed",
                retryable=True,
            )
        encoded = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
        if len(encoded.encode()) > 1024 * 1024:
            raise AdapterError(
                "HARNESS_PROTOCOL_ERROR",
                "DeepSeek carrier request exceeds the 1 MiB bound",
            )
        with self._write_lock:
            try:
                stdin.write(encoded + "\n")
                stdin.flush()
            except OSError as exc:
                raise AdapterError(
                    "HARNESS_UNAVAILABLE",
                    f"DeepSeek carrier write failed: {exc}",
                    retryable=True,
                ) from exc

    def _read_loop(self) -> None:
        stdout = self.process.stdout
        if stdout is None:
            self._notifications.put(_CARRIER_STREAM_END)
            return
        for line in stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self._notifications.put(
                    AdapterError(
                        "HARNESS_PROTOCOL_ERROR",
                        "DeepSeek carrier emitted invalid JSON",
                    )
                )
                continue
            if not isinstance(message, dict):
                continue
            request_id = message.get("id")
            if request_id is not None and "method" not in message:
                with self._pending_lock:
                    waiter = self._pending.get(request_id)
                if waiter is not None:
                    waiter.put(message)
                continue
            self._notifications.put(message)
        error = AdapterError(
            "HARNESS_UNAVAILABLE",
            "DeepSeek carrier stream closed",
            retryable=True,
        )
        with self._pending_lock:
            waiters = list(self._pending.values())
        for waiter in waiters:
            waiter.put(error)
        self._notifications.put(_CARRIER_STREAM_END)

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
                    f"DeepSeek carrier request timed out: {method}",
                    retryable=True,
                ) from exc
            if isinstance(response, AdapterError):
                raise response
            error = response.get("error") if isinstance(response, dict) else None
            if isinstance(error, dict):
                code = error.get("code")
                detail = error.get("detail")
                raise AdapterError(
                    code if isinstance(code, str) else "HARNESS_PROTOCOL_ERROR",
                    deepseek_runtime.sanitize_diagnostic(
                        detail if isinstance(detail, str) else "carrier request failed"
                    ),
                )
            return response.get("result") if isinstance(response, dict) else None
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def poll_notification(self, timeout: float) -> Mapping[str, Any] | None:
        try:
            message = self._notifications.get(timeout=timeout)
        except queue.Empty:
            if self.process.poll() is not None:
                raise AdapterError(
                    "HARNESS_UNAVAILABLE",
                    "DeepSeek carrier exited without closing its event stream",
                    retryable=True,
                )
            return None
        if message is _CARRIER_STREAM_END:
            raise AdapterError(
                "HARNESS_UNAVAILABLE",
                "DeepSeek carrier stream closed",
                retryable=True,
            )
        if isinstance(message, AdapterError):
            raise message
        return message if isinstance(message, dict) else None

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        try:
            self.request("shutdown", {})
        except AdapterError:
            pass
        cleanup_owned_process(self.process, 3.0)


class _ConversationProcessLease:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        self._stream = os.fdopen(descriptor, "r+")
        try:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._stream.close()
            raise AdapterError(
                "HARNESS_PROCESS_ALREADY_RUNNING",
                "another DeepSeek adapter owns this conversation process slot",
            ) from exc

    def close(self) -> None:
        try:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._stream.close()


def _run_ref(message_id: str, from_event_seq: int) -> str:
    value = json.dumps(
        {"message_id": message_id, "from_event_seq": from_event_seq},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return RUN_REF_PREFIX + base64.urlsafe_b64encode(value).decode().rstrip("=")


def _run_boundary(run_ref: str) -> int:
    if not isinstance(run_ref, str) or not run_ref.startswith(RUN_REF_PREFIX):
        raise AdapterError(
            "HARNESS_RUN_IDENTITY_INVALID",
            "DeepSeek run reference is malformed",
        )
    encoded = run_ref[len(RUN_REF_PREFIX):]
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        value = json.loads(raw)
        boundary = value["from_event_seq"]
        message_id = value["message_id"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AdapterError(
            "HARNESS_RUN_IDENTITY_INVALID",
            "DeepSeek run reference cannot be decoded",
        ) from exc
    if (
        not isinstance(boundary, int)
        or isinstance(boundary, bool)
        or boundary < 0
        or not isinstance(message_id, str)
        or not message_id
    ):
        raise AdapterError(
            "HARNESS_RUN_IDENTITY_INVALID",
            "DeepSeek run reference has invalid fields",
        )
    return boundary


def _bounded_native(value: Any) -> Any:
    def redact(item: Any, depth: int = 0) -> Any:
        if depth > 8:
            return "[TRUNCATED]"
        if isinstance(item, dict):
            return {
                str(key)[:128]: (
                    "[REDACTED]"
                    if SENSITIVE_KEY.search(str(key))
                    else redact(child, depth + 1)
                )
                for key, child in list(item.items())[:128]
            }
        if isinstance(item, list):
            return [redact(child, depth + 1) for child in item[:128]]
        if isinstance(item, str):
            return deepseek_runtime.sanitize_diagnostic(item, limit=2048)
        if isinstance(item, (int, float, bool)) or item is None:
            return item
        return str(item)[:256]

    safe = redact(value)
    encoded = json.dumps(safe, separators=(",", ":"), sort_keys=True).encode()
    if len(encoded) <= MAX_NATIVE_BYTES:
        return safe
    return {
        "truncated": True,
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "original_bytes": len(encoded),
    }


def _native_failure_code(reason: Any) -> str:
    error = reason.get("error") if isinstance(reason, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    if isinstance(code, str) and (
        code in NATIVE_ERROR_CODES or NATIVE_HTTP_ERROR_CODE.fullmatch(code)
    ):
        if code == "PI_AI_ERROR":
            message = error.get("message")
            match = (
                NATIVE_HTTP_STATUS.search(message)
                if isinstance(message, str)
                else None
            )
            if match is not None:
                return f"HARNESS_NATIVE_RUN_HTTP_{match.group(1) or match.group(2)}"
        return f"HARNESS_NATIVE_RUN_{code}"
    return "HARNESS_NATIVE_RUN_FAILED"


class DeepSeekAdapter(ConversationAdapter):
    harness = "deepseek"

    def __init__(
        self,
        *,
        manifest: Mapping[str, Any] | None = None,
        runtime_probe: Callable[..., deepseek_runtime.RuntimeStatus] = deepseek_runtime.runtime_status,
        transport_factory: Callable[..., DeepSeekTransport] = DeepSeekCarrierProcess,
        state_root: Path | None = None,
        start_ticks: Callable[..., int] = deepseek_runtime.process_start_ticks,
        record_identity: Callable[..., Mapping[str, Any]] = deepseek_runtime.record_process_identity,
        stream_inactivity_seconds: float = DEFAULT_STREAM_INACTIVITY_SECONDS,
        silent_probe_limit: int = DEFAULT_SILENT_PROBE_LIMIT,
    ) -> None:
        super().__init__(manifest or load_manifest(self.harness))
        self.runtime_probe = runtime_probe
        self.transport_factory = transport_factory
        self.state_root = state_root
        self.start_ticks = start_ticks
        self.record_identity = record_identity
        if stream_inactivity_seconds <= 0:
            raise ValueError("stream_inactivity_seconds must be positive")
        if silent_probe_limit <= 0:
            raise ValueError("silent_probe_limit must be positive")
        self.stream_inactivity_seconds = stream_inactivity_seconds
        self.silent_probe_limit = silent_probe_limit
        self._transport_instance: DeepSeekTransport | None = None
        self._lease: _ConversationProcessLease | None = None
        self._transport_identity: tuple[str, str, str | None] | None = None

    def _runtime_status(
        self, env: Mapping[str, str] | None = None
    ) -> deepseek_runtime.RuntimeStatus:
        try:
            return self.runtime_probe(**({} if env is None else {"env": env}))
        except deepseek_runtime.DeepSeekRuntimeError as exc:
            raise AdapterError(exc.code, exc.detail) from exc

    def probe(self) -> ProbeResult:
        status = self._runtime_status()
        if not status.available or status.runtime_version is None:
            raise AdapterError(
                status.error or "HARNESS_RUNTIME_MISSING",
                status.detail or "isolated DeepSeek carrier is unavailable",
                retryable=status.error == "HARNESS_RUNTIME_MISSING",
            )
        conversation = self.manifest["conversation"]
        checked = checked_version_compatibility(
            harness=self.harness,
            compatibility=conversation,
            version=status.runtime_version,
        )
        return ProbeResult(
            harness=self.harness,
            version=status.runtime_version,
            minimum_version=checked.minimum_version,
            capabilities=self.capabilities,
            maximum_version_exclusive=checked.maximum_version_exclusive,
            verified_version=checked.verified_version,
            compatibility=checked.compatibility,
        )

    @staticmethod
    def _session_ref(value: str) -> str:
        if not isinstance(value, str) or SESSION_REF.fullmatch(value) is None:
            raise AdapterError(
                "HARNESS_SESSION_LOST",
                f"DeepSeek session ref is malformed: {value}",
            )
        return value

    @staticmethod
    def _route(
        context: ConversationContext,
    ) -> tuple[str, str, dict[str, str], str, str]:
        try:
            projection = route_transport.context_projection(context, "deepseek")
        except route_transport.route_bindings.RouteResolutionError as exc:
            raise AdapterError(
                getattr(exc, "code", "HARNESS_ROUTE_INVALID"), str(exc)
            ) from exc
        if projection is None or not projection.model:
            raise AdapterError(
                "HARNESS_ROUTE_UNAVAILABLE",
                "DeepSeek requires one immutable controlled exact route",
            )
        binding = context.route_binding
        if not isinstance(binding, Mapping):
            raise AdapterError(
                "HARNESS_ROUTE_INVALID", "DeepSeek route binding is missing"
            )
        metadata = binding.get("adapter_metadata")
        if not isinstance(metadata, Mapping):
            raise AdapterError(
                "HARNESS_ROUTE_INVALID", "DeepSeek adapter metadata is missing"
            )
        provider = metadata.get("provider_route")
        if provider not in route_transport.route_bindings.DEEPSEEK_PROVIDER_ROUTES:
            raise AdapterError(
                "HARNESS_ROUTE_INVALID", "DeepSeek provider route is not reviewed"
            )
        if context.provider is not None and context.provider != provider:
            raise AdapterError(
                "HARNESS_ROUTE_MISMATCH",
                "stored provider disagrees with the immutable DeepSeek route",
            )
        try:
            manifest = deepseek_runtime.load_runtime_manifest()
            adapter = deepseek_runtime.provider_adapter(provider)
        except deepseek_runtime.DeepSeekRuntimeError as exc:
            raise AdapterError(exc.code, exc.detail) from exc
        evidence = {
            "provider_adapter_id": adapter["adapter_id"],
            "provider_adapter_digest": route_transport.route_bindings.digest_json(adapter),
            "provider_registry_sha256": manifest["provider_adapters"]["sha256"],
            "credential_kind": adapter["credential_kind"],
            "runtime_version": manifest["runtime"]["version"],
            "source_commit": manifest["source"]["commit"],
            "patch_sha256": manifest["patch"]["sha256"],
            "composition_sha256": adapter["composition_sha256"],
        }
        for field, expected in evidence.items():
            if metadata.get(field) != expected:
                raise AdapterError(
                    "HARNESS_PROVIDER_ADAPTER_DRIFT",
                    f"DeepSeek immutable {field} changed after route binding",
                )
        endpoint = metadata.get("endpoint_identity")
        if not isinstance(endpoint, str) or not endpoint:
            raise AdapterError(
                "HARNESS_PROVIDER_CONFIG_INVALID",
                "DeepSeek route has no exact credential-free endpoint identity",
            )
        if adapter["endpoint_env"] is None and endpoint != adapter["endpoint_default"]:
            raise AdapterError(
                "HARNESS_PROVIDER_CONFIG_INVALID",
                "DeepSeek provider endpoint changed after route binding",
            )
        provider_model = binding.get("provider_model")
        if not isinstance(provider_model, str) or not provider_model:
            raise AdapterError(
                "HARNESS_ROUTE_INVALID", "DeepSeek provider model is missing"
            )
        raw = metadata.get("provider_options")
        if not isinstance(raw, Mapping):
            raise AdapterError(
                "HARNESS_PROVIDER_OPTION_INVALID",
                "DeepSeek provider options are missing",
            )
        omitted = raw.get("omit")
        selected = raw.get("set")
        if not isinstance(omitted, list) or not isinstance(selected, Mapping):
            raise AdapterError(
                "HARNESS_PROVIDER_OPTION_INVALID",
                "DeepSeek provider options are malformed",
            )
        thinking: str | None = None
        reasoning: str | None = None
        if "thinking" in omitted:
            thinking = "omit"
        elif isinstance(selected.get("thinking"), Mapping):
            candidate = selected["thinking"].get("type")
            if isinstance(candidate, str):
                thinking = candidate
        if "reasoning_effort" in omitted:
            reasoning = "omit"
        elif isinstance(selected.get("reasoning_effort"), str):
            reasoning = selected["reasoning_effort"]
        if thinking is None or reasoning is None:
            raise AdapterError(
                "HARNESS_PROVIDER_OPTION_INVALID",
                "DeepSeek binding does not resolve both outbound reasoning fields",
            )
        try:
            options = deepseek_runtime.provider_request_options(
                thinking=thinking, reasoning_effort=reasoning
            )
        except deepseek_runtime.DeepSeekRuntimeError as exc:
            raise AdapterError(exc.code, exc.detail) from exc
        return (
            provider,
            provider_model,
            options,
            str(adapter["credential_source_env"]),
            endpoint,
        )

    @staticmethod
    def _conversation_id(context: ConversationContext) -> str:
        value = context.conversation_id
        if not isinstance(value, str) or not value.strip():
            raise AdapterError(
                "HARNESS_SESSION_ID_INVALID",
                "DeepSeek adapter requires the stable conversation identity",
            )
        return value

    def _assert_no_live_process(
        self, layout: deepseek_runtime.ConversationLayout
    ) -> None:
        if not layout.process_identity.is_file():
            return
        try:
            raw = layout.process_identity.read_text()
            if len(raw.encode()) > 16384:
                raise ValueError("process identity exceeds 16 KiB")
            evidence = json.loads(raw)
            pid = evidence["pid"]
            ticks = evidence["start_ticks"]
            argv_digest = evidence["argv_sha256"]
            if (
                not isinstance(pid, int)
                or isinstance(pid, bool)
                or pid <= 0
                or not isinstance(ticks, int)
                or isinstance(ticks, bool)
                or ticks <= 0
                or not isinstance(argv_digest, str)
                or len(argv_digest) != 64
            ):
                raise ValueError("process identity fields are invalid")
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AdapterError(
                "HARNESS_PROCESS_IDENTITY_INVALID",
                "stored DeepSeek process identity is malformed",
            ) from exc
        try:
            observed = self.start_ticks(pid)
        except deepseek_runtime.DeepSeekRuntimeError as exc:
            if exc.code == "HARNESS_PROCESS_IDENTITY_MISSING":
                return
            raise AdapterError(exc.code, exc.detail) from exc
        if observed == ticks:
            raise AdapterError(
                "HARNESS_PROCESS_ALREADY_RUNNING",
                "the conversation's prior DeepSeek process is still alive",
            )

    def _transport(
        self,
        context: ConversationContext,
        *,
        dispatch: bool,
    ) -> tuple[DeepSeekTransport, deepseek_runtime.ConversationLayout]:
        conversation_id = self._conversation_id(context)
        provider, model, options, credential_env, endpoint = self._route(context)
        identity = (conversation_id, model, context.binding_digest)
        if self._transport_instance is not None:
            if self._transport_identity != identity:
                raise AdapterError(
                    "HARNESS_PROCESS_IDENTITY_MISMATCH",
                    "DeepSeek adapter transport belongs to another conversation or route",
                )
            layout = deepseek_runtime.conversation_layout(
                conversation_id, state_root=self.state_root
            )
            return self._transport_instance, layout

        status = self._runtime_status(context.env)
        if not status.available or not status.carrier_python:
            raise AdapterError(
                status.error or "HARNESS_RUNTIME_MISSING",
                status.detail or "isolated DeepSeek carrier is unavailable",
            )
        layout = deepseek_runtime.conversation_layout(
            conversation_id, state_root=self.state_root
        )
        deepseek_runtime.provision_conversation(layout)
        lease = _ConversationProcessLease(layout.adapter_lock)
        transport: DeepSeekTransport | None = None
        try:
            self._assert_no_live_process(layout)
            boot_content = context.boot_content
            if dispatch and (not isinstance(boot_content, str) or not boot_content):
                raise AdapterError(
                    "HARNESS_BOOT_SNAPSHOT_MISSING",
                    "DeepSeek dispatch requires the committed boot-document bytes",
                )
            if not boot_content:
                boot_content = "super-coder bounded native-session recovery inspection"
            base_env = dict(context.env)
            api_key = base_env.get(credential_env, "")
            try:
                child_env = deepseek_runtime.launch_environment(
                    layout,
                    worktree=context.checked_worktree(),
                    system_prompt=boot_content,
                    provider=provider,
                    api_key=api_key,
                    base_url=endpoint,
                    base_env=base_env,
                )
            except deepseek_runtime.DeepSeekRuntimeError as exc:
                raise AdapterError(exc.code, exc.detail) from exc
            child_env.update(
                {
                    "SC_DEEPSEEK_PROVIDER": provider,
                    "SC_DEEPSEEK_MODEL": model,
                    "SC_DEEPSEEK_PROVIDER_OPTIONS": json.dumps(
                        options, separators=(",", ":"), sort_keys=True
                    ),
                    "SC_DEEPSEEK_PROVIDER_THINKING": options["thinking"],
                    "SC_DEEPSEEK_PROVIDER_REASONING_EFFORT": options["reasoningEffort"],
                }
            )
            argv = [status.carrier_python, "-I", str(WORKER)]
            transport = self.transport_factory(
                argv=argv,
                cwd=context.checked_worktree(),
                env=child_env,
            )
            pid = getattr(getattr(transport, "process", None), "pid", None)
            if not isinstance(pid, int) or pid <= 0:
                transport.close()
                raise AdapterError(
                    "HARNESS_PROCESS_IDENTITY_INVALID",
                    "DeepSeek carrier returned no process identity",
                )
            try:
                ticks = self.start_ticks(pid)
                self.record_identity(
                    layout, pid=pid, start_ticks=ticks, argv=argv
                )
            except deepseek_runtime.DeepSeekRuntimeError as exc:
                transport.close()
                raise AdapterError(exc.code, exc.detail) from exc
        except Exception:
            if transport is not None:
                transport.close()
            lease.close()
            raise
        assert transport is not None
        self._transport_instance = transport
        self._transport_identity = identity
        self._lease = lease
        return transport, layout

    @staticmethod
    def _identity_value(
        context: ConversationContext,
        session_ref: str,
    ) -> dict[str, Any]:
        boot_content = context.boot_content
        if not isinstance(boot_content, str) or not boot_content:
            raise AdapterError(
                "HARNESS_BOOT_SNAPSHOT_MISSING",
                "DeepSeek dispatch requires the committed boot-document bytes",
            )
        return {
            "schema_version": 1,
            "conversation_id": DeepSeekAdapter._conversation_id(context),
            "session_ref": session_ref,
            "worktree": str(context.checked_worktree()),
            "binding_digest": context.binding_digest,
            "boot_sha256": hashlib.sha256(boot_content.encode()).hexdigest(),
            "model": context.model,
            "effort": context.effort,
        }

    @staticmethod
    def _write_identity(
        layout: deepseek_runtime.ConversationLayout,
        value: Mapping[str, Any],
    ) -> None:
        temporary = layout.root / f".adapter-{os.getpid()}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w") as target:
                json.dump(dict(value), target, separators=(",", ":"), sort_keys=True)
                target.write("\n")
            os.replace(temporary, layout.adapter_identity)
            layout.adapter_identity.chmod(0o600)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _read_identity(
        layout: deepseek_runtime.ConversationLayout,
    ) -> dict[str, Any] | None:
        if not layout.adapter_identity.is_file():
            return None
        try:
            raw = layout.adapter_identity.read_text()
            if len(raw.encode()) > 16384:
                raise ValueError("adapter identity exceeds 16 KiB")
            value = json.loads(raw)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise AdapterError(
                "HARNESS_SESSION_IDENTITY_INVALID",
                "stored DeepSeek adapter identity is malformed",
            ) from exc
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise AdapterError(
                "HARNESS_SESSION_IDENTITY_INVALID",
                "stored DeepSeek adapter identity has an unknown schema",
            )
        return value

    def _validate_identity(
        self,
        layout: deepseek_runtime.ConversationLayout,
        context: ConversationContext,
        session_ref: str,
    ) -> None:
        stored = self._read_identity(layout)
        if stored is None:
            raise AdapterError(
                "HARNESS_SESSION_LOST",
                f"DeepSeek session identity is missing: {session_ref}",
            )
        expected = self._identity_value(context, session_ref)
        if stored != expected:
            differing = sorted(
                key for key in set(stored) | set(expected)
                if stored.get(key) != expected.get(key)
            )
            code = (
                "HARNESS_WORKTREE_MISMATCH"
                if "worktree" in differing
                else "HARNESS_SESSION_IDENTITY_MISMATCH"
            )
            raise AdapterError(
                code,
                "stored DeepSeek identity disagrees with current immutable "
                + ", ".join(differing),
            )

    def _validate_recovery_identity(
        self,
        layout: deepseek_runtime.ConversationLayout,
        context: ConversationContext,
        session_ref: str,
    ) -> dict[str, Any]:
        stored = self._read_identity(layout)
        if stored is None:
            raise AdapterError(
                "HARNESS_SESSION_LOST",
                f"DeepSeek session identity is missing: {session_ref}",
            )
        expected = {
            "conversation_id": self._conversation_id(context),
            "session_ref": session_ref,
            "worktree": str(context.checked_worktree()),
            "binding_digest": context.binding_digest,
            "model": context.model,
            "effort": context.effort,
        }
        differing = sorted(
            key for key, value in expected.items() if stored.get(key) != value
        )
        if differing:
            code = (
                "HARNESS_WORKTREE_MISMATCH"
                if "worktree" in differing
                else "HARNESS_SESSION_IDENTITY_MISMATCH"
            )
            raise AdapterError(
                code,
                "stored DeepSeek recovery identity disagrees with current immutable "
                + ", ".join(differing),
            )
        return stored

    def _start_turn(
        self,
        transport: DeepSeekTransport,
        layout: deepseek_runtime.ConversationLayout,
        session_ref: str,
        context: ConversationContext,
        message: str,
        *,
        resumed: bool,
    ) -> NativeTurn:
        if not resumed and self._read_identity(layout) is not None:
            raise AdapterError(
                "HARNESS_SESSION_IDENTITY_EXISTS",
                "DeepSeek conversation root already owns a native identity",
            )
        if not resumed:
            self._write_identity(layout, self._identity_value(context, session_ref))
        else:
            self._validate_identity(layout, context, session_ref)
        inspected = transport.request("session/start", {"sessionId": session_ref})
        actual = inspected.get("sessionId") if isinstance(inspected, dict) else None
        ensure_exact_session(session_ref, actual)
        last_seq = inspected.get("lastEventSeq") if isinstance(inspected, dict) else None
        if last_seq is not None and (
            not isinstance(last_seq, int) or isinstance(last_seq, bool) or last_seq < 0
        ):
            raise AdapterError(
                "HARNESS_PROTOCOL_ERROR",
                "DeepSeek session/start returned an invalid event boundary",
            )
        boundary = 0 if last_seq is None else last_seq + 1
        process = getattr(transport, "process", None)
        pid = getattr(process, "pid", None)
        turn = NativeTurn(
            harness=self.harness,
            session_ref=session_ref,
            run_ref=_run_ref(f"dispatch-uncertain-{uuid.uuid4().hex}", boundary),
            worktree=context.checked_worktree(),
            process_ref=str(pid) if isinstance(pid, int) and pid > 0 else None,
            metadata={
                "resumed": resumed,
                "from_event_seq": boundary,
                "seen_event_seq": set(),
                "usage_steps": set(),
                "unknown_native_events": [],
                "layout_key": layout.conversation_key,
                "binding_digest": context.binding_digest,
                "boot_sha256": self._identity_value(context, session_ref)["boot_sha256"],
            },
        )
        try:
            prompted = transport.request(
                "session/prompt", {"sessionId": session_ref, "message": message}
            )
        except AdapterError as exc:
            turn.metadata["dispatch_error"] = {
                "code": exc.code,
                "detail": deepseek_runtime.sanitize_diagnostic(exc.detail),
            }
            return turn
        message_id = prompted.get("messageId") if isinstance(prompted, dict) else None
        if not isinstance(message_id, str) or not message_id:
            turn.metadata["dispatch_error"] = {
                "code": "HARNESS_PROTOCOL_ERROR",
                "detail": "DeepSeek session/prompt returned no message identity",
            }
            return turn
        turn.run_ref = _run_ref(message_id, boundary)
        return turn

    def start(self, context: ConversationContext, message: str) -> NativeTurn:
        message = ensure_nonempty_message(message)
        layout = deepseek_runtime.conversation_layout(
            self._conversation_id(context), state_root=self.state_root
        )
        stored = self._read_identity(layout)
        session_ref = (
            self._session_ref(stored.get("session_ref"))
            if stored is not None
            else f"deepseek-{uuid.uuid4().hex}"
        )
        transport, layout = self._transport(context, dispatch=True)
        return self._start_turn(
            transport,
            layout,
            session_ref,
            context,
            message,
            resumed=stored is not None,
        )

    def resume(
        self,
        session_ref: str,
        context: ConversationContext,
        message: str,
    ) -> NativeTurn:
        session_ref = self._session_ref(session_ref)
        message = ensure_nonempty_message(message)
        layout = deepseek_runtime.conversation_layout(
            self._conversation_id(context), state_root=self.state_root
        )
        self._validate_identity(layout, context, session_ref)
        transport, layout = self._transport(context, dispatch=True)
        return self._start_turn(
            transport,
            layout,
            session_ref,
            context,
            message,
            resumed=True,
        )

    @staticmethod
    def _matches_session(session_ref: str, payload: Mapping[str, Any]) -> bool:
        candidate = payload.get("sessionId")
        return candidate is None or candidate == session_ref

    @staticmethod
    def _usage(raw: Mapping[str, Any]) -> dict[str, int | float]:
        return {
            target: raw[source]
            for source, target in (
                ("inputTokens", "input_tokens"),
                ("outputTokens", "output_tokens"),
                ("cacheReadTokens", "cache_read_tokens"),
                ("cacheWriteTokens", "cache_write_tokens"),
                ("reasoningTokens", "reasoning_tokens"),
            )
            if isinstance(raw.get(source), (int, float))
            and not isinstance(raw.get(source), bool)
        }

    def _terminal(
        self,
        turn: NativeTurn,
        event_type: str,
        payload: Mapping[str, Any],
        native_type: str,
        interrupt_evidence: str | None = None,
    ) -> NormalizedEvent | None:
        if turn.metadata.get("terminal"):
            return None
        turn.metadata["terminal"] = event_type
        if interrupt_evidence:
            turn.metadata["interrupt_evidence"] = interrupt_evidence
        unknown = turn.metadata.get("unknown_native_events")
        return NormalizedEvent(
            event_type,
            {
                **dict(payload),
                "session_ref": turn.session_ref,
                "run_ref": turn.run_ref,
                **({"unknown_native_events": list(unknown)} if unknown else {}),
            },
            native_type,
            interrupt_evidence,
        )

    def _session_event(
        self,
        turn: NativeTurn,
        event: Mapping[str, Any],
    ) -> list[NormalizedEvent]:
        native_type = event.get("type")
        seq = event.get("seq")
        data = event.get("data")
        if not isinstance(native_type, str) or not isinstance(data, dict):
            return []
        if isinstance(seq, int) and not isinstance(seq, bool):
            boundary = int(turn.metadata.get("from_event_seq", 0))
            seen = turn.metadata.setdefault("seen_event_seq", set())
            if seq < boundary or seq in seen:
                return []
            seen.add(seq)
        native = _bounded_native(event)
        if native_type == "turn/start":
            return [
                NormalizedEvent(
                    "run.started",
                    {"status": "running", "native": native},
                    native_type,
                )
            ]
        if native_type == "assistant/chunk":
            chunk = data.get("chunk")
            if not isinstance(chunk, dict):
                return []
            kind = chunk.get("type")
            if kind in {"text-delta", "reasoning-delta"} and isinstance(
                chunk.get("text"), str
            ):
                return [
                    NormalizedEvent(
                        "assistant.delta",
                        {
                            "text": chunk["text"],
                            "segment": (
                                "reasoning" if kind == "reasoning-delta" else "answer"
                            ),
                            "native": native,
                        },
                        f"{native_type}.{kind}",
                    )
                ]
            if kind == "usage" and isinstance(chunk.get("usage"), dict):
                usage = self._usage(chunk["usage"])
                step = (data.get("turn"), data.get("step"))
                if usage:
                    turn.metadata.setdefault("usage_steps", set()).add(step)
                    return [
                        NormalizedEvent(
                            "usage",
                            {"tokens": usage, "native": native},
                            f"{native_type}.usage",
                        )
                    ]
            return []
        if native_type == "assistant/message":
            usage = data.get("usage")
            step = (data.get("turn"), data.get("step"))
            if (
                isinstance(usage, dict)
                and step not in turn.metadata.setdefault("usage_steps", set())
            ):
                normalized = self._usage(usage)
                if normalized:
                    turn.metadata["usage_steps"].add(step)
                    return [
                        NormalizedEvent(
                            "usage",
                            {"tokens": normalized, "native": native},
                            native_type,
                        )
                    ]
            return []
        if native_type == "tool/call":
            return [
                NormalizedEvent(
                    "tool.started",
                    {
                        "tool_ref": data.get("callId"),
                        "name": data.get("name"),
                        "arguments": data.get("arguments"),
                        "native": native,
                    },
                    native_type,
                )
            ]
        if native_type == "tool/result":
            message = data.get("message")
            tool_ref = message.get("toolCallId") if isinstance(message, dict) else None
            is_error = message.get("isError") if isinstance(message, dict) else None
            return [
                NormalizedEvent(
                    "tool.completed",
                    {
                        "tool_ref": tool_ref,
                        "status": "failed" if is_error else "completed",
                        "native": native,
                    },
                    native_type,
                )
            ]
        if native_type == "approval/asked":
            return [
                NormalizedEvent(
                    "permission.requested",
                    {
                        "request_ref": data.get("id"),
                        "kind": native_type,
                        "native": native,
                    },
                    native_type,
                )
            ]
        if native_type in {"question/asked", "input/requested"}:
            return [
                NormalizedEvent(
                    "input.requested",
                    {"kind": native_type, "native": native},
                    native_type,
                )
            ]
        if native_type == "turn/end":
            reason = data.get("reason")
            kind = reason.get("kind") if isinstance(reason, dict) else None
            if kind == "completed":
                terminal = self._terminal(
                    turn,
                    "run.completed",
                    {"status": "completed", "native": native},
                    native_type,
                )
            elif kind in {"aborted", "interrupted"}:
                terminal = self._terminal(
                    turn,
                    "run.interrupted",
                    {"status": "cancelled", "native": native},
                    native_type,
                    "native",
                )
            else:
                terminal = self._terminal(
                    turn,
                    "run.failed",
                    {
                        "status": "failed",
                        "error": _native_failure_code(reason),
                        "reason": kind or "unknown",
                        "native": native,
                    },
                    native_type,
                )
            return [terminal] if terminal is not None else []
        unknown = turn.metadata.setdefault("unknown_native_events", [])
        if len(unknown) < MAX_UNKNOWN_EVENTS:
            unknown.append(native)
        return []

    def _interaction_failure(
        self,
        turn: NativeTurn,
        *,
        native_type: str,
        payload: Mapping[str, Any],
    ) -> NormalizedEvent | None:
        try:
            self.interrupt(turn)
        except AdapterError:
            pass
        return self._terminal(
            turn,
            "run.failed",
            {
                "status": "failed",
                "error": "HARNESS_APPROVAL_UNSUPPORTED",
                "native": _bounded_native(payload),
            },
            native_type,
        )

    def _dispatch_failure_terminal(
        self,
        turn: NativeTurn,
        failure: Mapping[str, Any],
    ) -> NormalizedEvent | None:
        try:
            cancellation = self.interrupt(turn)
        except AdapterError:
            return None
        if not cancellation.acknowledged:
            return None
        return self._terminal(
            turn,
            "run.failed",
            {
                "status": "failed",
                "error": failure.get("code") or "HARNESS_DISPATCH_UNCERTAIN",
                "detail": failure.get("detail") or "DeepSeek prompt dispatch failed",
                "native_cancelled": True,
            },
            "session/prompt",
        )

    def _silent_stream_terminal(
        self,
        turn: NativeTurn,
        probe_count: int,
    ) -> NormalizedEvent | None:
        transport = self._transport_instance
        if transport is None:
            raise AdapterError(
                "HARNESS_UNAVAILABLE",
                "DeepSeek carrier is not connected",
                retryable=True,
            )
        try:
            inspected = transport.request(
                "session/inspect", {"sessionId": turn.session_ref}
            )
            actual = inspected.get("sessionId") if isinstance(inspected, dict) else None
            ensure_exact_session(turn.session_ref, actual)
            state = inspected.get("status") if isinstance(inspected, dict) else None
            result = transport.request(
                "session/reconcile",
                {
                    "sessionId": turn.session_ref,
                    "fromEventSeq": int(turn.metadata.get("from_event_seq", 0)),
                },
            )
            actual = result.get("sessionId") if isinstance(result, dict) else None
            ensure_exact_session(turn.session_ref, actual)
            outcome = result.get("outcome") if isinstance(result, dict) else None
        except AdapterError:
            self.close()
            raise
        if outcome == "succeeded":
            return self._terminal(
                turn,
                "run.completed",
                {"status": "completed", "reconciled_after_silence": True},
                "session/reconcile",
            )
        if outcome == "failed":
            return self._terminal(
                turn,
                "run.failed",
                {
                    "status": "failed",
                    "error": "HARNESS_NATIVE_RUN_FAILED",
                    "reconciled_after_silence": True,
                },
                "session/reconcile",
            )
        if outcome == "cancelled":
            return self._terminal(
                turn,
                "run.interrupted",
                {"status": "cancelled", "reconciled_after_silence": True},
                "session/reconcile",
                "native",
            )
        if outcome == "running" and probe_count < self.silent_probe_limit:
            turn.metadata["silent_probe_state"] = state
            return None
        try:
            cancellation = self.interrupt(turn)
        except AdapterError:
            cancellation = InterruptResult(False, "native cancellation failed")
        if cancellation.acknowledged:
            terminal = self._terminal(
                turn,
                "run.failed",
                {
                    "status": "failed",
                    "error": "HARNESS_STREAM_INACTIVE",
                    "detail": "DeepSeek carrier stayed silent through bounded liveness probes",
                    "native_cancelled": True,
                    "last_inspected_state": state,
                },
                "session/reconcile",
            )
            self.close()
            return terminal
        self.close()
        raise AdapterError(
            "HARNESS_STREAM_INACTIVE",
            "DeepSeek carrier stayed silent and its native outcome is unknown",
        )

    def stream(self, turn: NativeTurn) -> Iterator[NormalizedEvent]:
        if self._transport_instance is None:
            raise AdapterError(
                "HARNESS_UNAVAILABLE",
                "DeepSeek carrier is not connected",
                retryable=True,
            )
        yield NormalizedEvent(
            "session.started",
            {
                "session_ref": turn.session_ref,
                "resumed": bool(turn.metadata.get("resumed")),
                "binding_digest": turn.metadata.get("binding_digest"),
                "boot_sha256": turn.metadata.get("boot_sha256"),
            },
            "session/start",
        )
        dispatch_error = turn.metadata.get("dispatch_error")
        if isinstance(dispatch_error, Mapping):
            terminal = self._dispatch_failure_terminal(turn, dispatch_error)
            if terminal is not None:
                yield terminal
            return
        silent_probes = 0
        transport = self._transport_instance
        while True:
            raw = transport.poll_notification(self.stream_inactivity_seconds)
            if raw is None:
                silent_probes += 1
                terminal = self._silent_stream_terminal(turn, silent_probes)
                if terminal is not None:
                    yield terminal
                    return
                continue
            silent_probes = 0
            method = raw.get("method")
            params = raw.get("params")
            if not isinstance(method, str) or not isinstance(params, dict):
                continue
            if method == "worker/error":
                terminal = self._terminal(
                    turn,
                    "run.failed",
                    {
                        "status": "failed",
                        "error": params.get("code") or "HARNESS_UNAVAILABLE",
                        "detail": deepseek_runtime.sanitize_diagnostic(
                            str(params.get("detail") or "carrier failed")
                        ),
                    },
                    method,
                )
                if terminal is not None:
                    yield terminal
                return
            if method == "native/request":
                native_method = str(params.get("method") or "native/request")
                event_type = (
                    "permission.requested"
                    if re.search(r"approval|permission", native_method, re.I)
                    else "input.requested"
                )
                yield NormalizedEvent(
                    event_type,
                    {
                        "request_ref": params.get("requestId"),
                        "kind": native_method,
                        "native": _bounded_native(params),
                    },
                    native_method,
                )
                terminal = self._interaction_failure(
                    turn, native_type=native_method, payload=params
                )
                if terminal is not None:
                    yield terminal
                return
            if method != "native/notification":
                continue
            native_method = params.get("method")
            payload = params.get("payload")
            if not isinstance(native_method, str) or not isinstance(payload, dict):
                continue
            if not self._matches_session(turn.session_ref, payload):
                continue
            if native_method != "session.event":
                continue
            event = payload.get("event")
            if not isinstance(event, dict):
                continue
            normalized = self._session_event(turn, event)
            interaction = next(
                (
                    item
                    for item in normalized
                    if item.type in {"permission.requested", "input.requested"}
                ),
                None,
            )
            for item in normalized:
                yield item
                if item.type in TERMINAL_EVENTS:
                    return
            if interaction is not None:
                terminal = self._interaction_failure(
                    turn,
                    native_type=interaction.native_type or "native.interaction",
                    payload=event,
                )
                if terminal is not None:
                    yield terminal
                return

    def interrupt(self, turn: NativeTurn) -> InterruptResult:
        if self._transport_instance is None:
            return InterruptResult(False, "DeepSeek carrier is not connected")
        result = self._transport_instance.request(
            "session/cancel", {"sessionId": turn.session_ref}
        )
        actual = result.get("sessionId") if isinstance(result, dict) else None
        ensure_exact_session(turn.session_ref, actual)
        accepted = result.get("accepted") if isinstance(result, dict) else None
        outcome = result.get("outcome") if isinstance(result, dict) else None
        if accepted is True and outcome == "cancelled":
            return InterruptResult(True, "native cancellation reached idle")
        return InterruptResult(False, str(outcome or "native cancellation not accepted"))

    def inspect(
        self,
        session_ref: str,
        context: ConversationContext,
    ) -> SessionInspection:
        session_ref = self._session_ref(session_ref)
        layout = deepseek_runtime.conversation_layout(
            self._conversation_id(context), state_root=self.state_root
        )
        try:
            self._validate_recovery_identity(layout, context, session_ref)
        except AdapterError as exc:
            if exc.code != "HARNESS_SESSION_LOST":
                raise
            return SessionInspection(session_ref, False, "missing")
        if context.boot_content:
            self._validate_identity(layout, context, session_ref)
        transport, layout = self._transport(context, dispatch=False)
        result = transport.request("session/inspect", {"sessionId": session_ref})
        actual = result.get("sessionId") if isinstance(result, dict) else None
        ensure_exact_session(session_ref, actual)
        presence = result.get("presence") if isinstance(result, dict) else None
        state = result.get("status") if isinstance(result, dict) else None
        return SessionInspection(
            session_ref,
            presence in {"persisted", "live"},
            state if isinstance(state, str) else "unknown",
            context.checked_worktree(),
            {"native": _bounded_native(result), "layout_key": layout.conversation_key},
        )

    def reconcile(
        self,
        turn: NativeTurn,
        context: ConversationContext,
    ) -> ReconcileResult:
        terminal = turn.metadata.get("terminal")
        if terminal in TERMINAL_EVENTS:
            outcome = {
                "run.completed": "succeeded",
                "run.failed": "failed",
                "run.interrupted": "cancelled",
            }[terminal]
            return ReconcileResult(
                outcome,
                True,
                f"terminal {terminal} was observed from the DeepSeek session log",
                "native" if outcome == "cancelled" else None,
            )
        session_ref = self._session_ref(turn.session_ref)
        boundary = int(turn.metadata.get("from_event_seq", _run_boundary(turn.run_ref)))
        layout = deepseek_runtime.conversation_layout(
            self._conversation_id(context), state_root=self.state_root
        )
        try:
            self._validate_recovery_identity(layout, context, session_ref)
        except AdapterError as exc:
            if exc.code != "HARNESS_SESSION_LOST":
                raise
            return ReconcileResult(
                "unknown", False, "DeepSeek exact session identity is missing"
            )
        transport, layout = self._transport(context, dispatch=False)
        result = transport.request(
            "session/reconcile",
            {"sessionId": session_ref, "fromEventSeq": boundary},
        )
        actual = result.get("sessionId") if isinstance(result, dict) else None
        ensure_exact_session(session_ref, actual)
        native = result.get("outcome") if isinstance(result, dict) else None
        if native not in {"running", "succeeded", "failed", "cancelled", "unknown"}:
            raise AdapterError(
                "HARNESS_RECONCILIATION_INVALID",
                "DeepSeek carrier returned an invalid reconciliation outcome",
            )
        proven = native != "unknown"
        return ReconcileResult(
            native,
            proven,
            (
                f"DeepSeek session/reconcile reports {native} from event {boundary} "
                f"in isolated root {layout.conversation_key}"
            ),
            "native" if native == "cancelled" else None,
        )

    def close(self) -> None:
        transport, self._transport_instance = self._transport_instance, None
        self._transport_identity = None
        lease, self._lease = self._lease, None
        try:
            if transport is not None:
                transport.close()
        finally:
            if lease is not None:
                lease.close()
