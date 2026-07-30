"""Transactional creation primitives for browser-native Sprint conversations.

Callers own the surrounding write transaction.  These helpers only persist
conversation, binding, prompt, event, and outbox rows; they never notify the
broker or perform harness/filesystem work.
"""
from __future__ import annotations

import hashlib
import json
import uuid


def append_event(
    con,
    conversation_id: str,
    event_type: str,
    payload: dict,
    *,
    message_id: int | None = None,
    run_id: int | None = None,
) -> int:
    sequence = int(
        con.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM conversation_events "
            "WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()[0]
    )
    con.execute(
        "INSERT INTO conversation_events "
        "(conversation_id,sequence,event_type,payload,message_id,run_id) "
        "VALUES (?,?,?,?,?,?)",
        (
            conversation_id,
            sequence,
            event_type,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            message_id,
            run_id,
        ),
    )
    return sequence


def enqueue_message(
    con,
    conversation_id: str,
    *,
    body: str,
    idempotency_key: str,
    sender_ref: str,
    message_kind: str = "prompt",
) -> int:
    """Atomically append one broker-dispatched message to a live conversation."""
    request_hash = hashlib.sha256(body.encode()).hexdigest()
    prior = con.execute(
        "SELECT message_id,request_hash FROM conversation_messages "
        "WHERE conversation_id=? AND idempotency_key=?",
        (conversation_id, idempotency_key),
    ).fetchone()
    if prior is not None:
        if prior["request_hash"] != request_hash:
            raise ValueError(
                f"conversation message key {idempotency_key!r} was reused "
                "with different content"
            )
        return int(prior["message_id"])
    conversation = con.execute(
        "SELECT state FROM conversations WHERE conversation_id=?",
        (conversation_id,),
    ).fetchone()
    if conversation is None:
        raise ValueError(f"conversation {conversation_id!r} does not exist")
    if conversation["state"] == "closed":
        raise ValueError(f"conversation {conversation_id!r} is closed")
    message_id = int(
        con.execute(
            "INSERT INTO conversation_messages "
            "(conversation_id,sender_kind,sender_ref,message_kind,body,"
            "idempotency_key,request_hash,state) "
            "VALUES (?,'engine',?,?,?,?,?,'queued')",
            (
                conversation_id,
                sender_ref,
                message_kind,
                body,
                idempotency_key,
                request_hash,
            ),
        ).lastrowid
    )
    con.execute(
        "INSERT INTO conversation_outbox (conversation_id,message_id) "
        "VALUES (?,?)",
        (conversation_id, message_id),
    )
    if conversation["state"] in ("idle", "waiting", "error"):
        con.execute(
            "UPDATE conversations SET state='queued',"
            "last_activity_at=datetime('now'),version=version+1 "
            "WHERE conversation_id=?",
            (conversation_id,),
        )
    else:
        con.execute(
            "UPDATE conversations SET last_activity_at=datetime('now'),"
            "version=version+1 WHERE conversation_id=?",
            (conversation_id,),
        )
    queue_position = int(
        con.execute(
            "SELECT COUNT(*) FROM conversation_messages "
            "WHERE conversation_id=? AND message_id<=? "
            "AND state IN ('accepted','queued','running')",
            (conversation_id, message_id),
        ).fetchone()[0]
    )
    append_event(
        con,
        conversation_id,
        "message.accepted",
        {
            "message_id": message_id,
            "queue_state": "queued",
            "queue_position": queue_position,
        },
        message_id=message_id,
    )
    return message_id


def conductor_conversation(con, sprint_doc_id: int):
    return con.execute(
        "SELECT c.conversation_id,c.state,b.binding_id "
        "FROM sprint_conversation_bindings b "
        "JOIN conversations c ON c.conversation_id=b.conversation_id "
        "WHERE b.sprint_doc_id=? AND b.role='conductor' "
        "AND b.state<>'terminal' AND c.state<>'closed' "
        "ORDER BY b.binding_id DESC LIMIT 1",
        (sprint_doc_id,),
    ).fetchone()


def enqueue_conductor_directive(
    con,
    *,
    sprint_doc_id: int,
    directive_id: int,
    source_kind: str,
    evidence: dict,
    idempotency_key: str,
) -> str | None:
    """Queue one committed directive/evidence packet to persistent Conductor."""
    conductor = conductor_conversation(con, sprint_doc_id)
    if conductor is None:
        return None
    body = json.dumps(
        {
            "sprint_doc_id": sprint_doc_id,
            "source_kind": source_kind,
            "directive_id": directive_id,
            "evidence": evidence,
            "instruction": f"Run `sc directives act {directive_id}`.",
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    enqueue_message(
        con,
        conductor["conversation_id"],
        body=body,
        idempotency_key=idempotency_key,
        sender_ref="sprint-bridge",
    )
    return str(conductor["conversation_id"])


def request_conductor_close(
    con,
    sprint_doc_id: int,
    *,
    reason: str,
) -> str | None:
    """Request terminal close, closing an already-idle Conductor immediately."""
    conductor = conductor_conversation(con, sprint_doc_id)
    if conductor is None:
        return None
    conversation_id = str(conductor["conversation_id"])
    exists = con.execute(
        "SELECT 1 FROM conversation_events WHERE conversation_id=? "
        "AND event_type='conversation.close.requested'",
        (conversation_id,),
    ).fetchone()
    if exists is None:
        append_event(
            con,
            conversation_id,
            "conversation.close.requested",
            {"reason": reason, "sprint_doc_id": sprint_doc_id},
        )
    state = conductor["state"]
    if state in ("idle", "waiting", "error"):
        con.execute(
            "UPDATE conversations SET state='closed',closed_at=datetime('now'),"
            "last_activity_at=datetime('now'),version=version+1 "
            "WHERE conversation_id=?",
            (conversation_id,),
        )
        append_event(
            con,
            conversation_id,
            "conversation.closed",
            {"state": "closed", "reason": reason},
        )
        con.execute(
            "UPDATE sprint_conversation_bindings SET state='terminal',"
            "outcome='closed',completed_at=datetime('now') "
            "WHERE binding_id=?",
            (conductor["binding_id"],),
        )
    return conversation_id


def create_sprint_conversation(
    con,
    *,
    sprint_doc_id: int,
    shell,
    role: str,
    lifecycle: str,
    route: dict,
    title: str,
    creation_key: str,
    prompt: str,
    unit_id: int | None = None,
    required_result_kind: str | None = None,
    source_directive_id: int | None = None,
) -> str:
    """Persist one Sprint conversation and its initial dispatch intent.

    Idempotency and assignment uniqueness are enforced by the conversations
    and sprint_conversation_bindings indices.  A successful return means every
    durable row exists in the caller's still-open transaction.
    """
    conversation_id = "cv_" + uuid.uuid4().hex
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
    creation_body = {
        "sprint_doc_id": sprint_doc_id,
        "shell_id": int(shell["shell_id"]),
        "role": role,
        "lifecycle": lifecycle,
        "unit_id": unit_id,
        "source_directive_id": source_directive_id,
        "required_result_kind": required_result_kind,
        "route": route,
        "title": title,
        "prompt_hash": prompt_hash,
    }
    creation_hash = hashlib.sha256(
        json.dumps(creation_body, sort_keys=True).encode()
    ).hexdigest()
    con.execute(
        "INSERT INTO conversations "
        "(conversation_id,shell_id,mode,owner_user_id,sprint_doc_id,harness,"
        "provider,model,effort,worktree,state,title,"
        "creation_idempotency_key,creation_request_hash) "
        "VALUES (?,?, 'sprint',NULL,?,?,?,?,?,?,'queued',?,?,?)",
        (
            conversation_id,
            shell["shell_id"],
            sprint_doc_id,
            route["harness"],
            route["provider"],
            route["model"],
            route["effort"],
            route["worktree"],
            title,
            creation_key,
            creation_hash,
        ),
    )
    binding_state = "active" if lifecycle == "persistent" else "pending"
    con.execute(
        "INSERT INTO sprint_conversation_bindings "
        "(conversation_id,sprint_doc_id,role,lifecycle,slot,unit_id,"
        "source_directive_id,required_result_kind,state,started_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,"
        "CASE WHEN ?='active' THEN datetime('now') ELSE NULL END)",
        (
            conversation_id,
            sprint_doc_id,
            role,
            lifecycle,
            shell["shortname"],
            unit_id,
            source_directive_id,
            required_result_kind,
            binding_state,
            binding_state,
        ),
    )
    prompt_key = f"{creation_key}:prompt"
    message_id = int(
        con.execute(
            "INSERT INTO conversation_messages "
            "(conversation_id,sender_kind,sender_ref,message_kind,body,"
            "idempotency_key,request_hash,state) "
            "VALUES (?,'engine','sprint','prompt',?,?,?,'queued')",
            (conversation_id, prompt, prompt_key, prompt_hash),
        ).lastrowid
    )
    con.execute(
        "INSERT INTO conversation_outbox (conversation_id,message_id) "
        "VALUES (?,?)",
        (conversation_id, message_id),
    )
    append_event(
        con,
        conversation_id,
        "conversation.created",
        {
            "shell_id": int(shell["shell_id"]),
            "mode": "sprint",
            "sprint_doc_id": sprint_doc_id,
            "role": role,
            "harness": route["harness"],
            "model": route["model"],
            "effort": route["effort"],
        },
    )
    append_event(
        con,
        conversation_id,
        "message.accepted",
        {"message_id": message_id, "queue_state": "queued", "queue_position": 1},
        message_id=message_id,
    )
    return conversation_id
