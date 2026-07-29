#!/usr/bin/env python3
"""OpenCode server-backed conversation adapter."""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Iterator, Mapping

from .base import (
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
    TERMINAL_EVENTS,
    UrlHttpTransport,
    ensure_exact_session,
    ensure_nonempty_message,
    load_manifest,
    terminal_outcome,
)


class OpenCodeAdapter(ConversationAdapter):
    harness = "opencode"

    def __init__(
        self,
        *,
        endpoint: str = "http://127.0.0.1:4096",
        password: str | None = None,
        transport: HttpTransport | None = None,
        manifest: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(manifest or load_manifest(self.harness))
        self.transport = transport or UrlHttpTransport(
            endpoint,
            password=password,
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
            return context.provider, context.model
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

    def _prompt(
        self,
        session_ref: str,
        context: ConversationContext,
        message: str,
        run_ref: str,
    ) -> None:
        body: dict[str, Any] = {
            "messageID": run_ref,
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
            f"/session/{session_ref}/prompt_async",
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
        # dispatch so a fast generation cannot finish in the prompt/stream gap.
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
                "event_stream": event_stream,
                "resumed": resumed,
            },
        )
        self._prompt(session_ref, context, message, run_ref)
        return turn

    def start(self, context: ConversationContext, message: str) -> NativeTurn:
        worktree = context.checked_worktree()
        message = ensure_nonempty_message(message)
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
        worktree = context.checked_worktree()
        message = ensure_nonempty_message(message)
        inspected = self.inspect(session_ref, context)
        if not inspected.exists:
            raise AdapterError(
                "HARNESS_SESSION_LOST",
                f"OpenCode session does not exist: {session_ref}",
            )
        permission = self._permission_rules(context)
        if permission:
            self.transport.request(
                "PATCH",
                f"/session/{session_ref}",
                query=self._query(worktree),
                body={"permission": permission},
            )
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
        for raw in native_stream:
            event_session = self._session_of(raw)
            if event_session and event_session != turn.session_ref:
                continue
            for event in self._normalize(raw):
                if event.type in TERMINAL_EVENTS:
                    turn.metadata["terminal"] = event.type
                yield event
                if event.type in TERMINAL_EVENTS:
                    return

    def interrupt(self, turn: NativeTurn) -> InterruptResult:
        result = self.transport.request(
            "POST",
            f"/session/{turn.session_ref}/abort",
            query=self._query(turn.worktree),
        )
        return InterruptResult(bool(result))

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
