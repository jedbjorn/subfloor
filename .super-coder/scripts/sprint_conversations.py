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
