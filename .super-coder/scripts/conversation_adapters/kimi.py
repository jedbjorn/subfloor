#!/usr/bin/env python3
"""Kimi process-per-turn conversation adapter."""
from __future__ import annotations

import json
import re
import signal
import threading
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

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
    cleanup_owned_process,
    command_version,
    ensure_nonempty_message,
    load_manifest,
    merged_env,
    signal_owned_process,
    terminal_outcome,
)

SESSION_REF = re.compile(r"^session_[0-9a-fA-F-]{36}$")
RUN_REF = re.compile(r"^kimi-(\d+)-(\d+)$")
USAGE_KEYS = {
    "inputOther": "input_tokens",
    "output": "output_tokens",
    "inputCacheRead": "cache_read_tokens",
    "inputCacheCreation": "cache_write_tokens",
}
STDERR_LIMIT = 16384
POLL_INTERVAL = 0.01
COMPLETION_POLL_INTERVAL = 0.05
COMPLETION_DRAIN_SECONDS = 0.1


class KimiAdapter(ConversationAdapter):
    harness = "kimi"

    def __init__(
        self,
        *,
        runner: ProcessRunner | None = None,
        manifest: Mapping[str, Any] | None = None,
        sessions_root: Path | None = None,
        identity_timeout: float = 5.0,
    ) -> None:
        super().__init__(manifest or load_manifest(self.harness))
        if identity_timeout <= 0:
            raise ValueError("identity_timeout must be positive")
        self.runner = runner or SubprocessRunner(start_new_session=True)
        self.sessions_root = sessions_root
        self.identity_timeout = identity_timeout

    def probe(self) -> ProbeResult:
        launch = self.manifest["headless"]["launch"][0]
        return self._probe_result(command_version([launch, "--version"]))

    @staticmethod
    def _validate_session_ref(session_ref: str) -> str:
        if not isinstance(session_ref, str) or not SESSION_REF.fullmatch(
            session_ref
        ):
            raise AdapterError(
                "HARNESS_SESSION_LOST",
                f"Kimi session ref is malformed: {session_ref}",
            )
        return session_ref

    def _environment(
        self,
        context: ConversationContext,
    ) -> tuple[dict[str, str], Path]:
        try:
            projection = route_transport.context_projection(context, self.harness)
        except route_transport.route_bindings.RouteResolutionError as exc:
            raise AdapterError(
                getattr(exc, "code", "HARNESS_ROUTE_INVALID"), str(exc)
            ) from exc
        effort_value = projection.effort if projection is not None else context.effort
        env = merged_env(self.manifest, context)
        effort_env = self.manifest["headless"].get("effort", {}).get("env")
        if effort_env:
            if effort_value:
                env[effort_env] = effort_value
            else:
                env.pop(effort_env, None)
        if self.sessions_root is not None:
            root = self.sessions_root
        else:
            configured = env.get("KIMI_CODE_HOME", "").strip()
            if not configured:
                env.pop("KIMI_CODE_HOME", None)
            merged_home = env.get("HOME", "").strip()
            home = Path(merged_home) if merged_home else Path.home()
            if configured == "~":
                data_root = home
            elif configured.startswith("~/"):
                data_root = home / configured[2:]
            elif configured:
                data_root = Path(configured)
                if not data_root.is_absolute():
                    data_root = context.worktree / data_root
            else:
                data_root = home / ".kimi-code"
            root = data_root / "sessions"
        return env, root.expanduser().resolve()

    def _command(
        self,
        context: ConversationContext,
        message: str,
        session_ref: str | None,
    ) -> list[str]:
        try:
            projection = route_transport.context_projection(context, self.harness)
        except route_transport.route_bindings.RouteResolutionError as exc:
            raise AdapterError(
                getattr(exc, "code", "HARNESS_ROUTE_INVALID"), str(exc)
            ) from exc
        model = projection.model if projection is not None else context.model
        hcfg = self.manifest["headless"]
        command = list(hcfg["launch"])
        command.extend([hcfg.get("prompt_flag", "-p"), message])
        command.extend(self.manifest["conversation"]["start"]["stream_flags"])
        if session_ref is not None:
            resume_flag = self.manifest["conversation"]["resume"][
                "session_flag"
            ]
            command.extend([resume_flag, session_ref])
        if model:
            model_flag = hcfg.get("model_flag")
            if not model_flag:
                raise AdapterError(
                    "HARNESS_MODEL_ROUTE_INVALID",
                    "Kimi adapter cannot apply a model",
                )
            command.extend([model_flag, model])
        return command

    @staticmethod
    def _session_dirs(root: Path) -> set[Path]:
        try:
            return {
                path.resolve()
                for path in root.glob("wd_*/session_*")
                if path.is_dir()
            }
        except OSError:
            return set()

    @staticmethod
    def _state_worktree(session_dir: Path) -> Path:
        state_path = session_dir / "state.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise AdapterError(
                "HARNESS_SESSION_INSPECTION_FAILED",
                f"cannot read Kimi session state: {state_path}",
                retryable=True,
            ) from exc
        if not isinstance(state, dict):
            raise AdapterError(
                "HARNESS_SESSION_INSPECTION_FAILED",
                f"Kimi session state has no workDir or cwd: {state_path}",
            )
        stored_paths: dict[str, Path] = {}
        for field in ("workDir", "cwd"):
            if field not in state:
                continue
            stored = state[field]
            if not isinstance(stored, str) or not stored:
                raise AdapterError(
                    "HARNESS_SESSION_INSPECTION_FAILED",
                    f"Kimi session {field} is invalid: {state_path}",
                )
            stored_path = Path(stored)
            if not stored_path.is_absolute():
                raise AdapterError(
                    "HARNESS_SESSION_INSPECTION_FAILED",
                    f"Kimi session {field} is not absolute: {state_path}",
                )
            try:
                stored_paths[field] = stored_path.resolve()
            except (OSError, RuntimeError, ValueError) as exc:
                raise AdapterError(
                    "HARNESS_SESSION_INSPECTION_FAILED",
                    f"cannot resolve Kimi session {field}: {state_path}",
                    retryable=True,
                ) from exc
        if not stored_paths:
            raise AdapterError(
                "HARNESS_SESSION_INSPECTION_FAILED",
                f"Kimi session state has no workDir or cwd: {state_path}",
            )
        resolved = set(stored_paths.values())
        if len(resolved) != 1:
            raise AdapterError(
                "HARNESS_SESSION_INSPECTION_FAILED",
                f"Kimi session workDir and cwd conflict: {state_path}",
            )
        return next(iter(resolved))

    @staticmethod
    def _wire_path(session_dir: Path) -> Path:
        return session_dir / "agents" / "main" / "wire.jsonl"

    @staticmethod
    def _wire_records(
        wire: Path,
        *,
        start_offset: int = 0,
    ) -> list[tuple[int, Mapping[str, Any]]]:
        records: list[tuple[int, Mapping[str, Any]]] = []
        try:
            with wire.open("rb") as stream:
                stream.seek(start_offset)
                while True:
                    offset = stream.tell()
                    line = stream.readline()
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        break
                    try:
                        raw = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if isinstance(raw, dict):
                        records.append((offset, raw))
        except OSError as exc:
            raise AdapterError(
                "HARNESS_SESSION_INSPECTION_FAILED",
                f"cannot read Kimi main wire: {wire}",
                retryable=True,
            ) from exc
        return records

    @staticmethod
    def _prompt_text(raw: Mapping[str, Any]) -> str | None:
        prompt_input = raw.get("input")
        if not isinstance(prompt_input, list):
            return None
        parts = [
            part.get("text")
            for part in prompt_input
            if isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        ]
        return "".join(parts) if parts else None

    @classmethod
    def _prompt_markers(cls, wire: Path) -> set[tuple[int, int]]:
        markers: set[tuple[int, int]] = set()
        for offset, raw in cls._wire_records(wire):
            if raw.get("type") != "turn.prompt":
                continue
            prompt_time = raw.get("time")
            if isinstance(prompt_time, int) and not isinstance(
                prompt_time, bool
            ):
                markers.add((prompt_time, offset))
        return markers

    @classmethod
    def _matching_prompts(
        cls,
        wire: Path,
        message: str,
        *,
        start_offset: int,
        known_markers: set[tuple[int, int]],
    ) -> list[tuple[int, int]]:
        matches: list[tuple[int, int]] = []
        for offset, raw in cls._wire_records(
            wire,
            start_offset=start_offset,
        ):
            if raw.get("type") != "turn.prompt":
                continue
            prompt_time = raw.get("time")
            prompt_text = cls._prompt_text(raw)
            if (
                not isinstance(prompt_time, int)
                or isinstance(prompt_time, bool)
                or prompt_text is None
            ):
                raise AdapterError(
                    "HARNESS_SESSION_DISCOVERY_FAILED",
                    "Kimi emitted a malformed turn.prompt marker",
                )
            marker = (prompt_time, offset)
            if marker not in known_markers and prompt_text == message:
                matches.append(marker)
        return matches

    _cleanup_process = staticmethod(cleanup_owned_process)

    def _watch_native_completion(
        self,
        turn: NativeTurn,
        process: Any,
        completed: threading.Event,
        stopped: threading.Event,
    ) -> None:
        wire_path = turn.metadata.get("wire_path")
        if not isinstance(wire_path, str):
            return
        wire = Path(wire_path)
        last_size = -1
        while not stopped.wait(COMPLETION_POLL_INTERVAL):
            poll = getattr(process, "poll", None)
            if callable(poll) and poll() is not None:
                return
            try:
                size = wire.stat().st_size
                if size == last_size:
                    continue
                last_size = size
                records = self._run_slice(turn)
            except (AdapterError, OSError):
                continue
            if not self._completion_proven(records):
                continue
            # Give final stdout metadata already in flight (especially the
            # resume hint) a bounded window to reach the validator.
            if stopped.wait(COMPLETION_DRAIN_SECONDS):
                return
            if turn.metadata.get("identity_mismatch"):
                return
            turn.metadata["native_completion_observed"] = True
            completed.set()
            self._cleanup_process(process, 1.0)
            return

    def _discover_start(
        self,
        root: Path,
        existing: set[Path],
        worktree: Path,
        message: str,
        process: Any,
    ) -> tuple[Path, int, int]:
        deadline = time.monotonic() + self.identity_timeout
        last_store_error: AdapterError | None = None
        while True:
            matches: list[tuple[Path, int, int]] = []
            for session_dir in sorted(self._session_dirs(root) - existing):
                if not SESSION_REF.fullmatch(session_dir.name):
                    continue
                try:
                    stored_worktree = self._state_worktree(session_dir)
                except AdapterError as exc:
                    last_store_error = exc
                    continue
                if stored_worktree != worktree:
                    continue
                wire = self._wire_path(session_dir)
                try:
                    prompts = self._matching_prompts(
                        wire,
                        message,
                        start_offset=0,
                        known_markers=set(),
                    )
                except AdapterError as exc:
                    last_store_error = exc
                    continue
                matches.extend(
                    (session_dir, prompt_time, offset)
                    for prompt_time, offset in prompts
                )
            if len(matches) > 1:
                raise AdapterError(
                    "HARNESS_SESSION_DISCOVERY_FAILED",
                    "multiple new Kimi sessions matched the dispatched prompt",
                )
            if matches:
                return matches[0]
            poll = getattr(process, "poll", None)
            if callable(poll) and poll() is not None:
                detail = (
                    last_store_error.detail
                    if last_store_error is not None
                    else "Kimi exited before native identity was captured"
                )
                raise AdapterError(
                    "HARNESS_SESSION_DISCOVERY_FAILED",
                    detail,
                )
            if time.monotonic() >= deadline:
                detail = (
                    last_store_error.detail
                    if last_store_error is not None
                    else "timed out discovering the new Kimi session"
                )
                raise AdapterError(
                    "HARNESS_SESSION_DISCOVERY_FAILED",
                    detail,
                    retryable=True,
                )
            time.sleep(POLL_INTERVAL)

    def _matching_session_dir(
        self,
        root: Path,
        session_ref: str,
    ) -> Path | None:
        try:
            matches = [
                path.resolve()
                for path in root.glob(f"wd_*/{session_ref}")
                if path.is_dir()
            ]
        except OSError as exc:
            raise AdapterError(
                "HARNESS_SESSION_INSPECTION_FAILED",
                f"cannot search Kimi session store: {root}",
                retryable=True,
            ) from exc
        if len(matches) > 1:
            raise AdapterError(
                "HARNESS_SESSION_INSPECTION_FAILED",
                f"Kimi session ref is ambiguous: {session_ref}",
            )
        return matches[0] if matches else None

    def _discover_resume(
        self,
        wire: Path,
        start_offset: int,
        known_markers: set[tuple[int, int]],
        message: str,
        process: Any,
    ) -> tuple[int, int]:
        deadline = time.monotonic() + self.identity_timeout
        last_store_error: AdapterError | None = None
        while True:
            try:
                if wire.stat().st_size < start_offset:
                    raise AdapterError(
                        "HARNESS_SESSION_DISCOVERY_FAILED",
                        "Kimi main wire shrank during resume",
                    )
                matches = self._matching_prompts(
                    wire,
                    message,
                    start_offset=start_offset,
                    known_markers=known_markers,
                )
            except (OSError, AdapterError) as exc:
                last_store_error = (
                    exc
                    if isinstance(exc, AdapterError)
                    else AdapterError(
                        "HARNESS_SESSION_INSPECTION_FAILED",
                        f"cannot inspect Kimi main wire: {wire}",
                        retryable=True,
                    )
                )
                matches = []
            if len(matches) > 1:
                raise AdapterError(
                    "HARNESS_SESSION_DISCOVERY_FAILED",
                    "multiple Kimi prompt markers matched the resumed turn",
                )
            if matches:
                return matches[0]
            poll = getattr(process, "poll", None)
            if callable(poll) and poll() is not None:
                detail = (
                    last_store_error.detail
                    if last_store_error is not None
                    else "Kimi exited before resumed turn identity was captured"
                )
                raise AdapterError(
                    "HARNESS_SESSION_DISCOVERY_FAILED",
                    detail,
                )
            if time.monotonic() >= deadline:
                detail = (
                    last_store_error.detail
                    if last_store_error is not None
                    else "timed out discovering the resumed Kimi turn"
                )
                raise AdapterError(
                    "HARNESS_SESSION_DISCOVERY_FAILED",
                    detail,
                    retryable=True,
                )
            time.sleep(POLL_INTERVAL)

    def _native_turn(
        self,
        process: Any,
        worktree: Path,
        session_dir: Path,
        prompt_time: int,
        prompt_offset: int,
        command: list[str],
        *,
        resumed: bool,
    ) -> NativeTurn:
        wire = self._wire_path(session_dir)
        pid = getattr(process, "pid", None)
        return NativeTurn(
            harness=self.harness,
            session_ref=session_dir.name,
            run_ref=f"kimi-{prompt_time}-{prompt_offset}",
            worktree=worktree,
            process_ref=str(pid) if pid is not None else None,
            metadata={
                "command": command,
                "resumed": resumed,
                "session_path": str(session_dir),
                "wire_path": str(wire),
                "prompt_offset": prompt_offset,
                "prompt_time": prompt_time,
            },
            opaque=process,
        )

    def start(self, context: ConversationContext, message: str) -> NativeTurn:
        worktree = context.checked_worktree()
        if context.permission_mode == "interactive":
            raise AdapterError(
                "HARNESS_PERMISSION_RESPONSE_UNSUPPORTED",
                "Kimi prompt mode cannot bridge interactive permissions",
            )
        message = ensure_nonempty_message(message)
        env, root = self._environment(context)
        existing = self._session_dirs(root)
        command = self._command(context, message, None)
        process = self.runner.spawn(command, cwd=worktree, env=env)
        try:
            session_dir, prompt_time, prompt_offset = self._discover_start(
                root,
                existing,
                worktree,
                message,
                process,
            )
        except Exception:
            self._cleanup_process(process, self.identity_timeout)
            raise
        return self._native_turn(
            process,
            worktree,
            session_dir,
            prompt_time,
            prompt_offset,
            command,
            resumed=False,
        )

    def resume(
        self,
        session_ref: str,
        context: ConversationContext,
        message: str,
    ) -> NativeTurn:
        session_ref = self._validate_session_ref(session_ref)
        worktree = context.checked_worktree()
        if context.permission_mode == "interactive":
            raise AdapterError(
                "HARNESS_PERMISSION_RESPONSE_UNSUPPORTED",
                "Kimi prompt mode cannot bridge interactive permissions",
            )
        message = ensure_nonempty_message(message)
        env, root = self._environment(context)
        session_dir = self._matching_session_dir(root, session_ref)
        if session_dir is None:
            raise AdapterError(
                "HARNESS_SESSION_LOST",
                f"Kimi session does not exist: {session_ref}",
            )
        if self._state_worktree(session_dir) != worktree:
            raise AdapterError(
                "HARNESS_WORKTREE_MISMATCH",
                "Kimi session belongs to a different worktree",
            )
        wire = self._wire_path(session_dir)
        try:
            start_offset = wire.stat().st_size
        except OSError as exc:
            raise AdapterError(
                "HARNESS_SESSION_INSPECTION_FAILED",
                f"cannot inspect Kimi main wire: {wire}",
                retryable=True,
            ) from exc
        known_markers = self._prompt_markers(wire)
        command = self._command(context, message, session_ref)
        process = self.runner.spawn(command, cwd=worktree, env=env)
        try:
            prompt_time, prompt_offset = self._discover_resume(
                wire,
                start_offset,
                known_markers,
                message,
                process,
            )
        except Exception:
            self._cleanup_process(process, self.identity_timeout)
            raise
        return self._native_turn(
            process,
            worktree,
            session_dir,
            prompt_time,
            prompt_offset,
            command,
            resumed=True,
        )

    @staticmethod
    def _normalize(raw: Mapping[str, Any]) -> list[NormalizedEvent]:
        role = raw.get("role")
        if not isinstance(role, str):
            return []
        if role == "assistant":
            events: list[NormalizedEvent] = []
            content = raw.get("content")
            if isinstance(content, str):
                events.append(
                    NormalizedEvent(
                        "assistant.delta",
                        {"text": content},
                        "assistant",
                    )
                )
            tool_calls = raw.get("tool_calls")
            if isinstance(tool_calls, list):
                for tool_call in tool_calls:
                    if not isinstance(tool_call, dict):
                        continue
                    function = tool_call.get("function")
                    if not isinstance(function, dict):
                        continue
                    tool_ref = tool_call.get("id")
                    name = function.get("name")
                    if not isinstance(tool_ref, str) or not isinstance(
                        name, str
                    ):
                        continue
                    events.append(
                        NormalizedEvent(
                            "tool.started",
                            {"tool_ref": tool_ref, "name": name},
                            "assistant.tool_call",
                        )
                    )
            return events
        if role == "tool":
            tool_ref = raw.get("tool_call_id")
            if isinstance(tool_ref, str):
                return [
                    NormalizedEvent(
                        "tool.completed",
                        {"tool_ref": tool_ref, "status": "completed"},
                        "tool",
                    )
                ]
        return []

    @staticmethod
    def _parse_run_ref(run_ref: str) -> tuple[int, int]:
        match = RUN_REF.fullmatch(run_ref)
        if not match:
            raise AdapterError(
                "HARNESS_SESSION_INSPECTION_FAILED",
                f"Kimi run ref is malformed: {run_ref}",
            )
        return int(match.group(1)), int(match.group(2))

    @classmethod
    def _run_coordinates(cls, turn: NativeTurn) -> tuple[int, int]:
        prompt_time, prompt_offset = cls._parse_run_ref(turn.run_ref)
        if (
            turn.metadata.get("prompt_time") != prompt_time
            or turn.metadata.get("prompt_offset") != prompt_offset
        ):
            raise AdapterError(
                "HARNESS_SESSION_INSPECTION_FAILED",
                "Kimi run metadata does not match its persisted run ref",
            )
        return prompt_time, prompt_offset

    def _restore_recovered_run_metadata(
        self,
        turn: NativeTurn,
        context: ConversationContext,
    ) -> None:
        if turn.metadata.get("recovered") is not True:
            return
        session_ref = self._validate_session_ref(turn.session_ref)
        prompt_time, prompt_offset = self._parse_run_ref(turn.run_ref)
        worktree = context.checked_worktree()
        _env, root = self._environment(context)
        session_dir = self._matching_session_dir(root, session_ref)
        if session_dir is None:
            raise AdapterError(
                "HARNESS_SESSION_LOST",
                f"Kimi session does not exist: {session_ref}",
            )
        if self._state_worktree(session_dir) != worktree:
            raise AdapterError(
                "HARNESS_WORKTREE_MISMATCH",
                "Kimi session belongs to a different worktree",
            )
        restored = {
            "session_path": str(session_dir),
            "wire_path": str(self._wire_path(session_dir)),
            "prompt_time": prompt_time,
            "prompt_offset": prompt_offset,
        }
        for key, value in restored.items():
            existing = turn.metadata.get(key, value)
            if existing != value:
                raise AdapterError(
                    "HARNESS_SESSION_INSPECTION_FAILED",
                    f"Kimi recovered {key} conflicts with persisted identity",
                )
        turn.metadata.update(restored)

    def _run_slice(self, turn: NativeTurn) -> list[Mapping[str, Any]]:
        prompt_time, prompt_offset = self._run_coordinates(turn)
        wire_value = turn.metadata.get("wire_path")
        if not isinstance(wire_value, str):
            raise AdapterError(
                "HARNESS_SESSION_INSPECTION_FAILED",
                "Kimi turn has no persisted main-wire path",
            )
        records = self._wire_records(
            Path(wire_value),
            start_offset=prompt_offset,
        )
        if not records:
            raise AdapterError(
                "HARNESS_SESSION_INSPECTION_FAILED",
                "Kimi run prompt is missing from the main wire",
            )
        first_offset, first = records[0]
        if (
            first_offset != prompt_offset
            or first.get("type") != "turn.prompt"
            or first.get("time") != prompt_time
        ):
            raise AdapterError(
                "HARNESS_SESSION_INSPECTION_FAILED",
                "Kimi run boundary does not identify its prompt marker",
            )
        run: list[Mapping[str, Any]] = []
        for _offset, raw in records:
            if run and raw.get("type") == "turn.prompt":
                break
            run.append(raw)
        return run

    @staticmethod
    def _usage(records: list[Mapping[str, Any]]) -> dict[str, int | float]:
        totals: dict[str, int | float] = {}
        for raw in records:
            if (
                raw.get("type") != "usage.record"
                or raw.get("usageScope") != "turn"
            ):
                continue
            usage = raw.get("usage")
            if not isinstance(usage, dict):
                continue
            for native, normalized in USAGE_KEYS.items():
                value = usage.get(native)
                if isinstance(value, (int, float)) and not isinstance(
                    value, bool
                ):
                    totals[normalized] = totals.get(normalized, 0) + value
        return totals

    @classmethod
    def _completion_proven(
        cls,
        records: list[Mapping[str, Any]],
    ) -> bool:
        """Return whether Kimi durably ended the exact run.

        Kimi writes turn-scoped usage after every model step.  A tool-calling
        step therefore has usage even though the next model step is already
        starting.  Only the final ``end_turn`` step plus its usage proves that
        a persistent child may be cleaned up safely.
        """
        latest_step: str | None = None
        completed = False
        for raw in records:
            if raw.get("type") == "usage.record":
                if raw.get("usageScope") == "turn" and cls._usage([raw]):
                    completed = latest_step == "end_turn"
                continue
            if raw.get("type") != "context.append_loop_event":
                continue
            event = raw.get("event")
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            if event_type == "step.begin":
                latest_step = None
                completed = False
            elif event_type == "step.end":
                finish_reason = event.get("finishReason")
                latest_step = (
                    finish_reason if isinstance(finish_reason, str) else None
                )
                completed = False
        return completed

    @staticmethod
    def _stderr(process: Any) -> str:
        captured = getattr(process, "_sc_conversation_stderr", None)
        if isinstance(captured, list):
            return "".join(str(part) for part in captured)[:STDERR_LIMIT]
        stderr = getattr(process, "stderr", None)
        getvalue = getattr(stderr, "getvalue", None)
        if callable(getvalue):
            return str(getvalue())[:STDERR_LIMIT]
        return ""

    def stream(self, turn: NativeTurn) -> Iterator[NormalizedEvent]:
        process = turn.opaque
        stdout = getattr(process, "stdout", None)
        if stdout is None:
            raise AdapterError(
                "HARNESS_PROTOCOL_ERROR",
                "Kimi process exposes no stdout stream",
            )
        yield NormalizedEvent(
            "session.started",
            {
                "session_ref": turn.session_ref,
                "resumed": bool(turn.metadata.get("resumed")),
            },
            "native-session-store",
        )
        yield NormalizedEvent(
            "run.started",
            {"run_ref": turn.run_ref, "status": "running"},
            "turn.prompt",
        )
        native_completed = threading.Event()
        stop_watcher = threading.Event()
        watcher = threading.Thread(
            target=self._watch_native_completion,
            args=(turn, process, native_completed, stop_watcher),
            name="kimi-native-completion",
            daemon=True,
        )
        watcher.start()
        try:
            for line in stdout:
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(raw, dict):
                    continue
                role = raw.get("role")
                if not isinstance(role, str):
                    continue
                if role == "meta" and raw.get("type") == "session.resume_hint":
                    if raw.get("session_id") != turn.session_ref:
                        turn.metadata["identity_mismatch"] = True
                        raise AdapterError(
                            "HARNESS_SESSION_MISMATCH",
                            "Kimi resume hint differs from persisted session ref",
                        )
                    continue
                for event in self._normalize(raw):
                    yield event
        finally:
            stop_watcher.set()
        returncode = process.wait()
        turn.metadata["returncode"] = returncode
        try:
            records = self._run_slice(turn)
        except AdapterError:
            records = []
        cancelled = any(raw.get("type") == "turn.cancel" for raw in records)
        acknowledged_sigint = (
            bool(turn.metadata.get("interrupt_acknowledged"))
            and returncode in {-signal.SIGINT, 128 + signal.SIGINT}
        )
        if cancelled or acknowledged_sigint:
            event = NormalizedEvent(
                "run.interrupted",
                {"status": "interrupted"},
                "turn.cancel" if cancelled else "process.exit",
                "native" if cancelled else "operator",
            )
            turn.metadata["terminal"] = event.type
            turn.metadata["interrupt_evidence"] = event.interrupt_evidence
            yield event
            return
        if native_completed.is_set() or returncode == 0:
            usage = self._usage(records)
            if usage:
                yield NormalizedEvent(
                    "usage",
                    {"tokens": usage},
                    "usage.record",
                )
            event = NormalizedEvent(
                "run.completed",
                {"status": "completed"},
                (
                    "usage.record"
                    if native_completed.is_set()
                    else "process.exit"
                ),
            )
            turn.metadata["terminal"] = event.type
            yield event
            return
        event = NormalizedEvent(
            "run.failed",
            {
                "status": "failed",
                "exit_code": returncode,
                "error": self._stderr(process),
            },
            "process.exit",
        )
        turn.metadata["terminal"] = event.type
        yield event

    def interrupt(self, turn: NativeTurn) -> InterruptResult:
        process = turn.opaque
        if process is None or process.poll() is not None:
            return InterruptResult(False, "Kimi process is not running")
        signal_owned_process(process, signal.SIGINT)
        turn.metadata["interrupt_acknowledged"] = True
        return InterruptResult(True)

    def inspect(
        self,
        session_ref: str,
        context: ConversationContext,
    ) -> SessionInspection:
        session_ref = self._validate_session_ref(session_ref)
        worktree = context.checked_worktree()
        _env, root = self._environment(context)
        session_dir = self._matching_session_dir(root, session_ref)
        if session_dir is None:
            return SessionInspection(session_ref, False, "missing")
        if self._state_worktree(session_dir) != worktree:
            raise AdapterError(
                "HARNESS_WORKTREE_MISMATCH",
                "Kimi session belongs to a different worktree",
            )
        wire = self._wire_path(session_dir)
        records = self._wire_records(wire)
        model: str | None = None
        effort: str | None = None
        last_prompt: str | None = None
        last_prompt_time: int | None = None
        last_prompt_offset: int | None = None
        latest_slice: list[Mapping[str, Any]] = []
        for offset, raw in records:
            native_type = raw.get("type")
            if native_type in {"config.update", "llm.request"}:
                if isinstance(raw.get("modelAlias"), str):
                    model = raw["modelAlias"]
                if isinstance(raw.get("thinkingEffort"), str):
                    effort = raw["thinkingEffort"]
            if native_type == "turn.prompt":
                prompt_time = raw.get("time")
                prompt_text = self._prompt_text(raw)
                if (
                    not isinstance(prompt_time, int)
                    or isinstance(prompt_time, bool)
                    or prompt_text is None
                ):
                    raise AdapterError(
                        "HARNESS_SESSION_INSPECTION_FAILED",
                        "Kimi main wire has a malformed turn.prompt marker",
                    )
                last_prompt = prompt_text
                last_prompt_time = prompt_time
                last_prompt_offset = offset
                latest_slice = [raw]
            elif latest_slice:
                latest_slice.append(raw)
        terminal = any(
            raw.get("type") == "turn.cancel" for raw in latest_slice
        ) or self._completion_proven(latest_slice)
        return SessionInspection(
            session_ref,
            True,
            "idle" if terminal else "unknown",
            worktree,
            {
                "session_path": str(session_dir),
                "wire_path": str(wire),
                "model": model,
                "effort": effort,
                "last_prompt": last_prompt,
                "last_prompt_time": last_prompt_time,
                "last_prompt_offset": last_prompt_offset,
            },
        )

    def reconcile(
        self,
        turn: NativeTurn,
        context: ConversationContext,
    ) -> ReconcileResult:
        terminal = turn.metadata.get("terminal")
        if terminal in TERMINAL_EVENTS:
            outcome = terminal_outcome(str(terminal))
            return ReconcileResult(
                outcome,
                True,
                f"terminal {terminal} was observed on Kimi stdout",
                (
                    turn.metadata.get("interrupt_evidence")
                    if outcome == "cancelled"
                    else None
                ),
            )
        if turn.metadata.get("identity_mismatch"):
            return ReconcileResult(
                "unknown",
                False,
                "Kimi stdout identity mismatched the persisted session",
            )
        process = turn.opaque
        if process is not None and process.poll() is None:
            return ReconcileResult(
                "running",
                True,
                "Kimi process is still running",
            )
        try:
            self._restore_recovered_run_metadata(turn, context)
            records = self._run_slice(turn)
        except AdapterError:
            return ReconcileResult(
                "unknown",
                False,
                "Kimi exact run slice is unavailable",
            )
        if any(raw.get("type") == "turn.cancel" for raw in records):
            return ReconcileResult(
                "cancelled",
                True,
                "Kimi exact run slice contains turn.cancel",
                "native",
            )
        if self._completion_proven(records):
            return ReconcileResult(
                "succeeded",
                True,
                "Kimi exact run slice contains end_turn and turn-scoped usage",
            )
        return ReconcileResult(
            "unknown",
            False,
            "Kimi exact run slice has no durable terminal proof",
        )
