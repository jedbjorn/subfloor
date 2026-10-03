"""Opt-in persistent Codex app-server driver owned by an F89 controller.

The controller owns durable intent, generation fencing and the outer cgroup.
This module never retries a native write or turns a native handle into a PID.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import selectors
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, Protocol

from conversation_runtime_contract import (
    CAP_AUTOMATION,
    CAP_SUBMISSION,
    MAX_FRAME_BYTES,
    DriverStart,
    EventSink,
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
    WriteReceipt,
    WriteState,
)

DRIVER_REVISION = "f89-codex-app-server-v1"
RPC_SECONDS = 20.0
MAX_PENDING_EVENTS = 128
MAX_INVENTORY_PAGES = 8
MAX_TRACKED = 512
TURN_TERMINALS = frozenset({"completed", "interrupted", "failed"})
WORK_TERMINALS = frozenset({"completed", "failed", "interrupted", "declined"})


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeContractError("NATIVE_SHAPE_INVALID", "native result is not an object")
    return value


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _process_identity(pid: int) -> ProcessIdentity:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return ProcessIdentity(pid, int(fields[19]))


class RpcError(RuntimeContractError):
    def __init__(self, code: str, detail: str, *, state: WriteState = "unknown"):
        super().__init__(code, detail)
        self.state = state


class Rpc(Protocol):
    @property
    def process(self) -> subprocess.Popen[bytes] | None: ...

    def request(self, method: str, params: Mapping[str, Any], *, deadline: float) -> Any: ...
    def notify(self, method: str, params: Mapping[str, Any], *, deadline: float) -> None: ...
    def close(self, *, deadline: float) -> bool: ...
    def fence(self, *, deadline: float) -> bool: ...


class JsonlRpc:
    """Bounded writes/waiters, one reader, no RPC lock held while awaiting a reply."""

    def __init__(self, *, argv: list[str], cwd: Path, env: Mapping[str, str],
                 on_message: Callable[[Mapping[str, Any]], None],
                 on_loss: Callable[[str], None], before_write: Callable[[], None]):
        self.process = subprocess.Popen(argv, cwd=cwd, env=dict(env), stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._on_message, self._on_loss = on_message, on_loss
        self._before_write = before_write
        self._write_lock = threading.Lock()
        self._lock = threading.Lock()
        self._pending: dict[int, queue.Queue[Any]] = {}
        self._next_id = 1
        self._lost = False
        self._closing = False
        # Retain only bounded diagnostics; persistence/redaction belongs to the owner.
        self.stderr_tail = bytearray()
        assert self.process.stdin is not None
        os.set_blocking(self.process.stdin.fileno(), False)
        self._reader = threading.Thread(target=self._read, name="codex-runtime-reader", daemon=True)
        self._stderr = threading.Thread(target=self._drain_stderr, name="codex-runtime-stderr", daemon=True)
        self._reader.start()
        self._stderr.start()

    def _loss(self, reason: str) -> None:
        with self._lock:
            if self._lost:
                return
            self._lost = True
            waiters = tuple(self._pending.values())
        for waiter in waiters:
            try:
                waiter.put_nowait(RpcError("NATIVE_STREAM_LOST", reason))
            except queue.Full:
                pass
        if not self._closing:
            self._on_loss(reason)

    def _read(self) -> None:
        assert self.process.stdout is not None
        try:
            while True:
                frame = self.process.stdout.readline(MAX_FRAME_BYTES + 1)
                if not frame:
                    self._loss("native stream closed")
                    return
                if len(frame) > MAX_FRAME_BYTES or not frame.endswith(b"\n"):
                    raise ValueError("native frame exceeds bound or is incomplete")
                message = json.loads(frame)
                if not isinstance(message, dict):
                    raise TypeError("native frame is not an object")
                if "id" in message and "method" not in message:
                    request_id = message["id"]
                    if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
                        raise ValueError("native response ID is invalid")
                    with self._lock:
                        waiter = self._pending.get(request_id) if isinstance(request_id, int) else None
                    if waiter is not None:
                        try:
                            waiter.put_nowait(message)
                        except queue.Full:
                            pass
                    # A late acknowledgement cannot replay a timed-out request.
                else:
                    self._on_message(message)
        except (OSError, ValueError, TypeError, RuntimeError):
            self._loss("native frame/reader failed; reconciliation required")

    def _drain_stderr(self) -> None:
        assert self.process.stderr is not None
        while chunk := self.process.stderr.read(4096):
            self.stderr_tail.extend(chunk)
            del self.stderr_tail[:-16384]

    def _write(self, message: Mapping[str, Any], *, deadline: float) -> None:
        encoded = (json.dumps(message, separators=(",", ":"), allow_nan=False) + "\n").encode()
        if len(encoded) > MAX_FRAME_BYTES:
            raise RpcError("NATIVE_FRAME_TOO_LARGE", "outgoing frame exceeds bound", state="not_written")
        if not self._write_lock.acquire(timeout=_remaining(deadline)):
            raise RpcError("NATIVE_TIMEOUT", "native write slot unavailable", state="not_written")
        written = 0
        try:
            self._before_write()
            if self._lost or self.process.stdin is None:
                raise RpcError("NATIVE_STREAM_LOST", "native transport unavailable", state="not_written")
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdin, selectors.EVENT_WRITE)
                while written < len(encoded):
                    remaining = _remaining(deadline)
                    if remaining <= 0 or not selector.select(remaining):
                        raise RpcError("NATIVE_TIMEOUT", "native write deadline expired",
                                       state="unknown" if written else "not_written")
                    try:
                        count = os.write(self.process.stdin.fileno(), encoded[written:])
                    except BlockingIOError:
                        continue
                    if count <= 0:
                        raise OSError("native write failed")
                    written += count
        except RpcError:
            if written:
                # JSONL cannot recover an unfinished frame by appending a new
                # request. Retain unknown intent and fence this transport.
                self._loss("native partial frame write failed; owner cleanup required")
            raise
        except OSError as exc:
            self._loss("native write stream failed; owner cleanup required")
            raise RpcError("NATIVE_STREAM_LOST", "native write failed", state="unknown") from exc
        finally:
            self._write_lock.release()

    def request(self, method: str, params: Mapping[str, Any], *, deadline: float) -> Any:
        deadline = min(deadline, time.monotonic() + RPC_SECONDS)
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            waiter: queue.Queue[Any] = queue.Queue(maxsize=1)
            self._pending[request_id] = waiter
        try:
            self._write({"id": request_id, "method": method, "params": params}, deadline=deadline)
            try:
                response = waiter.get(timeout=_remaining(deadline))
            except queue.Empty as exc:
                raise RpcError("NATIVE_TIMEOUT", f"native {method} acknowledgement unknown") from exc
            if isinstance(response, RpcError):
                raise response
            if "error" in response:
                # Do not export raw native errors that may echo credentials/config.
                raise RpcError("NATIVE_REJECTED", f"native {method} rejected", state="rejected")
            if "result" not in response:
                raise RpcError("NATIVE_SHAPE_INVALID", "native response has no result")
            return response["result"]
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def notify(self, method: str, params: Mapping[str, Any], *, deadline: float) -> None:
        self._write({"method": method, "params": params}, deadline=deadline)

    def fence(self, *, deadline: float) -> bool:
        # The driver closes its gate before this call. Finishing a frame that
        # was already being written is not a second dispatch; waiters need not
        # acknowledge before Close can proceed.
        if not self._write_lock.acquire(timeout=_remaining(deadline)):
            return False
        self._write_lock.release()
        return True

    def close(self, *, deadline: float) -> bool:
        self._closing = True
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=_remaining(deadline))
            except subprocess.TimeoutExpired:
                return False
        self._reader.join(timeout=_remaining(deadline))
        self._stderr.join(timeout=_remaining(deadline))
        # Closing a BufferedReader while another thread is blocked in read can
        # itself block indefinitely if a descendant inherited the pipe. Retain
        # descriptors for the controller's cgroup backstop in that case.
        for stream, reader in ((self.process.stdout, self._reader),
                               (self.process.stderr, self._stderr)):
            if stream is not None and not reader.is_alive():
                stream.close()
        if self._write_lock.acquire(timeout=_remaining(deadline)):
            try:
                if self.process.stdin is not None:
                    self.process.stdin.close()
            finally:
                self._write_lock.release()
        return not self._reader.is_alive() and not self._stderr.is_alive()


class CodexRuntimeDriver(RuntimeDriver):
    harness = "codex"
    revision = DRIVER_REVISION

    def __init__(self, *, rpc_factory: Callable[..., Rpc] = JsonlRpc):
        self._rpc_factory = rpc_factory
        self._rpc: Rpc | None = None
        self._context: RuntimeContext | None = None
        self._emit: EventSink = lambda event: None
        self._identity: RuntimeIdentity | None = None
        self._lock = threading.RLock()
        self._submit_lock = threading.Lock()
        self._parents: dict[str, str | None] = {}
        self._active: dict[str, str | None] = {}
        self._activity_revision: dict[str, int] = {}
        self._uncertain_activity: dict[str, set[str | None]] = {}
        self._turn_status: dict[tuple[str, str], str] = {}
        # Persist only qualified background identities/tombstones. Separately
        # bounded pending frames disappear when an ordinary command finishes.
        self._command_activity: dict[tuple[str, str], tuple[str, str, str] | None] = {}
        self._command_pending: dict[tuple[str, str], tuple[str, str, str] | None] = {}
        self._command_revision: dict[str, int] = {}
        self._requests: dict[tuple[str, str], NativeSubmission] = {}
        self._pending_submission: NativeSubmission | None = None
        self._unbound: list[Mapping[str, Any]] = []
        self._foreign_pending: list[Mapping[str, Any]] = []
        # Scalar-only diagnostics for the first finite probe intent. No raw
        # frame retention, extra emit, reader I/O, or synchronization surface.
        self._probe_intent: tuple[str, int] | None = None
        self._probe_turn: str | None = None
        self._probe_counts: dict[str, int] = {}
        self._probe_overflow = False
        self._partial = False
        self._lost = False
        self._closing = False
        self._cleanup_thread: int | None = None
        self._reconcile = threading.Event()
        self._reconciler: threading.Thread | None = None

    @property
    def _root(self) -> str:
        return self._identity.root_id if self._identity is not None else ""

    def _reference(self, thread: str, turn: str | None = None, *, item: str | None = None,
                   work: str | None = None, process: str | None = None) -> NativeReference:
        return NativeReference(self._root, thread, self._parents.get(thread), turn, item, work, process)

    def _event(self, kind: str, reference: NativeReference | None = None, *,
               provenance: str, data: Mapping[str, Any] | None = None,
               control_id: str | None = None, partial: bool = False) -> None:
        command = self._requests.get((reference.thread_id or "", reference.activity_id or "")) if reference else None
        if (command is not None and (command.request_id, command.request_sequence) == self._probe_intent and reference is not None
                and reference.thread_id == self._root and reference.activity_id == self._probe_turn):
            data = {**(data or {}), "first_rpc_observation": "overflow" if self._probe_overflow else "observed",
                    "first_rpc_counts": {} if self._probe_overflow else dict(self._probe_counts),
                    "first_rpc_binding_sha256": hashlib.sha256(json.dumps([
                        self._context.generation_id if self._context else None, self._root,
                        command.request_id, self._probe_turn]).encode()).hexdigest()}
        self._emit(RuntimeEvent(kind, reference=reference, request_id=command.request_id if command else None,
                                control_id=control_id, source=command.source if command else "system",
                                provenance=provenance, freshness="stale" if self._lost else "current",
                                partial=partial, grade="unverified", data=data or {}))

    def _loss(self, reason: str) -> None:
        with self._lock:
            self._lost = True
            self._partial = True
            data = {"detail": reason}
            if self._context and self._context.probe_capabilities:
                data["probe_lost_phase"] = ("startup" if self._probe_intent is None else
                    "acknowledgement" if self._probe_turn is None else
                    "post_first_turn" if self._turn_status.get((self._root, self._probe_turn)) in TURN_TERMINALS
                    else "first_turn")
            self._event("runtime.lost", provenance="codex:transport", data=data, partial=True)

    def _allowed(self, capability: str) -> bool:
        context = self._context
        return context is not None and (context.capability_evidence.get(capability) == "compatible"
                                        or capability in context.probe_capabilities)

    def _before_write(self) -> None:
        if self._closing and threading.get_ident() != self._cleanup_thread:
            raise RpcError("NATIVE_CLOSING", "generation is closing", state="not_written")

    def start(self, context: RuntimeContext, emit: EventSink, *, deadline: float) -> DriverStart:
        if self._context is not None:
            return DriverStart("unavailable", detail="driver generation already started")
        self._context, self._emit = context, emit
        if context.harness != self.harness or context.permission_mode != "unrestricted":
            return DriverStart("unavailable", detail="prepared harness/permission mode is unsupported",
                               capabilities={"permission_policy": "unverified"})
        if context.provider not in {None, "openai"}:
            return DriverStart("unavailable", detail="prepared provider has no demonstrated subscription route")
        if not context.model or not context.boot_content:
            return DriverStart("unavailable", detail="prepared native model and boot content are required")
        if (context.driver_revision != self.revision
                or hashlib.sha256(context.boot_content.encode()).hexdigest() != context.boot_digest):
            return DriverStart("unavailable", detail="prepared driver/boot binding differs")
        try:
            executable = context.executable.path
            if (not executable.is_absolute() or executable.resolve() != executable
                    or hashlib.sha256(executable.read_bytes()).hexdigest() != context.executable.sha256):
                raise RuntimeContractError("EXECUTABLE_CHANGED", "prepared executable binding differs")
            if not context.worktree.is_absolute() or not context.worktree.is_dir():
                raise RuntimeContractError("WORKTREE_INVALID", "prepared worktree is unavailable")
            if context.managed_mcp_files:
                raise RuntimeContractError("MCP_NOT_READY", "prepared Codex MCP injection shape is not released")
            argv = context.execution_argv([
                str(executable), "app-server", "--stdio", *context.managed_mcp_args, "--disable", "memories",
                "--disable", "external_agent_memory_import",
                "-c", 'forced_login_method="chatgpt"', "-c", 'model_provider="openai"',
                "-c", "memories.generate_memories=false", "-c", "memories.use_memories=false",
            ])
            env = {key: value for key, value in context.env.items()
                   if key not in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_BASE_URL",
                                  "OPENAI_ORG_ID", "OPENAI_PROJECT_ID"}}
            self._rpc = self._rpc_factory(argv=argv, cwd=context.worktree, env=env,
                                          on_message=self._message, on_loss=self._loss,
                                          before_write=self._before_write)
            rpc = self._rpc
            initialized = _object(rpc.request("initialize", {
                "clientInfo": {"name": "subfloor-native-runtime", "version": self.revision},
                "capabilities": {"experimentalApi": True}}, deadline=deadline))
            rpc.notify("initialized", {}, deadline=deadline)
            account = _object(rpc.request("account/read", {"refreshToken": False}, deadline=deadline))
            native_account = account.get("account")
            if not isinstance(native_account, dict) or native_account.get("type") != "chatgpt":
                raise RuntimeContractError("SUBSCRIPTION_UNAVAILABLE", "native ChatGPT login is required")
            # Observe only the immutable prepared route, never export account PII
            # or synthesize support from a static catalogue/version range.
            native_route = {"account_type": "chatgpt", "model": context.model, "efforts": []}
            cursor = None
            selected = None
            for _ in range(2):
                page = _object(rpc.request("model/list", {
                    "limit": 100, "includeHidden": True, "cursor": cursor}, deadline=deadline))
                models = page.get("data")
                if not isinstance(models, list) or len(models) > 100:
                    raise RuntimeContractError("NATIVE_ROUTE_UNVERIFIED", "bounded native model list unavailable")
                selected = next((model for model in models if isinstance(model, dict)
                                 and model.get("model") == context.model), None)
                if selected is not None:
                    break
                cursor = _string(page.get("nextCursor"))
                if not cursor:
                    break
            if selected is None:
                raise RuntimeContractError("NATIVE_ROUTE_UNAVAILABLE", "selected native model was not advertised")
            supported = selected.get("supportedReasoningEfforts")
            if (not isinstance(supported, list) or len(supported) > 32
                    or any(not isinstance(item, dict) or not isinstance(item.get("reasoningEffort"), str)
                           or not 1 <= len(item["reasoningEffort"]) <= 64 for item in supported)):
                raise RuntimeContractError("NATIVE_ROUTE_UNVERIFIED", "native effort advertisement malformed")
            native_route["efforts"] = sorted({item["reasoningEffort"] for item in supported})
            if context.effort and context.effort != "default" and context.effort not in native_route["efforts"]:
                raise RuntimeContractError("NATIVE_ROUTE_UNAVAILABLE", "selected native effort was not advertised")
            config = _object(_object(rpc.request("config/read", {
                "cwd": str(context.worktree), "includeLayers": False}, deadline=deadline)).get("config"))
            memories, features = config.get("memories"), config.get("features")
            if (not isinstance(memories, dict) or not isinstance(features, dict)
                    or memories.get("generate_memories") is not False
                    or memories.get("use_memories") is not False or features.get("memories") is not False):
                raise RuntimeContractError("NATIVE_MEMORY_UNVERIFIED", "required native no-memory flags unproved")
            started = _object(rpc.request("thread/start", {
                "model": context.model, "modelProvider": "openai", "allowProviderModelFallback": False,
                "cwd": str(context.worktree), "developerInstructions": context.boot_content,
                "approvalPolicy": "never", "sandbox": "danger-full-access",
                "config": {"features.memories": False, "memories.generate_memories": False,
                           "memories.use_memories": False}}, deadline=deadline))
            thread = _object(started.get("thread"))
            root = _string(thread.get("id"))
            if not root or Path(str(thread.get("cwd", ""))).resolve() != context.worktree.resolve():
                raise RuntimeContractError("OWNERSHIP_INVALID", "native root/worktree binding differs")
            process = _process_identity(rpc.process.pid) if rpc.process is not None else None
            with self._lock:
                self._identity = RuntimeIdentity(root, _string(thread.get("sessionId")), process,
                                                 {"experimentalApi": True,
                                                  "userAgent": initialized.get("userAgent"),
                                                  "driver_revision": self.revision,
                                                  "native_route": native_route})
                self._parents[root] = None
                self._active[root] = None
            rpc.request("thread/memoryMode/set", {"threadId": root, "mode": "disabled"}, deadline=deadline)
            with self._lock:
                self._identity = replace(self._identity, protocol={**self._identity.protocol,
                    "memory_policy": {"generate_memories": False, "use_memories": False,
                                      "feature_enabled": False, "root_mode": "disabled"}})
            self._reconciler = threading.Thread(target=self._reconcile_loop,
                                               name="codex-runtime-reconcile", daemon=True)
            self._reconciler.start()
            self._event("runtime.ready", provenance="codex:thread/start", data={"root_id": root})
            return DriverStart("ready", self._identity, capabilities={
                **context.capability_evidence, CAP_AUTOMATION: "unverified"})
        except (OSError, RuntimeContractError, ValueError) as exc:
            self._loss("native start failed; owner must verify scoped cleanup")
            return DriverStart("unavailable", self._identity, detail=getattr(exc, "code", "NATIVE_START_FAILED"),
                               capabilities={CAP_SUBMISSION: "inconclusive"})

    def submit(self, command: NativeSubmission, *, deadline: float) -> WriteReceipt:
        if not self._allowed(CAP_SUBMISSION):
            return WriteReceipt("unsupported", detail="subscription submission capability is not evidenced")
        if not command.text.strip() or self._rpc is None or not self._root or self._lost or self._closing:
            return WriteReceipt("rejected", detail="native runtime/input unavailable")
        if not self._submit_lock.acquire(timeout=_remaining(deadline)):
            return WriteReceipt("not_written", detail="primary submission slot unavailable")
        try:
            with self._lock:
                if (self._active.get(self._root) or self._root in self._uncertain_activity
                        or self._pending_submission is not None):
                    return WriteReceipt("not_written", detail="native primary activity must be reconciled")
                self._pending_submission = command
                if self._probe_intent is None and self._context and self._context.probe_capabilities:
                    self._probe_intent = (command.request_id, command.request_sequence)
            assert self._context is not None
            params: dict[str, Any] = {"threadId": self._root,
                                     "input": [{"type": "text", "text": command.text}],
                                     "model": self._context.model, "approvalPolicy": "never",
                                     "clientUserMessageId": command.message_id or command.request_id}
            if self._context.effort and self._context.effort != "default":
                params["effort"] = self._context.effort
            try:
                result = _object(self._rpc.request("turn/start", params, deadline=deadline))
                turn = _object(result.get("turn"))
                turn_id = _string(turn.get("id"))
                if not turn_id:
                    raise RpcError("NATIVE_SHAPE_INVALID", "turn/start returned no turn identity")
                with self._lock:
                    self._requests[(self._root, turn_id)] = command
                    if self._probe_turn is None and (command.request_id, command.request_sequence) == self._probe_intent:
                        self._probe_turn = turn_id
                    self._active[self._root] = turn_id
                    self._activity_revision[self._root] = self._activity_revision.get(self._root, 0) + 1
                    self._pending_submission = None
                    pending, self._unbound = self._unbound, []
                    for raw in pending:
                        self._message(raw)
                    # Completed old turns retain their native identity in the
                    # controller journal. Bound only this correlation cache;
                    # the controller retains submission deduplication forever.
                    for old in tuple(self._requests):
                        if len(self._requests) <= MAX_TRACKED:
                            break
                        if self._turn_status.get(old) in TURN_TERMINALS:
                            self._requests.pop(old)
                            self._turn_status.pop(old, None)
                    # A response alone does not manufacture processing.
                return WriteReceipt("written", True, turn_id)
            except (RuntimeContractError, OSError) as exc:
                with self._lock:
                    pending, self._unbound = self._unbound, []
                    for raw in pending:
                        self._message(raw, unbound=True)
                    # Unknown write retains the primary reservation until explicit reconciliation.
                    if isinstance(exc, RpcError) and exc.state in {"not_written", "rejected"}:
                        self._pending_submission = None
                state = exc.state if isinstance(exc, RpcError) else "unknown"
                return WriteReceipt(state, detail=getattr(exc, "code", "NATIVE_SUBMIT_UNKNOWN"))
        finally:
            self._submit_lock.release()

    def _message(self, raw: Mapping[str, Any], *, unbound: bool = False) -> None:
        method, params = raw.get("method"), raw.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            return
        with self._lock:
            thread = _string(params.get("threadId"))
            if not thread:
                if method in {"turn/started", "turn/completed", "item/completed"}:
                    self._loss("required native event identity missing")
                return
            if thread not in self._parents:
                if len(self._foreign_pending) < MAX_PENDING_EVENTS:
                    self._foreign_pending.append(raw)
                else:
                    self._partial = True
                self._reconcile.set()
                return
            nested = params.get("turn")
            turn = _string(params.get("turnId")) or (_string(nested.get("id")) if isinstance(nested, dict) else None)
            # Before acknowledgement no turn is bound; replay is counted once
            # only after the response binds this exact owned first turn. A
            # malformed field inside a bound frame still counts before refusal.
            if (thread == self._root and turn is not None and turn == self._probe_turn and self._probe_intent is not None
                    and (thread, turn) in self._requests
                    and (self._requests[(thread, turn)].request_id, self._requests[(thread, turn)].request_sequence) == self._probe_intent):
                names = {"turn/started": "turn_started", "turn/completed": "turn_completed",
                         "item/started": "item_started", "item/completed": "item_completed",
                         "item/agentMessage/delta": "assistant_delta",
                         "item/commandExecution/outputDelta": "terminal_delta"}
                name = names.get(method)
                if name:
                    self._probe_count(name)
                    if method == "turn/completed":
                        status = nested.get("status") if isinstance(nested, dict) else None
                        self._probe_count("status_" + (status if isinstance(status, str)
                            and status in TURN_TERMINALS else "unknown"))
                    value = params.get("item")
                    refused = ((method in {"item/agentMessage/delta", "item/commandExecution/outputDelta"}
                        and (not isinstance(params.get("delta"), str) or not _string(params.get("itemId"))))
                        or (method in {"turn/started", "turn/completed"} and not isinstance(nested, dict))
                        or (method in {"item/started", "item/completed"} and
                            (not isinstance(value, dict) or not _string(value.get("type")) or not _string(value.get("id"))
                             or (method == "item/completed" and value.get("type") == "agentMessage"
                                 and not isinstance(value.get("text"), str)))))
                    if refused:
                        self._probe_count("normalization_refused")
            if method in {"turn/started", "turn/completed"} and (not turn or not isinstance(nested, dict)):
                self._loss("required native turn identity missing")
                return
            if (thread == self._root and turn and (thread, turn) not in self._requests
                    and self._pending_submission is not None and not unbound):
                if len(self._unbound) >= MAX_PENDING_EVENTS:
                    self._loss("submission correlation buffer exceeded bound")
                else:
                    self._unbound.append(raw)
                return
            ref = self._reference(thread, turn)
            provenance = "codex:" + method
            if method == "turn/started" and turn:
                # Native final output may arrive after completion. An old
                # start must not reopen the completed turn/primary slot.
                if self._turn_status.get((thread, turn)) in TURN_TERMINALS:
                    return
                self._active[thread] = turn
                self._activity_revision[thread] = self._activity_revision.get(thread, 0) + 1
                self._turn_status[(thread, turn)] = "inProgress"
                self._event("activity.started", ref, provenance=provenance, data={"status": "inProgress"})
                self._event("activity.processed", ref, provenance=provenance)
            elif method == "turn/completed" and turn and isinstance(nested, dict):
                status = _string(nested.get("status")) or "unknown"
                was_terminal = self._turn_status.get((thread, turn)) in TURN_TERMINALS
                if was_terminal:
                    return  # A read/event already proved this scoped terminal.
                self._turn_status[(thread, turn)] = status
                self._activity_revision[thread] = self._activity_revision.get(thread, 0) + 1
                if status in TURN_TERMINALS:
                    if self._active.get(thread) == turn:
                        self._active[thread] = None
                    uncertain = self._uncertain_activity.get(thread)
                    if uncertain is not None:
                        uncertain.discard(turn)
                        if not uncertain:
                            self._uncertain_activity.pop(thread)
                self._event("activity.terminal" if status in TURN_TERMINALS else "snapshot.observed", ref,
                            provenance=provenance, data={"status": status}, partial=status not in TURN_TERMINALS)
            elif method in {"item/agentMessage/delta", "item/commandExecution/outputDelta"}:
                text = params.get("delta")
                if isinstance(text, str):
                    item_id = _string(params.get("itemId"))
                    bound_turn = _string(params.get("turnId"))
                    self._output("output.delta", self._reference(thread, bound_turn, item=item_id), text, provenance,
                                 output_kind="assistant" if method == "item/agentMessage/delta" else "terminal",
                                 partial=not bound_turn or not item_id)
            elif method in {"item/started", "item/completed"}:
                self._item(thread, turn, params.get("item"), completed=method == "item/completed", provenance=provenance)
            elif "id" in raw:
                self._event("capability.observed", ref, provenance=provenance,
                            data={"capability": "native_request_response", "state": "unavailable",
                                  "native_request_id": raw["id"]}, partial=True)

    def _probe_count(self, name: str) -> None:
        # Caller holds the existing native observation RLock. Overflow makes
        # the snapshot unavailable, including otherwise valid smaller counts.
        count = self._probe_counts.get(name, 0)
        if count >= 128:
            self._probe_overflow = True
        else:
            self._probe_counts[name] = count + 1

    def _output(self, kind: str, ref: NativeReference, text: str, provenance: str, *,
                output_kind: Literal["assistant", "terminal"], partial: bool = False) -> None:
        for offset in range(0, max(1, len(text)), 4096):
            self._event(kind, ref, provenance=provenance, partial=partial,
                        data={"kind": output_kind, "text": text[offset:offset + 4096], "offset": offset,
                              "complete": offset + 4096 >= len(text)})

    def _observe_command(self, thread: str, turn: str | None, value: Mapping[str, Any]) -> None:
        # Called under the native observation lock, never from assistant text
        # or the currently active foreground slot. Missing attribution is not
        # an inventory failure; it leaves the strict probe target unqualified.
        item = value.get("id")
        if (self._closing or thread not in self._parents or not isinstance(item, str)
                or not item or len(item) > 255):
            return
        key = (thread, item)
        self._command_revision[thread] = self._command_revision.get(thread, 0) + 1
        cache = self._command_activity if key in self._command_activity else self._command_pending
        if key not in cache and len(cache) >= MAX_TRACKED:
            return
        process, status = value.get("processId"), value.get("status")
        if process is None and status == "inProgress" and key not in cache:
            return  # Native startup may not have assigned a background handle yet.
        if (not isinstance(turn, str) or not turn or len(turn) > 255
                or not isinstance(process, str) or not process or len(process) > 255
                or not isinstance(status, str) or status not in {*WORK_TERMINALS, "inProgress"}):
            cache[key] = None
            return
        binding = (turn, process, status)
        if key in cache:
            previous = cache[key]
            if (previous is None or previous[:2] != binding[:2]
                    or (previous[2] in WORK_TERMINALS and status != previous[2])):
                cache[key] = None
                return
        for candidates in (self._command_activity, self._command_pending):
            for other, known in candidates.items():
                if other != key and other[0] == thread and known is not None and known[1] == process:
                    candidates[other] = None
                    cache[key] = None
                    return
        cache[key] = binding

    def _command_turn(self, reference: NativeReference) -> tuple[str | None, str]:
        key = (reference.thread_id or "", reference.item_id or "")
        binding = self._command_activity.get(key) if key in self._command_activity else self._command_pending.get(key)
        if binding is None or binding[1] != reference.native_process_id or binding[2] != "inProgress":
            reason = "pending_capacity" if key not in self._command_activity and len(self._command_pending) >= MAX_TRACKED else "unproved"
            return None, reason
        if key not in self._command_activity:
            if len(self._command_activity) >= MAX_TRACKED:
                return None, "background_capacity"
            self._command_activity[key] = binding
            self._command_pending.pop(key, None)
        return binding[0], "observed"

    def _item(self, thread: str, turn: str | None, value: Any, *, completed: bool, provenance: str) -> None:
        if not isinstance(value, dict):
            self._partial = True
            return
        kind, item = _string(value.get("type")), _string(value.get("id"))
        if not item or not kind:
            self._partial = True
            return
        ref = self._reference(thread, turn, item=item)
        if kind == "reasoning":
            return
        if kind == "agentMessage":
            if completed and isinstance(value.get("text"), str):
                self._output("output.final", ref, value["text"], provenance, output_kind="assistant")
            return
        if kind == "subAgentActivity":
            child = _string(value.get("agentThreadId"))
            if not child:
                self._partial = True
                return
            child_ref = NativeReference(self._root, child, thread, item_id=item, work_id=child)
            # kind=interrupted is action evidence, not child terminal truth.
            self._event("work.observed", child_ref, provenance=provenance,
                        data={"kind": "child", "state": "unknown", "native_action": value.get("kind"),
                              "agent_path": value.get("agentPath")}, partial=True)
            self._reconcile.set()
            return
        if kind != "commandExecution":
            self._event("work.observed", replace(ref, work_id=item), provenance=provenance,
                        data={"kind": "task", "native_type": kind, "state": "unknown"}, partial=True)
            return
        self._observe_command(thread, turn, value)
        process = _string(value.get("processId"))
        ref = replace(ref, work_id=process or item, native_process_id=process)
        status = _string(value.get("status")) or "unknown"
        work = NativeWork(ref, "terminal", status, provenance, time.time(),
                          freshness="current", data={"command": value.get("command"),
                                                     "cwd": value.get("cwd"), "exit_code": value.get("exitCode")})
        self._event("work.terminal" if completed and status in WORK_TERMINALS else "work.observed", ref,
                    provenance=provenance, data={"kind": "terminal", "state": status, **work.data},
                    partial=status not in WORK_TERMINALS and status != "inProgress")
        if completed and isinstance(value.get("aggregatedOutput"), str):
            self._output("output.final", ref, value["aggregatedOutput"], provenance, output_kind="terminal")

    def _reconcile_loop(self) -> None:
        while True:
            self._reconcile.wait()
            self._reconcile.clear()
            if self._closing or self._lost:
                return
            self.inventory(deadline=time.monotonic() + RPC_SECONDS)

    def _pages(self, method: str, params: Mapping[str, Any], *, deadline: float) -> tuple[list[dict[str, Any]], bool]:
        assert self._rpc is not None
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        cursors: set[str] = set()
        for _ in range(MAX_INVENTORY_PAGES):
            result = _object(self._rpc.request(method, {**params, "limit": 100, "cursor": cursor}, deadline=deadline))
            data = result.get("data")
            if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
                raise RuntimeContractError("NATIVE_SHAPE_INVALID", "native inventory data is invalid")
            rows.extend(data)
            cursor = result.get("nextCursor")
            if cursor is None:
                return rows, False
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise RuntimeContractError("NATIVE_SHAPE_INVALID", "native inventory cursor is invalid")
            cursors.add(cursor)
        return rows, True

    def inventory(self, *, deadline: float) -> NativeSnapshot:
        if (self._rpc is None or not self._root or self._lost or self._identity is None
                or self._identity.root_id != self._root or self._root not in self._parents):
            return NativeSnapshot(self._identity, None, freshness="stale", provenance="codex:transport")
        partial = self._partial
        root_uncertain = self._partial
        root_read_current = False
        root_read_revision = None
        observed: list[NativeWork] = []
        try:
            descendants, truncated = self._pages("thread/list", {
                "ancestorThreadId": self._root, "sourceKinds": ["subAgent", "subAgentThreadSpawn"],
                "useStateDbOnly": True}, deadline=deadline)
            partial |= truncated
            unresolved = list(descendants)
            with self._lock:
                while unresolved:
                    remaining = []
                    for child in unresolved:
                        thread, parent = _string(child.get("id")), _string(child.get("parentThreadId"))
                        if thread and parent in self._parents and thread != self._root and len(self._parents) < MAX_TRACKED:
                            existing = self._parents.get(thread)
                            if thread in self._parents and existing != parent:
                                raise RuntimeContractError("OWNERSHIP_INVALID", "native child ancestry changed")
                            self._parents[thread] = parent
                        else:
                            remaining.append(child)
                    if len(remaining) == len(unresolved):
                        partial |= bool(remaining)
                        break
                    unresolved = remaining
                pending, self._foreign_pending = self._foreign_pending, []
                for raw in pending:
                    params = raw.get("params")
                    if isinstance(params, dict) and params.get("threadId") in self._parents:
                        self._message(raw)
                threads = tuple(self._parents)
            for thread in threads:
                with self._lock:
                    read_revision = self._activity_revision.get(thread, 0)
                    command_revision = self._command_revision.get(thread, 0)
                result = _object(self._rpc.request("thread/read", {"threadId": thread, "includeTurns": True}, deadline=deadline))
                native = _object(result.get("thread"))
                if native.get("id") != thread:
                    raise RuntimeContractError("OWNERSHIP_INVALID", "native read returned a different thread")
                turns = native.get("turns")
                if not isinstance(turns, list) or not all(isinstance(turn, dict) for turn in turns):
                    raise RuntimeContractError("NATIVE_SHAPE_INVALID", "native turn inventory is invalid")
                unknown_status = any(turn.get("status") not in {*TURN_TERMINALS, "inProgress"} for turn in turns)
                partial |= unknown_status
                if thread == self._root:
                    root_uncertain |= unknown_status
                active = [turn for turn in turns if turn.get("status") == "inProgress"]
                if len(active) > 1:
                    raise RuntimeContractError("NATIVE_SHAPE_INVALID", "native thread has multiple active turns")
                turn_id = _string(active[0].get("id")) if active else None
                if active and not turn_id:
                    raise RuntimeContractError("NATIVE_SHAPE_INVALID", "active turn identity missing")
                with self._lock:
                    # A terminal event received during this read is newer than
                    # the snapshot; do not resurrect its completed activity.
                    if (self._activity_revision.get(thread, 0) != read_revision
                            or (turn_id and self._turn_status.get((thread, turn_id)) in TURN_TERMINALS)):
                        partial = True
                        if thread == self._root:
                            root_uncertain = True
                    else:
                        previous = self._active.get(thread)
                        by_id = {native_turn.get("id"): native_turn for native_turn in turns
                                 if isinstance(native_turn.get("id"), str)}
                        # A missing known active turn is not terminal proof.
                        # Preserve occupancy and refuse another native write.
                        unresolved_previous = bool(previous and by_id.get(previous, {}).get("status") not in TURN_TERMINALS
                                                   and self._turn_status.get((thread, previous)) not in TURN_TERMINALS)
                        uncertain = unknown_status or bool(unresolved_previous and turn_id != previous)
                        partial |= uncertain
                        if thread == self._root:
                            root_read_current = True
                            root_uncertain |= uncertain
                        if uncertain:
                            self._uncertain_activity[thread] = {
                                _string(native_turn.get("id")) for native_turn in turns
                                if native_turn.get("status") not in {*TURN_TERMINALS, "inProgress"}}
                            if unresolved_previous and turn_id != previous:
                                self._uncertain_activity[thread].add(previous)
                        else:
                            self._uncertain_activity.pop(thread, None)
                        self._active[thread] = previous if unresolved_previous and not turn_id else turn_id
                        self._activity_revision[thread] = read_revision + 1
                        if thread == self._root:
                            root_read_revision = read_revision + 1
                        command_read_current = self._command_revision.get(thread, 0) == command_revision
                        for native_turn in turns:
                            tid, status = _string(native_turn.get("id")), _string(native_turn.get("status"))
                            items = native_turn.get("items")
                            if "items" in native_turn and not command_read_current:
                                partial = True  # Native command evidence changed during this read.
                            elif "items" in native_turn:
                                if (not tid or status not in {*TURN_TERMINALS, "inProgress"}
                                        or not isinstance(items, list) or len(items) > MAX_TRACKED
                                        or not all(isinstance(item, dict) for item in items)):
                                    partial = True
                                else:
                                    for item in items:
                                        if item.get("type") == "commandExecution":
                                            self._observe_command(thread, tid, item)
                            if tid and status in TURN_TERMINALS:
                                prior_status = self._turn_status.get((thread, tid))
                                self._turn_status[(thread, tid)] = status
                                if prior_status not in TURN_TERMINALS and (
                                        tid == previous or (thread, tid) in self._requests or prior_status == "inProgress"):
                                    self._event("activity.terminal", self._reference(thread, tid),
                                                provenance="codex:thread/read", data={"status": status})
                if thread != self._root:
                    child_status = native.get("status")
                    status = _string(child_status.get("type")) if isinstance(child_status, dict) else None
                    observed.append(NativeWork(self._reference(thread, turn_id, work=thread), "child", status or "unknown",
                                               "codex:thread/read", time.time(), freshness="current",
                                               data={"can_accept_direct_input": native.get("canAcceptDirectInput")}))
                terminals, truncated = self._pages("thread/backgroundTerminals/list", {"threadId": thread}, deadline=deadline)
                partial |= truncated
                for terminal in terminals:
                    process, item = _string(terminal.get("processId")), _string(terminal.get("itemId"))
                    if not process or not item:
                        raise RuntimeContractError("NATIVE_SHAPE_INVALID", "terminal identity missing")
                    observed.append(NativeWork(self._reference(thread, item=item, work=process, process=process),
                                               "terminal", "inProgress", "codex:thread/backgroundTerminals/list", time.time(),
                                               freshness="current", data={"command": terminal.get("command"), "cwd": terminal.get("cwd")}))
                if not truncated:
                    with self._lock:
                        present = {(row.get("itemId"), row.get("processId")) for row in terminals}
                        # Complete current absence reconciles finished pending
                        # foreground commands; qualified background tombstones
                        # stay retained to refuse later replay/reused handles.
                        for key, binding in tuple(self._command_pending.items()):
                            if (key[0] == thread and binding is not None and binding[2] in WORK_TERMINALS
                                    and (key[1], binding[1]) not in present):
                                self._command_pending.pop(key)
            with self._lock:
                root_uncertain |= self._activity_revision.get(self._root) != root_read_revision
                partial |= root_uncertain
                # Recheck after all native reads: a late completion/conflict
                # must not leave a previously observed running target bound.
                for index, work in enumerate(observed):
                    if work.kind == "terminal":
                        activity, attribution = self._command_turn(work.reference)
                        observed[index] = replace(work, reference=replace(work.reference, activity_id=activity),
                            data={**work.data, "activity_attribution": attribution})
                for work in observed:
                    self._event("work.observed", work.reference, provenance=work.provenance,
                                data={"kind": work.kind, "state": work.state, **work.data}, partial=partial)
                primary_id = self._active.get(self._root)
                primary = self._reference(self._root, primary_id) if primary_id else None
                # Root occupancy comes from its attributable current read, not
                # aggregate child/terminal completeness. Work controls still
                # require the full nonpartial inventory.
                primary_state: Literal["idle", "active", "unknown"] = (
                    "unknown" if (self._pending_submission is not None or root_uncertain
                                  or not root_read_current or self._root in self._uncertain_activity)
                    else ("active" if primary_id else "idle"))
                return NativeSnapshot(self._identity, primary, tuple(observed), freshness="current", partial=partial,
                                      capabilities={CAP_AUTOMATION: "unverified"}, primary_state=primary_state,
                                      provenance="codex:owned-inventory")
        except (OSError, RuntimeContractError, ValueError):
            with self._lock:
                self._partial = True
                primary_id = self._active.get(self._root)
                return NativeSnapshot(self._identity, self._reference(self._root, primary_id) if primary_id else None,
                                      tuple(observed), freshness="stale", partial=True, provenance="codex:inventory-incomplete")

    def control(self, command: NativeControl, *, deadline: float) -> WriteReceipt:
        if self._closing and command.action != "close":
            return WriteReceipt("rejected", detail="generation is closing")
        if self._rpc is None or self._lost or not self._root:
            return WriteReceipt("rejected", detail="native runtime unavailable; owner cleanup still required")
        if command.action == "stop_automation":
            return WriteReceipt("unsupported", detail="native scheduling is unverified")
        if command.action == "close":
            cleanup = self.cleanup(deadline=deadline)
            return WriteReceipt("written" if cleanup.outcome == "complete" else "unknown", detail=cleanup.detail)
        if not self._allowed(command.action):
            return WriteReceipt("unsupported", detail="native control capability is not evidenced")
        target = command.target
        if target is None or target.root_id != self._root or target.thread_id not in self._parents:
            return WriteReceipt("rejected", detail="target is outside owned native ancestry")
        thread = target.thread_id
        assert thread is not None
        if target.activity_id and target.activity_id != command.expected_activity_id:
            return WriteReceipt("rejected", detail="control activity and expected identity differ")
        try:
            if command.action == "stop_reply":
                if thread != self._root or not command.expected_activity_id:
                    return WriteReceipt("rejected", detail="exact root activity required")
                self._interrupt(thread, command.expected_activity_id, deadline=deadline)
            elif target.native_process_id:
                if not self._allowed("stop_work_terminal"):
                    return WriteReceipt("unsupported", detail="terminal-stop target coverage is not evidenced")
                snapshot = self.inventory(deadline=deadline)
                if snapshot.partial or snapshot.freshness != "current" or not any(
                    work.kind == "terminal" and work.reference.thread_id == thread
                    and work.reference.native_process_id == target.native_process_id
                    and (target.activity_id is None or target.activity_id == work.reference.activity_id)
                    and (target.item_id is None or target.item_id == work.reference.item_id)
                    and (target.work_id is None or target.work_id == work.reference.work_id)
                    for work in snapshot.work):
                    return WriteReceipt("rejected", detail="terminal target is stale or unproved")
                result = _object(self._rpc.request("thread/backgroundTerminals/terminate", {
                    "threadId": thread, "processId": target.native_process_id}, deadline=deadline))
                if result.get("terminated") is not True:
                    return WriteReceipt("written", True, detail="native terminal termination not confirmed")
            elif thread != self._root and target.work_id == thread:
                if not self._allowed("stop_work_child"):
                    return WriteReceipt("unsupported", detail="child-stop target coverage is not evidenced")
                snapshot = self.inventory(deadline=deadline)
                if snapshot.partial or snapshot.freshness != "current":
                    return WriteReceipt("rejected", detail="child ancestry/inventory incomplete")
                active = self._active.get(thread)
                if active:
                    if active != command.expected_activity_id:
                        return WriteReceipt("rejected", detail="exact active child activity required")
                    self._interrupt(thread, active, deadline=deadline)
                elif command.expected_activity_id and self._turn_status.get(
                        (thread, command.expected_activity_id)) not in TURN_TERMINALS:
                    return WriteReceipt("rejected", detail="child activity outcome unknown")
                # Freeze terminal handles: never clean a later turn's newly spawned terminal.
                for work in snapshot.work:
                    if work.kind == "terminal" and work.reference.thread_id == thread:
                        self._rpc.request("thread/backgroundTerminals/terminate", {
                            "threadId": thread, "processId": work.reference.native_process_id}, deadline=deadline)
            else:
                return WriteReceipt("unsupported", detail="native control target is unsupported")
            self._event("control.acknowledged", target, provenance="codex:control-rpc", control_id=command.control_id,
                        data={"action": command.action})
            snapshot = self.inventory(deadline=deadline)
            completed = snapshot.freshness == "current" and not snapshot.partial
            if command.action == "stop_reply":
                completed &= self._turn_status.get((thread, command.expected_activity_id or "")) in TURN_TERMINALS
            elif target.native_process_id:
                completed &= not any(work.reference.native_process_id == target.native_process_id
                                     and work.reference.thread_id == thread for work in snapshot.work)
            else:
                completed &= not any(work.reference.thread_id == thread and (
                    work.kind == "terminal" or work.reference.activity_id) for work in snapshot.work)
                completed &= not any(parent == thread for parent in self._parents.values())
            self._event("control.outcome", target, provenance="codex:control-reconcile", control_id=command.control_id,
                        data={"outcome": "complete" if completed else "pending"}, partial=not completed)
            return WriteReceipt("written", True, detail="native control accepted; outcome separately observed")
        except (RuntimeContractError, OSError) as exc:
            self._event("control.outcome", target, provenance="codex:control-uncertain", control_id=command.control_id,
                        data={"outcome": "inconclusive"}, partial=True)
            state = exc.state if isinstance(exc, RpcError) else "unknown"
            return WriteReceipt(state, detail=getattr(exc, "code", "NATIVE_CONTROL_UNKNOWN"))

    def _interrupt(self, thread: str, expected: str, *, deadline: float) -> None:
        assert self._rpc is not None
        result = _object(self._rpc.request("thread/read", {"threadId": thread, "includeTurns": True}, deadline=deadline))
        native = _object(result.get("thread"))
        turns = native.get("turns")
        active = [turn for turn in turns if isinstance(turn, dict) and turn.get("status") == "inProgress"] if isinstance(turns, list) else []
        if native.get("id") != thread or len(active) != 1 or active[0].get("id") != expected:
            raise RpcError("NATIVE_TARGET_STALE", "exact activity is no longer active", state="rejected")
        self._rpc.request("turn/interrupt", {"threadId": thread, "turnId": expected}, deadline=deadline)

    def cleanup(self, *, deadline: float) -> NativeCleanup:
        with self._lock:
            if self._cleanup_thread is not None:
                return NativeCleanup("pending", detail="native cleanup is already in progress/completed; owner verifies unit")
            self._closing = True
            self._cleanup_thread = threading.get_ident()
        self._reconcile.set()
        if self._rpc is None:
            return NativeCleanup("complete", detail="no native transport; owner verifies unit separately")
        if not self._rpc.fence(deadline=deadline):
            return NativeCleanup("inconclusive", detail="in-flight native write did not fence; owner must stop unit")
        snapshot = self.inventory(deadline=deadline)
        try:
            # Native terminal handles remain thread-scoped; outer owner stops all cgroup work regardless.
            for thread in tuple(self._parents):
                if _remaining(deadline) <= 0:
                    break
                active = self._active.get(thread)
                if active:
                    self._interrupt(thread, active, deadline=deadline)
                self._rpc.request("thread/backgroundTerminals/clean", {"threadId": thread}, deadline=deadline)
            snapshot = self.inventory(deadline=deadline)
            exited = self._rpc.close(deadline=deadline)
        except (RuntimeContractError, OSError):
            exited = False
        if self._reconciler is not None:
            self._reconciler.join(timeout=_remaining(deadline))
        if exited:
            with self._lock:
                self._command_activity.clear()
                self._command_pending.clear()
                self._command_revision.clear()
        unresolved = [work.reference for work in snapshot.work
                      if work.kind == "terminal" or work.reference.activity_id or work.state == "unknown"]
        if snapshot.primary is not None:
            unresolved.append(snapshot.primary)
        return NativeCleanup("pending" if unresolved or snapshot.partial or not exited else "complete",
                             tuple(unresolved), detail="native cleanup only; owner must verify whole unit/cgroup")


def create_driver() -> RuntimeDriver:
    return CodexRuntimeDriver()
