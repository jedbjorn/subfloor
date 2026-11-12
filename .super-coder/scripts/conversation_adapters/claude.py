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
    compaction_event,
    ensure_exact_session,
    ensure_nonempty_message,
    load_manifest,
    managed_mcp_launch_args,
    merged_env,
    signal_owned_process,
    terminal_outcome,
    token_count,
    usage_event,
)

# Claude reads its auto-compact window from this variable before it reads the
# configured one (2.1.322: the resolver returns the env value first), so a
# value in the operator's environment would silently win over the engine's
# ``--autocompact``. The launch drops it whenever it passes the flag.
AUTO_COMPACT_WINDOW_ENV = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
INPUT_SIDE_KEYS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


class ClaudeAdapter(ConversationAdapter):
    harness = "claude"
    # A resumed headless run whose prompt is ``/compact`` (2.1.322). It makes
    # one summarising request, writes a ``compact_boundary`` with trigger
    # ``manual``, and ends with a ``result`` that names ``local_command``.
    manual_compaction = True
    COMPACT_PROMPT = "/compact"

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
        # The model the harness named in ``system.init``; it selects the
        # matching ``modelUsage`` entry when a result states a window.
        self._model: str | None = None

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
        command.extend(managed_mcp_launch_args(self.manifest, context))
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
        # Model default is a reserved binding value, not a native effort name.
        if (
            effort_value
            and effort_value != route_transport.route_bindings.DEFAULT_EFFORT
        ):
            effort = hcfg.get("effort") or {}
            if not effort.get("flag"):
                raise AdapterError(
                    "HARNESS_EFFORT_UNSUPPORTED",
                    "Claude adapter cannot apply effort",
                )
            command.extend([effort["flag"], effort_value])
        compact_window = context.effective_compact_window()
        if compact_window is not None:
            # The value is the window Claude compacts within, not the trigger
            # point; it is passed through as set, with no engine floor.
            command.extend(["--autocompact", str(compact_window)])
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
        compaction: bool = False,
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
        env = merged_env(self.manifest, context)
        if context.effective_compact_window() is not None:
            env.pop(AUTO_COMPACT_WINDOW_ENV, None)
        process = self.runner.spawn(
            command,
            cwd=worktree,
            env=env,
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
                **({"compaction": True} if compaction else {}),
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

    def compact(
        self,
        session_ref: str,
        context: ConversationContext,
    ) -> NativeTurn:
        """A resumed headless run whose whole prompt is ``/compact``."""
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
            self.COMPACT_PROMPT,
            session_ref,
            resume=True,
            compaction=True,
        )

    @staticmethod
    def _compact_refusal(raw: Mapping[str, Any]) -> str | None:
        """Why a ``/compact`` run did not compact, in Claude's own words.

        Two shapes (2.1.322): a ``status`` line whose ``compact_result`` is
        ``failed``, and a locally written assistant message (model
        ``<synthetic>``) such as "Error: No messages to compact". Neither is
        an error ``result``: the run still ends with subtype ``success``.
        """
        if raw.get("type") == "system" and raw.get("subtype") == "status":
            if raw.get("compact_result") != "failed":
                return None
            error = raw.get("compact_error")
            return (
                error.strip()
                if isinstance(error, str) and error.strip()
                else "Claude reported that the compaction failed"
            )
        if raw.get("type") != "assistant":
            return None
        message = raw.get("message")
        if not isinstance(message, dict) or message.get("model") != "<synthetic>":
            return None
        content = message.get("content")
        texts = [
            block["text"].strip()
            for block in (content if isinstance(content, list) else [])
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
        text = " ".join(part for part in texts if part)
        if not text:
            return None
        return text.removeprefix("Error:").strip() or text

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

        The ``result`` of a ``/compact`` run has the same zero-turn shape but
        names the command it ran (``local_command``): it is that run's
        terminal, never a preamble.
        """
        if raw.get("is_error"):
            return False
        if raw.get("local_command"):
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

    @staticmethod
    def _context_tokens(usage: Mapping[str, Any]) -> int | None:
        """Input side of one request: uncached input, cache read, cache write."""
        parts = [token_count(usage.get(key)) for key in INPUT_SIDE_KEYS]
        present = [part for part in parts if part is not None]
        return sum(present) if present else None

    @staticmethod
    def _compaction(
        raw: Mapping[str, Any],
        native_type: str,
    ) -> list[NormalizedEvent]:
        """A compact boundary, in the stream's or the transcript's spelling.

        The stream writes ``compact_metadata`` with ``pre_tokens``; the stored
        transcript writes ``compactMetadata`` with ``preTokens``. Both become
        the same single event, and the summary text is never read.
        """
        if raw.get("type") != "system" or raw.get("subtype") != "compact_boundary":
            return []
        metadata = raw.get("compact_metadata")
        if not isinstance(metadata, dict):
            metadata = raw.get("compactMetadata")
        if not isinstance(metadata, dict):
            metadata = {}

        def field(snake: str, camel: str) -> Any:
            value = metadata.get(snake)
            return metadata.get(camel) if value is None else value

        return [
            compaction_event(
                trigger=metadata.get("trigger"),
                tokens_before=field("pre_tokens", "preTokens"),
                tokens_after=field("post_tokens", "postTokens"),
                compaction_ref=raw.get("uuid"),
                native_type=native_type,
            )
        ]

    def _context_window(self, raw: Mapping[str, Any]) -> int | None:
        """The window a result states for the chat's own model, else None."""
        entry = self._model_usage(raw)
        if entry is None:
            return None
        return token_count(entry.get("contextWindow")) or None

    def _compaction_usage(self, raw: Mapping[str, Any]) -> dict[str, int]:
        """What a ``/compact`` run spent, in the stream's usage spelling.

        Its ``result.usage`` is all zeros (2.1.322): the summarising request
        is counted only in ``modelUsage``, which is that process's total and
        so this run's alone.
        """
        entry = self._model_usage(raw)
        if entry is None:
            return {}
        spelling = {
            "input_tokens": "inputTokens",
            "output_tokens": "outputTokens",
            "cache_read_input_tokens": "cacheReadInputTokens",
            "cache_creation_input_tokens": "cacheCreationInputTokens",
        }
        usage = {
            key: token_count(entry.get(camel)) for key, camel in spelling.items()
        }
        return {key: value for key, value in usage.items() if value is not None}

    def _model_usage(self, raw: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """The ``modelUsage`` entry of the chat's own model, else None.

        ``modelUsage`` is keyed by model and can also hold a delegated
        agent's model. Only an entry that names the chat's model, or the
        single entry of a one-model turn, is read; anything else is unknown.
        """
        model_usage = raw.get("modelUsage")
        if not isinstance(model_usage, dict) or not model_usage:
            return None

        def bare(name: str) -> str:
            return name.split("[", 1)[0]

        entry = None
        if self._model:
            entry = model_usage.get(self._model)
            if entry is None:
                matches = [
                    value
                    for key, value in model_usage.items()
                    if isinstance(key, str) and bare(key) == bare(self._model)
                ]
                entry = matches[0] if len(matches) == 1 else None
        if entry is None and len(model_usage) == 1:
            entry = next(iter(model_usage.values()))
        return entry if isinstance(entry, dict) else None

    def _normalize(
        self,
        raw: Mapping[str, Any],
    ) -> list[NormalizedEvent]:
        native_type = raw.get("type")
        if native_type == "system" and raw.get("subtype") == "init":
            model = raw.get("model")
            if isinstance(model, str) and model:
                self._model = model
            return [
                NormalizedEvent(
                    "run.started",
                    {"status": "running"},
                    "system.init",
                ),
            ]
        if native_type == "system" and raw.get("subtype") == "compact_boundary":
            return self._compaction(raw, "system.compact_boundary")
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
                    # One model request. A delegated agent's request is not
                    # the chat's context, so it carries no meter reading.
                    delegated = raw.get("parent_tool_use_id") is not None
                    return [
                        usage_event(
                            usage,
                            "stream_event.message_delta",
                            scope="subagent" if delegated else "request",
                            context_tokens=self._context_tokens(usage),
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
            # Older builds name the interruption in ``subtype``. 2.1.322
            # reports ``subtype: error_during_execution`` and names the abort
            # in ``terminal_reason`` (``aborted_streaming``) instead. Both are
            # structured fields; an execution error naming neither stays
            # failed.
            terminal_reason = str(raw.get("terminal_reason") or "")
            interrupted = any(
                token in field.lower()
                for field in (subtype, terminal_reason)
                for token in ("interrupt", "aborted")
            )
            failed = bool(raw.get("is_error")) or subtype.startswith("error")
            events: list[NormalizedEvent] = []
            usage = self._usage(raw)
            if raw.get("local_command") == "compact" and not any(usage.values()):
                usage = self._compaction_usage(raw)
            if usage:
                # The result sums every request of the turn: it is not the
                # size of the context, so it carries the window only.
                events.append(
                    usage_event(
                        usage,
                        "result",
                        scope="turn",
                        context_window=self._context_window(raw),
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
        # A manual compaction: the run reports a boundary, or says why not.
        compacting = bool(turn.metadata.get("compaction"))
        compacted = False
        refusal: str | None = None
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
            if compacting and isinstance(raw, dict):
                refusal = self._compact_refusal(raw) or refusal
            for event in self._normalize(raw):
                if compacting and event.type == "context.compacted":
                    compacted = True
                if (
                    compacting
                    and event.type == "run.completed"
                    and refusal
                    and not compacted
                ):
                    event = NormalizedEvent(
                        "run.failed",
                        {
                            "status": "failed",
                            "error": "HARNESS_COMPACT_REFUSED",
                            "detail": refusal,
                        },
                        event.native_type,
                    )
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
        # A compact boundary has no ``message``; it is read before that guard
        # so restart recovery records the compaction the stream would have.
        compaction = self._compaction(raw, "transcript.compact_boundary")
        if compaction:
            return compaction, None
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
        usage = self._usage(raw)
        if usage and not raw.get("isSidechain"):
            events.append(
                usage_event(
                    usage,
                    "assistant.usage",
                    scope="request",
                    context_tokens=self._context_tokens(usage),
                )
            )
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
            if any(event.type == "context.compacted" for event in entry_events):
                # Readings from before this boundary are stale. On a replay
                # the boundary itself may already be recorded and be dropped
                # by the store; re-recording the older readings after it
                # would leave the meter on a pre-compaction figure.
                events = [event for event in events if event.type != "usage"]
            events.extend(entry_events)
            if entry_text is not None:
                text = entry_text
        return offset + end + 1, events, text

    _LOCAL_COMMAND_OUTPUT = re.compile(
        r"<local-command-(stdout|stderr)>(.*?)</local-command-\1>", re.DOTALL
    )

    @classmethod
    def _transcript_refusal(cls, raw: Mapping[str, Any]) -> str | None:
        """Why a ``/compact`` run did not compact, as the transcript keeps it.

        2.1.322 stores the command's own output as a ``system`` entry with
        subtype ``local_command`` and the text in ``content``:
        ``<local-command-stderr>Error: No messages to compact</…>`` on an
        empty session, ``<local-command-stdout>Not enough messages to
        compact.</…>`` on one already compacted. (A compaction that happened
        writes "Compacted" as a ``user`` entry instead, after its boundary.)
        The stream's shapes are read too, for a build that stores those.
        """
        if raw.get("type") == "system" and raw.get("subtype") == "local_command":
            content = raw.get("content")
            if not isinstance(content, str):
                return None
            found = cls._LOCAL_COMMAND_OUTPUT.search(content)
            text = (found.group(2) if found else content).strip()
            if not text or text.startswith("Compacted"):
                return None
            return text.removeprefix("Error:").strip() or text
        return cls._compact_refusal(raw)

    def _compact_transcript_outcome(
        self,
        path: Path,
        offset: int,
    ) -> tuple[bool, str | None]:
        """(compacted, refusal) from what a ``/compact`` run appended.

        Only entries at or after ``offset`` are the run's own: a boundary or
        a reason left by an earlier run proves nothing about this one.
        """
        compacted = False
        refusal: str | None = None
        try:
            with path.open("rb") as stream:
                stream.seek(max(0, offset))
                data = stream.read()
        except FileNotFoundError:
            return False, None
        except OSError as exc:
            raise AdapterError(
                "HARNESS_SESSION_INSPECTION_FAILED",
                f"cannot read Claude session: {exc}",
                retryable=True,
            ) from exc
        for line in data.decode("utf-8", "replace").splitlines():
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            if self._compaction(raw, "transcript.compact_boundary"):
                compacted = True
            else:
                refusal = self._transcript_refusal(raw) or refusal
        return compacted, refusal

    def recovered_events(
        self,
        turn: NativeTurn,
        context: ConversationContext,
    ) -> list[NormalizedEvent]:
        """The compaction a recovered ``/compact`` run wrote, if it wrote one.

        A recovered run whose process is gone is reconciled, not streamed,
        so nothing has passed its boundary on. The marker is read from what
        the run appended; one already recorded from the pipe is dropped by
        the store (the boundary's ``uuid`` names it).
        """
        if not turn.metadata.get("compaction"):
            return []
        path, offset = self._run_transcript(turn, context)
        events: list[NormalizedEvent] = []
        try:
            with path.open("rb") as stream:
                stream.seek(offset)
                data = stream.read()
        except OSError:
            return []
        for line in data.decode("utf-8", "replace").splitlines():
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(raw, dict):
                events.extend(
                    self._compaction(raw, "transcript.compact_boundary")
                )
        return events

    def _run_transcript(
        self,
        turn: NativeTurn,
        context: ConversationContext,
    ) -> tuple[Path, int]:
        """The session file of a turn and where the turn's entries begin."""
        recorded = turn.metadata.get("transcript_path")
        path = (
            Path(str(recorded))
            if recorded
            else self._session_path(turn.session_ref, context.checked_worktree())
        )
        offset = turn.metadata.get("transcript_offset")
        return path, offset if isinstance(offset, int) and offset > 0 else 0

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
        run_offset = offset
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
        # The identity check is the attach lane's process.wait(): the broker
        # clears the run's process identity on this evidence exactly as it
        # does on a returncode, so the reaper never has to record the exit.
        turn.metadata["process_exited"] = True
        if turn.metadata.get("compaction"):
            # A ``/compact`` run writes no reply. What it did is in what it
            # appended: a boundary (already passed on above), or its reason.
            compacted, refusal = self._compact_transcript_outcome(
                path, run_offset
            )
            if refusal and not compacted:
                yield NormalizedEvent(
                    "run.failed",
                    {
                        "status": "failed",
                        "error": "HARNESS_COMPACT_REFUSED",
                        "detail": refusal,
                    },
                    "process.exit",
                )
                return
            yield NormalizedEvent(
                "run.completed", {"status": "completed"}, "process.exit"
            )
            return
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

    @staticmethod
    def _is_conversation_entry(raw: Mapping[str, Any]) -> bool:
        """Whether a transcript entry is a user or assistant message.

        Claude appends bookkeeping entries after a turn's reply (2.1.322:
        ``last-prompt``, ``mode``, ``cost-state``; also attachments and
        request records). The test is positive, so an entry type this code
        has never seen is bookkeeping too and cannot hide the reply.
        """
        return raw.get("type") in ("user", "assistant") and isinstance(
            raw.get("message"), dict
        )

    def inspect(
        self,
        session_ref: str,
        context: ConversationContext,
    ) -> SessionInspection:
        return self._inspect_since(session_ref, context, 0)

    def _inspect_since(
        self,
        session_ref: str,
        context: ConversationContext,
        since: int,
    ) -> SessionInspection:
        """Inspect a session, reading its final state from byte ``since`` on.

        The whole file is still checked for session and worktree identity.
        Only an entry that starts at or after ``since`` can be the final
        conversation entry, so a reply written before a run's launch cannot
        stand in for that run's own reply.
        """
        worktree = context.checked_worktree()
        path = self._session_path(session_ref, worktree)
        if not path.is_file():
            return SessionInspection(session_ref, False, "missing")
        last: Mapping[str, Any] | None = None
        seen_session = False
        position = 0
        try:
            with path.open("rb") as stream:
                for data in stream:
                    start = position
                    position += len(data)
                    line = data.decode("utf-8", "replace")
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
                    if start >= since and self._is_conversation_entry(raw):
                        last = raw
        except OSError as exc:
            raise AdapterError(
                "HARNESS_SESSION_INSPECTION_FAILED",
                f"cannot inspect Claude session: {exc}",
                retryable=True,
            ) from exc
        # A Claude session file never contains a ``result`` entry; the reply
        # that ends a turn is the last assistant text block with no tool call
        # after it. ``last`` is the last conversation entry, not the last
        # line: 2.1.322 writes bookkeeping after the reply. A synthetic
        # API-error entry is assistant text too, and is not a reply.
        terminal: str | None = None
        if last is not None and not last.get("isApiErrorMessage"):
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
        if turn.metadata.get("compaction"):
            # A ``/compact`` run writes no reply, so the reply test below
            # cannot settle it: its boundary or its reason does.
            path, since = self._run_transcript(turn, context)
            compacted, refusal = self._compact_transcript_outcome(path, since)
            if compacted:
                return ReconcileResult(
                    "succeeded",
                    True,
                    "Claude transcript contains this run's compact boundary",
                )
            if refusal:
                return ReconcileResult("failed", True, refusal)
            return ReconcileResult(
                "unknown",
                False,
                "Claude /compact run left neither a compact boundary nor a "
                "reason in its transcript",
            )
        # Only what this run appended can prove it completed: after a broker
        # restart the previous turn's reply is still the file's last message
        # if the process died before writing anything.
        offset = turn.metadata.get("transcript_offset")
        inspection = self._inspect_since(
            turn.session_ref,
            context,
            offset if isinstance(offset, int) and offset > 0 else 0,
        )
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
