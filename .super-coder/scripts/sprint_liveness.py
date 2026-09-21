"""Durable Sprint work expectations and their resolution.

A database trigger creates one expectation row the moment an actionable Sprint
message is read and accepted.  This module resolves those rows when the work
they cover reaches a terminal state — a review verdict, a superseded request, a
closed or grant-bypassed merged PR, a recalled or retired lane.

There is no automatic evaluation half.  Decisions #126, #127 and #130 retired
the liveness nudge, the Planner escalation and the waking ninety-minute
stuck-CI backstop: Sprint health is a derived progress-carrier projection, and
ordinary participant silence emits nothing.  Suppressor and stuck-CI conditions
are non-waking board attention, projected from the board's own reads.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone

import db_driver


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class SprintLivenessMonitor:
    """Resolve durable work expectations inside the caller's transaction."""

    def __init__(
        self,
        con: sqlite3.Connection,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.con = con
        self.con.row_factory = sqlite3.Row
        self.now = now or (lambda: datetime.now(timezone.utc))

    def resolve(self, message_id: int, resolution: str) -> bool:
        with db_driver.write_transaction(self.con, "sprint.liveness.resolve"):
            return self.resolve_in_transaction(message_id, resolution)

    def resolve_in_transaction(self, message_id: int, resolution: str) -> bool:
        """Resolve one expectation inside the caller's active transaction."""
        if not self.con.in_transaction:
            raise RuntimeError("liveness resolution requires an active transaction")
        resolution = resolution.strip()
        if not resolution:
            raise ValueError("liveness expectation resolution is empty")
        changed = self.con.execute(
            "UPDATE sprint_liveness_expectations SET resolved_at=?,resolution=?,"
            "next_evaluation_at=NULL WHERE message_id=? AND resolved_at IS NULL",
            (_stamp(self.now()), resolution, message_id),
        ).rowcount
        return changed == 1

    def resolve_review_requests_for_work_unit_in_transaction(
        self,
        work_unit_id: int,
        resolution: str,
    ) -> tuple[int, ...]:
        """Resolve every live review expectation owned by one editing lane."""
        if not self.con.in_transaction:
            raise RuntimeError("liveness resolution requires an active transaction")
        resolution = resolution.strip()
        if not resolution:
            raise ValueError("liveness expectation resolution is empty")
        rows = self.con.execute(
            "SELECT e.message_id FROM sprint_liveness_expectations e "
            "JOIN wake_message m ON m.message_id=e.message_id "
            "WHERE m.work_unit_id=? AND m.message_kind='review_request' "
            "AND e.resolved_at IS NULL ORDER BY e.message_id",
            (work_unit_id,),
        ).fetchall()
        message_ids = tuple(int(row[0]) for row in rows)
        if not message_ids:
            return ()
        marks = ",".join("?" for _ in message_ids)
        self.con.execute(
            "UPDATE sprint_liveness_expectations SET resolved_at=?,resolution=?,"
            "next_evaluation_at=NULL "
            f"WHERE message_id IN ({marks}) AND resolved_at IS NULL",
            (_stamp(self.now()), resolution, *message_ids),
        )
        return message_ids
