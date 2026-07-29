"""Shared authoritative sprint lifecycle helpers.

The document remains the prose artifact and ``sprint_units`` remains the board.
This module owns the executable declaration identity, state, owner, and routes
introduced by migration 0123.
"""
from __future__ import annotations

import hashlib


SPRINT_STATES = (
    "needs_owner",
    "declared",
    "active",
    "closing",
    "closed",
    "aborted",
)

SPRINT_EDGES = {
    "needs_owner": {"declared", "closed", "aborted"},
    "declared": {"active", "aborted"},
    "active": {"closing", "aborted"},
    "closing": {"closed", "aborted"},
    "closed": set(),
    "aborted": set(),
}


class SprintLifecycleError(ValueError):
    """An authoritative sprint lookup or transition failed."""


def body_sha256(body: "str | None") -> str:
    """Hash the exact canonical body stored in ``documents.body``."""
    return hashlib.sha256((body or "").encode("utf-8")).hexdigest()


def split_route(route: str) -> tuple[str, str]:
    """Split one persisted ``harness/model`` route at its first slash."""
    if not isinstance(route, str):
        raise SprintLifecycleError("route must be a harness/model string")
    route = route.strip()
    if "/" not in route:
        raise SprintLifecycleError(
            f"route {route!r} must be written as harness/model"
        )
    harness, selector = route.split("/", 1)
    if not harness.strip() or not selector.strip():
        raise SprintLifecycleError(
            f"route {route!r} must contain a harness and model"
        )
    return harness.strip(), selector.strip()


def sprint_row(con, sprint_doc_id: int):
    return con.execute(
        "SELECT * FROM sprints WHERE sprint_doc_id=?",
        (sprint_doc_id,),
    ).fetchone()


def is_active_sprint(con, sprint_doc_id: int) -> bool:
    return con.execute(
        "SELECT 1 FROM sprints WHERE sprint_doc_id=? AND state='active'",
        (sprint_doc_id,),
    ).fetchone() is not None


def active_sprint_doc_ids(con) -> set[int]:
    return {
        row[0]
        for row in con.execute(
            "SELECT sprint_doc_id FROM sprints WHERE state='active'"
        )
    }


def planner_for_sprint(con, sprint_doc_id: int):
    """Resolve the one stored originating Planner; never inspect fleet size."""
    row = con.execute(
        "SELECT sh.shell_id,sh.shortname,sh.flavor "
        "FROM sprints sp JOIN shells sh "
        "ON sh.shell_id=sp.planner_shell_id "
        "WHERE sp.sprint_doc_id=? "
        "AND sh.flavor='planner' AND COALESCE(sh.is_deleted,0)=0",
        (sprint_doc_id,),
    ).fetchone()
    if row is None:
        raise SprintLifecycleError(
            f"sprint {sprint_doc_id} has no active originating Planner"
        )
    return row


def route_for_role(con, sprint_doc_id: int, role: str) -> tuple[str, str]:
    columns = {
        "planner": "planner_route",
        "dev": "dev_route",
        "reviewer": "reviewer_route",
    }
    try:
        column = columns[role]
    except KeyError as exc:
        raise SprintLifecycleError(f"unknown sprint route role: {role}") from exc
    row = con.execute(
        f"SELECT {column} FROM sprints WHERE sprint_doc_id=?",
        (sprint_doc_id,),
    ).fetchone()
    if row is None or row[0] is None:
        raise SprintLifecycleError(
            f"sprint {sprint_doc_id} has no stored {role} route"
        )
    return split_route(row[0])


def transition(con, sprint_doc_id: int, new_state: str) -> None:
    row = sprint_row(con, sprint_doc_id)
    if row is None:
        raise SprintLifecycleError(f"no sprint declaration {sprint_doc_id}")
    old_state = row["state"]
    if new_state == old_state:
        return
    if new_state not in SPRINT_EDGES.get(old_state, set()):
        raise SprintLifecycleError(
            f"illegal sprint transition: {old_state} -> {new_state}"
        )
    sets = ["state=?"]
    params: list = [new_state]
    if new_state == "active":
        sets.append("handed_off_at=datetime('now')")
    if new_state in ("closed", "aborted"):
        sets.append("closed_at=datetime('now')")
    con.execute(
        f"UPDATE sprints SET {', '.join(sets)} WHERE sprint_doc_id=?",
        (*params, sprint_doc_id),
    )
