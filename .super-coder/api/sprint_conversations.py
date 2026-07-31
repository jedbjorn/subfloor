"""Sprint-scoped reuse of durable browser conversations.

This module owns no transaction boundary.  Arming and later Sprint transition
services call it inside their existing write transaction so conversation
creation, Sprint linkage, and the participant's current pointer commit (or
roll back) together.  Creating a conversation is deliberately DB-only: it
does not launch a harness or enqueue a wake.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

_PURPOSES = {"work", "fix", "merge", "fallback"}


class SprintConversationError(ValueError):
    """The requested Sprint conversation transition violates its contract."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _request_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _nonblank(value: str | None, field: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SprintConversationError(f"{field} must be a non-empty string")
    value = value.strip()
    if len(value) > maximum:
        raise SprintConversationError(f"{field} exceeds {maximum} characters")
    return value


def _participant(con, participant_id: int):
    row = con.execute(
        "SELECT p.participant_id,p.sprint_id,p.shell_id,p.current_conversation_id,"
        "s.shortname FROM sprint_participants p "
        "JOIN shells s ON s.shell_id=p.shell_id "
        "WHERE p.participant_id=?",
        (participant_id,),
    ).fetchone()
    if row is None:
        raise SprintConversationError("Sprint participant does not exist")
    return row


def _linked_to_participant(con, participant_id: int, conversation_id: str) -> bool:
    return (
        con.execute(
            "SELECT 1 FROM sprint_participant_conversations "
            "WHERE sprint_participant_id=? AND conversation_id=?",
            (participant_id, conversation_id),
        ).fetchone()
        is not None
    )


def _append_created_event(
    con,
    *,
    conversation_id: str,
    participant_id: int,
    sprint_id: int,
    purpose: str,
) -> None:
    con.execute(
        "INSERT INTO conversation_events "
        "(conversation_id,sequence,event_type,payload) VALUES (?,1,?,?)",
        (
            conversation_id,
            "conversation.created",
            _canonical_json(
                {
                    "scope": "sprint",
                    "sprint_id": sprint_id,
                    "participant_id": participant_id,
                    "purpose": purpose,
                }
            ),
        ),
    )


def create_and_select(
    con,
    *,
    participant_id: int,
    owner_user_id: int,
    purpose: str,
    harness: str,
    provider: str | None,
    model: str | None,
    effort: str | None,
    worktree: str,
    title: str,
    idempotency_key: str,
    parent_conversation_id: str | None = None,
    context_packet: dict[str, Any] | None = None,
) -> str:
    """Create and select one Sprint conversation inside the caller's txn.

    ``work`` is the participant's persistent lane. ``fix`` and ``merge`` are
    fresh linked outcome conversations. ``fallback`` is a linked replacement
    route and must retain the generated context packet that made the native
    harness change intelligible.
    """
    if purpose not in _PURPOSES:
        raise SprintConversationError(f"unsupported conversation purpose: {purpose}")
    if purpose == "work" and parent_conversation_id is not None:
        raise SprintConversationError("the persistent work conversation has no parent")
    if purpose != "work" and not parent_conversation_id:
        raise SprintConversationError(f"{purpose} conversations require a parent")
    if purpose == "fallback" and not context_packet:
        raise SprintConversationError("fallback conversations require a context packet")
    if purpose != "fallback" and context_packet is not None:
        raise SprintConversationError(
            "only fallback conversations store a context packet"
        )

    harness = _nonblank(harness, "harness", maximum=64)
    worktree = _nonblank(worktree, "worktree", maximum=4096)
    title = _nonblank(title, "title", maximum=200)
    idempotency_key = _nonblank(idempotency_key, "idempotency_key", maximum=255)
    participant = _participant(con, participant_id)

    if parent_conversation_id and not _linked_to_participant(
        con, participant_id, parent_conversation_id
    ):
        raise SprintConversationError(
            "parent conversation does not belong to the Sprint participant"
        )
    if purpose == "work":
        existing_work = con.execute(
            "SELECT conversation_id FROM sprint_participant_conversations "
            "WHERE sprint_participant_id=? AND purpose='work'",
            (participant_id,),
        ).fetchone()
        if existing_work is not None:
            existing = con.execute(
                "SELECT creation_idempotency_key FROM conversations "
                "WHERE conversation_id=?",
                (existing_work["conversation_id"],),
            ).fetchone()
            if existing["creation_idempotency_key"] != idempotency_key:
                raise SprintConversationError(
                    "the participant already has a persistent work conversation"
                )

    packet_json = (
        _canonical_json(context_packet) if context_packet is not None else None
    )
    request = {
        "participant_id": participant_id,
        "purpose": purpose,
        "harness": harness,
        "provider": provider,
        "model": model,
        "effort": effort,
        "worktree": worktree,
        "title": title,
        "parent_conversation_id": parent_conversation_id,
        "context_packet": context_packet,
    }
    request_hash = _request_hash(request)
    existing = con.execute(
        "SELECT conversation_id,creation_request_hash FROM conversations "
        "WHERE owner_user_id=? AND creation_idempotency_key=?",
        (owner_user_id, idempotency_key),
    ).fetchone()
    if existing is not None:
        if existing["creation_request_hash"] != request_hash:
            raise SprintConversationError(
                "idempotency key was reused with a different request"
            )
        conversation_id = existing["conversation_id"]
        if not _linked_to_participant(con, participant_id, conversation_id):
            raise SprintConversationError(
                "idempotent conversation is linked to another participant"
            )
        con.execute(
            "UPDATE sprint_participants SET current_conversation_id=? "
            "WHERE participant_id=?",
            (conversation_id, participant_id),
        )
        return conversation_id

    conversation_id = "cv_" + uuid.uuid4().hex
    con.execute(
        "INSERT INTO conversations "
        "(conversation_id,shell_id,owner_user_id,harness,provider,model,effort,"
        "worktree,title,creation_idempotency_key,creation_request_hash,"
        "conversation_scope) VALUES (?,?,?,?,?,?,?,?,?,?,?,'sprint')",
        (
            conversation_id,
            participant["shell_id"],
            owner_user_id,
            harness,
            provider,
            model,
            effort,
            worktree,
            title,
            idempotency_key,
            request_hash,
        ),
    )
    _append_created_event(
        con,
        conversation_id=conversation_id,
        participant_id=participant_id,
        sprint_id=participant["sprint_id"],
        purpose=purpose,
    )
    con.execute(
        "INSERT INTO sprint_participant_conversations "
        "(sprint_participant_id,conversation_id,purpose,"
        "parent_conversation_id,context_packet) VALUES (?,?,?,?,?)",
        (
            participant_id,
            conversation_id,
            purpose,
            parent_conversation_id,
            packet_json,
        ),
    )
    updated = con.execute(
        "UPDATE sprint_participants SET current_conversation_id=? "
        "WHERE participant_id=?",
        (conversation_id, participant_id),
    ).rowcount
    if updated != 1:
        raise SprintConversationError("Sprint participant disappeared during creation")
    return conversation_id


def select_work(con, participant_id: int) -> str:
    """Return the participant pointer to its persistent work conversation."""
    _participant(con, participant_id)
    row = con.execute(
        "SELECT conversation_id FROM sprint_participant_conversations "
        "WHERE sprint_participant_id=? AND purpose='work'",
        (participant_id,),
    ).fetchone()
    if row is None:
        raise SprintConversationError(
            "Sprint participant has no persistent work conversation"
        )
    con.execute(
        "UPDATE sprint_participants SET current_conversation_id=? "
        "WHERE participant_id=?",
        (row["conversation_id"], participant_id),
    )
    return row["conversation_id"]
