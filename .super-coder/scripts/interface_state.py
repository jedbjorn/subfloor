#!/usr/bin/env python3
"""Interface state machines — app-level transition validation (spec #20).

The DB triggers in migrations/0078_interface_sessions.sql are the backstop
(RAISE(ABORT)); these maps mirror them so the API/broker can pre-check and
fail with a friendly error instead of an IntegrityError. KEEP THE TWO IN
SYNC — tests/test_interface_transitions.py walks every (old, new) pair of
every machine against BOTH layers and fails on any drift.

Edge rationales trace to spec #20's Occupancy Model / Input Broker / Wake
Delivery sections. Two deliberate readings of the spec's edge lists:
- lifecycle lost|error → ended: the spec lists no such edge, but an
  unreconciled generation must be closable after exact process absence is
  proved ("the operator closes or replaces it") — occupancy walks
  unreconciled → ended and lifecycle must be able to follow.
- lifecycle starting → ended: "definite pre-spawn failure closes it" — the
  reservation fails before any process exists, so ended is provable.
"""
from __future__ import annotations


# One definition of whether a durable Interface session can still act or needs
# reconciliation. A row is closed only when all three terminal markers agree;
# every partial/legacy combination remains active and therefore fail-closed.
def active_session_sql(alias: str = "interface_sessions") -> str:
    return (
        f"NOT ({alias}.occupancy='ended' "
        f"AND {alias}.lifecycle='ended' "
        f"AND {alias}.ended_at IS NOT NULL)"
    )


def session_is_active(occupancy: str, lifecycle: str, ended_at) -> bool:
    return not (
        occupancy == "ended"
        and lifecycle == "ended"
        and ended_at is not None
    )


# ── Edge maps (mirror the 0078 triggers exactly) ─────────────────────────────

OCCUPANCY_EDGES = {
    "reserved": {"occupied", "unreconciled", "ended"},
    "occupied": {"unreconciled", "ended"},
    "unreconciled": {"occupied", "ended"},
    "ended": set(),
}

LIFECYCLE_EDGES = {
    "starting": {"idle", "stopping", "lost", "error", "ended"},
    "idle": {"busy", "stopping", "lost"},
    "busy": {"idle", "approval", "user_input", "error", "stopping", "lost"},
    "approval": {"busy", "error", "stopping", "lost"},
    "user_input": {"busy", "error", "stopping", "lost"},
    "stopping": {"ended", "lost", "error"},
    "lost": {"ended", "stopping"},
    "error": {"ended", "stopping"},
    "ended": set(),
}

COMPOSER_EDGES = {
    "unknown": {"clean", "dirty"},
    "clean": {"dirty", "unknown"},
    "dirty": {"clean", "unknown"},
}

DELIVERY_EDGES = {
    "normal": {"delivery_unknown"},
    "delivery_unknown": {"normal"},
}

WAKE_ITEM_EDGES = {
    # queued -> done: a message handled (read) during another batch's turn
    # completes without riding a batch of its own (spec #20 Wake Delivery:
    # "new message handled in the turn: complete it").
    "queued": {"batched", "done", "quarantined", "cancelled"},
    "batched": {"queued", "submitting", "cancelled"},
    "submitting": {"queued", "running", "cancelled"},
    "running": {"done", "reconcile", "queued", "quarantined", "cancelled"},
    "reconcile": {"queued", "done", "cancelled"},
    "quarantined": {"queued", "cancelled"},
    "done": set(),
    "cancelled": set(),
}

WAKE_BATCH_EDGES = {
    "queued": {"submitting", "complete"},
    "submitting": {"queued", "running", "delivery_unknown"},
    "running": {"complete", "delivery_unknown"},
    "delivery_unknown": {"complete"},
    "complete": set(),
}

RECEIPT_EDGES = {
    "intent": {"complete", "unknown"},
    "unknown": {"reconciled"},
    "complete": set(),
    "reconciled": set(),
}

# (table, pk column, state column, edge map) — one entry per machine.
MACHINES = {
    "occupancy": ("interface_sessions", "session_id", "occupancy", OCCUPANCY_EDGES),
    "lifecycle": ("interface_sessions", "session_id", "lifecycle", LIFECYCLE_EDGES),
    "composer": ("interface_input_state", "session_id", "composer", COMPOSER_EDGES),
    "delivery": ("interface_input_state", "session_id", "delivery", DELIVERY_EDGES),
    "wake_item": ("planner_wake_items", "item_id", "state", WAKE_ITEM_EDGES),
    "wake_batch": ("planner_wake_batches", "batch_id", "state", WAKE_BATCH_EDGES),
    "receipt": ("planner_action_receipts", "receipt_id", "state", RECEIPT_EDGES),
}

# State tables carrying an updated_at column, touched on every transition.
_UPDATED_AT_TABLES = {"interface_input_state", "planner_wake_items"}


class InterfaceTransitionError(ValueError):
    """An illegal state-machine edge, caught before the DB backstop fires."""


def check(edges: dict, old: str, new: str) -> None:
    """Raise InterfaceTransitionError unless old -> new is a legal edge
    (a same-state no-op is always legal — the triggers agree)."""
    if new == old:
        return
    if new not in edges.get(old, ()):  # unknown old state → empty set → raise
        raise InterfaceTransitionError(f"illegal transition: {old} -> {new}")


def transition(con, machine: str, row_id: int, new_state: str,
               extra_sets: dict | None = None) -> str:
    """Validated state move for one row. Returns the prior state.

    Reads the current state, checks the edge (friendly error), then UPDATEs;
    the DB trigger backstops any caller that skips this helper. extra_sets
    are additional column=value pairs written in the same UPDATE (timestamps,
    reasons) — column names are internal constants, never user input.
    """
    table, pk, col, edges = MACHINES[machine]
    row = con.execute(
        f"SELECT {col} FROM {table} WHERE {pk}=?", (row_id,)
    ).fetchone()
    if row is None:
        raise InterfaceTransitionError(f"{table} row {row_id} not found")
    old = row[0]
    check(edges, old, new_state)
    sets = {col: new_state, **(extra_sets or {})}
    clause = ", ".join(f"{c}=?" for c in sets)
    if table in _UPDATED_AT_TABLES:
        clause += ", updated_at=datetime('now')"
    con.execute(
        f"UPDATE {table} SET {clause} WHERE {pk}=?",
        (*sets.values(), row_id),
    )
    return old
