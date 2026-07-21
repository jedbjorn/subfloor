#!/usr/bin/env python3
"""Pure binding-state rules and wake-ledger reconstruction.

Provider adapters own transport. This module owns only the common state
contract: which lifecycle moves are valid, a compare-and-set DB transition,
and reconstruction of missing wake jobs from managed bindings plus unread
messages. Callers own transactions and commits.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping


BINDING_STATES = frozenset(
    {"starting", "foreground", "idle", "dispatching", "dormant", "released", "error"}
)

# Recovery is deliberately explicit: released/error bindings cannot dispatch
# directly. Re-manage/retry moves them through starting so capability and owner
# reconciliation happens before a transport is authorized.
_NEXT_STATES: Mapping[str, frozenset[str]] = {
    "starting": frozenset({"foreground", "idle", "dormant", "released", "error"}),
    "foreground": frozenset({"idle", "dispatching", "dormant", "released", "error"}),
    "idle": frozenset({"foreground", "dispatching", "dormant", "released", "error"}),
    "dispatching": frozenset({"foreground", "idle", "dormant", "released", "error"}),
    "dormant": frozenset(
        {"starting", "foreground", "idle", "dispatching", "released", "error"}
    ),
    "released": frozenset({"starting"}),
    "error": frozenset({"starting", "released"}),
}


class SessionControlError(RuntimeError):
    """Base error for session-control state operations."""


class UnknownBindingState(SessionControlError, ValueError):
    """A caller supplied a state outside the provider-neutral contract."""


class InvalidStateTransition(SessionControlError, ValueError):
    """The requested lifecycle edge is not allowed."""


class BindingNotFound(SessionControlError):
    """The requested binding row does not exist."""


class StaleBindingState(SessionControlError):
    """Another owner changed the row before this transition could commit."""

    def __init__(self, binding_id: int, expected: str, actual: str):
        self.binding_id = binding_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"binding {binding_id} state changed: expected {expected!r}, found {actual!r}"
        )


def _require_state(state: str) -> None:
    if state not in BINDING_STATES:
        raise UnknownBindingState(f"unknown session binding state: {state!r}")


def is_transition_allowed(current: str, target: str) -> bool:
    """Return whether ``current -> target`` is a valid lifecycle edge.

    A same-state refresh is valid so a reconciler can atomically refresh
    metadata without inventing a lifecycle transition.
    """
    _require_state(current)
    _require_state(target)
    return current == target or target in _NEXT_STATES[current]


def validate_transition(current: str, target: str) -> None:
    """Raise a precise error when a lifecycle edge is invalid."""
    if not is_transition_allowed(current, target):
        raise InvalidStateTransition(
            f"invalid session binding transition: {current} -> {target}"
        )


def transition_binding(
    con: sqlite3.Connection,
    binding_id: int,
    *,
    expected: str,
    target: str,
) -> None:
    """Compare-and-set one binding state, leaving commit control to caller."""
    validate_transition(expected, target)
    cur = con.execute(
        "UPDATE shell_session_bindings "
        "SET state = ?, updated_at = datetime('now') "
        "WHERE binding_id = ? AND state = ?",
        (target, binding_id, expected),
    )
    if cur.rowcount == 1:
        return

    row = con.execute(
        "SELECT state FROM shell_session_bindings WHERE binding_id = ?", (binding_id,)
    ).fetchone()
    if row is None:
        raise BindingNotFound(f"session binding {binding_id} not found")
    raise StaleBindingState(binding_id, expected, row[0])


def reconstruct_wake_jobs(con: sqlite3.Connection) -> int:
    """Insert missing wake jobs for every unread managed-binding message.

    Existing jobs retain their state/attempt history. The returned count is
    exact for this call, including zero on an idempotent rescan. The caller
    owns commit/rollback.
    """
    before = con.total_changes
    con.execute(
        "INSERT OR IGNORE INTO session_wake_jobs (binding_id, trigger_message_id) "
        "SELECT b.binding_id, m.message_id "
        "FROM shell_session_bindings AS b "
        "JOIN shell_messages AS m ON m.to_shell_id = b.shell_id "
        "WHERE b.managed = 1 AND m.read_at IS NULL"
    )
    return con.total_changes - before
