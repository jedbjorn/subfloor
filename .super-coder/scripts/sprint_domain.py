#!/usr/bin/env python3
"""Sprints v2 lifecycle authority, arming transaction, and service gate.

This module deliberately owns no GitHub or harness effects.  It commits the
durable facts those services consume, and the ArmedServiceSwitch prevents
registered poll/dispatch callbacks from running outside an armed Sprint.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
import hashlib
import json
import sqlite3
from dataclasses import dataclass

import db_driver
import sprint_participant_chats


SPRINT_TRANSITIONS = {
    "prepared": frozenset({"armed", "aborted"}),
    "armed": frozenset({"paused", "completed", "aborted"}),
    "paused": frozenset({"armed", "aborted"}),
    "completed": frozenset(),
    "aborted": frozenset(),
}


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


def transition_allowed(current: str, target: str) -> bool:
    return (
        current in SPRINT_TRANSITIONS
        and target in SPRINT_TRANSITIONS
        and (target == current or target in SPRINT_TRANSITIONS[current])
    )


class SprintLifecycleStore:
    """Transactional lifecycle operations over one engine DB connection."""

    def __init__(self, con: sqlite3.Connection) -> None:
        self.con = con
        self.con.row_factory = sqlite3.Row

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
            planner_participant = self._validate_arm_plan(sprint)
            self._update_lifecycle(
                sprint_id,
                current="prepared",
                target="armed",
                outcome=None,
            )
            conversation_ids = sprint_participant_chats.provision_at_arming(
                self.con, sprint_id
            )
            wake_ids = self._release_initial_work(
                sprint_id,
                planner_participant_id=planner_participant,
            )
            self._event(
                sprint_id,
                "lifecycle.armed",
                actor,
                {
                    "initial_conversation_ids": conversation_ids,
                    "initial_wake_ids": wake_ids,
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
            self._event(
                sprint_id,
                f"lifecycle.{target}",
                actor,
                {"from": current, "reason": reason},
            )
        return True

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
        with db_driver.write_transaction(self.con, "sprint.wake_failure"):
            row = self.con.execute(
                "SELECT wake_id,sprint_id,state,attempt_count,claim_owner "
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
            self.con.execute(
                "UPDATE sprint_wake_outbox SET attempt_count=?,state=?,"
                "last_error=?,failed_at=CASE WHEN ? THEN datetime('now') "
                "ELSE NULL END,claim_owner=NULL,claimed_at=NULL,"
                "lease_expires_at=NULL WHERE wake_id=?",
                (
                    attempt,
                    "failed" if terminal else "pending",
                    error,
                    1 if terminal else 0,
                    wake_id,
                ),
            )
            if terminal:
                sprint = self._sprint(int(row["sprint_id"]))
                if sprint["lifecycle"] == "armed":
                    self._update_lifecycle(
                        int(row["sprint_id"]),
                        current="armed",
                        target="paused",
                        outcome=None,
                    )
                    body = json.dumps(
                        {
                            "reason": "wake_delivery_exhausted",
                            "wake_id": wake_id,
                            "attempts": 3,
                            "last_error": error,
                        },
                        sort_keys=True,
                    )
                    self.con.execute(
                        "INSERT INTO sprint_reports "
                        "(sprint_id,report_kind,body) VALUES (?,'pause',?)",
                        (int(row["sprint_id"]), body),
                    )
                    self._event(
                        int(row["sprint_id"]),
                        "lifecycle.paused",
                        LifecycleActor("system"),
                        {
                            "reason": "wake_delivery_exhausted",
                            "wake_id": wake_id,
                        },
                    )
        return attempt

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

    def _validate_arm_plan(self, sprint: sqlite3.Row) -> int:
        sprint_id = int(sprint["sprint_id"])
        if int(sprint["merge_grant_enabled"]) != 1:
            raise SprintInvariantError("arming requires a committed merge grant")
        invalid_specs = self.con.execute(
            "SELECT COUNT(*) FROM sprint_specs ss "
            "JOIN sprint_spec_approvals a ON a.approval_id=ss.approval_id "
            "JOIN documents d ON d.document_id=ss.document_id "
            "WHERE ss.sprint_id=? AND "
            "(a.verdict<>'pass' OR a.document_id<>ss.document_id "
            "OR a.revision_sha256<>ss.bound_revision_sha256 "
            "OR d.kind<>'spec' OR d.feature_id<>?)",
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
        return int(planner[0])

    def _release_initial_work(
        self,
        sprint_id: int,
        *,
        planner_participant_id: int,
    ) -> list[int]:
        units = self.con.execute(
            "SELECT u.work_unit_id,u.assigned_shell_id,u.title,u.expected_output,"
            "p.participant_id FROM sprint_work_units u "
            "JOIN sprint_participants p ON p.sprint_id=u.sprint_id "
            "AND p.shell_id=u.assigned_shell_id AND p.role='developer' "
            "WHERE u.sprint_id=? AND u.disposition='planned' "
            "AND NOT EXISTS (SELECT 1 FROM sprint_work_unit_dependencies d "
            "WHERE d.work_unit_id=u.work_unit_id) "
            "ORDER BY u.planned_wave,u.work_unit_id",
            (sprint_id,),
        ).fetchall()
        wake_ids: list[int] = []
        for unit in units:
            unit_id = int(unit["work_unit_id"])
            key = f"sprint:{sprint_id}:work-unit:{unit_id}:assignment"
            message_id = self.con.execute(
                "INSERT INTO sprint_messages "
                "(sprint_id,from_participant_id,to_participant_id,work_unit_id,"
                "message_kind,body,actionable,disposition,idempotency_key) "
                "VALUES (?,?,?,?,?,?,1,'pending',?)",
                (
                    sprint_id,
                    planner_participant_id,
                    unit["participant_id"],
                    unit_id,
                    "work_assignment",
                    f"{unit['title']}\n\n{unit['expected_output']}",
                    key,
                ),
            ).lastrowid
            pending_wake = self.con.execute(
                "SELECT wake_id FROM sprint_wake_outbox "
                "WHERE sprint_id=? AND participant_id=? AND state='pending'",
                (sprint_id, unit["participant_id"]),
            ).fetchone()
            wake_id = (
                int(pending_wake["wake_id"])
                if pending_wake is not None
                else int(
                    self.con.execute(
                        "INSERT INTO sprint_wake_outbox "
                        "(sprint_id,participant_id,idempotency_key) "
                        "VALUES (?,?,?)",
                        (sprint_id, unit["participant_id"], f"wake:{key}"),
                    ).lastrowid
                )
            )
            self.con.execute(
                "INSERT INTO sprint_wake_messages "
                "(sprint_id,wake_id,message_id) VALUES (?,?,?)",
                (sprint_id, wake_id, message_id),
            )
            self.con.execute(
                "UPDATE sprint_work_units SET disposition='ready',"
                "updated_at=datetime('now') WHERE work_unit_id=?",
                (unit_id,),
            )
            if wake_id not in wake_ids:
                wake_ids.append(wake_id)
        return wake_ids

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
