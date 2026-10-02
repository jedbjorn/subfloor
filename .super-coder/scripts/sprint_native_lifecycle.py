"""Sprint lifecycle associations; native commands stay in the existing journal."""

from __future__ import annotations

import json
import time

import conversation_native_chats as chats
import db_driver
from conversation_runtime_contract import RuntimeContractError, payload_digest
from conversation_runtime_controller import encoded


def available(con) -> bool:
    return (
        con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sprint_native_lifecycle_intents'"
        ).fetchone()
        is not None
    )


def participant_chats(con, sprint_id: int, *, developers_only: bool = False):
    if not available(con):
        return []
    return con.execute(
        "SELECT DISTINCT c.*,p.participant_id,p.role AS participant_role FROM sprint_participants p "
        "JOIN sprint_participant_conversations pc ON pc.sprint_participant_id=p.participant_id "
        "JOIN conversations c ON c.conversation_id=pc.conversation_id "
        "WHERE p.sprint_id=? AND c.runtime_mode='native_experiment' AND c.shell_id=p.shell_id "
        + (
            "AND p.role='developer' AND NOT EXISTS(SELECT 1 FROM sprint_participants retained WHERE retained.sprint_id=p.sprint_id AND retained.shell_id=p.shell_id AND retained.role<>'developer') "
            if developers_only
            else ""
        )
        + "ORDER BY p.participant_id,c.created_at",
        (sprint_id,),
    ).fetchall()


def captured_generation(con, chat):
    runtime = json.loads(chat["runtime_projection"])
    gid = runtime.get("generation_id")
    row = con.execute(
        "SELECT * FROM conversation_runtime_generations WHERE generation_id=?", (gid,)
    ).fetchone()
    if row and (row["conversation_id"], row["owner_user_id"], row["shell_id"]) != (
        chat["conversation_id"],
        chat["owner_user_id"],
        chat["shell_id"],
    ):
        raise RuntimeContractError(
            "RUNTIME_NOT_OWNED",
            "Sprint generation conflicts with participant chat tenancy",
        )
    return runtime, row


def insert_intent(
    con,
    sprint_id: int,
    chat,
    lifecycle: str,
    action: str,
    *,
    primary=None,
    command_id=None,
    state="pending",
    detail="",
) -> str:
    version = con.execute(
        "SELECT version FROM sprints WHERE sprint_id=?", (sprint_id,)
    ).fetchone()[0] + (0 if lifecycle == "completed" else 1)
    runtime, _ = captured_generation(con, chat)
    identity = {
        "sprint": sprint_id,
        "participant": chat["participant_id"],
        "version": version,
        "lifecycle": lifecycle,
        "conversation": chat["conversation_id"],
        "generation": runtime.get("generation_id"),
    }
    iid = "sprint-native:" + payload_digest(identity)
    con.execute(
        "INSERT OR IGNORE INTO sprint_native_lifecycle_intents(intent_id,sprint_id,participant_id,conversation_id,generation_id,owner_user_id,shell_id,lifecycle,lifecycle_version,action,primary_json,command_id,state,detail,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            iid,
            sprint_id,
            chat["participant_id"],
            chat["conversation_id"],
            runtime.get("generation_id"),
            chat["owner_user_id"],
            chat["shell_id"],
            lifecycle,
            version,
            action,
            encoded(primary),
            command_id,
            state,
            detail,
            time.time(),
        ),
    )
    return iid


def persist_interrupts(con, sprint_id: int, lifecycle: str) -> tuple[str, ...]:
    if not con.in_transaction:
        raise RuntimeError("native Sprint lifecycle requires its transaction")
    conversations = []
    for chat in participant_chats(con, sprint_id):
        if chat["conversation_id"] in conversations:
            continue
        runtime, generation = captured_generation(con, chat)
        if chat["state"] == "closed":
            continue
        primary = runtime.get("primary")
        iid = insert_intent(
            con,
            sprint_id,
            chat,
            lifecycle,
            "stop_reply",
            primary=primary,
            detail="PRIMARY_INCONCLUSIVE",
        )
        current = (
            runtime.get("role") == "ordinary"
            and runtime.get("state") == "ready"
            and runtime.get("freshness") == "current"
            and runtime.get("partial") is False
            and 0 <= time.time() - runtime.get("observed_at", 0) <= 2
            and generation is not None
            and generation["state"] == "ready"
            and not generation["close_intent"]
            and isinstance(primary, dict)
            and bool(primary.get("root_id"))
            and bool(primary.get("activity_id"))
            and (
                primary.get("thread_id") == primary["root_id"]
                or chat["harness"] == "claude"
                and primary.get("thread_id") is None
            )
        )
        if current:
            body = {
                "action": "stop_reply",
                "generation_id": generation["generation_id"],
                "expected_activity_id": primary["activity_id"],
                "version": chat["version"],
            }
            try:
                _gid, command_id, command = chats.persist_control_intent(
                    con, chat["conversation_id"], chat["owner_user_id"], body, iid
                )
            except RuntimeContractError as exc:
                con.execute(
                    "UPDATE sprint_native_lifecycle_intents SET detail=? WHERE intent_id=?",
                    (exc.code, iid),
                )
            else:
                con.execute(
                    "UPDATE sprint_native_lifecycle_intents SET command_id=?,state=?,detail='' WHERE intent_id=?",
                    (command_id, command["state"], iid),
                )
        conversations.append(chat["conversation_id"])
    return tuple(sorted(set(conversations)))


def persist_developer_close(con, sprint_id: int) -> tuple[str, ...]:
    if not con.in_transaction:
        raise RuntimeError("native Sprint Close requires its transaction")
    requested = []
    for chat in participant_chats(con, sprint_id, developers_only=True):
        runtime, generation = captured_generation(con, chat)
        iid = insert_intent(
            con, sprint_id, chat, "completed", "close", detail="CLEANUP_PENDING"
        )
        if developer_cleanup_complete(
            con, sprint_id, conversation_id=chat["conversation_id"]
        ):
            con.execute(
                "UPDATE sprint_native_lifecycle_intents SET state='terminal',detail='native cleanup already complete' WHERE intent_id=?",
                (iid,),
            )
            continue
        if runtime.get("role") != "ordinary":
            continue
        chats.persist_close_intent(
            con, chat["conversation_id"], chat["owner_user_id"], chat["version"]
        )
        if generation:
            con.execute(
                "UPDATE sprint_native_lifecycle_intents SET command_id=?,state='accepted' WHERE intent_id=?",
                ("close:" + generation["generation_id"], iid),
            )
        requested.append(chat["conversation_id"])
    return tuple(requested)


def consume(service: chats.NativeChatsService, generation: str) -> None:
    """Postcommit dispatch once; a crash after admission retains unknown."""
    con = db_driver.connect(str(service.database))
    try:
        if not available(con):
            return
        rows = con.execute(
            "SELECT * FROM sprint_native_lifecycle_intents WHERE generation_id=? AND action='stop_reply' AND command_id IS NOT NULL AND state IN ('accepted','unknown') ORDER BY created_at LIMIT 64",
            (generation,),
        ).fetchall()
        for row in rows:
            client, owner, shell = service.attach(generation)
            with db_driver.write_transaction(con, "sprint.native_interrupt.dispatch"):
                chat = con.execute(
                    "SELECT * FROM conversations WHERE conversation_id=? AND owner_user_id=? AND shell_id=?",
                    (row["conversation_id"], row["owner_user_id"], row["shell_id"]),
                ).fetchone()
                if (
                    not chat
                    or owner != row["owner_user_id"]
                    or shell != row["shell_id"]
                ):
                    continue
                runtime, owned = captured_generation(con, chat)
                command = con.execute(
                    "SELECT * FROM conversation_runtime_commands WHERE generation_id=? AND command_id=?",
                    (generation, row["command_id"]),
                ).fetchone()
                if command is None:
                    continue
                if command["state"] != "accepted":
                    con.execute(
                        "UPDATE sprint_native_lifecycle_intents SET state=?,detail=? WHERE intent_id=?",
                        (
                            command["state"],
                            "retained native command receipt",
                            row["intent_id"],
                        ),
                    )
                    continue
                if (
                    chats._SERVICE is not service
                    or runtime.get("generation_id") != generation
                    or runtime.get("role") != "ordinary"
                    or owned is None
                    or owned["consumer_id"] != service.consumer
                    or owned["consumer_fence"] != client.lease["fence"]
                    or owned["consumer_expires"] <= time.time()
                ):
                    continue
                intent = json.loads(command["intent_json"])
                if runtime.get("capabilities", {}).get("stop_reply") != "compatible":
                    result = {
                        "state": "not_written",
                        "detail": "CAPABILITY_INCONCLUSIVE",
                    }
                    con.execute(
                        "UPDATE conversation_runtime_commands SET state='not_written',receipt_json=? WHERE generation_id=? AND command_id=?",
                        (encoded(result), generation, row["command_id"]),
                    )
                    con.execute(
                        "UPDATE sprint_native_lifecycle_intents SET state='not_written',detail='CAPABILITY_INCONCLUSIVE' WHERE intent_id=?",
                        (row["intent_id"],),
                    )
                    continue
                if (
                    runtime.get("primary") != json.loads(row["primary_json"])
                    or owned["close_intent"]
                ):
                    result = {"state": "not_written", "detail": "PRIMARY_CHANGED"}
                    con.execute(
                        "UPDATE conversation_runtime_commands SET state='not_written',receipt_json=? WHERE generation_id=? AND command_id=?",
                        (encoded(result), generation, row["command_id"]),
                    )
                    con.execute(
                        "UPDATE sprint_native_lifecycle_intents SET state='not_written',detail='PRIMARY_CHANGED' WHERE intent_id=?",
                        (row["intent_id"],),
                    )
                    continue
                # Persist ambiguity BEFORE the native edge. Recovery may read
                # journal outcomes, but cannot issue another write for this ID.
                con.execute(
                    "UPDATE conversation_runtime_commands SET state='unknown',receipt_json=? WHERE generation_id=? AND command_id=? AND state='accepted'",
                    (
                        encoded(
                            {
                                "state": "unknown",
                                "detail": "lifecycle dispatch in flight",
                            }
                        ),
                        generation,
                        row["command_id"],
                    ),
                )
                con.execute(
                    "UPDATE sprint_native_lifecycle_intents SET state='unknown',detail='native interruption pending' WHERE intent_id=?",
                    (row["intent_id"],),
                )
            try:
                result = client.request(
                    "control",
                    command=intent | {"payload_digest": command["payload_digest"]},
                    timeout=5,
                )
            except (OSError, RuntimeContractError):
                result = {
                    "state": "unknown",
                    "detail": "native interruption transport inconclusive",
                }
            service.store.receipt(generation, owner, shell, row["command_id"], result)
        reconcile(con, generation)
    finally:
        con.close()


def reconcile(con, generation: str | None = None) -> None:
    if not available(con):
        return
    with db_driver.write_transaction(con, "sprint.native_lifecycle.reconcile"):
        rows = con.execute(
            "SELECT * FROM sprint_native_lifecycle_intents"
            + (" WHERE generation_id=?" if generation else ""),
            (generation,) if generation else (),
        ).fetchall()
        for row in rows:
            if row["action"] == "close":
                if developer_cleanup_complete(
                    con, row["sprint_id"], conversation_id=row["conversation_id"]
                ):
                    con.execute(
                        "UPDATE sprint_native_lifecycle_intents SET state='terminal',detail='native cleanup complete' WHERE intent_id=?",
                        (row["intent_id"],),
                    )
                continue
            if row["command_id"]:
                command = con.execute(
                    "SELECT state,receipt_json FROM conversation_runtime_commands WHERE generation_id=? AND command_id=?",
                    (row["generation_id"], row["command_id"]),
                ).fetchone()
                if command:
                    result = json.loads(command["receipt_json"])
                    con.execute(
                        "UPDATE sprint_native_lifecycle_intents SET state=?,detail=? WHERE intent_id=?",
                        (
                            command["state"],
                            result.get("detail", "")[:255],
                            row["intent_id"],
                        ),
                    )


def developer_cleanup_complete(
    con, sprint_id: int, *, conversation_id: str | None = None
) -> bool:
    matched = False
    for chat in participant_chats(con, sprint_id, developers_only=True):
        if conversation_id and chat["conversation_id"] != conversation_id:
            continue
        matched = True
        runtime, generation = captured_generation(con, chat)
        if chat["state"] != "closed" or runtime.get("preparation_owner"):
            return False
        if generation is None and (
            runtime.get("state") != "closed"
            or runtime.get("preparation_cleanup", {}).get("unit_verified_exited")
            is not True
            or runtime.get("preparation_cleanup", {}).get("never_launched") is not True
        ):
            return False
        for retained in con.execute(
            "SELECT * FROM conversation_runtime_generations WHERE conversation_id=?",
            (chat["conversation_id"],),
        ):
            cleanup = json.loads(retained["cleanup_json"])
            if (
                (retained["owner_user_id"], retained["shell_id"])
                != (chat["owner_user_id"], chat["shell_id"])
                or retained["state"] != "closed"
                or cleanup.get("outcome") != "complete"
                or cleanup.get("unit_verified_exited") is not True
                or cleanup.get("unresolved_work")
                or cleanup.get("unresolved_definitions")
            ):
                return False
    return matched if conversation_id else True


def projection(con, sprint_id: int) -> list[dict]:
    if not available(con):
        return []
    rows = con.execute(
        "SELECT intent_id,participant_id,conversation_id,generation_id,lifecycle,action,state,detail FROM sprint_native_lifecycle_intents WHERE sprint_id=? ORDER BY created_at LIMIT 257",
        (sprint_id,),
    ).fetchall()
    return [dict(row) for row in rows[:256]]


def reenter(con, sprint_id: int) -> tuple[int, ...]:
    """A new notification, never another acceptance or replayed assignment."""
    if not available(con):
        return ()
    if not con.in_transaction:
        raise RuntimeError("native Resume re-entry requires its transaction")
    from sprint_message_delivery import SprintMessageStore

    messages = SprintMessageStore(con)
    rows = con.execute(
        "SELECT * FROM sprint_native_lifecycle_intents WHERE sprint_id=? AND lifecycle='paused' AND primary_json!='null' AND lifecycle_version=(SELECT MAX(lifecycle_version) FROM sprint_native_lifecycle_intents WHERE sprint_id=? AND lifecycle='paused') ORDER BY participant_id",
        (sprint_id, sprint_id),
    ).fetchall()
    wakes = []
    seen = set()
    for row in rows:
        if row["participant_id"] in seen:
            continue
        seen.add(row["participant_id"])
        chat = con.execute(
            "SELECT * FROM conversations WHERE conversation_id=? AND owner_user_id=? AND shell_id=?",
            (row["conversation_id"], row["owner_user_id"], row["shell_id"]),
        ).fetchone()
        if (
            not chat
            or chat["state"] == "closed"
            or json.loads(chat["runtime_projection"]).get("generation_id")
            != row["generation_id"]
        ):
            continue
        unit = con.execute(
            "SELECT work_unit_id FROM sprint_work_units WHERE sprint_id=? AND (assigned_shell_id=? OR reviewer_shell_id=?) AND disposition NOT IN ('completed','cancelled') ORDER BY work_unit_id LIMIT 1",
            (sprint_id, row["shell_id"], row["shell_id"]),
        ).fetchone()
        if (
            unit is None
            or con.execute(
                "SELECT 1 FROM sprint_wake_outbox WHERE receiver_shell_id=? AND state IN ('pending','delivering') LIMIT 1",
                (row["shell_id"],),
            ).fetchone()
        ):
            continue
        receipt = messages.send_in_transaction(
            sprint_id,
            to_participant_id=row["participant_id"],
            message_kind="notification",
            body=f"Sprint {sprint_id} resumed. Pause requested interruption of your captured foreground activity. Continue work unit {unit['work_unit_id']} from its current lane and inbox; do not re-accept or replay the assignment. Native interruption outcome: {row['state']}.",
            idempotency_key="sprint-resume-native:" + row["intent_id"],
            work_unit_id=unit["work_unit_id"],
            declared_type="re-enter",
        )
        if receipt.wake_id is not None:
            wakes.append(receipt.wake_id)
    return tuple(sorted(set(wakes)))


def relay_allowed(con, message) -> bool:
    """Retain a queued engine wake while its originating Sprint relay is off."""
    if (
        message["sender_kind"] != "engine"
        or message["sender_ref"] != "sprint-runtime"
        or not available(con)
    ):
        return True
    key = message["idempotency_key"]
    rows = con.execute(
        "SELECT DISTINCT s.lifecycle FROM sprint_wake_outbox w JOIN sprint_wake_messages wm ON wm.wake_id=w.wake_id JOIN wake_message m ON m.message_id=wm.message_id JOIN sprints s ON s.sprint_id=m.sprint_id WHERE w.receiver_shell_id=(SELECT shell_id FROM conversations WHERE conversation_id=?) AND (w.idempotency_key=? OR substr(?,1,length(w.idempotency_key)+9)=w.idempotency_key||':message:')",
        (message["conversation_id"], key, key),
    ).fetchall()
    return all(row["lifecycle"] == "armed" for row in rows)
