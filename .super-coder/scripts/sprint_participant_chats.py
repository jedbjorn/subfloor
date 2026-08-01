"""Sprint participant reuse of durable browser conversations.

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

import conversation_broker
import run as run_mod

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
        "SELECT p.participant_id,p.sprint_id,p.shell_id,"
        "p.persistent_conversation_id,p.current_conversation_id,"
        "s.shortname,s.flavor FROM sprint_participants p "
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


def _append_event(
    con,
    conversation_id: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    run_id: int | None = None,
) -> None:
    sequence = int(
        con.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM conversation_events "
            "WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()[0]
    )
    con.execute(
        "INSERT INTO conversation_events "
        "(conversation_id,sequence,event_type,payload,run_id) VALUES (?,?,?,?,?)",
        (
            conversation_id,
            sequence,
            event_type,
            _canonical_json(payload),
            run_id,
        ),
    )


def _cancel_queued_turns(con, conversation_id: str) -> int:
    message_ids = [
        int(row[0])
        for row in con.execute(
            "SELECT DISTINCT message_id FROM conversation_outbox "
            "WHERE conversation_id=? AND state IN ('pending','claimed')",
            (conversation_id,),
        )
    ]
    if not message_ids:
        return 0
    marks = ",".join("?" for _ in message_ids)
    cancelled = con.execute(
        "UPDATE conversation_messages SET state='cancelled',"
        "completed_at=datetime('now') "
        f"WHERE message_id IN ({marks}) "
        "AND state IN ('accepted','queued','running')",
        message_ids,
    ).rowcount
    con.execute(
        "UPDATE conversation_outbox SET state='cancelled',claim_owner=NULL,"
        "claimed_at=NULL,lease_expires_at=NULL "
        f"WHERE message_id IN ({marks}) AND state IN ('pending','claimed')",
        message_ids,
    )
    return int(cancelled)


def close_for_terminal_lifecycle(
    con,
    sprint_id: int,
    *,
    lifecycle: str,
) -> tuple[tuple[int, ...], tuple[str, ...]]:
    """Close linked conversations or persist close intent for live runs."""
    if not con.in_transaction:
        raise RuntimeError("Sprint conversation cleanup requires a transaction")
    if lifecycle not in {"completed", "aborted"}:
        raise ValueError("Sprint conversation cleanup requires a terminal lifecycle")
    rows = con.execute(
        "SELECT DISTINCT c.conversation_id,c.state "
        "FROM sprint_participant_conversations pc "
        "JOIN sprint_participants p "
        "ON p.participant_id=pc.sprint_participant_id "
        "JOIN conversations c ON c.conversation_id=pc.conversation_id "
        "WHERE p.sprint_id=? ORDER BY c.conversation_id",
        (sprint_id,),
    ).fetchall()
    run_ids: list[int] = []
    conversation_ids: list[str] = []
    now = str(con.execute("SELECT datetime('now')").fetchone()[0])
    for row in rows:
        conversation_id = str(row["conversation_id"])
        if row["state"] == "closed":
            continue
        conversation_ids.append(conversation_id)
        cancelled = _cancel_queued_turns(con, conversation_id)
        active = con.execute(
            "SELECT run_id FROM conversation_runs WHERE conversation_id=? "
            "AND state IN ('leased','starting','running') "
            "ORDER BY run_id DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        if active is not None:
            run_id = int(active["run_id"])
            run_ids.append(run_id)
            close_requested = con.execute(
                "SELECT 1 FROM conversation_events WHERE conversation_id=? "
                "AND event_type='conversation.close.requested' LIMIT 1",
                (conversation_id,),
            ).fetchone()
            if close_requested is None:
                _append_event(
                    con,
                    conversation_id,
                    "conversation.close.requested",
                    {
                        "cancelled_queued_turns": cancelled,
                        "reason": f"sprint.lifecycle.{lifecycle}",
                        "sprint_id": sprint_id,
                    },
                    run_id=run_id,
                )
            conversation_broker.BrokerStore.request_interrupt_in_transaction(
                con,
                run_id,
                requested_at=now,
            )
            continue
        if row["state"] == "queued":
            con.execute(
                "UPDATE conversations SET state='idle' WHERE conversation_id=?",
                (conversation_id,),
            )
        elif row["state"] == "running":
            con.execute(
                "UPDATE conversations SET state='error' WHERE conversation_id=?",
                (conversation_id,),
            )
        con.execute(
            "UPDATE conversations SET state='closed',closed_at=?,"
            "last_activity_at=?,version=version+1 WHERE conversation_id=?",
            (now, now, conversation_id),
        )
        _append_event(
            con,
            conversation_id,
            "conversation.closed",
            {
                "cancelled_queued_turns": cancelled,
                "reason": f"sprint.lifecycle.{lifecycle}",
                "sprint_id": sprint_id,
                "state": "closed",
            },
        )
    return tuple(run_ids), tuple(conversation_ids)


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
    if purpose == "work":
        updated = con.execute(
            "UPDATE sprint_participants SET persistent_conversation_id=?,"
            "current_conversation_id=?,updated_at=datetime('now') "
            "WHERE participant_id=?",
            (conversation_id, conversation_id, participant_id),
        ).rowcount
    else:
        updated = con.execute(
            "UPDATE sprint_participants SET current_conversation_id=?,"
            "updated_at=datetime('now') WHERE participant_id=?",
            (conversation_id, participant_id),
        ).rowcount
    if updated != 1:
        raise SprintConversationError("Sprint participant disappeared during creation")
    return conversation_id


def select_work(con, participant_id: int) -> str:
    """Return the participant pointer to its persistent work conversation."""
    participant = _participant(con, participant_id)
    conversation_id = participant["persistent_conversation_id"]
    if conversation_id is None or not _linked_to_participant(
        con, participant_id, conversation_id
    ):
        raise SprintConversationError(
            "Sprint participant has no persistent work conversation"
        )
    con.execute(
        "UPDATE sprint_participants SET current_conversation_id=?,"
        "updated_at=datetime('now') WHERE participant_id=?",
        (conversation_id, participant_id),
    )
    return conversation_id


def create_review_outcome(
    con,
    *,
    participant_id: int,
    purpose: str,
    idempotency_key: str,
) -> str:
    """Create and select a fresh Developer fix or merge conversation."""
    if purpose not in {"fix", "merge"}:
        raise SprintConversationError("review outcomes create fix or merge chats")
    row = con.execute(
        "SELECT p.sprint_id,p.role,p.harness,p.model,p.effort,"
        "p.current_conversation_id,sh.shortname,sh.flavor,owner.user_id "
        "FROM sprint_participants p "
        "JOIN sprints s ON s.sprint_id=p.sprint_id "
        "JOIN shells sh ON sh.shell_id=p.shell_id "
        "JOIN shells owner ON owner.shell_id=s.originating_planner_shell_id "
        "WHERE p.participant_id=?",
        (participant_id,),
    ).fetchone()
    if row is None:
        raise SprintConversationError("Sprint participant does not exist")
    if row["role"] != "developer":
        raise SprintConversationError("review outcomes target a Developer")
    if row["current_conversation_id"] is None:
        raise SprintConversationError("Developer has no current Sprint conversation")
    if row["user_id"] is None:
        raise SprintConversationError("originating Planner has no browser owner")
    worktree = run_mod.shell_work_dir(row["shortname"], row["flavor"])
    return create_and_select(
        con,
        participant_id=participant_id,
        owner_user_id=int(row["user_id"]),
        purpose=purpose,
        harness=row["harness"],
        provider=run_mod.session_provider(row["harness"], row["model"]),
        model=row["model"],
        effort=row["effort"],
        worktree=str(worktree.resolve(strict=False)),
        title=(
            f"Sprint {row['sprint_id']} · {purpose.title()} · {row['shortname']}"
        ),
        idempotency_key=idempotency_key,
        parent_conversation_id=row["current_conversation_id"],
    )


def provision_at_arming(con, sprint_id: int) -> list[str]:
    """Create every participant's persistent conversation during arming.

    The caller's arming transaction owns the commit.  This function performs
    no harness or Git operation and emits no wake, so even later-wave
    participants can receive durable placeholders without consuming usage.
    """
    rows = con.execute(
        "SELECT p.participant_id,p.role,p.harness,p.model,p.effort,"
        "s.originating_planner_shell_id,sh.shortname,sh.flavor,owner.user_id "
        "FROM sprint_participants p "
        "JOIN sprints s ON s.sprint_id=p.sprint_id "
        "JOIN shells sh ON sh.shell_id=p.shell_id "
        "JOIN shells owner ON owner.shell_id=s.originating_planner_shell_id "
        "WHERE p.sprint_id=? ORDER BY p.participant_id",
        (sprint_id,),
    ).fetchall()
    if not rows:
        raise SprintConversationError("arming requires Sprint participants")
    conversation_ids = []
    for row in rows:
        if row["user_id"] is None:
            raise SprintConversationError(
                "originating Planner has no browser operator owner"
            )
        worktree = run_mod.shell_work_dir(row["shortname"], row["flavor"])
        conversation_ids.append(
            create_and_select(
                con,
                participant_id=int(row["participant_id"]),
                owner_user_id=int(row["user_id"]),
                purpose="work",
                harness=row["harness"],
                provider=run_mod.session_provider(row["harness"], row["model"]),
                model=row["model"],
                effort=row["effort"],
                worktree=str(worktree.resolve(strict=False)),
                title=f"Sprint {sprint_id} · {row['role']} · {row['shortname']}",
                idempotency_key=(
                    f"sprint:{sprint_id}:participant:{row['participant_id']}:work"
                ),
            )
        )
    return conversation_ids


def attach_live_participations(con, shells: list[dict]) -> list[dict]:
    """Attach the one live/paused Sprint pill projection to shell rows."""
    rows = con.execute(
        "SELECT p.shell_id,p.role,p.disposition,p.current_conversation_id,"
        "s.sprint_id,s.lifecycle "
        "FROM sprint_participants p "
        "JOIN sprints s ON s.sprint_id=p.sprint_id "
        "WHERE s.lifecycle IN ('armed','paused') "
        "ORDER BY s.lifecycle='armed' DESC,s.paused_at DESC,s.sprint_id DESC"
    ).fetchall()
    by_shell: dict[int, dict] = {}
    for row in rows:
        shell_id = int(row["shell_id"])
        if shell_id in by_shell:
            continue
        if row["current_conversation_id"] is None:
            raise SprintConversationError(
                "live Sprint participant has no current conversation"
            )
        by_shell[shell_id] = {
            "sprint_id": int(row["sprint_id"]),
            "lifecycle": row["lifecycle"],
            "role": row["role"],
            "disposition": row["disposition"],
            "current_conversation_id": row["current_conversation_id"],
        }
    for shell in shells:
        shell["sprint"] = by_shell.get(int(shell["shell_id"]))
    return shells


def planner_fallback_packet(con, participant_id: int, *, reason: str) -> dict[str, Any]:
    """Build a bounded durable packet for one linked Planner replacement."""
    reason = _nonblank(reason, "reason", maximum=2000)
    row = con.execute(
        "SELECT p.participant_id,p.sprint_id,p.role,p.disposition,"
        "p.harness,p.model,p.effort,p.current_conversation_id,"
        "sh.shell_id,sh.shortname,s.lifecycle,s.feature_id,r.title "
        "FROM sprint_participants p "
        "JOIN shells sh ON sh.shell_id=p.shell_id "
        "JOIN sprints s ON s.sprint_id=p.sprint_id "
        "JOIN roadmap r ON r.feature_id=s.feature_id "
        "WHERE p.participant_id=?",
        (participant_id,),
    ).fetchone()
    if row is None:
        raise SprintConversationError("Sprint participant does not exist")
    if row["role"] != "planner":
        raise SprintConversationError("fallback route replacement is Planner-only")
    if row["current_conversation_id"] is None:
        raise SprintConversationError("Planner has no current Sprint conversation")
    specs = [
        {
            "document_id": int(spec["document_id"]),
            "revision_sha256": spec["bound_revision_sha256"],
        }
        for spec in con.execute(
            "SELECT document_id,bound_revision_sha256 FROM sprint_specs "
            "WHERE sprint_id=? ORDER BY document_id",
            (row["sprint_id"],),
        )
    ]
    units = [
        {
            "work_unit_id": int(unit["work_unit_id"]),
            "title": unit["title"],
            "disposition": unit["disposition"],
        }
        for unit in con.execute(
            "SELECT work_unit_id,title,disposition FROM sprint_work_units "
            "WHERE sprint_id=? AND disposition NOT IN ('completed','cancelled') "
            "ORDER BY planned_wave,work_unit_id",
            (row["sprint_id"],),
        )
    ]
    return {
        "packet_version": 1,
        "reason": reason,
        "sprint": {
            "sprint_id": int(row["sprint_id"]),
            "feature_id": int(row["feature_id"]),
            "feature_title": row["title"],
            "lifecycle": row["lifecycle"],
        },
        "participant": {
            "participant_id": int(row["participant_id"]),
            "shell_id": int(row["shell_id"]),
            "shortname": row["shortname"],
            "role": row["role"],
            "disposition": row["disposition"],
            "previous_route": {
                "harness": row["harness"],
                "model": row["model"],
                "effort": row["effort"],
            },
            "previous_conversation_id": row["current_conversation_id"],
        },
        "bound_specs": specs,
        "open_work_units": units,
    }


def create_planner_fallback(
    con,
    *,
    participant_id: int,
    reason: str,
    harness: str,
    model: str | None,
    effort: str | None,
    idempotency_key: str,
) -> str:
    """Create and select a linked fallback route inside the caller's txn."""
    packet = planner_fallback_packet(con, participant_id, reason=reason)
    participant = _participant(con, participant_id)
    owner = con.execute(
        "SELECT planner.user_id FROM sprints s "
        "JOIN shells planner ON planner.shell_id=s.originating_planner_shell_id "
        "WHERE s.sprint_id=?",
        (participant["sprint_id"],),
    ).fetchone()
    if owner is None or owner["user_id"] is None:
        raise SprintConversationError("originating Planner has no browser owner")
    worktree = run_mod.shell_work_dir(participant["shortname"], participant["flavor"])
    return create_and_select(
        con,
        participant_id=participant_id,
        owner_user_id=int(owner["user_id"]),
        purpose="fallback",
        harness=harness,
        provider=run_mod.session_provider(harness, model),
        model=model,
        effort=effort,
        worktree=str(worktree.resolve(strict=False)),
        title=(
            f"Sprint {participant['sprint_id']} · Planner fallback · "
            f"{participant['shortname']}"
        ),
        idempotency_key=idempotency_key,
        parent_conversation_id=packet["participant"]["previous_conversation_id"],
        context_packet=packet,
    )
