#!/usr/bin/env python3
"""Sprints v2 lifecycle authority, arming transaction, and service gate.

This module deliberately owns no GitHub or harness effects.  It commits the
durable facts those services consume, and the ArmedServiceSwitch prevents
registered poll/dispatch callbacks from running outside an armed Sprint.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import active_chat_registry
import conversation_broker
import conversation_events
import db_driver
import sprint_cleanup
import sprint_participant_chats
from conversation_adapters import AdapterError, ProbeResult, adapter_for

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE / "api"))
import model_catalog  # noqa: E402  (canonical local route evidence)
import route_bindings  # noqa: E402  (versioned participant route contract)

SPRINT_TRANSITIONS = {
    "prepared": frozenset({"armed", "aborted"}),
    "armed": frozenset({"paused", "completed", "aborted"}),
    "paused": frozenset({"armed", "aborted"}),
    "completed": frozenset(),
    "aborted": frozenset(),
}

EDITING_UNIT_DISPOSITIONS = frozenset(
    {"active", "in_review", "fixing", "merge_ready"}
)
WORK_UNIT_OUTPUT_KINDS = frozenset({"code", "report_only", "no_code"})
WAKE_CONTENTION_BACKOFF_SECONDS = (15, 60, 180, 300)
WAKE_CONTENTION_ATTEMPTS = 5
WAKE_DELIVERY_BACKOFF_SECONDS = (15, 60, 180)
WAKE_PICKUP_PAUSE_REASONS = frozenset(
    {
        "wake_pickup_unknown",
        "wake_pickup_failed",
        "wake_pickup_unread",
        "wake_pickup_evidence_invalid",
    }
)


def _participant_shell_id(con: sqlite3.Connection, participant_id: int) -> int:
    row = con.execute(
        "SELECT shell_id FROM sprint_participants WHERE participant_id=?",
        (participant_id,),
    ).fetchone()
    if row is None:
        raise SprintInvariantError("Sprint participant does not exist")
    return int(row[0])


class SprintLifecycleError(ValueError):
    """Base class for a rejected Sprint lifecycle operation."""


class SprintStateError(SprintLifecycleError):
    """The requested lifecycle edge is unknown or illegal."""


class SprintAuthorityError(SprintLifecycleError):
    """The actor does not own the requested lifecycle transition."""


class SprintInvariantError(SprintLifecycleError):
    """The durable Sprint plan is not eligible for the requested transition."""


class SprintConflictError(SprintInvariantError):
    """A bounded durable-state conflict prevents the requested transition."""

    def __init__(self, message: str, details: dict) -> None:
        self.details = details
        super().__init__(message)


class SprintPreflightError(SprintInvariantError):
    """The selected participant route is incompatible with this runtime."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict | None = None,
    ) -> None:
        self.code = code
        self.details = details or {}
        super().__init__(message)


class BoundRevisionUnavailable(SprintInvariantError):
    """A legacy binding has no hash-verified immutable governing body."""

    def __init__(self, details: dict[str, object]) -> None:
        self.details = details
        super().__init__("bound governing revision is unavailable for this legacy binding")


class SprintCleanupConflictError(SprintInvariantError):
    """An earlier successful Sprint still owns a participant worktree."""

    def __init__(self, blocker: sprint_cleanup.UnresolvedCleanupTarget) -> None:
        self.details = {
            "code": "prior_cleanup_unresolved",
            "prior_sprint_id": blocker.sprint_id,
            "cleanup_target_id": blocker.cleanup_target_id,
            "target_state": blocker.state,
            "path_label": blocker.path_label,
            "last_safe_fact": blocker.last_safe_fact,
            "status_command": (
                f"sc sprint cleanup-status --sprint {blocker.sprint_id}"
            ),
            "retry_command": (
                f"sc sprint cleanup --sprint {blocker.sprint_id} "
                "--key <stable-retry-key>"
            ),
        }
        super().__init__(
            f"prior Sprint {blocker.sprint_id} cleanup is {blocker.state} for "
            f"{blocker.path_label} (last_safe_fact={blocker.last_safe_fact}); "
            f"run `{self.details['status_command']}` and, after correcting a "
            f"failure, `{self.details['retry_command']}`"
        )


@dataclass(frozen=True)
class LifecycleActor:
    kind: str
    shell_id: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"planner", "fnb", "participant", "system"}:
            raise ValueError(f"unknown Sprint actor kind: {self.kind}")
        if self.kind in {"planner", "participant"} and self.shell_id is None:
            raise ValueError(f"{self.kind} actor requires shell_id")


@dataclass(frozen=True)
class PauseReceipt:
    changed: bool
    report_id: int | None
    interrupt_run_ids: tuple[int, ...]
    notification_conversation_ids: tuple[str, ...]


@dataclass(frozen=True)
class ResumeReceipt:
    changed: bool
    dispatched_wake_ids: tuple[int, ...]
    requeued_wake_ids: tuple[int, ...]
    projected_work_unit_ids: tuple[int, ...]
    resolved_review_message_ids: tuple[int, ...]
    spec_drift_document_ids: tuple[int, ...]
    anomalies: tuple[str, ...]


@dataclass(frozen=True)
class _WakeReconcileResult:
    wake_ids: tuple[int, ...]
    pause_receipt: PauseReceipt | None = None


@dataclass(frozen=True)
class AbortReceipt:
    changed: bool
    report_id: int | None
    interrupt_run_ids: tuple[int, ...]
    notification_conversation_ids: tuple[str, ...]


@dataclass(frozen=True)
class SpecApprovalReceipt:
    approval_id: int
    revision_sha256: str
    verdict: str
    created: bool


@dataclass(frozen=True)
class WorkUnitCompletionReceipt:
    sprint_id: int
    work_unit_id: int
    disposition: str
    completed_at: str
    output_kind: str
    stored_result_length: int
    stored_result_sha256: str
    changed: bool
    idempotent: bool
    created_wake_ids: tuple[int, ...]
    reused_wake_ids: tuple[int, ...]

    @property
    def wake_ids(self) -> tuple[int, ...]:
        return tuple(dict.fromkeys((*self.created_wake_ids, *self.reused_wake_ids)))


@dataclass(frozen=True)
class ParticipantBindingCandidate:
    participant_id: int
    binding: dict
    binding_digest: str
    evidence_snapshot: dict | None
    runtime_status: dict | None
    runtime_scope: dict | None
    source_fingerprint: str | None
    harness_version: str | None
    harness_support_state: str | None


@dataclass(frozen=True)
class ArmBindingPreflight:
    intent_fingerprint: str
    candidates: tuple[ParticipantBindingCandidate, ...]


@dataclass(frozen=True)
class ParticipantRerouteReceipt:
    changed: bool
    binding_status: str
    control_state: str
    route_revision: int | None
    binding_digest: str | None
    harness_version: str | None
    harness_support_state: str | None

    def __bool__(self) -> bool:
        return self.changed


_ROUTE_EVIDENCE_FIELDS = (
    "harness",
    "selector",
    "generation_id",
    "evidence_kind",
    "evidence_digest",
    "source_fingerprint",
    "harness_version",
    "harness_compatibility",
    "harness_support_state",
    "supported_efforts",
    "selector_binding",
    "effort_metadata",
    "adapter_metadata",
    "availability",
    "headless_supported",
    "stale",
    "last_seen_at",
)


def _next_assignment_key(
    con: sqlite3.Connection, sprint_id: int, work_unit_id: int
) -> str:
    """Next durable generation key for one lane's work-assignment message."""
    key_prefix = f"sprint:{sprint_id}:work-unit:{work_unit_id}:assignment:"
    generation = 0
    for row in con.execute(
        "SELECT idempotency_key FROM wake_message "
        "WHERE work_unit_id=? AND message_kind='work_assignment'",
        (work_unit_id,),
    ):
        key = str(row["idempotency_key"])
        if not key.startswith(key_prefix):
            continue
        suffix = key.removeprefix(key_prefix)
        if suffix.isdigit():
            generation = max(generation, int(suffix))
    return f"{key_prefix}{generation + 1}"


def _route_evidence_snapshot(row: dict | sqlite3.Row) -> dict:
    return {field: row[field] for field in _ROUTE_EVIDENCE_FIELDS}


def _route_binding_schema_available(con: sqlite3.Connection) -> bool:
    if con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='sprint_participant_route_bindings'"
    ).fetchone() is None:
        return False
    return "active_route_binding_id" in {
        str(row["name"]) for row in con.execute("PRAGMA table_info(sprint_participants)")
    }


def _participant_binding_candidate(
    con: sqlite3.Connection,
    participant: sqlite3.Row,
) -> ParticipantBindingCandidate:
    """Resolve one raw participant intent without persisting Sprint state."""
    harness = route_bindings.normalize_harness(str(participant["harness"]))
    model = participant["model"]
    effort = participant["effort"]
    participant_id = int(participant["participant_id"])
    runtime_status = None
    runtime_scope = None
    evidence_snapshot = None
    source_fingerprint = None
    harness_support_state = None
    if model is None or harness == "vibe":
        runtime_scope = model_catalog.harness_versions.runtime_scope()
        runtime_status = model_catalog.harness_runtime_status(harness)
        binding, binding_digest = route_bindings.resolve_v2(
            None,
            harness,
            model,
            effort,
            runtime_status=runtime_status,
            runtime_scope=runtime_scope,
        )
        harness_version = runtime_status.get(
            "observed_version", runtime_status.get("version")
        )
        harness_support_state = model_catalog._support_state(runtime_status)
    else:
        evidence = con.execute(
            "SELECT * FROM model_routes WHERE harness=? AND selector=?",
            (harness, model),
        ).fetchone()
        evidence_dict = dict(evidence) if evidence is not None else None
        binding, binding_digest = route_bindings.resolve_persisted_v2(
            con,
            evidence_dict,
            harness,
            model,
            effort,
        )
        if evidence_dict is not None:
            evidence_snapshot = _route_evidence_snapshot(evidence_dict)
            source_fingerprint = evidence_dict.get("source_fingerprint")
            harness_version = evidence_dict.get("harness_version")
            harness_support_state = evidence_dict.get("harness_support_state")
        else:
            harness_version = None
    return ParticipantBindingCandidate(
        participant_id=participant_id,
        binding=binding,
        binding_digest=binding_digest,
        evidence_snapshot=evidence_snapshot,
        runtime_status=runtime_status,
        runtime_scope=runtime_scope,
        source_fingerprint=source_fingerprint,
        harness_version=harness_version,
        harness_support_state=harness_support_state,
    )


def transition_allowed(current: str, target: str) -> bool:
    return (
        current in SPRINT_TRANSITIONS
        and target in SPRINT_TRANSITIONS
        and (target == current or target in SPRINT_TRANSITIONS[current])
    )


class SprintSpecApprovalStore:
    """Append exact-revision QAQC approvals signed by Review shells."""

    def __init__(self, con: sqlite3.Connection) -> None:
        self.con = con
        self.con.row_factory = sqlite3.Row

    def record(
        self,
        document_id: int,
        reviewer_shell_id: int,
        *,
        verdict: str,
        findings_document_id: int | None = None,
    ) -> SpecApprovalReceipt:
        if verdict not in {"pass", "fail"}:
            raise ValueError("QAQC verdict must be pass or fail")
        with db_driver.write_transaction(self.con, "sprint.spec_approval.record"):
            reviewer = self.con.execute(
                "SELECT flavor FROM shells WHERE shell_id=? "
                "AND COALESCE(is_deleted,0)=0",
                (reviewer_shell_id,),
            ).fetchone()
            if reviewer is None or reviewer["flavor"] != "reviewer":
                raise SprintAuthorityError(
                    "only an active Review shell may record Sprint QAQC"
                )
            document = self.con.execute(
                "SELECT feature_id,kind,body FROM documents WHERE document_id=?",
                (document_id,),
            ).fetchone()
            if document is None:
                raise KeyError(f"unknown document: {document_id}")
            if document["kind"] != "spec" or document["body"] is None:
                raise SprintInvariantError("Sprint QAQC requires a spec document body")
            if findings_document_id is not None:
                findings = self.con.execute(
                    "SELECT feature_id FROM documents WHERE document_id=?",
                    (findings_document_id,),
                ).fetchone()
                if findings is None:
                    raise KeyError(f"unknown findings document: {findings_document_id}")
                if findings["feature_id"] != document["feature_id"]:
                    raise SprintInvariantError(
                        "QAQC findings must belong to the reviewed spec feature"
                    )
            revision = hashlib.sha256(document["body"].encode()).hexdigest()
            existing = self.con.execute(
                "SELECT approval_id,verdict,findings_document_id "
                "FROM sprint_spec_approvals WHERE document_id=? "
                "AND revision_sha256=? AND reviewer_shell_id=?",
                (document_id, revision, reviewer_shell_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["verdict"] != verdict
                    or existing["findings_document_id"] != findings_document_id
                ):
                    raise SprintInvariantError(
                        "QAQC approval already exists with different evidence"
                    )
                return SpecApprovalReceipt(
                    int(existing["approval_id"]), revision, verdict, False
                )
            approval_id = int(
                self.con.execute(
                    "INSERT INTO sprint_spec_approvals "
                    "(document_id,revision_sha256,reviewer_shell_id,verdict,"
                    "findings_document_id) VALUES (?,?,?,?,?)",
                    (
                        document_id,
                        revision,
                        reviewer_shell_id,
                        verdict,
                        findings_document_id,
                    ),
                ).lastrowid
            )
        return SpecApprovalReceipt(approval_id, revision, verdict, True)

    def for_document(self, document_id: int) -> list[dict]:
        return [
            dict(row)
            for row in self.con.execute(
                "SELECT a.approval_id,a.document_id,a.revision_sha256,"
                "a.reviewer_shell_id,s.shortname AS reviewer_shortname,"
                "a.verdict,a.findings_document_id,a.reviewed_at "
                "FROM sprint_spec_approvals a JOIN shells s "
                "ON s.shell_id=a.reviewer_shell_id WHERE a.document_id=? "
                "ORDER BY a.approval_id",
                (document_id,),
            )
        ]


class SprintSpecRevisionStore:
    """Bound governing bodies exposed only to their Sprint's participants."""

    def __init__(self, con: sqlite3.Connection) -> None:
        self.con = con
        self.con.row_factory = sqlite3.Row

    def read(
        self,
        sprint_id: int,
        document_id: int,
        *,
        caller_shell_id: int | None = None,
        fnb: bool = False,
    ) -> dict[str, object]:
        sprint = self.con.execute(
            "SELECT 1 FROM sprints WHERE sprint_id=?", (sprint_id,)
        ).fetchone()
        if sprint is None:
            raise KeyError(f"unknown Sprint: {sprint_id}")
        if not fnb:
            participant = self.con.execute(
                "SELECT 1 FROM sprint_participants WHERE sprint_id=? AND shell_id=?",
                (sprint_id, caller_shell_id),
            ).fetchone()
            if participant is None:
                raise SprintAuthorityError(
                    "only a Sprint participant or the FnB may read its governing revision"
                )
        row = self.con.execute(
            "SELECT ss.bound_revision_sha256,ss.bound_revision_body,d.title,d.body "
            "FROM sprint_specs ss JOIN documents d USING (document_id) "
            "WHERE ss.sprint_id=? AND ss.document_id=?",
            (sprint_id, document_id),
        ).fetchone()
        if row is None:
            raise KeyError(
                f"document {document_id} is not bound to Sprint {sprint_id}"
            )
        current_body = row["body"] or ""
        current_sha256 = hashlib.sha256(current_body.encode()).hexdigest()
        bound_body = row["bound_revision_body"]
        if bound_body is None:
            raise BoundRevisionUnavailable(
                {
                    "code": "bound_revision_unavailable",
                    "sprint_id": sprint_id,
                    "document_id": document_id,
                    "bound_revision_sha256": str(row["bound_revision_sha256"]),
                    "current_revision_sha256": current_sha256,
                    "availability": "unavailable_legacy_drift",
                }
            )
        actual = hashlib.sha256(bound_body.encode()).hexdigest()
        if actual != row["bound_revision_sha256"]:
            raise SprintInvariantError(
                "stored governing body does not match its immutable revision hash"
            )
        return {
            "sprint_id": sprint_id,
            "document_id": document_id,
            "title": str(row["title"]),
            "bound_revision_sha256": str(row["bound_revision_sha256"]),
            "availability": "available",
            "body": str(bound_body),
        }


class SprintLifecycleStore:
    """Transactional lifecycle operations over one engine DB connection."""

    def __init__(
        self,
        con: sqlite3.Connection,
        *,
        interrupt_run: Callable[[int], bool] | None = None,
        notify_commit: Callable[[], bool] | None = None,
        probe_harness: Callable[[str], ProbeResult] | None = None,
        cleanup_store: sprint_cleanup.SprintCleanupTargetStore | None = None,
    ) -> None:
        self.con = con
        self.con.row_factory = sqlite3.Row
        self.interrupt_run = interrupt_run or conversation_broker.interrupt_run
        self.notify_commit = notify_commit or conversation_broker.notify_commit
        self.probe_harness = probe_harness or (
            lambda harness: adapter_for(harness).probe()
        )
        self.cleanup_store = cleanup_store or sprint_cleanup.SprintCleanupTargetStore(
            con
        )

    def armed_sprint_id(self) -> int | None:
        row = self.con.execute(
            "SELECT sprint_id FROM sprints WHERE lifecycle='armed'"
        ).fetchone()
        return int(row[0]) if row is not None else None

    def arm(
        self,
        sprint_id: int,
        planner_shell_id: int,
        *,
        conformance_reviewer_shell_id: int | None = None,
    ) -> list[int]:
        """Arm an eligible prepared Sprint and release initial work atomically.

        The returned IDs name the committed wake intents.  A uniqueness failure
        or any invalid plan rolls back the lifecycle update, messages, wakes,
        and work-unit dispositions together.
        """
        actor = LifecycleActor("planner", planner_shell_id)
        preflight = self._preflight_arm(
            sprint_id,
            actor,
            conformance_reviewer_shell_id=conformance_reviewer_shell_id,
        )
        with db_driver.write_transaction(self.con, "sprint.arm"):
            sprint = self._sprint(sprint_id)
            if sprint["lifecycle"] != "prepared":
                raise SprintStateError(
                    f"arm() requires prepared; found {sprint['lifecycle']}"
                )
            self._require_edge(sprint["lifecycle"], "armed")
            self._authorize(sprint, "armed", actor)
            planner_participant, selections = self._validate_arm_plan(sprint)
            selected_owner = self._resolve_conformance_owner(
                sprint_id,
                conformance_reviewer_shell_id,
                allow_single_reviewer_default=True,
            )
            self._require_prior_cleanup_resolved(sprint_id)
            current_fingerprint = self._participant_intent_fingerprint(sprint_id)
            if current_fingerprint != preflight.intent_fingerprint:
                raise SprintInvariantError(
                    "participant route intents changed during binding preflight; "
                    "retry arm"
                )
            self._require_candidate_evidence(preflight.candidates)
            binding_receipts = self._bind_candidates(
                preflight.candidates,
                transition="arm",
                actor=actor,
                sprint_id=sprint_id,
            )
            self._set_conformance_owner(
                sprint,
                selected_owner,
                actor,
                reason="selected while arming",
            )
            self._update_lifecycle(
                sprint_id,
                current="prepared",
                target="armed",
                outcome=None,
            )
            planner_wake_id = self._queue_arming_planner_wake(
                sprint_id,
                planner_participant_id=planner_participant,
                selections=selections,
            )
            work_wake_ids = self._release_initial_work(
                sprint_id,
                planner_participant_id=planner_participant,
            )
            wake_ids = [*work_wake_ids, planner_wake_id]
            self._event(
                sprint_id,
                "lifecycle.armed",
                actor,
                {
                    "initial_wake_ids": wake_ids,
                    "planner_wake_id": planner_wake_id,
                    "work_wake_ids": work_wake_ids,
                    "participant_bindings": binding_receipts,
                },
            )
            SprintWorkUnitStore(self.con)._queue_delivery_terminal(sprint_id)
        return wake_ids

    def _preflight_arm(
        self,
        sprint_id: int,
        actor: LifecycleActor,
        *,
        conformance_reviewer_shell_id: int | None,
    ) -> ArmBindingPreflight:
        """Probe one immutable route snapshot before arm takes a write lock."""
        if self.con.in_transaction:
            raise SprintInvariantError(
                "arm harness preflight requires a connection outside a transaction"
            )
        sprint = self._sprint(sprint_id)
        if sprint["lifecycle"] != "prepared":
            raise SprintStateError(
                f"arm() requires prepared; found {sprint['lifecycle']}"
            )
        self._require_edge(sprint["lifecycle"], "armed")
        self._authorize(sprint, "armed", actor)
        self._validate_arm_plan(sprint)
        self._resolve_conformance_owner(
            sprint_id,
            conformance_reviewer_shell_id,
            allow_single_reviewer_default=True,
        )
        self._require_prior_cleanup_resolved(sprint_id)
        participants = self._participant_intent_rows(sprint_id)
        if not _route_binding_schema_available(self.con):
            try:
                routes = sprint_participant_chats.prepare_sprint_participant_routes(
                    self.con, sprint_id
                )
            except sprint_participant_chats.SprintConversationError as exc:
                raise SprintPreflightError(str(exc)) from exc
            for harness in dict.fromkeys(route.harness for route in routes):
                try:
                    self.probe_harness(harness)
                except AdapterError as exc:
                    raise SprintPreflightError(str(exc)) from exc
            return ArmBindingPreflight(
                intent_fingerprint=self._intent_fingerprint(participants),
                candidates=(),
            )
        candidates = []
        for participant in participants:
            try:
                candidates.append(
                    _participant_binding_candidate(self.con, participant)
                )
            except route_bindings.RouteResolutionError as exc:
                raise SprintPreflightError(
                    exc.message,
                    code=exc.code,
                    details=exc.details,
                ) from exc
        controlled_generations = {
            candidate.binding["catalogue_generation"]
            for candidate in candidates
            if candidate.binding["control_state"] == "controlled"
        }
        if len(controlled_generations) > 1:
            raise SprintPreflightError(
                "controlled participant routes do not share one catalogue generation",
                code="thinking_evidence_stale",
                details={"catalogue_generations": sorted(controlled_generations)},
            )
        for harness in dict.fromkeys(
            candidate.binding["harness"] for candidate in candidates
        ):
            self._probe_bound_harness(harness)
        return ArmBindingPreflight(
            intent_fingerprint=self._intent_fingerprint(participants),
            candidates=tuple(candidates),
        )

    def _probe_bound_harness(self, harness: str) -> None:
        """Keep executable probing, but do not turn static ranges into admission."""
        if sprint_participant_chats.run_mod.load_adapter(harness).get("conversation"):
            try:
                sprint_participant_chats.require_sprint_harness(harness)
            except sprint_participant_chats.SprintConversationError as exc:
                raise SprintPreflightError(str(exc)) from exc
        try:
            self.probe_harness(harness)
        except AdapterError as exc:
            if exc.code != "HARNESS_VERSION_UNSUPPORTED":
                raise SprintPreflightError(str(exc)) from exc

    def _require_prior_cleanup_resolved(self, sprint_id: int) -> None:
        shell_ids = [
            int(row["shell_id"])
            for row in self.con.execute(
                "SELECT participant.shell_id FROM sprint_participants participant "
                "JOIN shells shell ON shell.shell_id=participant.shell_id "
                "WHERE participant.sprint_id=? "
                "AND COALESCE(shell.flavor,'')<>'admin' "
                "ORDER BY participant.shell_id",
                (sprint_id,),
            )
        ]
        blocker = self.cleanup_store.unresolved_worktree(
            shell_ids,
        )
        if blocker is not None:
            raise SprintCleanupConflictError(blocker)

    def _participant_intent_rows(self, sprint_id: int) -> list[sqlite3.Row]:
        return self.con.execute(
            "SELECT p.participant_id,p.shell_id,p.role,p.harness,p.model,p.effort,"
            "p.route,sh.shortname FROM sprint_participants p "
            "JOIN shells sh ON sh.shell_id=p.shell_id WHERE p.sprint_id=? "
            "ORDER BY CASE p.role WHEN 'planner' THEN 0 "
            "WHEN 'developer' THEN 1 ELSE 2 END,p.participant_id",
            (sprint_id,),
        ).fetchall()

    def _participant_intent_fingerprint(self, sprint_id: int) -> str:
        return self._intent_fingerprint(self._participant_intent_rows(sprint_id))

    @staticmethod
    def _intent_fingerprint(participants: Iterable[sqlite3.Row]) -> str:
        snapshot = [
            {
                "participant_id": int(participant["participant_id"]),
                "shell_id": int(participant["shell_id"]),
                "role": participant["role"],
                "shortname": participant["shortname"],
                "harness": participant["harness"],
                "model": participant["model"],
                "effort": participant["effort"],
                "route": participant["route"],
            }
            for participant in participants
        ]
        payload = json.dumps(snapshot, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def _require_candidate_evidence(
        self,
        candidates: Iterable[ParticipantBindingCandidate],
    ) -> None:
        for candidate in candidates:
            expected = candidate.evidence_snapshot
            if expected is None:
                continue
            current = self.con.execute(
                "SELECT * FROM model_routes WHERE harness=? AND selector=?",
                (candidate.binding["harness"], candidate.binding["requested_model"]),
            ).fetchone()
            if current is None or _route_evidence_snapshot(current) != expected:
                raise SprintInvariantError(
                    "participant route evidence changed during binding preflight; "
                    "retry arm"
                )

    def _bind_candidates(
        self,
        candidates: Iterable[ParticipantBindingCandidate],
        *,
        transition: str,
        actor: LifecycleActor,
        sprint_id: int,
    ) -> list[dict]:
        store = route_bindings.ParticipantRouteBindingStore(self.con)
        receipts = []
        for candidate in candidates:
            receipt = store.bind(
                candidate.participant_id,
                candidate.binding,
                candidate.binding_digest,
                transition=transition,
                runtime_status=candidate.runtime_status,
                runtime_scope=candidate.runtime_scope,
                source_fingerprint=candidate.source_fingerprint,
                harness_version=candidate.harness_version,
                harness_support_state=candidate.harness_support_state,
            )
            receipt.update(
                {
                    "control_state": candidate.binding["control_state"],
                    "effective_effort": candidate.binding["effective_effort"],
                }
            )
            receipts.append(receipt)
            if transition == "arm":
                self._event(
                    sprint_id, "participant.route_bound", actor, receipt
                )
            else:
                self._event(
                    sprint_id, "participant.route_revised", actor, receipt
                )
        return receipts

    def transition(
        self,
        sprint_id: int,
        target: str,
        actor: LifecycleActor,
        *,
        reason: str | None = None,
        terminal_outcome: str | None = None,
    ) -> bool:
        """Apply one authorized edge; same-state requests are idempotent."""
        if target == "paused":
            return self.pause(sprint_id, actor, reason=reason or "unspecified").changed
        if target == "armed":
            return self.resume(sprint_id, actor, reason=reason).changed
        if target == "aborted":
            return self.abort(
                sprint_id,
                actor,
                reason=reason or terminal_outcome or "aborted",
                terminal_outcome=terminal_outcome,
            ).changed
        cleanup_targets: tuple[sprint_cleanup.CleanupTargetDraft, ...] = ()
        if target == "completed":
            current_sprint = self._sprint(sprint_id)
            if current_sprint["lifecycle"] == "completed":
                return False
            cleanup_targets = self.cleanup_store.prepare_targets(sprint_id)
        closed_conversation_ids: tuple[str, ...] = ()
        with db_driver.write_transaction(self.con, "sprint.transition"):
            sprint = self._sprint(sprint_id)
            current = str(sprint["lifecycle"])
            if target == current:
                return False
            if current == "prepared" and target == "armed":
                raise SprintInvariantError(
                    "prepared Sprints must use arm() so plan release is atomic"
                )
            self._require_edge(current, target)
            self._authorize(sprint, target, actor)
            if target in {"completed", "aborted"} and not terminal_outcome:
                raise SprintInvariantError(
                    f"{target} transition requires terminal_outcome"
                )
            if target == "completed":
                assert terminal_outcome is not None
                closed_conversation_ids = self.complete_in_transaction(
                    sprint_id,
                    actor,
                    reason=reason or "Sprint completed",
                    terminal_outcome=terminal_outcome,
                    cleanup_targets=cleanup_targets,
                )
            else:
                self._update_lifecycle(
                    sprint_id,
                    current=current,
                    target=target,
                    outcome=terminal_outcome,
                )
                self._event(
                    sprint_id,
                    f"lifecycle.{target}",
                    actor,
                    {"from": current, "reason": reason},
                )
        notification_error: Exception | None = None
        for conversation_id in closed_conversation_ids:
            try:
                conversation_events.notify(conversation_id)
            except Exception as exc:  # noqa: BLE001 - finish post-commit fanout
                if notification_error is None:
                    notification_error = exc
        if notification_error is not None:
            raise notification_error
        return True

    def complete_in_transaction(
        self,
        sprint_id: int,
        actor: LifecycleActor,
        *,
        reason: str,
        terminal_outcome: str,
        cleanup_targets: tuple[sprint_cleanup.CleanupTargetDraft, ...],
    ) -> tuple[str, ...]:
        """Complete one terminal Sprint inside the caller's write transaction."""
        if not self.con.in_transaction:
            raise RuntimeError("Sprint completion requires an active transaction")
        reason = self._required_text(reason, "completion reason", 2000)
        terminal_outcome = self._required_text(
            terminal_outcome, "terminal outcome", 2000
        )
        sprint = self._sprint(sprint_id)
        current = str(sprint["lifecycle"])
        self._require_edge(current, "completed")
        self._authorize(sprint, "completed", actor)
        self._assert_terminal_work_in_transaction(sprint_id)
        self._update_lifecycle(
            sprint_id,
            current=current,
            target="completed",
            outcome=terminal_outcome,
        )
        closed = self._close_completed_participant_chats_in_transaction(sprint_id)
        self.cleanup_store.schedule_in_transaction(sprint_id, cleanup_targets)
        self._clear_coordinate_mode(
            sprint_id,
            actor,
            reason="Sprint closed by FnB",
        )
        self._event(
            sprint_id,
            "lifecycle.completed",
            actor,
            {
                "from": current,
                "reason": reason,
                "closed_conversation_ids": list(closed),
            },
        )
        return closed

    def complete_from_conformance_in_transaction(
        self,
        sprint_id: int,
        reviewer_shell_id: int,
        *,
        reason: str,
        terminal_outcome: str,
        idempotency_key: str,
        cleanup_targets: tuple[sprint_cleanup.CleanupTargetDraft, ...],
    ) -> tuple[str, ...]:
        """Project Reviewer conformance approval as an atomic terminal edge."""
        if not self.con.in_transaction:
            raise RuntimeError("conformance completion requires an active transaction")
        reason = self._required_text(reason, "completion reason", 2000)
        terminal_outcome = self._required_text(
            terminal_outcome, "terminal outcome", 2000
        )
        idempotency_key = self._required_text(
            idempotency_key, "conformance idempotency key", 220
        )
        sprint = self._sprint(sprint_id)
        current = str(sprint["lifecycle"])
        self._require_edge(current, "completed")
        if sprint["conformance_reviewer_shell_id"] is None or int(
            sprint["conformance_reviewer_shell_id"]
        ) != reviewer_shell_id:
            raise SprintAuthorityError(
                "only the selected conformance Reviewer may complete through conformance"
            )
        self._assert_terminal_work_in_transaction(sprint_id)
        self._update_lifecycle(
            sprint_id,
            current=current,
            target="completed",
            outcome=terminal_outcome,
        )
        closed_conversation_ids = (
            self._close_completed_participant_chats_in_transaction(
                sprint_id,
                retained_reviewer_shell_id=reviewer_shell_id,
            )
        )
        self.cleanup_store.schedule_in_transaction(sprint_id, cleanup_targets)
        self._event(
            sprint_id,
            "lifecycle.completed",
            LifecycleActor("participant", reviewer_shell_id),
            {
                "from": current,
                "reason": reason,
                "via": "conformance",
                "idempotency_key": idempotency_key,
                "closed_conversation_ids": list(closed_conversation_ids),
            },
        )
        return closed_conversation_ids

    def pause(
        self,
        sprint_id: int,
        actor: LifecycleActor,
        *,
        reason: str,
        detail: dict | None = None,
    ) -> PauseReceipt:
        """Atomically pause, persist intent/report/reason, and notify Planner."""
        reason = self._required_text(reason, "pause reason", 2000)
        with db_driver.write_transaction(self.con, "sprint.pause"):
            sprint = self._sprint(sprint_id)
            current = str(sprint["lifecycle"])
            if current == "paused":
                cleared = self._clear_coordinate_mode(
                    sprint_id,
                    actor,
                    reason="Sprint pause confirmed by FnB",
                )
                return PauseReceipt(cleared, None, (), ())
            self._require_edge(current, "paused")
            self._authorize(sprint, "paused", actor)
            receipt = self._pause_in_transaction(
                sprint,
                actor,
                reason=reason,
                detail=detail or {},
            )
            self._clear_coordinate_mode(
                sprint_id,
                actor,
                reason="Sprint paused by FnB",
            )
        self._signal_interrupts_and_notifications(receipt)
        return receipt

    def resume(
        self,
        sprint_id: int,
        actor: LifecycleActor,
        *,
        reason: str | None = None,
        conformance_reviewer_shell_id: int | None = None,
        reconcile_in_transaction: Callable[[sqlite3.Connection], None] | None = None,
        external_anomalies: Iterable[str] = (),
    ) -> ResumeReceipt:
        """Reconcile durable and supplied evidence before publishing re-arming."""
        anomalies = tuple(
            self._required_text(item, "reconciliation anomaly", 2000)
            for item in external_anomalies
        )
        all_anomalies = anomalies
        notice_conversations: tuple[str, ...] = ()
        pause_receipt: PauseReceipt | None = None
        with db_driver.write_transaction(self.con, "sprint.resume"):
            sprint = self._sprint(sprint_id)
            current = str(sprint["lifecycle"])
            if current == "armed":
                if conformance_reviewer_shell_id is not None and (
                    sprint["conformance_reviewer_shell_id"] is None
                    or int(sprint["conformance_reviewer_shell_id"])
                    != conformance_reviewer_shell_id
                ):
                    raise SprintConflictError(
                        "conformance ownership changes require a paused Sprint",
                        {"code": "conformance_owner_change_requires_pause"},
                    )
                return ResumeReceipt(False, (), (), (), (), (), anomalies)
            if current == "prepared":
                raise SprintInvariantError(
                    "prepared Sprints must use arm() so plan release is atomic"
                )
            self._require_edge(current, "armed")
            self._authorize(sprint, "armed", actor)
            self._require_prior_cleanup_resolved(sprint_id)
            selected_owner = self._resolve_resume_conformance_owner(
                sprint,
                conformance_reviewer_shell_id,
                reason=reason,
            )
            self._set_conformance_owner(
                sprint,
                selected_owner,
                actor,
                reason=reason or "preserved while resuming",
            )

            # Armed is not externally visible until this transaction commits.
            # That permits active reconciliation notifications to be created
            # before dispatch without exposing a half-reconciled Sprint.
            self._update_lifecycle(
                sprint_id,
                current=current,
                target="armed",
                outcome=None,
            )
            reset_wake_ids = self._reset_pickup_episodes_in_transaction(
                sprint_id,
                actor,
            )
            if reconcile_in_transaction is not None:
                reconcile_in_transaction(self.con)
            reconciliation = self._reconcile_unread_wakes_in_transaction(
                sprint_id,
                trigger="resume",
            )
            requeued = tuple(
                sorted(set(reset_wake_ids) | set(reconciliation.wake_ids))
            )
            pause_receipt = reconciliation.pause_receipt
            merge_pause_receipts: list[PauseReceipt] = []
            projected, resolved = (
                self._reconcile_registered_prs_in_transaction(
                    sprint_id,
                    pause_receipts=merge_pause_receipts,
                )
                if pause_receipt is None
                else ((), ())
            )
            if pause_receipt is None and merge_pause_receipts:
                pause_receipt = merge_pause_receipts[0]
            drift = self._spec_drift(sprint_id)
            all_anomalies = tuple(
                dict.fromkeys((*anomalies, *self._local_reconciliation_anomalies(sprint_id)))
            )
            evidence = self._reconciliation_evidence(
                sprint_id,
                trigger="resume",
                requeued_wake_ids=requeued,
                projected_work_unit_ids=projected,
                resolved_review_message_ids=resolved,
                spec_drift=drift,
                anomalies=all_anomalies,
            )
            self._event(
                sprint_id,
                "lifecycle.reconciled",
                actor,
                evidence,
            )
            if pause_receipt is None and (drift or all_anomalies or requeued):
                _, notice_conversations = self._queue_planner_notice(
                    sprint_id,
                    body=(
                        f"Sprint {sprint_id} resumed with reconciliation evidence: "
                        f"{len(requeued)} unread wake(s) repaired, "
                        f"{len(drift)} spec drift item(s), "
                        f"{len(all_anomalies)} anomaly item(s)."
                    ),
                    idempotency_key=(
                        f"sprint-resume:{sprint_id}:v{int(sprint['version']) + 1}"
                    ),
                )
            dispatched: tuple[int, ...] = ()
            if pause_receipt is None:
                units = SprintWorkUnitStore(self.con, lifecycle_store=self)
                pause_receipt = units._queue_delivery_terminal(sprint_id)
                if pause_receipt is None:
                    planner = self._planner_participant_id(sprint_id)
                    dispatched = tuple(
                        units._dispatch_ready_locked(
                            sprint_id,
                            planner_participant_id=planner,
                        )
                    )
                    self._event(
                        sprint_id,
                        "lifecycle.armed",
                        actor,
                        {
                            "from": current,
                            "reason": reason,
                            "reconciled": True,
                            "dispatched_wake_ids": list(dispatched),
                        },
                    )
        if pause_receipt is not None:
            self._signal_interrupts_and_notifications(pause_receipt)
        else:
            self._signal_notifications(notice_conversations)
        return ResumeReceipt(
            pause_receipt is None,
            dispatched,
            requeued,
            tuple(projected),
            tuple(resolved),
            tuple(sorted(drift)),
            all_anomalies,
        )

    def _reset_pickup_episodes_in_transaction(
        self,
        sprint_id: int,
        actor: LifecycleActor,
    ) -> tuple[int, ...]:
        """Make an authorized resume the anchor of fresh bounded pickup work."""
        report = self.con.execute(
            "SELECT body FROM sprint_reports WHERE sprint_id=? "
            "AND report_kind='pause' ORDER BY report_id DESC LIMIT 1",
            (sprint_id,),
        ).fetchone()
        if report is None:
            return ()
        pause = json.loads(str(report["body"]))
        pause_reason = pause.get("reason")
        wake_ids: tuple[int, ...]
        if pause_reason == "wake_contention_exhausted":
            wake_ids = (int(pause["detail"]["wake_id"]),)
            classification = "contention_episode_reset"
            episode_name = "contention-episode"
        elif pause_reason in WAKE_PICKUP_PAUSE_REASONS:
            wake_ids = tuple(
                int(row[0])
                for row in self.con.execute(
                    "SELECT DISTINCT "
                    "CAST(json_extract(e.payload,'$.wake_id') AS INTEGER) wake_id "
                    "FROM sprint_events e JOIN sprint_wake_outbox w "
                    "ON w.wake_id=json_extract(e.payload,'$.wake_id') "
                    "WHERE e.sprint_id=? AND e.event_type='wake.pickup_exhausted' "
                    "AND w.sprint_id=e.sprint_id "
                    "AND w.state IN ('delivered','failed') AND EXISTS ("
                    "SELECT 1 FROM sprint_wake_messages wm "
                    "JOIN wake_message m USING (message_id) "
                    "WHERE wm.wake_id=w.wake_id AND wm.sprint_id=e.sprint_id "
                    "AND m.read_at IS NULL) ORDER BY wake_id",
                    (sprint_id,),
                )
            )
            classification = "pickup_episode_reset"
            episode_name = "pickup-episode"
        else:
            return ()
        if not wake_ids:
            return ()

        placeholders = ",".join("?" for _ in wake_ids)
        wakes = self.con.execute(
            "SELECT w.wake_id,w.participant_id,w.receiver_shell_id,"
            "w.idempotency_key FROM sprint_wake_outbox w "
            f"WHERE w.sprint_id=? AND w.wake_id IN ({placeholders}) "
            "AND w.state IN ('delivered','failed') AND EXISTS ("
            "SELECT 1 FROM sprint_wake_messages wm "
            "JOIN wake_message m USING (message_id) "
            "WHERE wm.sprint_id=w.sprint_id AND wm.wake_id=w.wake_id "
            "AND m.read_at IS NULL) ORDER BY w.wake_id",
            (sprint_id, *wake_ids),
        ).fetchall()
        grouped: dict[int, list[sqlite3.Row]] = {}
        for wake in wakes:
            grouped.setdefault(int(wake["participant_id"]), []).append(wake)

        replacements: list[int] = []
        for participant_id, participant_wakes in grouped.items():
            receiver_shell_id = int(participant_wakes[0]["receiver_shell_id"])
            anchor_wake_id = max(int(wake["wake_id"]) for wake in participant_wakes)
            reset_key = (
                f"sprint-resume:{sprint_id}:{episode_name}:{anchor_wake_id}"
            )
            deliverable = self.con.execute(
                "SELECT wake_id,idempotency_key FROM sprint_wake_outbox "
                "WHERE receiver_shell_id=? AND state='pending'",
                (receiver_shell_id,),
            ).fetchone()
            if deliverable is None:
                replacement_wake_id = int(
                    self.con.execute(
                        "INSERT INTO sprint_wake_outbox "
                        "(sprint_id,participant_id,receiver_shell_id,idempotency_key,"
                        "available_at) VALUES (?,?,?,?,datetime('now'))",
                        (
                            sprint_id,
                            participant_id,
                            receiver_shell_id,
                            reset_key,
                        ),
                    ).lastrowid
                )
            else:
                replacement_wake_id = int(deliverable["wake_id"])
                if str(deliverable["idempotency_key"]) != reset_key:
                    self.con.execute(
                        "UPDATE sprint_wake_outbox SET idempotency_key=?,"
                        "available_at=datetime('now') WHERE wake_id=? "
                        "AND state='pending'",
                        (reset_key, replacement_wake_id),
                    )

            source_wake_ids = tuple(
                int(wake["wake_id"]) for wake in participant_wakes
            )
            source_placeholders = ",".join("?" for _ in source_wake_ids)
            unread_message_ids = tuple(
                int(row[0])
                for row in self.con.execute(
                    "SELECT m.message_id FROM sprint_wake_messages wm "
                    "JOIN wake_message m USING (message_id) "
                    f"WHERE wm.sprint_id=? AND wm.wake_id IN ({source_placeholders}) "
                    "AND m.read_at IS NULL ORDER BY m.message_id",
                    (sprint_id, *source_wake_ids),
                )
            )
            if not unread_message_ids:
                continue
            message_placeholders = ",".join("?" for _ in unread_message_ids)
            self.con.execute(
                "UPDATE wake_message SET delivered_at=NULL "
                f"WHERE message_id IN ({message_placeholders})",
                unread_message_ids,
            )
            self.con.execute(
                "UPDATE sprint_wake_messages SET wake_id=? "
                f"WHERE message_id IN ({message_placeholders})",
                (replacement_wake_id, *unread_message_ids),
            )
            for wake in participant_wakes:
                self._event(
                    sprint_id,
                    "wake.requeued",
                    actor,
                    {
                        "trigger": "resume",
                        "classification": classification,
                        "prior_wake_id": int(wake["wake_id"]),
                        "replacement_wake_id": replacement_wake_id,
                        "prior_idempotency_key": str(wake["idempotency_key"]),
                        "idempotency_key": reset_key,
                        "replacement_conversation_id": None,
                    },
                )
            replacements.append(replacement_wake_id)
        return tuple(sorted(set(replacements)))

    def abort(
        self,
        sprint_id: int,
        actor: LifecycleActor,
        *,
        reason: str,
        terminal_outcome: str | None,
    ) -> AbortReceipt:
        """Abort from prepared/armed/paused without deleting Sprint history."""
        reason = self._required_text(reason, "abort reason", 2000)
        outcome = self._required_text(
            terminal_outcome or "aborted",
            "abort outcome",
            500,
        )
        with db_driver.write_transaction(self.con, "sprint.abort"):
            sprint = self._sprint(sprint_id)
            current = str(sprint["lifecycle"])
            if current == "aborted":
                return AbortReceipt(False, None, (), ())
            self._require_edge(current, "aborted")
            self._authorize(sprint, "aborted", actor)
            run_ids, run_conversations = self._persist_interrupt_intents(sprint_id)
            self._update_lifecycle(
                sprint_id,
                current=current,
                target="aborted",
                outcome=outcome,
            )
            self._clear_coordinate_mode(
                sprint_id,
                actor,
                reason="Sprint cancelled by FnB",
            )
            report_id = int(
                self.con.execute(
                    "INSERT INTO sprint_reports "
                    "(sprint_id,report_kind,author_shell_id,body) "
                    "VALUES (?,'abort',?,?)",
                    (
                        sprint_id,
                        actor.shell_id,
                        json.dumps(
                            self._abort_report(sprint_id, current, reason, outcome),
                            sort_keys=True,
                        ),
                    ),
                ).lastrowid
            )
            _, notice_conversations = self._queue_planner_notice(
                sprint_id,
                body=f"Sprint {sprint_id} aborted: {reason}",
                idempotency_key=(
                    f"sprint-abort:{sprint_id}:v{int(sprint['version']) + 1}"
                ),
            )
            self._event(
                sprint_id,
                "lifecycle.aborted",
                actor,
                {
                    "from": current,
                    "reason": reason,
                    "report_id": report_id,
                    "interrupt_run_ids": list(run_ids),
                },
            )
            receipt = AbortReceipt(
                True,
                report_id,
                run_ids,
                tuple(
                    sorted(
                        set(run_conversations)
                        | set(notice_conversations)
                    )
                ),
            )
        self._signal_interrupts_and_notifications(receipt)
        return receipt

    def record_wake_failure(
        self,
        wake_id: int,
        error: str,
        *,
        target_conversation_id: str | None = None,
        expected_claim_owner: str | None = None,
    ) -> int:
        """Record one failed attempt; attempt three atomically auto-pauses."""
        error = error.strip()
        if not error:
            raise ValueError("wake failure requires an error")
        error = error[:16384]
        pause_receipts: list[PauseReceipt] = []
        closed_conversation_ids: list[str] = []
        with db_driver.write_transaction(self.con, "sprint.wake_failure"):
            row = self.con.execute(
                "SELECT wake_id,sprint_id,receiver_shell_id,state,attempt_count,"
                "claim_owner,idempotency_key "
                "FROM sprint_wake_outbox WHERE wake_id=?",
                (wake_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown Sprint wake: {wake_id}")
            if row["state"] in {"delivered", "failed", "cancelled"}:
                raise SprintInvariantError(
                    f"wake {wake_id} is terminal ({row['state']})"
                )
            if expected_claim_owner is not None and (
                row["state"] != "delivering"
                or row["claim_owner"] != expected_claim_owner
            ):
                raise SprintInvariantError("wake delivery claim is not owned")
            attempt = int(row["attempt_count"]) + 1
            if attempt > 3:
                raise SprintInvariantError(f"wake {wake_id} exhausted attempts")
            self.con.execute(
                "INSERT INTO sprint_wake_attempts "
                "(wake_id,attempt_number,target_conversation_id,outcome,error_detail) "
                "VALUES (?,?,?,'failed',?)",
                (wake_id, attempt, target_conversation_id, error),
            )
            terminal = attempt == 3
            backoff_seconds = WAKE_DELIVERY_BACKOFF_SECONDS[attempt - 1]
            self.con.execute(
                "UPDATE sprint_wake_outbox SET attempt_count=?,state=?,"
                "last_error=?,failed_at=CASE WHEN ? THEN datetime('now') "
                "ELSE NULL END,claim_owner=NULL,claimed_at=NULL,"
                "lease_expires_at=NULL,available_at=datetime('now', ?),"
                "quiet_since=CASE WHEN ? THEN NULL ELSE quiet_since END "
                "WHERE wake_id=?",
                (
                    attempt,
                    "failed" if terminal else "pending",
                    error,
                    1 if terminal else 0,
                    f"+{backoff_seconds} seconds",
                    1 if terminal else 0,
                    wake_id,
                ),
            )
            if terminal:
                recovery_exhausted = (
                    row["sprint_id"] is None
                    and str(row["idempotency_key"]).startswith("engine-recovery:")
                )
                active = active_chat_registry.get(
                    self.con, int(row["receiver_shell_id"])
                )
                closed = None
                if active is not None and (
                    target_conversation_id is None
                    or active.chat_id == target_conversation_id
                ):
                    closed = active_chat_registry.close_for_displacement(
                        self.con,
                        int(row["receiver_shell_id"]),
                        allow_live_process=True,
                    )
                if closed is not None:
                    sprint_participant_chats._append_event(
                        self.con,
                        closed.chat_id,
                        "conversation.closed",
                        {
                            "reason": (
                                "engine wake recovery exhausted; shell unbootable"
                                if recovery_exhausted
                                else "wake delivery exhausted"
                            ),
                            "state": "closed",
                            "wake_id": wake_id,
                            **(
                                {"unbootable_shell": True}
                                if recovery_exhausted
                                else {}
                            ),
                        },
                    )
                    closed_conversation_ids.append(closed.chat_id)
                if recovery_exhausted:
                    shell = self.con.execute(
                        "SELECT shortname FROM shells WHERE shell_id=?",
                        (int(row["receiver_shell_id"]),),
                    ).fetchone()
                    shortname = (
                        str(shell["shortname"])
                        if shell is not None and shell["shortname"]
                        else f"shell #{int(row['receiver_shell_id'])}"
                    )
                    self.con.execute(
                        "INSERT INTO flags "
                        "(display_name,priority,description,shell_id) "
                        "VALUES (?,'High',?,?)",
                        (
                            f"[Engine] {shortname} unbootable after wake recovery",
                            (
                                "Engine wake recovery exhausted its three-attempt "
                                f"budget (wake #{wake_id}, key "
                                f"{row['idempotency_key']}). Undelivered messages "
                                "remain attached to the terminal wake; manual "
                                "operator recovery is required."
                            ),
                            int(row["receiver_shell_id"]),
                        ),
                    )
                affected_sprints = self.con.execute(
                    "SELECT DISTINCT s.* FROM sprint_wake_messages wm "
                    "JOIN wake_message m USING (message_id) "
                    "JOIN sprints s ON s.sprint_id=m.sprint_id "
                    "WHERE wm.wake_id=? AND m.delivered_at IS NULL "
                    "AND s.lifecycle='armed' ORDER BY s.sprint_id",
                    (wake_id,),
                ).fetchall()
                for sprint in affected_sprints:
                    pause_receipts.append(
                        self._pause_in_transaction(
                            sprint,
                            LifecycleActor("system"),
                            reason="wake_delivery_exhausted",
                            detail={
                                "wake_id": wake_id,
                                "attempts": 3,
                                "last_error": error,
                            },
                        )
                    )
        for pause_receipt in pause_receipts:
            self._signal_interrupts_and_notifications(pause_receipt)
        for conversation_id in closed_conversation_ids:
            conversation_events.notify(conversation_id)
        return attempt

    def recover_on_startup(
        self, sprint_id: int | None = None
    ) -> tuple[tuple[int, str], ...]:
        """Reassert durable non-terminal recovery facts without changing state."""
        sql = (
            "SELECT sprint_id,lifecycle FROM sprints "
            "WHERE lifecycle IN ('prepared','armed','paused')"
        )
        params: tuple[int, ...] = ()
        if sprint_id is not None:
            sql += " AND sprint_id=?"
            params = (sprint_id,)
        rows = self.con.execute(sql + " ORDER BY sprint_id", params).fetchall()
        recovered: list[tuple[int, str]] = []
        for row in rows:
            sprint_id = int(row["sprint_id"])
            lifecycle = str(row["lifecycle"])
            run_ids: tuple[int, ...] = ()
            conversations: tuple[str, ...] = ()
            pause_receipt: PauseReceipt | None = None
            if lifecycle in {"armed", "paused"}:
                with db_driver.write_transaction(
                    self.con, "sprint.recover.startup"
                ):
                    if lifecycle == "paused":
                        run_ids, conversations = self._persist_interrupt_intents(
                            sprint_id
                        )
                    else:
                        reconciliation = self._reconcile_unread_wakes_in_transaction(
                            sprint_id,
                            trigger="startup",
                        )
                        pause_receipt = reconciliation.pause_receipt
                        if pause_receipt is None:
                            merge_pause_receipts: list[PauseReceipt] = []
                            self._reconcile_registered_prs_in_transaction(
                                sprint_id,
                                pause_receipts=merge_pause_receipts,
                            )
                            if merge_pause_receipts:
                                pause_receipt = merge_pause_receipts[0]
                            else:
                                pause_receipt = SprintWorkUnitStore(
                                    self.con, lifecycle_store=self
                                )._queue_delivery_terminal(sprint_id)
            if pause_receipt is not None:
                self._signal_interrupts_and_notifications(pause_receipt)
            elif run_ids or conversations:
                self._signal_interrupts_and_notifications(
                    PauseReceipt(True, None, run_ids, conversations)
                )
            recovered.append((sprint_id, lifecycle))
        return tuple(recovered)

    def reconcile_unread_pickup(
        self,
        sprint_id: int,
        *,
        trigger: str = "pulse",
    ) -> tuple[int, ...]:
        """Repair terminal wake delivery that left relevant messages unread."""
        trigger = self._required_text(trigger, "pickup recovery trigger", 64)
        with db_driver.write_transaction(self.con, "sprint.wake.pickup_recover"):
            sprint = self._sprint(sprint_id)
            if sprint["lifecycle"] != "armed":
                reconciliation = _WakeReconcileResult(())
            else:
                reconciliation = self._reconcile_unread_wakes_in_transaction(
                    sprint_id,
                    trigger=trigger,
                )
        if reconciliation.pause_receipt is not None:
            self._signal_interrupts_and_notifications(
                reconciliation.pause_receipt
            )
        return reconciliation.wake_ids

    def _pause_in_transaction(
        self,
        sprint: sqlite3.Row,
        actor: LifecycleActor,
        *,
        reason: str,
        detail: dict,
        notice_body: str | None = None,
    ) -> PauseReceipt:
        if not self.con.in_transaction:
            raise RuntimeError("pause requires an active transaction")
        sprint_id = int(sprint["sprint_id"])
        current = str(sprint["lifecycle"])
        self._require_edge(current, "paused")
        self._authorize(sprint, "paused", actor)
        run_ids, run_conversations = self._persist_interrupt_intents(sprint_id)
        template = self._pause_report(sprint_id, current, reason, actor, detail)
        self._update_lifecycle(
            sprint_id,
            current=current,
            target="paused",
            outcome=None,
        )
        report_id = int(
            self.con.execute(
                "INSERT INTO sprint_reports "
                "(sprint_id,report_kind,author_shell_id,body) "
                "VALUES (?,'pause',?,?)",
                (sprint_id, actor.shell_id, json.dumps(template, sort_keys=True)),
            ).lastrowid
        )
        _, notice_conversations = self._queue_planner_notice(
            sprint_id,
            body=notice_body or f"Sprint {sprint_id} paused: {reason}",
            idempotency_key=(
                f"sprint-pause:{sprint_id}:v{int(sprint['version']) + 1}"
            ),
        )
        self._event(
            sprint_id,
            "lifecycle.paused",
            actor,
            {
                "from": current,
                "reason": reason,
                "report_id": report_id,
                "interrupt_run_ids": list(run_ids),
                **detail,
            },
        )
        return PauseReceipt(
            True,
            report_id,
            run_ids,
            tuple(sorted(set(run_conversations) | set(notice_conversations))),
        )

    def _persist_interrupt_intents(
        self, sprint_id: int
    ) -> tuple[tuple[int, ...], tuple[str, ...]]:
        if not self.con.in_transaction:
            raise RuntimeError("interrupt persistence requires an active transaction")
        rows = self.con.execute(
            "SELECT DISTINCT r.run_id FROM conversation_runs r "
            "JOIN sprint_participant_conversations pc "
            "ON pc.conversation_id=r.conversation_id "
            "JOIN sprint_participants p "
            "ON p.participant_id=pc.sprint_participant_id "
            "WHERE p.sprint_id=? AND r.state IN ('leased','starting','running') "
            "ORDER BY r.run_id",
            (sprint_id,),
        ).fetchall()
        now = str(self.con.execute("SELECT datetime('now')").fetchone()[0])
        run_ids: list[int] = []
        conversations: list[str] = []
        for row in rows:
            run_id = int(row["run_id"])
            _, conversation_id = (
                conversation_broker.BrokerStore.request_interrupt_in_transaction(
                    self.con,
                    run_id,
                    requested_at=now,
                )
            )
            run_ids.append(run_id)
            conversations.append(conversation_id)
        return tuple(run_ids), tuple(sorted(set(conversations)))

    def _pause_report(
        self,
        sprint_id: int,
        lifecycle_before: str,
        reason: str,
        actor: LifecycleActor,
        detail: dict,
    ) -> dict:
        active_turns = [
            dict(row)
            for row in self.con.execute(
                "SELECT r.run_id,r.conversation_id,r.state,r.lease_owner,"
                "r.heartbeat_at,r.lease_expires_at FROM conversation_runs r "
                "JOIN sprint_participant_conversations pc "
                "ON pc.conversation_id=r.conversation_id "
                "JOIN sprint_participants p "
                "ON p.participant_id=pc.sprint_participant_id "
                "WHERE p.sprint_id=? AND r.state IN ('leased','starting','running') "
                "ORDER BY r.run_id",
                (sprint_id,),
            )
        ]
        work_units = [
            dict(row)
            for row in self.con.execute(
                "SELECT work_unit_id,assigned_shell_id,reviewer_shell_id,title,"
                "planned_wave,disposition FROM sprint_work_units "
                "WHERE sprint_id=? ORDER BY work_unit_id",
                (sprint_id,),
            )
        ]
        prs = [
            dict(row)
            for row in self.con.execute(
                "SELECT pr.registered_pr_id,pr.repository,pr.pr_number,"
                "t.normalized_state,t.observed_head_sha,t.observed_at "
                "FROM sprint_registered_prs pr LEFT JOIN sprint_pr_transitions t "
                "ON t.transition_id=(SELECT MAX(latest.transition_id) "
                "FROM sprint_pr_transitions latest "
                "WHERE latest.registered_pr_id=pr.registered_pr_id) "
                "WHERE pr.sprint_id=? ORDER BY pr.registered_pr_id",
                (sprint_id,),
            )
        ]
        anomalies = [
            {"event_type": row["event_type"], "payload": json.loads(row["payload"])}
            for row in self.con.execute(
                "SELECT event_type,payload FROM sprint_events WHERE sprint_id=? "
                "AND (event_type LIKE '%failed%' OR event_type LIKE '%error%' "
                "OR event_type LIKE '%unknown%' OR event_type LIKE '%escalat%') "
                "ORDER BY event_id DESC LIMIT 20",
                (sprint_id,),
            )
        ]
        return {
            "reason": reason,
            "actor": {"kind": actor.kind, "shell_id": actor.shell_id},
            "lifecycle_before": lifecycle_before,
            "detail": detail,
            "deterministic": {
                "active_turns": active_turns,
                "work_units": work_units,
                "registered_prs": prs,
                "recent_anomalies": anomalies,
            },
            "integrity_threat": "",
            "judgment": "",
            "recommendation": "",
        }

    def _abort_report(
        self,
        sprint_id: int,
        lifecycle_before: str,
        reason: str,
        outcome: str,
    ) -> dict:
        rows = self.con.execute(
            "SELECT work_unit_id,title,disposition FROM sprint_work_units "
            "WHERE sprint_id=? ORDER BY work_unit_id",
            (sprint_id,),
        ).fetchall()
        completed = [dict(row) for row in rows if row["disposition"] == "completed"]
        outstanding = [
            dict(row) for row in rows if row["disposition"] != "completed"
        ]
        return {
            "reason": reason,
            "terminal_outcome": outcome,
            "lifecycle_before": lifecycle_before,
            "completed_work": completed,
            "outstanding_work": outstanding,
            "recovery_disposition": "",
        }

    def _reconcile_registered_prs_in_transaction(
        self,
        sprint_id: int,
        *,
        pause_receipts: list[PauseReceipt] | None = None,
    ) -> tuple[tuple[int, ...], tuple[int, ...]]:
        if not self.con.in_transaction:
            raise RuntimeError("PR reconciliation requires an active transaction")
        rows = self.con.execute(
            "SELECT pr.registered_pr_id,t.transition_key,t.normalized_state "
            "FROM sprint_registered_prs pr JOIN sprint_pr_transitions t "
            "ON t.transition_id=(SELECT MAX(latest.transition_id) "
            "FROM sprint_pr_transitions latest "
            "WHERE latest.registered_pr_id=pr.registered_pr_id) "
            "WHERE pr.sprint_id=? ORDER BY pr.registered_pr_id",
            (sprint_id,),
        ).fetchall()
        projected: list[int] = []
        resolved: list[int] = []
        units = SprintWorkUnitStore(self.con)
        for row in rows:
            registered_pr_id = int(row["registered_pr_id"])
            unit_ids = tuple(
                int(item[0])
                for item in self.con.execute(
                    "SELECT work_unit_id FROM sprint_pr_work_units "
                    "WHERE registered_pr_id=? AND superseded_at IS NULL "
                    "ORDER BY work_unit_id",
                    (registered_pr_id,),
                )
            )
            if row["normalized_state"] == "merged":
                incomplete = tuple(
                    int(item[0])
                    for item in self.con.execute(
                        "SELECT work_unit_id FROM sprint_work_units "
                        "WHERE work_unit_id IN ("
                        + ",".join("?" for _ in unit_ids)
                        + ") AND disposition<>'completed' ORDER BY work_unit_id",
                        unit_ids,
                    )
                ) if unit_ids else ()
                if incomplete:
                    merge_ready = tuple(
                        int(item[0])
                        for item in self.con.execute(
                            "SELECT work_unit_id FROM sprint_work_units "
                            "WHERE work_unit_id IN ("
                            + ",".join("?" for _ in unit_ids)
                            + ") AND disposition='merge_ready' "
                            "ORDER BY work_unit_id",
                            unit_ids,
                        )
                    )
                    units.complete_from_merge_in_transaction(
                        sprint_id,
                        unit_ids,
                        transition_key=str(row["transition_key"]),
                        dispatch=False,
                        pause_receipts=pause_receipts,
                    )
                    projected.extend(merge_ready)
            elif row["normalized_state"] == "closed":
                for unit_id in unit_ids:
                    resolved.extend(
                        self._resolve_review_expectations_in_transaction(
                            unit_id,
                            "registered_pr.closed_without_merge",
                        )
                    )
        return tuple(sorted(set(projected))), tuple(sorted(set(resolved)))

    def _reconcile_unread_wakes_in_transaction(
        self,
        sprint_id: int,
        *,
        trigger: str,
    ) -> _WakeReconcileResult:
        if not self.con.in_transaction:
            raise RuntimeError("wake reconciliation requires an active transaction")
        rows = self.con.execute(
            "SELECT w.wake_id,w.sprint_id AS wake_sprint_id,"
            "m.to_participant_id AS participant_id,w.state,w.idempotency_key,"
            "w.attempt_count AS wake_attempt_count,MIN(m.message_id) AS message_id,"
            "(SELECT detail.work_unit_id FROM wake_message detail "
            "JOIN sprint_wake_messages detail_wm "
            "ON detail_wm.message_id=detail.message_id "
            "WHERE detail_wm.wake_id=w.wake_id AND detail.read_at IS NULL "
            "ORDER BY detail.message_id LIMIT 1) AS work_unit_id,"
            "CASE WHEN COUNT(*)=COUNT(m.delivered_at) "
            "THEN MIN(m.delivered_at) END AS delivered_at "
            "FROM sprint_wake_outbox w "
            "JOIN sprint_wake_messages wm ON wm.wake_id=w.wake_id "
            "JOIN wake_message m ON m.message_id=wm.message_id "
            "WHERE m.sprint_id=? AND wm.sprint_id=m.sprint_id "
            "AND w.state IN ('failed','delivered') "
            "AND m.read_at IS NULL "
            "GROUP BY w.wake_id,w.sprint_id,m.to_participant_id,"
            "w.state,w.idempotency_key "
            "ORDER BY w.wake_id",
            (sprint_id,),
        ).fetchall()
        replacements: list[int] = []
        for row in rows:
            old_wake_id = int(row["wake_id"])
            participant_id = int(row["participant_id"])
            wake_key = str(row["idempotency_key"])
            turn = self._wake_turn_evidence(old_wake_id)
            if (
                row["delivered_at"] is not None
                and row["wake_sprint_id"] != sprint_id
                and turn["run_state"] == "succeeded"
            ):
                continue
            # The delivered turn is still queued or actively running: the
            # receiver has not had its chance to read yet.  Requeuing now
            # duplicates the wake into the same chat under a fresh key.
            if turn["turn_live"]:
                continue
            shell_busy = (
                turn["run_state"] == "failed"
                and turn["error_code"] == "SHELL_BUSY"
            )
            busy_prefix = f"sprint-recovery:{sprint_id}:busy:"
            busy_recovery = wake_key.startswith(busy_prefix)
            if ":failed-wake:" in wake_key:
                continue
            if self._participant_has_pickup_turn(participant_id):
                continue
            classification: dict[str, str | int] = {}
            if shell_busy:
                original_wake_id = (
                    self._busy_recovery_origin(wake_key, sprint_id)
                    if busy_recovery
                    else old_wake_id
                )
                key_prefix = f"{busy_prefix}{original_wake_id}:"
                recovery_count = int(
                    self.con.execute(
                        "SELECT COUNT(*) FROM sprint_wake_outbox "
                        "WHERE sprint_id=? AND idempotency_key LIKE ?",
                        (sprint_id, f"{key_prefix}%"),
                    ).fetchone()[0]
                )
                current_attempt = 1 + recovery_count
                if current_attempt >= WAKE_CONTENTION_ATTEMPTS:
                    slot_state = self._busy_slot_state(turn["error_detail"])
                    shell = str(
                        self.con.execute(
                            "SELECT sh.shortname FROM sprint_participants p "
                            "JOIN shells sh USING (shell_id) "
                            "WHERE p.participant_id=?",
                            (participant_id,),
                        ).fetchone()[0]
                    )
                    pause_receipt = self._pause_in_transaction(
                        self._sprint(sprint_id),
                        LifecycleActor("system"),
                        reason="wake_contention_exhausted",
                        detail={
                            "wake_id": old_wake_id,
                            "original_wake_id": original_wake_id,
                            "participant_id": participant_id,
                            "shell": shell,
                            "attempts": current_attempt,
                            "slot_state": slot_state,
                            "last_error": turn["error_detail"],
                        },
                        notice_body=(
                            f"Sprint {sprint_id} paused: wake contention exhausted "
                            f"for shell {shell} after {current_attempt} attempts; "
                            f"last slot state: {slot_state}."
                        ),
                    )
                    return _WakeReconcileResult(
                        tuple(sorted(set(replacements))),
                        pause_receipt,
                    )
                next_attempt = current_attempt + 1
                backoff_seconds = WAKE_CONTENTION_BACKOFF_SECONDS[
                    next_attempt - 2
                ]
                recovery_key = f"{key_prefix}{next_attempt}"
                classification = {
                    "classification": "shell_busy",
                    "attempt": next_attempt,
                    "backoff_seconds": backoff_seconds,
                }
            else:
                if row["state"] == "delivered":
                    episode_attempt, lineage_valid = self._pickup_episode_attempt(
                        sprint_id,
                        old_wake_id,
                    )
                    evidence_valid = self._pickup_turn_evidence_valid(row, turn)
                    run_state = turn["run_state"]
                    if not evidence_valid or not lineage_valid:
                        pause_reason = "wake_pickup_evidence_invalid"
                        failure_class = "evidence_invalid"
                        error_code = "WAKE_PICKUP_EVIDENCE_INVALID"
                    elif run_state == "unknown":
                        pause_reason = "wake_pickup_unknown"
                        failure_class = "native_unknown"
                        error_code = str(turn["error_code"])
                    elif episode_attempt >= 2 and run_state == "failed":
                        pause_reason = "wake_pickup_failed"
                        failure_class = "native_failed"
                        error_code = str(turn["error_code"])
                    elif episode_attempt >= 2 and run_state == "succeeded":
                        pause_reason = "wake_pickup_unread"
                        failure_class = "terminal_unread"
                        error_code = "WAKE_PICKUP_UNREAD"
                    else:
                        pause_reason = None

                    if pause_reason is not None:
                        pause_receipt = self._exhaust_pickup_in_transaction(
                            sprint_id=sprint_id,
                            participant_id=participant_id,
                            wake_id=old_wake_id,
                            message_id=int(row["message_id"]),
                            work_unit_id=(
                                int(row["work_unit_id"])
                                if row["work_unit_id"] is not None
                                else None
                            ),
                            turn=turn,
                            pause_reason=pause_reason,
                            error_code=error_code,
                            failure_class=failure_class,
                            attempt_count=episode_attempt,
                        )
                        return _WakeReconcileResult(
                            tuple(sorted(set(replacements))),
                            pause_receipt,
                        )
                recovery_key = (
                    f"sprint-resume:{sprint_id}:failed-wake:{old_wake_id}"
                    if row["state"] == "failed"
                    else f"sprint-recovery:{sprint_id}:delivered-unread:{old_wake_id}"
                )
            receiver_shell_id = _participant_shell_id(self.con, participant_id)
            deliverable = self.con.execute(
                "SELECT wake_id,idempotency_key FROM sprint_wake_outbox WHERE "
                "(receiver_shell_id=? AND state='pending') "
                + (
                    ""
                    if shell_busy
                    else "OR (sprint_id=? AND participant_id=? "
                    "AND state='delivering') "
                )
                + "ORDER BY (state='pending') DESC,wake_id LIMIT 1",
                (
                    (receiver_shell_id,)
                    if shell_busy
                    else (receiver_shell_id, sprint_id, participant_id)
                ),
            ).fetchone()
            if deliverable is None:
                prior_recovery = self.con.execute(
                    "SELECT wake_id,state,idempotency_key FROM sprint_wake_outbox "
                    "WHERE idempotency_key=?",
                    (recovery_key,),
                ).fetchone()
                if prior_recovery is not None:
                    if prior_recovery["state"] not in {"pending", "delivering"}:
                        continue
                    deliverable = prior_recovery
            created = deliverable is None
            if created:
                if shell_busy:
                    new_wake_id = int(
                        self.con.execute(
                            "INSERT INTO sprint_wake_outbox "
                            "(sprint_id,participant_id,receiver_shell_id,"
                            "idempotency_key,available_at) "
                            "VALUES (?,?,?,?,datetime('now', ?))",
                            (
                                sprint_id,
                                participant_id,
                                receiver_shell_id,
                                recovery_key,
                                f"+{backoff_seconds} seconds",
                            ),
                        ).lastrowid
                    )
                else:
                    new_wake_id = int(
                        self.con.execute(
                            "INSERT INTO sprint_wake_outbox "
                            "(sprint_id,participant_id,receiver_shell_id,"
                            "idempotency_key) VALUES (?,?,?,?)",
                            (
                                sprint_id,
                                participant_id,
                                receiver_shell_id,
                                recovery_key,
                            ),
                        ).lastrowid
                    )
            else:
                new_wake_id = int(deliverable["wake_id"])
            if self._wake_requeue_recorded(sprint_id, old_wake_id, new_wake_id):
                continue
            if (
                shell_busy
                and not created
                and deliverable["idempotency_key"] != recovery_key
            ):
                self.con.execute(
                    "UPDATE sprint_wake_outbox SET idempotency_key=?,"
                    "available_at=datetime('now', ?) "
                    "WHERE wake_id=? AND state='pending'",
                    (
                        recovery_key,
                        f"+{backoff_seconds} seconds",
                        new_wake_id,
                    ),
                )
            recovered_message_ids = [
                int(message["message_id"])
                for message in self.con.execute(
                    "SELECT message_id FROM sprint_wake_messages "
                    "WHERE sprint_id=? AND wake_id=? ORDER BY message_id",
                    (sprint_id, old_wake_id),
                )
            ]
            self.con.execute(
                "UPDATE wake_message SET delivered_at=NULL WHERE message_id IN ("
                "SELECT message_id FROM sprint_wake_messages "
                "WHERE sprint_id=? AND wake_id=?)",
                (sprint_id, old_wake_id),
            )
            self.con.execute(
                "UPDATE sprint_wake_messages SET wake_id=? "
                "WHERE sprint_id=? AND wake_id=?",
                (new_wake_id, sprint_id, old_wake_id),
            )
            recovery_event_id = self._event(
                sprint_id,
                "wake.requeued",
                LifecycleActor("system"),
                {
                    "trigger": trigger,
                    "prior_wake_id": old_wake_id,
                    "prior_wake_state": str(row["state"]),
                    "prior_turn_state": turn,
                    "replacement_wake_id": new_wake_id,
                    "replacement_created": created,
                    "replacement_conversation_id": None,
                    **classification,
                    **(
                        {"failed_wake_id": old_wake_id}
                        if row["state"] == "failed"
                        else {}
                    ),
                },
            )
            self.con.executemany(
                "INSERT INTO sprint_wake_recovery_messages "
                "(recovery_event_id,sprint_id,prior_wake_id,replacement_wake_id,"
                "message_id) VALUES (?,?,?,?,?)",
                [
                    (
                        recovery_event_id,
                        sprint_id,
                        old_wake_id,
                        new_wake_id,
                        message_id,
                    )
                    for message_id in recovered_message_ids
                ],
            )
            replacements.append(new_wake_id)

        unwoken = self.con.execute(
            "SELECT m.message_id,m.to_participant_id FROM wake_message m "
            "WHERE m.sprint_id=? AND m.read_at IS NULL "
            "AND m.idempotency_key LIKE 'decline-result:%' "
            "AND m.to_participant_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM sprint_wake_messages wm "
            "WHERE wm.message_id=m.message_id) ORDER BY m.message_id",
            (sprint_id,),
        ).fetchall()
        for message in unwoken:
            participant_id = int(message["to_participant_id"])
            if self._participant_has_pickup_turn(participant_id):
                continue
            receiver_shell_id = _participant_shell_id(self.con, participant_id)
            deliverable = self.con.execute(
                "SELECT wake_id FROM sprint_wake_outbox WHERE "
                "(receiver_shell_id=? AND state='pending') OR "
                "(sprint_id=? AND participant_id=? AND state='delivering') "
                "ORDER BY (state='pending') DESC,wake_id LIMIT 1",
                (receiver_shell_id, sprint_id, participant_id),
            ).fetchone()
            created = deliverable is None
            if created:
                new_wake_id = int(
                    self.con.execute(
                        "INSERT INTO sprint_wake_outbox "
                        "(sprint_id,participant_id,receiver_shell_id,"
                        "idempotency_key) VALUES (?,?,?,?)",
                        (
                            sprint_id,
                            participant_id,
                            receiver_shell_id,
                            (
                                f"sprint-recovery:{sprint_id}:unwoken:"
                                f"{int(message['message_id'])}"
                            ),
                        ),
                    ).lastrowid
                )
            else:
                new_wake_id = int(deliverable["wake_id"])
            self.con.execute(
                "INSERT INTO sprint_wake_messages "
                "(sprint_id,wake_id,message_id) VALUES (?,?,?)",
                (sprint_id, new_wake_id, int(message["message_id"])),
            )
            recovery_event_id = self._event(
                sprint_id,
                "wake.requeued",
                LifecycleActor("system"),
                {
                    "trigger": trigger,
                    "prior_wake_id": None,
                    "prior_wake_state": "absent",
                    "prior_turn_state": {},
                    "message_id": int(message["message_id"]),
                    "replacement_wake_id": new_wake_id,
                    "replacement_created": created,
                    "replacement_conversation_id": None,
                },
            )
            self.con.execute(
                "INSERT INTO sprint_wake_recovery_messages "
                "(recovery_event_id,sprint_id,prior_wake_id,replacement_wake_id,"
                "message_id) VALUES (?, ?, NULL, ?, ?)",
                (
                    recovery_event_id,
                    sprint_id,
                    new_wake_id,
                    int(message["message_id"]),
                ),
            )
            replacements.append(new_wake_id)
        return _WakeReconcileResult(tuple(sorted(set(replacements))))

    def _pickup_episode_attempt(
        self,
        sprint_id: int,
        wake_id: int,
    ) -> tuple[int, bool]:
        rows = self.con.execute(
            "SELECT payload FROM sprint_events WHERE sprint_id=? "
            "AND event_type='wake.requeued' "
            "AND json_extract(payload,'$.replacement_wake_id')=? "
            "ORDER BY event_id",
            (sprint_id, wake_id),
        ).fetchall()
        pickup_rows = []
        for row in rows:
            payload = json.loads(row["payload"])
            if payload.get("classification") in {
                "shell_busy",
                "contention_episode_reset",
                "pickup_episode_reset",
            }:
                continue
            if payload.get("prior_wake_id") is not None:
                pickup_rows.append(payload)
        if not pickup_rows:
            return 1, True
        valid = all(
            isinstance(payload.get("prior_wake_id"), int)
            and self.con.execute(
                "SELECT 1 FROM sprint_wake_outbox WHERE wake_id=?",
                (payload["prior_wake_id"],),
            ).fetchone()
            is not None
            for payload in pickup_rows
        )
        return 2, valid

    @staticmethod
    def _pickup_turn_evidence_valid(
        wake: sqlite3.Row,
        turn: dict[str, str | int | bool | None],
    ) -> bool:
        if turn["turn_live"]:
            return True
        if (
            turn["attempt_number"] is None
            or turn["target_conversation_id"] is None
            or turn["attempt_outcome"] != "delivered"
            or int(turn["attempt_number"]) != int(wake["wake_attempt_count"])
        ):
            return False
        run_state = turn["run_state"]
        message_state = turn["message_state"]
        error_code = turn["error_code"]
        if run_state == "succeeded":
            return message_state == "completed" and error_code is None
        if run_state == "cancelled":
            return (
                message_state == "cancelled"
                and turn["interrupt_requested"] is True
            )
        if run_state in {"failed", "unknown"}:
            return (
                message_state == "failed"
                and isinstance(error_code, str)
                and bool(error_code.strip())
            )
        return False

    def _exhaust_pickup_in_transaction(
        self,
        *,
        sprint_id: int,
        participant_id: int,
        wake_id: int,
        message_id: int,
        work_unit_id: int | None,
        turn: dict[str, str | int | bool | None],
        pause_reason: str,
        error_code: str,
        failure_class: str,
        attempt_count: int,
    ) -> PauseReceipt:
        participant = self.con.execute(
            "SELECT p.role,sh.shortname,sh.shell_id "
            "FROM sprint_participants p JOIN shells sh USING (shell_id) "
            "WHERE p.sprint_id=? AND p.participant_id=?",
            (sprint_id, participant_id),
        ).fetchone()
        if participant is None:
            raise SprintInvariantError("Sprint participant does not exist")
        conversation_id = (
            str(turn["target_conversation_id"])
            if turn["target_conversation_id"] is not None
            else None
        )
        facts = {
            "sprint_id": sprint_id,
            "participant_id": participant_id,
            "shell": str(participant["shortname"]),
            "role": str(participant["role"]),
            "work_unit_id": work_unit_id,
            "message_id": message_id,
            "wake_id": wake_id,
            "conversation_id": conversation_id,
            "run_state": turn["run_state"],
            "error_code": error_code,
            "failure_class": failure_class,
            "attempt_count": attempt_count,
        }
        self._event(
            sprint_id,
            "wake.pickup_exhausted",
            LifecycleActor("system"),
            facts,
        )
        closed_conversation_id = self._close_exhausted_pickup_conversation(
            participant_id=participant_id,
            wake_id=wake_id,
            conversation_id=conversation_id,
            run_state=turn["run_state"],
        )
        detail = {
            **facts,
            "error_detail": str(turn["error_detail"] or "")[:2000] or None,
        }
        receipt = self._pause_in_transaction(
            self._sprint(sprint_id),
            LifecycleActor("system"),
            reason=pause_reason,
            detail=detail,
            notice_body=(
                f"Sprint {sprint_id} paused: pickup {failure_class} for "
                f"{participant['role']} shell {participant['shortname']} "
                f"(work unit {work_unit_id}, message {message_id}, wake {wake_id}, "
                f"{error_code}). Inspect the pause report, repair the named route "
                "or service, then use an authorized resume."
            ),
        )
        if closed_conversation_id is None:
            return receipt
        return PauseReceipt(
            receipt.changed,
            receipt.report_id,
            receipt.interrupt_run_ids,
            tuple(
                sorted(
                    set(receipt.notification_conversation_ids)
                    | {closed_conversation_id}
                )
            ),
        )

    def _close_exhausted_pickup_conversation(
        self,
        *,
        participant_id: int,
        wake_id: int,
        conversation_id: str | None,
        run_state: str | int | bool | None,
    ) -> str | None:
        if conversation_id is None or run_state not in {"failed", "unknown"}:
            return None
        row = self.con.execute(
            "SELECT c.state,c.shell_id FROM conversations c "
            "JOIN sprint_participants p ON p.shell_id=c.shell_id "
            "WHERE c.conversation_id=? AND p.participant_id=?",
            (conversation_id, participant_id),
        ).fetchone()
        if row is None or row["state"] != "error":
            return None
        shell_id = int(row["shell_id"])
        active = active_chat_registry.get(self.con, shell_id)
        if active is not None and active.chat_id == conversation_id:
            if active_chat_registry.has_live_process(active):
                return None
            closed = active_chat_registry.close_for_displacement(
                self.con,
                shell_id,
                allow_live_process=False,
            )
            if closed is None:
                return None
        else:
            changed = self.con.execute(
                "UPDATE conversations SET state='closed',closed_at=datetime('now'),"
                "last_activity_at=datetime('now'),version=version+1 "
                "WHERE conversation_id=? AND state='error'",
                (conversation_id,),
            ).rowcount
            if changed != 1:
                return None
        sprint_participant_chats._append_event(
            self.con,
            conversation_id,
            "conversation.closed",
            {
                "reason": "wake pickup exhausted",
                "state": "closed",
                "wake_id": wake_id,
            },
        )
        return conversation_id

    @staticmethod
    def _busy_recovery_origin(wake_key: str, sprint_id: int) -> int:
        prefix = f"sprint-recovery:{sprint_id}:busy:"
        origin, separator, attempt = wake_key.removeprefix(prefix).partition(":")
        if not separator or not origin.isdigit() or not attempt.isdigit():
            raise SprintInvariantError(
                f"invalid SHELL_BUSY recovery key: {wake_key}"
            )
        return int(origin)

    @staticmethod
    def _busy_slot_state(error_detail: str | int | None) -> str:
        detail = str(error_detail or "").lower()
        return "orphan" if "(orphan)" in detail else "busy"

    def _wake_requeue_recorded(
        self,
        sprint_id: int,
        prior_wake_id: int,
        replacement_wake_id: int,
    ) -> bool:
        rows = self.con.execute(
            "SELECT payload FROM sprint_events WHERE sprint_id=? "
            "AND event_type='wake.requeued' ORDER BY event_id",
            (sprint_id,),
        ).fetchall()
        return any(
            payload.get("prior_wake_id") == prior_wake_id
            and payload.get("replacement_wake_id") == replacement_wake_id
            for payload in (json.loads(row["payload"]) for row in rows)
        )

    def _participant_has_pickup_turn(self, participant_id: int) -> bool:
        return self.con.execute(
            "SELECT 1 FROM sprint_participant_conversations pc "
            "JOIN conversation_messages m "
            "ON m.conversation_id=pc.conversation_id "
            "JOIN conversations c ON c.conversation_id=m.conversation_id "
            "WHERE pc.sprint_participant_id=? "
            "AND c.state IN ('idle','queued','running','waiting') "
            "AND (m.state='queued' OR (m.state='running' AND EXISTS ("
            "SELECT 1 FROM conversation_runs r WHERE r.trigger_message_id=m.message_id "
            "AND r.state IN ('leased','starting','running') AND NOT EXISTS ("
            "SELECT 1 FROM conversation_events e WHERE e.run_id=r.run_id "
            "AND e.event_type='run.interrupt.requested')))) LIMIT 1",
            (participant_id,),
        ).fetchone() is not None

    def _wake_turn_evidence(
        self, wake_id: int
    ) -> dict[str, str | int | bool | None]:
        wake = self.con.execute(
            "SELECT idempotency_key FROM sprint_wake_outbox WHERE wake_id=?",
            (wake_id,),
        ).fetchone()
        attempt = self.con.execute(
            "SELECT attempt_number,target_conversation_id,native_run_ref,outcome "
            "FROM sprint_wake_attempts WHERE wake_id=? "
            "ORDER BY attempt_number DESC LIMIT 1",
            (wake_id,),
        ).fetchone()
        if wake is None or attempt is None:
            return {
                "attempt_number": None,
                "target_conversation_id": None,
                "native_run_ref": None,
                "attempt_outcome": None,
                "message_state": None,
                "run_state": None,
                "error_code": None,
                "error_detail": None,
                "interrupt_requested": False,
                "turn_live": False,
            }
        message = self.con.execute(
            "SELECT m.message_id,m.state,"
            "c.state AS conversation_state FROM conversation_messages m "
            "JOIN conversations c ON c.conversation_id=m.conversation_id "
            "WHERE m.conversation_id=? AND m.idempotency_key=?",
            (attempt["target_conversation_id"], wake["idempotency_key"]),
        ).fetchone()
        run_state = None
        error_code = None
        error_detail = None
        interrupt_requested = False
        turn_live = False
        if message is not None:
            run = self.con.execute(
                "SELECT run_id,state,error_code,error_detail FROM conversation_runs "
                "WHERE trigger_message_id=? "
                "ORDER BY attempt DESC LIMIT 1",
                (message["message_id"],),
            ).fetchone()
            if run is not None:
                run_state = str(run["state"])
                error_code = run["error_code"]
                error_detail = run["error_detail"]
                interrupt_requested = self.con.execute(
                    "SELECT 1 FROM conversation_events WHERE run_id=? "
                    "AND event_type='run.interrupt.requested' LIMIT 1",
                    (run["run_id"],),
                ).fetchone() is not None
            if str(message["conversation_state"]) in {
                "idle",
                "queued",
                "running",
                "waiting",
            }:
                if str(message["state"]) == "queued":
                    turn_live = True
                elif (
                    str(message["state"]) == "running"
                    and run is not None
                    and run_state in {"leased", "starting", "running"}
                ):
                    turn_live = (
                        self.con.execute(
                            "SELECT 1 FROM conversation_events "
                            "WHERE run_id=? "
                            "AND event_type='run.interrupt.requested' LIMIT 1",
                            (run["run_id"],),
                        ).fetchone()
                        is None
                    )
        return {
            "attempt_number": int(attempt["attempt_number"]),
            "target_conversation_id": attempt["target_conversation_id"],
            "native_run_ref": attempt["native_run_ref"],
            "attempt_outcome": str(attempt["outcome"]),
            "message_state": str(message["state"]) if message is not None else None,
            "run_state": run_state,
            "error_code": error_code,
            "error_detail": error_detail,
            "interrupt_requested": interrupt_requested,
            "turn_live": turn_live,
        }

    def _resolve_review_expectations_in_transaction(
        self, work_unit_id: int, resolution: str
    ) -> tuple[int, ...]:
        rows = self.con.execute(
            "SELECT e.message_id FROM sprint_liveness_expectations e "
            "JOIN wake_message m ON m.message_id=e.message_id "
            "WHERE m.work_unit_id=? AND m.message_kind='review_request' "
            "AND e.resolved_at IS NULL ORDER BY e.message_id",
            (work_unit_id,),
        ).fetchall()
        message_ids = tuple(int(row[0]) for row in rows)
        if message_ids:
            marks = ",".join("?" for _ in message_ids)
            self.con.execute(
                "UPDATE sprint_liveness_expectations SET resolved_at=datetime('now'),"
                "resolution=?,next_evaluation_at=NULL "
                f"WHERE message_id IN ({marks}) AND resolved_at IS NULL",
                (resolution, *message_ids),
            )
        return message_ids

    def _spec_drift(self, sprint_id: int) -> dict[int, dict[str, str]]:
        drift: dict[int, dict[str, str]] = {}
        rows = self.con.execute(
            "SELECT ss.document_id,ss.bound_revision_sha256,d.body "
            "FROM sprint_specs ss JOIN documents d USING (document_id) "
            "WHERE ss.sprint_id=? ORDER BY ss.document_id",
            (sprint_id,),
        ).fetchall()
        for row in rows:
            current = hashlib.sha256((row["body"] or "").encode()).hexdigest()
            if current != row["bound_revision_sha256"]:
                drift[int(row["document_id"])] = {
                    "bound_revision_sha256": str(row["bound_revision_sha256"]),
                    "current_revision_sha256": current,
                }
        return drift

    def _local_reconciliation_anomalies(self, sprint_id: int) -> tuple[str, ...]:
        anomalies = [
            f"participant {row['participant_id']} ({row['role']}) has no usable capacity"
            for row in self.con.execute(
                "SELECT p.participant_id,p.role FROM sprint_participants p "
                "JOIN shells sh USING (shell_id) WHERE p.sprint_id=? "
                "AND sh.is_deleted<>0 "
                "ORDER BY p.participant_id",
                (sprint_id,),
            )
        ]
        return tuple(anomalies)

    def _reconciliation_evidence(
        self,
        sprint_id: int,
        *,
        trigger: str,
        requeued_wake_ids: Iterable[int],
        projected_work_unit_ids: Iterable[int],
        resolved_review_message_ids: Iterable[int],
        spec_drift: dict[int, dict[str, str]],
        anomalies: Iterable[str],
    ) -> dict:
        return {
            "trigger": trigger,
            "native_runs": [
                dict(row)
                for row in self.con.execute(
                    "SELECT r.run_id,r.state,r.heartbeat_at,r.lease_expires_at,"
                    "EXISTS(SELECT 1 FROM conversation_events e "
                    "WHERE e.run_id=r.run_id "
                    "AND e.event_type='run.interrupt.requested') interrupt_requested "
                    "FROM conversation_runs r "
                    "JOIN sprint_participant_conversations pc "
                    "ON pc.conversation_id=r.conversation_id "
                    "JOIN sprint_participants p "
                    "ON p.participant_id=pc.sprint_participant_id "
                    "WHERE p.sprint_id=? ORDER BY r.run_id",
                    (sprint_id,),
                )
            ],
            "unread_message_ids": [
                int(row[0])
                for row in self.con.execute(
                    "SELECT message_id FROM wake_message WHERE sprint_id=? "
                    "AND read_at IS NULL ORDER BY message_id",
                    (sprint_id,),
                )
            ],
            "pending_wakes": [
                dict(row)
                for row in self.con.execute(
                    "SELECT wake_id,participant_id,state,attempt_count,available_at,"
                    "lease_expires_at FROM sprint_wake_outbox WHERE sprint_id=? "
                    "AND state IN ('pending','delivering') ORDER BY wake_id",
                    (sprint_id,),
                )
            ],
            "requeued_wake_ids": list(requeued_wake_ids),
            "work_units": [
                dict(row)
                for row in self.con.execute(
                    "SELECT work_unit_id,assigned_shell_id,disposition "
                    "FROM sprint_work_units WHERE sprint_id=? ORDER BY work_unit_id",
                    (sprint_id,),
                )
            ],
            "registered_prs": [
                dict(row)
                for row in self.con.execute(
                    "SELECT pr.registered_pr_id,pr.repository,pr.pr_number,"
                    "t.normalized_state,t.observed_head_sha "
                    "FROM sprint_registered_prs pr LEFT JOIN sprint_pr_transitions t "
                    "ON t.transition_id=(SELECT MAX(latest.transition_id) "
                    "FROM sprint_pr_transitions latest "
                    "WHERE latest.registered_pr_id=pr.registered_pr_id) "
                    "WHERE pr.sprint_id=? ORDER BY pr.registered_pr_id",
                    (sprint_id,),
                )
            ],
            "participant_capacity": [
                dict(row)
                for row in self.con.execute(
                    "SELECT p.participant_id,p.shell_id,p.role,p.disposition,"
                    "CASE WHEN sh.is_deleted=0 AND active.chat_id IS NOT NULL "
                    "THEN 1 ELSE 0 END available "
                    "FROM sprint_participants p JOIN shells sh USING (shell_id) "
                    "LEFT JOIN active_shell_chats active ON active.shell_id=p.shell_id "
                    "WHERE p.sprint_id=? ORDER BY p.participant_id",
                    (sprint_id,),
                )
            ],
            "projected_work_unit_ids": list(projected_work_unit_ids),
            "resolved_review_message_ids": list(resolved_review_message_ids),
            "spec_drift": {str(key): value for key, value in spec_drift.items()},
            "anomalies": list(anomalies),
        }

    def _planner_participant_id(self, sprint_id: int) -> int:
        row = self.con.execute(
            "SELECT participant_id FROM sprint_participants "
            "WHERE sprint_id=? AND role='planner'",
            (sprint_id,),
        ).fetchone()
        if row is None:
            raise SprintInvariantError("Sprint has no Planner participant")
        return int(row[0])

    def _queue_planner_notice(
        self,
        sprint_id: int,
        *,
        body: str,
        idempotency_key: str,
    ) -> tuple[int, tuple[str, ...]]:
        participant_id = self._planner_participant_id(sprint_id)
        receiver_shell_id = _participant_shell_id(self.con, participant_id)
        from sprint_message_delivery import SprintMessageStore

        receipt = SprintMessageStore(self.con).send_to_shell_in_transaction(
            receiver_shell_id,
            message_kind="notification",
            body=body,
            declared_type="re-enter",
            idempotency_key=idempotency_key,
        )
        return receipt.message_id, ()

    def _signal_interrupts_and_notifications(
        self, receipt: PauseReceipt | AbortReceipt
    ) -> None:
        self._signal_notifications(receipt.notification_conversation_ids)
        if receipt.changed and not receipt.notification_conversation_ids:
            self.notify_commit()
        for run_id in receipt.interrupt_run_ids:
            try:
                self.interrupt_run(run_id)
            except Exception as exc:  # noqa: BLE001 - intent remains retryable
                with db_driver.write_transaction(
                    self.con, "sprint.pause.interrupt_delivery"
                ):
                    sprint_id = self.con.execute(
                        "SELECT p.sprint_id FROM conversation_runs r "
                        "JOIN sprint_participant_conversations pc "
                        "ON pc.conversation_id=r.conversation_id "
                        "JOIN sprint_participants p "
                        "ON p.participant_id=pc.sprint_participant_id "
                        "WHERE r.run_id=?",
                        (run_id,),
                    ).fetchone()[0]
                    self._event(
                        int(sprint_id),
                        "pause.interrupt_delivery_failed",
                        LifecycleActor("system"),
                        {"run_id": run_id, "error": str(exc)[:2000]},
                    )

    def signal_pause_receipt(self, receipt: PauseReceipt) -> None:
        """Deliver one committed pause receipt to live runtime consumers."""
        if self.con.in_transaction:
            raise RuntimeError("pause signals require a committed transaction")
        self._signal_interrupts_and_notifications(receipt)

    def _signal_notifications(self, conversation_ids: Iterable[str]) -> None:
        for conversation_id in sorted(set(conversation_ids)):
            conversation_events.notify(conversation_id)
        if tuple(conversation_ids):
            self.notify_commit()

    @staticmethod
    def _required_text(value: str, label: str, limit: int) -> str:
        value = value.strip()
        if not value:
            raise ValueError(f"{label} is empty")
        if len(value) > limit:
            raise ValueError(f"{label} exceeds {limit} characters")
        return value

    def _sprint(self, sprint_id: int) -> sqlite3.Row:
        row = self.con.execute(
            "SELECT * FROM sprints WHERE sprint_id=?", (sprint_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown Sprint: {sprint_id}")
        return row

    @staticmethod
    def _require_edge(current: str, target: str) -> None:
        if current not in SPRINT_TRANSITIONS or not transition_allowed(
            current, target
        ):
            allowed = ", ".join(sorted(SPRINT_TRANSITIONS.get(current, ()))) or "none"
            raise SprintStateError(
                f"illegal Sprint lifecycle transition {current} -> {target}; "
                f"allowed: {allowed}"
            )

    def _authorize(
        self,
        sprint: sqlite3.Row,
        target: str,
        actor: LifecycleActor,
    ) -> None:
        current = str(sprint["lifecycle"])
        edge = (current, target)
        if edge == ("prepared", "armed"):
            allowed = {"planner"}
        elif target == "paused":
            allowed = {"planner", "fnb", "participant", "system"}
        elif edge == ("paused", "armed") or target in {"completed", "aborted"}:
            allowed = {"planner", "fnb"}
        else:
            allowed = set()
        if actor.kind not in allowed:
            raise SprintAuthorityError(
                f"{actor.kind} cannot transition Sprint {current} -> {target}"
            )
        if actor.kind == "planner" and actor.shell_id != sprint[
            "originating_planner_shell_id"
        ]:
            raise SprintAuthorityError("only the originating Planner owns this edge")
        if actor.kind == "participant":
            exists = self.con.execute(
                "SELECT 1 FROM sprint_participants "
                "WHERE sprint_id=? AND shell_id=?",
                (sprint["sprint_id"], actor.shell_id),
            ).fetchone()
            if exists is None:
                raise SprintAuthorityError(
                    "only an assigned participant may pause this Sprint"
                )

    def _validate_arm_plan(
        self,
        sprint: sqlite3.Row,
    ) -> tuple[int, list[sqlite3.Row]]:
        sprint_id = int(sprint["sprint_id"])
        if int(sprint["merge_grant_enabled"]) != 1:
            raise SprintInvariantError("arming requires a committed merge grant")
        bound_specs = self.con.execute(
            "SELECT ss.bound_revision_sha256,ss.bound_revision_body,"
            "d.document_id,d.feature_id,d.kind,d.body "
            "FROM sprint_specs ss "
            "LEFT JOIN documents d ON d.document_id=ss.document_id "
            "WHERE ss.sprint_id=?",
            (sprint_id,),
        ).fetchall()
        invalid_specs = any(
            row["document_id"] is None
            or row["kind"] != "spec"
            or row["feature_id"] is None
            or int(row["feature_id"]) != int(sprint["feature_id"])
            or not isinstance(row["body"], str)
            or not row["body"].strip()
            or hashlib.sha256(
                (
                    row["bound_revision_body"]
                    if row["bound_revision_body"] is not None
                    else row["body"]
                ).encode()
            ).hexdigest()
            != row["bound_revision_sha256"]
            for row in bound_specs
        )
        if not bound_specs or invalid_specs:
            raise SprintInvariantError(
                "arming requires at least one exact current governing spec"
            )
        roles = {
            row[0]
            for row in self.con.execute(
                "SELECT DISTINCT role FROM sprint_participants WHERE sprint_id=?",
                (sprint_id,),
            )
        }
        if roles != {"planner", "developer", "reviewer"}:
            raise SprintInvariantError(
                "arming requires Planner, Developer, and Reviewer capacity"
            )
        unavailable = self.con.execute(
            "SELECT COUNT(*) FROM sprint_participants p "
            "JOIN shells sh ON sh.shell_id=p.shell_id "
            "WHERE p.sprint_id=? AND sh.is_deleted<>0",
            (sprint_id,),
        ).fetchone()[0]
        if unavailable:
            raise SprintInvariantError("arming requires active participant shells")
        planner = self.con.execute(
            "SELECT participant_id FROM sprint_participants "
            "WHERE sprint_id=? AND shell_id=? AND role='planner'",
            (sprint_id, sprint["originating_planner_shell_id"]),
        ).fetchone()
        if planner is None:
            raise SprintInvariantError(
                "originating Planner must be an assigned Sprint participant"
            )
        unit_count = self.con.execute(
            "SELECT COUNT(*) FROM sprint_work_units WHERE sprint_id=?",
            (sprint_id,),
        ).fetchone()[0]
        invalid_units = self.con.execute(
            "SELECT COUNT(*) FROM sprint_work_units u "
            "LEFT JOIN sprint_participants d "
            "ON d.sprint_id=u.sprint_id AND d.shell_id=u.assigned_shell_id "
            "AND d.role='developer' "
            "LEFT JOIN sprint_participants r "
            "ON r.sprint_id=u.sprint_id AND r.shell_id=u.reviewer_shell_id "
            "AND r.role='reviewer' "
            "WHERE u.sprint_id=? "
            "AND (d.participant_id IS NULL OR r.participant_id IS NULL)",
            (sprint_id,),
        ).fetchone()[0]
        if unit_count == 0 or invalid_units:
            raise SprintInvariantError(
                "arming requires routed work units with assigned reviewers"
            )
        selections = self.con.execute(
            "SELECT p.participant_id,p.role,p.harness,p.model,p.effort,p.route,"
            "sh.shortname FROM sprint_participants p "
            "JOIN shells sh ON sh.shell_id=p.shell_id "
            "WHERE p.sprint_id=? ORDER BY "
            "CASE p.role WHEN 'planner' THEN 0 WHEN 'developer' THEN 1 ELSE 2 END,"
            "p.participant_id",
            (sprint_id,),
        ).fetchall()
        invalid_selections = [
            row
            for row in selections
            if not str(row["harness"]).strip()
            or (row["model"] is not None and not str(row["model"]).strip())
            or (row["effort"] is not None and not str(row["effort"]).strip())
        ]
        if invalid_selections:
            raise SprintInvariantError(
                "arming requires recorded model selections for every participant"
            )
        if _route_binding_schema_available(self.con):
            if any(
                row["active_route_binding_id"] is not None
                for row in self.con.execute(
                    "SELECT active_route_binding_id FROM sprint_participants "
                    "WHERE sprint_id=?",
                    (sprint_id,),
                )
            ):
                raise SprintInvariantError(
                    "prepared Sprint participants must remain unbound until arm"
                )
            if self.con.execute(
                "SELECT 1 FROM sprint_participant_route_bindings binding "
                "JOIN sprint_participants participant "
                "ON participant.participant_id=binding.participant_id "
                "WHERE participant.sprint_id=? LIMIT 1",
                (sprint_id,),
            ).fetchone() is not None:
                raise SprintInvariantError(
                    "prepared Sprint cannot contain participant route binding history"
                )
        return int(planner[0]), selections

    def _eligible_conformance_reviewers(self, sprint_id: int) -> tuple[int, ...]:
        return tuple(
            int(row[0])
            for row in self.con.execute(
                "SELECT participant.shell_id FROM sprint_participants participant "
                "JOIN shells shell ON shell.shell_id=participant.shell_id "
                "WHERE participant.sprint_id=? AND participant.role='reviewer' "
                "AND participant.disposition<>'declined' "
                "AND COALESCE(shell.is_deleted,0)=0 ORDER BY participant.shell_id",
                (sprint_id,),
            )
        )

    def _resolve_conformance_owner(
        self,
        sprint_id: int,
        requested_shell_id: int | None,
        *,
        allow_single_reviewer_default: bool,
    ) -> int:
        eligible = self._eligible_conformance_reviewers(sprint_id)
        if requested_shell_id is None and allow_single_reviewer_default and len(eligible) == 1:
            return eligible[0]
        if requested_shell_id is None:
            raise SprintPreflightError("arming requires a selected conformance Reviewer")
        if requested_shell_id not in eligible:
            raise SprintPreflightError(
                f"shell {requested_shell_id} is not an active Reviewer participant"
            )
        return requested_shell_id

    def _resolve_resume_conformance_owner(
        self,
        sprint: sqlite3.Row,
        requested_shell_id: int | None,
        *,
        reason: str | None,
    ) -> int:
        sprint_id = int(sprint["sprint_id"])
        eligible = self._eligible_conformance_reviewers(sprint_id)
        current = sprint["conformance_reviewer_shell_id"]
        current_owner = int(current) if current is not None else None
        if requested_shell_id is None and current_owner in eligible:
            return current_owner
        if requested_shell_id is None:
            command = (
                f"sc sprint resume --sprint {sprint_id} "
                "--conformance-reviewer-shell <shell_id> "
                '--reason "replace closeout owner"'
            )
            raise SprintConflictError(
                "Sprint has no eligible conformance owner; select one while paused",
                {
                    "code": "conformance_owner_required",
                    "eligible_reviewer_shell_ids": list(eligible),
                    "recovery_command": command,
                },
            )
        if requested_shell_id not in eligible:
            raise SprintPreflightError(
                f"shell {requested_shell_id} is not an active Reviewer participant"
            )
        if current_owner is not None and requested_shell_id != current_owner:
            self._required_text(reason or "", "ownership replacement reason", 2000)
        return requested_shell_id

    def _set_conformance_owner(
        self,
        sprint: sqlite3.Row,
        shell_id: int,
        actor: LifecycleActor,
        *,
        reason: str,
    ) -> bool:
        current = sprint["conformance_reviewer_shell_id"]
        previous = int(current) if current is not None else None
        if previous == shell_id:
            return False
        generation = int(sprint["conformance_owner_generation"]) + 1
        self.con.execute(
            "UPDATE sprints SET conformance_reviewer_shell_id=?,"
            "conformance_owner_generation=?,updated_at=datetime('now'),"
            "version=version WHERE sprint_id=?",
            (shell_id, generation, int(sprint["sprint_id"])),
        )
        self._event(
            int(sprint["sprint_id"]),
            "conformance_owner.changed",
            actor,
            {
                "previous_shell_id": previous,
                "new_shell_id": shell_id,
                "generation": generation,
                "reason": reason,
            },
        )
        return True

    def _assert_terminal_work_in_transaction(self, sprint_id: int) -> None:
        if not self.con.in_transaction:
            raise RuntimeError("Sprint terminality requires an active transaction")
        total = int(
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_work_units WHERE sprint_id=?",
                (sprint_id,),
            ).fetchone()[0]
        )
        rows = self.con.execute(
            "SELECT work_unit_id,disposition FROM sprint_work_units "
            "WHERE sprint_id=? AND disposition NOT IN ('completed','cancelled') "
            "ORDER BY work_unit_id LIMIT 50",
            (sprint_id,),
        ).fetchall()
        if total and not rows:
            return
        nonterminal_total = int(
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_work_units WHERE sprint_id=? "
                "AND disposition NOT IN ('completed','cancelled')",
                (sprint_id,),
            ).fetchone()[0]
        )
        details = {
            "code": "sprint_work_nonterminal" if total else "sprint_work_missing",
            "work_unit_count": total,
            "nonterminal_count": nonterminal_total,
            "nonterminal_work_units": [
                {
                    "work_unit_id": int(row["work_unit_id"]),
                    "disposition": str(row["disposition"]),
                }
                for row in rows
            ],
            "truncated": nonterminal_total > len(rows),
        }
        raise SprintConflictError(
            "successful Sprint completion requires at least one work unit and "
            "every work unit completed or cancelled",
            details,
        )

    def _queue_arming_planner_wake(
        self,
        sprint_id: int,
        *,
        planner_participant_id: int,
        selections: list[sqlite3.Row],
    ) -> int:
        lines = [
            f"Sprint {sprint_id} armed. Bound participant routes:"
        ]
        bindings_available = _route_binding_schema_available(self.con)
        for selection in selections:
            binding = None
            if bindings_available:
                binding = self.con.execute(
                    "SELECT binding.control_state,binding.requested_model,"
                    "binding.effective_effort,binding.route_revision "
                    "FROM sprint_participants participant "
                    "JOIN sprint_participant_route_bindings binding "
                    "ON binding.binding_id=participant.active_route_binding_id "
                    "WHERE participant.participant_id=?",
                    (selection["participant_id"],),
                ).fetchone()
            if binding is not None:
                model = binding["requested_model"] or "Harness default"
                effort = binding["effective_effort"] or "uncontrolled"
                lines.append(
                    f"- {selection['role']} {selection['shortname']}: "
                    f"{selection['harness']} · model={model} · "
                    f"Thinking level={effort} · {binding['control_state']} · "
                    f"route revision {binding['route_revision']}"
                )
                continue
            model = selection["model"] or "default"
            effort = selection["effort"] or "default"
            lines.append(
                f"- {selection['role']} {selection['shortname']}: "
                f"{selection['harness']} · model={model} · effort={effort}"
            )
        from sprint_message_delivery import SprintMessageStore

        receipt = SprintMessageStore(self.con).send_in_transaction(
            sprint_id,
            to_participant_id=planner_participant_id,
            message_kind="notification",
            body="\n".join(lines),
            actionable=False,
            declared_type="new",
            idempotency_key=f"sprint:{sprint_id}:arming-model-selections",
        )
        if receipt.wake_id is None:
            raise SprintInvariantError("Planner arming message has no wake")
        return receipt.wake_id

    def _release_initial_work(
        self,
        sprint_id: int,
        *,
        planner_participant_id: int,
    ) -> list[int]:
        return SprintWorkUnitStore(self.con)._dispatch_ready_locked(
            sprint_id,
            planner_participant_id=planner_participant_id,
        )

    def _close_completed_participant_chats_in_transaction(
        self,
        sprint_id: int,
        retained_reviewer_shell_id: int | None = None,
    ) -> tuple[str, ...]:
        """Close active chats immutably linked to a successfully closed Sprint."""
        if not self.con.in_transaction:
            raise RuntimeError("Sprint chat cleanup requires an active transaction")
        sprint = self.con.execute(
            "SELECT originating_planner_shell_id FROM sprints WHERE sprint_id=?",
            (sprint_id,),
        ).fetchone()
        if sprint is None:
            raise KeyError(f"unknown Sprint: {sprint_id}")
        planner_shell_id = int(sprint["originating_planner_shell_id"])
        planner = self.con.execute(
            "SELECT 1 FROM sprint_participants WHERE sprint_id=? AND shell_id=? "
            "AND role='planner'",
            (sprint_id, planner_shell_id),
        ).fetchone()
        if planner is None:
            raise SprintInvariantError(
                "Sprint has no originating Planner participant"
            )

        retained_shell_ids = {planner_shell_id}
        if retained_reviewer_shell_id is not None:
            reviewer = self.con.execute(
                "SELECT 1 FROM sprint_participants WHERE sprint_id=? AND shell_id=? "
                "AND role='reviewer'",
                (sprint_id, retained_reviewer_shell_id),
            ).fetchone()
            if reviewer is None:
                raise SprintAuthorityError(
                    "retained report author must be a participating Reviewer"
                )
            retained_shell_ids.add(retained_reviewer_shell_id)
        else:
            final_report_authors = self.con.execute(
                "SELECT DISTINCT report.author_shell_id FROM sprint_reports report "
                "JOIN sprint_participants participant "
                "ON participant.sprint_id=report.sprint_id "
                "AND participant.shell_id=report.author_shell_id "
                "AND participant.role='reviewer' "
                "WHERE report.sprint_id=? AND report.report_kind='final' "
                "ORDER BY report.author_shell_id",
                (sprint_id,),
            ).fetchall()
            if len(final_report_authors) == 1:
                retained_shell_ids.add(
                    int(final_report_authors[0]["author_shell_id"])
                )

        retained = sorted(retained_shell_ids)
        linked_active_chats = self.con.execute(
            "SELECT DISTINCT participant.participant_id,participant.shell_id,"
            "active.chat_id FROM sprint_participants participant "
            "JOIN sprint_participant_conversations link "
            "ON link.sprint_participant_id=participant.participant_id "
            "JOIN active_shell_chats active "
            "ON active.shell_id=participant.shell_id "
            "AND active.chat_id=link.conversation_id "
            "WHERE participant.sprint_id=? "
            "ORDER BY participant.participant_id",
            (sprint_id,),
        ).fetchall()
        closed_conversation_ids: list[str] = []
        for linked in linked_active_chats:
            shell_id = int(linked["shell_id"])
            if shell_id in retained_shell_ids:
                continue
            expected_chat_id = str(linked["chat_id"])
            closed = active_chat_registry.close_for_displacement(
                self.con,
                shell_id,
                allow_live_process=True,
            )
            if closed is None or closed.chat_id != expected_chat_id:
                raise SprintInvariantError(
                    f"Sprint-linked active chat changed while closing: "
                    f"{expected_chat_id}"
                )
            sprint_participant_chats._append_event(
                self.con,
                closed.chat_id,
                "conversation.closed",
                {
                    "reason": "sprint_completed",
                    "state": "closed",
                    "sprint_id": sprint_id,
                    "retained_shell_ids": retained,
                },
            )
            closed_conversation_ids.append(closed.chat_id)
        return tuple(closed_conversation_ids)

    def _update_lifecycle(
        self,
        sprint_id: int,
        *,
        current: str,
        target: str,
        outcome: str | None,
    ) -> None:
        timestamp_column = {
            "armed": "armed_at",
            "paused": "paused_at",
            "completed": "completed_at",
            "aborted": "aborted_at",
        }[target]
        result = self.con.execute(
            f"UPDATE sprints SET lifecycle=?,terminal_outcome=?,"
            f"{timestamp_column}=datetime('now'),updated_at=datetime('now'),"
            "version=version+1 WHERE sprint_id=? AND lifecycle=?",
            (target, outcome, sprint_id, current),
        )
        if result.rowcount != 1:
            raise SprintStateError("Sprint lifecycle changed concurrently")
        if target in {"completed", "aborted"}:
            self.con.execute(
                "UPDATE sprint_liveness_expectations "
                "SET resolved_at=datetime('now'),resolution=?,"
                "next_evaluation_at=NULL "
                "WHERE sprint_id=? AND resolved_at IS NULL",
                (f"sprint.{target}", sprint_id),
            )

    def _clear_coordinate_mode(
        self,
        sprint_id: int,
        actor: LifecycleActor,
        *,
        reason: str,
    ) -> bool:
        """Clear an operator-selected mode only for an FnB lifecycle action."""
        if actor.kind != "fnb":
            return False
        changed = self.con.execute(
            "UPDATE sprints SET coordinate_mode=0,updated_at=datetime('now'),"
            "version=version+1 WHERE sprint_id=? AND coordinate_mode=1",
            (sprint_id,),
        ).rowcount
        if changed:
            self._event(
                sprint_id,
                "coordinate_mode.cleared",
                actor,
                {"reason": reason},
            )
        return bool(changed)

    def _event(
        self,
        sprint_id: int,
        event_type: str,
        actor: LifecycleActor,
        payload: dict,
    ) -> int:
        return int(
            self.con.execute(
                "INSERT INTO sprint_events "
                "(sprint_id,event_type,actor_kind,actor_shell_id,payload) "
                "VALUES (?,?,?,?,?)",
                (
                    sprint_id,
                    event_type,
                    actor.kind,
                    actor.shell_id,
                    json.dumps(payload, sort_keys=True),
                ),
            ).lastrowid
        )


class SprintParticipantStore:
    """Planner-owned participant route selection for future Sprint wakes."""

    def __init__(
        self,
        con: sqlite3.Connection,
        *,
        probe_harness: Callable[[str], ProbeResult] | None = None,
    ) -> None:
        self.con = con
        self.con.row_factory = sqlite3.Row
        self.probe_harness = probe_harness or (
            lambda harness: adapter_for(harness).probe()
        )

    def _probe_bound_harness(self, harness: str) -> None:
        if sprint_participant_chats.run_mod.load_adapter(harness).get("conversation"):
            try:
                sprint_participant_chats.require_sprint_harness(harness)
            except sprint_participant_chats.SprintConversationError as exc:
                raise SprintPreflightError(str(exc)) from exc
        try:
            self.probe_harness(harness)
        except AdapterError as exc:
            if exc.code != "HARNESS_VERSION_UNSUPPORTED":
                raise SprintPreflightError(str(exc)) from exc

    def reroute(
        self,
        sprint_id: int,
        planner_shell_id: int,
        *,
        participant_shell_id: int,
        harness: str,
        model: str | None,
        effort: str | None,
        route: str | None = None,
    ) -> ParticipantRerouteReceipt:
        """Validate and replace one idle Developer/Reviewer launch selection."""
        try:
            harness = route_bindings.normalize_harness(harness)
        except route_bindings.RouteResolutionError as exc:
            raise SprintPreflightError(
                exc.message, code=exc.code, details=exc.details
            ) from exc
        if model is not None:
            if not isinstance(model, str) or not model or model != model.strip():
                raise ValueError(
                    "participant model must be an exact non-empty selector"
                )
        if effort is not None:
            if not isinstance(effort, str) or not effort.strip():
                raise ValueError("participant effort must be non-empty when supplied")
        if route is not None:
            route = route.strip()
            if not route:
                raise ValueError("participant display route must be non-empty")

        lifecycle, _ = SprintWorkUnitStore(self.con)._require_planner(
            sprint_id, planner_shell_id
        )
        if lifecycle not in {"prepared", "paused"}:
            raise SprintInvariantError(
                "participant routes may change only while a Sprint is prepared or paused"
            )
        participant = self._participant(sprint_id, participant_shell_id)
        if participant["role"] not in {"developer", "reviewer"}:
            raise SprintInvariantError(
                "only Developer or Reviewer participant routes may be changed"
            )
        proposed = {
            "participant_id": int(participant["participant_id"]),
            "harness": harness,
            "model": model,
            "effort": effort,
        }
        try:
            candidate = _participant_binding_candidate(self.con, proposed)
        except route_bindings.RouteResolutionError as exc:
            raise SprintPreflightError(
                exc.message, code=exc.code, details=exc.details
            ) from exc
        self._probe_bound_harness(candidate.binding["harness"])

        before = self._projection(participant)
        after = {
            "harness": candidate.binding["harness"],
            "model": candidate.binding["requested_model"],
            "effort": (
                candidate.binding["requested_effort"]
                if effort is not None else None
            ),
            "route": route if route is not None else participant["route"],
        }
        if before == after:
            active = self._active_binding(participant)
            return ParticipantRerouteReceipt(
                changed=False,
                binding_status=(
                    "unbound-intent" if lifecycle == "prepared"
                    else ("bound" if active is not None else "legacy")
                ),
                control_state=(
                    str(active["control_state"])
                    if active is not None else candidate.binding["control_state"]
                ),
                route_revision=(
                    int(active["route_revision"]) if active is not None else None
                ),
                binding_digest=(
                    str(active["binding_digest"]) if active is not None else None
                ),
                harness_version=(
                    str(active["harness_version"])
                    if active is not None else candidate.harness_version
                ),
                harness_support_state=(
                    str(active["harness_support_state"])
                    if active is not None else candidate.harness_support_state
                ),
            )

        binding_receipt = None
        with db_driver.write_transaction(self.con, "sprint.participant.reroute"):
            lifecycle, _ = SprintWorkUnitStore(self.con)._require_planner(
                sprint_id, planner_shell_id
            )
            if lifecycle not in {"prepared", "paused"}:
                raise SprintInvariantError(
                    "participant routes may change only while a Sprint is prepared or paused"
                )
            current = self._participant(sprint_id, participant_shell_id)
            if (
                self._projection(current) != before
                or current["active_route_binding_id"]
                != participant["active_route_binding_id"]
            ):
                raise SprintInvariantError(
                    "participant route changed during preflight; retry reroute"
                )
            expectation_facts = self._require_idle_projection(
                current, lifecycle, planner_shell_id=planner_shell_id
            )
            if lifecycle == "paused":
                expected = candidate.evidence_snapshot
                if expected is not None:
                    current_evidence = self.con.execute(
                        "SELECT * FROM model_routes WHERE harness=? AND selector=?",
                        (
                            candidate.binding["harness"],
                            candidate.binding["requested_model"],
                        ),
                    ).fetchone()
                    if (
                        current_evidence is None
                        or _route_evidence_snapshot(current_evidence) != expected
                    ):
                        raise SprintInvariantError(
                            "participant route evidence changed during reroute "
                            "preflight; retry reroute"
                        )
                binding_receipt = route_bindings.ParticipantRouteBindingStore(
                    self.con
                ).bind(
                    int(current["participant_id"]),
                    candidate.binding,
                    candidate.binding_digest,
                    transition="reroute",
                    runtime_status=candidate.runtime_status,
                    runtime_scope=candidate.runtime_scope,
                    source_fingerprint=candidate.source_fingerprint,
                    harness_version=candidate.harness_version,
                    harness_support_state=candidate.harness_support_state,
                )
            self.con.execute(
                "UPDATE sprint_participants SET harness=?,model=?,effort=?,route=?,"
                "updated_at=datetime('now') WHERE participant_id=?",
                (
                    after["harness"],
                    after["model"],
                    after["effort"],
                    after["route"],
                    current["participant_id"],
                ),
            )
            self.con.execute(
                "INSERT INTO sprint_events "
                "(sprint_id,event_type,actor_kind,actor_shell_id,payload) "
                "VALUES (?,'participant.route_changed','planner',?,?)",
                (
                    sprint_id,
                    planner_shell_id,
                    json.dumps(
                        {
                            "participant_id": int(current["participant_id"]),
                            "shell_id": participant_shell_id,
                            "role": str(current["role"]),
                            "before": before,
                            "retired_expectations": expectation_facts,
                            "after": {
                                **after,
                                "binding_status": (
                                    "bound" if binding_receipt is not None
                                    else "unbound-intent"
                                ),
                                "control_state": candidate.binding["control_state"],
                                "route_revision": (
                                    binding_receipt["route_revision"]
                                    if binding_receipt is not None else None
                                ),
                                "binding_digest": (
                                    candidate.binding_digest
                                    if binding_receipt is not None else None
                                ),
                                "harness_version": candidate.harness_version,
                                "harness_support_state": (
                                    candidate.harness_support_state
                                ),
                            },
                        },
                        sort_keys=True,
                    ),
                ),
            )
            if binding_receipt is not None:
                self.con.execute(
                    "INSERT INTO sprint_events "
                    "(sprint_id,event_type,actor_kind,actor_shell_id,payload) "
                    "VALUES (?,'participant.route_revised','planner',?,?)",
                    (
                        sprint_id,
                        planner_shell_id,
                        json.dumps(
                            {
                                **binding_receipt,
                                "control_state": candidate.binding["control_state"],
                                "before_binding_digest": (
                                    str(participant["binding_digest"])
                                    if participant["binding_digest"] is not None
                                    else None
                                ),
                            },
                            sort_keys=True,
                        ),
                    ),
                )
        return ParticipantRerouteReceipt(
            changed=True,
            binding_status=(
                "bound" if binding_receipt is not None else "unbound-intent"
            ),
            control_state=candidate.binding["control_state"],
            route_revision=(
                binding_receipt["route_revision"]
                if binding_receipt is not None else None
            ),
            binding_digest=(
                candidate.binding_digest if binding_receipt is not None else None
            ),
            harness_version=candidate.harness_version,
            harness_support_state=candidate.harness_support_state,
        )

    def _participant(self, sprint_id: int, shell_id: int) -> sqlite3.Row:
        row = self.con.execute(
            "SELECT participant.participant_id,participant.sprint_id,"
            "participant.shell_id,participant.role,participant.harness,"
            "participant.model,participant.effort,participant.route,"
            "participant.active_route_binding_id,binding.control_state,"
            "binding.route_revision,binding.binding_digest,binding.harness_version,"
            "binding.harness_support_state "
            "FROM sprint_participants participant "
            "LEFT JOIN sprint_participant_route_bindings binding "
            "ON binding.binding_id=participant.active_route_binding_id "
            "WHERE participant.sprint_id=? AND participant.shell_id=?",
            (sprint_id, shell_id),
        ).fetchone()
        if row is None:
            raise SprintInvariantError(
                f"shell {shell_id} is not a participant in this Sprint"
            )
        return row

    @staticmethod
    def _active_binding(participant: sqlite3.Row) -> sqlite3.Row | None:
        return (
            participant
            if participant["active_route_binding_id"] is not None
            else None
        )

    def _require_idle_projection(
        self,
        participant: sqlite3.Row,
        lifecycle: str,
        *,
        planner_shell_id: int,
    ) -> list[dict]:
        """Guard the route change and relax it for own-route expectations.

        A paused Sprint no longer refuses reroute solely because the
        participant owns released PR-bound units (spec #161): when every
        blocking open expectation belongs to the participant's own route, the
        reroute transaction retires it and queues a fresh one on the
        replacement route (delivered at resume).  Expectations owned by
        another seat — a released ``ready``/``in_review`` lane for a
        Developer — still block, as do live wakes, runs, and chats.
        """
        if lifecycle == "prepared":
            return []
        sprint_id = int(participant["sprint_id"])
        shell_id = int(participant["shell_id"])
        if participant["role"] == "developer":
            rows = self.con.execute(
                "SELECT work_unit_id,disposition,title,expected_output "
                "FROM sprint_work_units "
                "WHERE sprint_id=? AND assigned_shell_id=? AND disposition IN "
                "('ready','active','in_review','fixing','merge_ready') "
                "ORDER BY work_unit_id",
                (sprint_id, shell_id),
            ).fetchall()
            relaxable = {"active", "fixing", "merge_ready"}
        else:
            rows = self.con.execute(
                "SELECT work_unit_id,disposition,title,expected_output "
                "FROM sprint_work_units "
                "WHERE sprint_id=? AND reviewer_shell_id=? "
                "AND disposition='in_review' ORDER BY work_unit_id",
                (sprint_id, shell_id),
            ).fetchall()
            relaxable = {"in_review"}
        for row in rows:
            if str(row["disposition"]) not in relaxable:
                raise SprintInvariantError(
                    f"participant route still owns released work unit "
                    f"{int(row['work_unit_id'])} ({row['disposition']}); "
                    "recall or finish that expectation first"
                )
        wake = self.con.execute(
            "SELECT wake_id,state FROM sprint_wake_outbox "
            "WHERE participant_id=? AND state IN ('pending','delivering') "
            "ORDER BY wake_id LIMIT 1",
            (participant["participant_id"],),
        ).fetchone()
        if wake is not None:
            raise SprintInvariantError(
                f"participant route still owns wake {int(wake['wake_id'])} "
                f"({wake['state']}); finish delivery before reroute"
            )
        run = self.con.execute(
            "SELECT run.run_id,run.state FROM sprint_participant_conversations link "
            "JOIN conversation_runs run "
            "ON run.conversation_id=link.conversation_id "
            "WHERE link.sprint_participant_id=? "
            "AND run.state IN ('leased','starting','running') "
            "ORDER BY run.run_id LIMIT 1",
            (participant["participant_id"],),
        ).fetchone()
        if run is not None:
            raise SprintInvariantError(
                f"participant route still owns run {int(run['run_id'])} "
                f"({run['state']}); finish the run before reroute"
            )
        chat = active_chat_registry.get(self.con, shell_id)
        if chat is not None:
            raise SprintInvariantError(
                f"participant route still owns active chat {chat.chat_id}; "
                "close and unregister it before reroute"
            )
        if not rows:
            return []
        return self._retire_own_route_expectations(
            participant,
            rows,
            planner_shell_id=planner_shell_id,
        )

    def _retire_own_route_expectations(
        self,
        participant: sqlite3.Row,
        rows: list[sqlite3.Row],
        *,
        planner_shell_id: int,
    ) -> list[dict]:
        """Retire each own-route expectation and queue its replacement.

        Units, dispositions, PR links, and review history are not rewritten;
        the retired generation is declined/resolved, never erased.  Fresh
        expectations are durable immediately but deliver only once the Sprint
        is armed again (wake delivery gates on the armed lifecycle).
        """
        sprint_id = int(participant["sprint_id"])
        participant_id = int(participant["participant_id"])
        planner = self.con.execute(
            "SELECT participant_id FROM sprint_participants "
            "WHERE sprint_id=? AND role='planner'",
            (sprint_id,),
        ).fetchone()
        if planner is None:
            raise SprintInvariantError("Sprint has no Planner participant")
        planner_participant_id = int(planner["participant_id"])

        from sprint_liveness import SprintLivenessMonitor
        from sprint_message_delivery import SprintMessageStore

        messages = SprintMessageStore(self.con)
        monitor = SprintLivenessMonitor(self.con)
        facts: list[dict] = []
        for row in rows:
            unit_id = int(row["work_unit_id"])
            disposition = str(row["disposition"])
            if participant["role"] == "developer":
                if disposition == "merge_ready":
                    # The lane awaits the Developer's own merge authorization;
                    # no message expectation is bound to the old route.
                    facts.append(
                        {"work_unit_id": unit_id, "disposition": disposition}
                    )
                    continue
                retired = messages.retire_route_expectations_in_transaction(
                    sprint_id,
                    unit_id,
                    retirement_reason=(
                        "Rerouted by Planner: assignment moved to the "
                        "replacement route"
                    ),
                    resolution="assignment retired by Planner reroute",
                    kinds=("work_assignment",),
                )
                receipt = messages.send_in_transaction(
                    sprint_id,
                    from_participant_id=planner_participant_id,
                    to_participant_id=participant_id,
                    work_unit_id=unit_id,
                    message_kind="work_assignment",
                    body=f"{row['title']}\n\n{row['expected_output']}",
                    actionable=True,
                    declared_type="force-new",
                    idempotency_key=_next_assignment_key(
                        self.con, sprint_id, unit_id
                    ),
                )
                facts.append(
                    {
                        "work_unit_id": unit_id,
                        "disposition": disposition,
                        "retired_message_ids": list(retired),
                        "fresh_message_id": receipt.message_id,
                    }
                )
                continue
            request = self.con.execute(
                "SELECT message_id,body,from_participant_id FROM wake_message "
                "WHERE sprint_id=? AND work_unit_id=? AND to_participant_id=? "
                "AND message_kind='review_request' "
                "AND disposition IN ('pending','accepted') "
                "ORDER BY message_id DESC LIMIT 1",
                (sprint_id, unit_id, participant_id),
            ).fetchone()
            if request is None:
                raise SprintInvariantError(
                    f"participant route still owns released work unit "
                    f"{unit_id} ({disposition}); recall or finish that "
                    "expectation first"
                )
            request_id = int(request["message_id"])
            evidence = self.con.execute(
                "SELECT payload FROM sprint_events WHERE sprint_id=? "
                "AND event_type='review.requested' "
                "AND json_extract(payload,'$.message_id')=? "
                "ORDER BY event_id DESC LIMIT 1",
                (sprint_id, request_id),
            ).fetchone()
            if evidence is None:
                raise SprintInvariantError(
                    "open review request has no head evidence"
                )
            requested = json.loads(evidence["payload"])
            messages.supersede_actionable_in_transaction(
                request_id,
                "retired by Planner reroute; a fresh review request follows "
                "on the replacement route",
            )
            monitor.resolve_in_transaction(
                request_id,
                "review request retired by Planner reroute",
            )
            receipt = messages.send_in_transaction(
                sprint_id,
                from_participant_id=int(request["from_participant_id"]),
                to_participant_id=participant_id,
                work_unit_id=unit_id,
                message_kind="review_request",
                body=str(request["body"]),
                actionable=True,
                declared_type="force-new",
                idempotency_key=(
                    f"sprint:{sprint_id}:work-unit:{unit_id}:"
                    f"review-request:reroute:{request_id}"
                ),
            )
            self.con.execute(
                "INSERT INTO sprint_events "
                "(sprint_id,event_type,actor_kind,actor_shell_id,payload) "
                "VALUES (?,'review.requested','planner',?,?)",
                (
                    sprint_id,
                    planner_shell_id,
                    json.dumps(
                        {
                            "head_sha": requested.get("head_sha"),
                            "message_id": receipt.message_id,
                            "registered_pr_id": requested.get(
                                "registered_pr_id"
                            ),
                            "source": "planner.reroute",
                            "retired_message_id": request_id,
                            "transition_id": requested.get("transition_id"),
                            "work_unit_id": unit_id,
                        },
                        sort_keys=True,
                    ),
                ),
            )
            facts.append(
                {
                    "work_unit_id": unit_id,
                    "disposition": disposition,
                    "retired_message_ids": [request_id],
                    "fresh_message_id": receipt.message_id,
                }
            )
        return facts

    @staticmethod
    def _projection(participant: sqlite3.Row) -> dict:
        return {
            "harness": str(participant["harness"]),
            "model": participant["model"],
            "effort": participant["effort"],
            "route": participant["route"],
        }


class SprintWorkUnitStore:
    """Planner-owned work-unit planning and dependency-aware dispatch."""

    def __init__(
        self,
        con: sqlite3.Connection,
        *,
        lifecycle_store: SprintLifecycleStore | None = None,
    ) -> None:
        self.con = con
        self.con.row_factory = sqlite3.Row
        self.lifecycle = lifecycle_store or SprintLifecycleStore(con)

    def create(
        self,
        sprint_id: int,
        planner_shell_id: int,
        *,
        assigned_shell_id: int,
        reviewer_shell_id: int,
        title: str,
        expected_output: str,
        task_ids: Iterable[int],
        planned_wave: int = 0,
        dependency_ids: Iterable[int] = (),
        output_kind: str = "code",
    ) -> int:
        """Create one planned editing lane from existing governing-spec tasks."""
        title = title.strip()
        expected_output = expected_output.strip()
        tasks = tuple(dict.fromkeys(int(task_id) for task_id in task_ids))
        dependencies = tuple(
            dict.fromkeys(int(unit_id) for unit_id in dependency_ids)
        )
        if not title or not expected_output:
            raise ValueError("work unit title and expected output are required")
        if not tasks:
            raise SprintInvariantError("work units require at least one spec task")
        if planned_wave < 0:
            raise ValueError("planned wave must be non-negative")
        if output_kind not in WORK_UNIT_OUTPUT_KINDS:
            raise ValueError("work-unit output kind must be code, report_only, or no_code")

        with db_driver.write_transaction(self.con, "sprint.work_unit.create"):
            lifecycle, _ = self._require_planner(sprint_id, planner_shell_id)
            if lifecycle in {"completed", "aborted"}:
                raise SprintInvariantError("terminal Sprints reject new work units")
            self._require_participant(sprint_id, assigned_shell_id, "developer")
            self._require_participant(sprint_id, reviewer_shell_id, "reviewer")
            self._require_tasks(sprint_id, tasks)
            unit_id = int(
                self.con.execute(
                    "INSERT INTO sprint_work_units "
                    "(sprint_id,assigned_shell_id,reviewer_shell_id,title,"
                    "expected_output,planned_wave,output_kind) VALUES (?,?,?,?,?,?,?)",
                    (
                        sprint_id,
                        assigned_shell_id,
                        reviewer_shell_id,
                        title,
                        expected_output,
                        planned_wave,
                        output_kind,
                    ),
                ).lastrowid
            )
            self.con.executemany(
                "INSERT INTO sprint_work_unit_tasks "
                "(sprint_id,work_unit_id,task_id) VALUES (?,?,?)",
                ((sprint_id, unit_id, task_id) for task_id in tasks),
            )
            self._replace_dependencies(sprint_id, unit_id, dependencies)
            self._event(
                sprint_id,
                "work_unit.created",
                planner_shell_id,
                {
                    "work_unit_id": unit_id,
                    "assigned_shell_id": assigned_shell_id,
                    "reviewer_shell_id": reviewer_shell_id,
                    "planned_wave": planned_wave,
                    "output_kind": output_kind,
                    "task_ids": list(tasks),
                    "dependency_ids": list(dependencies),
                },
            )
        return unit_id

    def replan(
        self,
        sprint_id: int,
        work_unit_id: int,
        planner_shell_id: int,
        *,
        assigned_shell_id: int | None = None,
        reviewer_shell_id: int | None = None,
        title: str | None = None,
        expected_output: str | None = None,
        task_ids: Iterable[int] | None = None,
        planned_wave: int | None = None,
        dependency_ids: Iterable[int] | None = None,
        output_kind: str | None = None,
    ) -> bool:
        """Replace the editable plan projection and append its before/after fact."""
        if planned_wave is not None and planned_wave < 0:
            raise ValueError("planned wave must be non-negative")
        with db_driver.write_transaction(self.con, "sprint.work_unit.replan"):
            self._require_planner(sprint_id, planner_shell_id)
            unit = self._unit(sprint_id, work_unit_id)
            if unit["disposition"] != "planned":
                raise SprintInvariantError(
                    "only planned work units may be replanned; pause and recall "
                    "released work first"
                )
            before = self._plan_projection(unit)
            assigned_shell_id = (
                int(unit["assigned_shell_id"])
                if assigned_shell_id is None
                else assigned_shell_id
            )
            reviewer_shell_id = (
                int(unit["reviewer_shell_id"])
                if reviewer_shell_id is None
                else reviewer_shell_id
            )
            title = str(unit["title"]) if title is None else title.strip()
            expected_output = (
                str(unit["expected_output"])
                if expected_output is None
                else expected_output.strip()
            )
            if not title or not expected_output:
                raise ValueError("work unit title and expected output are required")
            tasks = (
                tuple(before["task_ids"])
                if task_ids is None
                else tuple(dict.fromkeys(int(task_id) for task_id in task_ids))
            )
            if task_ids is not None and not tasks:
                raise SprintInvariantError("work units require at least one spec task")
            dependencies = (
                tuple(before["dependency_ids"])
                if dependency_ids is None
                else tuple(
                    dict.fromkeys(int(unit_id) for unit_id in dependency_ids)
                )
            )
            planned_wave = (
                int(unit["planned_wave"])
                if planned_wave is None
                else planned_wave
            )
            self._require_participant(sprint_id, assigned_shell_id, "developer")
            self._require_participant(sprint_id, reviewer_shell_id, "reviewer")
            self._require_tasks(sprint_id, tasks)
            if output_kind is None:
                output_kind = str(unit["output_kind"])
            if output_kind not in WORK_UNIT_OUTPUT_KINDS:
                raise ValueError(
                    "work-unit output kind must be code, report_only, or no_code"
                )
            after = {
                "assigned_shell_id": assigned_shell_id,
                "reviewer_shell_id": reviewer_shell_id,
                "title": title,
                "expected_output": expected_output,
                "task_ids": sorted(tasks),
                "planned_wave": planned_wave,
                "output_kind": output_kind,
                "dependency_ids": sorted(dependencies),
            }
            if before == after:
                return False
            self.con.execute(
                "UPDATE sprint_work_units SET assigned_shell_id=?,"
                "reviewer_shell_id=?,title=?,expected_output=?,planned_wave=?,"
                "output_kind=?,"
                "updated_at=datetime('now') "
                "WHERE work_unit_id=?",
                (
                    assigned_shell_id,
                    reviewer_shell_id,
                    title,
                    expected_output,
                    planned_wave,
                    output_kind,
                    work_unit_id,
                ),
            )
            if before["task_ids"] != after["task_ids"]:
                self.con.execute(
                    "DELETE FROM sprint_work_unit_tasks "
                    "WHERE sprint_id=? AND work_unit_id=?",
                    (sprint_id, work_unit_id),
                )
                self.con.executemany(
                    "INSERT INTO sprint_work_unit_tasks "
                    "(sprint_id,work_unit_id,task_id) VALUES (?,?,?)",
                    ((sprint_id, work_unit_id, task_id) for task_id in tasks),
                )
            self._replace_dependencies(sprint_id, work_unit_id, dependencies)
            self._event(
                sprint_id,
                "work_unit.replanned",
                planner_shell_id,
                {
                    "work_unit_id": work_unit_id,
                    "before": before,
                    "after": after,
                },
            )
        return True

    def recall(
        self,
        sprint_id: int,
        work_unit_id: int,
        planner_shell_id: int,
        *,
        reason: str,
    ) -> bool:
        """Return one paused, unmerged lane to the editable plan projection."""
        reason = reason.strip()
        if not reason:
            raise ValueError("work-unit recall reason is required")
        if len(reason) > 8000:
            raise ValueError(
                f"work-unit recall reason is {len(reason)} characters; maximum is 8000"
            )
        with db_driver.write_transaction(self.con, "sprint.work_unit.recall"):
            lifecycle, _ = self._require_planner(sprint_id, planner_shell_id)
            if lifecycle != "paused":
                raise SprintInvariantError(
                    "work units may be recalled only while the Sprint is paused"
                )
            unit = self._unit(sprint_id, work_unit_id)
            before = str(unit["disposition"])
            if before == "planned":
                return False
            if before in {"completed", "cancelled"}:
                raise SprintInvariantError("terminal work units cannot be recalled")
            registered = self.con.execute(
                "SELECT registered_pr_id FROM sprint_pr_work_units "
                "WHERE sprint_id=? AND work_unit_id=? "
                "AND superseded_at IS NULL "
                "ORDER BY registered_pr_id LIMIT 1",
                (sprint_id, work_unit_id),
            ).fetchone()
            if registered is not None:
                raise SprintInvariantError(
                    f"work unit {work_unit_id} is bound to registered PR "
                    f"{int(registered['registered_pr_id'])}; preserve that lane and "
                    "plan a replacement instead"
                )

            from sprint_message_delivery import SprintMessageStore

            message_ids = SprintMessageStore(
                self.con
            ).recall_work_assignment_in_transaction(
                sprint_id,
                work_unit_id,
                reason,
            )
            self.con.execute(
                "UPDATE sprint_work_units SET disposition='planned',"
                "updated_at=datetime('now') WHERE work_unit_id=?",
                (work_unit_id,),
            )
            self._event(
                sprint_id,
                "work_unit.recalled",
                planner_shell_id,
                {
                    "work_unit_id": work_unit_id,
                    "before": before,
                    "after": "planned",
                    "assignment_message_ids": list(message_ids),
                    "reason": reason,
                },
            )
        return True

    def dispatch_ready(self, sprint_id: int) -> list[int]:
        """Release every dependency-ready unit that has shell capacity."""
        pause_receipt: PauseReceipt | None = None
        dispatched: list[int] = []
        with db_driver.write_transaction(self.con, "sprint.work_unit.dispatch"):
            sprint = self.con.execute(
                "SELECT lifecycle,originating_planner_shell_id FROM sprints "
                "WHERE sprint_id=?",
                (sprint_id,),
            ).fetchone()
            if sprint is None:
                raise KeyError(f"unknown Sprint: {sprint_id}")
            if sprint["lifecycle"] == "armed":
                pause_receipt = self._queue_delivery_terminal(sprint_id)
                if pause_receipt is None:
                    planner = self._require_participant(
                        sprint_id,
                        int(sprint["originating_planner_shell_id"]),
                        "planner",
                    )
                    dispatched = self._dispatch_ready_locked(
                        sprint_id,
                        planner_participant_id=planner,
                    )
        if pause_receipt is not None:
            self.lifecycle.signal_pause_receipt(pause_receipt)
        return dispatched

    def complete(
        self,
        sprint_id: int,
        work_unit_id: int,
        shell_id: int,
        *,
        result: str,
    ) -> WorkUnitCompletionReceipt:
        """Record a non-code Developer result and release newly unblocked work."""
        result = result.strip()
        if not result:
            raise ValueError("work-unit completion result is required")
        if len(result) > 8000:
            raise ValueError(
                f"work-unit completion result is {len(result)} characters; "
                "maximum is 8000"
            )
        pause_receipt: PauseReceipt | None = None
        receipt: WorkUnitCompletionReceipt
        with db_driver.write_transaction(self.con, "sprint.work_unit.complete"):
            unit = self._unit(sprint_id, work_unit_id)
            if int(unit["assigned_shell_id"]) != shell_id:
                raise SprintAuthorityError("only the assigned Developer owns completion")
            if unit["disposition"] == "completed":
                if unit["completion_result"] != result:
                    raise SprintInvariantError(
                        "work unit was already completed with a different result"
                    )
                return self._completion_receipt(
                    sprint_id,
                    work_unit_id,
                    changed=False,
                )
            if unit["output_kind"] not in {"report_only", "no_code"}:
                raise SprintInvariantError(
                    "code work units complete only through the merge judgment chain"
                )
            if unit["disposition"] != "active":
                raise SprintInvariantError(
                    f"cannot complete work unit from {unit['disposition']}"
                )
            self.con.execute(
                "UPDATE sprint_work_units SET disposition='completed',"
                "completion_result=?,completed_at=datetime('now'),"
                "updated_at=datetime('now') "
                "WHERE work_unit_id=?",
                (result, work_unit_id),
            )
            existing_wake_ids = {
                int(row[0])
                for row in self.con.execute("SELECT wake_id FROM sprint_wake_outbox")
            }
            completion_wake_ids: list[int] = []
            sprint = self.con.execute(
                "SELECT lifecycle,originating_planner_shell_id FROM sprints "
                "WHERE sprint_id=?",
                (sprint_id,),
            ).fetchone()
            pause_receipt = self._queue_delivery_terminal(
                sprint_id,
                completion_wake_ids=completion_wake_ids,
            )
            lifecycle = self.con.execute(
                "SELECT lifecycle FROM sprints WHERE sprint_id=?",
                (sprint_id,),
            ).fetchone()[0]
            if lifecycle == "armed":
                planner = self._require_participant(
                    sprint_id,
                    int(sprint["originating_planner_shell_id"]),
                    "planner",
                )
                completion_wake_ids.extend(
                    self._dispatch_ready_locked(
                        sprint_id,
                        planner_participant_id=planner,
                    )
                )
            wake_ids = tuple(dict.fromkeys(completion_wake_ids))
            created_wake_ids = tuple(
                wake_id for wake_id in wake_ids if wake_id not in existing_wake_ids
            )
            reused_wake_ids = tuple(
                wake_id for wake_id in wake_ids if wake_id in existing_wake_ids
            )
            self._event(
                sprint_id,
                "work_unit.completed",
                shell_id,
                {
                    "work_unit_id": work_unit_id,
                    "output_kind": str(unit["output_kind"]),
                    "stored_result_length": len(result),
                    "stored_result_sha256": hashlib.sha256(result.encode()).hexdigest(),
                    "wake_ids": list(wake_ids),
                },
                actor_kind="participant",
            )
            receipt = self._completion_receipt(
                sprint_id,
                work_unit_id,
                changed=True,
                created_wake_ids=created_wake_ids,
                reused_wake_ids=reused_wake_ids,
            )
        if pause_receipt is not None:
            self.lifecycle.signal_pause_receipt(pause_receipt)
        return receipt

    def cancel(
        self,
        sprint_id: int,
        work_unit_id: int,
        planner_shell_id: int,
        *,
        reason: str,
    ) -> bool:
        """Cancel one unreleased plan without rewriting completed history."""
        reason = reason.strip()
        if not reason:
            raise ValueError("work-unit cancellation reason is required")
        if len(reason) > 8000:
            raise ValueError(
                f"work-unit cancellation reason is {len(reason)} characters; "
                "maximum is 8000"
            )
        pause_receipt: PauseReceipt | None = None
        with db_driver.write_transaction(self.con, "sprint.work_unit.cancel"):
            self._require_planner(sprint_id, planner_shell_id)
            unit = self._unit(sprint_id, work_unit_id)
            if unit["disposition"] == "cancelled":
                if unit["completion_result"] != reason:
                    raise SprintInvariantError(
                        "work unit was already cancelled with a different reason"
                    )
                return False
            if unit["disposition"] != "planned":
                raise SprintInvariantError("only planned work units may be cancelled")
            self.con.execute(
                "UPDATE sprint_work_units SET disposition='cancelled',"
                "completion_result=?,completed_at=datetime('now'),"
                "updated_at=datetime('now') WHERE work_unit_id=?",
                (reason, work_unit_id),
            )
            self._event(
                sprint_id,
                "work_unit.cancelled",
                planner_shell_id,
                {"work_unit_id": work_unit_id, "reason": reason},
            )
            pause_receipt = self._queue_delivery_terminal(sprint_id)
        if pause_receipt is not None:
            self.lifecycle.signal_pause_receipt(pause_receipt)
        return True

    def resolve(
        self,
        sprint_id: int,
        work_unit_id: int,
        planner_shell_id: int,
        *,
        target: str,
        reason: str,
    ) -> bool:
        """Force-move one non-terminal lane to a terminal Planner disposition.

        Paused-only, Planner-only, reasoned, and audited: retires the lane's
        open route expectations, supersedes its PR links (rows retained), and
        wakes the assigned Developer and Reviewer.  PR-bound units are the
        point of the verb; ``--to completed`` fabricates no merge observation.
        """
        if target not in {"completed", "cancelled"}:
            raise ValueError("resolve-unit target must be completed or cancelled")
        reason = reason.strip()
        if not reason:
            raise ValueError("work-unit resolution reason is required")
        if len(reason) > 8000:
            raise ValueError(
                f"work-unit resolution reason is {len(reason)} characters; "
                "maximum is 8000"
            )
        with db_driver.write_transaction(self.con, "sprint.work_unit.resolve"):
            lifecycle, planner_participant_id = self._require_planner(
                sprint_id, planner_shell_id
            )
            if lifecycle != "paused":
                raise SprintInvariantError(
                    "work units may be resolved only while the Sprint is paused"
                )
            unit = self._unit(sprint_id, work_unit_id)
            before = str(unit["disposition"])
            if before in {"completed", "cancelled"}:
                override = self.con.execute(
                    "SELECT payload FROM sprint_events WHERE sprint_id=? "
                    "AND event_type='planner_override' "
                    "AND json_extract(payload,'$.work_unit_id')=? "
                    "ORDER BY event_id DESC LIMIT 1",
                    (sprint_id, work_unit_id),
                ).fetchone()
                if override is not None:
                    prior = json.loads(override["payload"])
                    if (
                        before == target
                        and prior.get("after") == target
                        and prior.get("reason") == reason
                    ):
                        return False
                raise SprintConflictError(
                    f"work unit {work_unit_id} is already {before}; "
                    "resolve-unit does not move a terminal work unit",
                    {
                        "code": "work_unit_already_terminal",
                        "work_unit_id": work_unit_id,
                        "disposition": before,
                    },
                )
            pr_numbers = [
                int(row["pr_number"])
                for row in self.con.execute(
                    "SELECT pr.pr_number FROM sprint_pr_work_units link "
                    "JOIN sprint_registered_prs pr "
                    "ON pr.registered_pr_id=link.registered_pr_id "
                    "WHERE link.sprint_id=? AND link.work_unit_id=? "
                    "AND link.superseded_at IS NULL ORDER BY pr.pr_number",
                    (sprint_id, work_unit_id),
                )
            ]
            from sprint_message_delivery import SprintMessageStore

            messages = SprintMessageStore(self.con)
            retired_message_ids = messages.retire_route_expectations_in_transaction(
                sprint_id,
                work_unit_id,
                retirement_reason=f"Resolved by Planner as {target}: {reason}",
                resolution=f"work unit resolved by Planner as {target}: {reason}",
            )
            self.con.execute(
                "UPDATE sprint_pr_work_units SET superseded_at=datetime('now') "
                "WHERE sprint_id=? AND work_unit_id=? AND superseded_at IS NULL",
                (sprint_id, work_unit_id),
            )
            self.con.execute(
                "UPDATE sprint_work_units SET disposition=?,completion_result=?,"
                "completed_at=datetime('now'),updated_at=datetime('now'),"
                "completion_source=CASE WHEN ?='completed' "
                "THEN 'planner_override' ELSE completion_source END "
                "WHERE work_unit_id=?",
                (target, reason, target, work_unit_id),
            )
            # The durable event lands before any wake: it, not the narration,
            # is the delivery proof of the override.
            self._event(
                sprint_id,
                "planner_override",
                planner_shell_id,
                {
                    "work_unit_id": work_unit_id,
                    "before": before,
                    "after": target,
                    "reason": reason,
                    "pr_numbers": pr_numbers,
                    "retired_message_ids": list(retired_message_ids),
                },
            )
            recipients = self.con.execute(
                "SELECT participant_id FROM sprint_participants "
                "WHERE sprint_id=? AND shell_id IN (?,?) "
                "AND role IN ('developer','reviewer') ORDER BY participant_id",
                (
                    sprint_id,
                    int(unit["assigned_shell_id"]),
                    int(unit["reviewer_shell_id"]),
                ),
            ).fetchall()
            reason_key = hashlib.sha256(reason.encode()).hexdigest()[:16]
            for recipient in recipients:
                messages.send_in_transaction(
                    sprint_id,
                    from_participant_id=planner_participant_id,
                    to_participant_id=int(recipient["participant_id"]),
                    work_unit_id=work_unit_id,
                    message_kind="notification",
                    body=(
                        f"Planner resolved work unit {work_unit_id} from "
                        f"{before} to {target}: {reason}"
                    ),
                    actionable=False,
                    declared_type="re-enter",
                    idempotency_key=(
                        f"planner-override:{sprint_id}:{work_unit_id}:{target}:"
                        f"{reason_key}:participant:{int(recipient['participant_id'])}"
                    ),
                )
        return True

    def complete_from_merge_in_transaction(
        self,
        sprint_id: int,
        work_unit_ids: Iterable[int],
        *,
        transition_key: str,
        dispatch: bool = True,
        pause_receipts: list[PauseReceipt] | None = None,
    ) -> list[int]:
        """Project an observed merge, then release newly ready work atomically."""
        if not self.con.in_transaction:
            raise RuntimeError("merge observation requires an active transaction")
        unit_ids = tuple(sorted({int(unit_id) for unit_id in work_unit_ids}))
        if not unit_ids:
            raise SprintInvariantError("merged PR has no linked work units")
        sprint = self.con.execute(
            "SELECT lifecycle,originating_planner_shell_id FROM sprints "
            "WHERE sprint_id=?",
            (sprint_id,),
        ).fetchone()
        if sprint is None:
            raise KeyError(f"unknown Sprint: {sprint_id}")
        if sprint["lifecycle"] not in {"armed", "paused"}:
            return []
        placeholders = ",".join("?" for _ in unit_ids)
        units = self.con.execute(
            "SELECT work_unit_id,assigned_shell_id,disposition "
            "FROM sprint_work_units "
            f"WHERE sprint_id=? AND work_unit_id IN ({placeholders}) "
            "ORDER BY work_unit_id",
            (sprint_id, *unit_ids),
        ).fetchall()
        if {int(unit["work_unit_id"]) for unit in units} != set(unit_ids):
            raise SprintInvariantError("merged PR links unknown Sprint work units")

        changed = False
        for unit in units:
            if unit["disposition"] == "completed":
                continue
            if unit["disposition"] != "merge_ready":
                existing_bypass = self.con.execute(
                    "SELECT 1 FROM sprint_events WHERE sprint_id=? "
                    "AND event_type='merge.grant_bypassed' "
                    "AND json_extract(payload,'$.transition_key')=? "
                    "AND json_extract(payload,'$.work_unit_id')=?",
                    (sprint_id, transition_key, int(unit["work_unit_id"])),
                ).fetchone()
                if existing_bypass is None:
                    self._event(
                        sprint_id,
                        "merge.grant_bypassed",
                        None,
                        {
                            "before": unit["disposition"],
                            "transition_key": transition_key,
                            "work_unit_id": int(unit["work_unit_id"]),
                        },
                        actor_kind="system",
                    )
                    # The notice mirrors the event and is deduped on the same
                    # condition: re-observing the merge after the unit's
                    # disposition moved on (resume reconciliation) must not
                    # rebuild the body under the same idempotency key.
                    self._queue_planner_merge_bypass(
                        sprint_id,
                        work_unit_id=int(unit["work_unit_id"]),
                        transition_key=transition_key,
                        before=str(unit["disposition"]),
                    )
                continue
            changed = True
            self.con.execute(
                "UPDATE sprint_work_units SET disposition='completed',"
                "completed_at=datetime('now'),updated_at=datetime('now') "
                "WHERE work_unit_id=?",
                (unit["work_unit_id"],),
            )
            self._event(
                sprint_id,
                "work_unit.completed",
                None,
                {
                    "source": "pr.merge_observed",
                    "transition_key": transition_key,
                    "work_unit_id": int(unit["work_unit_id"]),
                },
                actor_kind="system",
            )
        if changed:
            pause_receipt = self._queue_delivery_terminal(sprint_id)
            if pause_receipt is not None and pause_receipts is not None:
                pause_receipts.append(pause_receipt)
        lifecycle = self.con.execute(
            "SELECT lifecycle FROM sprints WHERE sprint_id=?",
            (sprint_id,),
        ).fetchone()[0]
        if not changed or not dispatch or lifecycle != "armed":
            return []
        planner = self._require_participant(
            sprint_id,
            int(sprint["originating_planner_shell_id"]),
            "planner",
        )
        return self._dispatch_ready_locked(
            sprint_id,
            planner_participant_id=planner,
        )

    def _queue_delivery_terminal(
        self,
        sprint_id: int,
        *,
        completion_wake_ids: list[int] | None = None,
    ) -> PauseReceipt | None:
        state = self.con.execute(
            "SELECT s.lifecycle,COUNT(u.work_unit_id) AS total,"
            "COALESCE(SUM(u.disposition='completed'),0) AS completed,"
            "COALESCE(SUM(u.disposition='cancelled'),0) AS cancelled,"
            "s.conformance_reviewer_shell_id,s.conformance_owner_generation "
            "FROM sprints s LEFT JOIN sprint_work_units u USING (sprint_id) "
            "WHERE s.sprint_id=? GROUP BY s.lifecycle",
            (sprint_id,),
        ).fetchone()
        if (
            state is None
            or state["lifecycle"] != "armed"
            or not state["total"]
            or state["total"] != state["completed"] + state["cancelled"]
        ):
            return None
        total = int(state["total"])
        owner_shell_id = state["conformance_reviewer_shell_id"]
        owner = None
        if owner_shell_id is not None:
            owner = self.con.execute(
                "SELECT participant.participant_id FROM sprint_participants participant "
                "JOIN shells shell ON shell.shell_id=participant.shell_id "
                "WHERE participant.sprint_id=? AND participant.shell_id=? "
                "AND participant.role='reviewer' "
                "AND participant.disposition<>'declined' "
                "AND COALESCE(shell.is_deleted,0)=0",
                (sprint_id, int(owner_shell_id)),
            ).fetchone()
        if owner is None:
            lifecycle = self.lifecycle
            eligible = lifecycle._eligible_conformance_reviewers(sprint_id)
            command = (
                f"sc sprint resume --sprint {sprint_id} "
                "--conformance-reviewer-shell <shell_id> "
                '--reason "replace closeout owner"'
            )
            detail = {
                "code": "conformance_owner_required",
                "eligible_reviewer_shell_ids": list(eligible),
                "recovery_command": command,
            }
            sprint = lifecycle._sprint(sprint_id)
            receipt = lifecycle._pause_in_transaction(
                sprint,
                LifecycleActor("system"),
                reason="conformance_owner_required",
                detail=detail,
                notice_body=(
                    f"Sprint {sprint_id} reached delivery terminal without an "
                    "eligible conformance owner. Eligible Reviewer shell IDs: "
                    f"{list(eligible)}. Recover with `{command}`."
                ),
            )
            lifecycle._event(
                sprint_id,
                "conformance_owner.required",
                LifecycleActor("system"),
                detail,
            )
            return receipt
        owner_shell_id = int(owner_shell_id)
        generation = int(state["conformance_owner_generation"])
        if self.con.execute(
            "SELECT 1 FROM sprint_events WHERE sprint_id=? "
            "AND event_type='sprint.delivery_terminal' "
            "AND json_extract(payload,'$.terminal_count')=? "
            "AND json_extract(payload,'$.conformance_reviewer_shell_id')=? "
            "AND json_extract(payload,'$.conformance_owner_generation')=?",
            (sprint_id, total, owner_shell_id, generation),
        ).fetchone():
            return None
        base_key = (
            f"sprint:{sprint_id}:delivery-terminal:{total}:"
            f"owner:{owner_shell_id}:generation:{generation}"
        )
        body = (
            f"All planned delivery work for Sprint {sprint_id} is terminal "
            f"({total} units: {int(state['completed'])} completed, "
            f"{int(state['cancelled'])} cancelled). Begin whole-Sprint "
            "conformance per sprint_rev — compile the evidence packet, judge "
            "integrated main, then send the Planner either a re-enter decision "
            "or your conclude decision."
        )
        from sprint_message_delivery import SprintMessageStore

        messages = SprintMessageStore(self.con)
        message_receipt = messages.send_in_transaction(
            sprint_id,
            to_participant_id=int(owner["participant_id"]),
            message_kind="notification",
            body=body,
            actionable=False,
            declared_type="new",
            idempotency_key=base_key,
        )
        if completion_wake_ids is not None and message_receipt.wake_id is not None:
            completion_wake_ids.append(int(message_receipt.wake_id))
        self._event(
            sprint_id,
            "sprint.delivery_terminal",
            None,
            {
                "terminal_count": total,
                "completed_count": int(state["completed"]),
                "cancelled_count": int(state["cancelled"]),
                "conformance_reviewer_shell_id": owner_shell_id,
                "conformance_owner_generation": generation,
            },
            actor_kind="system",
        )
        return None

    def _completion_receipt(
        self,
        sprint_id: int,
        work_unit_id: int,
        *,
        changed: bool,
        created_wake_ids: tuple[int, ...] = (),
        reused_wake_ids: tuple[int, ...] = (),
    ) -> WorkUnitCompletionReceipt:
        unit = self._unit(sprint_id, work_unit_id)
        result = str(unit["completion_result"])
        completed_at = unit["completed_at"]
        if unit["disposition"] != "completed" or completed_at is None:
            raise SprintInvariantError("work-unit completion receipt requires completed state")
        if not changed:
            event = self.con.execute(
                "SELECT payload FROM sprint_events WHERE sprint_id=? "
                "AND event_type='work_unit.completed' "
                "AND json_extract(payload,'$.work_unit_id')=? "
                "ORDER BY event_id DESC LIMIT 1",
                (sprint_id, work_unit_id),
            ).fetchone()
            prior_wake_ids: tuple[int, ...] = ()
            if event is not None:
                payload = json.loads(event["payload"])
                prior_wake_ids = tuple(int(value) for value in payload.get("wake_ids", ()))
            reused_wake_ids = prior_wake_ids
        return WorkUnitCompletionReceipt(
            sprint_id=sprint_id,
            work_unit_id=work_unit_id,
            disposition=str(unit["disposition"]),
            completed_at=str(completed_at),
            output_kind=str(unit["output_kind"]),
            stored_result_length=len(result),
            stored_result_sha256=hashlib.sha256(result.encode()).hexdigest(),
            changed=changed,
            idempotent=not changed,
            created_wake_ids=created_wake_ids,
            reused_wake_ids=reused_wake_ids,
        )

    def _queue_planner_merge_bypass(
        self,
        sprint_id: int,
        *,
        work_unit_id: int,
        transition_key: str,
        before: str,
    ) -> int:
        planner = self.con.execute(
            "SELECT participant_id FROM sprint_participants "
            "WHERE sprint_id=? AND role='planner'",
            (sprint_id,),
        ).fetchone()
        if planner is None:
            raise SprintInvariantError("Sprint has no Planner participant")
        planner_id = int(planner["participant_id"])
        key = f"merge-grant-bypassed:{transition_key}:unit:{work_unit_id}"
        from sprint_message_delivery import SprintMessageStore

        receipt = SprintMessageStore(self.con).send_in_transaction(
            sprint_id,
            to_participant_id=planner_id,
            work_unit_id=work_unit_id,
            message_kind="notification",
            body=(
                f"Merged PR bypassed the Sprint merge grant for work unit "
                f"{work_unit_id} from {before}; the unit remains incomplete "
                "and requires Planner disposition: pause the Sprint and run "
                f"`sc sprint resolve-unit --sprint {sprint_id} "
                f"--work-unit {work_unit_id} --to completed|cancelled "
                "--reason <text>`."
            ),
            actionable=False,
            declared_type="re-enter",
            idempotency_key=key,
        )
        return receipt.message_id

    def _dispatch_ready_locked(
        self,
        sprint_id: int,
        *,
        planner_participant_id: int,
    ) -> list[int]:
        occupied = {
            int(row[0])
            for row in self.con.execute(
                "SELECT assigned_shell_id FROM sprint_work_units "
                "WHERE sprint_id=? AND disposition IN "
                "('ready','active','in_review','fixing','merge_ready')",
                (sprint_id,),
            )
        }
        candidates = self.con.execute(
            "SELECT u.*,p.participant_id FROM sprint_work_units u "
            "JOIN sprint_participants p ON p.sprint_id=u.sprint_id "
            "AND p.shell_id=u.assigned_shell_id AND p.role='developer' "
            "WHERE u.sprint_id=? AND u.disposition='planned' "
            "AND NOT EXISTS ("
            "SELECT 1 FROM sprint_work_unit_dependencies d "
            "JOIN sprint_work_units upstream "
            "ON upstream.sprint_id=d.sprint_id "
            "AND upstream.work_unit_id=d.depends_on_work_unit_id "
            "WHERE d.sprint_id=u.sprint_id AND d.work_unit_id=u.work_unit_id "
            "AND upstream.disposition<>'completed') "
            "ORDER BY u.planned_wave,u.work_unit_id",
            (sprint_id,),
        ).fetchall()
        wake_ids: list[int] = []
        for unit in candidates:
            shell_id = int(unit["assigned_shell_id"])
            if shell_id in occupied:
                continue
            wake_id = self._queue_assignment(
                sprint_id,
                planner_participant_id=planner_participant_id,
                unit=unit,
            )
            occupied.add(shell_id)
            if wake_id not in wake_ids:
                wake_ids.append(wake_id)
        return wake_ids

    def _queue_assignment(
        self,
        sprint_id: int,
        *,
        planner_participant_id: int,
        unit: sqlite3.Row,
    ) -> int:
        unit_id = int(unit["work_unit_id"])
        key = _next_assignment_key(self.con, sprint_id, unit_id)
        # Local import avoids a module cycle: the message store uses the domain
        # lifecycle for durable failure evidence.
        from sprint_message_delivery import SprintMessageStore

        receipt = SprintMessageStore(self.con).send_in_transaction(
            sprint_id,
            from_participant_id=planner_participant_id,
            to_participant_id=int(unit["participant_id"]),
            work_unit_id=unit_id,
            message_kind="work_assignment",
            body=f"{unit['title']}\n\n{unit['expected_output']}",
            actionable=True,
            declared_type="force-new",
            idempotency_key=key,
        )
        if receipt.wake_id is None:
            raise SprintInvariantError("work assignment has no wake")
        message_id = receipt.message_id
        wake_id = receipt.wake_id
        self.con.execute(
            "UPDATE sprint_work_units SET disposition='ready',"
            "updated_at=datetime('now') WHERE work_unit_id=?",
            (unit_id,),
        )
        self._event(
            sprint_id,
            "work_unit.ready",
            None,
            {
                "work_unit_id": unit_id,
                "message_id": message_id,
                "wake_id": wake_id,
            },
            actor_kind="system",
        )
        return wake_id

    def _replace_dependencies(
        self,
        sprint_id: int,
        work_unit_id: int,
        dependency_ids: tuple[int, ...],
    ) -> None:
        if work_unit_id in dependency_ids:
            raise SprintInvariantError("work units cannot depend on themselves")
        if dependency_ids:
            placeholders = ",".join("?" for _ in dependency_ids)
            found = {
                int(row[0])
                for row in self.con.execute(
                    "SELECT work_unit_id FROM sprint_work_units "
                    f"WHERE sprint_id=? AND work_unit_id IN ({placeholders})",
                    (sprint_id, *dependency_ids),
                )
            }
            if found != set(dependency_ids):
                raise SprintInvariantError(
                    "dependencies must name work units in the same Sprint"
                )
        self.con.execute(
            "DELETE FROM sprint_work_unit_dependencies "
            "WHERE sprint_id=? AND work_unit_id=?",
            (sprint_id, work_unit_id),
        )
        self.con.executemany(
            "INSERT INTO sprint_work_unit_dependencies "
            "(sprint_id,work_unit_id,depends_on_work_unit_id) VALUES (?,?,?)",
            (
                (sprint_id, work_unit_id, dependency_id)
                for dependency_id in dependency_ids
            ),
        )
        self._require_acyclic(sprint_id)

    def _require_acyclic(self, sprint_id: int) -> None:
        graph: dict[int, set[int]] = {}
        for row in self.con.execute(
            "SELECT work_unit_id,depends_on_work_unit_id "
            "FROM sprint_work_unit_dependencies WHERE sprint_id=?",
            (sprint_id,),
        ):
            graph.setdefault(int(row[0]), set()).add(int(row[1]))

        visiting: set[int] = set()
        visited: set[int] = set()

        def visit(unit_id: int) -> None:
            if unit_id in visiting:
                raise SprintInvariantError("work-unit dependencies must be acyclic")
            if unit_id in visited:
                return
            visiting.add(unit_id)
            for dependency_id in graph.get(unit_id, ()):
                visit(dependency_id)
            visiting.remove(unit_id)
            visited.add(unit_id)

        for unit_id in graph:
            visit(unit_id)

    def _require_tasks(self, sprint_id: int, task_ids: tuple[int, ...]) -> None:
        placeholders = ",".join("?" for _ in task_ids)
        found = {
            int(row[0])
            for row in self.con.execute(
                "SELECT t.task_id FROM spec_tasks t "
                "JOIN sprint_specs ss ON ss.document_id=t.document_id "
                f"WHERE ss.sprint_id=? AND t.task_id IN ({placeholders})",
                (sprint_id, *task_ids),
            )
        }
        if found != set(task_ids):
            raise SprintInvariantError(
                "work-unit tasks must belong to a governing Sprint spec"
            )

    def _require_planner(
        self,
        sprint_id: int,
        planner_shell_id: int,
    ) -> tuple[str, int]:
        sprint = self.con.execute(
            "SELECT lifecycle,originating_planner_shell_id FROM sprints "
            "WHERE sprint_id=?",
            (sprint_id,),
        ).fetchone()
        if sprint is None:
            raise KeyError(f"unknown Sprint: {sprint_id}")
        if int(sprint["originating_planner_shell_id"]) != planner_shell_id:
            raise SprintAuthorityError("only the originating Planner owns replanning")
        participant = self._require_participant(
            sprint_id, planner_shell_id, "planner"
        )
        return str(sprint["lifecycle"]), participant

    def _require_participant(
        self,
        sprint_id: int,
        shell_id: int,
        role: str,
    ) -> int:
        row = self.con.execute(
            "SELECT participant_id FROM sprint_participants "
            "WHERE sprint_id=? AND shell_id=? AND role=?",
            (sprint_id, shell_id, role),
        ).fetchone()
        if row is None:
            raise SprintInvariantError(
                f"shell {shell_id} is not this Sprint's assigned {role}"
            )
        return int(row[0])

    def _unit(self, sprint_id: int, work_unit_id: int) -> sqlite3.Row:
        row = self.con.execute(
            "SELECT * FROM sprint_work_units WHERE sprint_id=? AND work_unit_id=?",
            (sprint_id, work_unit_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown Sprint work unit: {work_unit_id}")
        return row

    def _plan_projection(self, unit: sqlite3.Row) -> dict:
        tasks = [
            int(row[0])
            for row in self.con.execute(
                "SELECT task_id FROM sprint_work_unit_tasks "
                "WHERE sprint_id=? AND work_unit_id=? ORDER BY task_id",
                (unit["sprint_id"], unit["work_unit_id"]),
            )
        ]
        dependencies = [
            int(row[0])
            for row in self.con.execute(
                "SELECT depends_on_work_unit_id "
                "FROM sprint_work_unit_dependencies "
                "WHERE sprint_id=? AND work_unit_id=? "
                "ORDER BY depends_on_work_unit_id",
                (unit["sprint_id"], unit["work_unit_id"]),
            )
        ]
        return {
            "assigned_shell_id": int(unit["assigned_shell_id"]),
            "reviewer_shell_id": int(unit["reviewer_shell_id"]),
            "title": str(unit["title"]),
            "expected_output": str(unit["expected_output"]),
            "task_ids": tasks,
            "planned_wave": int(unit["planned_wave"]),
            "output_kind": str(unit["output_kind"]),
            "dependency_ids": dependencies,
        }

    def _event(
        self,
        sprint_id: int,
        event_type: str,
        actor_shell_id: int | None,
        payload: dict,
        *,
        actor_kind: str = "planner",
    ) -> None:
        self.con.execute(
            "INSERT INTO sprint_events "
            "(sprint_id,event_type,actor_kind,actor_shell_id,payload) "
            "VALUES (?,?,?,?,?)",
            (
                sprint_id,
                event_type,
                actor_kind,
                actor_shell_id,
                json.dumps(payload, sort_keys=True),
            ),
        )


class ArmedServiceSwitch:
    """Run Sprint external-service callbacks only while a Sprint is armed.

    ``recover_on_startup`` is the restart seam: it reads durable lifecycle once
    and immediately services an armed Sprint.  A supervisor may call ``tick``
    on its normal pulse; prepared, paused, completed, aborted, and empty states
    invoke zero callbacks.
    """

    def __init__(
        self,
        store: SprintLifecycleStore,
        callbacks: Iterable[Callable[[int, str], None]],
    ) -> None:
        self.store = store
        self.callbacks = tuple(callbacks)

    def recover_on_startup(self) -> bool:
        self.store.recover_on_startup()
        return self._run("startup")

    def tick(self) -> bool:
        return self._run("pulse")

    def _run(self, trigger: str) -> bool:
        sprint_id = self.store.armed_sprint_id()
        if sprint_id is None:
            return False
        for callback in self.callbacks:
            callback(sprint_id, trigger)
        return True
