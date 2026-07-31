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
                    "expected_output,planned_wave) VALUES (?,?,?,?,?,?)",
                    (
                        sprint_id,
                        assigned_shell_id,
                        reviewer_shell_id,
                        title,
                        expected_output,
                        planned_wave,
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
            before = self._plan_projection(unit)
            after = {
                "assigned_shell_id": assigned_shell_id,
                "reviewer_shell_id": reviewer_shell_id,
                "planned_wave": planned_wave,
                "dependency_ids": sorted(dependencies),
            }
            if before == after:
                return False
            self.con.execute(
                "UPDATE sprint_work_units SET assigned_shell_id=?,"
                "reviewer_shell_id=?,planned_wave=?,updated_at=datetime('now') "
                "WHERE work_unit_id=?",
                (
                    assigned_shell_id,
                    reviewer_shell_id,
                    planned_wave,
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
    ) -> list[int]:
        """Record Developer completion and release newly unblocked work."""
        with db_driver.write_transaction(self.con, "sprint.work_unit.complete"):
            unit = self._unit(sprint_id, work_unit_id)
            if int(unit["assigned_shell_id"]) != shell_id:
                raise SprintAuthorityError("only the assigned Developer owns completion")
            if unit["disposition"] == "completed":
                return []
            if unit["disposition"] not in EDITING_UNIT_DISPOSITIONS:
                raise SprintInvariantError(
                    f"cannot complete work unit from {unit['disposition']}"
                )
            self.con.execute(
                "UPDATE sprint_work_units SET disposition='completed',"
                "completed_at=datetime('now'),updated_at=datetime('now') "
                "WHERE work_unit_id=?",
                (work_unit_id,),
            )
            self._event(
                sprint_id,
                "work_unit.completed",
                shell_id,
                {"work_unit_id": work_unit_id},
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

    def complete_from_merge_in_transaction(
        self,
        sprint_id: int,
        work_unit_ids: Iterable[int],
        *,
        transition_key: str,
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
        if sprint["lifecycle"] != "armed":
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
            changed = True
            if unit["disposition"] != "merge_ready":
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
        if not changed:
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
        sprint_participant_chats.select_work(
            self.con, int(unit["participant_id"])
        )
        generation = int(
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_messages "
                "WHERE work_unit_id=? AND message_kind='work_assignment'",
                (unit_id,),
            ).fetchone()[0]
        ) + 1
        key = (
            f"sprint:{sprint_id}:work-unit:{unit_id}:assignment:{generation}"
        )
        message_id = int(
            self.con.execute(
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
        )
        pending = self.con.execute(
            "SELECT wake_id FROM sprint_wake_outbox "
            "WHERE sprint_id=? AND participant_id=? AND state='pending'",
            (sprint_id, unit["participant_id"]),
        ).fetchone()
        wake_id = (
            int(pending["wake_id"])
            if pending is not None
            else int(
                self.con.execute(
                    "INSERT INTO sprint_wake_outbox "
                    "(sprint_id,participant_id,idempotency_key) VALUES (?,?,?)",
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
