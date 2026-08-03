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
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import active_chat_registry
import conversation_broker
import conversation_events
import db_driver
import sprint_participant_chats

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


class SprintLifecycleStore:
    """Transactional lifecycle operations over one engine DB connection."""

    def __init__(
        self,
        con: sqlite3.Connection,
        *,
        interrupt_run: Callable[[int], bool] | None = None,
        notify_commit: Callable[[], bool] | None = None,
    ) -> None:
        self.con = con
        self.con.row_factory = sqlite3.Row
        self.interrupt_run = interrupt_run or conversation_broker.interrupt_run
        self.notify_commit = notify_commit or conversation_broker.notify_commit

    def armed_sprint_id(self) -> int | None:
        row = self.con.execute(
            "SELECT sprint_id FROM sprints WHERE lifecycle='armed'"
        ).fetchone()
        return int(row[0]) if row is not None else None

    def arm(self, sprint_id: int, planner_shell_id: int) -> list[int]:
        """Arm an eligible prepared Sprint and release initial work atomically.

        The returned IDs name the committed wake intents.  A uniqueness failure
        or any invalid plan rolls back the lifecycle update, messages, wakes,
        and work-unit dispositions together.
        """
        actor = LifecycleActor("planner", planner_shell_id)
        with db_driver.write_transaction(self.con, "sprint.arm"):
            sprint = self._sprint(sprint_id)
            if sprint["lifecycle"] != "prepared":
                raise SprintStateError(
                    f"arm() requires prepared; found {sprint['lifecycle']}"
                )
            self._require_edge(sprint["lifecycle"], "armed")
            self._authorize(sprint, "armed", actor)
            planner_participant, selections = self._validate_arm_plan(sprint)
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
                },
            )
        return wake_ids

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
            self._update_lifecycle(
                sprint_id,
                current=current,
                target=target,
                outcome=terminal_outcome,
            )
            if target == "completed":
                self._clear_coordinate_mode(
                    sprint_id,
                    actor,
                    reason="Sprint closed by FnB",
                )
            self._event(
                sprint_id,
                f"lifecycle.{target}",
                actor,
                {"from": current, "reason": reason},
            )
        return True

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
                return ResumeReceipt(False, (), (), (), (), (), anomalies)
            if current == "prepared":
                raise SprintInvariantError(
                    "prepared Sprints must use arm() so plan release is atomic"
                )
            self._require_edge(current, "armed")
            self._authorize(sprint, "armed", actor)

            # Armed is not externally visible until this transaction commits.
            # That permits active reconciliation notifications to be created
            # before dispatch without exposing a half-reconciled Sprint.
            self._update_lifecycle(
                sprint_id,
                current=current,
                target="armed",
                outcome=None,
            )
            reset_wake_ids = self._reset_contention_episode_in_transaction(
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
            projected, resolved = (
                self._reconcile_registered_prs_in_transaction(sprint_id)
                if pause_receipt is None
                else ((), ())
            )
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
                planner = self._planner_participant_id(sprint_id)
                dispatched = tuple(
                    SprintWorkUnitStore(self.con)._dispatch_ready_locked(
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

    def _reset_contention_episode_in_transaction(
        self,
        sprint_id: int,
        actor: LifecycleActor,
    ) -> tuple[int, ...]:
        """Make a human resume the durable anchor of a fresh busy episode."""
        report = self.con.execute(
            "SELECT body FROM sprint_reports WHERE sprint_id=? "
            "AND report_kind='pause' ORDER BY report_id DESC LIMIT 1",
            (sprint_id,),
        ).fetchone()
        if report is None:
            return ()
        pause = json.loads(str(report["body"]))
        if pause.get("reason") != "wake_contention_exhausted":
            return ()
        wake_id = int(pause["detail"]["wake_id"])
        wake = self.con.execute(
            "SELECT w.participant_id,w.idempotency_key "
            "FROM sprint_wake_outbox w "
            "WHERE w.sprint_id=? AND w.wake_id=? "
            "AND w.state IN ('delivered','failed') AND EXISTS ("
            "SELECT 1 FROM sprint_wake_messages wm "
            "JOIN wake_message m USING (message_id) "
            "WHERE wm.sprint_id=w.sprint_id AND wm.wake_id=w.wake_id "
            "AND m.read_at IS NULL)",
            (sprint_id, wake_id),
        ).fetchone()
        if wake is None:
            return ()
        reset_key = f"sprint-resume:{sprint_id}:contention-episode:{wake_id}"
        replacement_wake_id = int(
            self.con.execute(
                "INSERT INTO sprint_wake_outbox "
                "(sprint_id,participant_id,receiver_shell_id,idempotency_key,"
                "available_at) VALUES (?,?,?,?,datetime('now'))",
                (
                    sprint_id,
                    int(wake["participant_id"]),
                    _participant_shell_id(self.con, int(wake["participant_id"])),
                    reset_key,
                ),
            ).lastrowid
        )
        self.con.execute(
            "UPDATE wake_message SET delivered_at=NULL WHERE message_id IN ("
            "SELECT message_id FROM sprint_wake_messages "
            "WHERE sprint_id=? AND wake_id=?)",
            (sprint_id, wake_id),
        )
        self.con.execute(
            "UPDATE sprint_wake_messages SET wake_id=? "
            "WHERE sprint_id=? AND wake_id=?",
            (replacement_wake_id, sprint_id, wake_id),
        )
        self._event(
            sprint_id,
            "wake.requeued",
            actor,
            {
                "trigger": "resume",
                "classification": "contention_episode_reset",
                "prior_wake_id": wake_id,
                "replacement_wake_id": replacement_wake_id,
                "prior_idempotency_key": str(wake["idempotency_key"]),
                "idempotency_key": reset_key,
                "replacement_conversation_id": None,
            },
        )
        return (replacement_wake_id,)

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
                "lease_expires_at=NULL,available_at=datetime('now', ?) "
                "WHERE wake_id=?",
                (
                    attempt,
                    "failed" if terminal else "pending",
                    error,
                    1 if terminal else 0,
                    f"+{backoff_seconds} seconds",
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
                            self._reconcile_registered_prs_in_transaction(sprint_id)
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
        self, sprint_id: int
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
                    "WHERE registered_pr_id=? ORDER BY work_unit_id",
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
            shell_busy = (
                turn["run_state"] == "failed"
                and turn["error_code"] == "SHELL_BUSY"
            )
            busy_prefix = f"sprint-recovery:{sprint_id}:busy:"
            busy_recovery = wake_key.startswith(busy_prefix)
            if (
                (wake_key.startswith("sprint-recovery:") and not busy_recovery)
                or ":failed-wake:" in wake_key
            ):
                continue
            if busy_recovery and not shell_busy:
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
            self._event(
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
            self._event(
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
            replacements.append(new_wake_id)
        return _WakeReconcileResult(tuple(sorted(set(replacements))))

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

    def _wake_turn_evidence(self, wake_id: int) -> dict[str, str | int | None]:
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
            }
        message = self.con.execute(
            "SELECT message_id,state FROM conversation_messages "
            "WHERE conversation_id=? AND idempotency_key=?",
            (attempt["target_conversation_id"], wake["idempotency_key"]),
        ).fetchone()
        run_state = None
        error_code = None
        error_detail = None
        if message is not None:
            run = self.con.execute(
                "SELECT state,error_code,error_detail FROM conversation_runs "
                "WHERE trigger_message_id=? "
                "ORDER BY attempt DESC LIMIT 1",
                (message["message_id"],),
            ).fetchone()
            if run is not None:
                run_state = str(run["state"])
                error_code = run["error_code"]
                error_detail = run["error_detail"]
        return {
            "attempt_number": int(attempt["attempt_number"]),
            "target_conversation_id": attempt["target_conversation_id"],
            "native_run_ref": attempt["native_run_ref"],
            "attempt_outcome": str(attempt["outcome"]),
            "message_state": str(message["state"]) if message is not None else None,
            "run_state": run_state,
            "error_code": error_code,
            "error_detail": error_detail,
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
        elif edge == ("paused", "armed"):
            allowed = {"planner", "fnb"}
        elif target in {"completed", "aborted"}:
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
        invalid_specs = self.con.execute(
            "SELECT COUNT(*) FROM sprint_specs ss "
            "JOIN sprint_spec_approvals a ON a.approval_id=ss.approval_id "
            "JOIN documents d ON d.document_id=ss.document_id "
            "JOIN shells reviewer ON reviewer.shell_id=a.reviewer_shell_id "
            "WHERE ss.sprint_id=? AND "
            "(a.verdict<>'pass' OR a.document_id<>ss.document_id "
            "OR a.revision_sha256<>ss.bound_revision_sha256 "
            "OR d.kind<>'spec' OR d.feature_id<>? "
            "OR reviewer.flavor<>'reviewer' "
            "OR COALESCE(reviewer.is_deleted,0)<>0)",
            (sprint_id, sprint["feature_id"]),
        ).fetchone()[0]
        bound_specs = self.con.execute(
            "SELECT ss.bound_revision_sha256,d.body FROM sprint_specs ss "
            "JOIN documents d ON d.document_id=ss.document_id "
            "WHERE ss.sprint_id=?",
            (sprint_id,),
        ).fetchall()
        current_revision_mismatch = any(
            row["body"] is None
            or hashlib.sha256(row["body"].encode()).hexdigest()
            != row["bound_revision_sha256"]
            for row in bound_specs
        )
        if not bound_specs or invalid_specs or current_revision_mismatch:
            raise SprintInvariantError(
                "arming requires at least one exact, passing spec approval"
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
            "SELECT p.participant_id,p.role,p.harness,p.model,p.effort,"
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
        return int(planner[0]), selections

    def _queue_arming_planner_wake(
        self,
        sprint_id: int,
        *,
        planner_participant_id: int,
        selections: list[sqlite3.Row],
    ) -> int:
        lines = [
            f"Sprint {sprint_id} armed. Recorded participant launch selections:"
        ]
        for selection in selections:
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
    ) -> None:
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
        )


class SprintWorkUnitStore:
    """Planner-owned work-unit planning and dependency-aware dispatch."""

    def __init__(self, con: sqlite3.Connection) -> None:
        self.con = con
        self.con.row_factory = sqlite3.Row

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
        assigned_shell_id: int,
        reviewer_shell_id: int,
        planned_wave: int,
        dependency_ids: Iterable[int],
        output_kind: str | None = None,
    ) -> bool:
        """Replace the editable plan projection and append its before/after fact."""
        if planned_wave < 0:
            raise ValueError("planned wave must be non-negative")
        dependencies = tuple(
            dict.fromkeys(int(unit_id) for unit_id in dependency_ids)
        )
        with db_driver.write_transaction(self.con, "sprint.work_unit.replan"):
            self._require_planner(sprint_id, planner_shell_id)
            unit = self._unit(sprint_id, work_unit_id)
            if unit["disposition"] != "planned":
                raise SprintInvariantError(
                    "only planned work units may be replanned; return assigned "
                    "work to the ready pool first"
                )
            self._require_participant(sprint_id, assigned_shell_id, "developer")
            self._require_participant(sprint_id, reviewer_shell_id, "reviewer")
            if output_kind is None:
                output_kind = str(unit["output_kind"])
            if output_kind not in WORK_UNIT_OUTPUT_KINDS:
                raise ValueError(
                    "work-unit output kind must be code, report_only, or no_code"
                )
            before = self._plan_projection(unit)
            after = {
                "assigned_shell_id": assigned_shell_id,
                "reviewer_shell_id": reviewer_shell_id,
                "planned_wave": planned_wave,
                "output_kind": output_kind,
                "dependency_ids": sorted(dependencies),
            }
            if before == after:
                return False
            self.con.execute(
                "UPDATE sprint_work_units SET assigned_shell_id=?,"
                "reviewer_shell_id=?,planned_wave=?,output_kind=?,"
                "updated_at=datetime('now') "
                "WHERE work_unit_id=?",
                (
                    assigned_shell_id,
                    reviewer_shell_id,
                    planned_wave,
                    output_kind,
                    work_unit_id,
                ),
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

    def dispatch_ready(self, sprint_id: int) -> list[int]:
        """Release every dependency-ready unit that has shell capacity."""
        with db_driver.write_transaction(self.con, "sprint.work_unit.dispatch"):
            sprint = self.con.execute(
                "SELECT lifecycle,originating_planner_shell_id FROM sprints "
                "WHERE sprint_id=?",
                (sprint_id,),
            ).fetchone()
            if sprint is None:
                raise KeyError(f"unknown Sprint: {sprint_id}")
            if sprint["lifecycle"] != "armed":
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

    def complete(
        self,
        sprint_id: int,
        work_unit_id: int,
        shell_id: int,
        *,
        result: str,
    ) -> list[int]:
        """Record a non-code Developer result and release newly unblocked work."""
        result = result.strip()
        if not result:
            raise ValueError("work-unit completion result is required")
        if len(result) > 8000:
            raise ValueError(
                f"work-unit completion result is {len(result)} characters; "
                "maximum is 8000"
            )
        with db_driver.write_transaction(self.con, "sprint.work_unit.complete"):
            unit = self._unit(sprint_id, work_unit_id)
            if int(unit["assigned_shell_id"]) != shell_id:
                raise SprintAuthorityError("only the assigned Developer owns completion")
            if unit["disposition"] == "completed":
                if unit["completion_result"] != result:
                    raise SprintInvariantError(
                        "work unit was already completed with a different result"
                    )
                return []
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
            self._event(
                sprint_id,
                "work_unit.completed",
                shell_id,
                {
                    "work_unit_id": work_unit_id,
                    "output_kind": str(unit["output_kind"]),
                    "result": result,
                },
                actor_kind="participant",
            )
            sprint = self.con.execute(
                "SELECT lifecycle,originating_planner_shell_id FROM sprints "
                "WHERE sprint_id=?",
                (sprint_id,),
            ).fetchone()
            if sprint["lifecycle"] != "armed":
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
        return True

    def complete_from_merge_in_transaction(
        self,
        sprint_id: int,
        work_unit_ids: Iterable[int],
        *,
        transition_key: str,
        dispatch: bool = True,
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
        if not changed or not dispatch or sprint["lifecycle"] != "armed":
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
                "and requires Planner disposition."
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
        generation = int(
            self.con.execute(
                "SELECT COUNT(*) FROM wake_message "
                "WHERE work_unit_id=? AND message_kind='work_assignment'",
                (unit_id,),
            ).fetchone()[0]
        ) + 1
        key = (
            f"sprint:{sprint_id}:work-unit:{unit_id}:assignment:{generation}"
        )
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
            declared_type="new",
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
