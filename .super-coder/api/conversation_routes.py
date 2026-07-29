"""Browser-native normal conversation HTTP resources (Feature #24).

The localhost operator is the only actor for normal-mode conversations.
Browser mutations additionally prove same-origin.  Native harness session and
run references remain server-side; projections and SSE expose normalized
conversation data only.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import http.client
import io
import json
import re
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
DB_PATH = ENGINE / "shell_db.db"
SCRIPTS = ENGINE / "scripts"
sys.path.insert(0, str(SCRIPTS))

import conversation_broker
import conversation_events
import db_driver
import run as run_mod
from conversation_adapters import ADAPTER_TYPES

_ALLOWED_HOST_SET = frozenset(("127.0.0.1", "localhost", "::1"))
_CONVERSATION_STATES = frozenset(
    ("idle", "queued", "running", "waiting", "error", "closed")
)
_ID = re.compile(r"^cv_[0-9a-f]{32}$")
_DETAIL_PATH = re.compile(r"^/api/conversations/(cv_[0-9a-f]{32})$")
_MESSAGES_PATH = re.compile(r"^/api/conversations/(cv_[0-9a-f]{32})/messages$")
_EVENTS_PATH = re.compile(r"^/api/conversations/(cv_[0-9a-f]{32})/events$")
_INTERRUPTIONS_PATH = re.compile(
    r"^/api/conversations/(cv_[0-9a-f]{32})/interruptions$"
)
_SENSITIVE_EVENT_KEYS = frozenset(
    (
        "api_key",
        "authorization",
        "credential",
        "credentials",
        "harness_session_ref",
        "analysis",
        "native_type",
        "native_session",
        "process_ref",
        "run_ref",
        "secret",
        "session_id",
        "session_ref",
        "thinking",
        "threadid",
        "token",
        "turnid",
        "reasoning",
    )
)
SSE_HEARTBEAT_SECONDS = 15.0
SSE_BATCH = 200


class ApiError(RuntimeError):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: dict | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(f"{code}: {message}")


def _db():
    return db_driver.connect(str(DB_PATH))


def _json(status: int, obj, headers=None):
    return (
        status,
        [
            ("Content-Type", "application/json"),
            ("Cache-Control", "no-store"),
            *list(headers or []),
        ],
        json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode(),
    )


def _err(
    status: int,
    code: str,
    message: str,
    details: dict | None = None,
    headers=None,
):
    return _json(
        status,
        {"error": {"code": code, "message": message, "details": details or {}}},
        headers,
    )


def _api_error(exc: ApiError):
    return _err(exc.status, exc.code, exc.message, exc.details)


def _parse_headers(headers_raw: str):
    return http.client.parse_headers(io.BytesIO(headers_raw.encode("latin-1")))


def _host_ok(headers) -> bool:
    host = (headers.get("Host") or "").strip()
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    elif ":" in host:
        host = host.rsplit(":", 1)[0]
    return host in _ALLOWED_HOST_SET


def _same_origin_as_host(origin: str, host: str) -> bool:
    parsed = urlparse(origin)
    return (
        parsed.scheme in ("http", "https")
        and not (parsed.path or parsed.params or parsed.query or parsed.fragment)
        and parsed.netloc == host
    )


def _mutation_site_ok(headers) -> bool:
    origin = headers.get("Origin")
    if origin and not _same_origin_as_host(origin, headers.get("Host") or ""):
        return False
    return headers.get("Sec-Fetch-Site") in (None, "same-origin", "none")


def _operator(con, headers) -> dict:
    authz = headers.get("Authorization") or ""
    if authz:
        if authz[:7].lower() != "bearer " or not authz[7:].strip():
            raise ApiError(401, "UNAUTHORIZED", "invalid Authorization header")
        shell = con.execute(
            "SELECT shell_id FROM shells WHERE api_key=? AND COALESCE(is_deleted,0)=0",
            (authz[7:].strip(),),
        ).fetchone()
        if shell is None:
            raise ApiError(
                401,
                "UNAUTHORIZED",
                "the presented Bearer token matches no shell",
            )
        raise ApiError(
            403,
            "OPERATOR_REQUIRED",
            "normal conversations are owned by the browser operator",
        )
    row = con.execute(
        "SELECT user_id,username FROM users WHERE is_active=1 ORDER BY user_id LIMIT 1"
    ).fetchone()
    if row is None:
        raise ApiError(503, "OPERATOR_UNAVAILABLE", "no active operator exists")
    return {"user_id": int(row["user_id"]), "username": row["username"]}


def _body(raw: bytes) -> dict:
    try:
        value = json.loads(raw) if raw else {}
    except (UnicodeDecodeError, ValueError) as exc:
        raise ApiError(400, "BAD_JSON", "request body is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ApiError(400, "BAD_JSON", "request body must be a JSON object")
    return value


def _only_fields(body: dict, allowed: set[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            "unknown field(s): " + ", ".join(unknown),
            {"fields": unknown},
        )


def _integer(value, name: str) -> int:
    if isinstance(value, bool):
        raise ApiError(422, "VALIDATION_ERROR", f"{name} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(422, "VALIDATION_ERROR", f"{name} must be an integer") from exc


def _nonblank(
    value,
    name: str,
    *,
    maximum: int = 255,
    optional: bool = False,
) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ApiError(422, "VALIDATION_ERROR", f"{name} must be a nonblank string")
    value = value.strip()
    if len(value) > maximum:
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            f"{name} must be at most {maximum} characters",
        )
    return value


def _idempotency_key(headers) -> str:
    key = headers.get("Idempotency-Key")
    if not isinstance(key, str) or not key.strip():
        raise ApiError(
            422,
            "IDEMPOTENCY_KEY_REQUIRED",
            "Idempotency-Key header is required for this mutation",
        )
    key = key.strip()
    if len(key) > 255:
        raise ApiError(
            422,
            "IDEMPOTENCY_KEY_INVALID",
            "Idempotency-Key must be at most 255 characters",
        )
    return key


def _request_hash(body: dict) -> str:
    encoded = json.dumps(
        body,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _cursor_encode(value: dict) -> str:
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _cursor_decode(raw: str, kind: str) -> dict:
    try:
        padding = "=" * (-len(raw) % 4)
        value = json.loads(base64.urlsafe_b64decode(raw + padding))
    except Exception as exc:
        raise ApiError(422, "CURSOR_INVALID", f"invalid {kind} cursor") from exc
    if not isinstance(value, dict) or value.get("v") != 1:
        raise ApiError(422, "CURSOR_INVALID", f"invalid {kind} cursor")
    return value


def _limit(query, *, default: int = 50, maximum: int = 200) -> int:
    raw = query.get("limit", [str(default)])[0]
    value = _integer(raw, "limit")
    if not 1 <= value <= maximum:
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            f"limit must be between 1 and {maximum}",
        )
    return value


def _append_event(
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


def _conversation_row(con, conversation_id: str, owner_user_id: int):
    return con.execute(
        "SELECT c.conversation_id,c.shell_id,c.mode,c.owner_user_id,c.harness,"
        "c.provider,c.model,c.effort,c.state,c.title,c.created_at,"
        "c.last_activity_at,c.closed_at,c.version,s.display_name,s.shortname "
        "FROM conversations c JOIN shells s ON s.shell_id=c.shell_id "
        "WHERE c.conversation_id=? AND c.mode='normal' AND c.owner_user_id=?",
        (conversation_id, owner_user_id),
    ).fetchone()


def _conversation_projection(row) -> dict:
    return {
        "conversation_id": row["conversation_id"],
        "shell": {
            "shell_id": int(row["shell_id"]),
            "display_name": row["display_name"],
            "shortname": row["shortname"],
        },
        "mode": row["mode"],
        "route": {
            "harness": row["harness"],
            "provider": row["provider"],
            "model": row["model"],
            "effort": row["effort"],
        },
        "state": row["state"],
        "title": row["title"],
        "created_at": row["created_at"],
        "last_activity_at": row["last_activity_at"],
        "closed_at": row["closed_at"],
        "version": int(row["version"]),
    }


def _message_projection(row) -> dict:
    return {
        "message_id": int(row["message_id"]),
        "conversation_id": row["conversation_id"],
        "sender_kind": row["sender_kind"],
        "sender_ref": row["sender_ref"],
        "message_kind": row["message_kind"],
        "body": row["body"],
        "caused_by_message_id": row["caused_by_message_id"],
        "state": row["state"],
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
    }


def _message_row(con, message_id: int):
    return con.execute(
        "SELECT message_id,conversation_id,sender_kind,sender_ref,message_kind,"
        "body,caused_by_message_id,state,created_at,completed_at "
        "FROM conversation_messages WHERE message_id=?",
        (message_id,),
    ).fetchone()


def _require_conversation(con, conversation_id: str, owner_user_id: int):
    row = _conversation_row(con, conversation_id, owner_user_id)
    if row is None:
        raise ApiError(404, "CONVERSATION_NOT_FOUND", "conversation does not exist")
    return row


def _live_shell_session(shell) -> str | None:
    snapshot = run_mod.shell_liveness.compute()
    if shell["flavor"] == "admin":
        return "busy" if snapshot.get("admin_root_pids") else None
    return run_mod.shell_liveness.session_state(
        shell["shortname"] or "",
        snapshot,
    )


def _wait_for_cli_release(shell) -> str | None:
    """Drain a just-finished browser process, but never steal a live CLI slot."""
    state = _live_shell_session(shell)
    for _ in range(40):
        if state is None:
            return None
        time.sleep(0.05)
        state = _live_shell_session(shell)
    return state


def _create_conversation(con, operator: dict, headers, body: dict):
    _only_fields(body, {"shell_id", "title", "harness", "model", "effort"})
    key = _idempotency_key(headers)
    request_hash = _request_hash(body)
    shell_id = _integer(body.get("shell_id"), "shell_id")

    con.execute("BEGIN IMMEDIATE")
    existing = con.execute(
        "SELECT conversation_id,creation_request_hash FROM conversations "
        "WHERE mode='normal' AND owner_user_id=? "
        "AND creation_idempotency_key=?",
        (operator["user_id"], key),
    ).fetchone()
    if existing is not None:
        if existing["creation_request_hash"] != request_hash:
            con.rollback()
            raise ApiError(
                409,
                "CONVERSATION_IDEMPOTENCY_CONFLICT",
                "Idempotency-Key was reused with a different request",
            )
        row = _require_conversation(
            con, existing["conversation_id"], operator["user_id"]
        )
        con.commit()
        return _json(
            201,
            _conversation_projection(row),
            [("Location", f"/api/conversations/{row['conversation_id']}")],
        )

    shell = con.execute(
        "SELECT shell_id,display_name,shortname,flavor FROM shells "
        "WHERE shell_id=? AND (user_id=? OR is_shared=1) "
        "AND COALESCE(is_deleted,0)=0",
        (shell_id, operator["user_id"]),
    ).fetchone()
    if shell is None:
        con.rollback()
        raise ApiError(
            422,
            "SHELL_NOT_LAUNCHABLE",
            "shell is unknown, deleted, or unavailable to this operator",
        )

    open_conversations = con.execute(
        "SELECT conversation_id,state FROM conversations "
        "WHERE mode='normal' AND shell_id=? AND state!='closed'",
        (shell_id,),
    ).fetchall()
    if not open_conversations:
        live_state = _wait_for_cli_release(shell)
        if live_state is not None:
            con.rollback()
            raise ApiError(
                409,
                "SHELL_BUSY",
                f"shell {shell['shortname']!r} has a live CLI session; "
                "close it before opening a browser chat",
                {"shell_id": shell_id, "state": live_state},
            )

    defaults = run_mod.flavor_defaults(con).get(shell["flavor"])
    harness = body.get("harness")
    if harness is None:
        harness = (
            (defaults or {}).get("default_harness")
            or run_mod._configured_harness()
            or "claude"
        )
    harness = _nonblank(harness, "harness", maximum=64)
    if harness not in ADAPTER_TYPES:
        con.rollback()
        raise ApiError(
            422,
            "HARNESS_CONVERSATION_UNSUPPORTED",
            f"harness {harness!r} has no browser conversation adapter",
        )
    try:
        run_mod.conductor_policy.require_harness(shell["flavor"], harness)
    except ValueError as exc:
        con.rollback()
        raise ApiError(422, "HARNESS_ROUTE_INVALID", str(exc)) from exc

    model = body.get("model")
    if model is None and defaults:
        model = defaults["models"].get(harness)
    model = _nonblank(model, "model", maximum=255, optional=True)
    if harness == "opencode" and model is None:
        con.rollback()
        raise ApiError(
            422,
            "HARNESS_MODEL_REQUIRED",
            "OpenCode browser conversations require an exact model from a "
            "provider connected in OpenCode",
        )
    effort = _nonblank(body.get("effort"), "effort", maximum=64, optional=True)
    adapter = run_mod.load_adapter(harness)
    if effort is None:
        effort = run_mod.default_headless_effort(adapter)
    try:
        run_mod.validate_headless_request(adapter, model, effort)
    except ValueError as exc:
        con.rollback()
        raise ApiError(422, "HARNESS_ROUTE_INVALID", str(exc)) from exc
    title = _nonblank(body.get("title"), "title", maximum=200, optional=True)
    worktree = run_mod.shell_work_dir(shell["shortname"], shell["flavor"])
    worktree = worktree.resolve(strict=False)
    if worktree.exists() and not worktree.is_dir():
        con.rollback()
        raise ApiError(
            422,
            "HARNESS_WORKTREE_MISSING",
            "the shell worktree path exists but is not a directory",
            {"shell_id": shell_id},
        )

    conversation_id = "cv_" + uuid.uuid4().hex
    provider = run_mod.session_provider(harness, model)
    running = [
        row for row in open_conversations
        if row["state"] in ("queued", "running")
    ]
    if running:
        con.rollback()
        raise ApiError(
            409,
            "BROWSER_CHAT_BUSY",
            "the open browser chat has a turn in progress",
            {"conversation_id": running[0]["conversation_id"]},
        )
    auto_closed = []
    for row in open_conversations:
        con.execute(
            "UPDATE conversations SET state='closed',closed_at=datetime('now'),"
            "last_activity_at=datetime('now'),version=version+1 "
            "WHERE conversation_id=?",
            (row["conversation_id"],),
        )
        _append_event(
            con,
            row["conversation_id"],
            "conversation.closed",
            {"status": "closed", "reason": "another browser chat opened"},
        )
        auto_closed.append(row["conversation_id"])
    con.execute(
        "INSERT INTO conversations "
        "(conversation_id,shell_id,mode,owner_user_id,harness,provider,model,"
        "effort,worktree,title,creation_idempotency_key,"
        "creation_request_hash) VALUES (?,?,'normal',?,?,?,?,?,?,?,?,?)",
        (
            conversation_id,
            shell_id,
            operator["user_id"],
            harness,
            provider,
            model,
            effort,
            str(worktree),
            title,
            key,
            request_hash,
        ),
    )
    _append_event(
        con,
        conversation_id,
        "conversation.created",
        {
            "shell_id": shell_id,
            "harness": harness,
            "model": model,
            "effort": effort,
        },
    )
    con.commit()
    for closed_id in auto_closed:
        conversation_events.notify(closed_id)
    conversation_events.notify(conversation_id)
    row = _require_conversation(con, conversation_id, operator["user_id"])
    return _json(
        201,
        _conversation_projection(row),
        [("Location", f"/api/conversations/{conversation_id}")],
    )


def _list_conversations(con, operator: dict, query):
    limit = _limit(query, maximum=100)
    clauses = ["c.mode='normal'", "c.owner_user_id=?"]
    params: list = [operator["user_id"]]
    shell = query.get("shell_id", [None])[0]
    if shell not in (None, ""):
        clauses.append("c.shell_id=?")
        params.append(_integer(shell, "shell_id"))
    state = query.get("state", [None])[0]
    if state:
        if state not in _CONVERSATION_STATES:
            raise ApiError(422, "VALIDATION_ERROR", "invalid conversation state")
        clauses.append("c.state=?")
        params.append(state)
    mode = query.get("mode", [None])[0]
    if mode and mode != "normal":
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            "this endpoint exposes normal conversations only",
        )
    cursor = query.get("cursor", [None])[0]
    if cursor:
        decoded = _cursor_decode(cursor, "conversation")
        if not isinstance(decoded.get("a"), str) or not _ID.fullmatch(
            str(decoded.get("id", ""))
        ):
            raise ApiError(422, "CURSOR_INVALID", "invalid conversation cursor")
        clauses.append(
            "(c.last_activity_at<? OR (c.last_activity_at=? AND c.conversation_id<?))"
        )
        params.extend((decoded["a"], decoded["a"], decoded["id"]))
    rows = con.execute(
        "SELECT c.conversation_id,c.shell_id,c.mode,c.owner_user_id,c.harness,"
        "c.provider,c.model,c.effort,c.state,c.title,c.created_at,"
        "c.last_activity_at,c.closed_at,c.version,s.display_name,s.shortname "
        "FROM conversations c JOIN shells s ON s.shell_id=c.shell_id WHERE "
        + " AND ".join(clauses)
        + " ORDER BY c.last_activity_at DESC,c.conversation_id DESC LIMIT ?",
        (*params, limit + 1),
    ).fetchall()
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = _cursor_encode(
            {"v": 1, "a": last["last_activity_at"], "id": last["conversation_id"]}
        )
    return _json(
        200,
        {
            "items": [_conversation_projection(row) for row in page],
            "next_cursor": next_cursor,
        },
    )


def _patch_conversation(con, operator: dict, conversation_id: str, body: dict):
    _only_fields(body, {"version", "title", "state"})
    if "version" not in body:
        raise ApiError(
            422, "VALIDATION_ERROR", "version is required for conversation updates"
        )
    version = _integer(body["version"], "version")
    if version <= 0:
        raise ApiError(422, "VALIDATION_ERROR", "version must be positive")
    if "title" not in body and "state" not in body:
        raise ApiError(422, "VALIDATION_ERROR", "no conversation change supplied")
    title = (
        _nonblank(body.get("title"), "title", maximum=200, optional=True)
        if "title" in body
        else None
    )
    if "state" in body and body["state"] != "closed":
        raise ApiError(422, "VALIDATION_ERROR", "state may only be changed to closed")

    con.execute("BEGIN IMMEDIATE")
    row = _require_conversation(con, conversation_id, operator["user_id"])
    if int(row["version"]) != version:
        con.rollback()
        raise ApiError(
            409,
            "CONVERSATION_VERSION_CONFLICT",
            "conversation version does not match",
            {"expected": int(row["version"]), "received": version},
        )
    closing = body.get("state") == "closed"
    if row["state"] == "closed":
        con.rollback()
        raise ApiError(409, "CONVERSATION_CLOSED", "conversation is already closed")
    if closing and row["state"] not in ("idle", "waiting", "error"):
        con.rollback()
        raise ApiError(
            409,
            "BROWSER_CHAT_BUSY",
            "a queued or running conversation cannot be closed",
            {"state": row["state"]},
        )
    sets = ["version=version+1", "last_activity_at=datetime('now')"]
    params: list = []
    if "title" in body:
        sets.append("title=?")
        params.append(title)
    if closing:
        sets.extend(("state='closed'", "closed_at=datetime('now')"))
    changed = con.execute(
        f"UPDATE conversations SET {','.join(sets)} "
        "WHERE conversation_id=? AND owner_user_id=? AND version=?",
        (*params, conversation_id, operator["user_id"], version),
    ).rowcount
    if changed != 1:
        con.rollback()
        raise ApiError(
            409,
            "CONVERSATION_VERSION_CONFLICT",
            "conversation changed concurrently",
        )
    _append_event(
        con,
        conversation_id,
        (
            "conversation.updated"
            if "title" in body and closing
            else "conversation.closed"
            if closing
            else "conversation.renamed"
        ),
        {
            **({"title": title} if "title" in body else {}),
            **({"state": "closed"} if closing else {}),
        },
    )
    con.commit()
    conversation_events.notify(conversation_id)
    return _json(
        200,
        _conversation_projection(
            _require_conversation(con, conversation_id, operator["user_id"])
        ),
    )


def _create_message(con, operator: dict, conversation_id: str, headers, body: dict):
    _only_fields(body, {"text"})
    key = _idempotency_key(headers)
    text = _nonblank(body.get("text"), "text", maximum=1048576)
    normalized = {"text": text}
    request_hash = _request_hash(normalized)

    con.execute("BEGIN IMMEDIATE")
    conversation = _require_conversation(con, conversation_id, operator["user_id"])
    existing = con.execute(
        "SELECT message_id,request_hash FROM conversation_messages "
        "WHERE conversation_id=? AND idempotency_key=?",
        (conversation_id, key),
    ).fetchone()
    if existing is not None:
        if existing["request_hash"] != request_hash:
            con.rollback()
            raise ApiError(
                409,
                "MESSAGE_IDEMPOTENCY_CONFLICT",
                "Idempotency-Key was reused with a different message",
            )
        message = _message_projection(_message_row(con, int(existing["message_id"])))
        con.commit()
        return _json(
            202,
            {
                "message": message,
                "queue_position": _accepted_queue_position(con, message["message_id"]),
            },
            [("Location", f"/api/conversations/{conversation_id}/messages")],
        )
    if conversation["state"] == "closed":
        con.rollback()
        raise ApiError(
            409, "CONVERSATION_CLOSED", "closed conversations reject messages"
        )
    target_state = (
        conversation["state"]
        if conversation["state"] in ("queued", "running")
        else "queued"
    )
    message_id = int(
        con.execute(
            "INSERT INTO conversation_messages "
            "(conversation_id,sender_kind,sender_ref,message_kind,body,"
            "idempotency_key,request_hash,state) "
            "VALUES (?,'user',?,'prompt',?,?,?,'queued')",
            (
                conversation_id,
                str(operator["user_id"]),
                text,
                key,
                request_hash,
            ),
        ).lastrowid
    )
    con.execute(
        "INSERT INTO conversation_outbox (conversation_id,message_id) VALUES (?,?)",
        (conversation_id, message_id),
    )
    con.execute(
        "UPDATE conversations SET state=?,last_activity_at=datetime('now'),"
        "version=version+1 WHERE conversation_id=?",
        (target_state, conversation_id),
    )
    position = _queue_position(con, message_id)
    _append_event(
        con,
        conversation_id,
        "message.accepted",
        {
            "message_id": message_id,
            "queue_state": "queued",
            "queue_position": position,
        },
        message_id=message_id,
    )
    con.commit()
    conversation_events.notify(conversation_id)
    conversation_broker.notify_commit()
    return _json(
        202,
        {
            "message": _message_projection(_message_row(con, message_id)),
            "queue_position": position,
        },
        [("Location", f"/api/conversations/{conversation_id}/messages")],
    )


def _queue_position(con, message_id: int) -> int:
    row = con.execute(
        "SELECT COUNT(*) FROM conversation_outbox target "
        "JOIN conversation_outbox queued "
        "ON queued.conversation_id=target.conversation_id "
        "AND queued.outbox_id<=target.outbox_id "
        "WHERE target.message_id=? AND queued.state IN ('pending','claimed')",
        (message_id,),
    ).fetchone()
    return int(row[0]) if row else 0


def _accepted_queue_position(con, message_id: int) -> int:
    row = con.execute(
        "SELECT payload FROM conversation_events "
        "WHERE message_id=? AND event_type='message.accepted' "
        "ORDER BY sequence LIMIT 1",
        (message_id,),
    ).fetchone()
    if row is None:
        return _queue_position(con, message_id)
    try:
        value = json.loads(row["payload"]).get("queue_position")
    except (TypeError, ValueError):
        return _queue_position(con, message_id)
    return int(value) if isinstance(value, int) and value >= 0 else 0


def _list_messages(con, operator: dict, conversation_id: str, query):
    _require_conversation(con, conversation_id, operator["user_id"])
    limit = _limit(query)
    after = 0
    cursor = query.get("cursor", [None])[0]
    if cursor:
        decoded = _cursor_decode(cursor, "message")
        after = _integer(decoded.get("id"), "cursor message id")
        if after < 0:
            raise ApiError(422, "CURSOR_INVALID", "invalid message cursor")
    rows = con.execute(
        "SELECT message_id,conversation_id,sender_kind,sender_ref,message_kind,"
        "body,caused_by_message_id,state,created_at,completed_at "
        "FROM conversation_messages WHERE conversation_id=? AND message_id>? "
        "ORDER BY message_id LIMIT ?",
        (conversation_id, after, limit + 1),
    ).fetchall()
    page = rows[:limit]
    next_cursor = (
        _cursor_encode({"v": 1, "id": int(page[-1]["message_id"])})
        if len(rows) > limit
        else None
    )
    return _json(
        200,
        {
            "items": [_message_projection(row) for row in page],
            "next_cursor": next_cursor,
        },
    )


def _request_interrupt(run_id: int, *, replay: bool) -> None:
    try:
        conversation_broker.interrupt_run(run_id)
        return
    except conversation_broker.BrokerError as exc:
        if exc.code == "CONVERSATION_BROKER_UNAVAILABLE":
            try:
                conversation_broker.BrokerStore(DB_PATH).request_interrupt(run_id)
            except conversation_broker.BrokerError as store_exc:
                exc = store_exc
            else:
                # The durable event is enough for startup reconciliation; this
                # wake additionally reaches a broker that came online in the
                # narrow gap between the service lookup and the store commit.
                conversation_broker.notify_commit()
                return
        if exc.code in (
            "CONVERSATION_RUN_NOT_ACTIVE",
            "CONVERSATION_RUN_NOT_FOUND",
        ):
            if replay:
                return
            raise ApiError(
                409,
                "RUN_ALREADY_TERMINAL",
                "the run became terminal before interruption delivery",
            ) from exc
        raise ApiError(503, exc.code, exc.detail) from exc


def _interrupt(con, operator: dict, conversation_id: str, headers, body: dict):
    _only_fields(body, {"run_id"})
    key = _idempotency_key(headers)
    requested_run = _integer(body["run_id"], "run_id") if "run_id" in body else None
    normalized = {"run_id": requested_run}
    request_hash = _request_hash(normalized)

    con.execute("BEGIN IMMEDIATE")
    _require_conversation(con, conversation_id, operator["user_id"])
    existing = con.execute(
        "SELECT message_id,request_hash,body FROM conversation_messages "
        "WHERE conversation_id=? AND idempotency_key=?",
        (conversation_id, key),
    ).fetchone()
    if existing is not None:
        if existing["request_hash"] != request_hash:
            con.rollback()
            raise ApiError(
                409,
                "MESSAGE_IDEMPOTENCY_CONFLICT",
                "Idempotency-Key was reused with a different interruption",
            )
        audit = _message_projection(_message_row(con, int(existing["message_id"])))
        run_id = int(json.loads(existing["body"])["run_id"])
        con.commit()
        _request_interrupt(run_id, replay=True)
        return _json(202, {"interruption": audit, "run_id": run_id})

    clauses = [
        "conversation_id=?",
        "state IN ('leased','starting','running')",
    ]
    params: list = [conversation_id]
    if requested_run is not None:
        clauses.append("run_id=?")
        params.append(requested_run)
    active = con.execute(
        "SELECT run_id FROM conversation_runs WHERE "
        + " AND ".join(clauses)
        + " ORDER BY run_id DESC LIMIT 1",
        params,
    ).fetchone()
    if active is None:
        con.rollback()
        raise ApiError(
            409,
            "RUN_ALREADY_TERMINAL",
            "no matching active run can be interrupted",
        )
    run_id = int(active["run_id"])
    audit_body = json.dumps(
        {"kind": "interrupt", "run_id": run_id},
        separators=(",", ":"),
        sort_keys=True,
    )
    message_id = int(
        con.execute(
            "INSERT INTO conversation_messages "
            "(conversation_id,sender_kind,sender_ref,message_kind,body,"
            "idempotency_key,request_hash,state,completed_at) "
            "VALUES (?,'user',?,'control',?,?,?,'completed',datetime('now'))",
            (
                conversation_id,
                str(operator["user_id"]),
                audit_body,
                key,
                request_hash,
            ),
        ).lastrowid
    )
    con.execute(
        "UPDATE conversations SET last_activity_at=datetime('now'),"
        "version=version+1 WHERE conversation_id=?",
        (conversation_id,),
    )
    con.commit()
    _request_interrupt(run_id, replay=False)
    return _json(
        202,
        {
            "interruption": _message_projection(_message_row(con, message_id)),
            "run_id": run_id,
        },
    )


def handle(method: str, path: str, headers_raw: str, raw_body: bytes) -> tuple:
    headers = _parse_headers(headers_raw)
    if not _host_ok(headers):
        return _err(
            403,
            "HOST_NOT_ALLOWED",
            "conversation API serves 127.0.0.1/localhost only",
        )
    parsed = urlparse(path)
    query = parse_qs(parsed.query)
    con = _db()
    try:
        try:
            operator = _operator(con, headers)
            if method in ("POST", "PATCH", "PUT", "DELETE") and not _mutation_site_ok(
                headers
            ):
                raise ApiError(
                    403, "NOT_SAME_ORIGIN", "cross-site conversation mutation rejected"
                )
            body = _body(raw_body)
            if parsed.path == "/api/conversations":
                if method == "GET":
                    return _list_conversations(con, operator, query)
                if method == "POST":
                    return _create_conversation(con, operator, headers, body)
            detail = _DETAIL_PATH.fullmatch(parsed.path)
            if detail:
                conversation_id = detail.group(1)
                if method == "GET":
                    return _json(
                        200,
                        _conversation_projection(
                            _require_conversation(
                                con, conversation_id, operator["user_id"]
                            )
                        ),
                    )
                if method == "PATCH":
                    return _patch_conversation(con, operator, conversation_id, body)
            messages = _MESSAGES_PATH.fullmatch(parsed.path)
            if messages:
                conversation_id = messages.group(1)
                if method == "GET":
                    return _list_messages(con, operator, conversation_id, query)
                if method == "POST":
                    return _create_message(
                        con, operator, conversation_id, headers, body
                    )
            interruptions = _INTERRUPTIONS_PATH.fullmatch(parsed.path)
            if interruptions and method == "POST":
                return _interrupt(con, operator, interruptions.group(1), headers, body)
            if _EVENTS_PATH.fullmatch(parsed.path) and method == "GET":
                return _err(
                    406,
                    "SSE_REQUIRED",
                    "conversation events require the live SSE transport",
                )
            return _err(
                404,
                "NO_SUCH_ROUTE",
                f"no conversation route: {method} {parsed.path}",
            )
        except ApiError as exc:
            if con.in_transaction:
                con.rollback()
            return _api_error(exc)
        except Exception:  # noqa: BLE001 — uniform boundary for handler faults
            if con.in_transaction:
                con.rollback()
            return _err(
                500,
                "INTERNAL_ERROR",
                "conversation request failed",
            )
    finally:
        con.close()


def _sensitive_event_key(key: str) -> bool:
    lowered = key.lower()
    normalized = lowered.replace("-", "_")
    return (
        lowered in _SENSITIVE_EVENT_KEYS
        or normalized in _SENSITIVE_EVENT_KEYS
        or normalized.endswith(("_token", "_secret", "_credential"))
    )


def _redact_event_value(value, secrets: tuple[str, ...]):
    if isinstance(value, dict):
        return {
            key: _redact_event_value(child, secrets)
            for key, child in value.items()
            if not _sensitive_event_key(key)
        }
    if isinstance(value, list):
        return [_redact_event_value(child, secrets) for child in value]
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[redacted]")
    return value


def _event_projection(row, secrets: tuple[str, ...]) -> dict:
    return {
        "sequence": int(row["sequence"]),
        "event_type": row["event_type"],
        "payload_version": int(row["payload_version"]),
        "payload": _redact_event_value(json.loads(row["payload"]), secrets),
        "message_id": row["message_id"],
        "run_id": row["run_id"],
        "created_at": row["created_at"],
    }


def _stream_authorize(headers_raw: str, conversation_id: str):
    headers = _parse_headers(headers_raw)
    if not _host_ok(headers):
        raise ApiError(
            403,
            "HOST_NOT_ALLOWED",
            "conversation API serves 127.0.0.1/localhost only",
        )
    con = _db()
    try:
        operator = _operator(con, headers)
        _require_conversation(con, conversation_id, operator["user_id"])
    finally:
        con.close()
    return headers


def _after_sequence(query, headers) -> int:
    cursor = query.get("cursor", [None])[0]
    last_event = headers.get("Last-Event-ID")
    raw_after = query.get("after", [None])[0]
    supplied = sum(value not in (None, "") for value in (cursor, last_event, raw_after))
    if supplied > 1:
        raise ApiError(
            422,
            "CURSOR_INVALID",
            "supply only one of cursor, after, or Last-Event-ID",
        )
    if cursor:
        decoded = _cursor_decode(cursor, "event")
        after = _integer(decoded.get("sequence"), "event sequence")
    elif last_event:
        after = _integer(last_event, "Last-Event-ID")
    elif raw_after:
        after = _integer(raw_after, "after")
    else:
        after = 0
    if after < 0:
        raise ApiError(422, "CURSOR_INVALID", "event sequence cannot be negative")
    return after


def _event_batch(conversation_id: str, after: int) -> list[dict]:
    con = _db()
    try:
        secret_values = [
            row[0]
            for row in con.execute(
                "SELECT harness_session_ref FROM conversations "
                "WHERE conversation_id=? AND harness_session_ref IS NOT NULL "
                "UNION SELECT harness_session_before FROM conversation_runs "
                "WHERE conversation_id=? AND harness_session_before IS NOT NULL "
                "UNION SELECT harness_session_after FROM conversation_runs "
                "WHERE conversation_id=? AND harness_session_after IS NOT NULL "
                "UNION SELECT runner_ref FROM conversation_runs "
                "WHERE conversation_id=? AND runner_ref IS NOT NULL",
                (conversation_id,) * 4,
            )
            if isinstance(row[0], str) and row[0]
        ]
        secrets = tuple(sorted(set(secret_values), key=len, reverse=True))
        rows = con.execute(
            "SELECT sequence,event_type,payload_version,payload,message_id,"
            "run_id,created_at FROM conversation_events "
            "WHERE conversation_id=? AND sequence>? "
            "ORDER BY sequence LIMIT ?",
            (conversation_id, after, SSE_BATCH),
        ).fetchall()
        return [_event_projection(row, secrets) for row in rows]
    finally:
        con.close()


async def _stream_error(writer, exc: ApiError) -> None:
    status, headers, body = _api_error(exc)
    lines = [f"HTTP/1.1 {status} Error"]
    for key, value in headers:
        lines.append(f"{key}: {value}")
    lines.extend((f"Content-Length: {len(body)}", "Connection: close"))
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
    await writer.drain()
    writer.close()


async def stream_events(
    method: str,
    path: str,
    headers_raw: str,
    writer,
) -> bool:
    """Claim and serve a conversation event request; return False otherwise."""
    parsed = urlparse(path)
    match = _EVENTS_PATH.fullmatch(parsed.path)
    if match is None or method != "GET":
        return False
    conversation_id = match.group(1)
    try:
        headers = await asyncio.to_thread(
            _stream_authorize, headers_raw, conversation_id
        )
        after = _after_sequence(parse_qs(parsed.query), headers)
    except ApiError as exc:
        await _stream_error(writer, exc)
        return True
    except Exception:  # noqa: BLE001 — stream faults need a JSON response
        await _stream_error(
            writer,
            ApiError(500, "INTERNAL_ERROR", "conversation event stream failed"),
        )
        return True

    response_head = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/event-stream\r\n"
        b"Cache-Control: no-store\r\n"
        b"X-Accel-Buffering: no\r\n"
        b"Connection: keep-alive\r\n\r\n"
    )
    writer.write(response_head)
    await writer.drain()
    try:
        while True:
            observed = conversation_events.generation(conversation_id)
            events = await asyncio.to_thread(_event_batch, conversation_id, after)
            if events:
                for event in events:
                    after = event["sequence"]
                    data = json.dumps(
                        event,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    frame = (
                        f"id: {after}\nevent: {event['event_type']}\ndata: {data}\n\n"
                    ).encode()
                    writer.write(frame)
                await writer.drain()
                continue
            await asyncio.to_thread(
                conversation_events.wait,
                conversation_id,
                observed,
                SSE_HEARTBEAT_SECONDS,
            )
            if conversation_events.generation(conversation_id) == observed:
                writer.write(b": keepalive\n\n")
                await writer.drain()
    except (BrokenPipeError, ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()
    return True
