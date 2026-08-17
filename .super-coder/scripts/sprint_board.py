"""Read-only browser projections for the Sprints v2 FnB board.

The board is assembled from authoritative Sprint records inside one SQLite
read transaction.  It deliberately owns no lifecycle writes and performs no
GitHub, harness, message-delivery, or liveness work.
"""
from __future__ import annotations

import base64
import json
import re
import sqlite3
from collections import defaultdict
from typing import Any

import sprint_cleanup
import sprint_health
import sprint_runtime

LIFECYCLES = frozenset({"prepared", "armed", "paused", "completed", "aborted"})
UNIT_COLUMNS = {
    "completed": "done",
    "cancelled": "done",
    "in_review": "review",
    "fixing": "review",
    "merge_ready": "review",
    "active": "dev",
    "planned": "waiting",
    "ready": "waiting",
    "blocked": "blocked",
}
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_PICKUP_PAUSE_REASONS = frozenset(
    {
        "wake_pickup_unknown",
        "wake_pickup_failed",
        "wake_pickup_unread",
        "wake_pickup_evidence_invalid",
    }
)
_RECOVERY_INSTRUCTION = (
    "Inspect the pause report, repair the named route or service, then use an "
    "authorized resume."
)
_MAX_HEALTH_MESSAGE_REPLIES = 100

# Payloads are internal evidence.  Only fields with an explicit browser use
# are projected; an unknown event remains visible with an empty detail object.
_EVENT_FIELDS = {
    "sprint.declared": frozenset(
        {
            "feature_id",
            "spec_document_ids",
            "spec_approval_ids",
            "participant_shell_ids",
        }
    ),
    "sprint.delivery_terminal": frozenset(
        {
            "terminal_count",
            "completed_count",
            "cancelled_count",
            "conformance_reviewer_shell_id",
            "conformance_owner_generation",
        }
    ),
    "sprint.cleanup_scheduled": frozenset(
        {
            "aggregate_state",
            "artifact_target_ids",
            "target_count",
            "worktree_target_ids",
        }
    ),
    "sprint.cleanup_adopted": frozenset(
        {"aggregate_state", "request_kind", "target_count", "target_ids"}
    ),
    "sprint.cleanup_requeued": frozenset(
        {"aggregate_state", "request_kind", "target_count", "target_ids"}
    ),
    "sprint.cleanup_failed": frozenset(
        {
            "aggregate_state",
            "attempt_count",
            "cleanup_target_id",
            "claim_generation",
            "error_code",
            "path_label",
            "target_kind",
        }
    ),
    "sprint.cleanup_completed": frozenset(
        {"aggregate_state", "succeeded_count", "target_count"}
    ),
    "lifecycle.armed": frozenset(
        {
            "from",
            "reason",
            "initial_conversation_ids",
            "initial_wake_ids",
            "reconciled",
            "dispatched_wake_ids",
        }
    ),
    "lifecycle.paused": frozenset(
        {
            "from",
            "reason",
            "report_id",
            "interrupt_run_ids",
            "wake_id",
            "attempts",
            "code",
            "eligible_reviewer_shell_ids",
            "recovery_command",
        }
    ),
    "lifecycle.aborted": frozenset(
        {"from", "reason", "report_id", "interrupt_run_ids"}
    ),
    "lifecycle.completed": frozenset({"from", "reason"}),
    "lifecycle.reconciled": frozenset(
        {
            "trigger",
            "requeued_wake_ids",
            "projected_work_unit_ids",
            "resolved_review_message_ids",
            "spec_drift",
            "anomalies",
        }
    ),
    "coordinate_mode.enabled": frozenset({"conversation_id", "reason"}),
    "coordinate_mode.cleared": frozenset({"reason"}),
    "conformance_owner.changed": frozenset(
        {"previous_shell_id", "new_shell_id", "generation", "reason"}
    ),
    "conformance_owner.required": frozenset(
        {"code", "eligible_reviewer_shell_ids", "recovery_command"}
    ),
    "pause.interrupt_delivery_failed": frozenset({"run_id"}),
    "work_unit.created": frozenset(
        {
            "work_unit_id",
            "assigned_shell_id",
            "reviewer_shell_id",
            "task_ids",
            "dependency_ids",
            "planned_wave",
            "output_kind",
        }
    ),
    "work_unit.replanned": frozenset({"work_unit_id", "before", "after"}),
    "work_unit.recalled": frozenset(
        {"work_unit_id", "before", "after", "assignment_message_ids", "reason"}
    ),
    "work_unit.ready": frozenset({"work_unit_id", "message_id", "wake_id"}),
    "work_unit.accepted": frozenset({"work_unit_id", "message_id"}),
    "work_unit.completed": frozenset(
        {"work_unit_id", "result", "output_kind", "source", "transition_key"}
    ),
    "work_unit.cancelled": frozenset({"work_unit_id", "reason"}),
    "participant.route_changed": frozenset(
        {"participant_id", "shell_id", "role", "before", "after"}
    ),
    "participant.route_bound": frozenset(
        {
            "binding_id",
            "participant_id",
            "route_revision",
            "binding_digest",
            "control_state",
            "effective_effort",
        }
    ),
    "participant.route_revised": frozenset(
        {
            "binding_id",
            "participant_id",
            "route_revision",
            "binding_digest",
            "control_state",
            "before_binding_digest",
        }
    ),
    "review.requested": frozenset(
        {
            "work_unit_id",
            "registered_pr_id",
            "message_id",
            "head_sha",
            "previous_head_sha",
            "source",
        }
    ),
    "review.approved": frozenset(
        {"work_unit_id", "registered_pr_id", "message_id", "conversation_id", "head_sha"}
    ),
    "review.changes_requested": frozenset(
        {"work_unit_id", "registered_pr_id", "message_id", "conversation_id", "head_sha"}
    ),
    "review.request_invalidated": frozenset(
        {
            "work_unit_id",
            "registered_pr_id",
            "invalidated_message_id",
            "head_sha",
            "previous_head_sha",
        }
    ),
    "review.approval_invalidated": frozenset(
        {
            "work_unit_id",
            "registered_pr_id",
            "invalidated_message_id",
            "head_sha",
            "previous_head_sha",
            "transition_key",
        }
    ),
    "merge.authorized": frozenset(
        {"work_unit_id", "registered_pr_id", "pr_number", "head_sha"}
    ),
    "merge.grant_bypassed": frozenset({"work_unit_id", "before", "transition_key"}),
    "conformance.recorded": frozenset({"report_id", "followup_count", "followup_ids"}),
    "final_report.recorded": frozenset({"report_id"}),
    "followup.dispositioned": frozenset({"followup_id", "disposition", "resolution"}),
    "spec.body_edited": frozenset(
        {
            "document_id",
            "feature_id",
            "before_sha256",
            "after_sha256",
            "editor_surface",
            "editor_shell_id",
            "editor_shortname",
            "authority",
            "notification_state",
        }
    ),
    "spec.edit_notification_unavailable": frozenset(
        {"document_id", "edit_event_id", "planner_shell_id"}
    ),
    "wake.pickup_exhausted": frozenset(
        {
            "sprint_id",
            "participant_id",
            "shell",
            "role",
            "work_unit_id",
            "message_id",
            "wake_id",
            "conversation_id",
            "run_state",
            "error_code",
            "failure_class",
            "attempt_count",
        }
    ),
    "wake.requeued": frozenset({"failed_wake_id", "replacement_wake_id"}),
    "liveness.nudged": frozenset(
        {"expectation_message_id", "silence_episode", "nudge_message_id"}
    ),
    "liveness.sanctioned_quiet": frozenset(
        {
            "expectation_message_id",
            "silence_episode",
            "suppressor_kind",
            "evidence_key",
        }
    ),
    "liveness.ci_stalled": frozenset(
        {
            "registered_pr_id",
            "transition_key",
            "backstop_message_id",
        }
    ),
    "liveness.escalated": frozenset(
        {
            "expectation_message_id",
            "silence_episode",
            "escalation_message_id",
            "planner_delivery_route",
        }
    ),
    "liveness.escalation_delivery_unavailable": frozenset(
        {
            "expectation_message_id",
            "silence_episode",
            "escalation_message_id",
            "planner_delivery_route",
        }
    ),
    "pr.registered": frozenset(
        {"registered_pr_id", "repository", "pr_number", "work_unit_ids"}
    ),
    "pr.registration_reconciled": frozenset(
        {
            "from_sprint_id",
            "from_work_unit_ids",
            "head_sha",
            "merge_sha",
            "normalized_state",
            "pr_number",
            "reason",
            "registered_pr_id",
            "repository",
            "to_sprint_id",
            "to_work_unit_id",
        }
    ),
    "pr.transition": frozenset(
        {"registered_pr_id", "transition_id", "normalized_state"}
    ),
    "pr.no_checks_observed": frozenset(
        {
            "registered_pr_id",
            "subscription_id",
            "transition_id",
            "observed_head_sha",
        }
    ),
    "pr.poll_failed": frozenset(
        {"registered_pr_id", "pr_number", "failure_count", "backoff_seconds", "trigger"}
    ),
}


class ProjectionError(ValueError):
    """A browser request cannot be projected safely."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)


def pickup_projection(
    con: sqlite3.Connection,
    sprint_id: int,
    *,
    requeued_wake_ids: tuple[int, ...] = (),
    include_current_requeue: bool = False,
    include_exhausted: bool = True,
) -> dict[str, Any]:
    """Project monitor action or the board's current bounded pickup state."""
    sprint = con.execute(
        "SELECT lifecycle FROM sprints WHERE sprint_id=?",
        (sprint_id,),
    ).fetchone()
    if sprint is None:
        raise ProjectionError(404, "sprint_not_found", "Sprint not found")
    pause_reason = None
    exhausted = None
    if str(sprint["lifecycle"]) == "paused":
        paused = con.execute(
            "SELECT json_extract(payload,'$.reason') reason "
            "FROM sprint_events WHERE sprint_id=? "
            "AND event_type='lifecycle.paused' ORDER BY event_id DESC LIMIT 1",
            (sprint_id,),
        ).fetchone()
        candidate = str(paused["reason"]) if paused and paused["reason"] else None
        if candidate in _PICKUP_PAUSE_REASONS:
            pause_reason = candidate
            event = con.execute(
                "SELECT payload FROM sprint_events WHERE sprint_id=? "
                "AND event_type='wake.pickup_exhausted' "
                "ORDER BY event_id DESC LIMIT 1",
                (sprint_id,),
            ).fetchone()
            if event is not None:
                try:
                    payload = json.loads(event["payload"])
                except (TypeError, ValueError):
                    payload = {}
                allowed = _EVENT_FIELDS["wake.pickup_exhausted"]
                exhausted = {
                    key: payload[key]
                    for key in allowed
                    if key in payload
                }
                exhausted["recovery_instruction"] = _RECOVERY_INSTRUCTION

    current_requeues = tuple(sorted(set(requeued_wake_ids)))
    if include_current_requeue and pause_reason is None and not current_requeues:
        current_requeues = tuple(
            int(row[0])
            for row in con.execute(
                "SELECT DISTINCT CAST(json_extract(e.payload,'$.replacement_wake_id') "
                "AS INTEGER) replacement_wake_id FROM sprint_events e "
                "JOIN sprint_wake_outbox w ON w.wake_id=CAST("
                "json_extract(e.payload,'$.replacement_wake_id') AS INTEGER) "
                "WHERE e.sprint_id=? AND e.event_type='wake.requeued' "
                "AND w.state IN ('pending','delivering') "
                "AND EXISTS (SELECT 1 FROM sprint_wake_messages wm "
                "JOIN wake_message m ON m.message_id=wm.message_id "
                "WHERE wm.wake_id=w.wake_id AND m.read_at IS NULL) "
                "ORDER BY replacement_wake_id LIMIT 100",
                (sprint_id,),
            )
        )
    action = "paused" if pause_reason else "requeued" if current_requeues else "none"
    result: dict[str, Any] = {
        "action": action,
        "requeued_wake_ids": list(current_requeues),
        "pause_reason": pause_reason,
    }
    if include_exhausted and exhausted is not None:
        result["exhausted"] = exhausted
    return result


def _cursor_encode(kind: str, values: list[Any]) -> str:
    raw = json.dumps(
        {"v": 1, "kind": kind, "values": values},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _cursor_decode(raw: str | None, kind: str, size: int) -> list[Any] | None:
    if not raw:
        return None
    try:
        padding = "=" * (-len(raw) % 4)
        value = json.loads(base64.urlsafe_b64decode(raw + padding))
    except Exception as exc:
        raise ProjectionError(422, "cursor_invalid", f"invalid {kind} cursor") from exc
    if not isinstance(value, dict):
        raise ProjectionError(422, "cursor_invalid", f"invalid {kind} cursor")
    values = value.get("values")
    if value.get("v") != 1 or value.get("kind") != kind or not isinstance(values, list):
        raise ProjectionError(422, "cursor_invalid", f"invalid {kind} cursor")
    if len(values) != size:
        raise ProjectionError(422, "cursor_invalid", f"invalid {kind} cursor")
    if (
        not isinstance(values[0], str)
        or not values[0]
        or any(not isinstance(item, int) or isinstance(item, bool) for item in values[1:])
    ):
        raise ProjectionError(422, "cursor_invalid", f"invalid {kind} cursor")
    return values


def parse_limit(raw: str | None, *, default: int = 50) -> int:
    try:
        value = default if raw is None else int(raw)
    except (TypeError, ValueError) as exc:
        raise ProjectionError(422, "validation_error", "limit must be an integer") from exc
    if not 1 <= value <= 100:
        raise ProjectionError(
            422,
            "validation_error",
            "limit must be between 1 and 100",
            {"field": "limit"},
        )
    return value


def parse_work_unit_id(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ProjectionError(
            422, "validation_error", "work_unit_id must be a positive integer"
        ) from exc
    if value <= 0:
        raise ProjectionError(
            422, "validation_error", "work_unit_id must be a positive integer"
        )
    return value


def _pr_url(repository: str, number: int) -> str | None:
    if not _REPOSITORY.fullmatch(repository):
        return None
    return f"https://github.com/{repository}/pull/{number}"


class SprintBoardProjection:
    """Assemble bounded FnB board resources from one SQLite connection."""

    def __init__(self, con: sqlite3.Connection) -> None:
        self.con = con
        self.con.row_factory = sqlite3.Row

    def list_sprints(
        self,
        *,
        lifecycle: str | None,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        if lifecycle is not None and lifecycle not in LIFECYCLES:
            raise ProjectionError(
                422,
                "validation_error",
                "lifecycle must be an exact Sprint v2 lifecycle",
                {"field": "lifecycle", "allowed": sorted(LIFECYCLES)},
            )
        after = _cursor_decode(cursor, "sprints", 2)
        where = []
        params: list[Any] = []
        if lifecycle is not None:
            where.append("sp.lifecycle=?")
            params.append(lifecycle)
        if after is not None:
            where.append("(sp.created_at < ? OR (sp.created_at=? AND sp.sprint_id<?))")
            params.extend((after[0], after[0], after[1]))
        clause = " WHERE " + " AND ".join(where) if where else ""
        rows = self.con.execute(
            "SELECT sp.sprint_id,sp.feature_id,r.title feature_title,"
            "sp.originating_planner_shell_id,planner.shortname planner_shortname,"
            "sp.conformance_reviewer_shell_id,sp.conformance_owner_generation,"
            "owner.shortname conformance_owner_shortname,"
            "sp.lifecycle,sp.created_at,sp.armed_at,sp.paused_at,sp.completed_at,"
            "sp.aborted_at,sp.terminal_outcome,"
            "SUM(CASE WHEN u.disposition IN ('completed','cancelled') THEN 1 ELSE 0 END) done_count,"
            "SUM(CASE WHEN u.disposition IN ('in_review','fixing','merge_ready') THEN 1 ELSE 0 END) review_count,"
            "SUM(CASE WHEN u.disposition='active' THEN 1 ELSE 0 END) dev_count,"
            "SUM(CASE WHEN u.disposition IN ('planned','ready') THEN 1 ELSE 0 END) waiting_count,"
            "SUM(CASE WHEN u.disposition='blocked' THEN 1 ELSE 0 END) blocked_count "
            "FROM sprints sp JOIN roadmap r ON r.feature_id=sp.feature_id "
            "JOIN shells planner ON planner.shell_id=sp.originating_planner_shell_id "
            "LEFT JOIN shells owner ON owner.shell_id=sp.conformance_reviewer_shell_id "
            "LEFT JOIN sprint_work_units u ON u.sprint_id=sp.sprint_id"
            f"{clause} GROUP BY sp.sprint_id ORDER BY sp.created_at DESC,sp.sprint_id DESC "
            "LIMIT ?",
            (*params, limit + 1),
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = []
        cleanup_store = sprint_cleanup.SprintCleanupTargetStore(self.con)
        for row in rows:
            cleanup = cleanup_store.project(int(row["sprint_id"]))
            items.append(
                {
                    "sprint_id": int(row["sprint_id"]),
                    "feature": {
                        "feature_id": int(row["feature_id"]),
                        "title": row["feature_title"],
                    },
                    "planner": {
                        "shell_id": int(row["originating_planner_shell_id"]),
                        "shortname": row["planner_shortname"],
                    },
                    "conformance_owner": (
                        {
                            "shell_id": int(row["conformance_reviewer_shell_id"]),
                            "shortname": row["conformance_owner_shortname"],
                            "generation": int(row["conformance_owner_generation"]),
                        }
                        if row["conformance_reviewer_shell_id"] is not None
                        else None
                    ),
                    "lifecycle": row["lifecycle"],
                    "created_at": row["created_at"],
                    "armed_at": row["armed_at"],
                    "paused_at": row["paused_at"],
                    "completed_at": row["completed_at"],
                    "aborted_at": row["aborted_at"],
                    "terminal_outcome": row["terminal_outcome"],
                    "cleanup": {
                        "aggregate_state": cleanup.aggregate_state,
                        "target_count": cleanup.target_count,
                        "pending_count": cleanup.pending_count,
                        "running_count": cleanup.running_count,
                        "succeeded_count": cleanup.succeeded_count,
                        "failed_count": cleanup.failed_count,
                    },
                    "column_counts": {
                        "done": int(row["done_count"]),
                        "review": int(row["review_count"]),
                        "dev": int(row["dev_count"]),
                        "waiting": int(row["waiting_count"]),
                        "blocked": int(row["blocked_count"]),
                    },
                }
            )
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = _cursor_encode(
                "sprints", [last["created_at"], int(last["sprint_id"])]
            )
        return {"items": items, "next_cursor": next_cursor}

    def board(self, sprint_id: int) -> dict[str, Any]:
        owns_transaction = not self.con.in_transaction
        if owns_transaction:
            self.con.execute("BEGIN")
        try:
            return self._board_in_snapshot(sprint_id)
        finally:
            if owns_transaction:
                self.con.rollback()

    def _health_messages(
        self,
        sprint_id: int,
        message_ids: set[int],
    ) -> list[dict[str, Any]]:
        """Project exact health references independently of audit-history limits."""
        if not message_ids:
            return []
        encoded_ids = json.dumps(sorted(message_ids))
        linked_replies: dict[int, list[int]] = defaultdict(list)
        linked_reply_counts: dict[int, int] = {}
        for row in self.con.execute(
            "WITH ranked AS ("
            " SELECT m.reply_to_message_id,m.message_id,"
            " ROW_NUMBER() OVER (PARTITION BY m.reply_to_message_id "
            " ORDER BY m.message_id) reply_rank,"
            " COUNT(*) OVER (PARTITION BY m.reply_to_message_id) reply_count "
            " FROM wake_message m JOIN json_each(?) refs "
            " ON m.reply_to_message_id=CAST(refs.value AS INTEGER) "
            " WHERE m.sprint_id=?) "
            "SELECT * FROM ranked WHERE reply_rank<=? "
            "ORDER BY reply_to_message_id,message_id",
            (encoded_ids, sprint_id, _MAX_HEALTH_MESSAGE_REPLIES),
        ):
            parent_id = int(row["reply_to_message_id"])
            linked_replies[parent_id].append(int(row["message_id"]))
            linked_reply_counts[parent_id] = int(row["reply_count"])

        rows = self.con.execute(
            "SELECT m.message_id,m.work_unit_id,m.message_kind,m.body,m.intent,"
            "m.requires_reply,m.reply_to_message_id,m.created_at,m.delivered_at,"
            "m.read_at,sender_shell.shell_id sender_shell_id,"
            "sender_shell.shortname sender_shortname,"
            "recipient_shell.shell_id recipient_shell_id,"
            "recipient_shell.shortname recipient_shortname "
            "FROM json_each(?) refs JOIN wake_message m "
            "ON m.message_id=CAST(refs.value AS INTEGER) "
            "LEFT JOIN sprint_participants sender "
            "ON sender.participant_id=m.from_participant_id "
            "LEFT JOIN shells sender_shell "
            "ON sender_shell.shell_id=COALESCE(sender.shell_id,m.sender_shell_id) "
            "JOIN sprint_participants recipient "
            "ON recipient.participant_id=m.to_participant_id "
            "JOIN shells recipient_shell ON recipient_shell.shell_id=recipient.shell_id "
            "WHERE m.sprint_id=? ORDER BY m.message_id",
            (encoded_ids, sprint_id),
        ).fetchall()
        projected = []
        for row in rows:
            message_id = int(row["message_id"])
            reply_ids = linked_replies[message_id]
            reply_count = linked_reply_counts.get(message_id, 0)
            work_unit_id = row["work_unit_id"]
            projected.append(
                {
                    "message_id": message_id,
                    "scope": "work_unit" if work_unit_id is not None else "sprint",
                    "work_unit_id": (
                        int(work_unit_id) if work_unit_id is not None else None
                    ),
                    "kind": row["message_kind"],
                    "body": row["body"],
                    "intent": row["intent"],
                    "requires_reply": bool(row["requires_reply"]),
                    "reply_to_message_id": (
                        int(row["reply_to_message_id"])
                        if row["reply_to_message_id"] is not None
                        else None
                    ),
                    "linked_reply_message_ids": reply_ids,
                    "linked_reply_count": reply_count,
                    "linked_replies_truncated": reply_count > len(reply_ids),
                    "created_at": row["created_at"],
                    "delivered_at": row["delivered_at"],
                    "read_at": row["read_at"],
                    "sender": (
                        {
                            "shell_id": int(row["sender_shell_id"]),
                            "shortname": row["sender_shortname"],
                        }
                        if row["sender_shell_id"] is not None
                        else None
                    ),
                    "recipient": {
                        "shell_id": int(row["recipient_shell_id"]),
                        "shortname": row["recipient_shortname"],
                    },
                }
            )
        return projected

    def _board_in_snapshot(self, sprint_id: int) -> dict[str, Any]:
        sprint = self.con.execute(
            "SELECT sp.*,r.title feature_title,planner.shortname planner_shortname "
            "FROM sprints sp JOIN roadmap r ON r.feature_id=sp.feature_id "
            "JOIN shells planner ON planner.shell_id=sp.originating_planner_shell_id "
            "WHERE sp.sprint_id=?",
            (sprint_id,),
        ).fetchone()
        if sprint is None:
            raise ProjectionError(404, "sprint_not_found", "Sprint not found")

        runtime = sprint_runtime.runtime_status(self.con)
        pickup = pickup_projection(
            self.con,
            sprint_id,
            include_current_requeue=True,
        )
        exhausted = pickup.get("exhausted")
        exhausted_unit_id = (
            int(exhausted["work_unit_id"])
            if exhausted and exhausted.get("work_unit_id") is not None
            else None
        )
        unavailable_delivery: dict[int, dict[str, Any]] = {}
        if runtime["state"] != "live":
            for row in self.con.execute(
                "SELECT m.work_unit_id,w.wake_id,w.attempt_count "
                "FROM sprint_wake_outbox w "
                "JOIN sprint_wake_messages wm ON wm.wake_id=w.wake_id "
                "JOIN wake_message m ON m.message_id=wm.message_id "
                "WHERE m.sprint_id=? AND m.work_unit_id IS NOT NULL "
                "AND m.read_at IS NULL AND w.state='pending' "
                "AND w.attempt_count=0 ORDER BY w.wake_id",
                (sprint_id,),
            ):
                unavailable_delivery.setdefault(
                    int(row["work_unit_id"]),
                    {
                        "state": "runtime_unavailable",
                        "runtime_state": runtime["state"],
                        "wake_id": int(row["wake_id"]),
                        "attempt_count": int(row["attempt_count"]),
                    },
                )

        specs = [
            {
                "document_id": int(row["document_id"]),
                "title": row["title"],
                "kind": row["kind"],
                "seq": int(row["seq"]),
                "frozen": bool(row["frozen"]),
                "bound_revision_sha256": row["bound_revision_sha256"],
            }
            for row in self.con.execute(
                "SELECT d.document_id,d.title,d.kind,d.seq,d.frozen,"
                "ss.bound_revision_sha256 FROM sprint_specs ss "
                "JOIN documents d ON d.document_id=ss.document_id "
                "WHERE ss.sprint_id=? ORDER BY d.seq,d.document_id",
                (sprint_id,),
            )
        ]
        participants = [
            {
                "participant_id": int(row["participant_id"]),
                "shell_id": int(row["shell_id"]),
                "shortname": row["shortname"],
                "display_name": row["display_name"],
                "role": row["role"],
                "harness": row["harness"],
                "model": row["model"],
                "effort": row["effort"],
                "route": row["route"],
                "binding_status": (
                    "bound"
                    if row["active_route_binding_id"] is not None
                    else (
                        "unbound-intent"
                        if sprint["lifecycle"] == "prepared" else "legacy"
                    )
                ),
                "route_contract_version": (
                    2 if row["active_route_binding_id"] is not None else 1
                ),
                "control_state": row["control_state"],
                "effective_effort": row["effective_effort"],
                "native_variant_id": row["native_variant_id"],
                "route_revision": (
                    int(row["route_revision"])
                    if row["route_revision"] is not None else None
                ),
                "binding_digest": row["binding_digest"],
                "catalogue_generation": row["catalogue_generation"],
                "disposition": row["disposition"],
                "current_conversation_id": row["current_conversation_id"],
            }
            for row in self.con.execute(
                "SELECT p.participant_id,p.shell_id,sh.shortname,sh.display_name,"
                "p.role,p.harness,p.model,p.effort,p.route,p.disposition,"
                "p.active_route_binding_id,binding.control_state,"
                "binding.effective_effort,binding.native_variant_id,"
                "binding.route_revision,binding.binding_digest,"
                "binding.catalogue_generation,"
                "active.chat_id AS current_conversation_id "
                "FROM sprint_participants p "
                "JOIN shells sh ON sh.shell_id=p.shell_id "
                "LEFT JOIN sprint_participant_route_bindings binding "
                "ON binding.binding_id=p.active_route_binding_id "
                "LEFT JOIN active_shell_chats active ON active.shell_id=p.shell_id "
                "WHERE p.sprint_id=? "
                "ORDER BY p.participant_id",
                (sprint_id,),
            )
        ]
        participant_by_shell = {row["shell_id"]: row for row in participants}

        task_rows: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in self.con.execute(
            "SELECT link.work_unit_id,t.task_id,t.title,t.status,t.seq,d.document_id,"
            "d.title document_title FROM sprint_work_unit_tasks link "
            "JOIN spec_tasks t ON t.task_id=link.task_id "
            "JOIN documents d ON d.document_id=t.document_id "
            "WHERE link.sprint_id=? ORDER BY link.work_unit_id,t.seq,t.task_id",
            (sprint_id,),
        ):
            task_rows[int(row["work_unit_id"])].append(
                {
                    "task_id": int(row["task_id"]),
                    "title": row["title"],
                    "status": row["status"],
                    "seq": int(row["seq"]),
                    "document_id": int(row["document_id"]),
                    "document_title": row["document_title"],
                }
            )

        prerequisites: dict[int, list[int]] = defaultdict(list)
        dependents: dict[int, list[int]] = defaultdict(list)
        dependencies = []
        for row in self.con.execute(
            "SELECT work_unit_id,depends_on_work_unit_id "
            "FROM sprint_work_unit_dependencies WHERE sprint_id=? "
            "ORDER BY work_unit_id,depends_on_work_unit_id",
            (sprint_id,),
        ):
            unit_id = int(row["work_unit_id"])
            upstream_id = int(row["depends_on_work_unit_id"])
            prerequisites[unit_id].append(upstream_id)
            dependents[upstream_id].append(unit_id)
            dependencies.append(
                {"work_unit_id": unit_id, "depends_on_work_unit_id": upstream_id}
            )

        prs: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in self.con.execute(
            "SELECT link.work_unit_id,pr.registered_pr_id,pr.repository,pr.pr_number,"
            "pr.registered_at,t.normalized_state,t.observed_head_sha,t.observed_at "
            "FROM sprint_pr_work_units link "
            "JOIN sprint_registered_prs pr ON pr.registered_pr_id=link.registered_pr_id "
            "LEFT JOIN sprint_pr_transitions t ON t.transition_id=("
            " SELECT latest.transition_id FROM sprint_pr_transitions latest "
            " WHERE latest.registered_pr_id=pr.registered_pr_id "
            " ORDER BY latest.transition_id DESC LIMIT 1) "
            "WHERE link.sprint_id=? ORDER BY link.work_unit_id,pr.registered_pr_id",
            (sprint_id,),
        ):
            number = int(row["pr_number"])
            repository = str(row["repository"])
            prs[int(row["work_unit_id"])].append(
                {
                    "registered_pr_id": int(row["registered_pr_id"]),
                    "repository": repository,
                    "pr_number": number,
                    "url": _pr_url(repository, number),
                    "registered_at": row["registered_at"],
                    "normalized_state": row["normalized_state"],
                    "observed_head_sha": row["observed_head_sha"],
                    "observed_at": row["observed_at"],
                }
            )

        messages: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in self.con.execute(
            "WITH ranked AS ("
            " SELECT m.*,ROW_NUMBER() OVER (PARTITION BY m.work_unit_id "
            " ORDER BY m.message_id DESC) rank FROM wake_message m "
            " WHERE m.sprint_id=? AND m.work_unit_id IS NOT NULL) "
            "SELECT ranked.*,sender.shell_id sender_shell_id,"
            "sender_shell.shortname sender_shortname,recipient.shell_id recipient_shell_id,"
            "recipient_shell.shortname recipient_shortname "
            "FROM ranked LEFT JOIN sprint_participants sender "
            "ON sender.participant_id=ranked.from_participant_id "
            "LEFT JOIN shells sender_shell ON sender_shell.shell_id=sender.shell_id "
            "JOIN sprint_participants recipient "
            "ON recipient.participant_id=ranked.to_participant_id "
            "JOIN shells recipient_shell ON recipient_shell.shell_id=recipient.shell_id "
            "WHERE ranked.rank<=100 ORDER BY ranked.work_unit_id,ranked.message_id DESC",
            (sprint_id,),
        ):
            messages[int(row["work_unit_id"])].append(
                {
                    "message_id": int(row["message_id"]),
                    "kind": row["message_kind"],
                    "body": row["body"],
                    "actionable": bool(row["actionable"]),
                    "disposition": row["disposition"],
                    "read_at": row["read_at"],
                    "created_at": row["created_at"],
                    "sender": (
                        {
                            "shell_id": int(row["sender_shell_id"]),
                            "shortname": row["sender_shortname"],
                        }
                        if row["sender_shell_id"] is not None
                        else None
                    ),
                    "recipient": {
                        "shell_id": int(row["recipient_shell_id"]),
                        "shortname": row["recipient_shortname"],
                    },
                }
            )

        health = sprint_health.SprintHealthProjection(self.con).project(sprint_id)
        health_message_ids = {
            int(ref["message_id"])
            for root in health["health"].get("root_causes", [])
            for ref in root.get("message_refs", [])
        }
        health_message_ids.update(
            int(ref["message_id"])
            for unit_health in health["work_units"].values()
            for ref in unit_health.get("message_refs", [])
        )
        health_messages = self._health_messages(sprint_id, health_message_ids)

        units = []
        counts = {name: 0 for name in ("done", "review", "dev", "waiting", "blocked")}
        for row in self.con.execute(
            "SELECT u.*,dev.shortname developer_shortname,rev.shortname reviewer_shortname "
            "FROM sprint_work_units u JOIN shells dev ON dev.shell_id=u.assigned_shell_id "
            "JOIN shells rev ON rev.shell_id=u.reviewer_shell_id "
            "WHERE u.sprint_id=? ORDER BY u.planned_wave,u.work_unit_id",
            (sprint_id,),
        ):
            unit_id = int(row["work_unit_id"])
            column = UNIT_COLUMNS[str(row["disposition"])]
            counts[column] += 1
            units.append(
                {
                    "work_unit_id": unit_id,
                    "title": row["title"],
                    "expected_output": row["expected_output"],
                    "output_kind": row["output_kind"],
                    "completion_result": row["completion_result"],
                    "planned_wave": int(row["planned_wave"]),
                    "disposition": row["disposition"],
                    "column": column,
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "completed_at": row["completed_at"],
                    "developer": {
                        "shell_id": int(row["assigned_shell_id"]),
                        "shortname": row["developer_shortname"],
                        "current_conversation_id": participant_by_shell.get(
                            int(row["assigned_shell_id"]), {}
                        ).get("current_conversation_id"),
                    },
                    "reviewer": {
                        "shell_id": int(row["reviewer_shell_id"]),
                        "shortname": row["reviewer_shortname"],
                        "current_conversation_id": participant_by_shell.get(
                            int(row["reviewer_shell_id"]), {}
                        ).get("current_conversation_id"),
                    },
                    "task_ids": [item["task_id"] for item in task_rows[unit_id]],
                    "tasks": task_rows[unit_id],
                    "prerequisite_ids": prerequisites[unit_id],
                    "dependent_ids": dependents[unit_id],
                    "pull_requests": prs[unit_id],
                    "messages": messages[unit_id],
                    "health": health["work_units"][unit_id],
                    **(
                        {"pickup": exhausted}
                        if unit_id == exhausted_unit_id
                        else {}
                    ),
                    **(
                        {"delivery": unavailable_delivery[unit_id]}
                        if unit_id in unavailable_delivery
                        else {}
                    ),
                }
            )

        feed_counts = self.con.execute(
            "SELECT (SELECT COUNT(*) FROM sprint_events WHERE sprint_id=?) event_count,"
            "(SELECT COUNT(*) FROM sprint_judgments WHERE sprint_id=?) judgment_count,"
            "(SELECT COUNT(*) FROM sprint_reports WHERE sprint_id=?) report_count",
            (sprint_id, sprint_id, sprint_id),
        ).fetchone()
        cleanup = sprint_cleanup.SprintCleanupTargetStore(self.con).project(sprint_id)
        return {
            "sprint": {
                "sprint_id": int(sprint["sprint_id"]),
                "feature": {
                    "feature_id": int(sprint["feature_id"]),
                    "title": sprint["feature_title"],
                },
                "planner": {
                    "shell_id": int(sprint["originating_planner_shell_id"]),
                    "shortname": sprint["planner_shortname"],
                },
                "conformance_owner": (
                    {
                        "shell_id": int(sprint["conformance_reviewer_shell_id"]),
                        "shortname": self.con.execute(
                            "SELECT shortname FROM shells WHERE shell_id=?",
                            (int(sprint["conformance_reviewer_shell_id"]),),
                        ).fetchone()[0],
                        "generation": int(sprint["conformance_owner_generation"]),
                    }
                    if sprint["conformance_reviewer_shell_id"] is not None
                    else None
                ),
                "lifecycle": sprint["lifecycle"],
                "created_at": sprint["created_at"],
                "armed_at": sprint["armed_at"],
                "paused_at": sprint["paused_at"],
                "completed_at": sprint["completed_at"],
                "aborted_at": sprint["aborted_at"],
                "terminal_outcome": sprint["terminal_outcome"],
                "version": int(sprint["version"]),
            },
            "specs": specs,
            "participants": participants,
            "work_units": units,
            "dependencies": dependencies,
            "column_counts": counts,
            "runtime": runtime,
            "pickup": pickup,
            "health": health["health"],
            "health_messages": health_messages,
            "cleanup": {
                "aggregate_state": cleanup.aggregate_state,
                "target_count": cleanup.target_count,
                "pending_count": cleanup.pending_count,
                "running_count": cleanup.running_count,
                "succeeded_count": cleanup.succeeded_count,
                "failed_count": cleanup.failed_count,
            },
            "feed_counts": {
                "events": int(feed_counts["event_count"]),
                "summaries": int(feed_counts["judgment_count"])
                + int(feed_counts["report_count"]),
            },
        }

    def events(
        self,
        sprint_id: int,
        *,
        limit: int,
        cursor: str | None,
        work_unit_id: int | None,
    ) -> dict[str, Any]:
        self._require_sprint(sprint_id)
        self._require_work_unit(sprint_id, work_unit_id)
        after = _cursor_decode(cursor, "events", 2)
        where = ["e.sprint_id=?"]
        params: list[Any] = [sprint_id]
        if work_unit_id is not None:
            where.append("CAST(json_extract(e.payload,'$.work_unit_id') AS INTEGER)=?")
            params.append(work_unit_id)
        if after is not None:
            where.append("(e.created_at < ? OR (e.created_at=? AND e.event_id<?))")
            params.extend((after[0], after[0], after[1]))
        rows = self.con.execute(
            "SELECT e.event_id,e.event_type,e.actor_kind,e.actor_shell_id,"
            "sh.shortname actor_shortname,e.payload,e.created_at "
            "FROM sprint_events e LEFT JOIN shells sh ON sh.shell_id=e.actor_shell_id "
            f"WHERE {' AND '.join(where)} ORDER BY e.created_at DESC,e.event_id DESC LIMIT ?",
            (*params, limit + 1),
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            allowed = _EVENT_FIELDS.get(str(row["event_type"]), frozenset())
            items.append(
                {
                    "event_id": int(row["event_id"]),
                    "type": row["event_type"],
                    "actor": {
                        "kind": row["actor_kind"],
                        "shell_id": row["actor_shell_id"],
                        "shortname": row["actor_shortname"],
                    },
                    "created_at": row["created_at"],
                    "details": {key: payload[key] for key in allowed if key in payload},
                }
            )
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = _cursor_encode(
                "events", [last["created_at"], int(last["event_id"])]
            )
        return {"items": items, "next_cursor": next_cursor}

    def summaries(
        self,
        sprint_id: int,
        *,
        limit: int,
        cursor: str | None,
        work_unit_id: int | None,
    ) -> dict[str, Any]:
        self._require_sprint(sprint_id)
        self._require_work_unit(sprint_id, work_unit_id)
        after = _cursor_decode(cursor, "summaries", 3)
        judgment_filter = " AND j.work_unit_id=?" if work_unit_id is not None else ""
        params: list[Any] = [sprint_id]
        if work_unit_id is not None:
            params.append(work_unit_id)
        reports_sql = (
            "SELECT r.created_at,0 source_rank,r.report_id record_id,'report' source,"
            "r.report_kind kind,r.body,NULL work_unit_id,r.author_shell_id shell_id,"
            "sh.shortname FROM sprint_reports r LEFT JOIN shells sh "
            "ON sh.shell_id=r.author_shell_id WHERE r.sprint_id=?"
        )
        if work_unit_id is not None:
            reports_sql = (
                "SELECT NULL created_at,0 source_rank,NULL record_id,'report' source,"
                "NULL kind,NULL body,NULL work_unit_id,NULL shell_id,NULL shortname WHERE 0"
            )
        else:
            params.append(sprint_id)
        sql = (
            "SELECT * FROM ("
            "SELECT j.created_at,1 source_rank,j.judgment_id record_id,'judgment' source,"
            "j.kind,j.body,j.work_unit_id,p.shell_id,sh.shortname "
            "FROM sprint_judgments j JOIN sprint_participants p "
            "ON p.participant_id=j.participant_id JOIN shells sh ON sh.shell_id=p.shell_id "
            f"WHERE j.sprint_id=?{judgment_filter} UNION ALL {reports_sql}) combined"
        )
        if after is not None:
            sql += (
                " WHERE (created_at < ? OR (created_at=? AND source_rank < ?) OR "
                "(created_at=? AND source_rank=? AND record_id<?))"
            )
            params.extend((after[0], after[0], after[1], after[0], after[1], after[2]))
        sql += " ORDER BY created_at DESC,source_rank DESC,record_id DESC LIMIT ?"
        params.append(limit + 1)
        rows = self.con.execute(sql, params).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [
            {
                "source": row["source"],
                "id": int(row["record_id"]),
                "kind": row["kind"],
                "body": row["body"],
                "summary": " ".join(str(row["body"]).split())[:240],
                "work_unit_id": row["work_unit_id"],
                "author": {
                    "shell_id": row["shell_id"],
                    "shortname": row["shortname"],
                },
                "created_at": row["created_at"],
            }
            for row in rows
        ]
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = _cursor_encode(
                "summaries",
                [last["created_at"], int(last["source_rank"]), int(last["record_id"])],
            )
        return {"items": items, "next_cursor": next_cursor}

    def _require_sprint(self, sprint_id: int) -> None:
        if self.con.execute(
            "SELECT 1 FROM sprints WHERE sprint_id=?", (sprint_id,)
        ).fetchone() is None:
            raise ProjectionError(404, "sprint_not_found", "Sprint not found")

    def _require_work_unit(self, sprint_id: int, work_unit_id: int | None) -> None:
        if work_unit_id is None:
            return
        row = self.con.execute(
            "SELECT sprint_id FROM sprint_work_units WHERE work_unit_id=?",
            (work_unit_id,),
        ).fetchone()
        if row is None or int(row["sprint_id"]) != sprint_id:
            raise ProjectionError(
                422,
                "work_unit_scope_mismatch",
                "work_unit_id does not belong to this Sprint",
                {"work_unit_id": work_unit_id},
            )
