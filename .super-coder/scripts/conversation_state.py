#!/usr/bin/env python3
"""Conversation state-machine vocabulary mirrored by migration 0132.

The database triggers are the final backstop. API and broker code import these
maps to reject a bad edge with a useful typed error before SQLite reduces it to
an IntegrityError. Keep the maps and migration trigger edges in lockstep; the
conversation schema tests walk every old/new pair through both layers.
"""
from __future__ import annotations


CONVERSATION_TRANSITIONS = {
    "idle": frozenset({"queued", "closed"}),
    "queued": frozenset({"idle", "running"}),
    "running": frozenset({"idle", "queued", "waiting", "error"}),
    "waiting": frozenset({"queued", "closed"}),
    "error": frozenset({"queued", "closed"}),
    "closed": frozenset(),
}

MESSAGE_TRANSITIONS = {
    "accepted": frozenset(
        {"queued", "running", "completed", "failed", "cancelled"}
    ),
    "queued": frozenset({"running", "failed", "cancelled"}),
    "running": frozenset({"completed", "failed", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

RUN_TRANSITIONS = {
    "leased": frozenset({"starting", "failed", "cancelled", "unknown"}),
    "starting": frozenset({"running", "failed", "cancelled", "unknown"}),
    "running": frozenset({"succeeded", "failed", "cancelled", "unknown"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "unknown": frozenset(),
}

OUTBOX_TRANSITIONS = {
    "pending": frozenset({"claimed", "cancelled"}),
    "claimed": frozenset({"pending", "dispatched", "cancelled"}),
    "dispatched": frozenset(),
    "cancelled": frozenset(),
}

MACHINES = {
    "conversation": CONVERSATION_TRANSITIONS,
    "message": MESSAGE_TRANSITIONS,
    "run": RUN_TRANSITIONS,
    "outbox": OUTBOX_TRANSITIONS,
}

INITIAL_STATES = {
    "conversation": "idle",
    "message": "accepted",
    "run": "leased",
    "outbox": "pending",
}


class ConversationStateError(ValueError):
    """A caller requested an unknown machine/state or an illegal edge."""

    def __init__(
        self,
        machine: str,
        current: str,
        target: str,
        allowed: frozenset[str],
    ) -> None:
        self.machine = machine
        self.current = current
        self.target = target
        self.allowed = allowed
        choices = ", ".join(sorted(allowed)) or "none (terminal)"
        super().__init__(
            f"illegal {machine} transition {current} -> {target}; "
            f"allowed: {choices}"
        )


def states(machine: str) -> frozenset[str]:
    """The complete state vocabulary for one conversation machine."""
    try:
        return frozenset(MACHINES[machine])
    except KeyError as exc:
        raise KeyError(f"unknown conversation state machine: {machine}") from exc


def allowed_targets(machine: str, current: str) -> frozenset[str]:
    """Legal changed-state targets. Same-state idempotent reasserts are separate."""
    try:
        transitions = MACHINES[machine]
    except KeyError as exc:
        raise KeyError(f"unknown conversation state machine: {machine}") from exc
    try:
        return transitions[current]
    except KeyError as exc:
        raise KeyError(f"unknown {machine} state: {current}") from exc


def transition_allowed(machine: str, current: str, target: str) -> bool:
    """True for a legal edge or an idempotent same-state reassertion."""
    if target not in states(machine):
        return False
    return target == current or target in allowed_targets(machine, current)


def require_transition(machine: str, current: str, target: str) -> None:
    """Raise a typed, actionable error before the DB trigger backstop fires."""
    allowed = allowed_targets(machine, current)
    if target == current:
        return
    if target not in states(machine) or target not in allowed:
        raise ConversationStateError(machine, current, target, allowed)
