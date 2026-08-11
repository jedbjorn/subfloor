"""Browser-native conversation HTTP resources (Feature #24).

The localhost operator is the only actor for conversations.
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
import math
import re
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
DB_PATH = ENGINE / "shell_db.db"
SCRIPTS = ENGINE / "scripts"
sys.path.insert(0, str(SCRIPTS))

import active_chat_registry
import conversation_broker
import conversation_events
import conversation_git_targets
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
_TRANSCRIPT_PATH = re.compile(
    r"^/api/conversations/(cv_[0-9a-f]{32})/transcript$"
)
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
TRANSCRIPT_MAX_TURNS = 200
TRANSCRIPT_MAX_SOURCE_EVENTS = 20_000
TRANSCRIPT_MAX_SOURCE_BYTES = 8 * 1024 * 1024
TRANSCRIPT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
TRANSCRIPT_PROJECTION_VERSION = 2
TRANSCRIPT_MAX_WARNINGS = 20
TRANSCRIPT_MAX_ACTIVITY_LABEL_BYTES = 1024


@dataclass(frozen=True)
class TranscriptLimits:
    max_turns: int = TRANSCRIPT_MAX_TURNS
    max_source_events: int = TRANSCRIPT_MAX_SOURCE_EVENTS
    max_source_bytes: int = TRANSCRIPT_MAX_SOURCE_BYTES
    max_response_bytes: int = TRANSCRIPT_MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        if min(
            self.max_turns,
            self.max_source_events,
            self.max_source_bytes,
            self.max_response_bytes,
        ) <= 0:
            raise ValueError("transcript limits must be positive")


DEFAULT_TRANSCRIPT_LIMITS = TranscriptLimits()


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
            "conversations are owned by the browser operator",
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


def _strict_boolean(query, name: str) -> bool | None:
    values = query.get(name)
    if values is None:
        return None
    if len(values) != 1 or values[0] not in ("true", "false"):
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            f"{name} must be exactly one lowercase true or false value",
        )
    return values[0] == "true"


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


# A close request only counts until the conversation is next reopened: every
# reader of conversation.close.requested events pins to sequences after the
# latest conversation.reopened event, so a pre-reopen close request cannot
# strand or re-close a reopened chat.  The broker's claim/finish queries
# apply the same scoping.
# A Sprint chat is "managed" only while its sprint can still deliver into it:
# armed and paused lifecycles route wakes and hold resumable context, so the
# GUI keeps chat lifecycle in engine hands.  Once the sprint reaches a
# terminal lifecycle the chat is an ordinary leftover the operator may close
# (reopen stays scope-blocked — see SPRINT_CONVERSATION_MANAGED).
_SPRINT_MANAGED_COLUMN = (
    "EXISTS(SELECT 1 FROM sprint_participant_conversations link"
    " JOIN sprint_participants p"
    " ON p.participant_id=link.sprint_participant_id"
    " JOIN sprints sp ON sp.sprint_id=p.sprint_id"
    " WHERE link.conversation_id=c.conversation_id"
    " AND sp.lifecycle IN ('armed','paused')) AS sprint_managed"
)

_CLOSE_REQUESTED_AFTER_REOPEN = (
    "requested.event_type='conversation.close.requested' "
    "AND requested.sequence>COALESCE("
    "(SELECT MAX(reopened.sequence) FROM conversation_events reopened "
    "WHERE reopened.conversation_id=requested.conversation_id "
    "AND reopened.event_type='conversation.reopened'),0)"
)


def _conversation_row(con, conversation_id: str, owner_user_id: int):
    return con.execute(
        "SELECT c.conversation_id,c.shell_id,c.owner_user_id,c.harness,"
        "c.provider,c.model,c.effort,c.state,c.title,c.starred,"
        "c.conversation_scope,c.created_at,"
        "c.last_activity_at,c.closed_at,c.version,c.harness_session_ref,"
        "s.display_name,s.shortname,"
        "CASE WHEN c.state!='closed' THEN ("
        " SELECT requested.created_at FROM conversation_events requested "
        " WHERE requested.conversation_id=c.conversation_id "
        " AND " + _CLOSE_REQUESTED_AFTER_REOPEN +
        " ORDER BY requested.sequence DESC LIMIT 1"
        ") END AS close_requested_at,"
        "(SELECT COUNT(*) FROM conversation_messages queued "
        " WHERE queued.conversation_id=c.conversation_id "
        " AND queued.message_kind!='control' "
        " AND queued.state IN ('accepted','queued')) AS queued_count,"
        "(SELECT active.run_id FROM conversation_runs active "
        " WHERE active.conversation_id=c.conversation_id "
        " AND active.state IN ('leased','starting','running') "
        " ORDER BY active.run_id DESC LIMIT 1) AS active_run_id,"
        + _SPRINT_MANAGED_COLUMN
        + " FROM conversations c JOIN shells s ON s.shell_id=c.shell_id "
        "WHERE c.conversation_id=? AND c.owner_user_id=?",
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
        "route": {
            "harness": row["harness"],
            "provider": row["provider"],
            "model": row["model"],
            "effort": row["effort"],
        },
        "scope": row["conversation_scope"],
        "state": row["state"],
        "title": row["title"],
        "starred": bool(row["starred"]),
        "created_at": row["created_at"],
        "last_activity_at": row["last_activity_at"],
        "closed_at": row["closed_at"],
        "close_requested_at": row["close_requested_at"],
        "sprint_managed": bool(row["sprint_managed"]),
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


def _close_requested(con, conversation_id: str) -> bool:
    return (
        con.execute(
            "SELECT 1 FROM conversation_events requested "
            "WHERE requested.conversation_id=? "
            "AND " + _CLOSE_REQUESTED_AFTER_REOPEN + " LIMIT 1",
            (conversation_id,),
        ).fetchone()
        is not None
    )


def _reopen_conversation(con, operator: dict, conversation) -> list[str]:
    """Walk a closed normal chat back to idle so the pending send resumes it.

    Mirrors the create-path guards: a live CLI session blocks the shell when
    nothing else is open, a mid-turn open chat refuses, and an idle open chat
    auto-closes — the reopened chat becomes the shell's one open normal chat.
    Returns the auto-closed conversation ids for post-commit notification.
    """
    conversation_id = conversation["conversation_id"]
    if conversation["conversation_scope"] != "normal":
        raise ApiError(
            409,
            "SPRINT_CONVERSATION_MANAGED",
            "Sprint conversations reopen only with Sprint lifecycle control",
        )
    shell = con.execute(
        "SELECT shell_id,display_name,shortname,flavor FROM shells "
        "WHERE shell_id=? AND (user_id=? OR is_shared=1) "
        "AND COALESCE(is_deleted,0)=0",
        (conversation["shell_id"], operator["user_id"]),
    ).fetchone()
    if shell is None:
        raise ApiError(
            422,
            "SHELL_NOT_LAUNCHABLE",
            "shell is unknown, deleted, or unavailable to this operator",
        )
    _refuse_admin_browser_chat(shell)
    active = active_chat_registry.get(con, int(conversation["shell_id"]))
    if active is None:
        live_state = _live_shell_session(shell)
        if live_state is not None:
            raise ApiError(
                409,
                "SHELL_BUSY",
                f"shell {shell['shortname']!r} has a live CLI session; "
                "close it before reopening a browser chat",
                {
                    "shell_id": int(conversation["shell_id"]),
                    "state": live_state,
                },
            )
    try:
        closed = active_chat_registry.close_active(
            con,
            int(conversation["shell_id"]),
        )
    except active_chat_registry.ActiveChatBusy as exc:
        raise ApiError(
            409,
            "BROWSER_CHAT_BUSY",
            "the active chat has a turn in progress",
            {
                "conversation_id": (
                    active.chat_id if active is not None else None
                )
            },
        ) from exc
    auto_closed = []
    if closed is not None:
        _append_event(
            con,
            closed.chat_id,
            "conversation.closed",
            {"status": "closed", "reason": "another browser chat reopened"},
        )
        auto_closed.append(closed.chat_id)
    con.execute(
        "UPDATE conversations SET state='idle',closed_at=NULL,"
        "last_activity_at=datetime('now'),version=version+1 "
        "WHERE conversation_id=?",
        (conversation_id,),
    )
    _append_event(
        con,
        conversation_id,
        "conversation.reopened",
        {"state": "idle"},
    )
    active_chat_registry.register(
        con,
        int(conversation["shell_id"]),
        str(conversation_id),
    )
    return auto_closed


def _cancel_queued_turns(con, conversation_id: str) -> int:
    message_ids = [
        int(row["message_id"])
        for row in con.execute(
            "SELECT DISTINCT message_id FROM conversation_outbox "
            "WHERE conversation_id=? AND state IN ('pending','claimed')",
            (conversation_id,),
        ).fetchall()
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
        "UPDATE conversation_outbox SET state='cancelled',"
        "claim_owner=NULL,claimed_at=NULL,lease_expires_at=NULL "
        f"WHERE message_id IN ({marks}) AND state IN ('pending','claimed')",
        message_ids,
    )
    return int(cancelled)


def _enable_planner_coordinate_mode(
    con,
    *,
    shell_id: int,
    conversation_id: str,
) -> int | None:
    sprint = con.execute(
        "SELECT sprint_id FROM sprints WHERE lifecycle='armed' "
        "AND originating_planner_shell_id=? AND coordinate_mode=0",
        (shell_id,),
    ).fetchone()
    if sprint is None:
        return None
    sprint_id = int(sprint["sprint_id"])
    changed = con.execute(
        "UPDATE sprints SET coordinate_mode=1,updated_at=datetime('now'),"
        "version=version+1 WHERE sprint_id=? AND lifecycle='armed' "
        "AND coordinate_mode=0",
        (sprint_id,),
    ).rowcount
    if changed != 1:
        return None
    con.execute(
        "INSERT INTO sprint_events "
        "(sprint_id,event_type,actor_kind,payload) VALUES "
        "(?,'coordinate_mode.enabled','fnb',?)",
        (
            sprint_id,
            json.dumps(
                {"conversation_id": conversation_id, "reason": "Planner chat closed"},
                separators=(",", ":"),
                sort_keys=True,
            ),
        ),
    )
    return sprint_id


def _begin_close(con, conversation_id: str, current_state: str) -> None:
    """Unconditionally close and unlink a chat selected by the FnB."""
    active = con.execute(
        "SELECT run_id,trigger_message_id FROM conversation_runs "
        "WHERE conversation_id=? AND state IN ('leased','starting','running') "
        "ORDER BY run_id DESC LIMIT 1",
        (conversation_id,),
    ).fetchone()
    cancelled = _cancel_queued_turns(con, conversation_id)
    if active is not None:
        run_id = int(active["run_id"])
        if not _close_requested(con, conversation_id):
            _append_event(
                con,
                conversation_id,
                "conversation.close.requested",
                {"cancelled_queued_turns": cancelled},
                run_id=run_id,
            )

    recovered_state = None
    if current_state == "queued":
        con.execute(
            "UPDATE conversations SET state='idle' WHERE conversation_id=?",
            (conversation_id,),
        )
    elif current_state == "running":
        if active is None:
            recovered_state = current_state
        con.execute(
            "UPDATE conversations SET state='error' WHERE conversation_id=?",
            (conversation_id,),
        )
    con.execute(
        "UPDATE conversations SET state='closed',closed_at=datetime('now'),"
        "last_activity_at=datetime('now'),version=version+1 "
        "WHERE conversation_id=?",
        (conversation_id,),
    )
    con.execute(
        "DELETE FROM active_shell_chats WHERE chat_id=?",
        (conversation_id,),
    )
    _append_event(
        con,
        conversation_id,
        "conversation.closed",
        {
            "state": "closed",
            "cancelled_queued_turns": cancelled,
            **(
                {"recovered_orphaned_state": recovered_state}
                if recovered_state
                else {}
            ),
            **(
                {"displaced_run_id": int(active["run_id"])}
                if active is not None
                else {}
            ),
        },
        run_id=int(active["run_id"]) if active is not None else None,
    )


def _deliver_close_interrupt(run_id: int) -> None:
    try:
        conversation_broker.interrupt_run(run_id)
    except conversation_broker.BrokerError:
        # The close transaction already persisted interrupt intent. A live
        # broker is nudged now; startup/lease recovery delivers it otherwise.
        conversation_broker.notify_commit()


def _live_shell_session(shell) -> str | None:
    snapshot = run_mod.shell_liveness.compute()
    if shell["flavor"] == "admin":
        return "busy" if snapshot.get("admin_root_pids") else None
    return run_mod.shell_liveness.session_state(
        shell["shortname"] or "",
        snapshot,
    )


def _refuse_admin_browser_chat(shell) -> None:
    """Admin maintains main directly at the repo root — one working tree with
    no per-shell attribution (any harness process under the root reads as "the
    admin slot"), so a browser chat would surface timing-dependent SHELL_BUSY
    refusals whenever anyone works anywhere in the repo. Refuse deliberately
    instead, carrying the exact CLI commands the operator runs in its place."""
    if shell["flavor"] != "admin":
        return
    root = str(run_mod.REPO_ROOT)
    raise ApiError(
        422,
        "ADMIN_SHELL_CLI_ONLY",
        f"shell {shell['shortname']!r} is admin-flavor and CLI-only; open a "
        f"terminal and run: cd {root} && make dos-e s={shell['shortname']}",
        {
            "shell_id": int(shell["shell_id"]),
            "shortname": shell["shortname"],
            "repo_root": root,
        },
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
    shell_id = _integer(body.get("shell_id"), "shell_id")

    # Read a possible replay before preparation, but compare it only after the
    # request has been resolved to the same canonical route stored for a new
    # conversation. The transaction repeats every authoritative DB read.
    existing = con.execute(
        "SELECT conversation_id,creation_request_hash FROM conversations "
        "WHERE owner_user_id=? "
        "AND creation_idempotency_key=?",
        (operator["user_id"], key),
    ).fetchone()

    shell = con.execute(
        "SELECT shell_id,display_name,shortname,flavor FROM shells "
        "WHERE shell_id=? AND (user_id=? OR is_shared=1) "
        "AND COALESCE(is_deleted,0)=0",
        (shell_id, operator["user_id"]),
    ).fetchone()
    if shell is None:
        raise ApiError(
            422,
            "SHELL_NOT_LAUNCHABLE",
            "shell is unknown, deleted, or unavailable to this operator",
        )
    _refuse_admin_browser_chat(shell)

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
        raise ApiError(
            422,
            "HARNESS_CONVERSATION_UNSUPPORTED",
            f"harness {harness!r} has no browser conversation adapter",
        )
    selected_model = _nonblank(
        body.get("model"), "model", maximum=255, optional=True
    )
    effort = _nonblank(body.get("effort"), "effort", maximum=64, optional=True)
    adapter = run_mod.load_adapter(harness)
    try:
        resolved = run_mod.resolve_headless_route(
            harness=harness,
            adapter=adapter,
            flavor_model=(
                (defaults.get("models") or {}).get(harness)
                if defaults
                else None
            ),
            model=selected_model,
            effort=effort,
        )
    except ValueError as exc:
        raise ApiError(422, "HARNESS_ROUTE_INVALID", str(exc)) from exc
    harness = resolved.harness
    provider = resolved.provider
    model = resolved.model
    effort = resolved.effort
    title = _nonblank(body.get("title"), "title", maximum=200, optional=True)
    worktree = run_mod.shell_work_dir(shell["shortname"], shell["flavor"])
    worktree = worktree.resolve(strict=False)
    if worktree.exists() and not worktree.is_dir():
        raise ApiError(
            422,
            "HARNESS_WORKTREE_MISSING",
            "the shell worktree path exists but is not a directory",
            {"shell_id": shell_id},
        )
    request_hash = _request_hash(
        {
            "shell_id": shell_id,
            "title": title,
            "harness": harness,
            "provider": provider,
            "model": model,
            "effort": effort,
            "worktree": str(worktree),
        }
    )
    if existing is not None:
        if existing["creation_request_hash"] != request_hash:
            raise ApiError(
                409,
                "CONVERSATION_IDEMPOTENCY_CONFLICT",
                "Idempotency-Key was reused with a different request",
            )
        row = _require_conversation(
            con, existing["conversation_id"], operator["user_id"]
        )
        return _json(
            201,
            _conversation_projection(row),
            [("Location", f"/api/conversations/{row['conversation_id']}")],
        )

    if active_chat_registry.get(con, shell_id) is None:
        live_state = _wait_for_cli_release(shell)
        if live_state is not None:
            raise ApiError(
                409,
                "SHELL_BUSY",
                f"shell {shell['shortname']!r} has a live CLI session; "
                "close it before opening a browser chat",
                {"shell_id": shell_id, "state": live_state},
            )

    conversation_id = "cv_" + uuid.uuid4().hex
    closed_id = None
    with db_driver.write_transaction(con, "conversation.create.close_active"):
        existing = con.execute(
            "SELECT conversation_id,creation_request_hash FROM conversations "
            "WHERE owner_user_id=? "
            "AND creation_idempotency_key=?",
            (operator["user_id"], key),
        ).fetchone()
        if existing is not None:
            if existing["creation_request_hash"] != request_hash:
                raise ApiError(
                    409,
                    "CONVERSATION_IDEMPOTENCY_CONFLICT",
                    "Idempotency-Key was reused with a different request",
                )
            row = _require_conversation(
                con, existing["conversation_id"], operator["user_id"]
            )
            return _json(
                201,
                _conversation_projection(row),
                [("Location", f"/api/conversations/{row['conversation_id']}")],
            )

        current_shell = con.execute(
            "SELECT shell_id,display_name,shortname,flavor FROM shells "
            "WHERE shell_id=? AND (user_id=? OR is_shared=1) "
            "AND COALESCE(is_deleted,0)=0",
            (shell_id, operator["user_id"]),
        ).fetchone()
        if current_shell is None:
            raise ApiError(
                422,
                "SHELL_NOT_LAUNCHABLE",
                "shell is unknown, deleted, or unavailable to this operator",
            )
        if (
            current_shell["shortname"] != shell["shortname"]
            or current_shell["flavor"] != shell["flavor"]
        ):
            raise ApiError(
                409,
                "SHELL_CHANGED",
                "shell routing changed while the browser chat was prepared; retry",
                {"shell_id": shell_id},
            )

        if active_chat_registry.get(con, shell_id) is None:
            live_state = _live_shell_session(current_shell)
            if live_state is not None:
                raise ApiError(
                    409,
                    "SHELL_BUSY",
                    f"shell {current_shell['shortname']!r} has a live CLI session; "
                    "close it before opening a browser chat",
                    {"shell_id": shell_id, "state": live_state},
                )

        active = active_chat_registry.get(con, shell_id)
        try:
            closed = active_chat_registry.close_active(con, shell_id)
        except active_chat_registry.ActiveChatBusy as exc:
            raise ApiError(
                409,
                "BROWSER_CHAT_BUSY",
                "the active chat has a turn in progress",
                {
                    "conversation_id": (
                        active.chat_id if active is not None else None
                    )
                },
            ) from exc
        if closed is not None:
            _append_event(
                con,
                closed.chat_id,
                "conversation.closed",
                {"status": "closed", "reason": "another chat opened"},
            )
            closed_id = closed.chat_id

    # Closing is intentionally its own committed step.  If replacement
    # creation fails, the old chat stays closed and no second chat exists.
    if closed_id is not None:
        conversation_events.notify(closed_id)

    with db_driver.write_transaction(con, "conversation.create.register"):
        existing = con.execute(
            "SELECT conversation_id,creation_request_hash FROM conversations "
            "WHERE owner_user_id=? AND creation_idempotency_key=?",
            (operator["user_id"], key),
        ).fetchone()
        if existing is not None:
            if existing["creation_request_hash"] != request_hash:
                raise ApiError(
                    409,
                    "CONVERSATION_IDEMPOTENCY_CONFLICT",
                    "Idempotency-Key was reused with a different request",
                )
            row = _require_conversation(
                con, existing["conversation_id"], operator["user_id"]
            )
            return _json(
                201,
                _conversation_projection(row),
                [("Location", f"/api/conversations/{row['conversation_id']}")],
            )
        if active_chat_registry.get(con, shell_id) is not None:
            raise ApiError(
                409,
                "ACTIVE_CHAT_CHANGED",
                "another chat became active while the replacement was prepared; retry",
                {"shell_id": shell_id},
            )
        con.execute(
            "INSERT INTO conversations "
            "(conversation_id,shell_id,owner_user_id,harness,provider,model,"
            "effort,worktree,title,creation_idempotency_key,"
            "creation_request_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
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
        active_chat_registry.register(con, shell_id, conversation_id)

    conversation_events.notify(conversation_id)
    conversation_git_targets.safely_observe_and_persist(
        DB_PATH,
        conversation_id,
    )
    row = _require_conversation(con, conversation_id, operator["user_id"])
    return _json(
        201,
        _conversation_projection(row),
        [("Location", f"/api/conversations/{conversation_id}")],
    )


def _list_conversations(con, operator: dict, query):
    if "mode" in query:
        raise ApiError(422, "VALIDATION_ERROR", "unknown query field: mode")
    limit = _limit(query, maximum=100)
    clauses = ["c.owner_user_id=?"]
    params: list = [operator["user_id"]]
    shell = query.get("shell_id", [None])[0]
    shell_id = None
    if shell not in (None, ""):
        shell_id = _integer(shell, "shell_id")
        clauses.append("c.shell_id=?")
        params.append(shell_id)
    state = query.get("state", [None])[0]
    if state:
        if state not in _CONVERSATION_STATES:
            raise ApiError(422, "VALIDATION_ERROR", "invalid conversation state")
        clauses.append("c.state=?")
        params.append(state)
    starred = _strict_boolean(query, "starred")
    if starred is not None:
        clauses.append("c.starred=?")
        params.append(int(starred))
    open_only = _strict_boolean(query, "open")
    if open_only is not None:
        compatible = state != "closed" if open_only else state in (None, "closed")
        if state is not None and not compatible:
            raise ApiError(
                422,
                "VALIDATION_ERROR",
                "open and state filters cannot both be true",
            )
        clauses.append("c.state!='closed'" if open_only else "c.state='closed'")
    scope = {
        "owner_user_id": int(operator["user_id"]),
        "shell_id": shell_id,
        "starred": starred,
        "open": open_only,
        "state": state or None,
    }
    cursor = query.get("cursor", [None])[0]
    if cursor:
        decoded = _cursor_decode(cursor, "conversation")
        if not isinstance(decoded.get("a"), str) or not _ID.fullmatch(
            str(decoded.get("id", ""))
        ):
            raise ApiError(422, "CURSOR_INVALID", "invalid conversation cursor")
        if decoded.get("scope") != scope:
            raise ApiError(
                422,
                "CURSOR_INVALID",
                "conversation cursor does not match the requested filter scope",
            )
        clauses.append(
            "(c.last_activity_at<? OR (c.last_activity_at=? AND c.conversation_id<?))"
        )
        params.extend((decoded["a"], decoded["a"], decoded["id"]))
    rows = con.execute(
        "SELECT c.conversation_id,c.shell_id,c.owner_user_id,c.harness,"
        "c.provider,c.model,c.effort,c.state,c.title,c.starred,"
        "c.conversation_scope,c.created_at,"
        "c.last_activity_at,c.closed_at,c.version,s.display_name,s.shortname,"
        "CASE WHEN c.state!='closed' THEN ("
        " SELECT requested.created_at FROM conversation_events requested "
        " WHERE requested.conversation_id=c.conversation_id "
        " AND " + _CLOSE_REQUESTED_AFTER_REOPEN +
        " ORDER BY requested.sequence DESC LIMIT 1"
        ") END AS close_requested_at,"
        + _SPRINT_MANAGED_COLUMN
        + " FROM conversations c JOIN shells s ON s.shell_id=c.shell_id WHERE "
        + " AND ".join(clauses)
        + " ORDER BY c.last_activity_at DESC,c.conversation_id DESC LIMIT ?",
        (*params, limit + 1),
    ).fetchall()
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = _cursor_encode(
            {
                "v": 1,
                "a": last["last_activity_at"],
                "id": last["conversation_id"],
                "scope": scope,
            }
        )
    return _json(
        200,
        {
            "items": [_conversation_projection(row) for row in page],
            "next_cursor": next_cursor,
        },
    )


def _patch_conversation(con, operator: dict, conversation_id: str, body: dict):
    _only_fields(body, {"version", "title", "state", "starred"})
    if "version" not in body:
        raise ApiError(
            422, "VALIDATION_ERROR", "version is required for conversation updates"
        )
    version = _integer(body["version"], "version")
    if version <= 0:
        raise ApiError(422, "VALIDATION_ERROR", "version must be positive")
    if not {"title", "state", "starred"}.intersection(body):
        raise ApiError(422, "VALIDATION_ERROR", "no conversation change supplied")
    title = (
        _nonblank(body.get("title"), "title", maximum=200, optional=True)
        if "title" in body
        else None
    )
    if "starred" in body and not isinstance(body["starred"], bool):
        raise ApiError(422, "VALIDATION_ERROR", "starred must be a boolean")
    if "state" in body and body["state"] != "closed":
        raise ApiError(422, "VALIDATION_ERROR", "state may only be changed to closed")

    active_run_id = None
    with db_driver.write_transaction(con, "conversation.patch"):
        row = _require_conversation(con, conversation_id, operator["user_id"])
        if int(row["version"]) != version:
            raise ApiError(
                409,
                "CONVERSATION_VERSION_CONFLICT",
                "conversation version does not match",
                {"expected": int(row["version"]), "received": version},
            )
        closing = body.get("state") == "closed"
        if row["state"] == "closed" and {"title", "state"}.intersection(body):
            raise ApiError(
                409,
                "CONVERSATION_CLOSED",
                "conversation is already closed",
            )
        sets = []
        params: list = []
        if "title" in body:
            sets.append("title=?")
            params.append(title)
        if "starred" in body:
            sets.append("starred=?")
            params.append(int(body["starred"]))
        if closing:
            if sets:
                con.execute(
                    f"UPDATE conversations SET {','.join(sets)} "
                    "WHERE conversation_id=?",
                    (*params, conversation_id),
                )
                _append_event(
                    con,
                    conversation_id,
                    "conversation.updated",
                    {
                        **({"title": title} if "title" in body else {}),
                        **(
                            {"starred": body["starred"]}
                            if "starred" in body
                            else {}
                        ),
                    },
                )
            _begin_close(con, conversation_id, row["state"])
            _enable_planner_coordinate_mode(
                con,
                shell_id=int(row["shell_id"]),
                conversation_id=conversation_id,
            )
        else:
            sets.insert(0, "version=version+1")
            if "title" in body:
                sets.append("last_activity_at=datetime('now')")
            changed = con.execute(
                f"UPDATE conversations SET {','.join(sets)} "
                "WHERE conversation_id=? AND owner_user_id=? AND version=?",
                (*params, conversation_id, operator["user_id"], version),
            ).rowcount
            if changed != 1:
                raise ApiError(
                    409,
                    "CONVERSATION_VERSION_CONFLICT",
                    "conversation changed concurrently",
                )
            _append_event(
                con,
                conversation_id,
                (
                    "conversation.renamed"
                    if set(body) == {"version", "title"}
                    else "conversation.updated"
                ),
                {
                    **({"title": title} if "title" in body else {}),
                    **(
                        {"starred": body["starred"]}
                        if "starred" in body
                        else {}
                    ),
                },
            )
    conversation_events.notify(conversation_id)
    if active_run_id is not None:
        _deliver_close_interrupt(active_run_id)
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

    auto_closed: list[str] = []
    reopened = False
    with db_driver.write_transaction(con, "conversation.message.create"):
        conversation = _require_conversation(
            con,
            conversation_id,
            operator["user_id"],
        )
        existing = con.execute(
            "SELECT message_id,request_hash FROM conversation_messages "
            "WHERE conversation_id=? AND idempotency_key=?",
            (conversation_id, key),
        ).fetchone()
        if existing is not None:
            if existing["request_hash"] != request_hash:
                raise ApiError(
                    409,
                    "MESSAGE_IDEMPOTENCY_CONFLICT",
                    "Idempotency-Key was reused with a different message",
                )
            message = _message_projection(
                _message_row(con, int(existing["message_id"]))
            )
            return _json(
                202,
                {
                    "message": message,
                    "queue_position": _accepted_queue_position(
                        con,
                        message["message_id"],
                    ),
                },
                [("Location", f"/api/conversations/{conversation_id}/messages")],
            )
        if conversation["state"] == "closed":
            auto_closed = _reopen_conversation(con, operator, conversation)
            reopened = True
        elif _close_requested(con, conversation_id):
            raise ApiError(
                409,
                "CONVERSATION_CLOSING",
                "the conversation is stopping active work before it closes",
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
            "INSERT INTO conversation_outbox (conversation_id,message_id) "
            "VALUES (?,?)",
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
    for closed_id in auto_closed:
        conversation_events.notify(closed_id)
    conversation_events.notify(conversation_id)
    if reopened:
        conversation_git_targets.safely_observe_and_persist(
            DB_PATH,
            conversation_id,
        )
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

    replay = False
    with db_driver.write_transaction(con, "conversation.interrupt.create"):
        _require_conversation(con, conversation_id, operator["user_id"])
        existing = con.execute(
            "SELECT message_id,request_hash,body FROM conversation_messages "
            "WHERE conversation_id=? AND idempotency_key=?",
            (conversation_id, key),
        ).fetchone()
        if existing is not None:
            if existing["request_hash"] != request_hash:
                raise ApiError(
                    409,
                    "MESSAGE_IDEMPOTENCY_CONFLICT",
                    "Idempotency-Key was reused with a different interruption",
                )
            audit = _message_projection(
                _message_row(con, int(existing["message_id"]))
            )
            run_id = int(json.loads(existing["body"])["run_id"])
            replay = True
        else:
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
            audit = _message_projection(_message_row(con, message_id))
    _request_interrupt(run_id, replay=replay)
    return _json(
        202,
        {
            "interruption": audit,
            "run_id": run_id,
        },
    )


def _utf8_prefix(value: str, maximum: int) -> str:
    raw = value.encode()
    if len(raw) <= maximum:
        return value
    return raw[:maximum].decode("utf-8", errors="ignore")


def _transcript_activity_label(event_type: str, payload: dict) -> str:
    if event_type == "permission.requested":
        label = "Waiting for permission"
    elif event_type == "input.requested":
        label = "Waiting for input"
    elif event_type == "run.interrupted":
        label = "Turn interrupted"
    elif event_type == "run.unknown":
        label = "Turn outcome could not be proven"
    else:
        detail = payload.get("error") or payload.get("detail")
        label = f"Turn failed — {detail}" if detail else "Turn failed"
    return _utf8_prefix(label, TRANSCRIPT_MAX_ACTIVITY_LABEL_BYTES)


def _transcript_size(projection: dict) -> int:
    return len(
        json.dumps(
            projection,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    )


def _transcript_truncation(
    *,
    reason: str,
    omitted_message_count: int,
    omitted_source_event_count: int,
    omitted_through_sequence: int,
    retained_from_sequence: int,
) -> dict:
    return {
        "reason": reason,
        "omitted_message_count": max(0, int(omitted_message_count)),
        "omitted_source_event_count": max(
            0, int(omitted_source_event_count)
        ),
        "omitted_through_sequence": max(0, int(omitted_through_sequence)),
        "retained_from_sequence": max(0, int(retained_from_sequence)),
    }


def _bound_transcript_response(
    projection: dict,
    *,
    limits: TranscriptLimits,
    total_messages: int,
    total_events: int,
) -> None:
    if _transcript_size(projection) <= limits.max_response_bytes:
        return

    items = projection["items"]
    projection["truncation"] = _transcript_truncation(
        reason="response_byte_limit",
        omitted_message_count=max(
            0,
            total_messages
            - len({item["message_id"] for item in items if item["kind"] == "user"}),
        ),
        omitted_source_event_count=0,
        omitted_through_sequence=0,
        retained_from_sequence=min(
            (int(item["order_sequence"]) for item in items),
            default=projection["through_sequence"],
        ),
    )

    def group_key(item: dict) -> tuple[str, int | str]:
        message_id = item.get("message_id")
        return (
            ("message", int(message_id))
            if message_id is not None
            else ("item", item["item_id"])
        )

    while _transcript_size(projection) > limits.max_response_bytes:
        keys = []
        for item in items:
            key = group_key(item)
            if key not in keys:
                keys.append(key)
        if len(keys) <= 1:
            break
        removed = keys[0]
        items[:] = [item for item in items if group_key(item) != removed]

    retained_from = min(
        (int(item["order_sequence"]) for item in items),
        default=projection["through_sequence"],
    )
    omitted_events = min(
        total_events,
        max(0, retained_from - 1),
    )
    retained_users = {
        item["message_id"] for item in items if item["kind"] == "user"
    }
    projection["truncation"] = _transcript_truncation(
        reason="response_byte_limit",
        omitted_message_count=max(0, total_messages - len(retained_users)),
        omitted_source_event_count=min(total_events, omitted_events),
        omitted_through_sequence=max(0, retained_from - 1),
        retained_from_sequence=retained_from,
    )

    for item in sorted(
        items,
        key=lambda candidate: len(str(candidate.get("text", "")).encode()),
        reverse=True,
    ):
        if _transcript_size(projection) <= limits.max_response_bytes:
            break
        text = item.get("text")
        if not isinstance(text, str) or not text:
            continue
        low = 0
        high = len(text.encode())
        while low < high:
            middle = (low + high + 1) // 2
            candidate = _utf8_prefix(text, middle)
            item["text"] = candidate
            item["text_truncated"] = True
            if _transcript_size(projection) <= limits.max_response_bytes:
                low = middle
            else:
                high = middle - 1
        item["text"] = _utf8_prefix(text, low)
        item["text_truncated"] = True

    if _transcript_size(projection) > limits.max_response_bytes:
        raise ApiError(
            503,
            "TRANSCRIPT_PROJECTION_UNAVAILABLE",
            "the transcript control envelope exceeds its response limit",
        )


def _transcript_projection(
    con,
    conversation_id: str,
    *,
    owner_user_id: int,
    limits: TranscriptLimits = DEFAULT_TRANSCRIPT_LIMITS,
) -> dict:
    if con.in_transaction:
        raise RuntimeError("transcript projection requires a fresh read view")

    con.execute("BEGIN")
    try:
        conversation = _require_conversation(
            con,
            conversation_id,
            owner_user_id,
        )
        through_sequence = int(
            con.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM conversation_events "
                "WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()[0]
        )
        message_rows = con.execute(
            "WITH ranked AS ("
            " SELECT message_id,body,state,created_at,completed_at,"
            "ROW_NUMBER() OVER (ORDER BY message_id DESC) AS source_rank,"
            "COUNT(*) OVER() AS total_messages,"
            "SUM(length(CAST(body AS BLOB))) OVER ("
            " ORDER BY message_id DESC ROWS UNBOUNDED PRECEDING"
            ") AS message_source_bytes "
            " FROM conversation_messages "
            " WHERE conversation_id=? AND message_kind='prompt'"
            ") SELECT * FROM ranked WHERE source_rank<=? "
            "AND (message_source_bytes<=? OR source_rank=1) "
            "ORDER BY message_id",
            (
                conversation_id,
                limits.max_turns,
                limits.max_source_bytes,
            ),
        ).fetchall()
        total_messages = (
            int(message_rows[0]["total_messages"]) if message_rows else 0
        )
        message_source_bytes = max(
            (int(row["message_source_bytes"]) for row in message_rows),
            default=0,
        )
        remaining_source_bytes = max(
            1,
            limits.max_source_bytes - message_source_bytes,
        )
        message_ids = [int(row["message_id"]) for row in message_rows]
        marks = ",".join("?" for _ in message_ids)
        run_where = (
            f"r.trigger_message_id IN ({marks})"
            if marks
            else "0"
        )
        active_run_id = conversation["active_run_id"]
        run_rows = con.execute(
            "SELECT r.run_id,r.trigger_message_id,r.state,"
            "r.started_at,r.ended_at,r.error_code,r.error_detail,"
            "r.harness_session_before,r.harness_session_after,r.runner_ref,"
            "(SELECT COUNT(*) FROM conversation_events evidence_count "
            " WHERE evidence_count.run_id=r.run_id "
            " AND evidence_count.event_type IN "
            " ('assistant.delta','tool.started','tool.completed',"
            "'permission.requested','input.requested') "
            " AND evidence_count.sequence<=?) AS segmentation_evidence_count,"
            "(SELECT COALESCE(MAX(boundary.sequence),0) "
            " FROM conversation_events boundary "
            " WHERE boundary.run_id=r.run_id "
            " AND boundary.event_type IN "
            " ('tool.started','tool.completed','permission.requested',"
            "'input.requested') "
            " AND boundary.sequence<=?) AS latest_boundary_sequence "
            "FROM conversation_runs r WHERE r.conversation_id=? AND ("
            + run_where
            + (" OR r.run_id=?" if active_run_id is not None else "")
            + ") ORDER BY r.run_id",
            (
                through_sequence,
                through_sequence,
                conversation_id,
                *message_ids,
                *((int(active_run_id),) if active_run_id is not None else ()),
            ),
        ).fetchall()
        event_rows = con.execute(
            "WITH ranked AS ("
            " SELECT sequence,event_type,payload_version,payload,message_id,"
            "run_id,created_at,"
            "ROW_NUMBER() OVER (ORDER BY sequence DESC) AS source_rank,"
            "COUNT(*) OVER () AS total_events,"
            "SUM(length(CAST(payload AS BLOB))) OVER ("
            " ORDER BY sequence DESC ROWS UNBOUNDED PRECEDING"
            ") AS source_bytes,"
            "COUNT(*) OVER ("
            " ORDER BY sequence ROWS BETWEEN UNBOUNDED PRECEDING "
            " AND 1 PRECEDING"
            ") AS older_count,"
            "LAG(sequence) OVER (ORDER BY sequence) AS prior_sequence,"
            "MAX(CASE WHEN run_id IS NOT NULL AND event_type IN "
            " ('tool.started','tool.completed','permission.requested',"
            "'input.requested') THEN sequence ELSE 0 END) OVER ("
            " PARTITION BY run_id ORDER BY sequence "
            " ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW"
            ") AS segment_anchor_sequence "
            " FROM conversation_events "
            " WHERE conversation_id=? AND sequence<=?"
            ") SELECT * FROM ranked "
            "WHERE source_rank<=? "
            "AND (source_bytes<=? OR source_rank=1) "
            "ORDER BY sequence",
            (
                conversation_id,
                through_sequence,
                limits.max_source_events,
                remaining_source_bytes,
            ),
        ).fetchall()

        total_events = (
            int(event_rows[0]["total_events"]) if event_rows else 0
        )
        secrets = tuple(sorted({
            value
            for value in (
                conversation["harness_session_ref"],
                *(
                    candidate
                    for row in run_rows
                    for candidate in (
                        row["harness_session_before"],
                        row["harness_session_after"],
                        row["runner_ref"],
                    )
                ),
            )
            if isinstance(value, str) and value
        }, key=len, reverse=True))

        warnings = []
        events = []
        for row in event_rows:
            sequence = int(row["sequence"])
            if int(row["payload_version"]) != 1:
                if len(warnings) < TRANSCRIPT_MAX_WARNINGS:
                    warnings.append({
                        "sequence": sequence,
                        "code": "UNSUPPORTED_PAYLOAD_VERSION",
                    })
                continue
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                payload = None
            if not isinstance(payload, dict):
                if len(warnings) < TRANSCRIPT_MAX_WARNINGS:
                    warnings.append({
                        "sequence": sequence,
                        "code": "MALFORMED_EVENT_PAYLOAD",
                    })
                continue
            if (
                row["event_type"] == "assistant.delta"
                and not isinstance(payload.get("text"), str)
            ):
                if len(warnings) < TRANSCRIPT_MAX_WARNINGS:
                    warnings.append({
                        "sequence": sequence,
                        "code": "MALFORMED_EVENT_PAYLOAD",
                    })
                continue
            projected_payload = _project_event_payload(
                row["event_type"],
                payload,
                secrets,
            )
            events.append({
                "sequence": sequence,
                "event_type": row["event_type"],
                "payload": projected_payload,
                "message_id": row["message_id"],
                "run_id": row["run_id"],
                "created_at": row["created_at"],
                "segment_anchor_sequence": int(row["segment_anchor_sequence"]),
            })

        accepted_sequence = {
            int(event["message_id"]): event["sequence"]
            for event in events
            if event["event_type"] == "message.accepted"
            and event["message_id"] is not None
        }
        segments_by_run: dict[int, dict[int, list[dict]]] = {}
        context_tokens_by_run: dict[int, dict[int, int]] = {}
        latest_assistant_anchor_by_run: dict[int, int] = {}
        evidence_count_by_run: dict[int, int] = {}
        activities = []
        boundary_types = {
            "tool.started",
            "tool.completed",
            "permission.requested",
            "input.requested",
        }
        activity_types = {
            "permission.requested",
            "input.requested",
            "run.failed",
            "run.interrupted",
            "run.unknown",
        }
        for event in events:
            run_id = event["run_id"]
            if run_id is not None and (
                event["event_type"] == "assistant.delta"
                or event["event_type"] in boundary_types
            ):
                evidence_count_by_run[int(run_id)] = (
                    evidence_count_by_run.get(int(run_id), 0) + 1
                )
            if event["event_type"] == "assistant.delta" and run_id is not None:
                anchor = event["segment_anchor_sequence"]
                segments_by_run.setdefault(int(run_id), {}).setdefault(
                    anchor,
                    [],
                ).append(event)
                latest_assistant_anchor_by_run[int(run_id)] = anchor
            elif event["event_type"] == "usage" and run_id is not None:
                context_tokens = event["payload"].get("context_tokens")
                assistant_anchor = latest_assistant_anchor_by_run.get(int(run_id))
                if isinstance(context_tokens, int) and not isinstance(
                    context_tokens,
                    bool,
                ) and assistant_anchor is not None:
                    context_tokens_by_run.setdefault(int(run_id), {})[
                        assistant_anchor
                    ] = context_tokens
            elif event["event_type"] in activity_types:
                activities.append(event)

        run_by_message: dict[int, list] = {}
        run_by_id = {}
        for row in run_rows:
            run_by_id[int(row["run_id"])] = row
            run_by_message.setdefault(
                int(row["trigger_message_id"]),
                [],
            ).append(row)

        items = []
        retained_messages = set()
        retained_runs = set()
        for message in message_rows:
            message_id = int(message["message_id"])
            order_sequence = accepted_sequence.get(message_id)
            message_runs = run_by_message.get(message_id, [])
            projected_runs = [
                run
                for run in message_runs
                if (
                    evidence_count_by_run.get(int(run["run_id"]), 0)
                    == int(run["segmentation_evidence_count"])
                    or run["state"] in ("leased", "starting", "running")
                )
            ]
            if order_sequence is None or (message_runs and not projected_runs):
                continue
            retained_messages.add(message_id)
            retained_runs.update(int(run["run_id"]) for run in projected_runs)
            items.append({
                "item_id": f"message:{message_id}",
                "kind": "user",
                "order_sequence": order_sequence,
                "message_id": message_id,
                "run_id": None,
                "created_at": message["created_at"],
                "text": message["body"],
                "state": message["state"],
                "completed_at": message["completed_at"],
                "text_truncated": False,
            })
            for run in projected_runs:
                run_id = int(run["run_id"])
                segments = segments_by_run.get(run_id, {})
                for anchor, deltas in sorted(
                    segments.items(),
                    key=lambda entry: entry[1][0]["sequence"],
                ):
                    items.append({
                        "item_id": f"run:{run_id}:assistant:{anchor}",
                        "kind": "assistant",
                        "order_sequence": deltas[0]["sequence"],
                        "message_id": message_id,
                        "run_id": run_id,
                        "created_at": deltas[0]["created_at"],
                        "text": "".join(
                            delta["payload"]["text"] for delta in deltas
                        ),
                        "outcome": run["state"],
                        "segment_anchor_sequence": anchor,
                        "first_sequence": deltas[0]["sequence"],
                        "last_sequence": deltas[-1]["sequence"],
                        "context_tokens": context_tokens_by_run.get(
                            run_id,
                            {},
                        ).get(anchor),
                        "text_truncated": False,
                    })

        for event in activities:
            message_id = (
                int(event["message_id"])
                if event["message_id"] is not None
                else None
            )
            if message_id is not None and message_id not in retained_messages:
                continue
            run_id = event["run_id"]
            if run_id is not None and int(run_id) not in retained_runs:
                continue
            items.append({
                "item_id": f"event:{event['sequence']}",
                "kind": "activity",
                "order_sequence": event["sequence"],
                "message_id": message_id,
                "run_id": (
                    int(run_id) if run_id is not None else None
                ),
                "created_at": event["created_at"],
                "activity_type": event["event_type"],
                "label": _transcript_activity_label(
                    event["event_type"],
                    event["payload"],
                ),
                "sequence": event["sequence"],
            })
        items.sort(key=lambda item: (item["order_sequence"], item["item_id"]))

        message_source_limited = (
            len(message_rows) < min(total_messages, limits.max_turns)
        )
        source_reason = "source_byte_limit" if message_source_limited else None
        if len(event_rows) < total_events:
            if len(event_rows) < min(total_events, limits.max_source_events):
                source_reason = "source_byte_limit"
            elif source_reason is None:
                source_reason = "source_event_limit"
        turn_limited = total_messages > len(message_rows)
        reason = source_reason or ("turn_limit" if turn_limited else None)
        retained_from = min(
            (int(item["order_sequence"]) for item in items),
            default=through_sequence,
        )
        earliest_source = event_rows[0] if event_rows else None
        omitted_rows = [
            row for row in event_rows
            if int(row["sequence"]) < retained_from
        ]
        source_omitted = 0
        omitted_through = 0
        if earliest_source is not None and reason:
            source_omitted = (
                int(earliest_source["older_count"]) + len(omitted_rows)
            )
            omitted_through = max(
                int(earliest_source["prior_sequence"] or 0),
                max(
                    (int(row["sequence"]) for row in omitted_rows),
                    default=0,
                ),
            )
        projection = {
            "conversation_id": conversation_id,
            "projection_version": TRANSCRIPT_PROJECTION_VERSION,
            "through_sequence": through_sequence,
            "controls": {
                "conversation_version": int(conversation["version"]),
                "conversation_state": conversation["state"],
                "queued_count": int(conversation["queued_count"]),
                "active_run_id": (
                    int(active_run_id) if active_run_id is not None else None
                ),
                "close_requested_at": conversation["close_requested_at"],
            },
            "items": items,
            "truncation": (
                _transcript_truncation(
                    reason=reason,
                    omitted_message_count=max(
                        0, total_messages - len(retained_messages)
                    ),
                    omitted_source_event_count=source_omitted,
                    omitted_through_sequence=omitted_through,
                    retained_from_sequence=retained_from,
                )
                if reason
                else None
            ),
        }
        if active_run_id is not None and int(active_run_id) in run_by_id:
            active_run = run_by_id[int(active_run_id)]
            projection["assistant_cursor"] = {
                "run_id": int(active_run_id),
                "segment_anchor_sequence": int(
                    active_run["latest_boundary_sequence"]
                ),
            }
        if warnings:
            projection["warnings"] = warnings
        _bound_transcript_response(
            projection,
            limits=limits,
            total_messages=total_messages,
            total_events=total_events,
        )
        return projection
    finally:
        con.rollback()


def handle(method: str, path: str, headers_raw: str, raw_body: bytes) -> tuple:
    headers = _parse_headers(headers_raw)
    if not _host_ok(headers):
        return _err(
            403,
            "HOST_NOT_ALLOWED",
            "conversation API serves 127.0.0.1/localhost only",
        )
    parsed = urlparse(path)
    query = parse_qs(parsed.query, keep_blank_values=True)
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
            transcript = _TRANSCRIPT_PATH.fullmatch(parsed.path)
            if transcript and method == "GET":
                return _json(
                    200,
                    _transcript_projection(
                        con,
                        transcript.group(1),
                        owner_user_id=operator["user_id"],
                    ),
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
        except db_driver.OperationalError as exc:
            if con.in_transaction:
                con.rollback()
            if db_driver.is_busy_error(exc):
                return _err(
                    503,
                    "ENGINE_DB_BUSY",
                    "engine database is busy; retry this request",
                    {"retry_after": 2},
                )
            return _err(
                500,
                "INTERNAL_ERROR",
                "conversation request failed",
            )
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


def _token_count(value) -> int | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        or int(value) != value
    ):
        return None
    return int(value)


def _usage_context_tokens(payload: dict) -> int | None:
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        return None
    for key in ("totalTokens", "total_tokens", "total"):
        total = _token_count(tokens.get(key))
        if total is not None:
            return total
    fields = (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    )
    values = [_token_count(tokens.get(key)) for key in fields]
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def _project_event_payload(
    event_type: str,
    payload: dict,
    secrets: tuple[str, ...],
) -> dict:
    projected = _redact_event_value(payload, secrets)
    if event_type != "usage":
        return projected
    context_tokens = _usage_context_tokens(projected)
    if context_tokens is not None:
        projected["context_tokens"] = context_tokens
    return projected


def _event_projection(row, secrets: tuple[str, ...]) -> dict:
    payload = json.loads(row["payload"])
    return {
        "sequence": int(row["sequence"]),
        "event_type": row["event_type"],
        "payload_version": int(row["payload_version"]),
        "payload": _project_event_payload(row["event_type"], payload, secrets),
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
    if cursor not in (None, "") and raw_after not in (None, ""):
        raise ApiError(
            422,
            "CURSOR_INVALID",
            "cursor and after are mutually exclusive",
        )
    if cursor:
        decoded = _cursor_decode(cursor, "event")
        after = _integer(decoded.get("sequence"), "event sequence")
    elif raw_after:
        after = _integer(raw_after, "after")
    else:
        after = 0
    if last_event not in (None, ""):
        after = max(after, _integer(last_event, "Last-Event-ID"))
    if after < 0:
        raise ApiError(422, "CURSOR_INVALID", "event sequence cannot be negative")
    return after


def _event_batch(conversation_id: str, after: int) -> list[dict]:
    con = _db()
    try:
        con.execute("BEGIN")
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
        if con.in_transaction:
            con.rollback()
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
