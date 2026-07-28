"""Shared sprint-unit state vocabulary.

This module is deliberately dependency-free.  The Interface API validates the
board with it, the boot renderer decides which assignments remain live with it,
and the supervised reconciler uses the same terminal set for its tick.
"""

UNIT_COLUMNS = (
    "unit_id", "sprint_doc_id", "seq", "unit_title", "dev_shell_id",
    "reviewer_shell_id", "state", "depends_on", "overlap", "branch",
    "pr_number", "review_head", "assigned_at", "state_changed_at",
    "updated_at", "updated_by_shell_id",
)

_STATE_VOCABULARY = (
    ("pending", False),
    ("working", False),
    ("in_review", False),
    ("blocked", False),
    ("merged", True),
    ("cancelled", True),
)
UNIT_STATES = tuple(state for state, _terminal in _STATE_VOCABULARY)
TERMINAL_UNIT_STATES = tuple(
    state for state, terminal in _STATE_VOCABULARY if terminal
)

# The board's transition machine (moved here from the retired Interface
# stack, conductor Step 1 — this module is the state vocabulary's one home).
# Unit state is NOT moved through a generic transition() helper: the board's
# PATCH route pre-checks with check_transition() and then writes state,
# state_changed_at, updated_at and updated_by_shell_id in ONE update, so the
# move stays attributable to the planner that made it.
SPRINT_UNIT_EDGES = {
    "pending": {"working", "cancelled"},
    "working": {"in_review", "blocked", "merged", "cancelled"},
    "in_review": {"working", "blocked", "merged", "cancelled"},
    "blocked": {"working", "cancelled"},
    "merged": set(),
    "cancelled": set(),
}


class SprintTransitionError(ValueError):
    """An illegal state-machine edge, caught before the DB backstop fires."""


def check_transition(edges: dict, old: str, new: str) -> None:
    """Raise SprintTransitionError unless old -> new is a legal edge
    (a same-state no-op is always legal — the triggers agree)."""
    if new == old:
        return
    if new not in edges.get(old, ()):  # unknown old state → empty set → raise
        raise SprintTransitionError(f"illegal transition: {old} -> {new}")


def board_writer(con, sprint_doc_id: int) -> "int | None":
    """Return the one shell related to this sprint as its board writer.

    Released bindings still name the writer. Filtering on ``released_at``
    widens the fence after any of the engine's three release paths — shell
    recovery, update discard, or sprint close/freeze — from one related shell
    to a whole flavor class. A foreign planner could then move a unit into
    ``merged``, the terminal state that makes the reconciler stop watching it.
    The most recent binding therefore remains authoritative until a later
    binding records an explicit handoff.

    A sprint that has never had a binding returns ``None``. Documents carry no
    author column, so a "document author" fallback is not representable. Callers
    must not synthesize an owner from planner flavor: permission to write a
    never-bound board is not a relationship to that sprint.
    """
    row = con.execute(
        "SELECT planner_shell_id FROM sprint_planner_bindings "
        "WHERE sprint_doc_id=? "
        "ORDER BY binding_id DESC LIMIT 1",
        (sprint_doc_id,),
    ).fetchone()
    return row[0] if row is not None else None
