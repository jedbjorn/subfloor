"""Native wake placement uses the installed owner, never a PID idle heuristic."""

from __future__ import annotations

import json
import time
from pathlib import Path

import conversation_native_chats
import route_bindings
from conversation_runtime_contract import RuntimeContractError, payload_digest


def native_chat(con, cid: str):
    columns = {row["name"] for row in con.execute("PRAGMA table_info(conversations)")}
    if "runtime_mode" not in columns:
        return None
    row = con.execute(
        "SELECT * FROM conversations WHERE conversation_id=?", (cid,)
    ).fetchone()
    return row if row and row["runtime_mode"] == "native_experiment" else None


def owner(con):
    service = conversation_native_chats._SERVICE
    database = next(
        (
            row["file"]
            for row in con.execute("PRAGMA database_list")
            if row["name"] == "main"
        ),
        "",
    )
    if (
        service is None
        or not database
        or Path(database).resolve() != Path(service.database).resolve()
    ):
        raise RuntimeContractError(
            "CLEANUP_PENDING", "native wake has no matching installed owner"
        )
    return service


def outstanding(con, cid: str, generation: str) -> bool:
    return (
        con.execute(
            "SELECT 1 FROM conversation_messages WHERE conversation_id=? AND state IN ('accepted','queued','running') LIMIT 1",
            (cid,),
        ).fetchone()
        is not None
        or con.execute(
            "SELECT 1 FROM conversation_runtime_commands WHERE generation_id=? AND kind='submit' AND state IN ('accepted','written','processed','unknown') LIMIT 1",
            (generation,),
        ).fetchone()
        is not None
    )


def released(con, cid: str) -> bool:
    chat = native_chat(con, cid)
    if chat is None or chat["state"] != "closed":
        return False
    runtime = json.loads(chat["runtime_projection"])
    row = con.execute(
        "SELECT * FROM conversation_runtime_generations WHERE generation_id=?",
        (runtime.get("generation_id"),),
    ).fetchone()
    if row is None or (
        row["conversation_id"],
        row["owner_user_id"],
        row["shell_id"],
    ) != (cid, chat["owner_user_id"], chat["shell_id"]):
        return False
    cleanup = json.loads(row["cleanup_json"])
    return bool(
        row["state"] == "closed"
        and runtime.get("state") == "closed"
        and cleanup.get("outcome") == "complete"
        and cleanup.get("unit_verified_exited") is True
        and not cleanup.get("unresolved_work")
        and not cleanup.get("unresolved_definitions")
    )


def current_observation(con, service, chat, observation: dict) -> bool:
    runtime = json.loads(chat["runtime_projection"])
    status = observation.get("status", {})
    quiet = status.get("quiet", {})
    generation = runtime.get("generation_id")
    row = con.execute(
        "SELECT * FROM conversation_runtime_generations WHERE generation_id=?",
        (generation,),
    ).fetchone()
    shell = con.execute(
        "SELECT user_id,is_deleted FROM shells WHERE shell_id=?", (chat["shell_id"],)
    ).fetchone()
    return bool(
        conversation_native_chats._SERVICE is service
        and owner(con) is service
        and observation.get("conversation_id") == chat["conversation_id"]
        and observation.get("generation_id") == generation
        and observation.get("owner_user_id") == chat["owner_user_id"]
        and observation.get("shell_id") == chat["shell_id"]
        and runtime.get("role") == "ordinary"
        and runtime.get("state") == "ready"
        and chat["state"] != "closed"
        and row
        and row["conversation_id"] == chat["conversation_id"]
        and row["owner_user_id"] == chat["owner_user_id"]
        and row["shell_id"] == chat["shell_id"]
        and row["harness"] == chat["harness"]
        and shell
        and shell["user_id"] == chat["owner_user_id"]
        and not shell["is_deleted"]
        and not row["close_intent"]
        and row["state"] == "ready"
        and row["consumer_fence"] == observation.get("consumer_fence")
        and row["consumer_id"] == service.consumer
        and row["consumer_expires"] > time.time()
        and 0 <= time.time() - observation.get("observed_at", 0) <= 2
        and status.get("generation") == generation
        and status.get("ready") is True
        and status.get("lost") is False
        and status.get("partial") is False
        and status.get("closing") is False
        and status.get("primary") is None
        and quiet.get("idle") is True
        and quiet.get("pending_submission") is False
        and type(quiet.get("epoch")) is int
        and quiet["epoch"] >= 0
        and not outstanding(con, chat["conversation_id"], generation)
    )


def observe(con, cid: str):
    service = owner(con)
    return service, service.observe_wake(con, cid)


def signature(observation: dict) -> str:
    return payload_digest(
        {
            key: observation[key]
            for key in (
                "conversation_id",
                "generation_id",
                "owner_user_id",
                "shell_id",
                "consumer_fence",
            )
        }
        | {
            "epoch": observation["status"]["quiet"]["epoch"],
            "idle_since": observation["status"]["quiet"]["idle_since"],
        }
    )


def checked_shell_route(con, shell_id: int, harness: str, model: str, effort: str):
    service = owner(con)
    shell = con.execute(
        "SELECT user_id,is_deleted FROM shells WHERE shell_id=?", (shell_id,)
    ).fetchone()
    if shell is None or shell["user_id"] != 1 or shell["is_deleted"]:
        raise RuntimeContractError(
            "RUNTIME_NOT_OWNED",
            "native wake requires the named synthetic operator shell",
        )
    binding, digest = service.resolve_route(harness, model, effort)
    route_bindings.validate_v2_binding(binding)
    current = con.execute(
        "SELECT user_id,is_deleted FROM shells WHERE shell_id=?", (shell_id,)
    ).fetchone()
    if (
        owner(con) is not service
        or current is None
        or tuple(current) != tuple(shell)
        or (binding["harness"], binding["requested_model"], binding["requested_effort"])
        != (harness, model, effort)
        or binding["effective_effort"] != effort
        or payload_digest(binding) != digest
        or binding["selector_binding"].get("proof_state") != "checked_native_selection"
    ):
        raise RuntimeContractError(
            "CAPABILITY_INCONCLUSIVE", "native wake route ownership or proof changed"
        )
    return binding, digest


def request_rotation(con, cid: str, wake_id: int, quiet_seconds: int) -> None:
    service, observed = observe(con, cid)
    chat = native_chat(con, cid)
    if chat is None or not current_observation(con, service, chat, observed):
        raise RuntimeContractError(
            "WAKE_NOT_QUIET", "native wake must wait for current idle ownership"
        )
    service.request_close(
        con,
        cid,
        chat["owner_user_id"],
        chat["version"],
        wake_id=wake_id,
        observation=observed,
        quiet_seconds=quiet_seconds,
    )
    service.notify()
