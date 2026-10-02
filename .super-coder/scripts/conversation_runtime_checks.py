"""F89 installed identity, capability evidence and bounded isolated checks.

Integration owns service lifetime and the pre-registered probe allocation. This
module never changes a living generation, creates a unit, prepares boot/policy,
refreshes the model catalogue, or retries an ambiguous native submission.
"""
from __future__ import annotations

import hashlib
import math
import queue
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Protocol

from conversation_runtime_contract import (
    CAP_AUTOMATION,
    CAP_STOP_REPLY,
    CAP_STOP_WORK,
    CAP_SUBMISSION,
    CONTRACT_REVISION,
    EVENT_KINDS,
    GUI_CAPABILITIES,
    ExecutableBinding,
    Grade,
    NativeReference,
    ProcessIdentity,
    RuntimeContext,
    RuntimeContractError,
    RuntimeDriver,
    RuntimeEvent,
    payload_digest,
    public_payload,
)

PROBE_SECONDS = 180.0
CLEANUP_RESERVE = 20.0
REQUIRED_COVERAGE = {
    CAP_SUBMISSION: frozenset({"repeated_input", "root_identity", "processing_terminal", "boot_fixture", "memory_disabled"}),
    CAP_STOP_REPLY: frozenset({"expected_activity", "terminal", "background_isolation"}),
    CAP_STOP_WORK: frozenset({"owned_work", "terminal_outcome", "sibling_isolation"}),
    CAP_AUTOMATION: frozenset({"creation_guard", "non_durable", "fire", "delete", "definitions_cleanup"}),
}


# A capability pass admits only the target kinds actually exercised. No
# terminal-only observation certifies native child/user-agent cancellation.
TARGET_COVERAGE = {
    "codex": {
        "stop_work_terminal": frozenset({"target:terminal", "terminal_identity", "terminal_outcome", "sibling_isolation"}),
        "stop_work_child": frozenset({"target:child", "child_ancestry", "child_expected_activity", "child_interrupt_terminal", "child_scoped_terminal_cleanup", "sibling_isolation"}),
    },
    "claude": {
        "stop_work_terminal": frozenset({"target:terminal", "native_TaskStop", "matched_success", "later_complete_snapshot", "sibling_isolation"}),
    },
}


def _covered(fingerprint: Fingerprint, evidence: CapabilityEvidence, *, cleanup: bool) -> bool:
    required = REQUIRED_COVERAGE[evidence.capability] | ({"owned_unit_cleanup"} if cleanup else set())
    return (required <= evidence.coverage and bool(evidence.provenance)
            and (evidence.capability != CAP_STOP_WORK or any(
                target <= evidence.coverage and not any(d.capability == variant and d.grade != "compatible" for d in evidence.diagnostics)
                for variant, target in TARGET_COVERAGE.get(fingerprint.harness, {}).items())))


@dataclass(frozen=True)
class Diagnostic:
    capability: str
    grade: Grade
    code: str
    path: str = ""


@dataclass(frozen=True)
class Fingerprint:
    harness: str
    executable: ExecutableBinding
    driver_revision: str
    policy_digest: str
    provider: str | None
    model: str | None
    effort: str | None
    settings_digest: str
    implementation_digest: str
    contract_revision: str = CONTRACT_REVISION

    def __post_init__(self) -> None:
        if not self.implementation_digest or not self.settings_digest:
            raise ValueError("captured implementation and canonical settings identity required")

    @classmethod
    def capture(cls, context: RuntimeContext, *, settings_digest: str, implementation_digest: str) -> Fingerprint:
        # No generation/conversation/boot IDs: identical relevant policy and
        # route share bounded inference evidence across chats.
        return cls(context.harness, context.executable, context.driver_revision,
                   context.policy_digest, context.provider, context.model,
                   context.effort, settings_digest, implementation_digest)

    @property
    def key(self) -> str:
        value = asdict(self)
        value["executable"]["path"] = str(self.executable.path)
        return payload_digest(value)


@dataclass(frozen=True)
class InstalledObservation:
    binding: ExecutableBinding | None
    changed: bool
    grade: Grade
    code: str = ""


def _version(path: Path, deadline: float) -> str:
    result = subprocess.run([str(path), "--version"], stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=max(.001, deadline-time.monotonic()), check=False)
    if result.returncode or len(result.stdout) > 4096:
        raise ValueError("version unavailable")
    version = result.stdout.decode().strip()
    if not version:
        raise ValueError("version missing")
    return version


class ExecutableObserver:
    """Service-owned bounded metadata loop; callbacks must only enqueue work.

    No inference happens here. Hash/version reads run only after a target or
    metadata change. Old ExecutableBinding objects remain immutable snapshots.
    """
    def __init__(self, installed_path: Path, *, version_reader: Callable[[Path, float], str] = _version):
        if not installed_path.is_absolute():
            raise ValueError("installed executable path must be absolute")
        self.path, self.version_reader = installed_path, version_reader
        self._metadata: tuple[Any, ...] | None = None
        self._binding: ExecutableBinding | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def _stat(path: Path) -> tuple[Any, ...]:
        info = path.stat()
        return (str(path), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def observe(self, *, force: bool = False) -> InstalledObservation:
        with self._lock:
            try:
                target = self.path.resolve(strict=True)
                metadata = self._stat(target)
                if metadata == self._metadata and not force:
                    return InstalledObservation(self._binding, False, "compatible")
                digest = hashlib.sha256()
                with target.open("rb") as binary:
                    for chunk in iter(lambda: binary.read(1024*1024), b""):
                        digest.update(chunk)
                version = self.version_reader(target, time.monotonic()+3)
                if self.path.resolve(strict=True) != target or self._stat(target) != metadata:
                    return InstalledObservation(None, True, "inconclusive", "EXECUTABLE_CHANGED_DURING_READ")
                binding = ExecutableBinding(target, digest.hexdigest(), version)
                changed = binding != self._binding
                self._metadata, self._binding = metadata, binding
                return InstalledObservation(binding, changed, "compatible")
            except (OSError, ValueError, subprocess.TimeoutExpired):
                return InstalledObservation(None, True, "inconclusive", "INSTALLED_IDENTITY_UNAVAILABLE")

    def start(self, on_change: Callable[[InstalledObservation], None], *, interval: float = 30) -> None:
        if self._thread is not None or interval < .01:
            raise ValueError("one bounded service observer required")

        def loop() -> None:
            pending: InstalledObservation | None = None
            while not self._stop.is_set():
                observation = self.observe()
                if observation.changed:
                    pending = observation
                if pending is not None:
                    try:
                        on_change(pending)
                    except Exception:  # noqa: BLE001 - retry owner publication without losing observed replacement
                        pending = replace(pending, code="OWNER_PUBLICATION_RETRY")
                    else:
                        pending = None
                self._stop.wait(interval)

        self._thread = threading.Thread(target=loop, name="native-installed-identity", daemon=True)
        self._thread.start()

    def close(self, *, timeout: float = 4) -> bool:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=min(max(timeout, 0), 4))
        return self._thread is None or not self._thread.is_alive()


def required_subset(observed: Any, required: Any, *, path: str = "") -> str | None:
    """JSON/schema required subset; extra properties/enums/methods are harmless."""
    if isinstance(required, Mapping):
        if not isinstance(observed, Mapping):
            return path or "$"
        for key, value in required.items():
            location = f"{path}.{key}" if path else str(key)
            if key not in observed:
                return location
            failure = required_subset(observed[key], value, path=location)
            if failure:
                return failure
    elif isinstance(required, list):
        if not isinstance(observed, list):
            return path or "$"
        for value in required:
            if not any(required_subset(item, value, path=path) is None for item in observed):
                return path or "$"
    elif type(observed) is not type(required) or observed != required:
        return path or "$"
    return None


@dataclass(frozen=True)
class CapabilityEvidence:
    capability: str
    grade: Grade
    coverage: frozenset[str] = frozenset()
    provenance: tuple[str, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()
    observed_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if self.capability not in GUI_CAPABILITIES or self.grade not in {"compatible", "incompatible", "inconclusive", "unverified"}:
            raise ValueError("supported capability grade required")
        if (len(self.coverage) > 64 or len(self.provenance) > 64
                or any(not isinstance(value, str) or not 1 <= len(value) <= 512 for value in (*self.coverage, *self.provenance))
                or not isinstance(self.observed_at, (int, float)) or not math.isfinite(self.observed_at)):
            raise ValueError("bounded public evidence required")


class EvidenceCache:
    """Owner may persist exported public evidence using its canonical store."""
    def __init__(self):
        self._lock = threading.Lock()
        self._items: dict[str, dict[str, CapabilityEvidence]] = {}
        self._fingerprints: dict[str, Fingerprint] = {}

    def put(self, fingerprint: Fingerprint, evidence: CapabilityEvidence) -> None:
        if evidence.capability not in GUI_CAPABILITIES or evidence.grade not in {"compatible", "incompatible", "inconclusive", "unverified"}:
            raise ValueError("supported capability evidence required")
        with self._lock:
            self._items.setdefault(fingerprint.key, {})[evidence.capability] = evidence
            self._fingerprints[fingerprint.key] = fingerprint
            while len(self._items) > 128:
                oldest = next(iter(self._items))
                self._items.pop(oldest)
                self._fingerprints.pop(oldest)

    def get(self, fingerprint: Fingerprint, capability: str) -> CapabilityEvidence | None:
        with self._lock:
            evidence = self._items.get(fingerprint.key, {}).get(capability)
        if evidence is None or (evidence.grade == "compatible" and not _covered(fingerprint, evidence, cleanup=True)):
            return None
        if evidence.grade in {"unverified", "inconclusive"} and time.time()-evidence.observed_at > 60:
            return None
        return evidence

    def admission(self, fingerprint: Fingerprint) -> dict[str, Grade]:
        grades: dict[str, Grade] = {}
        for cap in GUI_CAPABILITIES:
            item = self.get(fingerprint, cap)
            grades[cap] = item.grade if item else "unverified"
        work = self.get(fingerprint, CAP_STOP_WORK)
        with self._lock:
            observed_work = self._items.get(fingerprint.key, {}).get(CAP_STOP_WORK)
        for variant in ("stop_work_terminal", "stop_work_child"):
            required = TARGET_COVERAGE.get(fingerprint.harness, {}).get(variant)
            scoped = next((d for d in reversed(observed_work.diagnostics) if d.capability == variant), None) if observed_work else None
            grades[variant] = (scoped.grade if scoped and scoped.grade != "compatible" else
                               "compatible" if required and work and work.grade == "compatible"
                               and required <= work.coverage else "inconclusive")
        return grades

    def export(self, *, sensitive_values: tuple[str, ...] = ()) -> dict[str, Any]:
        with self._lock:
            records = []
            for key, evidence in self._items.items():
                fingerprint = asdict(self._fingerprints[key])
                fingerprint["executable"]["path"] = str(fingerprint["executable"]["path"])
                items = {}
                for cap, item in evidence.items():
                    value = asdict(item)
                    value["coverage"] = sorted(item.coverage)
                    items[cap] = value
                records.append({"key": key, "fingerprint": fingerprint, "evidence": items})
        return public_payload({"revision": CONTRACT_REVISION, "records": records}, sensitive_values=sensitive_values)

    @classmethod
    def restore(cls, payload: Mapping[str, Any]) -> EvidenceCache:
        cache = cls()
        if payload.get("revision") != CONTRACT_REVISION:
            return cache  # An older contract cannot certify this driver.
        records = payload.get("records")
        if not isinstance(records, list) or len(records) > 128:
            raise ValueError("bounded cache records required")
        try:
            for record in records:
                data = dict(record["fingerprint"])
                executable = dict(data.pop("executable"))
                executable["path"] = Path(executable["path"])
                fingerprint = Fingerprint(executable=ExecutableBinding(**executable), **data)
                if record["key"] != fingerprint.key:
                    raise ValueError("cache fingerprint changed")
                for cap, raw in record["evidence"].items():
                    item = dict(raw)
                    item["coverage"] = frozenset(item["coverage"])
                    item["provenance"] = tuple(item["provenance"])
                    item["diagnostics"] = tuple(Diagnostic(**entry) for entry in item["diagnostics"])
                    evidence = CapabilityEvidence(**item)
                    if cap != evidence.capability:
                        raise ValueError("cache capability changed")
                    cache.put(fingerprint, evidence)
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError("invalid public capability cache") from exc
        return cache


class RuntimeValidator:
    """Normal events validate a captured generation, never the installed binary.

    Additional fields pass. Identity/terminal/control contradictions downgrade
    only affected operations; Close/supervision are never gated by a grade.
    """
    def __init__(self, root_id: str, *, harness: str = "codex"):
        self.root, self.harness = root_id, harness
        self._terminal: set[tuple[str, str]] = set()

    def validate(self, event: RuntimeEvent) -> tuple[Diagnostic, ...]:
        if event.kind not in EVENT_KINDS:
            return ()
        ref = event.reference
        if ref and ref.root_id != self.root:
            return tuple(Diagnostic(cap, "incompatible", "FOREIGN_NATIVE_ROOT") for cap in GUI_CAPABILITIES)
        if event.kind in {"runtime.lost", "ownership.failed"}:
            return tuple(Diagnostic(cap, "inconclusive", "NATIVE_EVIDENCE_LOST") for cap in GUI_CAPABILITIES)
        if event.kind.startswith("output.") and ref and (not ref.activity_id or self.harness == "codex" and (not ref.thread_id or not ref.item_id)):
            return (Diagnostic(CAP_SUBMISSION, "inconclusive", "OUTPUT_BINDING_PARTIAL"),)
        if event.kind.startswith("activity.") and (not ref or not ref.activity_id or self.harness == "codex" and not ref.thread_id):
            return tuple(Diagnostic(cap, "incompatible", "ACTIVITY_BINDING_MISSING") for cap in (CAP_SUBMISSION, CAP_STOP_REPLY))
        if event.kind == "control.outcome" and not event.control_id:
            return tuple(Diagnostic(cap, "incompatible", "CONTROL_BINDING_MISSING") for cap in (CAP_STOP_REPLY, CAP_STOP_WORK))
        if ref and ref.activity_id and event.kind.startswith("activity."):
            key = (ref.thread_id or self.root, ref.activity_id)
            if event.kind == "activity.terminal":
                if len(self._terminal) >= 4096 and key not in self._terminal:
                    return (Diagnostic(CAP_SUBMISSION, "inconclusive", "ACTIVITY_EVIDENCE_CAPACITY"),)
                self._terminal.add(key)
            elif key in self._terminal:
                return (Diagnostic(CAP_SUBMISSION, "incompatible", "TERMINAL_ACTIVITY_REOPENED"),)
        if event.kind == "control.outcome" and event.data.get("outcome") == "complete" and (
                event.partial or event.freshness in {"unknown", "stale"}):
            capability = (CAP_STOP_REPLY if ref and ref.activity_id and not ref.work_id and not ref.native_process_id else
                          "stop_work_child" if self.harness == "codex" and ref and ref.work_id and not ref.native_process_id else "stop_work_terminal")
            return (Diagnostic(capability, "incompatible", "UNPROVED_CONTROL_SUCCESS"),)
        return ()

    def frame(self, value: Mapping[str, Any]) -> tuple[Diagnostic, ...]:
        kind = value.get("kind")
        if isinstance(kind, str) and kind not in EVENT_KINDS:
            return ()
        try:
            known = {item.name for item in fields(RuntimeEvent)}
            data = {key: item for key, item in value.items() if key in known}
            ref = data.get("reference")
            if ref is not None:
                names = {item.name for item in fields(NativeReference)}
                reference = {key: item for key, item in ref.items() if key in names}
                if reference.get("os_process") is not None:
                    reference["os_process"] = ProcessIdentity(**reference["os_process"])
                data["reference"] = NativeReference(**reference)
            return self.validate(RuntimeEvent(**data))
        except (RuntimeContractError, TypeError, ValueError, AttributeError):
            affected = ({CAP_SUBMISSION} if isinstance(kind, str) and kind.startswith("output.")
                        else {CAP_STOP_WORK, CAP_AUTOMATION} if isinstance(kind, str) and kind.startswith("work.")
                        else {CAP_SUBMISSION, CAP_STOP_REPLY})
            return tuple(Diagnostic(cap, "incompatible", "REQUIRED_EVENT_SHAPE_CHANGED") for cap in affected)


@dataclass(frozen=True)
class CleanupProof:
    unit_verified_exited: bool
    native_outcome: str
    unresolved_work: tuple[str, ...] = ()
    unresolved_definitions: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return (self.unit_verified_exited is True and self.native_outcome == "complete"
                and not self.unresolved_work and not self.unresolved_definitions)


@dataclass(frozen=True)
class ProbeSession:
    context: RuntimeContext
    driver: RuntimeDriver
    unit: str
    isolation_root: Path
    registered: bool
    # The owner implements finite capability scenarios using this exact driver;
    # evidence must reflect native outcomes, not ack/prose/schema alone.
    exercise: Callable[[RuntimeDriver, float], Mapping[str, CapabilityEvidence]]


class ProbeFactory(Protocol):
    def reserve(self, fingerprint: Fingerprint, capabilities: frozenset[str], *, deadline: float) -> ProbeSession: ...
    def cleanup(self, fingerprint: Fingerprint, *, deadline: float) -> CleanupProof:
        """Fence allocations/writes even if reserve timed out before returning.

        Resolve only the recorded probe allocation; independently stop/inspect
        its owned unit/cgroup. Never target living user runtimes or shared units.
        """
        ...


@dataclass(frozen=True)
class CheckResult:
    fingerprint: Fingerprint
    evidence: Mapping[str, CapabilityEvidence]
    cleanup: CleanupProof | None = None


def _bounded(operation: Callable[[], Any], deadline: float) -> Any:
    if time.monotonic() >= deadline:
        raise TimeoutError("bounded native operation expired before dispatch")
    results: queue.Queue[Any] = queue.Queue(maxsize=1)

    def run() -> None:
        try:
            results.put((True, operation()))
        except Exception as exc:  # noqa: BLE001 - finite owner operation must resolve its future
            # Exception messages may include private prepared environment.
            results.put((False, exc))

    threading.Thread(target=run, name="native-check-operation", daemon=True).start()
    try:
        ok, result = results.get(timeout=max(.001, deadline-time.monotonic()))
    except queue.Empty as exc:
        raise TimeoutError("bounded native check timed out") from exc
    if not ok:
        raise result
    return result


class CompatibilityChecker:
    def __init__(self, *, cache: EvidenceCache | None = None):
        self.cache = cache or EvidenceCache()
        self._lock = threading.Lock()
        self._flights: dict[str, Future[CheckResult]] = {}
        self._slots = {name: threading.BoundedSemaphore(2) for name in ("codex", "claude")}
        self._retained: dict[str, Fingerprint] = {}

    def request(self, fingerprint: Fingerprint, *, observed_interface: Mapping[str, Any],
                requirements: Mapping[str, Any], factory: ProbeFactory,
                active_generations: frozenset[str] = frozenset(), seconds: float = PROBE_SECONDS) -> Future[CheckResult]:
        if fingerprint.harness not in self._slots or not 0 < seconds <= PROBE_SECONDS or not requirements or not set(requirements) <= GUI_CAPABILITIES:
            raise ValueError("bounded supported capability check required")
        with self._lock:
            if fingerprint.key in self._flights:
                return self._flights[fingerprint.key]
            future: Future[CheckResult] = Future()
            self._flights[fingerprint.key] = future

        def run() -> None:
            evidence: dict[str, CapabilityEvidence] = {}
            remaining = set(requirements)
            for cap, shape in requirements.items():
                mismatch = required_subset(observed_interface, shape)
                if mismatch:
                    evidence[cap] = CapabilityEvidence(cap, "incompatible", diagnostics=(Diagnostic(cap, "incompatible", "REQUIRED_INTERFACE_CHANGED", mismatch),))
                else:
                    cached = self.cache.get(fingerprint, cap)
                    if cached:
                        evidence[cap] = cached
            remaining -= evidence.keys()
            cleanup: CleanupProof | None = None
            slot = self._slots[fingerprint.harness]
            acquired = False
            deadline = time.monotonic()+seconds
            work_deadline = deadline-min(CLEANUP_RESERVE, seconds/3)
            diagnostics: dict[tuple[str, str], Diagnostic] = {}
            validator: RuntimeValidator | None = None
            pending: list[RuntimeEvent] = []
            event_lock = threading.Lock()

            def emit(event: RuntimeEvent) -> None:
                with event_lock:
                    if validator is None:
                        if len(pending) >= 128:
                            for cap in remaining:
                                diagnostics[(cap, "PROBE_EVENT_LIMIT")] = Diagnostic(cap, "inconclusive", "PROBE_EVENT_LIMIT")
                            return
                        pending.append(event)
                    else:
                        for diagnostic in validator.validate(event):
                            diagnostics[(diagnostic.capability, diagnostic.code)] = diagnostic

            try:
                if remaining:
                    with self._lock:
                        retained = fingerprint.key in self._retained
                    acquired = not retained and slot.acquire(blocking=False)
                    if not acquired:
                        raise TimeoutError("owned probe capacity unavailable")
                    session = _bounded(lambda: factory.reserve(fingerprint, frozenset(remaining), deadline=work_deadline), work_deadline)
                    context = session.context
                    if (not session.registered or not session.unit or context.generation_id in active_generations
                            or context.worktree.resolve() == session.isolation_root.resolve()
                            or session.isolation_root.resolve() not in context.worktree.resolve().parents
                            or session.isolation_root.resolve() not in context.state_root.resolve().parents
                            or Fingerprint.capture(context, settings_digest=fingerprint.settings_digest, implementation_digest=fingerprint.implementation_digest) != fingerprint
                            or session.driver.revision != fingerprint.driver_revision):
                        raise RuntimeContractError("PROBE_OWNERSHIP_INVALID", "isolated captured probe binding required")
                    started = _bounded(lambda: session.driver.start(context, emit, deadline=work_deadline), work_deadline)
                    if started.state != "ready" or started.identity is None:
                        raise TimeoutError("account/policy/consent readiness unproved")
                    with event_lock:
                        validator = RuntimeValidator(started.identity.root_id, harness=fingerprint.harness)
                        for event in pending:
                            for diagnostic in validator.validate(event):
                                diagnostics[(diagnostic.capability, diagnostic.code)] = diagnostic
                        pending.clear()
                    measured = _bounded(lambda: session.exercise(session.driver, work_deadline), work_deadline)
                    for cap in remaining:
                        item = measured.get(cap)
                        if (item is None or item.capability != cap or item.grade == "compatible" and (
                                not _covered(fingerprint, item, cleanup=False))):
                            item = CapabilityEvidence(cap, "inconclusive", diagnostics=(Diagnostic(cap, "inconclusive", "BEHAVIOR_COVERAGE_MISSING"),))
                        evidence[cap] = item
            except Exception as exc:  # noqa: BLE001 - account/setup failures stay scoped and inconclusive
                code = exc.code if isinstance(exc, RuntimeContractError) else "PROBE_UNAVAILABLE_OR_TIMEOUT"
                for cap in remaining:
                    evidence[cap] = CapabilityEvidence(cap, "inconclusive", diagnostics=(Diagnostic(cap, "inconclusive", code),))
            finally:
                if acquired:
                    try:
                        cleanup = _bounded(lambda: factory.cleanup(fingerprint, deadline=deadline), deadline)
                    except Exception:  # noqa: BLE001 - cleanup proof fails closed without exposing private diagnostics
                        cleanup = CleanupProof(False, "inconclusive")
                    if cleanup.complete:
                        slot.release()
                        for cap in remaining:
                            item = evidence.get(cap)
                            if item and item.grade == "compatible":
                                evidence[cap] = replace(item, coverage=item.coverage | {"owned_unit_cleanup"},
                                                        provenance=(*item.provenance, "owner:unit_cleanup_verified"))
                    else:
                        with self._lock:
                            self._retained[fingerprint.key] = fingerprint
                        for cap in remaining:
                            item = evidence.get(cap)
                            if item is None or item.grade != "incompatible":
                                evidence[cap] = CapabilityEvidence(cap, "inconclusive", diagnostics=(Diagnostic(cap, "inconclusive", "OWNED_CLEANUP_UNPROVED"),))
                with event_lock:
                    final_diagnostics = tuple(diagnostics.values())
                for diagnostic in final_diagnostics:
                    if diagnostic.capability in {"stop_work_child", "stop_work_terminal"} and CAP_STOP_WORK in remaining:
                        item = evidence[CAP_STOP_WORK]
                        evidence[CAP_STOP_WORK] = replace(item, diagnostics=(*item.diagnostics, diagnostic))
                    elif diagnostic.capability in remaining:
                        cap = diagnostic.capability
                        if evidence[cap].grade != "incompatible":
                            evidence[cap] = CapabilityEvidence(cap, diagnostic.grade, diagnostics=(diagnostic,))
                for item in evidence.values():
                    self.cache.put(fingerprint, item)
                future.set_result(CheckResult(fingerprint, evidence, cleanup))
                with self._lock:
                    self._flights.pop(fingerprint.key, None)

        threading.Thread(target=run, name="native-compatibility-check", daemon=True).start()
        return future

    def validate_runtime(self, fingerprint: Fingerprint, validator: RuntimeValidator, event: RuntimeEvent) -> tuple[Diagnostic, ...]:
        diagnostics = validator.validate(event)
        for diagnostic in diagnostics:
            if diagnostic.capability in {"stop_work_child", "stop_work_terminal"}:
                with self.cache._lock:
                    previous = self.cache._items.get(fingerprint.key, {}).get(CAP_STOP_WORK)
                item = previous or CapabilityEvidence(CAP_STOP_WORK, "inconclusive")
                retained = tuple(d for d in item.diagnostics if d.capability != diagnostic.capability)
                self.cache.put(fingerprint, replace(item, diagnostics=(*retained, diagnostic)))
            else:
                self.cache.put(fingerprint, CapabilityEvidence(diagnostic.capability, diagnostic.grade, diagnostics=(diagnostic,)))
        return diagnostics

    def resolve_cleanup(self, fingerprint: Fingerprint, proof: CleanupProof) -> bool:
        if not proof.complete:
            return False
        with self._lock:
            if self._retained.pop(fingerprint.key, None) is None:
                return False
            self._slots[fingerprint.harness].release()
        return True
