"""Shared sprint-unit state vocabulary.

This module is deliberately dependency-free.  The Interface API validates the
board with it, the boot renderer decides which assignments remain live with it,
and the supervised reconciler uses the same terminal set for its tick.
"""

UNIT_COLUMNS = (
    "unit_id", "sprint_doc_id", "seq", "unit_title", "dev_shell_id",
    "reviewer_shell_id", "state", "depends_on", "overlap", "branch",
    "pr_number", "assigned_at", "state_changed_at", "updated_at",
    "updated_by_shell_id",
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
