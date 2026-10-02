"""Opt-in native foreground Claude driver, owned by one F89 controller.

PTY bytes are drained, never interpreted as task truth. Session-owned hooks and
the exact transcript supply processing/output/terminal evidence. A channel
notification is a write, not a processed receipt. Production ClaudeAdapter is
unchanged; this module does not launch a print process, SDK or native handoff.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import pty
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from conversation_runtime_contract import (
    MAX_FRAME_BYTES,
    DriverStart,
    EventSink,
    Grade,
    NativeCleanup,
    NativeControl,
    NativeReference,
    NativeSnapshot,
    NativeSubmission,
    NativeWork,
    ProcessIdentity,
    RuntimeContext,
    RuntimeContractError,
    RuntimeDriver,
    RuntimeEvent,
    RuntimeIdentity,
    StartupConsent,
    WriteReceipt,
)

CHANNEL = "subfloor_runtime"
REVISION = "f89-claude-foreground-v3"
ASSETS = Path(__file__).resolve().parents[2] / "assets/runtime/claude"
MAX_RECORDS = 4096
MAX_NOTIFICATIONS = 64
TEXT_CHUNK = 4000
STARTUP_BYTES = 8192
AUTH_STATUS_BYTES = 16384
STARTUP_TITLE = "WARNING: Loading development channels"
# Installed 2.1.287 --ax-screen-reader ConfirmCancel emits this finite choice.
# This recognizes one pre-ready dialog, not a terminal/screen model.
STARTUP_CHOICE = re.compile(
    r"WARNING: Loading development channels\s+"
    r"--dangerously-load-development-channels is for local channel development only\. "
    r"Do not use this option to run channels you have downloaded off the internet\.\s+"
    r"Please use --channels to run a list of approved channels\.\s+"
    rf"Channels:\s+server:{CHANNEL}\s+"
    r"y\. I am using this for local development\s+n\. Exit\s+Enter y/n:\s*\Z"
)
STARTUP_ANSI = re.compile(rb"\x1b\[[0-9;?]*[mKGHJhl]")
PROVIDER_OVERRIDES = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_SIMPLE", "CLAUDE_CODE_SAFE_MODE", "CLAUDE_CODE_DISABLE_CLAUDE_MDS",
)
HOOKS = (
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "PostToolUseFailure", "MessageDisplay", "Stop", "StopFailure", "SessionEnd",
    "SubagentStart", "SubagentStop", "ConfigChange",
)


def _parse_auth_status(raw: bytes, returncode: int) -> dict[str, str]:
    """Consume only native route enums; account identifiers never leave here."""
    try:
        value = json.loads(raw) if len(raw) <= AUTH_STATUS_BYTES else None
    except (ValueError, UnicodeError):
        value = None
    if (returncode != 0 or not isinstance(value, dict) or value.get("loggedIn") is not True
            or value.get("authMethod") != "claude.ai" or value.get("apiProvider") != "firstParty"):
        raise RuntimeContractError("NATIVE_ACCOUNT_INCONCLUSIVE", "native subscription route was not observed")
    auth = {"method": "claude.ai", "provider": "firstParty"}
    subscription = value.get("subscriptionType")
    if isinstance(subscription, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", subscription):
        auth["subscription_type"] = subscription
    return auth


def _auth_status(context: RuntimeContext, env: Mapping[str, str], deadline: float, *,
                 cancel: threading.Event | None = None, spawn: Callable[..., Any] | None = None) -> dict[str, str]:
    """Fixed zero-inference observation inside the already-owned native unit."""
    limit = min(deadline, time.monotonic() + 3)
    if time.monotonic() >= limit or cancel is not None and cancel.is_set():
        raise RuntimeContractError("NATIVE_ACCOUNT_INCONCLUSIVE", "native account observation deadline expired")
    try:
        process = (spawn or subprocess.Popen)(context.execution_argv([str(context.executable.path), "auth", "status", "--json"]),
            cwd=context.worktree, env=dict(env), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise RuntimeContractError("NATIVE_ACCOUNT_INCONCLUSIVE", "native account observation unavailable") from exc
    data = bytearray()
    try:
        assert process.stdout is not None
        fd = process.stdout.fileno()
        os.set_blocking(fd, False)
        while True:
            if cancel is not None and cancel.is_set():
                raise RuntimeContractError("NATIVE_ACCOUNT_INCONCLUSIVE", "native account observation cancelled")
            remaining = limit - time.monotonic()
            if remaining <= 0:
                raise RuntimeContractError("NATIVE_ACCOUNT_INCONCLUSIVE", "native account observation deadline expired")
            if not select.select([fd], [], [], min(.05, remaining))[0]:
                continue
            chunk = os.read(fd, min(4096, AUTH_STATUS_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > AUTH_STATUS_BYTES:
                raise RuntimeContractError("NATIVE_ACCOUNT_INCONCLUSIVE", "native account observation exceeded its bound")
        return _parse_auth_status(bytes(data), process.wait(timeout=max(.001, limit - time.monotonic())))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeContractError("NATIVE_ACCOUNT_INCONCLUSIVE", "native account observation unavailable") from exc
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=.2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=.2)
        if process.stdout is not None:
            process.stdout.close()


def process_identity(pid: int) -> ProcessIdentity | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return ProcessIdentity(pid, int(fields[19]))
    except (OSError, IndexError, ValueError):
        return None


def _write_private(path: Path, value: Any) -> None:
    data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _string(value: Any, limit: int = 512) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= limit else None


def _metadata(values: Mapping[str, Any]) -> dict[str, Any]:
    """Keep inventory descriptions bounded; never shorten identity/outcome."""
    result = {key: value for key, value in values.items() if value is None or isinstance(value, (str, bool, int))}
    while len(json.dumps(result).encode()) > 1024:
        strings = [(key, value) for key, value in result.items() if isinstance(value, str) and value]
        if not strings:
            return {"metadata_truncated": True}
        key, value = max(strings, key=lambda pair: len(json.dumps(pair[1])))
        result[key] = value[:len(value) // 2]
        result["metadata_truncated"] = True
    return result


@dataclass
class Activity:
    request_id: str | None
    source: Any
    terminal: bool = False
    stop_seen: bool = False
    texts: set[str] = field(default_factory=set)
    displays: dict[str, tuple[int, str]] = field(default_factory=dict)
    automation_id: str | None = None
    model_messages: set[str] = field(default_factory=set)
    readiness_reply: bool = False
    terminal_status: str | None = None
    terminal_provenance: str | None = None


class ClaudeRuntimeDriver(RuntimeDriver):
    harness = "claude"
    revision = REVISION

    def __init__(self, *, popen: Callable[..., Any] = subprocess.Popen) -> None:
        self._popen = popen
        self._condition = threading.Condition(threading.RLock())
        self._emit_lock = threading.RLock()
        self._emit: EventSink = lambda _event: None
        self._context: RuntimeContext | None = None
        self._identity: RuntimeIdentity | None = None
        self._process: Any = None
        self._master: int | None = None
        self._reader: threading.Thread | None = None
        self._closed = threading.Event()
        self._closing = False
        self._auth_cancel = threading.Event()
        self._auth_process: Any = None
        self._auth_identity: ProcessIdentity | None = None
        self._session_started = False
        self._channel_peer: ProcessIdentity | None = None
        self._ready = False
        self._readiness_nonce = str(uuid.uuid4())
        self._readiness_prompt: str | None = None
        self._readiness_queued = False
        self._readiness_tool_count = 0
        self._native_auth: dict[str, str] = {}
        self._native_model: str | None = None
        self._native_effort: str | None = None
        self._route_inconclusive = False
        self._lost = False
        self._primary: str | None = None
        self._primary_state: Literal["idle", "active", "unknown"] = "unknown"
        self._activities: dict[str, Activity] = {}
        self._requests: dict[str, NativeSubmission] = {}
        self._controls: dict[str, tuple[str, str]] = {}
        self._control_tools: dict[str, str] = {}
        self._pending_results: dict[str, tuple[str, bool, str | None]] = {}
        self._cleanup_controls: set[str] = set()
        self._notifications: deque[dict[str, Any]] = deque()
        self._notification_sequence = 0
        self._pending_dispatch: str | None = None
        self._work: dict[str, NativeWork] = {}
        self._definitions: dict[str, bool | None] = {}
        self._created_definitions: set[str] = set()
        self._snapshot_at = 0.0
        self._snapshot_partial = True
        self._transcript: Path | None = None
        self._transcript_offset = 0
        self._transcript_pending = b""
        self._parent_activity: dict[str, str] = {}
        self._tool_inputs: dict[str, tuple[str, Mapping[str, Any], str | None]] = {}
        self._hook_keys: set[str] = set()
        self._policy_changed = False
        # Volatile diagnostic bytes only; never emitted or persisted as output.
        self._pty_tail: deque[bytes] = deque(maxlen=16)
        self._startup_bytes = b""
        self._startup_setup: StartupConsent | None = None
        self._startup_confirmed = False
        self._configuration_files: tuple[Path, ...] = ()
        self._configuration_sha256 = ""

    def _capabilities(self) -> dict[str, Grade]:
        evidence = self._context.capability_evidence if self._context else {}
        return {name: "inconclusive" if self._policy_changed or self._route_inconclusive else evidence.get(name, "unverified") for name in (
            "submission", "background_tasks", "stop_work", "stop_reply", "automation",
        )} | {"direct_task_control": "incompatible", "continuous_inventory": "incompatible"}

    def _allowed(self, capability: str) -> bool:
        context = self._context
        return bool(context and not self._policy_changed and not self._route_inconclusive and (
            context.capability_evidence.get(capability) == "compatible"
            or capability in context.probe_capabilities
        ))

    def _send_event(self, event: RuntimeEvent) -> None:
        with self._emit_lock:
            self._emit(event)

    def _reference(self, activity: str | None = None, *, work: str | None = None,
                   item: str | None = None) -> NativeReference:
        assert self._identity is not None
        return NativeReference(root_id=self._identity.root_id, activity_id=activity,
                               work_id=work, item_id=item)

    def _fail_observation(self, reason: str, *, ownership: bool = False) -> None:
        self._snapshot_partial = True
        self._primary_state = "unknown"
        if ownership:
            self._lost = True
        self._send_event(RuntimeEvent(
            "ownership.failed" if ownership else "snapshot.observed",
            reference=self._reference() if self._identity else None,
            freshness="stale", partial=True, grade="inconclusive",
            provenance="claude:validator", data={"reason": reason},
        ))

    def _prepare(self, context: RuntimeContext) -> tuple[list[str], dict[str, str]]:
        if context.harness != "claude" or context.managed_mcp_args:
            raise RuntimeContractError("CONTEXT_INVALID", "Claude needs canonical MCP files")
        if context.driver_revision != REVISION:
            raise RuntimeContractError("DRIVER_CHANGED", "captured driver revision differs")
        if context.provider not in (None, "anthropic", "claude", "claude.ai", "native"):
            raise RuntimeContractError("ROUTE_UNAVAILABLE", "native Claude subscription route required")
        if context.controller_endpoint is None:
            raise RuntimeContractError("ASSET_UNAVAILABLE", "private controller ingress required")
        if not context.worktree.is_dir():
            raise RuntimeContractError("BOOT_INVALID", "canonical worktree is missing")
        boot = context.boot_content.encode()
        if not boot or hashlib.sha256(boot).hexdigest() != context.boot_digest:
            raise RuntimeContractError("BOOT_INVALID", "canonical boot digest differs")
        if (context.worktree / "CLAUDE.md").read_bytes() != boot:
            raise RuntimeContractError("BOOT_INVALID", "canonical Claude boot bytes differ")
        if not context.executable.path.is_file() or context.executable.path.is_symlink():
            raise RuntimeContractError("EXECUTABLE_CHANGED", "capture the resolved executable before launch")
        with context.executable.path.open("rb") as executable:
            executable_digest = hashlib.file_digest(executable, "sha256").hexdigest()
        if executable_digest != context.executable.sha256:
            raise RuntimeContractError("EXECUTABLE_CHANGED", "captured executable changed before launch")
        modes = {"unrestricted": ["--dangerously-skip-permissions"],
                 "bypassPermissions": ["--dangerously-skip-permissions"]}
        if context.permission_mode in {"default", "dontAsk", "acceptEdits", "plan", "auto"}:
            permissions = ["--permission-mode", context.permission_mode]
        elif context.permission_mode in modes:
            permissions = modes[context.permission_mode]
        else:
            raise RuntimeContractError("PERMISSION_UNAVAILABLE", "prepared permission mode unsupported")
        env = {str(k): str(v) for k, v in context.env.items()}
        for key in PROVIDER_OVERRIDES:
            env.pop(key, None)
        env.update({
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_BG_EXIT_HANDOFF": "1", "CLAUDE_CODE_TOOL_MEMORY_LIMIT": "0",
            "SC_F89_CONTROLLER_ENDPOINT": str(context.controller_endpoint),
            "SC_F89_GENERATION_ID": context.generation_id,
            "SC_F89_ASSET_ROOT": str(ASSETS),
            "SC_F89_SCHEDULING": "1" if self._allowed("automation") else "0",
        })
        if not self._allowed("automation"):
            env["CLAUDE_CODE_DISABLE_CRON"] = "1"
        node = shutil.which("node", path=env.get("PATH"))
        if node is None or not (ASSETS / "node_modules/@modelcontextprotocol/sdk/package.json").is_file():
            raise RuntimeContractError("CHANNEL_UNAVAILABLE", "prepare pinned channel dependencies first")
        context.state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if context.state_root.is_symlink() or context.state_root.stat().st_mode & 0o077:
            raise RuntimeContractError("STATE_INVALID", "generation state must be private")
        hook_settings: dict[str, Any] = {"autoMemoryEnabled": False, "hooks": {}}
        # The parameter deny remains effective when a command hook fails open;
        # native proof under the exact permission policy gates scheduling.
        hook_settings["permissions"] = {"deny": ["CronCreate(durable:true)"] if self._allowed("automation") else ["CronCreate"]}
        for event in HOOKS:
            command = shlex.join([sys.executable, str(ASSETS / "hook.py"), "--event", event])
            group: dict[str, Any] = {"hooks": [{"type": "command", "command": command, "timeout": 3}]}
            if event in {"PreToolUse", "PostToolUse", "PostToolUseFailure", "SubagentStart", "SubagentStop"}:
                group["matcher"] = "*"
            hook_settings["hooks"][event] = [group]
        settings = context.state_root / "claude-runtime-settings.json"
        channel = context.state_root / "claude-runtime-mcp.json"
        _write_private(settings, hook_settings)
        _write_private(channel, {"mcpServers": {CHANNEL: {
            "command": node, "args": [str(ASSETS / "channel.mjs")],
        }}})
        for mcp_file in context.managed_mcp_files:
            if not mcp_file.is_file():
                raise RuntimeContractError("MCP_INVALID", "prepared managed MCP file missing")
        assert self._identity is not None
        argv = [str(context.executable.path), "--session-id", self._identity.root_id,
                "--ax-screen-reader",
                "--settings", str(settings), "--strict-mcp-config", "--mcp-config",
                *map(str, context.managed_mcp_files), str(channel),
                "--dangerously-load-development-channels", f"server:{CHANNEL}", *permissions]
        if context.model:
            argv += ["--model", context.model]
        if context.effort:
            argv += ["--effort", context.effort]
        self._configuration_files = (context.worktree / "CLAUDE.md",
            *((context.worktree / "AGENTS.md",) if (context.worktree / "AGENTS.md").is_file() else ()),
            settings, channel, *context.managed_mcp_files,
            *(ASSETS / name for name in ("channel.mjs", "asset-client.mjs", "hook.py", "asset_client.py", "pty_exec.py", "package-lock.json")))
        self._configuration_sha256 = self._configuration_digest(context)
        # No --bare/--restricted/--safe-mode/--print or instruction-discovery suppression.
        return context.execution_argv(argv), env

    def _spawn_auth(self, deadline: float, *args: Any, **kwargs: Any) -> Any:
        # Only process creation is serialized with Close. Bounded observation
        # must release this condition so Close can fence and stop its own child.
        with self._condition:
            if self._closing or self._auth_cancel.is_set() or time.monotonic() >= deadline:
                raise RuntimeContractError("NATIVE_ACCOUNT_INCONCLUSIVE", "native account observation cancelled")
            self._auth_process = subprocess.Popen(*args, **kwargs)
            self._auth_identity = process_identity(self._auth_process.pid)
            return self._auth_process

    def _validate_launch(self, context: RuntimeContext, deadline: float) -> None:
        if self._closing or self._auth_cancel.is_set() or time.monotonic() >= deadline:
            raise RuntimeContractError("NATIVE_START_FENCED", "native launch fenced by Close or deadline")
        if (not context.executable.path.is_file() or context.executable.path.is_symlink()
                or hashlib.sha256(context.executable.path.read_bytes()).hexdigest() != context.executable.sha256):
            raise RuntimeContractError("EXECUTABLE_CHANGED", "captured executable changed before launch")
        if self._configuration_digest(context) != self._configuration_sha256:
            raise RuntimeContractError("BOOT_CHANGED", "captured native configuration changed before launch")
        if time.monotonic() >= deadline:
            raise RuntimeContractError("NATIVE_START_FENCED", "native launch deadline expired during validation")

    def start(self, context: RuntimeContext, emit: EventSink, *, deadline: float) -> DriverStart:
        with self._condition:
            if self._context is not None or self._closing:
                return DriverStart("unavailable", self._identity, "generation already started")
            self._context, self._emit = context, emit
            root = str(uuid.uuid4())
            self._identity = RuntimeIdentity(root_id=root, session_id=root)
            try:
                argv, env = self._prepare(context)
            except (OSError, RuntimeContractError) as exc:
                self._route_inconclusive = True
                return DriverStart("unavailable", self._identity, str(exc), self._capabilities())
        try:
            auth = _auth_status(context, env, deadline, cancel=self._auth_cancel,
                spawn=lambda *args, **kwargs: self._spawn_auth(deadline, *args, **kwargs))
        except (OSError, RuntimeContractError) as exc:
            with self._condition:
                self._route_inconclusive = True
                return DriverStart("unavailable", self._identity, str(exc), self._capabilities())
        with self._condition:
            try:
                self._validate_launch(context, deadline)
                self._native_auth = auth
                master, slave = pty.openpty()
                os.set_blocking(master, False)
                self._master = master
                try:
                    self._process = self._popen(
                        [sys.executable, str(ASSETS / "pty_exec.py"), *argv],
                        stdin=slave, stdout=slave, stderr=slave, cwd=context.worktree,
                        env=env, start_new_session=True,
                    )
                finally:
                    os.close(slave)
                self._identity = RuntimeIdentity(root_id=root, session_id=root,
                    process=process_identity(self._process.pid),
                    protocol={"transport": "foreground-pty-channel", "revision": REVISION,
                              "auth_observation": dict(self._native_auth)})
                self._reader = threading.Thread(target=self._read_loop, name="claude-runtime-reader", daemon=True)
                self._reader.start()
            except (OSError, RuntimeContractError) as exc:
                self._route_inconclusive = True
                if self._master is not None:
                    os.close(self._master)
                    self._master = None
                return DriverStart("unavailable", self._identity, str(exc), self._capabilities())
            while not self._ready and not self._lost and not self._closing and not self._startup_setup and time.monotonic() < deadline:
                self._condition.wait(min(0.1, max(0, deadline - time.monotonic())))
            if self._startup_setup and not self._lost and not self._closing:
                return DriverStart("needs_consent", self._identity,
                    "Explicit operator enablement of this captured local channel is required.",
                    self._capabilities(), setup=self._startup_setup)
            if not self._ready:
                return DriverStart("unavailable", self._identity,
                    "controlled native channel/trust readiness not established; no dialog was auto-accepted",
                    self._capabilities())
            return DriverStart("ready", self._identity, "native session/channel observed", self._capabilities())

    def _read_loop(self) -> None:
        try:
            while not self._closed.is_set():
                master = self._master
                if master is None:
                    break
                if select.select([master], [], [], 0.1)[0]:
                    with self._condition:
                        # Setup confirmation serializes with every consumed
                        # startup byte, including bytes awaiting observation.
                        try:
                            chunk = os.read(master, 8192) if master == self._master else b""
                        except OSError:
                            chunk = b""
                        if chunk:
                            self._pty_tail.append(chunk)
                            self._observe_startup(chunk)
                self._drain_transcript()
                if self._process.poll() is not None:
                    self._drain_transcript()
                    with self._condition:
                        self._lost, self._primary_state = True, "unknown"
                        self._condition.notify_all()
                    self._send_event(RuntimeEvent("runtime.lost", reference=self._reference(),
                        provenance="claude:process-exit", freshness="stale", partial=True,
                        grade="inconclusive", data={"exit_code": self._process.returncode}))
                    break
        except (OSError, ValueError, RuntimeContractError):
            with self._condition:
                self._lost = True
                self._fail_observation("native reader unavailable")
                self._condition.notify_all()

    def _configuration_digest(self, context: RuntimeContext) -> str:
        files = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in self._configuration_files}
        binding = {"files": files, "driver_revision": REVISION, "boot_digest": context.boot_digest,
                   "policy_digest": context.policy_digest, "model": context.model, "effort": context.effort,
                   "permission_mode": context.permission_mode, "display": "--ax-screen-reader"}
        return hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()

    def _observe_startup(self, chunk: bytes) -> None:
        if self._startup_confirmed or self._ready or self._lost or self._closing or self._activities:
            self._withdraw_setup("previous finite startup choice is no longer available")
            return
        self._startup_bytes = (self._startup_bytes + chunk)[-STARTUP_BYTES:]
        plain = STARTUP_ANSI.sub(b"", self._startup_bytes).decode(errors="replace")
        start = plain.rfind(STARTUP_TITLE)
        if start < 0 or not STARTUP_CHOICE.fullmatch(plain[start:]):
            self._withdraw_setup("previous startup choice changed or is no longer completely observed")
            return
        if self._startup_setup is None:
            assert self._context
            self._startup_setup = StartupConsent(self._context.generation_id, str(uuid.uuid4()),
                self._context.executable.sha256, REVISION, self._configuration_sha256, time.time(),
                "Documented local-development channel choice observed; confirmation does not establish readiness.")
            self._send_event(RuntimeEvent("runtime.setup", provenance="claude:finite-ax-startup-choice",
                data=asdict(self._startup_setup)))
            self._condition.notify_all()

    def _withdraw_setup(self, reason: str) -> None:
        setup, self._startup_setup = self._startup_setup, None
        if setup:
            self._send_event(RuntimeEvent("runtime.setup", provenance="claude:finite-ax-startup-withdrawn",
                freshness="stale", partial=True, grade="inconclusive", data=asdict(setup) | {"detail": reason}))

    def _enable_local_channel(self, command: NativeControl, *, deadline: float) -> WriteReceipt:
        with self._condition:
            if self._closing or self._lost or self._ready or self._startup_confirmed or time.monotonic() >= deadline:
                return WriteReceipt("rejected", detail="captured startup choice is no longer available")
            setup = self._startup_setup
            if not setup or command.options != {"setup_id": setup.setup_id,
                    "configuration_sha256": setup.configuration_sha256}:
                return WriteReceipt("rejected", detail="exact current startup setup binding required")
            if command.target is not None and (not self._identity or command.target.root_id != self._identity.root_id):
                return WriteReceipt("rejected", detail="startup target differs from captured native root")
            if self._master is None or self._identity is None or self._identity.process is None or not self._context:
                return WriteReceipt("not_written", detail="owned native startup descriptor unavailable")
            try:
                # Drain any already-readable change before confirming the finite
                # choice. Bound this admission; never interpret general screens.
                for _ in range(4):
                    if not select.select([self._master], [], [], 0)[0]:
                        break
                    chunk = os.read(self._master, STARTUP_BYTES)
                    if not chunk:
                        return WriteReceipt("not_written", detail="native startup transport ended")
                    self._observe_startup(chunk)
                if (select.select([self._master], [], [], 0)[0] or self._startup_setup != setup
                        or process_identity(self._identity.process.pid) != self._identity.process
                        or self._configuration_digest(self._context) != setup.configuration_sha256
                        or hashlib.sha256(self._context.executable.path.read_bytes()).hexdigest() != setup.executable_sha256
                        or time.monotonic() >= deadline):
                    self._withdraw_setup("startup phase or captured configuration changed before confirmation")
                    return WriteReceipt("not_written", detail="startup phase or captured configuration changed")
                # Fence BEFORE the keyboard edge. Partial/error is unknown and
                # cannot be replayed or treated as consent/readiness.
                self._startup_confirmed = True
                self._startup_setup = None
                written = os.write(self._master, b"y\r")
            except OSError:
                return WriteReceipt("unknown" if self._startup_confirmed else "not_written",
                                    detail="startup confirmation transport unavailable")
            if written != 2:
                return WriteReceipt("unknown", detail="partial startup confirmation; never replay")
            self._send_event(RuntimeEvent("control.acknowledged", control_id=command.control_id,
                provenance="claude:finite-ax-startup-confirmation", data={"state": "written", "readiness": False}))
            return WriteReceipt("written", detail="finite startup confirmation written; native readiness remains pending")

    def _queue(self, content: str, metadata: dict[str, str]) -> None:
        if len(self._notifications) >= MAX_NOTIFICATIONS:
            raise RuntimeContractError("BACKPRESSURE", "native notification buffer full")
        frame = {"content": content, "meta": metadata}
        if len(json.dumps(frame).encode()) > MAX_FRAME_BYTES - 4096:
            raise RuntimeContractError("INPUT_TOO_LARGE", "native channel input too large")
        self._notification_sequence += 1
        self._notifications.append({"sequence": self._notification_sequence, **frame})
        self._pending_dispatch = metadata.get("request_id") or metadata.get("control_id")
        self._condition.notify_all()

    def submit(self, command: NativeSubmission, *, deadline: float) -> WriteReceipt:
        with self._condition:
            if command.request_id in self._requests:
                return WriteReceipt("unknown", detail="submission already accepted; never replay")
            if self._closing or self._lost or self._policy_changed or not self._ready or time.monotonic() >= deadline:
                return WriteReceipt("not_written", detail="native readiness or deadline unavailable")
            if self._primary_state != "idle" or self._pending_dispatch or any(
                r.request_id not in {a.request_id for a in self._activities.values()}
                for r in self._requests.values()
            ):
                return WriteReceipt("not_written", detail="primary processing boundary occupied or unknown")
            if len(self._requests) >= MAX_RECORDS:
                return WriteReceipt("rejected", detail="generation attribution capacity exhausted")
            context = self._context
            assert context is not None
            try:
                self._queue(command.text, {"generation_id": context.generation_id,
                    "conversation_id": context.conversation_id, "request_id": command.request_id,
                    "payload_digest": command.payload_digest})
            except RuntimeContractError as exc:
                return WriteReceipt("rejected", detail=str(exc))
            self._requests[command.request_id] = command
            # It is accepted to a private transport queue, NOT yet native-written.
            return WriteReceipt("unknown", acknowledged=True, detail="accepted; channel write/processing pending")

    def inventory(self, *, deadline: float) -> NativeSnapshot:
        with self._condition:
            return NativeSnapshot(identity=self._identity,
                primary=self._reference(self._primary) if self._primary else None,
                work=tuple(self._work.values()), observed_at=self._snapshot_at or time.time(),
                freshness="stale" if self._lost else "last_observed",
                partial=self._snapshot_partial or self._lost,
                capabilities=self._capabilities(), primary_state=self._primary_state,
                provenance="claude:owned-hooks-transcript")

    def control(self, command: NativeControl, *, deadline: float) -> WriteReceipt:
        if command.action == "enable_local_channel":
            return self._enable_local_channel(command, deadline=deadline)
        with self._condition:
            if time.monotonic() >= deadline or self._lost or (self._closing and command.action != "close"):
                return WriteReceipt("not_written", detail="native control deadline/session unavailable")
            if command.action not in {"stop_reply", "stop_work", "stop_automation", "close"}:
                return WriteReceipt("unsupported", detail="native control action unsupported")
            if command.action == "close":
                self._closing = True
                return WriteReceipt("written", acknowledged=True, detail="Close intent; cleanup is separate")
            target = command.target
            if not target or not self._identity or target.root_id != self._identity.root_id:
                return WriteReceipt("rejected", detail="target is not this owned root")
            if command.action == "stop_reply":
                if not self._allowed("stop_reply"):
                    return WriteReceipt("unsupported", detail="scoped native PTY reply stop needs direct evidence")
                if not self._primary or command.expected_activity_id != self._primary:
                    return WriteReceipt("rejected", detail="primary activity changed")
                if self._master is None or command.control_id in self._controls:
                    return WriteReceipt("unknown", detail="interrupt unavailable/already requested")
                self._controls[command.control_id] = ("stop_reply", self._primary)
                try:
                    os.write(self._master, b"\x03")
                except OSError:
                    return WriteReceipt("unknown", detail="PTY interrupt write ambiguous; never replay")
                return WriteReceipt("written", detail="PTY interrupt written; terminal evidence pending")
            capability = "automation" if command.action == "stop_automation" else "stop_work"
            if not self._allowed(capability):
                return WriteReceipt("unsupported", detail=f"{capability} not proven on this generation")
            work_id = target.work_id
            if not work_id or work_id not in self._work:
                return WriteReceipt("rejected", detail="target lacks observed owned work provenance")
            kind = self._work[work_id].kind
            if (command.action == "stop_work" and kind != "terminal") or (command.action == "stop_automation" and kind != "automation"):
                return WriteReceipt("unsupported", detail="this native work type lacks proved individual control")
            if command.control_id in self._controls:
                return WriteReceipt("unknown", detail="control already requested; reconcile rather than replay")
            if self._primary_state != "idle" or self._pending_dispatch:
                return WriteReceipt("not_written", detail="model control awaits the primary turn boundary")
            tool = "CronDelete" if command.action == "stop_automation" else "TaskStop"
            field = "id" if tool == "CronDelete" else "task_id"
            text = (f"Use native {tool} with {field} {json.dumps(work_id)}. "
                    "Report the actual native result; do not claim success without calling it. No other work.")
            assert self._context is not None
            try:
                self._queue(text, {"generation_id": self._context.generation_id,
                    "conversation_id": self._context.conversation_id, "control_id": command.control_id})
            except RuntimeContractError as exc:
                return WriteReceipt("rejected", detail=str(exc))
            self._controls[command.control_id] = (tool, work_id)
            return WriteReceipt("unknown", acknowledged=True, detail="model stop request accepted; native result pending")

    def asset(self, payload: Mapping[str, Any], *, peer: ProcessIdentity,
              deadline: float) -> Mapping[str, Any]:
        with self._condition:
            kind = payload.get("kind")
            if kind == "channel.ready":
                if self._channel_peer and self._channel_peer != peer:
                    raise RuntimeContractError("CHANNEL_CHANGED", "channel peer changed in live generation")
                self._channel_peer = peer
                self._maybe_ready()
                return {"accepted": True}
            if kind in {"channel.pull", "channel.sent", "channel.reply"}:
                if peer != self._channel_peer:
                    raise RuntimeContractError("CHANNEL_INVALID", "unregistered channel peer")
                if kind == "channel.pull":
                    after = payload.get("after", 0)
                    if not isinstance(after, int) or isinstance(after, bool) or after < 0:
                        raise RuntimeContractError("CHANNEL_INVALID", "invalid notification cursor")
                    return {"notifications": [n for n in self._notifications if n["sequence"] > after
                        and (not self._closing or n["meta"].get("control_id") in self._cleanup_controls)][:8]}
                if kind == "channel.sent":
                    sequence = payload.get("sequence")
                    notification = next((n for n in self._notifications if n["sequence"] == sequence), None)
                    if notification is None:
                        return {"accepted": False}
                    self._notifications.remove(notification)
                    self._send_event(RuntimeEvent("control.acknowledged",
                        reference=self._reference(), request_id=notification["meta"].get("request_id"),
                        control_id=notification["meta"].get("control_id"), provenance="claude:channel-transport-write",
                        data={"state": "written", "processed": False}))
                    return {"accepted": True}
                request_id = payload.get("request_id")
                if request_id == self._readiness_nonce and payload.get("text") == self._readiness_nonce:
                    return {"accepted": self._readiness_prompt is not None}
                if request_id not in self._requests:
                    return {"accepted": False, "reason": "reply has no accepted engine request"}
                activity_id = next((p for p, a in self._activities.items() if a.request_id == request_id), None)
                if activity_id and isinstance(payload.get("text"), str):
                    self._output(activity_id, payload["text"], "claude:channel-reply")
                return {"accepted": activity_id is not None}
            if kind != "hook" or not isinstance(payload.get("event"), Mapping):
                raise RuntimeContractError("ASSET_INVALID", "unknown Claude asset operation")
            self._hook(payload["event"])
            return {"accepted": True}

    def _maybe_ready(self) -> None:
        if (not self._closing and not self._lost and self._session_started and self._channel_peer
                and self._native_auth and self._context and self._native_model == self._context.model
                and not self._readiness_queued and self._allowed("submission")):
            # A generation-specific processed native challenge establishes the
            # route after controlled consent. Cached grades/descriptors do not.
            assert self._context
            self._queue(f'Respond with {self._readiness_nonce} as plain assistant text. '
                        'Do not call any tools, write files, schedule work, or do other work.', {
                "generation_id": self._context.generation_id,
                "conversation_id": self._context.conversation_id,
                "request_id": self._readiness_nonce, "readiness_nonce": self._readiness_nonce})
            self._readiness_queued = True

    def _hook(self, event: Mapping[str, Any]) -> None:
        assert self._identity and self._context
        if event.get("session_id") != self._identity.root_id:
            self._fail_observation("native session identity differs", ownership=True)
            return
        if event.get("cwd") != str(self._context.worktree):
            self._fail_observation("native worktree differs", ownership=True)
            return
        name = event.get("hook_event_name")
        prompt_id = _string(event.get("prompt_id"))
        if name == "SessionStart":
            path = event.get("transcript_path")
            if not isinstance(path, str):
                self._fail_observation("SessionStart has no exact transcript")
                return
            candidate = Path(path)
            home = self._context.env.get("HOME")
            config = self._context.env.get("CLAUDE_CONFIG_DIR")
            if not config and not home:
                self._fail_observation("canonical native config location unavailable")
                return
            projects = Path(config or str(Path(home or "") / ".claude")) / "projects"
            if candidate.name != f"{self._identity.root_id}.jsonl" or not candidate.resolve().is_relative_to(projects.resolve()):
                self._fail_observation("transcript outside exact owned native session", ownership=True)
                return
            self._transcript = candidate
            model = event.get("model")
            self._native_model = model if isinstance(model, str) and re.fullmatch(r"[a-zA-Z0-9._:-]{1,255}", model) else None
            if self._native_model != self._context.model:
                self._route_observation_inconclusive(mismatch=self._native_model is not None)
            self._session_started = True
            self._maybe_ready()
            return
        if name == "UserPromptSubmit" and prompt_id:
            self._prompt(prompt_id, event.get("prompt"), "claude:UserPromptSubmit")
        elif name in {"PreToolUse", "PostToolUse", "PostToolUseFailure"}:
            self._tool(event, prompt_id)
        elif name == "MessageDisplay":
            self._display(event)
        elif name == "SubagentStart":
            agent_id = _string(event.get("agent_id"))
            if agent_id and len(self._work) < MAX_RECORDS:
                ref = NativeReference(root_id=self._identity.root_id, thread_id=agent_id,
                                      activity_id=prompt_id, work_id=agent_id)
                self._work[agent_id] = NativeWork(ref, "child", "running", "claude:SubagentStart",
                    time.time(), grade="unverified", data={"agent_type": _string(event.get("agent_type"))})
                self._send_event(RuntimeEvent("work.observed", reference=ref, provenance="claude:SubagentStart",
                    grade="unverified", data={"kind": "child", "state": "running"}))
        elif name in {"Stop", "SubagentStop"}:
            self._snapshot(event)
            agent_id = _string(event.get("agent_id"))
            if name == "SubagentStop" and agent_id and agent_id in self._work:
                work = self._work[agent_id]
                self._work[agent_id] = NativeWork(work.reference, "child", "stop_hook_observed",
                    "claude:SubagentStop-agent_id", time.time(), grade="unverified", data=work.data)
                self._send_event(RuntimeEvent("work.observed", reference=work.reference,
                    provenance="claude:SubagentStop-agent_id", grade="unverified",
                    data={"kind": "child", "state": "stop_hook_observed", "terminal": "unverified"}))
            if name == "Stop" and prompt_id in self._activities:
                self._activities[prompt_id].stop_seen = True
                if prompt_id == self._readiness_prompt:
                    effort = event.get("effort")
                    level = effort.get("level") if isinstance(effort, Mapping) else None
                    self._native_effort = level if isinstance(level, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", level) else None
                    self._complete_readiness()
                # Stop is pre-terminal and may be blocked by another hook.
                self._drain_transcript()
        elif name == "StopFailure" and prompt_id in self._activities:
            self._terminal(prompt_id, "failed", "claude:StopFailure")
        elif name == "ConfigChange":
            self._policy_changed = True
            self._fail_observation("native configuration changed; generation policy must be reconciled")
        elif name == "SessionEnd":
            self._lost = True
            self._primary_state = "unknown"
        self._condition.notify_all()

    def _prompt(self, prompt_id: str, prompt: Any, provenance: str) -> None:
        if prompt_id in self._activities:
            return
        if not isinstance(prompt, str) or len(self._activities) >= MAX_RECORDS:
            self._fail_observation("prompt attribution invalid or full")
            return
        assert self._context
        request_id: str | None = None
        control_id: str | None = None
        for match in re.finditer(r'<channel\s+([^>]+)>', prompt):
            attrs = {k: html.unescape(v) for k, v in re.findall(r'(\w+)="([^"]*)"', match.group(1))}
            if attrs.get("source") != CHANNEL:
                continue
            if attrs.get("generation_id") != self._context.generation_id or attrs.get("conversation_id") != self._context.conversation_id:
                self._fail_observation("channel envelope generation differs", ownership=True)
                return
            if attrs.get("readiness_nonce") == self._readiness_nonce and attrs.get("request_id") == self._readiness_nonce:
                self._readiness_prompt = prompt_id
            candidate = attrs.get("request_id")
            if candidate in self._requests:
                if request_id and request_id != candidate:
                    self._fail_observation("batched primary GUI prompts require explicit reconciliation")
                    return
                request_id = candidate
                if attrs.get("payload_digest") != self._requests[candidate].payload_digest:
                    self._fail_observation("channel payload binding differs", ownership=True)
                    return
            if attrs.get("control_id") in self._controls:
                control_id = attrs["control_id"]
            # The native outer envelope is authoritative. Examples of channel
            # markup in the user's body do not create a second dispatch.
            break
        source: Any = "gui" if request_id else "native_completion" if "<task-notification>" in prompt else "automation" if any(
            w.kind == "automation" and w.data.get("prompt") == prompt for w in self._work.values()
        ) else "system"
        automation_id = next((w.reference.work_id for w in self._work.values()
            if w.kind == "automation" and w.data.get("prompt") == prompt), None)
        self._activities[prompt_id] = Activity(request_id, source, automation_id=automation_id)
        if self._pending_dispatch in {request_id, control_id} or prompt_id == self._readiness_prompt:
            self._pending_dispatch = None
        self._primary, self._primary_state = prompt_id, "active"
        if control_id:
            self._control_tools[prompt_id] = control_id
        for kind in ("activity.started", "activity.processed"):
            self._send_event(RuntimeEvent(kind, reference=self._reference(prompt_id),
                request_id=request_id, control_id=control_id, source=source, provenance=provenance,
                grade="compatible", data={"prompt_id": prompt_id}))
        task_id = re.search(r'<task-id>([^<]+)</task-id>', prompt)
        status = re.search(r'<status>([^<]+)</status>', prompt)
        if source == "native_completion" and task_id and status and status[1] in {"completed", "failed", "stopped"} and task_id[1] in self._work:
            self._work_terminal(task_id[1], status[1], "claude:task-notification")

    def _output(self, prompt_id: str, text: str, provenance: str, item: str | None = None) -> None:
        activity = self._activities.get(prompt_id)
        if not activity:
            return
        if prompt_id == self._readiness_prompt:
            activity.readiness_reply |= text.strip() == self._readiness_nonce
            return  # Initialization is not a synthetic GUI user exchange.
        digest = hashlib.sha256(text.encode()).hexdigest()
        if digest in activity.texts:
            return
        if len(activity.texts) >= MAX_RECORDS or len(text.encode()) > MAX_FRAME_BYTES:
            self._fail_observation("native output exceeds generation bounds")
            return
        activity.texts.add(digest)
        for offset in range(0, max(1, len(text)), TEXT_CHUNK):
            self._send_event(RuntimeEvent("output.final", reference=self._reference(prompt_id, item=item),
                request_id=activity.request_id, source=activity.source, provenance=provenance,
                partial=len(text) > TEXT_CHUNK, grade="compatible",
                data={"text": text[offset:offset + TEXT_CHUNK], "text_digest": digest,
                      "part": offset // TEXT_CHUNK, "last": offset + TEXT_CHUNK >= len(text)}))

    def _display(self, event: Mapping[str, Any]) -> None:
        prompt_id = _string(event.get("turn_id")) or _string(event.get("prompt_id"))
        item = _string(event.get("message_id"))
        activity = self._activities.get(prompt_id or "")
        index, delta = event.get("index"), event.get("delta")
        if not activity or not item or not isinstance(index, int) or not isinstance(delta, str):
            self._fail_observation("display lacks attributable native fields")
            return
        if item not in activity.displays and len(activity.displays) >= MAX_RECORDS:
            self._fail_observation("native display attribution capacity exhausted")
            return
        expected, text = activity.displays.get(item, (0, ""))
        if index < expected:
            return
        if index != expected or len(text) + len(delta) > MAX_FRAME_BYTES:
            self._fail_observation("display gap/size exceeds bounded message")
            return
        activity.displays[item] = (index + 1, text + delta)
        if event.get("final") is True:
            self._output(prompt_id or "", text + delta, "claude:MessageDisplay", item)
        # Short native messages are complete batches. Partial streaming remains
        # separately unverified, avoiding duplicated transcript/display deltas.

    def _snapshot(self, event: Mapping[str, Any]) -> None:
        tasks, crons = event.get("background_tasks"), event.get("session_crons")
        if not isinstance(tasks, list) or not isinstance(crons, list):
            self._snapshot_partial = True
            return
        observed = time.time()
        seen: set[str] = set()
        partial = False
        for row in tasks + crons:
            if not isinstance(row, Mapping) or not _string(row.get("id")):
                partial = True
                continue
            work_id = row["id"]
            if work_id not in self._work and len(self._work) >= MAX_RECORDS:
                partial = True
                continue
            seen.add(work_id)
            existing = self._work.get(work_id)
            kind: Any = "automation" if "schedule" in row else "terminal" if row.get("type") == "shell" else "task"
            kind_partial = bool(kind == "task" and existing and existing.kind in {"terminal", "automation"})
            if kind_partial and existing is not None:
                # An incomplete/unsupported snapshot row cannot erase a kind
                # established by an attributable native tool result.
                kind = existing.kind
                partial = True
            data = _metadata({k: row[k] for k in ("description", "command", "schedule", "prompt", "recurring") if k in row})
            partial = partial or data.get("metadata_truncated") is True
            if kind == "automation":
                self._definitions.setdefault(work_id, None)
            conflicting = bool(existing and existing.state in {"completed", "stopped", "failed", "deleted"})
            state = "unknown_conflicting_snapshot" if conflicting else str(row.get("status", "scheduled" if kind == "automation" else "unknown"))
            self._work[work_id] = NativeWork(self._reference(work=work_id), kind, state,
                "claude:Stop-snapshot", observed, durable=self._definitions.get(work_id), data=data)
            self._send_event(RuntimeEvent("work.observed", reference=self._reference(work=work_id),
                provenance="claude:Stop-snapshot", freshness="last_observed", partial=kind_partial,
                grade="compatible", data=data | {"kind": kind, "state": self._work[work_id].state}))
            if conflicting:
                partial = True  # Conflicting late snapshot never silently revives a terminal.
        for work_id, work in list(self._work.items()):
            if work.kind == "child":
                continue  # Stop's arrays are not a complete native agent registry.
            if not partial and work_id not in seen and work.state not in {"completed", "stopped", "failed", "deleted"}:
                self._work[work_id] = NativeWork(work.reference, work.kind, "absent_from_snapshot",
                    "claude:Stop-snapshot", observed, durable=work.durable, data=work.data)
        self._snapshot_at, self._snapshot_partial = observed, partial
        for control_id, (work_id, success, result_prompt) in list(self._pending_results.items()):
            if not partial and success and result_prompt and event.get("prompt_id") == result_prompt and work_id not in seen:
                self._work_terminal(work_id, "stopped", "claude:native-result+later-snapshot")
                is_automation = self._controls.get(control_id, (None, None))[0] == "CronDelete"
                if is_automation:
                    self._definitions.pop(work_id, None)
                    self._work_terminal(work_id, "deleted", "claude:CronDelete+later-snapshot")
                self._send_event(RuntimeEvent("control.outcome", reference=self._reference(work=work_id),
                    control_id=control_id, provenance="claude:native-result+later-snapshot", grade="compatible",
                    data={"outcome": "complete", "native_outcome": "native_deleted" if is_automation else "native_stopped", "os_verified": False}))
                del self._pending_results[control_id]
        for activity in self._activities.values():
            work_id = activity.automation_id
            scheduled_work = self._work.get(work_id or "")
            if not partial and work_id and scheduled_work and activity.terminal and work_id not in seen and scheduled_work.durable is False and scheduled_work.data.get("recurring") is False:
                self._definitions.pop(work_id, None)
                self._work_terminal(work_id, "completed", "claude:oneshot-native-turn+later-snapshot")
        self._send_event(RuntimeEvent("snapshot.observed", reference=self._reference(),
            provenance="claude:Stop-snapshot", freshness="last_observed", partial=partial,
            grade="compatible", data={"work_ids": sorted(seen), "observed_at": observed}))

    def _tool(self, event: Mapping[str, Any], prompt_id: str | None) -> None:
        tool, tool_id = _string(event.get("tool_name")), _string(event.get("tool_use_id"))
        args = event.get("tool_input")
        name = event.get("hook_event_name")
        if not tool or not tool_id or not isinstance(args, Mapping):
            self._fail_observation("native tool fields missing")
            return
        key = f"{name}:{tool_id}"
        if key in self._hook_keys:
            return
        if len(self._hook_keys) >= MAX_RECORDS:
            self._fail_observation("native tool attribution capacity exhausted")
            return
        self._hook_keys.add(key)
        self._tool_inputs[tool_id] = (tool, args, prompt_id)
        if name == "PreToolUse":
            if prompt_id == self._readiness_prompt:
                self._readiness_tool_count += 1
            return
        response = event.get("tool_response")
        if not isinstance(response, Mapping):
            control_id = self._control_tools.get(prompt_id or "")
            if control_id and name == "PostToolUseFailure":
                self._send_event(RuntimeEvent("control.outcome", reference=self._reference(prompt_id),
                    control_id=control_id, provenance="claude:PostToolUseFailure", grade="inconclusive",
                    data={"outcome": "native_failed"}))
            return
        if tool == "Bash" and _string(response.get("backgroundTaskId")):
            work_id = response["backgroundTaskId"]
            self._work[work_id] = NativeWork(self._reference(prompt_id, work=work_id), "terminal", "running",
                "claude:Bash-PostToolUse", time.time(), grade="compatible", data=_metadata({
                    "command": args.get("command"), "description": args.get("description"), "tool_use_id": tool_id}))
            self._send_event(RuntimeEvent("work.observed", reference=self._work[work_id].reference,
                provenance="claude:Bash-PostToolUse", grade="compatible",
                data=dict(self._work[work_id].data) | {"kind": "terminal", "state": "running"}))
        elif tool == "CronCreate" and _string(response.get("id")):
            work_id = response["id"]
            durable = response.get("durable")
            self._definitions[work_id] = durable if isinstance(durable, bool) else None
            self._created_definitions.add(work_id)
            self._work[work_id] = NativeWork(self._reference(prompt_id, work=work_id), "automation", "scheduled",
                "claude:CronCreate-PostToolUse", time.time(), durable=self._definitions[work_id], data=_metadata({
                    "prompt": args.get("prompt"), "schedule": args.get("cron"), "recurring": response.get("recurring")}))
            if not self._allowed("automation") or durable is not False:
                self._fail_observation("native schedule creation violated scoped guard", ownership=True)
        elif tool == "TaskStop":
            control_id = self._control_tools.get(prompt_id or "")
            work_id = args.get("task_id")
            if control_id and self._controls.get(control_id) == ("TaskStop", work_id):
                success = (name == "PostToolUse" and response.get("task_id") == work_id
                    and response.get("task_type") == "local_bash" and not response.get("error")
                    and response.get("status") not in {"failed", "error"})
                self._pending_results[control_id] = (str(work_id), success, prompt_id)
                self._send_event(RuntimeEvent("control.acknowledged", reference=self._reference(prompt_id, work=str(work_id)),
                    control_id=control_id, provenance="claude:TaskStop-PostToolUse", grade="compatible" if success else "inconclusive",
                    data={"native_result": {k: response[k] for k in ("task_id", "task_type", "status") if k in response},
                          "outcome": "pending_snapshot" if success else "inconclusive"}))
        elif tool == "CronDelete":
            control_id = self._control_tools.get(prompt_id or "")
            work_id = args.get("id")
            cleanup_id = next((identifier for identifier in self._cleanup_controls
                if self._controls.get(identifier) == ("CronDelete", work_id)), None)
            if control_id in self._cleanup_controls and cleanup_id:
                control_id = cleanup_id
            success = (name == "PostToolUse" and response.get("id") == work_id
                       and work_id in self._definitions and self._controls.get(control_id or "") == ("CronDelete", work_id))
            if control_id:
                self._pending_results[control_id] = (str(work_id), success, prompt_id)
            self._send_event(RuntimeEvent("control.acknowledged", reference=self._reference(prompt_id),
                control_id=control_id, provenance="claude:CronDelete-PostToolUse",
                grade="unverified", data={"outcome": "pending_snapshot" if success else "inconclusive"}))

    def _work_terminal(self, work_id: str, state: str, provenance: str) -> None:
        work = self._work.get(work_id)
        if not work:
            return
        self._work[work_id] = NativeWork(work.reference, work.kind, state, provenance, time.time(),
            grade="compatible", durable=work.durable, data=work.data)
        self._send_event(RuntimeEvent("work.terminal", reference=work.reference, provenance=provenance,
            grade="compatible", data={"kind": work.kind, "state": state, "os_verified": False}))

    def _terminal(self, prompt_id: str, status: str, provenance: str) -> None:
        activity = self._activities.get(prompt_id)
        if not activity or activity.terminal:
            return
        activity.terminal = True
        activity.terminal_status, activity.terminal_provenance = status, provenance
        self._send_event(RuntimeEvent("activity.terminal", reference=self._reference(prompt_id),
            request_id=activity.request_id, source=activity.source, provenance=provenance,
            grade="compatible", data={"status": status}))
        if self._primary == prompt_id:
            self._primary, self._primary_state = None, "idle"
        scheduled = self._work.get(activity.automation_id or "")
        if not self._snapshot_partial and activity.automation_id and scheduled and scheduled.state == "absent_from_snapshot" and scheduled.durable is False and scheduled.data.get("recurring") is False:
            self._definitions.pop(activity.automation_id, None)
            self._work_terminal(activity.automation_id, "completed", "claude:oneshot-snapshot+native-terminal")
        self._complete_readiness()
        for control_id, (action, target) in self._controls.items():
            if action == "stop_reply" and target == prompt_id:
                self._send_event(RuntimeEvent("control.outcome", reference=self._reference(prompt_id),
                    control_id=control_id, provenance=provenance, grade="compatible",
                    data={"outcome": "complete", "native_outcome": "primary_terminal_observed", "interrupt_reason": "unverified"}))
        self._condition.notify_all()

    def _route_observation_inconclusive(self, *, mismatch: bool = False) -> None:
        assert self._context
        self._route_inconclusive = self._route_inconclusive or mismatch
        self._send_event(RuntimeEvent("capability.observed", reference=self._reference(self._readiness_prompt),
            provenance="claude:observed-selected-route", grade="inconclusive", partial=True,
            data={"capability": "submission", "code": "NATIVE_ROUTE_INCONCLUSIVE",
                  "requested_model": self._context.model, "observed_model": self._native_model,
                  "requested_effort": self._context.effort, "observed_effort": self._native_effort}))

    def _complete_readiness(self) -> None:
        prompt_id = self._readiness_prompt
        activity = self._activities.get(prompt_id or "")
        if (activity and activity.terminal and not self._ready and not self._lost and not self._closing
                and not self._policy_changed and not self._route_inconclusive
                and activity.terminal_status == "completed" and activity.readiness_reply
                and activity.terminal_provenance == "claude:owned-transcript-turn_duration"):
            assert self._context and self._identity
            if (not self._native_auth or not self._native_model or not self._native_effort
                    or self._native_model != self._context.model or self._native_effort != self._context.effort):
                self._route_observation_inconclusive(mismatch=bool(
                    self._native_effort and self._native_effort != self._context.effort))
                self._condition.notify_all()
                return
            route = {"account_type": self._native_auth["method"], "model": self._native_model,
                     "efforts": [self._native_effort], "auth": dict(self._native_auth),
                     "observation_origin": "claude:auth-status+SessionStart+readiness-Stop",
                     "model_evidence": "active_selected_model", "effort_evidence": "effective_selected_level",
                     "catalogue_observed": False}
            self._identity = replace(self._identity, protocol=dict(self._identity.protocol) | {"native_route": route})
            self._ready = True
            self._send_event(RuntimeEvent("runtime.ready", reference=self._reference(prompt_id),
                provenance="claude:processed-readiness+owned-transcript-terminal", grade="compatible",
                data={"readiness": "generation-native-processing-observed", "inference_turns": 1,
                      "observed_model_message_ids": len(activity.model_messages),
                      "observed_tool_calls": self._readiness_tool_count,
                      "tool_coverage": "configured-hooks; native coverage requires owner evidence",
                      "native_route": route,
                      "capabilities": self._capabilities()}))
        self._condition.notify_all()

    def _drain_transcript(self) -> None:
        path = self._transcript
        if path is None:
            return
        with self._condition:
            try:
                with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
                    if stream.seek(0, os.SEEK_END) < self._transcript_offset:
                        self._fail_observation("owned transcript rotated/truncated")
                        return
                    stream.seek(self._transcript_offset)
                    chunk = stream.read(MAX_FRAME_BYTES)
            except FileNotFoundError:
                return
            self._transcript_offset += len(chunk)
            data = self._transcript_pending + chunk
            lines = data.split(b"\n")
            self._transcript_pending = lines.pop()
            if len(self._transcript_pending) > MAX_FRAME_BYTES:
                self._transcript_pending = b""
                self._fail_observation("owned transcript record exceeds contract")
                return
            for line in lines:
                try:
                    raw = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    self._fail_observation("owned transcript JSON invalid")
                    continue
                if isinstance(raw, Mapping):
                    self._transcript_record(raw)

    def _transcript_record(self, raw: Mapping[str, Any]) -> None:
        assert self._identity
        if raw.get("sessionId") != self._identity.root_id or raw.get("isSidechain") is True:
            return
        record_id = _string(raw.get("uuid"))
        parent = raw.get("parentUuid")
        prompt_id = _string(raw.get("promptId")) or self._parent_activity.get(str(parent))
        message = raw.get("message")
        if raw.get("type") == "user" and _string(raw.get("promptId")) and isinstance(message, Mapping):
            content = message.get("content")
            if isinstance(content, str):
                self._prompt(prompt_id or "", content, "claude:transcript-user-promptId")
        if record_id and prompt_id:
            if len(self._parent_activity) >= MAX_RECORDS:
                self._fail_observation("transcript ancestry capacity exhausted")
                return
            self._parent_activity[record_id] = prompt_id
        if not prompt_id or prompt_id not in self._activities:
            return
        if raw.get("type") == "assistant" and isinstance(message, Mapping):
            model_message = _string(message.get("id"))
            if model_message:
                self._activities[prompt_id].model_messages.add(model_message)
            for block in message.get("content", []):
                if isinstance(block, Mapping) and block.get("type") == "text" and isinstance(block.get("text"), str):
                    self._output(prompt_id, block["text"], "claude:owned-transcript-text", record_id)
        if raw.get("type") == "system" and raw.get("subtype") == "turn_duration":
            self._terminal(prompt_id, "completed", "claude:owned-transcript-turn_duration")

    def cleanup(self, *, deadline: float) -> NativeCleanup:
        with self._condition:
            self._closing = True
            self._auth_cancel.set()
            auth_process, auth_identity = self._auth_process, self._auth_identity
            self._withdraw_setup("Close fenced the previous finite startup choice")
            self._condition.notify_all()
            # Pending and already-granted channel writes remain ambiguous; no
            # replay or processing success follows from cancellation on Close.
            self._notifications.clear()
            if self._definitions and self._ready and not self._lost and self._primary_state == "idle" and self._allowed("automation"):
                deletable = [identifier for identifier in self._definitions if identifier in self._created_definitions][:64]
                if deletable:
                    for identifier in deletable:
                        control_id = str(uuid.uuid4())
                        self._cleanup_controls.add(control_id)
                        self._controls[control_id] = ("CronDelete", identifier)
                    control_id = next(iter(self._cleanup_controls))
                    assert self._context
                    self._queue("Close cleanup: use native CronDelete individually for these exact owned IDs: "
                                + json.dumps(deletable) + ". No other work. Report actual native results.", {
                        "generation_id": self._context.generation_id, "conversation_id": self._context.conversation_id,
                        "control_id": control_id})
                    native_deadline = min(deadline, time.monotonic() + 5)
                    while any(identifier in self._definitions for identifier in deletable) and time.monotonic() < native_deadline and not self._lost:
                        self._condition.wait(min(0.1, native_deadline - time.monotonic()))
            channel_peer = self._channel_peer
        if (auth_process is not None and auth_process.poll() is None
                and auth_identity and process_identity(auth_identity.pid) == auth_identity):
            try:
                auth_process.terminate()
                auth_process.wait(timeout=max(.01, min(.2, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                auth_process.kill()
                auth_process.wait(timeout=.2)
            except ProcessLookupError:
                pass
        # The peer came from verified private ingress (UID/cgroup + PID/start).
        # Fence the separate channel writer; never interpret native task IDs as
        # PIDs. A prior transport write can already have occurred and stays unknown.
        if channel_peer and process_identity(channel_peer.pid) == channel_peer:
            try:
                os.kill(channel_peer.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        process = self._process
        if process is not None and process.poll() is None:
            identity = self._identity.process if self._identity else None
            if identity and process_identity(identity.pid) == identity:
                process.terminate()
                try:
                    process.wait(timeout=max(0.01, min(1.0, deadline - time.monotonic())))
                except subprocess.TimeoutExpired:
                    pass
        self._closed.set()
        if self._reader and self._reader is not threading.current_thread():
            self._reader.join(timeout=max(0, min(0.5, deadline - time.monotonic())))
        if self._master is not None:
            os.close(self._master)
            self._master = None
        with self._condition:
            definitions = tuple(self._definitions)
            work = tuple(w.reference for w in self._work.values()
                         if w.state not in {"completed", "stopped", "failed", "deleted"})
        return NativeCleanup("pending" if definitions or work else "complete",
            unresolved_work=work, unresolved_definitions=definitions,
            detail="native definitions/work require reconciliation; owner verifies full OS unit independently")


def create_driver() -> RuntimeDriver:
    return ClaudeRuntimeDriver()
