"""Sprint wake-chat creation backed by the active-chat registry.

Sprint participants keep durable conversation history, but current-chat state
belongs exclusively to ``active_shell_chats``.  Callers own transaction
boundaries; New delivery closes the previous registry chat in a committed
transaction before calling ``create_wake_conversation``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

import active_chat_registry
import run as run_mod

_WAKE_ROLES = {
    "developer": ("Developer", "sprint_dev"),
    "reviewer": ("Reviewer", "sprint_rev"),
    "planner": ("Originating Planner", "sprint_pln"),
}


class SprintConversationError(ValueError):
    """The requested Sprint conversation transition violates its contract."""


class WakeConversationBusy(SprintConversationError):
    """A wake replacement lost the boundary race to another active chat."""


@dataclass(frozen=True)
class PreparedShellWake:
    shell_id: int
    owner_user_id: int
    shortname: str
    harness: str
    provider: str | None
    model: str | None
    effort: str | None
    worktree: str


def wake_prompt(sprint_id: int, role: str) -> str:
    try:
        label, skill = _WAKE_ROLES[role]
    except KeyError as exc:
        raise SprintConversationError(
            f"unsupported Sprint participant role: {role}"
        ) from exc
    return (
        f"Sprint {sprint_id} handoff for your {label} role. Load `{skill}`. "
        f"Run `sc sprint inbox --sprint {sprint_id}` now and act on the Sprint "
        f"message(s) using `{skill}`. Confirm every Sprint write succeeds before "
        "stopping. If a Sprint command failed or did not confirm its durable write, "
        "retry that command. Do not re-check the inbox otherwise — new messages "
        "arrive as their own wakes."
    )


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
    wake_id: int,
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
                    "wake_id": wake_id,
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


def prepare_shell_wake_conversation(con, shell_id: int) -> PreparedShellWake:
    """Resolve a non-Sprint shell route before any active chat is closed."""
    shell = con.execute(
        "SELECT shell_id,user_id,shortname,flavor FROM shells "
        "WHERE shell_id=? AND COALESCE(is_deleted,0)=0",
        (shell_id,),
    ).fetchone()
    if shell is None:
        raise SprintConversationError("wake receiver shell does not exist")
    if shell["user_id"] is None:
        raise SprintConversationError("wake receiver shell has no browser owner")

    prior = con.execute(
        "SELECT harness,model,effort FROM conversations WHERE shell_id=? "
        "ORDER BY created_at DESC,conversation_id DESC LIMIT 1",
        (shell_id,),
    ).fetchone()
    defaults = run_mod.flavor_defaults(con).get(shell["flavor"], {})
    harness = (
        (str(prior["harness"]) if prior is not None else None)
        or defaults.get("default_harness")
        or run_mod._configured_harness()
        or "claude"
    )
    model = (
        str(prior["model"])
        if prior is not None and prior["model"] is not None
        else defaults.get("models", {}).get(harness)
    )
    adapter = run_mod.load_adapter(harness)
    effort = (
        str(prior["effort"])
        if prior is not None and prior["effort"] is not None
        else run_mod.default_headless_effort(adapter)
    )
    try:
        run_mod.validate_headless_request(adapter, model, effort)
    except ValueError as exc:
        raise SprintConversationError(str(exc)) from exc
    worktree = run_mod.shell_work_dir(shell["shortname"], shell["flavor"])
    return PreparedShellWake(
        shell_id=int(shell["shell_id"]),
        owner_user_id=int(shell["user_id"]),
        shortname=str(shell["shortname"]),
        harness=harness,
        provider=run_mod.session_provider(harness, model),
        model=model,
        effort=effort,
        worktree=str(worktree.resolve(strict=False)),
    )


def create_shell_wake_conversation(
    con,
    *,
    wake_id: int,
    route: PreparedShellWake,
) -> str:
    """Create and register a prepared engine-wide wake chat."""
    if not con.in_transaction:
        raise RuntimeError("wake conversation creation requires a transaction")
    if active_chat_registry.get(con, route.shell_id) is not None:
        raise WakeConversationBusy("another chat became active before wake creation")

    key = f"shell:{route.shell_id}:wake:{wake_id}"
    request = {
        "effort": route.effort,
        "harness": route.harness,
        "model": route.model,
        "shell_id": route.shell_id,
        "wake_id": wake_id,
        "worktree": route.worktree,
    }
    existing = con.execute(
        "SELECT conversation_id,shell_id,state,creation_request_hash "
        "FROM conversations WHERE owner_user_id=? "
        "AND creation_idempotency_key=?",
        (route.owner_user_id, key),
    ).fetchone()
    request_hash = _request_hash(request)
    if existing is not None:
        if (
            int(existing["shell_id"]) != route.shell_id
            or existing["creation_request_hash"] != request_hash
            or existing["state"] == "closed"
        ):
            raise SprintConversationError("idempotent wake chat is not reusable")
        conversation_id = str(existing["conversation_id"])
        active_chat_registry.register(con, route.shell_id, conversation_id)
        return conversation_id

    conversation_id = "cv_" + uuid.uuid4().hex
    con.execute(
        "INSERT INTO conversations "
        "(conversation_id,shell_id,owner_user_id,harness,provider,model,effort,"
        "worktree,title,creation_idempotency_key,creation_request_hash) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            conversation_id,
            route.shell_id,
            route.owner_user_id,
            route.harness,
            route.provider,
            route.model,
            route.effort,
            route.worktree,
            f"Wake {wake_id} · {route.shortname}",
            key,
            request_hash,
        ),
    )
    _append_event(
        con,
        conversation_id,
        "conversation.created",
        {"scope": "normal", "shell_id": route.shell_id, "wake_id": wake_id},
    )
    active_chat_registry.register(con, route.shell_id, conversation_id)
    return conversation_id


def create_wake_conversation(
    con,
    *,
    wake_id: int,
    sprint_id: int,
    participant_id: int,
) -> str:
    """Create and register one Sprint wake chat after close commits."""
    if not con.in_transaction:
        raise RuntimeError("wake conversation creation requires a transaction")
    row = con.execute(
        "SELECT p.participant_id,p.sprint_id,p.shell_id,p.harness,p.model,p.effort,"
        "s.conversation_generation,sh.shortname,sh.flavor,owner.user_id "
        "FROM sprint_participants p "
        "JOIN sprints s ON s.sprint_id=p.sprint_id "
        "JOIN shells sh ON sh.shell_id=p.shell_id "
        "JOIN shells owner ON owner.shell_id=s.originating_planner_shell_id "
        "WHERE p.participant_id=? AND p.sprint_id=?",
        (participant_id, sprint_id),
    ).fetchone()
    if row is None:
        raise SprintConversationError("wake recipient is not a Sprint participant")
    if row["user_id"] is None:
        raise SprintConversationError("originating Planner has no browser owner")
    if active_chat_registry.get(con, int(row["shell_id"])) is not None:
        raise WakeConversationBusy("another chat became active before wake creation")

    generation = _nonblank(
        row["conversation_generation"],
        "Sprint conversation generation",
        maximum=32,
    )
    key = f"generation:{generation}:wake:{wake_id}"
    existing = con.execute(
        "SELECT conversation_id,state FROM conversations "
        "WHERE owner_user_id=? AND creation_idempotency_key=?",
        (row["user_id"], key),
    ).fetchone()
    if existing is not None:
        if existing["state"] == "closed":
            raise SprintConversationError("idempotent wake chat is already closed")
        conversation_id = str(existing["conversation_id"])
        if not _linked_to_participant(con, participant_id, conversation_id):
            raise SprintConversationError(
                "idempotent wake chat belongs to another participant"
            )
        active_chat_registry.register(con, int(row["shell_id"]), conversation_id)
        return conversation_id

    worktree = run_mod.shell_work_dir(row["shortname"], row["flavor"])
    request = {
        "effort": row["effort"],
        "harness": row["harness"],
        "model": row["model"],
        "participant_id": participant_id,
        "provider": run_mod.session_provider(row["harness"], row["model"]),
        "sprint_id": sprint_id,
        "wake_id": wake_id,
        "worktree": str(worktree.resolve(strict=False)),
    }
    conversation_id = "cv_" + uuid.uuid4().hex
    con.execute(
        "INSERT INTO conversations "
        "(conversation_id,shell_id,owner_user_id,harness,provider,model,effort,"
        "worktree,title,creation_idempotency_key,creation_request_hash,"
        "conversation_scope) VALUES (?,?,?,?,?,?,?,?,?,?,?,'sprint')",
        (
            conversation_id,
            row["shell_id"],
            row["user_id"],
            row["harness"],
            request["provider"],
            row["model"],
            row["effort"],
            request["worktree"],
            f"Sprint {sprint_id} · Wake {wake_id} · {row['shortname']}",
            key,
            _request_hash(request),
        ),
    )
    _append_created_event(
        con,
        conversation_id=conversation_id,
        participant_id=participant_id,
        sprint_id=sprint_id,
        wake_id=wake_id,
    )
    con.execute(
        "INSERT INTO sprint_participant_conversations "
        "(sprint_participant_id,conversation_id) VALUES (?,?)",
        (participant_id, conversation_id),
    )
    active_chat_registry.register(con, int(row["shell_id"]), conversation_id)
    return conversation_id


def attach_live_participations(con, shells: list[dict]) -> list[dict]:
    """Attach the live/paused Sprint projection using registry state."""
    rows = con.execute(
        "SELECT p.shell_id,p.role,p.disposition,a.chat_id AS current_conversation_id,"
        "s.sprint_id,s.lifecycle "
        "FROM sprint_participants p "
        "JOIN sprints s ON s.sprint_id=p.sprint_id "
        "LEFT JOIN active_shell_chats a ON a.shell_id=p.shell_id "
        "WHERE s.lifecycle IN ('armed','paused') "
        "ORDER BY s.lifecycle='armed' DESC,s.paused_at DESC,s.sprint_id DESC"
    ).fetchall()
    by_shell: dict[int, dict] = {}
    for row in rows:
        shell_id = int(row["shell_id"])
        if shell_id in by_shell:
            continue
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
