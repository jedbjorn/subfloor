#!/usr/bin/env python3
"""Managed DeepSeek Browser adapter over the stock loopback Host API."""
from __future__ import annotations

import base64
import json
import re
import uuid
from typing import Any, Callable, Iterator, Mapping

import deepseek_host
import route_transport

from .base import (
    AdapterError,
    ConversationAdapter,
    ConversationContext,
    InterruptResult,
    NativeTurn,
    NormalizedEvent,
    ProbeResult,
    ReconcileResult,
    SessionInspection,
    ensure_nonempty_message,
    load_manifest,
)


SESSION_REF = re.compile(r"^sc-[0-9a-f]{32}$")
RUN_REF_PREFIX = "deepseek-host-run-v1:"
MAX_UNKNOWN_EVENTS = 24
SENSITIVE_KEY = re.compile(
    r"(?:key|token|secret|password|credential|authorization)", re.I
)


# Compatibility exports for imports of the former carrier protocol.
DeepSeekTransport = deepseek_host.HostTransport
DeepSeekHostClient = deepseek_host.DeepSeekHostClient


def _adapter_error(exc: deepseek_host.DeepSeekHostError) -> AdapterError:
    return AdapterError(
        exc.code,
        exc.detail,
        retryable=exc.code in {
            "HARNESS_HOST_UNAVAILABLE",
            "HARNESS_HOST_STREAM_LOST",
            "HARNESS_HOST_STREAM_TIMEOUT",
        },
    )


def _bounded_native(value: Any) -> Any:
    def redact(item: Any, depth: int = 0) -> Any:
        if depth > 8:
            return "[TRUNCATED]"
        if isinstance(item, Mapping):
            return {
                str(key): (
                    "[REDACTED]"
                    if SENSITIVE_KEY.search(str(key))
                    else redact(child, depth + 1)
                )
                for key, child in list(item.items())[:128]
            }
        if isinstance(item, list):
            return [redact(child, depth + 1) for child in item[:128]]
        if isinstance(item, str):
            return deepseek_host._redact_text(item, limit=2_048)
        if isinstance(item, (int, float, bool)) or item is None:
            return item
        return str(type(item).__name__)

    return redact(value)


def _run_ref(boundary: int) -> str:
    payload = json.dumps(
        {"from_seq": boundary, "nonce": uuid.uuid4().hex},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return RUN_REF_PREFIX + base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _run_boundary(run_ref: str) -> int:
    if not isinstance(run_ref, str) or not run_ref.startswith(RUN_REF_PREFIX):
        raise AdapterError(
            "HARNESS_RUN_REF_INVALID", "DeepSeek run reference is malformed"
        )
    encoded = run_ref[len(RUN_REF_PREFIX):]
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        )
    except (ValueError, json.JSONDecodeError) as exc:
        raise AdapterError(
            "HARNESS_RUN_REF_INVALID", "DeepSeek run reference is malformed"
        ) from exc
    boundary = payload.get("from_seq") if isinstance(payload, dict) else None
    if not isinstance(boundary, int) or isinstance(boundary, bool) or boundary < 0:
        raise AdapterError(
            "HARNESS_RUN_REF_INVALID", "DeepSeek run reference boundary is invalid"
        )
    return boundary


class DeepSeekAdapter(ConversationAdapter):
    harness = "deepseek"

    def __init__(
        self,
        manifest: Mapping[str, Any] | None = None,
        *,
        client_factory: Callable[[], deepseek_host.HostTransport] = (
            deepseek_host.DeepSeekHostClient
        ),
    ) -> None:
        super().__init__(manifest or load_manifest(self.harness))
        self.client_factory = client_factory

    def _client(self) -> deepseek_host.HostTransport:
        try:
            return self.client_factory()
        except deepseek_host.DeepSeekHostError as exc:
            raise _adapter_error(exc) from exc

    def probe(self) -> ProbeResult:
        try:
            described = self._client().call("host.describe", {})
        except deepseek_host.DeepSeekHostError as exc:
            raise _adapter_error(exc) from exc
        version = described.get("version") if isinstance(described, Mapping) else None
        conversation = self.manifest["conversation"]
        if version != conversation["verified_cli_version"]:
            raise AdapterError(
                "HARNESS_VERSION_UNSUPPORTED",
                "DeepSeek Host version does not match the pinned official runtime",
            )
        return ProbeResult(
            harness=self.harness,
            version=version,
            minimum_version=conversation["minimum_cli_version"],
            capabilities=self.capabilities,
            maximum_version_exclusive=conversation["maximum_cli_version_exclusive"],
            verified_version=conversation["verified_cli_version"],
            compatibility="verified",
        )

    @staticmethod
    def _conversation_id(context: ConversationContext) -> str:
        value = context.conversation_id
        if not isinstance(value, str) or not value:
            raise AdapterError(
                "HARNESS_IDENTITY_MISSING",
                "DeepSeek adapter requires the stable conversation identity",
            )
        return value

    @classmethod
    def _new_session_ref(cls, context: ConversationContext) -> str:
        identity = uuid.uuid5(
            uuid.NAMESPACE_URL, cls._conversation_id(context)
        ).hex
        return f"sc-{identity}"

    @staticmethod
    def _session_ref(value: str) -> str:
        if not isinstance(value, str) or SESSION_REF.fullmatch(value) is None:
            raise AdapterError(
                "HARNESS_SESSION_MISMATCH", "DeepSeek session reference is malformed"
            )
        return value

    @staticmethod
    def _history(
        client: deepseek_host.HostTransport, session_ref: str
    ) -> list[dict]:
        value = client.call(
            "session.history", {"sessionId": session_ref, "maxMessages": 200}
        )
        entries = value.get("events") if isinstance(value, Mapping) else None
        if not isinstance(entries, list):
            raise AdapterError(
                "HARNESS_PROTOCOL_ERROR", "DeepSeek Host returned invalid history"
            )
        events = []
        for entry in entries:
            event = entry.get("event") if isinstance(entry, Mapping) else None
            if not isinstance(event, dict):
                raise AdapterError(
                    "HARNESS_PROTOCOL_ERROR", "DeepSeek Host history row is invalid"
                )
            events.append(event)
        return events

    @classmethod
    def _boundary(
        cls, client: deepseek_host.HostTransport, session_ref: str
    ) -> int:
        events = cls._history(client, session_ref)
        seqs = [
            event.get("seq")
            for event in events
            if isinstance(event.get("seq"), int)
            and not isinstance(event.get("seq"), bool)
        ]
        return max(seqs, default=-1) + 1

    @staticmethod
    def _route(
        client: deepseek_host.HostTransport,
        context: ConversationContext,
    ) -> deepseek_host.ConfiguredRoute:
        try:
            projection = route_transport.context_projection(context, "deepseek")
        except route_transport.route_bindings.RouteResolutionError as exc:
            raise AdapterError(exc.code, exc.message) from exc
        if projection is None or not projection.model:
            raise AdapterError(
                "HARNESS_ROUTE_INVALID", "DeepSeek requires one immutable exact route"
            )
        binding = context.route_binding
        if not isinstance(binding, Mapping):
            raise AdapterError(
                "HARNESS_ROUTE_INVALID", "DeepSeek route binding is missing"
            )
        metadata = binding.get("adapter_metadata")
        if not isinstance(metadata, Mapping):
            raise AdapterError(
                "HARNESS_ROUTE_INVALID", "DeepSeek route metadata is missing"
            )
        try:
            route = deepseek_host.route_for(client, projection.model)
        except deepseek_host.DeepSeekHostError as exc:
            raise _adapter_error(exc) from exc
        expected = route.binding_metadata(binding["requested_effort"])
        if dict(metadata) != expected:
            raise AdapterError(
                "HARNESS_ROUTE_STALE",
                "DeepSeek official configuration changed after exact route binding",
            )
        if context.provider is not None and context.provider != route.provider:
            raise AdapterError(
                "HARNESS_ROUTE_INVALID",
                "stored provider disagrees with the immutable DeepSeek route",
            )
        return route

    @staticmethod
    def _select(
        client: deepseek_host.HostTransport,
        session_ref: str,
        route: deepseek_host.ConfiguredRoute,
        effort: str,
    ) -> None:
        payload = {
            "sessionId": session_ref,
            "provider": route.provider,
            "model": route.model,
            **({} if effort == "default" else {"reasoningEffort": effort}),
        }
        selected = client.call("session.selectModel", payload)
        expected = {
            "provider": route.provider,
            "model": route.model,
            **({} if effort == "default" else {"reasoningEffort": effort}),
        }
        if not isinstance(selected, Mapping) or selected.get("selected") != expected:
            raise AdapterError(
                "HARNESS_ROUTE_MISMATCH",
                "DeepSeek Host did not select the exact bound provider/model route",
            )

    def _turn(
        self,
        client: deepseek_host.HostTransport,
        session_ref: str,
        context: ConversationContext,
        message: str,
        *,
        resumed: bool,
        route: deepseek_host.ConfiguredRoute | None = None,
    ) -> NativeTurn:
        route = route or self._route(client, context)
        boundary = self._boundary(client, session_ref)
        binding = context.route_binding
        effort = (
            binding.get("requested_effort")
            if isinstance(binding, Mapping)
            else None
        )
        self._select(client, session_ref, route, effort or "default")
        stream = client.open_events()
        try:
            accepted = client.call(
                "session.prompt",
                {
                    "sessionId": session_ref,
                    "mode": "queue",
                    "content": [{"type": "text", "text": message}],
                },
            )
        except Exception:
            stream.close()
            raise
        if not isinstance(accepted, Mapping) or accepted.get("accepted") is not True:
            stream.close()
            raise AdapterError(
                "HARNESS_PROTOCOL_ERROR", "DeepSeek Host did not accept the prompt"
            )
        return NativeTurn(
            harness=self.harness,
            session_ref=session_ref,
            run_ref=_run_ref(boundary),
            worktree=context.checked_worktree(),
            metadata={
                "from_event_seq": boundary,
                "seen_event_seq": set(),
                "resumed": resumed,
                "context": context,
                "route": route,
                "client": client,
                "stream": stream,
            },
            opaque=stream,
        )

    def start(self, context: ConversationContext, message: str) -> NativeTurn:
        message = ensure_nonempty_message(message)
        client = self._client()
        route = self._route(client, context)
        session_ref = self._new_session_ref(context)
        try:
            created = client.call(
                "session.create",
                {
                    "sessionId": session_ref,
                    "cwd": str(context.checked_worktree()),
                },
            )
        except deepseek_host.DeepSeekHostError as exc:
            raise _adapter_error(exc) from exc
        if not isinstance(created, Mapping) or created.get("sessionId") != session_ref:
            raise AdapterError(
                "HARNESS_SESSION_MISMATCH",
                "DeepSeek Host did not preserve the caller-stable session identity",
            )
        return self._turn(
            client, session_ref, context, message, resumed=False, route=route
        )

    def resume(
        self,
        session_ref: str,
        context: ConversationContext,
        message: str,
    ) -> NativeTurn:
        session_ref = self._session_ref(session_ref)
        message = ensure_nonempty_message(message)
        client = self._client()
        route = self._route(client, context)
        try:
            created = client.call(
                "session.create",
                {
                    "sessionId": session_ref,
                    "cwd": str(context.checked_worktree()),
                },
            )
        except deepseek_host.HostRpcError as exc:
            raise AdapterError("HARNESS_SESSION_LOST", exc.detail) from exc
        except deepseek_host.DeepSeekHostError as exc:
            raise _adapter_error(exc) from exc
        if not isinstance(created, Mapping) or created.get("sessionId") != session_ref:
            raise AdapterError(
                "HARNESS_SESSION_MISMATCH",
                "DeepSeek Host did not cold-resume the exact native session",
            )
        return self._turn(
            client, session_ref, context, message, resumed=True, route=route
        )

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

    @staticmethod
    def _terminal(
        turn: NativeTurn,
        event_type: str,
        payload: Mapping[str, Any],
        native_type: str,
        interrupt_evidence: str | None = None,
    ) -> NormalizedEvent | None:
        if turn.metadata.get("terminal"):
            return None
        turn.metadata["terminal"] = event_type
        return NormalizedEvent(
            event_type,
            {
                **dict(payload),
                "session_ref": turn.session_ref,
                "run_ref": turn.run_ref,
            },
            native_type,
            interrupt_evidence,
        )

    def _session_event(
        self, turn: NativeTurn, event: Mapping[str, Any]
    ) -> list[NormalizedEvent]:
        native_type = event.get("type")
        seq = event.get("seq")
        data = event.get("data")
        if not isinstance(native_type, str) or not isinstance(data, Mapping):
            return []
        if isinstance(seq, int) and not isinstance(seq, bool):
            boundary = int(turn.metadata.get("from_event_seq", 0))
            seen = turn.metadata.setdefault("seen_event_seq", set())
            if seq < boundary or seq in seen:
                return []
            seen.add(seq)
        native = _bounded_native(event)
        if native_type == "turn/start":
            return [NormalizedEvent(
                "run.started", {"status": "running", "native": native}, native_type
            )]
        if native_type == "assistant/chunk":
            chunk = data.get("chunk")
            if not isinstance(chunk, Mapping):
                return []
            kind = chunk.get("type")
            if kind in {"text-delta", "reasoning-delta"} and isinstance(
                chunk.get("text"), str
            ):
                return [NormalizedEvent(
                    "assistant.delta",
                    {
                        "text": chunk["text"],
                        "segment": (
                            "reasoning" if kind == "reasoning-delta" else "answer"
                        ),
                        "native": native,
                    },
                    f"{native_type}.{kind}",
                )]
            if kind == "usage" and isinstance(chunk.get("usage"), Mapping):
                usage = self._usage(chunk["usage"])
                if usage:
                    return [NormalizedEvent(
                        "usage",
                        {"tokens": usage, "native": native},
                        f"{native_type}.usage",
                    )]
            return []
        if native_type == "assistant/message" and isinstance(
            data.get("usage"), Mapping
        ):
            usage = self._usage(data["usage"])
            return [NormalizedEvent(
                "usage", {"tokens": usage, "native": native}, native_type
            )] if usage else []
        if native_type == "tool/call":
            return [NormalizedEvent(
                "tool.started",
                {
                    "tool_ref": data.get("callId"),
                    "name": data.get("name"),
                    "arguments": data.get("arguments"),
                    "native": native,
                },
                native_type,
            )]
        if native_type == "tool/result":
            message = data.get("message")
            tool_ref = (
                message.get("toolCallId") if isinstance(message, Mapping) else None
            )
            is_error = message.get("isError") if isinstance(message, Mapping) else None
            return [NormalizedEvent(
                "tool.completed",
                {
                    "tool_ref": tool_ref,
                    "status": "failed" if is_error else "completed",
                    "native": native,
                },
                native_type,
            )]
        if native_type == "turn/end":
            reason = data.get("reason")
            kind = reason.get("kind") if isinstance(reason, Mapping) else None
            if kind == "completed":
                terminal = self._terminal(
                    turn,
                    "run.completed",
                    {"status": "completed", "native": native},
                    native_type,
                )
            elif kind in {"aborted", "cancelled", "interrupted"}:
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
                        "error": "HARNESS_NATIVE_RUN_FAILED",
                        "reason": kind or "unknown",
                        "detail": json.dumps(
                            _bounded_native(reason),
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    },
                    native_type,
                )
            return [terminal] if terminal is not None else []
        unknown = turn.metadata.setdefault("unknown_native_events", [])
        if len(unknown) < MAX_UNKNOWN_EVENTS:
            unknown.append(native)
        return []

    def stream(self, turn: NativeTurn) -> Iterator[NormalizedEvent]:
        stream = turn.metadata.get("stream")
        if stream is None:
            raise AdapterError(
                "HARNESS_PROTOCOL_ERROR", "DeepSeek Host event stream is missing"
            )
        yield NormalizedEvent(
            "session.started",
            {
                "session_ref": turn.session_ref,
                "resumed": bool(turn.metadata.get("resumed")),
            },
            "session.create",
        )
        try:
            for envelope in stream:
                payload = (
                    envelope.get("payload")
                    if isinstance(envelope, Mapping)
                    else None
                )
                if not isinstance(payload, Mapping):
                    continue
                frame_type = payload.get("type")
                if (
                    frame_type == "session/event"
                    and payload.get("sessionId") == turn.session_ref
                ):
                    event = payload.get("event")
                    if isinstance(event, Mapping):
                        for normalized in self._session_event(turn, event):
                            yield normalized
                            if normalized.type in {
                                "run.completed", "run.failed", "run.interrupted"
                            }:
                                return
                elif (
                    frame_type in {"approval/requested", "question/requested"}
                    and payload.get("sessionId") == turn.session_ref
                ):
                    self.interrupt(turn)
                    terminal = self._terminal(
                        turn,
                        "run.failed",
                        {
                            "status": "failed",
                            "error": "HARNESS_APPROVAL_UNSUPPORTED",
                        },
                        str(frame_type),
                    )
                    if terminal is not None:
                        yield terminal
                    return
                elif frame_type == "stream/error":
                    break
        except deepseek_host.DeepSeekHostError:
            pass
        finally:
            stream.close()
        if turn.metadata.get("terminal"):
            return
        result = self.reconcile(turn, turn.metadata["context"])
        terminal_type = {
            "cancelled": "run.interrupted",
            "failed": "run.failed",
            "succeeded": "run.completed",
        }.get(result.outcome, "run.failed")
        terminal = self._terminal(
            turn,
            terminal_type,
            {
                "status": result.outcome,
                **(
                    {"error": "HARNESS_RECONCILIATION_UNKNOWN"}
                    if not result.proven
                    else {}
                ),
            },
            "session.history",
            result.interrupt_evidence,
        )
        if terminal is not None:
            yield terminal

    def interrupt(self, turn: NativeTurn) -> InterruptResult:
        client = turn.metadata.get("client")
        if client is None:
            return InterruptResult(False, "DeepSeek Host client is unavailable")
        try:
            result = client.call("session.cancel", {"sessionId": turn.session_ref})
        except deepseek_host.DeepSeekHostError as exc:
            raise _adapter_error(exc) from exc
        accepted = isinstance(result, Mapping) and result.get("accepted") is True
        return InterruptResult(
            accepted,
            None if accepted else "DeepSeek Host did not acknowledge cancellation",
        )

    @staticmethod
    def _history_outcome(
        events: list[dict], boundary: int
    ) -> tuple[str, bool, str | None]:
        relevant = [
            event
            for event in events
            if isinstance(event.get("seq"), int) and event["seq"] >= boundary
        ]
        terminal = next(
            (
                event
                for event in reversed(relevant)
                if event.get("type") == "turn/end"
            ),
            None,
        )
        if terminal is not None:
            data = terminal.get("data")
            reason = data.get("reason") if isinstance(data, Mapping) else None
            kind = reason.get("kind") if isinstance(reason, Mapping) else None
            if kind == "completed":
                return "succeeded", True, None
            if kind in {"aborted", "cancelled", "interrupted"}:
                return "cancelled", True, "native"
            return "failed", True, None
        if any(event.get("type") == "turn/start" for event in relevant):
            return "running", True, None
        return "unknown", False, None

    def inspect(
        self, session_ref: str, context: ConversationContext
    ) -> SessionInspection:
        session_ref = self._session_ref(session_ref)
        client = self._client()
        try:
            listed = client.call("session.list", {})
            items = listed.get("items") if isinstance(listed, Mapping) else None
            if not isinstance(items, list):
                raise AdapterError(
                    "HARNESS_PROTOCOL_ERROR",
                    "DeepSeek Host returned invalid session list",
                )
            row = next(
                (
                    item
                    for item in items
                    if isinstance(item, Mapping)
                    and item.get("sessionId") == session_ref
                ),
                None,
            )
            if row is None:
                return SessionInspection(session_ref, False, "missing")
            if row.get("cwd") != str(context.checked_worktree()):
                raise AdapterError(
                    "HARNESS_WORKTREE_MISMATCH",
                    "DeepSeek native session belongs to another worktree",
                )
            events = self._history(client, session_ref)
        except deepseek_host.HostRpcError:
            return SessionInspection(session_ref, False, "missing")
        except deepseek_host.DeepSeekHostError as exc:
            raise _adapter_error(exc) from exc
        outcome, proven, _interrupt = self._history_outcome(events, 0)
        state = "running" if row.get("running") is True else outcome
        return SessionInspection(
            session_ref,
            True,
            state,
            context.checked_worktree(),
            {
                "last_seq": max(
                    (event.get("seq", -1) for event in events), default=-1
                ),
                "proven": proven,
            },
        )

    def reconcile(
        self, turn: NativeTurn, context: ConversationContext
    ) -> ReconcileResult:
        client = turn.metadata.get("client") or self._client()
        try:
            events = self._history(client, turn.session_ref)
        except deepseek_host.DeepSeekHostError as exc:
            raise _adapter_error(exc) from exc
        boundary = _run_boundary(turn.run_ref)
        outcome, proven, interrupt = self._history_outcome(events, boundary)
        return ReconcileResult(
            outcome,
            proven,
            (
                f"DeepSeek Host history proves {outcome} from event {boundary}"
                if proven
                else (
                    "DeepSeek Host history has no terminal evidence "
                    f"from event {boundary}"
                )
            ),
            interrupt,
        )

    def close(self) -> None:
        return None
