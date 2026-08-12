"""Sprints v2 conformance follow-ups and bounded close evidence."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import conversation_events
import db_driver
import sprint_cleanup
from sprint_domain import (
    LifecycleActor,
    SprintAuthorityError,
    SprintInvariantError,
    SprintLifecycleStore,
)

DEFAULT_SECTION_LIMIT = 50
MAX_SECTION_LIMIT = 200


@dataclass(frozen=True)
class ConformanceReceipt:
    report_id: int
    followup_ids: tuple[int, ...]
    final_report_id: int
    planner_message_id: int
    planner_wake_id: int
    completed: bool
    created: bool


@dataclass(frozen=True)
class FinalReportReceipt:
    report_id: int
    created: bool


@dataclass(frozen=True)
class CompletionReceipt:
    changed: bool
    report_id: int | None
    report_created: bool


class SprintCloseStore:
    """Record close-out findings and compile deterministic evidence packets."""

    def __init__(
        self,
        con: sqlite3.Connection,
        *,
        cleanup_store: sprint_cleanup.SprintCleanupTargetStore | None = None,
    ) -> None:
        self.con = con
        self.con.row_factory = sqlite3.Row
        self.cleanup_store = cleanup_store or sprint_cleanup.SprintCleanupTargetStore(
            con
        )

    def record_conformance(
        self,
        sprint_id: int,
        reviewer_shell_id: int,
        *,
        body: str,
        findings: Iterable[dict[str, Any]],
        final_report: str,
        reason: str,
        terminal_outcome: str,
        idempotency_key: str,
    ) -> ConformanceReceipt:
        """Commit conformance, terminal close, and Planner receipt atomically."""
        body = self._required(body, "conformance body")
        final_report = self._required(final_report, "final report body")
        reason = self._required(reason, "completion reason", maximum=2000)
        terminal_outcome = self._required(
            terminal_outcome, "terminal outcome", maximum=2000
        )
        idempotency_key = self._required(
            idempotency_key, "idempotency key", maximum=220
        )
        normalized = tuple(self._normalize_finding(item) for item in findings)
        existing_before = self.con.execute(
            "SELECT 1 FROM sprint_reports WHERE sprint_id=? "
            "AND report_kind='conformance' AND idempotency_key=?",
            (sprint_id, idempotency_key),
        ).fetchone()
        cleanup_targets = (
            ()
            if existing_before is not None
            else self.cleanup_store.prepare_targets(sprint_id)
        )
        closed_conversation_ids: tuple[str, ...] = ()
        with db_driver.write_transaction(self.con, "sprint.close.conformance"):
            self._require_reviewer(sprint_id, reviewer_shell_id)
            planner_shell_id = self._planner_shell_id(sprint_id)
            existing = self.con.execute(
                "SELECT report_id,body FROM sprint_reports "
                "WHERE sprint_id=? AND report_kind='conformance' "
                "AND idempotency_key=?",
                (sprint_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                report_id, followup_ids = self._replay_evidence(
                    existing, body, normalized, idempotency_key
                )
                final_report_id = self._replay_final_report(
                    sprint_id,
                    reviewer_shell_id=reviewer_shell_id,
                    body=final_report,
                    idempotency_key=idempotency_key,
                )
                self._replay_completion(
                    sprint_id,
                    reviewer_shell_id=reviewer_shell_id,
                    reason=reason,
                    terminal_outcome=terminal_outcome,
                    idempotency_key=idempotency_key,
                )
                notification = self._send_planner_notification(
                    sprint_id,
                    reviewer_shell_id=reviewer_shell_id,
                    planner_shell_id=planner_shell_id,
                    report_id=report_id,
                    followup_ids=followup_ids,
                    final_report_id=final_report_id,
                    reason=reason,
                    terminal_outcome=terminal_outcome,
                    idempotency_key=idempotency_key,
                )
                return ConformanceReceipt(
                    report_id,
                    followup_ids,
                    final_report_id,
                    notification.message_id,
                    self._required_wake_id(notification.wake_id),
                    True,
                    False,
                )

            lifecycle = self._lifecycle(sprint_id)
            if lifecycle != "armed":
                raise SprintInvariantError(
                    f"conformance requires an armed Sprint, not {lifecycle}"
                )

            report_id = int(
                self.con.execute(
                    "INSERT INTO sprint_reports "
                    "(sprint_id,report_kind,author_shell_id,body,idempotency_key) "
                    "VALUES (?,'conformance',?,?,?)",
                    (sprint_id, reviewer_shell_id, body, idempotency_key),
                ).lastrowid
            )
            followup_ids = []
            for index, finding in enumerate(normalized, start=1):
                self._validate_links(sprint_id, finding)
                followup_ids.append(
                    int(
                        self.con.execute(
                            "INSERT INTO sprint_followups "
                            "(sprint_id,source_report_id,severity,title,body,"
                            "spec_document_id,work_unit_id,idempotency_key) "
                            "VALUES (?,?,?,?,?,?,?,?)",
                            (
                                sprint_id,
                                report_id,
                                finding["severity"],
                                finding["title"],
                                finding["body"],
                                finding["spec_document_id"],
                                finding["work_unit_id"],
                                f"{idempotency_key}:finding:{index}",
                            ),
                        ).lastrowid
                    )
                )
            final_report_id = self._insert_final_report(
                sprint_id,
                reviewer_shell_id=reviewer_shell_id,
                body=final_report,
                idempotency_key=idempotency_key,
            )
            notification = self._send_planner_notification(
                sprint_id,
                reviewer_shell_id=reviewer_shell_id,
                planner_shell_id=planner_shell_id,
                report_id=report_id,
                followup_ids=tuple(followup_ids),
                final_report_id=final_report_id,
                reason=reason,
                terminal_outcome=terminal_outcome,
                idempotency_key=idempotency_key,
            )
            planner_wake_id = self._required_wake_id(notification.wake_id)
            self._event(
                sprint_id,
                "conformance.recorded",
                reviewer_shell_id,
                {
                    "report_id": report_id,
                    "final_report_id": final_report_id,
                    "followup_count": len(followup_ids),
                    "followup_ids": followup_ids,
                    "planner_message_id": notification.message_id,
                    "planner_wake_id": planner_wake_id,
                },
            )
            closed_conversation_ids = SprintLifecycleStore(
                self.con,
                cleanup_store=self.cleanup_store,
            ).complete_from_conformance_in_transaction(
                sprint_id,
                reviewer_shell_id,
                reason=reason,
                terminal_outcome=terminal_outcome,
                idempotency_key=idempotency_key,
                cleanup_targets=cleanup_targets,
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
        return ConformanceReceipt(
            report_id,
            tuple(followup_ids),
            final_report_id,
            notification.message_id,
            planner_wake_id,
            True,
            True,
        )

    def record_final_report(
        self,
        sprint_id: int,
        caller_shell_id: int,
        *,
        body: str,
        idempotency_key: str,
    ) -> FinalReportReceipt:
        """Commit the optional final synthesis before lifecycle completion."""
        body = self._required(body, "final report body")
        idempotency_key = self._required(
            idempotency_key, "idempotency key", maximum=220
        )
        with db_driver.write_transaction(self.con, "sprint.close.final_report"):
            return self._record_final_report_in_transaction(
                sprint_id,
                caller_shell_id,
                body=body,
                idempotency_key=idempotency_key,
            )

    def complete(
        self,
        sprint_id: int,
        caller_shell_id: int,
        *,
        reason: str,
        terminal_outcome: str,
        final_report: str | None = None,
        idempotency_key: str | None = None,
    ) -> CompletionReceipt:
        """Atomically record an optional final report and complete the Sprint."""
        reason = self._required(reason, "completion reason", maximum=2000)
        terminal_outcome = self._required(
            terminal_outcome, "terminal outcome", maximum=2000
        )
        if (final_report is None) != (idempotency_key is None):
            raise ValueError(
                "final_report and idempotency_key must be provided together"
            )
        if final_report is not None:
            final_report = self._required(final_report, "final report body")
            idempotency_key = self._required(
                idempotency_key or "", "idempotency key", maximum=220
            )
        lifecycle = self._lifecycle(sprint_id)
        cleanup_targets = (
            ()
            if lifecycle == "completed"
            else self.cleanup_store.prepare_targets(sprint_id)
        )
        closed_conversation_ids: tuple[str, ...] = ()
        with db_driver.write_transaction(self.con, "sprint.close.complete"):
            sprint = self._require_close_authority(sprint_id, caller_shell_id)
            report = None
            if final_report is not None and idempotency_key is not None:
                report = self._record_final_report_in_transaction(
                    sprint_id,
                    caller_shell_id,
                    body=final_report,
                    idempotency_key=idempotency_key,
                )
            if sprint["lifecycle"] == "completed":
                return CompletionReceipt(
                    False,
                    report.report_id if report else None,
                    report.created if report else False,
                )
            actor_kind = "fnb" if sprint["caller_flavor"] == "admin" else "planner"
            closed_conversation_ids = SprintLifecycleStore(
                self.con,
                cleanup_store=self.cleanup_store,
            ).complete_in_transaction(
                sprint_id,
                LifecycleActor(actor_kind, caller_shell_id),
                reason=reason,
                terminal_outcome=terminal_outcome,
                cleanup_targets=cleanup_targets,
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
        return CompletionReceipt(
            True,
            report.report_id if report else None,
            report.created if report else False,
        )

    def _record_final_report_in_transaction(
        self,
        sprint_id: int,
        caller_shell_id: int,
        *,
        body: str,
        idempotency_key: str,
    ) -> FinalReportReceipt:
        if not self.con.in_transaction:
            raise RuntimeError("final report requires an active transaction")
        sprint = self._require_close_authority(sprint_id, caller_shell_id)
        existing = self.con.execute(
            "SELECT report_id,body,idempotency_key FROM sprint_reports "
            "WHERE sprint_id=? AND report_kind='final' ORDER BY report_id LIMIT 1",
            (sprint_id,),
        ).fetchone()
        if existing is not None:
            if (
                existing["body"] != body
                or existing["idempotency_key"] != idempotency_key
            ):
                raise SprintInvariantError("Sprint already has a different final report")
            return FinalReportReceipt(int(existing["report_id"]), False)
        if sprint["lifecycle"] != "armed":
            raise SprintInvariantError(
                f"final report requires an armed Sprint, not {sprint['lifecycle']}"
            )
        report_id = int(
            self.con.execute(
                "INSERT INTO sprint_reports "
                "(sprint_id,report_kind,author_shell_id,body,idempotency_key) "
                "VALUES (?,'final',?,?,?)",
                (sprint_id, caller_shell_id, body, idempotency_key),
            ).lastrowid
        )
        actor_kind = "fnb" if sprint["caller_flavor"] == "admin" else "planner"
        self._event(
            sprint_id,
            "final_report.recorded",
            caller_shell_id,
            {"report_id": report_id},
            actor_kind=actor_kind,
        )
        return FinalReportReceipt(report_id, True)

    def disposition_followup(
        self,
        sprint_id: int,
        followup_id: int,
        caller_shell_id: int,
        *,
        disposition: str,
        resolution: str | None = None,
    ) -> bool:
        """Record the FnB's terminal disposition of one conformance follow-up."""
        if disposition not in {"accepted", "resolved", "dismissed"}:
            raise ValueError(
                "follow-up disposition must be accepted, resolved, or dismissed"
            )
        normalized_resolution = (resolution or "").strip()
        if disposition == "accepted" and normalized_resolution:
            raise ValueError("accepted follow-ups do not take a resolution")
        if disposition in {"resolved", "dismissed"} and not normalized_resolution:
            raise ValueError(f"{disposition} follow-ups require a resolution")
        if len(normalized_resolution) > 8000:
            raise ValueError(
                f"follow-up resolution is {len(normalized_resolution)} characters; "
                "maximum is 8000"
            )
        with db_driver.write_transaction(self.con, "sprint.followup.disposition"):
            caller = self.con.execute(
                "SELECT flavor FROM shells WHERE shell_id=? "
                "AND COALESCE(is_deleted,0)=0",
                (caller_shell_id,),
            ).fetchone()
            if caller is None or caller["flavor"] != "admin":
                raise SprintAuthorityError("only FnB may disposition Sprint follow-ups")
            followup = self.con.execute(
                "SELECT sprint_id,disposition,resolution FROM sprint_followups "
                "WHERE sprint_id=? AND followup_id=?",
                (sprint_id, followup_id),
            ).fetchone()
            if followup is None:
                raise KeyError(
                    f"unknown Sprint follow-up {followup_id} for Sprint {sprint_id}"
                )
            if followup["disposition"] != "pending":
                existing_resolution = (followup["resolution"] or "").strip()
                if (
                    followup["disposition"] == disposition
                    and existing_resolution == normalized_resolution
                ):
                    return False
                raise SprintInvariantError(
                    "Sprint follow-up already has a terminal disposition"
                )
            if disposition == "accepted":
                self.con.execute(
                    "UPDATE sprint_followups SET disposition='accepted' "
                    "WHERE followup_id=?",
                    (followup_id,),
                )
            else:
                self.con.execute(
                    "UPDATE sprint_followups SET disposition=?,resolution=?,"
                    "resolved_at=datetime('now') WHERE followup_id=?",
                    (disposition, normalized_resolution, followup_id),
                )
            self._event(
                int(followup["sprint_id"]),
                "followup.dispositioned",
                caller_shell_id,
                {
                    "followup_id": followup_id,
                    "disposition": disposition,
                    "resolution": normalized_resolution or None,
                },
                actor_kind="fnb",
            )
        return True

    def compile_evidence_packet(
        self,
        sprint_id: int,
        caller_shell_id: int,
        *,
        section_limit: int = DEFAULT_SECTION_LIMIT,
    ) -> dict[str, Any]:
        """Return bounded deterministic evidence; full history stays linked."""
        if not 1 <= section_limit <= MAX_SECTION_LIMIT:
            raise ValueError(
                f"section limit must be between 1 and {MAX_SECTION_LIMIT}"
            )
        sprint = self._require_close_authority(
            sprint_id, caller_shell_id, allow_reviewer=True
        )
        events = self._events(sprint_id)
        specs = self._spec_revisions(sprint_id)
        units = self._planned_vs_actual(sprint_id)
        prs = self._pr_outcomes(sprint_id)
        judgments = self._rows(
            "SELECT j.judgment_id,j.work_unit_id,j.kind,j.body,j.created_at,"
            "p.shell_id,s.shortname FROM sprint_judgments j "
            "JOIN sprint_participants p ON p.participant_id=j.participant_id "
            "JOIN shells s ON s.shell_id=p.shell_id "
            "WHERE j.sprint_id=? ORDER BY j.judgment_id",
            (sprint_id,),
        )
        pause_events = [
            event
            for event in events
            if any(
                marker in event["event_type"]
                for marker in ("pause", "resume", "recover", "interrupt", "drift")
            )
        ]
        anomaly_events = [
            event
            for event in events
            if any(
                marker in event["event_type"]
                for marker in ("fail", "error", "escalat", "anomal", "drift")
            )
        ]
        failed_wakes = self._rows(
            "SELECT wake_id,participant_id,state,attempt_count,last_error,"
            "created_at,failed_at FROM sprint_wake_outbox "
            "WHERE sprint_id=? AND (state='failed' OR last_error IS NOT NULL) "
            "ORDER BY wake_id",
            (sprint_id,),
        )
        unresolved_units = [
            unit
            for unit in units
            if unit["disposition"] not in {"completed", "cancelled"}
        ]
        pending_messages = self._rows(
            "SELECT message_id,to_participant_id,work_unit_id,message_kind,created_at "
            "FROM wake_message WHERE sprint_id=? AND actionable=1 "
            "AND disposition='pending' ORDER BY message_id",
            (sprint_id,),
        )
        pending_followups = self._rows(
            "SELECT followup_id,severity,title,spec_document_id,work_unit_id,"
            "disposition,created_at FROM sprint_followups WHERE sprint_id=? "
            "AND disposition='pending' ORDER BY followup_id",
            (sprint_id,),
        )
        conformance_reports = self._rows(
            "SELECT r.report_id,r.author_shell_id,s.shortname,r.body,r.created_at "
            "FROM sprint_reports r LEFT JOIN shells s "
            "ON s.shell_id=r.author_shell_id WHERE r.sprint_id=? "
            "AND r.report_kind='conformance' ORDER BY r.report_id",
            (sprint_id,),
        )
        conformance_followups = self._rows(
            "SELECT followup_id,source_report_id,severity,title,body,"
            "spec_document_id,work_unit_id,disposition,resolution,created_at,"
            "resolved_at FROM sprint_followups WHERE sprint_id=? "
            "ORDER BY followup_id",
            (sprint_id,),
        )
        final_reports = self._rows(
            "SELECT r.report_id,r.author_shell_id,s.shortname,r.body,r.created_at "
            "FROM sprint_reports r LEFT JOIN shells s "
            "ON s.shell_id=r.author_shell_id WHERE r.sprint_id=? "
            "AND r.report_kind='final' ORDER BY r.report_id",
            (sprint_id,),
        )
        spec_events = [
            event
            for event in events
            if "spec" in event["event_type"] or "drift" in event["event_type"]
        ]
        return {
            "packet_version": 1,
            "scope": {
                "sprint_id": sprint_id,
                "feature_id": int(sprint["feature_id"]),
                "feature_title": sprint["feature_title"],
                "lifecycle": sprint["lifecycle"],
                "originating_planner_shell_id": int(
                    sprint["originating_planner_shell_id"]
                ),
                "conformance_reviewer_shell_id": (
                    int(sprint["conformance_reviewer_shell_id"])
                    if sprint["conformance_reviewer_shell_id"] is not None
                    else None
                ),
                "conformance_owner_generation": int(
                    sprint["conformance_owner_generation"]
                ),
                "created_at": sprint["created_at"],
                "armed_at": sprint["armed_at"],
                "completed_at": sprint["completed_at"],
                "aborted_at": sprint["aborted_at"],
            },
            "spec_revisions": {
                "bound": specs,
                "mid_sprint_edits": self._bounded(spec_events, section_limit),
            },
            "planned_vs_actual": self._bounded(units, section_limit),
            "pr_outcomes": self._bounded(prs, section_limit),
            "judgments_and_deviations": self._bounded(judgments, section_limit),
            "pause_and_recovery": self._bounded(pause_events, section_limit),
            "wake_health": self._wake_health(sprint_id, failed_wakes, section_limit),
            "anomalies": {
                "events": self._bounded(anomaly_events, section_limit),
                "failed_wakes": self._bounded(failed_wakes, section_limit),
            },
            "conformance": {
                "reports": self._bounded(conformance_reports, section_limit),
                "followups": self._bounded(conformance_followups, section_limit),
                "final_reports": self._bounded(final_reports, section_limit),
                "missing_conformance": not conformance_reports,
                "missing_final_report": not final_reports,
            },
            "unresolved_work": {
                "work_units": self._bounded(unresolved_units, section_limit),
                "actionable_messages": self._bounded(
                    pending_messages, section_limit
                ),
                "followups": self._bounded(pending_followups, section_limit),
            },
            "full_history_links": self._history_links(sprint_id),
        }

    def timeline(self, sprint_id: int, caller_shell_id: int) -> dict[str, Any]:
        """Return the append-only full event projection behind packet links."""
        self._require_participant_or_admin(sprint_id, caller_shell_id)
        return {"sprint_id": sprint_id, "events": self._events(sprint_id)}

    def _spec_revisions(self, sprint_id: int) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT ss.document_id,ss.bound_revision_sha256,ss.approval_id,"
            "ss.bound_revision_body,ss.included_at,d.title,d.body,"
            "a.reviewer_shell_id,a.verdict,"
            "a.revision_sha256 AS reviewed_revision_sha256,"
            "a.findings_document_id,a.reviewed_at FROM sprint_specs ss "
            "JOIN documents d ON d.document_id=ss.document_id "
            "LEFT JOIN sprint_spec_approvals a ON a.approval_id=ss.approval_id "
            "WHERE ss.sprint_id=? ORDER BY ss.document_id",
            (sprint_id,),
        )
        for row in rows:
            body = row.pop("body") or ""
            row["current_revision_sha256"] = hashlib.sha256(body.encode()).hexdigest()
            bound_body = row.pop("bound_revision_body")
            row["bound_body_availability"] = (
                "available" if bound_body is not None else "unavailable_legacy_drift"
            )
            row["read_command"] = (
                f"sc sprint spec-revision --sprint {sprint_id} "
                f"--document {row['document_id']}"
            )
        return rows

    def _planned_vs_actual(self, sprint_id: int) -> list[dict[str, Any]]:
        units = self._rows(
            "SELECT work_unit_id,assigned_shell_id,reviewer_shell_id,title,"
            "expected_output,output_kind,completion_result,planned_wave,"
            "disposition,created_at,updated_at,completed_at "
            "FROM sprint_work_units WHERE sprint_id=? "
            "ORDER BY planned_wave,work_unit_id",
            (sprint_id,),
        )
        for unit in units:
            work_unit_id = int(unit["work_unit_id"])
            unit["task_ids"] = [
                int(row[0])
                for row in self.con.execute(
                    "SELECT task_id FROM sprint_work_unit_tasks "
                    "WHERE sprint_id=? AND work_unit_id=? ORDER BY task_id",
                    (sprint_id, work_unit_id),
                )
            ]
            unit["depends_on"] = [
                int(row[0])
                for row in self.con.execute(
                    "SELECT depends_on_work_unit_id "
                    "FROM sprint_work_unit_dependencies "
                    "WHERE sprint_id=? AND work_unit_id=? "
                    "ORDER BY depends_on_work_unit_id",
                    (sprint_id, work_unit_id),
                )
            ]
        return units

    def _pr_outcomes(self, sprint_id: int) -> list[dict[str, Any]]:
        prs = self._rows(
            "SELECT rp.registered_pr_id,rp.repository,rp.pr_number,"
            "rp.owner_participant_id,rp.registered_at,t.normalized_state,"
            "t.observed_head_sha,t.observed_at FROM sprint_registered_prs rp "
            "LEFT JOIN sprint_pr_transitions t ON t.transition_id=("
            "SELECT MAX(t2.transition_id) FROM sprint_pr_transitions t2 "
            "WHERE t2.registered_pr_id=rp.registered_pr_id) "
            "WHERE rp.sprint_id=? ORDER BY rp.registered_pr_id",
            (sprint_id,),
        )
        for pr in prs:
            registered_pr_id = int(pr["registered_pr_id"])
            pr["work_unit_ids"] = [
                int(row[0])
                for row in self.con.execute(
                    "SELECT work_unit_id FROM sprint_pr_work_units "
                    "WHERE sprint_id=? AND registered_pr_id=? ORDER BY work_unit_id",
                    (sprint_id, registered_pr_id),
                )
            ]
            pr["url"] = (
                f"https://github.com/{pr['repository']}/pull/{pr['pr_number']}"
            )
        return prs

    def _wake_health(
        self,
        sprint_id: int,
        failed_wakes: list[dict[str, Any]],
        limit: int,
    ) -> dict[str, Any]:
        wake_states = {
            row[0]: int(row[1])
            for row in self.con.execute(
                "SELECT state,COUNT(*) FROM sprint_wake_outbox "
                "WHERE sprint_id=? GROUP BY state ORDER BY state",
                (sprint_id,),
            )
        }
        attempt_outcomes = {
            row[0]: int(row[1])
            for row in self.con.execute(
                "SELECT a.outcome,COUNT(*) FROM sprint_wake_attempts a "
                "JOIN sprint_wake_outbox w ON w.wake_id=a.wake_id "
                "WHERE w.sprint_id=? GROUP BY a.outcome ORDER BY a.outcome",
                (sprint_id,),
            )
        }
        message_counts = {
            row[0]: int(row[1])
            for row in self.con.execute(
                "SELECT message_kind,COUNT(*) FROM wake_message "
                "WHERE sprint_id=? AND message_kind IN ('nudge','escalation') "
                "GROUP BY message_kind ORDER BY message_kind",
                (sprint_id,),
            )
        }
        expectation_counts = {
            "open": int(
                self.con.execute(
                    "SELECT COUNT(*) FROM sprint_liveness_expectations "
                    "WHERE sprint_id=? AND resolved_at IS NULL",
                    (sprint_id,),
                ).fetchone()[0]
            ),
            "resolved": int(
                self.con.execute(
                    "SELECT COUNT(*) FROM sprint_liveness_expectations "
                    "WHERE sprint_id=? AND resolved_at IS NOT NULL",
                    (sprint_id,),
                ).fetchone()[0]
            ),
        }
        return {
            "wake_states": wake_states,
            "attempt_outcomes": attempt_outcomes,
            "liveness_expectations": expectation_counts,
            "nudges": message_counts.get("nudge", 0),
            "escalations": message_counts.get("escalation", 0),
            "failed_wakes": self._bounded(failed_wakes, limit),
        }

    def _history_links(self, sprint_id: int) -> dict[str, Any]:
        conversations = self._rows(
            "SELECT c.conversation_id,link.sprint_participant_id,p.shell_id,p.role,"
            "c.conversation_scope,c.state "
            "FROM sprint_participant_conversations link "
            "JOIN sprint_participants p "
            "ON p.participant_id=link.sprint_participant_id "
            "JOIN conversations c ON c.conversation_id=link.conversation_id "
            "WHERE p.sprint_id=? ORDER BY p.participant_id,link.created_at,"
            "link.participant_conversation_id",
            (sprint_id,),
        )
        for conversation in conversations:
            conversation["url"] = (
                f"/api/conversations/{conversation['conversation_id']}"
            )
        return {
            "timeline": f"/_sc/sprint/{sprint_id}/timeline",
            "participant_conversations": conversations,
        }

    def _events(self, sprint_id: int) -> list[dict[str, Any]]:
        events = self._rows(
            "SELECT event_id,event_type,actor_kind,actor_shell_id,payload,created_at "
            "FROM sprint_events WHERE sprint_id=? ORDER BY event_id",
            (sprint_id,),
        )
        for event in events:
            event["payload"] = json.loads(event["payload"])
        return events

    def _require_close_authority(
        self,
        sprint_id: int,
        caller_shell_id: int,
        *,
        allow_reviewer: bool = False,
    ) -> sqlite3.Row:
        row = self.con.execute(
            "SELECT sp.*,r.title AS feature_title,s.flavor AS caller_flavor "
            "FROM sprints sp JOIN roadmap r ON r.feature_id=sp.feature_id "
            "JOIN shells s ON s.shell_id=? WHERE sp.sprint_id=?",
            (caller_shell_id, sprint_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown Sprint: {sprint_id}")
        if (
            int(row["originating_planner_shell_id"]) != caller_shell_id
            and row["caller_flavor"] != "admin"
        ):
            if not allow_reviewer:
                raise SprintAuthorityError(
                    "only the owning Planner or FnB may record the final report"
                )
            try:
                self._require_reviewer(sprint_id, caller_shell_id)
            except SprintAuthorityError:
                raise SprintAuthorityError(
                    "only the owning Planner, FnB, or a participating Reviewer "
                    "may compile"
                ) from None
        return row

    def _require_participant_or_admin(
        self, sprint_id: int, caller_shell_id: int
    ) -> None:
        row = self.con.execute(
            "SELECT s.flavor,EXISTS(SELECT 1 FROM sprint_participants p "
            "WHERE p.sprint_id=? AND p.shell_id=?) AS participates "
            "FROM shells s WHERE s.shell_id=?",
            (sprint_id, caller_shell_id, caller_shell_id),
        ).fetchone()
        if row is None or (not row["participates"] and row["flavor"] != "admin"):
            raise SprintAuthorityError(
                "only Sprint participants or FnB may read the timeline"
            )

    def _require_reviewer(self, sprint_id: int, shell_id: int) -> int:
        role = self.con.execute(
            "SELECT participant.participant_id,sprint.conformance_reviewer_shell_id "
            "FROM sprints sprint JOIN sprint_participants participant "
            "ON participant.sprint_id=sprint.sprint_id "
            "WHERE sprint.sprint_id=? AND participant.shell_id=? "
            "AND participant.role='reviewer'",
            (sprint_id, shell_id),
        ).fetchone()
        if (
            role is None
            or role["conformance_reviewer_shell_id"] is None
            or int(role["conformance_reviewer_shell_id"]) != shell_id
        ):
            raise SprintAuthorityError(
                "only the selected conformance Reviewer may record conformance"
            )
        return int(role["participant_id"])

    def _planner_shell_id(self, sprint_id: int) -> int:
        planner = self.con.execute(
            "SELECT sprint.originating_planner_shell_id FROM sprints sprint "
            "JOIN sprint_participants participant "
            "ON participant.sprint_id=sprint.sprint_id "
            "AND participant.shell_id=sprint.originating_planner_shell_id "
            "WHERE sprint.sprint_id=? AND participant.role='planner'",
            (sprint_id,),
        ).fetchone()
        if planner is None:
            raise SprintInvariantError(
                "Sprint has no originating Planner participant"
            )
        return int(planner["originating_planner_shell_id"])

    def _send_planner_notification(
        self,
        sprint_id: int,
        *,
        reviewer_shell_id: int,
        planner_shell_id: int,
        report_id: int,
        followup_ids: tuple[int, ...],
        final_report_id: int,
        reason: str,
        terminal_outcome: str,
        idempotency_key: str,
    ) -> Any:
        from sprint_message_delivery import SprintMessageStore

        followups = ",".join(str(value) for value in followup_ids) or "none"
        rendered_body = (
            f"Sprint {sprint_id} completed by Reviewer conformance. "
            f"conformance_report_id={report_id}; final_report_id={final_report_id}; "
            f"followup_ids={followups}; outcome={terminal_outcome}; "
            "cleanup_state=pending. Managed participant worktrees are not reusable "
            "until the engine-authored cleanup receipt reports succeeded.\n\n"
            f"Reason: {reason}"
        )
        return SprintMessageStore(self.con).send_to_shell_in_transaction(
            planner_shell_id,
            message_kind="notification",
            body=rendered_body,
            idempotency_key=f"{idempotency_key}:planner-completed",
            sender_shell_id=reviewer_shell_id,
            declared_type="re-enter",
        )

    @staticmethod
    def _required_wake_id(wake_id: int | None) -> int:
        if wake_id is None:
            raise SprintInvariantError("Planner completion notice has no delivery intent")
        return int(wake_id)

    def _lifecycle(self, sprint_id: int) -> str:
        row = self.con.execute(
            "SELECT lifecycle FROM sprints WHERE sprint_id=?", (sprint_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown Sprint: {sprint_id}")
        return str(row["lifecycle"])

    def _validate_links(self, sprint_id: int, finding: dict[str, Any]) -> None:
        document_id = finding["spec_document_id"]
        if document_id is not None and self.con.execute(
            "SELECT 1 FROM sprint_specs WHERE sprint_id=? AND document_id=?",
            (sprint_id, document_id),
        ).fetchone() is None:
            raise SprintInvariantError(
                f"spec document {document_id} is not bound to Sprint {sprint_id}"
            )
        work_unit_id = finding["work_unit_id"]
        if work_unit_id is not None and self.con.execute(
            "SELECT 1 FROM sprint_work_units WHERE sprint_id=? AND work_unit_id=?",
            (sprint_id, work_unit_id),
        ).fetchone() is None:
            raise SprintInvariantError(
                f"work unit {work_unit_id} does not belong to Sprint {sprint_id}"
            )

    def _replay_evidence(
        self,
        report: sqlite3.Row,
        body: str,
        findings: tuple[dict[str, Any], ...],
        key: str,
    ) -> tuple[int, tuple[int, ...]]:
        if report["body"] != body:
            raise SprintInvariantError(
                "conformance idempotency key was reused with different input"
            )
        rows = self._rows(
            "SELECT followup_id,severity,title,body,spec_document_id,work_unit_id "
            "FROM sprint_followups WHERE source_report_id=? ORDER BY followup_id",
            (report["report_id"],),
        )
        comparable = [
            {
                "severity": row["severity"],
                "title": row["title"],
                "body": row["body"],
                "spec_document_id": row["spec_document_id"],
                "work_unit_id": row["work_unit_id"],
            }
            for row in rows
        ]
        if comparable != list(findings):
            raise SprintInvariantError(
                f"conformance idempotency key {key!r} was reused with different findings"
            )
        return (
            int(report["report_id"]),
            tuple(int(row["followup_id"]) for row in rows),
        )

    def _insert_final_report(
        self,
        sprint_id: int,
        *,
        reviewer_shell_id: int,
        body: str,
        idempotency_key: str,
    ) -> int:
        return int(
            self.con.execute(
                "INSERT INTO sprint_reports "
                "(sprint_id,report_kind,author_shell_id,body,idempotency_key) "
                "VALUES (?,'final',?,?,?)",
                (
                    sprint_id,
                    reviewer_shell_id,
                    body,
                    f"{idempotency_key}:final-report",
                ),
            ).lastrowid
        )

    def _replay_final_report(
        self,
        sprint_id: int,
        *,
        reviewer_shell_id: int,
        body: str,
        idempotency_key: str,
    ) -> int:
        row = self.con.execute(
            "SELECT report_id,author_shell_id,body FROM sprint_reports "
            "WHERE sprint_id=? AND report_kind='final' AND idempotency_key=?",
            (sprint_id, f"{idempotency_key}:final-report"),
        ).fetchone()
        if row is None or (
            int(row["author_shell_id"]) != reviewer_shell_id or row["body"] != body
        ):
            raise SprintInvariantError(
                "conformance idempotency key was reused with a different final report"
            )
        return int(row["report_id"])

    def _replay_completion(
        self,
        sprint_id: int,
        *,
        reviewer_shell_id: int,
        reason: str,
        terminal_outcome: str,
        idempotency_key: str,
    ) -> None:
        sprint = self.con.execute(
            "SELECT lifecycle,terminal_outcome FROM sprints WHERE sprint_id=?",
            (sprint_id,),
        ).fetchone()
        event = self.con.execute(
            "SELECT payload FROM sprint_events WHERE sprint_id=? "
            "AND event_type='lifecycle.completed' AND actor_kind='participant' "
            "AND actor_shell_id=? ORDER BY event_id DESC LIMIT 1",
            (sprint_id, reviewer_shell_id),
        ).fetchone()
        expected = {
            "from": "armed",
            "reason": reason,
            "via": "conformance",
            "idempotency_key": idempotency_key,
        }
        event_payload = json.loads(event["payload"]) if event is not None else {}
        closed_conversation_ids = event_payload.pop(
            "closed_conversation_ids", []
        )
        if (
            sprint is None
            or sprint["lifecycle"] != "completed"
            or sprint["terminal_outcome"] != terminal_outcome
            or event is None
            or event_payload != expected
            or not isinstance(closed_conversation_ids, list)
            or any(
                not isinstance(conversation_id, str)
                for conversation_id in closed_conversation_ids
            )
        ):
            raise SprintInvariantError(
                "conformance idempotency key was reused with different completion"
            )

    @classmethod
    def _normalize_finding(cls, finding: Any) -> dict[str, Any]:
        if not isinstance(finding, dict):
            raise TypeError("each conformance finding must be an object")
        unknown = set(finding) - {
            "severity",
            "title",
            "body",
            "spec_document_id",
            "work_unit_id",
        }
        if unknown:
            raise ValueError(
                "unknown conformance finding field(s): " + ", ".join(sorted(unknown))
            )
        return {
            "severity": cls._required(
                str(finding.get("severity") or ""), "finding severity", 32
            ),
            "title": cls._required(
                str(finding.get("title") or ""), "finding title", 255
            ),
            "body": cls._required(
                str(finding.get("body") or ""), "finding body", 8000
            ),
            "spec_document_id": cls._optional_int(
                finding.get("spec_document_id"), "spec_document_id"
            ),
            "work_unit_id": cls._optional_int(
                finding.get("work_unit_id"), "work_unit_id"
            ),
        }

    @staticmethod
    def _optional_int(value: Any, name: str) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise TypeError(f"{name} must be a positive integer")
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a positive integer") from exc
        if result <= 0:
            raise ValueError(f"{name} must be a positive integer")
        return result

    @staticmethod
    def _required(value: str, name: str, maximum: int = 8000) -> str:
        value = value.strip()
        if not value:
            raise ValueError(f"{name} is required")
        if len(value) > maximum:
            raise ValueError(
                f"{name} is {len(value)} characters; maximum is {maximum}"
            )
        return value

    @staticmethod
    def _bounded(items: list[dict[str, Any]], limit: int) -> dict[str, Any]:
        return {
            "items": items[:limit],
            "total": len(items),
            "truncated": max(0, len(items) - limit),
        }

    def _rows(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        return [dict(row) for row in self.con.execute(sql, params)]

    def _event(
        self,
        sprint_id: int,
        event_type: str,
        shell_id: int,
        payload: dict[str, Any],
        *,
        actor_kind: str = "participant",
    ) -> None:
        self.con.execute(
            "INSERT INTO sprint_events "
            "(sprint_id,event_type,actor_kind,actor_shell_id,payload) "
            "VALUES (?,?,?,?,?)",
            (
                sprint_id,
                event_type,
                actor_kind,
                shell_id,
                json.dumps(payload, separators=(",", ":"), sort_keys=True),
            ),
        )
