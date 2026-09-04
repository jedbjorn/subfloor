#!/usr/bin/env python3
"""Claude process-per-turn conversation adapter."""
from __future__ import annotations

import json
import os
import re
import signal
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Iterator, Mapping

import active_chat_registry
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
    ProcessRunner,
    ReconcileResult,
    SessionInspection,
    SubprocessRunner,
    command_version,
    ensure_exact_session,
    ensure_nonempty_message,
    load_manifest,
    managed_mcp_launch_args,
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
        attach_poll_seconds: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        signal_group: Callable[[int, signal.Signals], None] = os.killpg,
    ) -> None:
        super().__init__(manifest or load_manifest(self.harness))
        self.runner = runner or SubprocessRunner()
        self.config_dir = config_dir
        self.attach_poll_seconds = attach_poll_seconds
        self.sleep = sleep
        self.signal_group = signal_group

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
        command.extend(managed_mcp_launch_args(self.manifest))
        prompt_flag = hcfg.get("prompt_flag", "-p")
        command.extend([prompt_flag, message])
        command.extend(self.manifest["conversation"]["start"]["stream_flags"])
        session = self.manifest["conversation"][
            "resume" if resume else "start"
        ]["session_flag"]
        command.extend([session, session_ref])
        try:
            projection = route_transport.context_projection(context, self.harness)
        except route_transport.route_bindings.RouteResolutionError as exc:
            raise AdapterError(
                getattr(exc, "code", "HARNESS_ROUTE_INVALID"), str(exc)
            ) from exc
        model = projection.model if projection is not None else context.model
        effort_value = projection.effort if projection is not None else context.effort
        if model:
            model_flag = hcfg.get("model_flag")
            if not model_flag:
                raise AdapterError(
                    "HARNESS_MODEL_ROUTE_INVALID",
                    "Claude adapter cannot apply a model",
                )
            command.extend([model_flag, model])
        if effort_value:
            effort = hcfg.get("effort") or {}
            if not effort.get("flag"):
                raise AdapterError(
                    "HARNESS_EFFORT_UNSUPPORTED",
                    "Claude adapter cannot apply effort",
                )
            command.extend([effort["flag"], effort_value])
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
        # The transcript position is read before the spawn: anything the child
        # appends belongs to this turn and must survive a lost pipe.
        transcript = self._session_path(session_ref, worktree)
        transcript_offset = (
            transcript.stat().st_size if transcript.is_file() else 0
        )
        process = self.runner.spawn(
            context.execution_argv(command),
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
            metadata={
                "command": command,
                "resumed": resume,
                "transcript_path": str(transcript),
                "transcript_offset": transcript_offset,
            },
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
    def _is_resume_preamble(raw: Mapping[str, Any]) -> bool:
        """A ``result`` that closes no turn (#1497).

        On ``--resume``, Claude Code (observed on 2.1.260) first flushes a
        background-task notification left by the previous turn as its own
        queued turn and emits a ``result`` for it — ``num_turns`` 0, no
        ``stop_reason``, zero usage — before the prompt's turn begins. Treating
        it as terminal completed the run with an empty reply and left the
        child running the real turn unobserved. The prompt's own ``result``
        follows in the same process and remains the terminal.
        """
        if raw.get("is_error"):
            return False
        if str(raw.get("subtype") or "").startswith("error"):
            return False
        return raw.get("num_turns") == 0 and raw.get("stop_reason") is None

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
            if self._is_resume_preamble(raw):
                return []
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

    @staticmethod
    def _identity_alive(turn: NativeTurn) -> bool:
        pid = turn.metadata.get("process_pid")
        start_ticks = turn.metadata.get("process_start_ticks")
        if not isinstance(pid, int) or not isinstance(start_ticks, int):
            return False
        return active_chat_registry.process_identity(str(pid)) == (
            pid,
            start_ticks,
        )

    def _transcript_events(
        self,
        raw: Mapping[str, Any],
    ) -> tuple[list[NormalizedEvent], str | None]:
        """Normalize one transcript entry; report its assistant text."""
        message = raw.get("message")
        if not isinstance(message, dict):
            return [], None
        if raw.get("type") == "user":
            return self._normalize(raw), None
        if raw.get("type") != "assistant":
            return [], None
        content = message.get("content")
        if not isinstance(content, list):
            return [], None
        events: list[NormalizedEvent] = []
        text: str | None = None
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(
                block.get("text"), str
            ):
                text = block["text"]
                events.append(
                    NormalizedEvent(
                        "assistant.delta",
                        {"text": text},
                        "assistant.text",
                    )
                )
            elif block.get("type") == "tool_use":
                events.append(
                    NormalizedEvent(
                        "tool.started",
                        {"tool_ref": block.get("id"), "name": block.get("name")},
                        "assistant.tool_use",
                    )
                )
        return events, text

    def _drain_transcript(
        self,
        path: Path,
        offset: int,
    ) -> tuple[int, list[NormalizedEvent], str | None]:
        """Consume only whole lines, so a half-written entry is read next."""
        try:
            with path.open("rb") as stream:
                stream.seek(offset)
                data = stream.read()
        except FileNotFoundError:
            return offset, [], None
        except OSError as exc:
            raise AdapterError(
                "HARNESS_SESSION_INSPECTION_FAILED",
                f"cannot tail Claude session: {exc}",
                retryable=True,
            ) from exc
        end = data.rfind(b"\n")
        if end < 0:
            return offset, [], None
        events: list[NormalizedEvent] = []
        text: str | None = None
        for line in data[: end + 1].decode("utf-8", "replace").splitlines():
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            entry_events, entry_text = self._transcript_events(raw)
            events.extend(entry_events)
            if entry_text is not None:
                text = entry_text
        return offset + end + 1, events, text

    def attach(self, turn: NativeTurn) -> Iterator[NormalizedEvent]:
        """Re-observe a live turn through the session file it keeps writing."""
        transcript = turn.metadata.get("transcript_path")
        if not transcript or not self._identity_alive(turn):
            raise AdapterError(
                "HARNESS_ATTACH_UNSUPPORTED",
                "Claude attach needs a transcript path and a live process",
            )
        path = Path(str(transcript))
        offset = int(turn.metadata.get("transcript_offset") or 0)
        yield NormalizedEvent(
            "session.started",
            {
                "session_ref": turn.session_ref,
                "resumed": True,
                "attached": True,
            },
            "transcript-attach",
        )
        yield NormalizedEvent("run.started", {"status": "running"}, "transcript-attach")
        last_text: str | None = None
        while True:
            # Liveness is read first so the final drain always follows exit.
            alive = self._identity_alive(turn)
            offset, events, text = self._drain_transcript(path, offset)
            yield from events
            if text is not None:
                last_text = text
            if not alive:
                break
            self.sleep(self.attach_poll_seconds)
        turn.metadata["transcript_offset"] = offset
        if last_text is None:
            yield NormalizedEvent(
                "run.failed",
                {
                    "error": "HARNESS_EXITED_WITHOUT_REPLY",
                    "detail": "Claude exited without writing a reply",
                },
                "process.exit",
            )
            return
        yield NormalizedEvent(
            "run.completed",
            {"status": "completed", "result": last_text},
            "process.exit",
        )

    def interrupt(self, turn: NativeTurn) -> InterruptResult:
        process = turn.opaque
        if process is None:
            # An attached turn has no pipe; its recorded group is the handle.
            group = turn.metadata.get("process_group_id")
            if not isinstance(group, int) or not self._identity_alive(turn):
                return InterruptResult(False, "Claude process is not running")
            self.signal_group(group, signal.SIGINT)
            return InterruptResult(True)
        if process.poll() is not None:
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
                    if stored_cwd is not None and not Path(
                        str(stored_cwd)
                    ).resolve().is_relative_to(worktree):
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
        # A Claude session file never contains a ``result`` entry; the reply
        # that ends a turn is the last assistant text block with no tool call
        # after it.
        terminal: str | None = None
        if last is not None:
            events, text = self._transcript_events(last)
            if text is not None and not any(
                event.type == "tool.started" for event in events
            ):
                terminal = "run.completed"
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
