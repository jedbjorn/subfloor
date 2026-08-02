#!/usr/bin/env python3
"""Claude process-per-turn conversation adapter."""
from __future__ import annotations

import json
import os
import re
import signal
import uuid
from pathlib import Path
from typing import Any, Iterator, Mapping

from .base import (
    AdapterError,
    ConversationAdapter,
    ConversationContext,
    InterruptResult,
    NativeTurn,
    NormalizedEvent,
    ProbeResult,
    ProcessRunner,
    ReconcileResult,
    SessionInspection,
    SubprocessRunner,
    TERMINAL_EVENTS,
    command_version,
    ensure_exact_session,
    ensure_nonempty_message,
    load_manifest,
    merged_env,
    signal_owned_process,
    terminal_outcome,
)


class ClaudeAdapter(ConversationAdapter):
    harness = "claude"

    def __init__(
        self,
        *,
        runner: ProcessRunner | None = None,
        manifest: Mapping[str, Any] | None = None,
        config_dir: Path | None = None,
    ) -> None:
        super().__init__(manifest or load_manifest(self.harness))
        self.runner = runner or SubprocessRunner()
        self.config_dir = config_dir

    def probe(self) -> ProbeResult:
        launch = self.manifest["headless"]["launch"][0]
        return self._probe_result(command_version([launch, "--version"]))

    def _command(
        self,
        *,
        context: ConversationContext,
        message: str,
        session_ref: str,
        resume: bool,
    ) -> list[str]:
        hcfg = self.manifest["headless"]
        command = list(hcfg["launch"])
        prompt_flag = hcfg.get("prompt_flag", "-p")
        command.extend([prompt_flag, message])
        command.extend(self.manifest["conversation"]["start"]["stream_flags"])
        session = self.manifest["conversation"][
            "resume" if resume else "start"
        ]["session_flag"]
        command.extend([session, session_ref])
        if context.model:
            model_flag = hcfg.get("model_flag")
            if not model_flag:
                raise AdapterError(
                    "HARNESS_MODEL_ROUTE_INVALID",
                    "Claude adapter cannot apply a model",
                )
            command.extend([model_flag, context.model])
        if context.effort:
            effort = hcfg.get("effort") or {}
            if not effort.get("flag"):
                raise AdapterError(
                    "HARNESS_EFFORT_UNSUPPORTED",
                    "Claude adapter cannot apply effort",
                )
            command.extend([effort["flag"], context.effort])
        if context.permission_mode == "unrestricted":
            command.append("--dangerously-skip-permissions")
        return command

    def _launch(
        self,
        context: ConversationContext,
        message: str,
        session_ref: str,
        *,
        resume: bool,
    ) -> NativeTurn:
        worktree = context.checked_worktree()
        if (
            context.permission_mode == "interactive"
            and not self.capabilities.interactive_permission_response
        ):
            raise AdapterError(
                "HARNESS_PERMISSION_RESPONSE_UNSUPPORTED",
                "Claude print mode cannot bridge interactive permissions",
            )
        message = ensure_nonempty_message(message)
        command = self._command(
            context=context,
            message=message,
            session_ref=session_ref,
            resume=resume,
        )
        process = self.runner.spawn(
            command,
            cwd=worktree,
            env=merged_env(self.manifest, context),
        )
        pid = getattr(process, "pid", None)
        return NativeTurn(
            harness=self.harness,
            session_ref=session_ref,
            run_ref=f"claude-{uuid.uuid4()}",
            worktree=worktree,
            process_ref=str(pid) if pid is not None else None,
            metadata={"command": command, "resumed": resume},
            opaque=process,
        )

    def start(self, context: ConversationContext, message: str) -> NativeTurn:
        return self._launch(
            context,
            message,
            str(uuid.uuid4()),
            resume=False,
        )

    def resume(
        self,
        session_ref: str,
        context: ConversationContext,
        message: str,
    ) -> NativeTurn:
        try:
            uuid.UUID(session_ref)
        except ValueError as exc:
            raise AdapterError(
                "HARNESS_SESSION_LOST",
                f"Claude session ref is not a UUID: {session_ref}",
            ) from exc
        if not self.inspect(session_ref, context).exists:
            raise AdapterError(
                "HARNESS_SESSION_LOST",
                f"Claude session does not exist: {session_ref}",
            )
        return self._launch(
            context,
            message,
            session_ref,
            resume=True,
        )

    @staticmethod
    def _session_from(raw: Mapping[str, Any]) -> str | None:
        for field in ("session_id", "sessionId"):
            value = raw.get(field)
            if isinstance(value, str):
                return value
        message = raw.get("message")
        if isinstance(message, dict):
            return ClaudeAdapter._session_from(message)
        return None

    @staticmethod
    def _usage(raw: Mapping[str, Any]) -> dict[str, int | float]:
        usage = raw.get("usage")
        if not isinstance(usage, dict):
            message = raw.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
        if not isinstance(usage, dict):
            return {}
        return {
            key: value
            for key, value in usage.items()
            if isinstance(value, (int, float))
        }

    def _normalize(
        self,
        raw: Mapping[str, Any],
    ) -> list[NormalizedEvent]:
        native_type = raw.get("type")
        if native_type == "system" and raw.get("subtype") == "init":
            return [
                NormalizedEvent(
                    "run.started",
                    {"status": "running"},
                    "system.init",
                ),
            ]
        if native_type == "stream_event":
            event = raw.get("event")
            if not isinstance(event, dict):
                return []
            event_type = event.get("type")
            if event_type == "content_block_delta":
                delta = event.get("delta")
                if (
                    isinstance(delta, dict)
                    and delta.get("type") == "text_delta"
                    and isinstance(delta.get("text"), str)
                ):
                    return [
                        NormalizedEvent(
                            "assistant.delta",
                            {"text": delta["text"]},
                            "stream_event.content_block_delta",
                        )
                    ]
            if event_type == "content_block_start":
                block = event.get("content_block")
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    return [
                        NormalizedEvent(
                            "tool.started",
                            {
                                "tool_ref": block.get("id"),
                                "name": block.get("name"),
                            },
                            "stream_event.content_block_start",
                        )
                    ]
            if event_type == "message_delta":
                usage = self._usage(event)
                if usage:
                    return [
                        NormalizedEvent(
                            "usage",
                            {"tokens": usage},
                            "stream_event.message_delta",
                        )
                    ]
            return []
        if native_type == "user":
            content = raw.get("message", {}).get("content")
            if not isinstance(content, list):
                return []
            events = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    events.append(
                        NormalizedEvent(
                            "tool.completed",
                            {
                                "tool_ref": part.get("tool_use_id"),
                                "status": (
                                    "failed"
                                    if part.get("is_error")
                                    else "completed"
                                ),
                            },
                            "user.tool_result",
                        )
                    )
            return events
        if native_type == "result":
            subtype = str(raw.get("subtype") or "")
            detail = str(raw.get("result") or raw.get("error") or "")
            interrupted = any(
                token in subtype.lower()
                for token in ("interrupt", "aborted")
            )
            failed = bool(raw.get("is_error")) or subtype.startswith("error")
            events: list[NormalizedEvent] = []
            usage = self._usage(raw)
            if usage:
                events.append(
                    NormalizedEvent(
                        "usage",
                        {"tokens": usage},
                        "result",
                    )
                )
            result_type = (
                "run.interrupted"
                if interrupted
                else "run.failed" if failed else "run.completed"
            )
            events.append(
                NormalizedEvent(
                    result_type,
                    {
                        "status": (
                            "interrupted"
                            if interrupted
                            else "failed" if failed else "completed"
                        ),
                        "result": detail if detail else None,
                    },
                    "result",
                    "native" if interrupted else None,
                )
            )
            return events
        return []

    def stream(self, turn: NativeTurn) -> Iterator[NormalizedEvent]:
        process = turn.opaque
        stdout = getattr(process, "stdout", None)
        if stdout is None:
            raise AdapterError(
                "HARNESS_PROTOCOL_ERROR",
                "Claude process exposes no stdout stream",
            )
        yield NormalizedEvent(
            "session.started",
            {
                "session_ref": turn.session_ref,
                "resumed": bool(turn.metadata.get("resumed")),
            },
            "session-id-or-resume",
        )
        terminal = False
        for line in stdout:
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AdapterError(
                    "HARNESS_PROTOCOL_ERROR",
                    "Claude emitted invalid stream JSON",
                ) from exc
            native_session = self._session_from(raw)
            if native_session:
                ensure_exact_session(turn.session_ref, native_session)
            for event in self._normalize(raw):
                if event.type in TERMINAL_EVENTS:
                    terminal = True
                    turn.metadata["terminal"] = event.type
                    if event.interrupt_evidence:
                        turn.metadata["interrupt_evidence"] = (
                            event.interrupt_evidence
                        )
                yield event
        returncode = process.wait()
        turn.metadata["returncode"] = returncode
        if not terminal:
            event = NormalizedEvent(
                "run.failed",
                {
                    "error": "stream ended without a terminal result",
                    "exit_code": returncode,
                },
                "process.exit",
            )
            turn.metadata["terminal"] = event.type
            yield event

    def interrupt(self, turn: NativeTurn) -> InterruptResult:
        process = turn.opaque
        if process is None or process.poll() is not None:
            return InterruptResult(False, "Claude process is not running")
        signal_owned_process(process, signal.SIGINT)
        return InterruptResult(True)

    def _session_path(self, session_ref: str, worktree: Path) -> Path:
        root = (
            self.config_dir
            or Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
        )
        # Claude maps the absolute cwd to a project directory by replacing
        # every non-alphanumeric character independently. In particular,
        # ``/.sc-worktrees`` becomes ``--sc-worktrees``; replacing separators
        # alone points at a plausible but nonexistent ``-.sc-worktrees`` path
        # and makes every exact resume look like a lost session.
        project = re.sub(r"[^A-Za-z0-9]", "-", str(worktree))
        return root / "projects" / project / f"{session_ref}.jsonl"

    def inspect(
        self,
        session_ref: str,
        context: ConversationContext,
    ) -> SessionInspection:
        worktree = context.checked_worktree()
        path = self._session_path(session_ref, worktree)
        if not path.is_file():
            return SessionInspection(session_ref, False, "missing")
        last: Mapping[str, Any] | None = None
        seen_session = False
        try:
            with path.open() as stream:
                for line in stream:
                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(raw, dict):
                        continue
                    native_session = self._session_from(raw)
                    if native_session:
                        ensure_exact_session(session_ref, native_session)
                        seen_session = True
                    stored_cwd = raw.get("cwd")
                    if (
                        stored_cwd is not None
                        and Path(str(stored_cwd)).resolve() != worktree
                    ):
                        raise AdapterError(
                            "HARNESS_WORKTREE_MISMATCH",
                            "Claude session belongs to a different worktree",
                        )
                    last = raw
        except OSError as exc:
            raise AdapterError(
                "HARNESS_SESSION_INSPECTION_FAILED",
                f"cannot inspect Claude session: {exc}",
                retryable=True,
            ) from exc
        terminal: str | None = None
        if last and last.get("type") == "result":
            events = self._normalize(last)
            terminal_event = next(
                (event.type for event in events if event.type in TERMINAL_EVENTS),
                None,
            )
            terminal = terminal_event
        return SessionInspection(
            session_ref,
            seen_session or path.is_file(),
            "idle" if terminal else "unknown",
            worktree,
            {
                "path": str(path),
                "terminal": terminal,
                "mtime_ns": path.stat().st_mtime_ns,
            },
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
                f"terminal {terminal} was observed on Claude stdout",
                (
                    turn.metadata.get("interrupt_evidence")
                    if outcome == "cancelled"
                    else None
                ),
            )
        process = turn.opaque
        if process is not None and process.poll() is None:
            return ReconcileResult(
                "running",
                True,
                "Claude process is still running",
            )
        inspection = self.inspect(turn.session_ref, context)
        transcript_terminal = inspection.metadata.get("terminal")
        if transcript_terminal in TERMINAL_EVENTS:
            outcome = terminal_outcome(str(transcript_terminal))
            return ReconcileResult(
                outcome,
                True,
                "Claude transcript contains a terminal result",
                "native" if outcome == "cancelled" else None,
            )
        return ReconcileResult(
            "unknown",
            False,
            "Claude process ended without a terminal stream or transcript result",
        )
