"""Shared sprint-unit state vocabulary.

This module is deliberately dependency-free. The sprint board API validates the
board with it, the boot renderer decides which worker assignments remain live,
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
