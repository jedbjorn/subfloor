"""Finite native checks over an owner-allocated, separately supervised controller.

This module never prepares policy, launches units, chooses credentials, or
imports proof receipts. The owner supplies fixed isolated allocation/cleanup
and scoped physical observations. Every native intent is submitted once.
"""
from __future__ import annotations

import copy
import dataclasses
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from conversation_runtime import RuntimeClient, RuntimeStore
from conversation_runtime_checks import (
    CapabilityEvidence,
    CleanupProof,
    Diagnostic,
    Fingerprint,
    ProbeSession,
    RuntimeValidator,
)
from conversation_runtime_contract import (
    CAP_AUTOMATION,
    CAP_STOP_REPLY,
    CAP_STOP_WORK,
    CAP_SUBMISSION,
    EVENT_KINDS,
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
    payload_digest,
)
from conversation_runtime_controller import observed_claude_memory


@dataclass(frozen=True)
class OwnedProbe:
    context: RuntimeContext
    client: RuntimeClient
    store: RuntimeStore
    owner_user_id: int
    shell_id: int
    unit: str
    isolation_root: Path
    registered: bool
    # Fixed owner callbacks, bound to the synthetic row and captured unit.
    observe_marker: Callable[[str, float], bool] | None = None
    process_identity: Callable[[int], ProcessIdentity | None] | None = None
    on_identity: Callable[[RuntimeIdentity], None] | None = None
    # The owner presents this exact finite probe generation to the operator.
    # This notification neither confirms consent nor transfers it to a chat.
    on_setup: Callable[[StartupConsent | None], None] | None = None


def _timeout(deadline: float, limit: float = 5) -> float:
    remaining = deadline-time.monotonic()
    if remaining <= 0:
        raise TimeoutError("owned probe deadline expired")
    return min(remaining, limit)


def _known(cls, value: Mapping[str, Any]) -> dict[str, Any]:
    return {item.name: value[item.name] for item in dataclasses.fields(cls) if item.name in value}


def _reference(value: Mapping[str, Any] | None) -> NativeReference | None:
    if value is None:
        return None
    fields = _known(NativeReference, value)
    if fields.get("os_process") is not None:
        fields["os_process"] = ProcessIdentity(**_known(ProcessIdentity, fields["os_process"]))
    return NativeReference(**fields)


def _identity(value: Mapping[str, Any]) -> RuntimeIdentity:
    fields = _known(RuntimeIdentity, value)
    if fields.get("process") is not None:
        fields["process"] = ProcessIdentity(**_known(ProcessIdentity, fields["process"]))
    identity = RuntimeIdentity(**fields)
    if (not isinstance(identity.root_id, str) or not 1 <= len(identity.root_id) <= 255
            or not isinstance(identity.protocol, Mapping)):
        raise RuntimeContractError("PROBE_IDENTITY_INVALID", "captured native root/protocol required")
    return identity


class ControllerProbeDriver(RuntimeDriver):
    """Small private-client facade; continuous journal drain stays independent.

    The actual harness driver and every descendant stay in the owner's unit.
    This facade owns no native pipe and performs no retry of ambiguous intent.
    """
    def __init__(self, owned: OwnedProbe):
        self.owned = owned
        self.harness, self.revision = owned.context.harness, owned.context.driver_revision
        self.identity: RuntimeIdentity | None = None
        self.events: list[RuntimeEvent] = []
        self.diagnostics: list[Diagnostic] = []
        self.primary: str | None = None
        self._lock = threading.RLock()
        self._closing = False
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self._emit: EventSink | None = None
        self._deadline = 0.0
        self._sequence = 0
        self._validator: RuntimeValidator | None = None
        self._ready = False
        self._started = False
        self._setup: StartupConsent | None = None
        self._cleanup_lock = threading.Lock()
        self._native_cleanup: NativeCleanup | None = None
        self._first_native_cleanup: NativeCleanup | None = None
        self._first_close_has_outcome = False
        self._scenario: _Scenarios | None = None

    def _capture_identity(self, value: Mapping[str, Any]) -> None:
        identity = _identity(value)
        with self._lock:
            if self.identity is not None and (identity.root_id != self.identity.root_id
                    or identity.process != self.identity.process):
                raise RuntimeContractError("PROBE_IDENTITY_INVALID", "captured root/process changed")
            self.identity = identity
            if self._validator is None:
                self._validator = RuntimeValidator(identity.root_id, harness=self.harness)

    def _publish_setup(self, value: Mapping[str, Any] | None) -> None:
        setup = StartupConsent(**_known(StartupConsent, value)) if value is not None else None
        context = self.owned.context
        if setup is not None and (self.harness != "claude" or setup.generation_id != context.generation_id
                or setup.executable_sha256 != context.executable.sha256
                or setup.driver_revision != context.driver_revision):
            raise RuntimeContractError("PROBE_SETUP_INVALID", "setup differs from captured probe generation")
        with self._lock:
            if self._closing:
                setup = None
            if setup != self._setup:
                self._setup = setup
                if self.owned.on_setup is not None:
                    self.owned.on_setup(setup)

    def _attach(self, deadline: float) -> None:
        owned = self.owned
        lease = owned.store.attach(owned.context.generation_id, owned.owner_user_id,
                                   owned.shell_id, owned.client.consumer, lifetime=60)
        owned.client.lease = lease
        owned.client.request("attach", timeout=_timeout(deadline), **lease)

    def start(self, context: RuntimeContext, emit: EventSink, *, deadline: float) -> DriverStart:
        with self._lock:
            if self._closing or context != self.owned.context or self._started:
                return DriverStart("unavailable", detail="owned probe binding/start gate differs")
            self._started = True
            self._emit, self._deadline = emit, deadline
        self._attach(deadline)
        data = dataclasses.asdict(context)
        for key in ("state_root", "worktree", "controller_endpoint"):
            data[key] = str(data[key]) if data[key] is not None else None
        data["executable"]["path"] = str(data["executable"]["path"])
        data["managed_mcp_files"] = [str(path) for path in data["managed_mcp_files"]]
        with self._lock:
            if self._closing:
                return DriverStart("unknown", detail="owned cleanup fenced Open")
        result = self.owned.client.request("open", timeout=_timeout(deadline, 120), context=data)
        if result.get("identity") is not None:
            self._capture_identity(result["identity"])
        with self._lock:
            if self._closing:
                return DriverStart("unknown", self.identity, detail="owned cleanup fenced startup")
            self._reader = threading.Thread(target=self._drain, name="owned-native-probe-events", daemon=True)
            self._reader.start()
        # Open is submitted once. A consent acknowledgement is never readiness:
        # wait only on this already-owned controller's actual ready observation.
        try:
            while True:
                with self._lock:
                    if self._closing:
                        return DriverStart("unknown", self.identity, detail="owned cleanup fenced startup")
                if result.get("identity") is not None:
                    self._capture_identity(result["identity"])
                ready = result.get("state") == "ready" or result.get("ready") is True
                if result.get("lost") is True:
                    return DriverStart("unavailable", self.identity, detail="owned native readiness lost")
                if ready and self.identity is not None:
                    self._publish_setup(None)
                    with self._lock:
                        if self._closing:
                            return DriverStart("unknown", self.identity, detail="owned cleanup fenced readiness")
                        self._ready = True
                        if self.owned.on_identity is not None:
                            self.owned.on_identity(dataclasses.replace(self.identity, protocol=copy.deepcopy(self.identity.protocol)))
                        if self._closing:
                            return DriverStart("unknown", self.identity, detail="owned cleanup fenced readiness")
                    return DriverStart("ready", self.identity, capabilities=result.get("capabilities", {}))
                if result.get("state") == "unavailable":
                    return DriverStart("unavailable", self.identity, detail="owned native readiness unavailable")
                self._publish_setup(result.get("setup"))
                if self._stop.wait(min(.1, _timeout(deadline, .1))):
                    return DriverStart("unknown", self.identity, detail="owned cleanup withdrew startup")
                result = self.owned.client.request("status", timeout=_timeout(deadline))
                if result.get("generation") != context.generation_id:
                    raise RuntimeContractError("PROBE_SETUP_INVALID", "private status generation changed")
        except TimeoutError:
            return DriverStart("unknown", self.identity, detail="finite account/consent readiness expired")
        finally:
            self._publish_setup(None)

    def _drain(self) -> None:
        while not self._stop.is_set() and time.monotonic() < self._deadline:
            try:
                if self.owned.client.lease.get("expires", 0) < time.time()+20:
                    self._attach(self._deadline)
                replay = self.owned.client.request("subscribe", after=self._sequence,
                                                   timeout=_timeout(self._deadline))
                for item in replay.get("events", ()):
                    raw = item["event"]
                    if self._validator is not None:
                        with self._lock:
                            self.diagnostics.extend(self._validator.frame(raw))
                            self.diagnostics = self.diagnostics[-128:]
                    if raw.get("kind") not in EVENT_KINDS:
                        continue
                    try:
                        fields = _known(RuntimeEvent, raw)
                        fields["reference"] = _reference(fields.get("reference"))
                        event = RuntimeEvent(**fields)
                    except (RuntimeContractError, TypeError, ValueError, AttributeError):
                        continue  # recorded required-shape diagnostic; keep draining/Close
                    with self._lock:
                        if len(self.events) >= 2048:
                            raise RuntimeContractError("PROBE_EVENT_LIMIT", "finite probe journal limit")
                        self.events.append(event)
                    if self._emit is not None:
                        self._emit(event)
                committed = self.owned.store.ingest(self.owned.context.generation_id,
                    self.owned.owner_user_id, self.owned.shell_id, self.owned.client.lease, replay)
                self.owned.client.request("ack", sequence=committed, timeout=_timeout(self._deadline))
                with self._lock:
                    self._sequence = committed
                    self.primary = replay.get("primary")
                self._stop.wait(.1)
            except (RuntimeError, OSError, ValueError, TypeError, KeyError):
                with self._lock:
                    self.diagnostics.append(Diagnostic(CAP_SUBMISSION, "inconclusive", "PROBE_READER_UNAVAILABLE"))
                break

    def _dispatch(self, cid: str, kind: str, payload: Mapping[str, Any], deadline: float) -> dict:
        with self._lock:
            if (self._closing or not self._ready) and kind != "close":
                return {"state": "not_written", "detail": "owned cleanup fenced new intent"}
            _timeout(deadline)
            command = self.owned.store.intent(self.owned.context.generation_id, self.owned.owner_user_id,
                self.owned.shell_id, self.owned.client.lease, cid, kind, payload)
        try:
            result = self.owned.client.request("close" if kind == "close" else kind,
                                               command=command, timeout=_timeout(deadline))
        except (RuntimeError, OSError):
            # No new ID or prompt retry, even if the remote write succeeded.
            result = {"state": "unknown", "detail": "owned controller outcome unproved"}
        self.owned.store.receipt(self.owned.context.generation_id, self.owned.owner_user_id,
                                self.owned.shell_id, cid, result)
        return result

    def submit(self, command: NativeSubmission, *, deadline: float) -> WriteReceipt:
        result = self._dispatch(command.request_id, "submit", {
            "text": command.text, "source": command.source}, deadline)
        return WriteReceipt(**_known(WriteReceipt, result))

    def inventory(self, *, deadline: float) -> NativeSnapshot:
        result = self.owned.client.request("snapshot", timeout=_timeout(deadline))
        fields = _known(NativeSnapshot, result)
        fields["identity"] = _identity(result["identity"]) if result.get("identity") is not None else None
        fields["primary"] = _reference(fields.get("primary"))
        work = []
        for item in fields.get("work", ()):
            values = _known(NativeWork, item)
            values["reference"] = _reference(values["reference"])
            work.append(NativeWork(**values))
        fields["work"] = tuple(work)
        return NativeSnapshot(**fields)

    def control(self, command: NativeControl, *, deadline: float) -> WriteReceipt:
        result = self._dispatch(command.control_id, "control", {
            "action": command.action,
            "target": dataclasses.asdict(command.target) if command.target else None,
            "expected_activity_id": command.expected_activity_id, "options": dict(command.options)}, deadline)
        return WriteReceipt(**_known(WriteReceipt, result))

    def cleanup(self, *, deadline: float) -> NativeCleanup:
        with self._lock:
            self._closing = True
            self._ready = False
            self._stop.set()
            self._publish_setup(None)
        if not self._cleanup_lock.acquire(timeout=max(0, deadline-time.monotonic())):
            return NativeCleanup("inconclusive")
        try:
            # The owner may already have stopped the controller after an
            # explicit Close. Preserve its proved native result; the factory
            # still asks the owner for fresh scoped OS evidence on every call.
            if self._native_cleanup is not None:
                return self._native_cleanup
            result = self._dispatch("probe-close", "close", {"action": "close"}, deadline)
            if self._reader is not None:
                self._reader.join(timeout=min(1, max(0, deadline-time.monotonic())))
            unresolved = []
            for value in result.get("unresolved_work", ()):
                if not isinstance(value, dict):
                    return NativeCleanup("inconclusive")
                ref = _reference(value)
                if ref is None:
                    return NativeCleanup("inconclusive")
                unresolved.append(ref)
            outcome = result.get("outcome")
            has_outcome = isinstance(outcome, str) and outcome in {"complete", "pending", "failed", "inconclusive"}
            native_outcome = cast(Literal["complete", "pending", "failed", "inconclusive"], outcome) if has_outcome else "inconclusive"
            native = NativeCleanup(native_outcome, tuple(unresolved),
                                   tuple(result.get("unresolved_definitions", ())))
            with self._lock:
                if self._first_native_cleanup is None:
                    self._first_native_cleanup = native
                    self._first_close_has_outcome = has_outcome
            if native.outcome == "complete" and not native.unresolved_work and not native.unresolved_definitions:
                self._native_cleanup = native
            return native
        finally:
            self._cleanup_lock.release()


@dataclass
class _Allocation:
    reserving: bool = True
    closing: bool = False
    owned: OwnedProbe | None = None
    driver: ControllerProbeDriver | None = None
    cleaned: bool = False


class NativeProbeFactory:
    def __init__(self, allocate: Callable[[Fingerprint, frozenset[str], float], OwnedProbe],
                 cleanup: Callable[[Fingerprint, float], CleanupProof]):
        self._allocate, self._cleanup = allocate, cleanup
        self._lock = threading.Lock()
        self._allocations: dict[str, _Allocation] = {}

    def reserve(self, fingerprint: Fingerprint, capabilities: frozenset[str], *, deadline: float) -> ProbeSession:
        with self._lock:
            previous = self._allocations.get(fingerprint.key)
            if previous and (previous.reserving or not previous.cleaned):
                raise RuntimeContractError("PROBE_ALLOCATION_RETAINED", "earlier owned allocation unresolved")
            entry = _Allocation()
            self._allocations[fingerprint.key] = entry
        try:
            owned = self._allocate(fingerprint, capabilities, deadline)
            with self._lock:
                entry.owned = owned
                if entry.closing or time.monotonic() >= deadline:
                    raise RuntimeContractError("PROBE_ALLOCATION_FENCED", "cleanup fenced late allocation")
                driver = entry.driver = ControllerProbeDriver(owned)
            return ProbeSession(owned.context, driver, owned.unit, owned.isolation_root,
                owned.registered, lambda native, end: self._exercise(native, capabilities, end))
        finally:
            with self._lock:
                entry.reserving = False

    def cleanup(self, fingerprint: Fingerprint, *, deadline: float) -> CleanupProof:
        with self._lock:
            entry = self._allocations.get(fingerprint.key)
            if entry:
                entry.closing = True
            driver = entry.driver if entry else None
        native = NativeCleanup("inconclusive")
        try:
            if driver is not None:
                native = driver.cleanup(deadline=deadline)
        finally:
            # Owner fences allocations and verifies the exact registered unit,
            # even if reserve/start/RPC has not returned or native Close failed.
            proof = self._cleanup(fingerprint, deadline)
        with self._lock:
            if entry is not None and entry.reserving:
                return CleanupProof(False, "inconclusive")
            composite = proof
            if driver is not None:
                composite = CleanupProof(proof.unit_verified_exited,
                    proof.native_outcome if proof.native_outcome != "complete" else native.outcome,
                    tuple(dict.fromkeys((*proof.unresolved_work,
                        *(ref.work_id or ref.thread_id or ref.root_id for ref in native.unresolved_work)))),
                    tuple(dict.fromkeys((*proof.unresolved_definitions, *native.unresolved_definitions))))
            if entry is not None:
                entry.cleaned = composite.complete
            return composite

    def witness(self, fingerprint: Fingerprint) -> dict[str, Any]:
        """Fixed semantic diagnostics only; never native payloads or intent IDs."""
        with self._lock:
            entry = self._allocations.get(fingerprint.key)
            driver = entry.driver if entry else None
        if driver is None:
            return {"waiting_stage": "allocation", "first_close_observed": False}
        with driver._lock:
            scenario, closing = driver._scenario, driver._closing
            cleanup = driver._first_native_cleanup
            retained = driver._native_cleanup is not None
            observed = driver._first_close_has_outcome
        result: dict[str, Any] = scenario.witness() if scenario else {"waiting_stage": "startup"}
        result.update(first_close_observed=observed,
            first_close_outcome=cleanup.outcome if cleanup and cleanup.outcome in {
                "complete", "pending", "failed", "inconclusive"} else "inconclusive",
            first_close_unresolved_work=len(cleanup.unresolved_work) if cleanup else 0,
            first_close_unresolved_definitions=len(cleanup.unresolved_definitions) if cleanup else 0,
            native_complete_retained=retained, cleanup_fenced=closing)
        return result

    @staticmethod
    def _exercise(native: RuntimeDriver, capabilities: frozenset[str], deadline: float) -> Mapping[str, CapabilityEvidence]:
        if not isinstance(native, ControllerProbeDriver):
            raise RuntimeContractError("PROBE_DRIVER_INVALID", "owned controller proxy required")
        if native.harness == "claude":
            from conversation_runtime_claude_probe import ClaudeScenarios
            return ClaudeScenarios(native, capabilities, deadline).run()
        return _Scenarios(native, capabilities, deadline).run()


class _Scenarios:
    def __init__(self, driver: ControllerProbeDriver, capabilities: frozenset[str], deadline: float):
        self.driver, self.caps = driver, capabilities
        # Return scoped measurements before the checker outer RPC deadline.
        self.deadline = deadline-min(.25, max(0.0, deadline-time.monotonic())/10)
        self.owned = driver.owned
        self.nonce = uuid.uuid4().hex[:16]
        self.ordinal = 0
        self.coverage: dict[str, set[str]] = {cap: set() for cap in capabilities}
        self.failures: list[Diagnostic] = []
        self.stage = CAP_SUBMISSION
        self.waiting_stage = "startup"
        self.first_request: str | None = None
        self.first_activity: str | None = None
        self.second_request: str | None = None
        self.work_observation: dict[str, Any] = {"initial_snapshot_observed": False,
            "initial_snapshot_current": False, "initial_snapshot_partial": False,
            "initial_root_terminal_current": False, "initial_child_ancestry_current": False,
            "initial_child_active_turn_present": False, "observed_child_terminal_current": False,
            "root_tagged_pid_candidates": 0, "root_owned_pid_matches": 0,
            "child_tagged_pid_candidates": 0, "child_owned_pid_matches": 0,
            "sibling_tagged_pid_candidates": 0, "sibling_owned_pid_matches": 0}
        for role in ("root", "sibling", "child"):
            self.work_observation.update({role+"_rejected_pid_output_records": 0,
                                          role+"_pid_observation": "unobserved"})
        self._root_label: str | None = None
        self._child_label: str | None = None
        self._sibling_label: str | None = None
        with driver._lock:
            driver._scenario = self

    def _phase(self, stage: str) -> None:
        with self.driver._lock:
            self.waiting_stage = stage

    def witness(self) -> dict[str, Any]:
        with self.driver._lock:
            request, activity, second = self.first_request, self.first_activity, self.second_request
            stage, root, events = self.waiting_stage, self.driver.identity, tuple(self.driver.events)
            work = dict(self.work_observation)
        matched = [e for e in events if request and activity and self._root_event(e, request, activity)]
        statuses = ("completed", "failed", "interrupted", "other")
        root_terminals = dict.fromkeys(statuses, 0)
        child_terminals = dict.fromkeys(statuses, 0)
        for event in events:
            if event.kind != "activity.terminal":
                continue
            status = event.data.get("status")
            key = status if isinstance(status, str) and status in statuses else "other"
            if event in matched:
                root_terminals[key] += 1
            ref = event.reference
            if (root and ref and ref.root_id == root.root_id and ref.thread_id not in {None, root.root_id}
                    and event.freshness == "current" and not event.partial
                    and event.grade not in {"incompatible", "inconclusive"}):
                child_terminals[key] += 1
        def text(items: list[RuntimeEvent]) -> str:
            return "".join(e.data["text"] for e in items if e.kind == "output.final" and e.reference
                and e.reference.work_id is None and e.reference.native_process_id is None
                and isinstance(e.data.get("text"), str))
        reply = "READY "+self.nonce in text(matched)
        recalled = [e for e in events if second and e.reference and e.reference.activity_id
                    and self._root_event(e, second, e.reference.activity_id)]
        processed = any(e.kind == "activity.processed" for e in matched)
        return {"waiting_stage": stage, "first_root_processed": processed,
                "first_root_terminal_counts": root_terminals, "child_terminal_counts": child_terminals,
                "first_final_nonce_matches": reply, "first_successful_reply": bool(
                    processed and root_terminals["completed"] and reply),
                "second_final_nonce_matches": self.nonce in text(recalled), **work}

    def _wait(self, predicate: Callable[[], Any]) -> Any:
        while time.monotonic() < self.deadline:
            found = predicate()
            if found:
                return found
            time.sleep(.1)
        raise TimeoutError("finite native behavior unproved")

    def _events(self) -> tuple[RuntimeEvent, ...]:
        with self.driver._lock:
            return tuple(self.driver.events)

    def _interrupted(self, target: NativeReference, request: str | None = None) -> bool:
        # A successful interrupt RPC is separate from native turn truth. A
        # completed/failed turn may have won the race before the control; only
        # the consumed native interrupted status proves this finite action.
        return any(e.kind == "activity.terminal" and e.reference is not None
            and e.data.get("status") == "interrupted"
            and e.freshness == "current" and not e.partial
            and e.grade not in {"inconclusive", "incompatible"}
            and (request is None or e.request_id == request)
            and all(getattr(e.reference, key) == getattr(target, key)
                for key in ("root_id", "thread_id", "parent_thread_id", "activity_id"))
            for e in self._events())

    def _root_event(self, event: RuntimeEvent, request: str, activity: str) -> bool:
        ref, root = event.reference, self.driver.identity
        return bool(ref and root and event.request_id == request and ref.root_id == root.root_id
            and ref.activity_id == activity and (ref.thread_id == root.root_id
                or self.driver.harness == "claude" and ref.thread_id is None)
            and event.freshness == "current" and not event.partial
            and event.grade not in {"incompatible", "inconclusive"})

    def _successful_reply(self, request: str, receipt: WriteReceipt, text: str) -> bool:
        activity = receipt.native_activity_id
        if not activity:
            return False
        events = [event for event in self._events() if self._root_event(event, request, activity)]
        terminal = any(e.kind == "activity.terminal" and e.data.get("status") == "completed" for e in events)
        processed = any(e.kind == "activity.processed" for e in events)
        # Native command output is useful for owned PID observations, but it
        # cannot stand in for the assistant's actual completed nonce reply.
        output = "".join(e.data.get("text", "") for e in events if e.kind == "output.final"
                         and e.reference is not None and e.reference.work_id is None
                         and e.reference.native_process_id is None and isinstance(e.data.get("text"), str))
        return terminal and processed and text in output

    def _submit(self, text: str) -> tuple[str, WriteReceipt]:
        self.ordinal += 1
        request = "probe-"+self.nonce+"-"+str(self.ordinal)
        with self.driver._lock:
            if self.ordinal == 1:
                self.first_request = request
            elif self.ordinal == 2:
                self.second_request = request
        receipt = self.driver.submit(NativeSubmission(request, self.ordinal,
            payload_digest({"text": text}), text), deadline=self.deadline)
        if receipt.state not in {"written", "unknown"}:
            raise RuntimeContractError("PROBE_SUBMISSION_UNPROVED", "no blind retry of native intent")
        # An ambiguous transport receipt may resolve through this exact intent's
        # processing event. It never permits replay or a new prompt by itself.
        processed = self._wait(lambda: next((event for event in self._events()
            if event.kind == "activity.processed" and event.reference is not None
            and event.reference.activity_id and self._root_event(event, request, event.reference.activity_id)
            and (receipt.native_activity_id is None or receipt.native_activity_id == event.reference.activity_id)), None))
        receipt = dataclasses.replace(receipt, native_activity_id=processed.reference.activity_id)
        if self.ordinal == 1:
            with self.driver._lock:
                self.first_activity = processed.reference.activity_id
        return request, receipt

    def _control(self, action: Literal["stop_reply", "stop_work"], ref: NativeReference, expected: str | None = None) -> str:
        self.ordinal += 1
        control_id = "probe-control-"+self.nonce+"-"+str(self.ordinal)
        receipt = self.driver.control(NativeControl(control_id,
            self.ordinal, "probe-digest", action, ref, expected), deadline=self.deadline)
        if receipt.state not in {"written", "unknown"}:
            raise RuntimeContractError("PROBE_CONTROL_UNPROVED", "matching native control unproved")
        return control_id

    def _control_completed(self, control: str, target: NativeReference) -> bool:
        def matches(event: RuntimeEvent) -> bool:
            ref = event.reference
            return bool(ref and event.control_id == control and event.freshness == "current"
                and not event.partial and event.grade not in {"incompatible", "inconclusive"}
                and all(getattr(ref, key) == getattr(target, key) for key in
                    ("root_id", "thread_id", "activity_id", "item_id", "work_id", "native_process_id")
                    if getattr(target, key) is not None))
        return any(e.kind == "control.outcome" and e.data.get("outcome") == "complete"
                   and matches(e) for e in self._events())

    def _retained_root(self, root: RuntimeIdentity, snapshot: NativeSnapshot) -> bool:
        current = snapshot.identity
        return bool(root.process and self._alive(root.process) and current
                    and current.root_id == root.root_id and current.process == root.process)

    def _inventory(self) -> NativeSnapshot:
        result = self.driver.inventory(deadline=self.deadline)
        if self.waiting_stage == "initial_snapshot":
            with self.driver._lock:
                self.work_observation.update(initial_snapshot_observed=True,
                    initial_snapshot_current=result.freshness == "current", initial_snapshot_partial=result.partial)
        if result.partial or result.freshness != "current":
            raise RuntimeContractError("PROBE_INVENTORY_PARTIAL", "complete owned snapshot required")
        return result

    def _pid(self, label: str, target: NativeWork) -> ProcessIdentity | None:
        key = "root" if label == self._root_label else "child" if label == self._child_label else "sibling" if label == self._sibling_label else None
        if key:
            with self.driver._lock:
                self.work_observation.update({key+"_tagged_pid_candidates": 0, key+"_owned_pid_matches": 0,
                    key+"_rejected_pid_output_records": 0, key+"_pid_observation": "missing_terminal_output"})
        outputs: list[str] = []
        offsets: dict[tuple[Any, ...], dict[int, tuple[str, bool]]] = {}
        invalid_offsets: set[tuple[Any, ...]] = set()
        parts: dict[tuple[Any, ...], dict[int, tuple[str, bool]]] = {}
        ambiguous: set[tuple[Any, ...]] = set()
        rejected = 0
        root = self.driver.identity
        if (not root or target.kind != "terminal" or target.freshness != "current"
                or target.grade in {"incompatible", "inconclusive"}
                or target.reference.root_id != root.root_id or not target.reference.thread_id
                or not target.reference.activity_id or not target.reference.item_id
                or target.state not in {"running", "inProgress"}):
            return None
        for event in self._events():
            ref = event.reference
            value = event.data.get("text")
            if not (event.kind in {"output.delta", "output.final"} and isinstance(value, str)):
                continue
            scope = bool(ref and all(getattr(ref, key) == getattr(target.reference, key)
                for key in ("root_id", "thread_id", "parent_thread_id", "activity_id", "item_id"))
                and all(getattr(ref, key) in {None, getattr(target.reference, key)}
                    for key in ("work_id", "native_process_id")))
            if (not scope or event.data.get("kind") != "terminal" or event.partial
                    or event.freshness != "current" or event.grade in {"inconclusive", "incompatible"}):
                rejected += bool(re.search(re.escape(label)+r"=(\d+)\b", value))
                continue
            if ref:
                digest, part = event.data.get("text_digest"), event.data.get("part")
                output_key = (ref.thread_id, ref.activity_id, ref.item_id, event.kind,
                              digest if isinstance(digest, str) else None)
                if "text_digest" in event.data:
                    last = event.data.get("last")
                    if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                            or type(part) is not int or not 0 <= part < 128 or type(last) is not bool):
                        ambiguous.add(output_key)
                        continue
                    rows = parts.setdefault(output_key, {})
                    if part in rows and rows[part] != (value, last):
                        ambiguous.add(output_key)
                    rows[part] = (value, last)
                else:
                    offset, complete = event.data.get("offset"), event.data.get("complete")
                    if type(offset) is not int or not 0 <= offset < 512 * 1024 or type(complete) is not bool:
                        invalid_offsets.add(output_key)
                        continue
                    # Codex offsets are local to each normalized native output
                    # record, not a cumulative item stream. Every offset zero
                    # begins another record; finals and delta packets may reset.
                    if offset == 0:
                        offsets[output_key] = {}
                        invalid_offsets.discard(output_key)
                    rows = offsets.setdefault(output_key, {})
                    if offset in rows:
                        if rows[offset] != (value, complete):
                            invalid_offsets.add(output_key)
                        # Exact repeated chunks do not append another copy.
                        continue
                    if offset != sum(len(text) for text, _ in rows.values()):
                        invalid_offsets.add(output_key)
                    rows[offset] = (value, complete)
                    if complete:
                        if output_key not in invalid_offsets:
                            outputs.append("".join(text for text, _ in rows.values()))
                        offsets.pop(output_key, None)
                        invalid_offsets.discard(output_key)
        for output_key, rows in parts.items():
            last_parts = [part for part, (_, last) in rows.items() if last]
            if (output_key not in ambiguous and last_parts == [len(rows)-1]
                    and sorted(rows) == list(range(len(rows)))):
                outputs.append("".join(rows[part][0] for part in sorted(rows)))
        # The fixed command prints a complete newline-terminated PID line.
        # An unfinished delta cannot qualify before later digits arrive.
        # Distinct full-text records/turns/items cannot append digits to PIDs;
        # contiguous complete-record chunks reassemble and replay parts dedup.
        text = "\n".join(outputs)
        matches = list(dict.fromkeys(re.findall(re.escape(label)+r"=([1-9]\d*)\r?\n", text)))[:128]
        callback = self.owned.process_identity
        identities = []
        if callback is not None:
            for match in matches:
                identity = callback(int(match))
                if identity is not None and identity.pid == int(match):
                    identities.append(identity)
        key = "root" if label == self._root_label else "child" if label == self._child_label else "sibling" if label == self._sibling_label else None
        if key:
            with self.driver._lock:
                self.work_observation[key+"_tagged_pid_candidates"] = min(len(matches), 128)
                self.work_observation[key+"_owned_pid_matches"] = min(len(identities), 128)
                self.work_observation[key+"_rejected_pid_output_records"] = min(rejected, 128)
                self.work_observation[key+"_pid_observation"] = (
                    "matched" if len(identities) == 1 else "ambiguous" if len(identities) > 1
                    else "no_owned_match" if matches else "missing_terminal_output")
        return identities[0] if len(identities) == 1 else None

    def _terminal_pid(self, label: str, thread: str) -> tuple[NativeWork, ProcessIdentity] | None:
        # A current complete inventory qualifies the exact output item. An
        # assistant echo or a stale/missing terminal cannot establish OS scope.
        found = []
        key = "root" if label == self._root_label else "child" if label == self._child_label else "sibling" if label == self._sibling_label else None
        totals = {"tagged_pid_candidates": 0, "owned_pid_matches": 0, "rejected_pid_output_records": 0}
        for work in self._inventory().work:
            if work.kind == "terminal" and work.freshness == "current" and work.reference.thread_id == thread:
                identity = self._pid(label, work)
                if key:
                    with self.driver._lock:
                        for suffix in totals:
                            totals[suffix] += self.work_observation[key+"_"+suffix]
                if identity is not None:
                    found.append((work, identity))
        if key:
            with self.driver._lock:
                self.work_observation.update({key+"_"+suffix: min(count, 128) for suffix, count in totals.items()})
                self.work_observation[key+"_pid_observation"] = (
                    "matched" if len(found) == 1 else "ambiguous" if len(found) > 1
                    else "no_owned_match" if totals["tagged_pid_candidates"] else "missing_terminal_output")
        return found[0] if len(found) == 1 else None

    @staticmethod
    def _terminal_present(snapshot: NativeSnapshot, target: NativeWork) -> bool:
        return any(work.kind == "terminal" and work.freshness == "current"
            and work.state in {"running", "inProgress"} and work.grade not in {"incompatible", "inconclusive"}
            and all(getattr(work.reference, key) == getattr(target.reference, key)
                for key in ("root_id", "thread_id", "parent_thread_id", "activity_id", "item_id", "work_id", "native_process_id")
                if getattr(target.reference, key) is not None) for work in snapshot.work)

    def _alive(self, process: ProcessIdentity) -> bool:
        callback = self.owned.process_identity
        return callback is not None and callback(process.pid) == process

    def run(self) -> Mapping[str, CapabilityEvidence]:
        if not self.caps-{CAP_AUTOMATION}:
            return {CAP_AUTOMATION: CapabilityEvidence(CAP_AUTOMATION, "inconclusive", diagnostics=(
                Diagnostic(CAP_AUTOMATION, "inconclusive", "NATIVE_AUTOMATION_UNVERIFIED"),))}
        root = self.driver.identity
        policy = root.protocol.get("memory_policy", {}) if root else {}
        if not isinstance(policy, dict):
            policy = {}
        if self.driver.harness == "codex":
            memory = (policy.get("generate_memories") is False and policy.get("use_memories") is False
                      and policy.get("feature_enabled") is False and policy.get("root_mode") == "disabled")
        elif self.driver.harness == "claude":
            # D413 accepts this explicitly labelled configuration/source/owned
            # hook inference. It does not assert native effective telemetry.
            _, memory = observed_claude_memory(policy, self.owned.context, root)
        else:
            memory = False
        route = root.protocol.get("native_route", {}) if root else {}
        route_observed = (isinstance(route, dict) and route.get("model") == self.owned.context.model
                          and route.get("account_type") == "chatgpt"
                          and isinstance(route.get("efforts"), list)
                          and (self.owned.context.effort in {None, "default"} or self.owned.context.effort in route["efforts"])
                          if self.driver.harness == "codex" else isinstance(route, dict)
                          and route.get("model") == self.owned.context.model
                          and route.get("account_type") == "claude.ai"
                          and isinstance(route.get("efforts"), list)
                          and self.owned.context.effort in route["efforts"])
        grants = self.owned.context.probe_capabilities
        if (not memory or not route_observed or root is None or root.process is None
                or self.owned.process_identity is None or not self._alive(root.process)
                or self.owned.observe_marker is None
                or CAP_SUBMISSION not in grants):
            return {cap: CapabilityEvidence(cap, "inconclusive", diagnostics=(
                Diagnostic(cap, "inconclusive", "PROBE_ROUTE_MEMORY_BOOT_OR_GRANT_UNPROVED"),)) for cap in self.caps}
        child_enabled = self.driver.harness == "codex" and "stop_work_child" in grants and CAP_STOP_WORK in self.caps
        work_enabled = CAP_STOP_WORK in self.caps and "stop_work_terminal" in grants
        background = work_enabled or CAP_STOP_REPLY in self.caps
        root_label, child_label = "F89_ROOT_"+self.nonce, "F89_CHILD_"+self.nonce
        sibling_label = "F89_SIBLING_"+self.nonce
        self._root_label, self._child_label, self._sibling_label = root_label, child_label, sibling_label
        root_command = f"python -u -c 'import os,time;print(\"{root_label}=\"+str(os.getpid()),flush=True);time.sleep(105)'"
        sibling_command = f"python -u -c 'import os,time;print(\"{sibling_label}=\"+str(os.getpid()),flush=True);time.sleep(110)'"
        child_command = f"python -u -c 'import os,time;print(\"{child_label}=\"+str(os.getpid()),flush=True);time.sleep(110)'"
        prompt = ("Authorized finite synthetic compatibility probe. Obey the no-memory boot. Never inspect "
                  "credentials/environment or unrelated files. Call managed browser fixture_identity and "
                  f"fixture_state with marker {self.nonce}. Remember nonce {self.nonce}. ")
        if background:
            prompt += f"Start exactly one background terminal running {root_command}, yield 1000ms. "
            prompt += f"Start one additional independent root sibling background terminal running {sibling_command}, yield 1000ms. "
        if child_enabled:
            prompt += (f"Spawn exactly one native child inheriting this model/effort. It starts exactly one "
                       f"background terminal running {child_command}, yield 1000ms, then waits only on that "
                       "same terminal and reports output. No additional resources or agents. Do not wait for child. ")
        prompt += "Immediately reply READY plus the nonce and any observed tagged process IDs. No other tools/resources."
        try:
            self._phase("first_processing")
            first, first_receipt = self._submit(prompt)
            self._phase("first_reply")
            self._wait(lambda: self._successful_reply(first, first_receipt, "READY "+self.nonce))
            if not self.owned.observe_marker(self.nonce, self.deadline):
                raise RuntimeContractError("PROBE_BOOT_FIXTURE_UNPROVED", "physical managed marker not observed")
            self._phase("nonce_recall")
            second, second_receipt = self._submit("Reply only with the remembered nonce from the previous message. No tools or resources.")
            self._wait(lambda: self._successful_reply(second, second_receipt, self.nonce))
            root_snapshot = self.driver.inventory(deadline=self.deadline)
            if root_snapshot.freshness != "current" or not self._retained_root(root, root_snapshot):
                raise RuntimeContractError("PROBE_ROOT_IDENTITY_UNPROVED", "same captured root/process required")
            if CAP_SUBMISSION in self.coverage:
                self.coverage[CAP_SUBMISSION].update({"repeated_input", "root_identity", "processing_terminal", "boot_fixture", "memory_disabled"})
            # Work/PID observations qualify controls, independently of the
            # already successful root messaging and captured root identity.
            self.stage = CAP_STOP_REPLY if CAP_STOP_REPLY in self.caps else "stop_work_terminal"
            self._phase("initial_snapshot")
            initial = self._inventory()
            root_work = next((w for w in initial.work if w.kind == "terminal" and w.freshness == "current" and w.reference.thread_id == root.root_id), None)
            child = next((w for w in initial.work if w.kind == "child" and w.freshness == "current" and w.reference.parent_thread_id == root.root_id), None)
            with self.driver._lock:
                self.work_observation.update(initial_root_terminal_current=bool(root_work and root_work.freshness == "current"),
                    initial_child_ancestry_current=bool(child and child.freshness == "current"),
                    initial_child_active_turn_present=bool(child and child.reference.activity_id))
            self._phase("root_tagged_pid")
            root_pid = sibling_pid = None
            sibling_work = None
            if background:
                root_work, root_pid = self._wait(lambda: self._terminal_pid(root_label, root.root_id))
                self._phase("sibling_tagged_pid")
                sibling_work, sibling_pid = self._wait(lambda: self._terminal_pid(sibling_label, root.root_id))
                if sibling_work.reference == root_work.reference or sibling_pid == root_pid:
                    raise RuntimeContractError("PROBE_SIBLING_UNPROVED", "distinct owned root sibling required")
            child_terminal = None
            child_pid = None
            if CAP_STOP_REPLY in self.caps:
                self.stage = CAP_STOP_REPLY
                self._phase("stop_reply")
                if CAP_STOP_REPLY not in grants or root_work is None or root_pid is None:
                    raise RuntimeContractError("PROBE_STOP_REPLY_GRANT_UNPROVED", "background isolation/grant required")
                stopped, receipt = self._submit("Write 1000 numbered finite fixture lines. No tools/resources.")
                self._wait(lambda: self.driver.primary == receipt.native_activity_id)
                target = NativeReference(root.root_id, root.root_id, activity_id=receipt.native_activity_id)
                reply_control = self._control("stop_reply", target, receipt.native_activity_id)
                self._wait(lambda: self._control_completed(reply_control, target))
                self._wait(lambda: self._interrupted(target, stopped))
                after = self._inventory()
                if (not self._retained_root(root, after) or not self._alive(root_pid)
                        or not self._terminal_present(after, root_work)
                        or sibling_pid is None or not self._alive(sibling_pid)
                        or sibling_work is None or not self._terminal_present(after, sibling_work)):
                    raise RuntimeContractError("PROBE_BACKGROUND_ISOLATION_BROKEN", "root interruption changed background ownership")
                self.coverage[CAP_STOP_REPLY].update({"expected_activity", "terminal", "background_isolation"})
            if work_enabled and root_work is not None and root_pid is not None:
                self.stage = "stop_work_terminal"
                self._phase("stop_terminal")
                terminal_control = self._control("stop_work", root_work.reference)
                self._wait(lambda: self._control_completed(terminal_control, root_work.reference))
                self._wait(lambda: not self._alive(root_pid))
                self._wait(lambda: not any(w.kind == "terminal" and w.reference.thread_id == root.root_id
                    and w.reference.native_process_id == root_work.reference.native_process_id for w in self._inventory().work))
                after = self._inventory()
                if (not self._retained_root(root, after)
                        or sibling_pid is None or not self._alive(sibling_pid)
                        or sibling_work is None or not self._terminal_present(after, sibling_work)):
                    raise RuntimeContractError("PROBE_SIBLING_ISOLATION_BROKEN", "terminal stop changed root/sibling")
                if self.driver.harness == "claude":
                    self._wait(lambda: any(e.kind == "control.acknowledged" and e.control_id == terminal_control
                        and e.provenance == "claude:TaskStop-PostToolUse" and e.grade == "compatible" for e in self._events()))
                    self._wait(lambda: any(e.kind == "control.outcome" and e.control_id == terminal_control
                        and e.data.get("native_outcome") == "native_stopped" and e.freshness == "current"
                        and not e.partial for e in self._events()))
                    self.coverage[CAP_STOP_WORK].update({"native_TaskStop", "matched_success", "later_complete_snapshot"})
                self.coverage[CAP_STOP_WORK].update({"owned_work", "terminal_outcome", "sibling_isolation",
                    "target:terminal", "terminal_identity"})
            if child_enabled:
                self.stage = "stop_work_child"
                self._phase("child_ancestry")
                child = next((w for w in self._inventory().work if w.kind == "child" and w.freshness == "current"
                    and w.reference.parent_thread_id == root.root_id), None)
                if child is None or not child.reference.activity_id:
                    raise RuntimeContractError("PROBE_CHILD_UNPROVED", "owned child ancestry/current turn required")
                self._phase("child_tagged_pid")
                child_terminal, child_pid = self._wait(lambda: self._terminal_pid(child_label, child.reference.thread_id or ""))
                with self.driver._lock:
                    self.work_observation["observed_child_terminal_current"] = True
                self.stage = "stop_work_child"
                self._phase("stop_child")
                fresh = next(w for w in self._inventory().work if w.kind == "child" and w.freshness == "current"
                    and w.reference.root_id == root.root_id and w.reference.parent_thread_id == root.root_id
                    and w.reference.thread_id == child.reference.thread_id
                    and w.reference.activity_id == child.reference.activity_id)
                child_control = self._control("stop_work", fresh.reference, fresh.reference.activity_id)
                self._wait(lambda: self._control_completed(child_control, fresh.reference))
                self._wait(lambda: not self._alive(child_pid))
                self._wait(lambda: self._interrupted(fresh.reference))
                self._wait(lambda: not any(w.kind == "terminal" and w.reference.thread_id == child.reference.thread_id
                    and w.reference.native_process_id == child_terminal.reference.native_process_id for w in self._inventory().work))
                after = self._inventory()
                if (not self._retained_root(root, after) or sibling_pid is not None and not self._alive(sibling_pid)
                        or sibling_work is not None and not self._terminal_present(after, sibling_work)):
                    raise RuntimeContractError("PROBE_SIBLING_ISOLATION_BROKEN", "child stop changed root process/thread/sibling")
                self.coverage[CAP_STOP_WORK].update({"owned_work", "terminal_outcome", "sibling_isolation", "target:child",
                    "child_ancestry", "child_expected_activity", "child_interrupt_terminal", "child_scoped_terminal_cleanup"})
        except (RuntimeError, OSError, ValueError, StopIteration) as exc:
            code = exc.code if isinstance(exc, RuntimeContractError) else "NATIVE_BEHAVIOR_UNPROVED"
            failure_grade: Grade = "incompatible" if code in {"PROBE_SIBLING_ISOLATION_BROKEN", "PROBE_BACKGROUND_ISOLATION_BROKEN"} else "inconclusive"
            self.failures.append(Diagnostic(self.stage, failure_grade, code))
        if not self.failures:
            self._phase("finished")
        result = {}
        for cap in self.caps:
            diagnostics = tuple(d for d in (*self.driver.diagnostics, *self.failures) if d.capability in {cap, "stop_work_terminal", "stop_work_child"}
                                and (cap == CAP_STOP_WORK or d.capability == cap))
            broad = tuple(d for d in diagnostics if d.capability == cap)
            grade: Grade = "incompatible" if any(d.grade == "incompatible" for d in broad) else "inconclusive" if broad else "compatible"
            if cap == CAP_AUTOMATION or not self.coverage[cap] and grade != "incompatible":
                grade = "inconclusive"
            result[cap] = CapabilityEvidence(cap, grade, frozenset(self.coverage[cap]),
                ("native:owned_controller_finite_scenario",), diagnostics)
        return result
