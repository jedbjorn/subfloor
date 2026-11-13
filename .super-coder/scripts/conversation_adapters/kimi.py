#!/usr/bin/env python3
"""Kimi process-per-turn conversation adapter."""
from __future__ import annotations

import json
import queue
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import route_transport
import toml_compat

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
    compaction_event,
    ensure_nonempty_message,
    load_manifest,
    merged_env,
    signal_owned_process,
    terminal_outcome,
    token_count,
    usage_event,
)

SESSION_REF = re.compile(r"^session_[0-9a-fA-F-]{36}$")
RUN_REF = re.compile(r"^kimi-(\d+)-(\d+)$")
USAGE_KEYS = {
    "inputOther": "input_tokens",
    "output": "output_tokens",
    "inputCacheRead": "cache_read_tokens",
    "inputCacheCreation": "cache_write_tokens",
}
# The input side of one model request: what the model re-read, which is the
# size of the chat's context at that request (Feature #95, spec #275).
CONTEXT_KEYS = ("inputOther", "inputCacheRead", "inputCacheCreation")
STDERR_LIMIT = 16384
POLL_INTERVAL = 0.01
COMPLETION_POLL_INTERVAL = 0.05
COMPLETION_DRAIN_SECONDS = 0.1

# Manual compaction (spec #275 gate, AC9a). Kimi Code 2.5.3 runs ``/compact``
# only as a command of its ACP server: as the prompt of a resumed ``kimi -p``
# run it reaches the model as plain text. Over ACP, ``session/prompt`` with
# ``/compact`` answers at once and compacts in the background, so the
# connection is held open until the main wire records the compaction's end;
# closing it earlier cancels the compaction (``full_compaction.cancel``).
COMPACT_PROMPT = "/compact"
COMPACT_BEGIN = "full_compaction.begin"
COMPACT_APPLY = "context.apply_compaction"
COMPACT_COMPLETE = "full_compaction.complete"
COMPACT_CANCEL = "full_compaction.cancel"
COMPACT_SOURCES = {"manual": "manual", "auto": "auto"}
ACP_PROTOCOL_VERSION = 1
ACP_REQUEST_TIMEOUT = 60.0
COMPACTION_TIMEOUT = 900.0
# The first ACP ``session/load`` of a session appends a project-context
# injection to the context a moment after it answers. A compaction that
# begins before it lands is cancelled by Kimi when it does (seen live on
# 2.5.3), so ``/compact`` waits until the main wire has been quiet this long.
ACP_SETTLE_SECONDS = 1.0
ACP_SETTLE_LIMIT_SECONDS = 10.0


class KimiAcpClient:
    """A minimal JSON-RPC client for ``kimi acp`` over stdio.

    Only what a manual compaction needs: requests with responses, the
    agent's message text (the reason a compaction did not start), and a
    refusal of every agent-to-client request, since a compaction asks for
    no permission and no file access.
    """

    def __init__(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float = ACP_REQUEST_TIMEOUT,
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
        self.stderr: list[str] = []
        self.process.__dict__["_sc_conversation_stderr"] = self.stderr
        self.agent_text: list[str] = []
        self._write_lock = threading.Lock()
        self._pending: dict[int, queue.Queue[Any]] = {}
        self._pending_lock = threading.Lock()
        self._next_id = 1
        threading.Thread(
            target=self._drain_stderr, name="kimi-acp-stderr", daemon=True
        ).start()
        threading.Thread(
            target=self._read_loop, name="kimi-acp-reader", daemon=True
        ).start()

    def _drain_stderr(self) -> None:
        stderr = self.process.stderr
        if stderr is None:
            return
        remaining = STDERR_LIMIT
        for line in stderr:
            if remaining <= 0:
                continue
            chunk = line[:remaining]
            self.stderr.append(chunk)
            remaining -= len(chunk)

    def _write(self, message: Mapping[str, Any]) -> None:
        stdin = self.process.stdin
        if stdin is None or stdin.closed:
            raise AdapterError(
                "HARNESS_UNAVAILABLE", "Kimi ACP stdin is closed", retryable=True
            )
        encoded = json.dumps(
            {"jsonrpc": "2.0", **message}, separators=(",", ":")
        )
        with self._write_lock:
            try:
                stdin.write(encoded + "\n")
                stdin.flush()
            except (OSError, ValueError) as exc:
                raise AdapterError(
                    "HARNESS_UNAVAILABLE",
                    f"Kimi ACP write failed: {exc}",
                    retryable=True,
                ) from exc

    def _read_loop(self) -> None:
        stdout = self.process.stdout
        if stdout is not None:
            for line in stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict):
                    self._dispatch(message)
        closed = AdapterError(
            "HARNESS_UNAVAILABLE", "Kimi ACP stream closed", retryable=True
        )
        with self._pending_lock:
            waiters = list(self._pending.values())
        for waiter in waiters:
            waiter.put(closed)

    def _dispatch(self, message: Mapping[str, Any]) -> None:
        request_id = message.get("id")
        method = message.get("method")
        if request_id is not None and method is None:
            with self._pending_lock:
                waiter = self._pending.get(request_id)
            if waiter is not None:
                waiter.put(message)
            return
        if request_id is not None:
            # An agent-to-client request. A compaction needs none of them.
            reply: dict[str, Any] = {
                "id": request_id,
                "error": {"code": -32601, "message": "not supported"},
            }
            if method == "session/request_permission":
                reply = {
                    "id": request_id,
                    "result": {"outcome": {"outcome": "cancelled"}},
                }
            try:
                self._write(reply)
            except AdapterError:
                pass
            return
        if method == "session/update":
            update = (message.get("params") or {}).get("update") or {}
            content = update.get("content") if isinstance(update, dict) else None
            if (
                isinstance(update, dict)
                and update.get("sessionUpdate") == "agent_message_chunk"
                and isinstance(content, dict)
                and isinstance(content.get("text"), str)
            ):
                self.agent_text.append(content["text"])

    def request(
        self,
        method: str,
        params: Mapping[str, Any],
        timeout: float | None = None,
    ) -> Any:
        with self._pending_lock:
            request_id = self._next_id
            self._next_id += 1
            waiter: queue.Queue[Any] = queue.Queue(maxsize=1)
            self._pending[request_id] = waiter
        try:
            self._write({"id": request_id, "method": method, "params": params})
            try:
                response = waiter.get(
                    timeout=self.timeout if timeout is None else timeout
                )
            except queue.Empty as exc:
                raise AdapterError(
                    "HARNESS_TIMEOUT",
                    f"Kimi ACP request timed out: {method}",
                    retryable=True,
                ) from exc
            if isinstance(response, AdapterError):
                raise response
            if "error" in response:
                raise AdapterError(
                    "HARNESS_PROTOCOL_ERROR",
                    f"Kimi ACP {method} failed: {response['error']}",
                )
            return response.get("result")
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def close(self, timeout: float = 5.0) -> None:
        """End the connection: stdin first, so the server exits on its own."""
        stdin = self.process.stdin
        if stdin is not None and not stdin.closed:
            try:
                stdin.close()
            except OSError:
                pass
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass
        if self.process.poll() is None:
            cleanup_owned_process(self.process, 1.0)


AcpFactory = Callable[..., Any]


class KimiAdapter(ConversationAdapter):
    harness = "kimi"
    # Gate passed live on Kimi Code 2.5.3 (spec #275 AC9a): ``/compact`` over
    # ACP ``session/prompt`` writes ``context.apply_compaction`` and this
    # adapter ends the run cleanly. ``/compact`` as a ``kimi -p`` prompt does
    # not compact. Kimi takes no token-count trigger at launch (its own
    # threshold is a ratio in the user's config), so the engine compacts it
    # between turns instead: ``compact_window_native`` stays False.
    manual_compaction = True

    def __init__(
        self,
        *,
        runner: ProcessRunner | None = None,
        manifest: Mapping[str, Any] | None = None,
        sessions_root: Path | None = None,
        identity_timeout: float = 5.0,
        acp_factory: AcpFactory | None = None,
        compaction_timeout: float = COMPACTION_TIMEOUT,
        acp_settle_seconds: float = ACP_SETTLE_SECONDS,
    ) -> None:
        super().__init__(manifest or load_manifest(self.harness))
        if identity_timeout <= 0:
            raise ValueError("identity_timeout must be positive")
        if compaction_timeout <= 0:
            raise ValueError("compaction_timeout must be positive")
        self.runner = runner or SubprocessRunner(start_new_session=True)
        self.sessions_root = sessions_root
        self.identity_timeout = identity_timeout
        self.acp_factory = acp_factory or KimiAcpClient
        self.compaction_timeout = compaction_timeout
        self.acp_settle_seconds = max(0.0, acp_settle_seconds)
        self._acp: Any = None

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
            # Clear inherited overrides when the binding selects Model default.
            if (
                effort_value
                and effort_value != route_transport.route_bindings.DEFAULT_EFFORT
            ):
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
        compaction: bool = False,
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
                # A manual compaction's run is bounded by its
                # ``full_compaction.begin`` record, not by a ``turn.prompt``.
                **({"compaction": True} if compaction else {}),
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

    def _compact_command(self) -> list[str]:
        compact = self.manifest["conversation"].get("compact") or {}
        return [
            *self.manifest["headless"]["launch"],
            *compact.get("command", ["acp"]),
        ]

    def _settle_wire(self, wire: Path) -> int:
        """Wait, bounded, until the main wire stops growing; its size then."""
        limit = time.monotonic() + ACP_SETTLE_LIMIT_SECONDS
        size = -1
        quiet_since = time.monotonic()
        while True:
            try:
                current = wire.stat().st_size
            except OSError:
                current = size
            now = time.monotonic()
            if current != size:
                size, quiet_since = current, now
            if now - quiet_since >= self.acp_settle_seconds or now >= limit:
                return size
            time.sleep(COMPLETION_POLL_INTERVAL)

    def _discover_compaction(
        self,
        wire: Path,
        start_offset: int,
        client: Any,
    ) -> tuple[int, int]:
        """The ``full_compaction.begin`` this request appended to the wire."""
        deadline = time.monotonic() + self.identity_timeout
        process = getattr(client, "process", None)
        while True:
            try:
                records = self._wire_records(wire, start_offset=start_offset)
            except AdapterError:
                records = []
            begins = [
                (raw.get("time"), offset)
                for offset, raw in records
                if raw.get("type") == COMPACT_BEGIN
            ]
            if len(begins) > 1:
                raise AdapterError(
                    "HARNESS_SESSION_DISCOVERY_FAILED",
                    "multiple Kimi compactions began for one request",
                )
            if begins:
                begin_time, offset = begins[0]
                if not isinstance(begin_time, int) or isinstance(begin_time, bool):
                    raise AdapterError(
                        "HARNESS_SESSION_DISCOVERY_FAILED",
                        "Kimi emitted a malformed full_compaction.begin marker",
                    )
                return begin_time, offset
            poll = getattr(process, "poll", None)
            exited = callable(poll) and poll() is not None
            if exited or time.monotonic() >= deadline:
                said = "".join(getattr(client, "agent_text", []) or []).strip()
                raise AdapterError(
                    "HARNESS_COMPACT_REFUSED",
                    said[:2000] or "Kimi started no compaction for /compact",
                )
            time.sleep(POLL_INTERVAL)

    def compact(
        self,
        session_ref: str,
        context: ConversationContext,
    ) -> NativeTurn:
        """``/compact`` through Kimi's ACP server, on the stored session.

        The run is identified by the ``full_compaction.begin`` record the
        request appends to the session's main wire, as a turn is by its
        ``turn.prompt``. The ACP connection stays open, owned by this
        adapter, until ``stream`` sees the compaction end.
        """
        session_ref = self._validate_session_ref(session_ref)
        worktree = context.checked_worktree()
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
        command = self._compact_command()
        try:
            client = self.acp_factory(command, cwd=worktree, env=env)
        except OSError as exc:
            raise AdapterError(
                "HARNESS_UNAVAILABLE",
                f"cannot start Kimi ACP: {exc}",
                retryable=True,
            ) from exc
        try:
            client.request(
                "initialize",
                {
                    "protocolVersion": ACP_PROTOCOL_VERSION,
                    "clientCapabilities": {
                        "fs": {"readTextFile": False, "writeTextFile": False},
                        "terminal": False,
                    },
                    "clientInfo": {"name": "super-coder", "version": "1"},
                },
            )
            client.request(
                "session/load",
                {
                    "sessionId": session_ref,
                    "cwd": str(worktree),
                    "mcpServers": [],
                },
            )
            # Only what /compact appends is this run's: anything the load
            # wrote (its context injection, or a compaction of Kimi's own)
            # lies before this boundary and is never claimed by the run.
            settled = self._settle_wire(wire)
            if settled >= start_offset:
                start_offset = settled
            # What the agent says from here on is its answer to /compact; the
            # load replays the session's history first.
            said = getattr(client, "agent_text", None)
            if isinstance(said, list):
                said.clear()
            client.request(
                "session/prompt",
                {
                    "sessionId": session_ref,
                    "prompt": [{"type": "text", "text": COMPACT_PROMPT}],
                },
            )
            begin_time, begin_offset = self._discover_compaction(
                wire, start_offset, client
            )
        except Exception:
            client.close()
            raise
        self._acp = client
        return self._native_turn(
            client.process,
            worktree,
            session_dir,
            begin_time,
            begin_offset,
            command,
            resumed=True,
            compaction=True,
        )

    def close(self) -> None:
        """Release a compaction's ACP connection the stream did not end."""
        client, self._acp = self._acp, None
        if client is not None:
            client.close()

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
        compaction = bool(turn.metadata.get("compaction"))
        marker = COMPACT_BEGIN if compaction else "turn.prompt"
        first_offset, first = records[0]
        if (
            first_offset != prompt_offset
            or first.get("type") != marker
            or first.get("time") != prompt_time
        ):
            raise AdapterError(
                "HARNESS_SESSION_INSPECTION_FAILED",
                "Kimi run boundary does not identify its prompt marker",
            )
        run: list[Mapping[str, Any]] = []
        for _offset, raw in records:
            native_type = raw.get("type")
            if run and (
                native_type == "turn.prompt"
                or (compaction and native_type == COMPACT_BEGIN)
            ):
                break
            run.append(raw)
            if compaction and native_type in (COMPACT_COMPLETE, COMPACT_CANCEL):
                break
        return run

    def turn_prompt(self, turn: NativeTurn) -> str | None:
        """The prompt text the main wire recorded for this exact turn."""
        return self._prompt_text(self._run_slice(turn)[0])

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

    @staticmethod
    def _model_windows(turn: NativeTurn) -> dict[str, int]:
        """Each model alias's ``max_context_size`` from Kimi's config.

        Read only, from the ``config.toml`` beside the session store this
        turn used; the engine never writes it. Empty when it cannot be read:
        a window that is not known is left out, never guessed.
        """
        session_path = turn.metadata.get("session_path")
        if not isinstance(session_path, str):
            return {}
        config = Path(session_path).parent.parent.parent / "config.toml"
        if not toml_compat.AVAILABLE:
            return {}
        try:
            parsed = toml_compat.loads(config.read_text(encoding="utf-8"))
        except (OSError, RuntimeError, ValueError):
            return {}
        models = parsed.get("models")
        if not isinstance(models, dict):
            return {}
        windows: dict[str, int] = {}
        for alias, model in models.items():
            if not isinstance(model, dict):
                continue
            size = token_count(model.get("max_context_size"))
            if size:
                windows[str(alias)] = size
        return windows

    @staticmethod
    def _request_time(raw: Mapping[str, Any]) -> int | None:
        value = raw.get("time")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        return None

    @classmethod
    def _request_reading(
        cls,
        raw: Mapping[str, Any],
        windows: Mapping[str, int],
    ) -> NormalizedEvent | None:
        """One ``usage`` reading of scope ``request`` for one model step.

        A ``usage.record`` with ``usageScope: turn`` is one model request of
        the chat; ``context_tokens`` is its input side. Kimi's events are
        read after the run ends, so ``created_at`` is ingest time:
        ``requested_at`` carries the record's own ``time`` (epoch ms).
        """
        tokens = cls._usage([raw])
        if not tokens:
            return None
        usage = raw.get("usage")
        sizes = [
            token_count(usage.get(key))
            for key in CONTEXT_KEYS
            if isinstance(usage, dict)
        ]
        present = [size for size in sizes if size is not None]
        model = raw.get("model")
        event = usage_event(
            tokens,
            "usage.record",
            scope="request",
            context_tokens=sum(present) if present else None,
            context_window=windows.get(model) if isinstance(model, str) else None,
        )
        requested_at = cls._request_time(raw)
        if requested_at is None:
            return event
        return NormalizedEvent(
            event.type,
            {**event.payload, "requested_at": requested_at},
            event.native_type,
        )

    @staticmethod
    def _compaction_marker(
        raw: Mapping[str, Any],
        source: Any,
        session_ref: str,
    ) -> NormalizedEvent:
        """``context.apply_compaction`` as ``context.compacted``.

        The figures only: ``summary`` and ``contextSummary`` are never read
        into the event. The trigger is the ``source`` of the compaction's
        ``full_compaction.begin``.
        """
        applied_at = raw.get("time")
        return compaction_event(
            trigger=COMPACT_SOURCES.get(source, "unknown"),
            native_type=COMPACT_APPLY,
            tokens_before=raw.get("tokensBefore"),
            tokens_after=raw.get("tokensAfter"),
            compaction_ref=(
                f"{session_ref}@{applied_at}"
                if isinstance(applied_at, int) and not isinstance(applied_at, bool)
                else None
            ),
        )

    @classmethod
    def _wire_events(
        cls,
        records: list[Mapping[str, Any]],
        windows: Mapping[str, int],
        session_ref: str,
    ) -> list[NormalizedEvent]:
        """The meter readings and compactions of one turn, in wire order.

        Session-scoped usage records (a compaction's own request among them)
        are not requests of the chat and give no reading.
        """
        events: list[NormalizedEvent] = []
        source: Any = None
        for raw in records:
            native_type = raw.get("type")
            if native_type == COMPACT_BEGIN:
                source = raw.get("source")
            elif native_type == COMPACT_APPLY:
                events.append(cls._compaction_marker(raw, source, session_ref))
            elif (
                native_type == "usage.record"
                and raw.get("usageScope") == "turn"
            ):
                reading = cls._request_reading(raw, windows)
                if reading is not None:
                    events.append(reading)
        return events

    @staticmethod
    def _compaction_outcome(
        records: list[Mapping[str, Any]],
    ) -> str | None:
        """``completed``, ``cancelled``, or None while it is still running."""
        for raw in records:
            if raw.get("type") == COMPACT_COMPLETE:
                return "completed"
            if raw.get("type") == COMPACT_CANCEL:
                return "cancelled"
        return None

    @classmethod
    def _compaction_cost(
        cls,
        records: list[Mapping[str, Any]],
        session_ref: str,
    ) -> NormalizedEvent | None:
        """The tokens the compaction's own request spent (any usage scope:
        Kimi records it as ``session``), as one ``compaction`` usage event.

        ``usage_ref`` names the record, so the store keeps one copy when a
        recovered run replays a cost its live stream already recorded.
        """
        totals: dict[str, int | float] = {}
        last_time: int | None = None
        for raw in records:
            if raw.get("type") != "usage.record":
                continue
            usage = raw.get("usage")
            if not isinstance(usage, dict):
                continue
            for native, normalized in USAGE_KEYS.items():
                value = usage.get(native)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    totals[normalized] = totals.get(normalized, 0) + value
            last_time = cls._request_time(raw)
        if not totals:
            return None
        event = usage_event(totals, "usage.record", scope="compaction")
        if last_time is None:
            return event
        return NormalizedEvent(
            event.type,
            {**event.payload, "usage_ref": f"{session_ref}@{last_time}"},
            event.native_type,
        )

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

    def _stream_compaction(self, turn: NativeTurn) -> Iterator[NormalizedEvent]:
        """A manual compaction's run, read from the main wire.

        Waits, bounded, for the compaction's end record while the ACP
        connection stays open, then ends the connection. Completed: the
        marker, the request's own cost, ``run.completed``. Cancelled by
        Kimi: ``run.failed``. No end within the bound: ``run.completed``
        with no marker, which the broker records as not reported.
        """
        process = turn.opaque
        yield NormalizedEvent(
            "session.started",
            {"session_ref": turn.session_ref, "resumed": True},
            "native-session-store",
        )
        yield NormalizedEvent(
            "run.started",
            {"run_ref": turn.run_ref, "status": "running"},
            COMPACT_BEGIN,
        )
        deadline = time.monotonic() + self.compaction_timeout
        records: list[Mapping[str, Any]] = []
        outcome: str | None = None
        exited = False
        while True:
            try:
                records = self._run_slice(turn)
            except AdapterError:
                records = []
            outcome = self._compaction_outcome(records)
            if outcome is not None:
                break
            poll = getattr(process, "poll", None)
            if callable(poll) and poll() is not None:
                if exited:
                    break
                # One more read: the end record may land as the server exits.
                exited = True
                continue
            if time.monotonic() >= deadline:
                break
            time.sleep(COMPLETION_POLL_INTERVAL)
        client = self._acp
        self._acp = None
        if client is not None:
            client.close()
        elif process is not None and process.poll() is None:
            self._cleanup_process(process, 1.0)
        wait = getattr(process, "wait", None)
        returncode = wait() if callable(wait) else None
        turn.metadata["returncode"] = returncode
        if turn.metadata.get("interrupt_acknowledged") and outcome != "completed":
            event = NormalizedEvent(
                "run.interrupted",
                {"status": "interrupted"},
                COMPACT_CANCEL if outcome == "cancelled" else "process.exit",
                "operator",
            )
            turn.metadata["terminal"] = event.type
            turn.metadata["interrupt_evidence"] = event.interrupt_evidence
            yield event
            return
        if outcome == "completed":
            yield from self._wire_events(records, {}, turn.session_ref)
            cost = self._compaction_cost(records, turn.session_ref)
            if cost is not None:
                yield cost
            event = NormalizedEvent(
                "run.completed", {"status": "completed"}, COMPACT_COMPLETE
            )
        elif outcome == "cancelled" or exited:
            said = "".join(getattr(client, "agent_text", []) or []).strip()
            event = NormalizedEvent(
                "run.failed",
                {
                    "status": "failed",
                    "error": "HARNESS_COMPACT_REFUSED",
                    "detail": (
                        said[:2000]
                        or (
                            "Kimi cancelled the compaction"
                            if outcome == "cancelled"
                            else "Kimi ACP exited before the compaction ended"
                        )
                    ),
                    "exit_code": returncode,
                },
                COMPACT_CANCEL if outcome == "cancelled" else "process.exit",
            )
        else:
            # The bounded wait ran out: closing the connection cancelled the
            # compaction, so nothing is reported.
            event = NormalizedEvent(
                "run.completed", {"status": "completed"}, "compaction.timeout"
            )
        turn.metadata["terminal"] = event.type
        yield event

    def stream(self, turn: NativeTurn) -> Iterator[NormalizedEvent]:
        if turn.metadata.get("compaction"):
            yield from self._stream_compaction(turn)
            return
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
        # The context meter and compactions (spec #275): each model request
        # the run made and each compaction Kimi applied, in wire order, on
        # every ending. They are facts of the session whether or not the
        # turn completed.
        if records:
            yield from self._wire_events(
                records, self._model_windows(turn), turn.session_ref
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
                # The whole-turn total, as before, now labelled: it is not
                # the size of the context, so it carries none.
                yield usage_event(usage, "usage.record", scope="turn")
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
        if turn.metadata.get("compaction"):
            ended = self._compaction_outcome(records)
            if ended == "completed":
                return ReconcileResult(
                    "succeeded",
                    True,
                    "Kimi main wire records this compaction's completion",
                )
            if ended == "cancelled":
                return ReconcileResult(
                    "failed",
                    True,
                    "Kimi main wire records this compaction as cancelled",
                )
            return ReconcileResult(
                "unknown",
                False,
                "Kimi main wire has no end for this compaction",
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

    def recovered_events(
        self,
        turn: NativeTurn,
        context: ConversationContext,
    ) -> list[NormalizedEvent]:
        """The compaction a recovered ``/compact`` run wrote, if it wrote one.

        A recovered run whose process is gone is reconciled, not streamed,
        so its marker and its own cost are read from the main wire, as the
        live stream would have yielded them. One already recorded is dropped
        by the store: the marker names its ``compaction_ref`` and the cost
        its ``usage_ref``.
        """
        if not turn.metadata.get("compaction"):
            return []
        try:
            self._restore_recovered_run_metadata(turn, context)
            records = self._run_slice(turn)
        except AdapterError:
            return []
        if self._compaction_outcome(records) != "completed":
            return []
        events = [
            event
            for event in self._wire_events(records, {}, turn.session_ref)
            if event.type == "context.compacted"
        ]
        cost = self._compaction_cost(records, turn.session_ref)
        if cost is not None:
            events.append(cost)
        return events
