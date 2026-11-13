"""Browser-chat compaction: the window setting, the meter, the re-ground line,
the manual request and the cold-return rule.

Feature #95. The engine summarises nothing. It asks each harness to compact
earlier, records that a compaction happened, reads how full a chat is from
what the harness reports, tells the shell to re-read its durable state on
the first message after a compaction, lets the operator ask the harness to
compact now, and says when returning to a large idle chat will be expensive.

Most of this is pure functions over values the caller already holds, so the
broker (dispatch) and the API (projection) read one definition. Spec #275
adds the engine's own requests: one shared, non-HTTP enqueue helper that the
button, the between-turn threshold check and the idle scheduler all use, and
the two rules that call it. The per-harness settings they read live in
``compaction_settings``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

LOG = logging.getLogger(__name__)

# The engine-added line. It is appended to the dispatched text only: the
# stored message, the boot file and the system prompt are never touched, so
# the cached prefix of the chat is unchanged.
# Worded as a direct instruction with the exact commands (decision #451): in
# live tests an advisory line was read and not acted on.
REGROUND_LINE = (
    "Your context was compacted. Your first action this turn, before you "
    "reply: run `sc mem get state`. If you carry a task or work unit, also "
    "run `sc context --task <id>` or `sc context --work-unit <id>`."
)

# The environment fallback for Claude's and Codex's auto-compaction trigger,
# read from the server process environment like the reaper's: a token count,
# or ``off``. Since spec #275 a saved per-harness row wins over it and it
# never touches idle compaction; ``compaction_settings`` resolves the order.
DEFAULT_COMPACT_WINDOW = 250_000
COMPACT_WINDOW_OFF = "off"
COMPACT_WINDOW_ENV = {
    "claude": "SC_CHAT_COMPACT_WINDOW_CLAUDE",
    "codex": "SC_CHAT_COMPACT_WINDOW_CODEX",
}

# The manual compact request. It travels the chat's own queue as an
# engine-sent control message, so it is ordered with the operator's messages
# and never runs beside a turn. It is not an operator message: the transcript
# lists prompts only, and the request is recorded as ``compaction.requested``.
MANUAL_COMPACT_SENDER = "compact-button"
# Stored as the control message's body. Claude takes it as the prompt of a
# resumed headless run; the other harnesses are asked through their API and
# never see this text.
MANUAL_COMPACT_BODY = "/compact"
COMPACTION_REQUESTED = "compaction.requested"
COMPACTION_FAILED = "compaction.failed"
COMPACTION_NOT_REPORTED = "compaction.not_reported"
# What a manual request's own run may write to the chat's log. The summary a
# harness streams while it compacts is never among them. The tokens the
# summarising request spent are kept, under their own scope: they are a cost
# of the chat, never a reading of how full its context is.
MANUAL_COMPACT_RUN_EVENTS = frozenset(
    {"session.started", "run.started", "context.compacted", "usage"}
)
COMPACTION_USAGE_SCOPE = "compaction"
# The events that say the model answered: what "last model activity" reads.
MODEL_ACTIVITY_EVENT_TYPES = ("usage", "context.compacted")

# Why a chat compacted (spec #275 "Compaction requests"). The first three are
# the engine's own requests and are written as ``cause`` on
# ``compaction.requested``; the broker copies the request's cause onto the
# outcome it records. A compaction the harness made on its own, with no
# engine request behind it, is ``harness``.
CAUSE_OPERATOR = "operator"
CAUSE_THRESHOLD = "threshold"
CAUSE_IDLE = "idle"
CAUSE_HARNESS = "harness"
CAUSES = (CAUSE_OPERATOR, CAUSE_THRESHOLD, CAUSE_IDLE, CAUSE_HARNESS)
ENGINE_CAUSES = frozenset({CAUSE_OPERATOR, CAUSE_THRESHOLD, CAUSE_IDLE})

# The refusals the shared enqueue helper returns, worded as the compact
# button has always worded them.
REFUSAL_MESSAGES = {
    "CONVERSATION_NOT_FOUND": "conversation does not exist",
    "MESSAGE_IDEMPOTENCY_CONFLICT": (
        "Idempotency-Key was reused with a different request"
    ),
    "COMPACTION_UNSUPPORTED": "{harness} has no manual compaction trigger",
    "CONVERSATION_CLOSED": "the conversation is closed",
    "CONVERSATION_CLOSING": (
        "the conversation is stopping active work before it closes"
    ),
    "COMPACTION_NO_SESSION": "this chat has no harness session to compact yet",
    "COMPACTION_PENDING": "a compaction is already in progress for this chat",
    "CONVERSATION_BUSY": (
        "the chat is working; compact it when the turn has finished"
    ),
}

# The cold-return notice: a chat this large, idle this long, has lost its
# provider cache, so the next message re-reads the whole context uncached.
# Both are engine settings, read from the server environment like the window.
COLD_RETURN_TOKENS = 60_000
COLD_RETURN_IDLE_MINUTES = 60
COLD_RETURN_TOKENS_ENV = "SC_CHAT_COLD_RETURN_TOKENS"
COLD_RETURN_IDLE_MINUTES_ENV = "SC_CHAT_COLD_RETURN_MINUTES"

# Newest events read to find the latest meter reading and window.
METER_EVENT_LIMIT = 200
METER_EVENT_TYPES = ("usage", "context.compacted")


def compact_window(
    harness: str,
    env: Mapping[str, str] | None = None,
) -> int | None:
    """The compact window to pass ``harness`` for a browser chat, or None.

    None means "say nothing; the harness default applies": the harness has
    no window setting in this delivery (Kimi, OpenCode), or the operator set
    ``off``. The value is passed through as set; the engine invents no floor
    and leaves the harness to apply its own minimum.
    """
    name = COMPACT_WINDOW_ENV.get(harness)
    if name is None:
        return None
    raw = (os.environ if env is None else env).get(name, "").strip()
    if not raw:
        return DEFAULT_COMPACT_WINDOW
    if raw.lower() == COMPACT_WINDOW_OFF:
        return None
    try:
        value = int(raw.replace("_", "").replace(",", ""))
    except ValueError:
        value = 0
    if value <= 0:
        LOG.warning(
            "%s=%r is not a token count or 'off'; using the default %d",
            name,
            raw,
            DEFAULT_COMPACT_WINDOW,
        )
        return DEFAULT_COMPACT_WINDOW
    return value


def manual_compact_sql(alias: str = "m") -> str:
    """SQL that is true for the engine's own compact request message.

    ``alias`` names a ``conversation_messages`` row. The sender is the test:
    an operator's message is never sent by the engine under this name.
    """
    return (
        f"({alias}.sender_kind='engine' AND {alias}.message_kind='control' "
        f"AND {alias}.sender_ref='{MANUAL_COMPACT_SENDER}')"
    )


def compaction_usage_payload(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """The usage of a manual compaction's own request, as it is stored.

    None when the harness stated no tokens at all (a refused request spends
    none). The figure is the summarising request's, so it never carries a
    context size: the meter reads ``request`` readings only.
    """
    tokens = payload.get("tokens")
    if not isinstance(tokens, Mapping) or not any(
        isinstance(value, (int, float)) and not isinstance(value, bool) and value
        for value in tokens.values()
    ):
        return None
    stored = {
        key: value for key, value in payload.items() if key != "context_tokens"
    }
    stored["scope"] = COMPACTION_USAGE_SCOPE
    return stored


def settle_cancelled_requests(
    con,
    conversation_id: str,
    message_ids: Iterable[int],
    *,
    error_code: str,
    error_detail: str | None = None,
) -> int:
    """Give each cancelled manual compact request its one result line.

    For the paths that cancel a run or a queued message without the broker
    finishing it (the reaper, a chat closed or displaced with work queued):
    every press still ends in a line, here "Compaction stopped". Messages
    that are not compact requests, and requests that already have a result,
    are left alone. Runs inside the caller's write transaction.
    """
    from conversation_broker import BrokerStore

    written = 0
    for message_id in message_ids:
        request = con.execute(
            "SELECT 1 FROM conversation_messages m WHERE m.message_id=? AND "
            + manual_compact_sql("m"),
            (int(message_id),),
        ).fetchone()
        if request is None:
            continue
        sequence = BrokerStore._append_compaction_outcome(
            con,
            conversation_id=conversation_id,
            message_id=int(message_id),
            outcome="cancelled",
            error_code=error_code,
            error_detail=error_detail,
        )
        if sequence is not None:
            written += 1
    return written


def last_model_activity(rows: Iterable[Mapping[str, Any]]) -> str | None:
    """When the chat's model last answered, or None when it never has.

    ``rows`` are the chat's events, newest first; the answer is the time of
    the newest usage or compaction event among them. Closing, reopening,
    renaming, stopping and a refused compact request all stamp the chat's
    ``last_activity_at``; none of them warms a provider cache, so the
    cold-return rule reads this instead.
    """
    for row in rows:
        if row["event_type"] in MODEL_ACTIVITY_EVENT_TYPES:
            return row["created_at"]
    return None


def _positive_setting(
    name: str,
    default: int,
    env: Mapping[str, str] | None,
) -> int:
    raw = (os.environ if env is None else env).get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw.replace("_", "").replace(",", ""))
    except ValueError:
        value = 0
    if value <= 0:
        LOG.warning("%s=%r is not a positive number; using %d", name, raw, default)
        return default
    return value


def cold_return_settings(env: Mapping[str, str] | None = None) -> dict[str, int]:
    """The two thresholds of the cold-return notice."""
    return {
        "threshold_tokens": _positive_setting(
            COLD_RETURN_TOKENS_ENV, COLD_RETURN_TOKENS, env
        ),
        "threshold_minutes": _positive_setting(
            COLD_RETURN_IDLE_MINUTES_ENV, COLD_RETURN_IDLE_MINUTES, env
        ),
    }


def _instant(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str) and value.strip():
        try:
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    # The engine database writes UTC without an offset.
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def cold_return_due(
    context_tokens: Any,
    last_activity_at: Any,
    now: Any,
    settings: Mapping[str, int] | None = None,
) -> bool:
    """True when the chat is large and has been idle past the threshold.

    Large is at or above the token setting; idle is more than the minute
    setting since the chat's last model activity (``last_activity_at`` here
    is that time, not the conversation row's stamp). A size the harness
    never reported, or a time that cannot be read, is not a reason to warn.
    """
    limits = settings or cold_return_settings()
    size = _count(context_tokens)
    last, current = _instant(last_activity_at), _instant(now)
    if size is None or last is None or current is None:
        return False
    if size < limits["threshold_tokens"]:
        return False
    return (current - last).total_seconds() > limits["threshold_minutes"] * 60


def with_reground(body: str) -> str:
    """``body`` as dispatched after a compaction: the line goes at the end."""
    return f"{body.rstrip()}\n\n{REGROUND_LINE}"


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def meter_reading(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The chat's context meter from its newest usage and compaction events.

    ``rows`` are newest first, each with ``sequence``, ``event_type``,
    ``payload`` (JSON text or a dict) and ``created_at``. The size is the
    latest per-request reading an adapter reported, or the size a newer
    compaction left behind. A newer compaction that stated no size after
    leaves the size unknown until the next request: the larger figure from
    before it is no longer true. The window is the latest one a harness
    stated. Whatever was not reported stays None: nothing here is estimated.
    """
    reading: dict[str, Any] = {
        "context_tokens": None,
        "context_window": None,
        "measured_at": None,
        "sequence": None,
    }
    size_settled = False
    for row in rows:
        if row["event_type"] not in METER_EVENT_TYPES:
            continue
        payload = row["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                continue
        if not isinstance(payload, dict):
            continue
        if reading["context_window"] is None and row["event_type"] == "usage":
            window = _count(payload.get("context_window"))
            if window:
                reading["context_window"] = window
        if not size_settled:
            if row["event_type"] == "usage":
                size = (
                    _count(payload.get("context_tokens"))
                    if payload.get("scope") == "request"
                    else None
                )
            else:
                size = _count(payload.get("tokens_after"))
                size_settled = True
            if size is not None:
                size_settled = True
                reading["context_tokens"] = size
                reading["measured_at"] = row["created_at"]
                reading["sequence"] = int(row["sequence"])
        if size_settled and reading["context_window"] is not None:
            break
    return reading


def read_meter(con, conversation_id: str) -> dict[str, Any]:
    """``meter_reading`` over one conversation's newest matching events."""
    marks = ",".join("?" for _ in METER_EVENT_TYPES)
    rows = con.execute(
        "SELECT sequence,event_type,payload,created_at "
        "FROM conversation_events WHERE conversation_id=? "
        f"AND event_type IN ({marks}) ORDER BY sequence DESC LIMIT ?",
        (conversation_id, *METER_EVENT_TYPES, METER_EVENT_LIMIT),
    ).fetchall()
    return meter_reading(rows)


# ── Engine-requested compaction (spec #275) ─────────────────────────────────


@dataclass(frozen=True)
class EnqueueResult:
    """What the shared enqueue helper did.

    ``refusal`` is a code from ``REFUSAL_MESSAGES`` and ``detail`` its
    wording when nothing was queued. ``existing`` is true when the
    idempotency key named a request already made, whose id and state are
    returned unchanged.
    """

    message_id: int | None = None
    state: str | None = None
    refusal: str | None = None
    detail: str | None = None
    existing: bool = False
    pending_message_id: int | None = None

    @property
    def queued(self) -> bool:
        return self.refusal is None and not self.existing


def manual_trigger_supported(harness: str | None) -> bool:
    """True when ``harness``'s adapter has a confirmed manual trigger."""
    from conversation_adapters import ADAPTER_TYPES

    return bool(
        getattr(ADAPTER_TYPES.get(harness or ""), "manual_compaction", False)
    )


def compact_window_native(harness: str | None) -> bool:
    """True when ``harness``'s adapter takes the trigger as a token count."""
    from conversation_adapters import ADAPTER_TYPES

    return bool(
        getattr(ADAPTER_TYPES.get(harness or ""), "compact_window_native", False)
    )


def request_hash(cause: str) -> str:
    """The stored request hash of an engine compaction with ``cause``.

    The button's hash is unchanged from spec #272 (``{"compact": true}``),
    so a press retried across this change still matches its first row.
    """
    body: dict[str, Any] = {"compact": True}
    if cause != CAUSE_OPERATOR:
        body["cause"] = cause
    encoded = json.dumps(
        body, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


# One chat's state as the helper and the idle rule read it. Columns, in
# order: harness, state, harness_session_ref, close_requested,
# pending_compaction_message_id, active_run_id, outbox_pending.
_CHAT_SQL = (
    "SELECT c.harness,c.state,c.harness_session_ref,"
    "EXISTS(SELECT 1 FROM conversation_events requested "
    " WHERE requested.conversation_id=c.conversation_id "
    " AND requested.event_type='conversation.close.requested' "
    " AND requested.sequence>COALESCE("
    " (SELECT MAX(reopened.sequence) FROM conversation_events reopened "
    "  WHERE reopened.conversation_id=c.conversation_id "
    "  AND reopened.event_type='conversation.reopened'),0)) "
    "AS close_requested,"
    "(SELECT MAX(m.message_id) FROM conversation_messages m "
    " WHERE m.conversation_id=c.conversation_id AND "
    + manual_compact_sql("m")
    + " AND m.state IN ('accepted','queued','running')) "
    "AS pending_compaction_message_id,"
    "(SELECT active.run_id FROM conversation_runs active "
    " WHERE active.conversation_id=c.conversation_id "
    " AND active.state IN ('leased','starting','running') "
    " ORDER BY active.run_id DESC LIMIT 1) AS active_run_id,"
    "EXISTS(SELECT 1 FROM conversation_outbox o "
    " WHERE o.conversation_id=c.conversation_id "
    " AND o.state IN ('pending','claimed')) AS outbox_pending "
    "FROM conversations c WHERE c.conversation_id=?"
)


def _refused(code: str, harness: str | None = None, **extra: Any) -> EnqueueResult:
    return EnqueueResult(
        refusal=code,
        detail=REFUSAL_MESSAGES[code].format(harness=harness or "this harness"),
        **extra,
    )


def enqueue_compaction(
    con,
    conversation_id: str,
    cause: str,
    *,
    idempotency_key: str,
    user_id: int | None = None,
    hash_value: str | None = None,
    extra_payload: Mapping[str, Any] | None = None,
) -> EnqueueResult:
    """Queue one engine compaction request. Runs in the caller's transaction.

    The one path for every engine compaction: the button (``operator``),
    the between-turn check (``threshold``) and the idle scheduler
    (``idle``). It applies the button's refusals (no manual trigger, closed,
    closing, no harness session, a compaction already pending, a turn
    running or work queued), then inserts the engine-sent control message
    with the existing sender and body, queues it, moves the chat to
    ``queued``, and appends ``compaction.requested`` carrying ``cause``. A
    refusal is returned, never raised, and writes nothing.
    ``idempotency_key`` is the message's key: a second call with the same
    key returns the first request instead of queuing another.
    """
    if cause not in ENGINE_CAUSES:
        raise ValueError(f"not an engine compaction cause: {cause!r}")
    digest = hash_value or request_hash(cause)
    existing = con.execute(
        "SELECT message_id,request_hash,state FROM conversation_messages "
        "WHERE conversation_id=? AND idempotency_key=?",
        (conversation_id, idempotency_key),
    ).fetchone()
    if existing is not None:
        if existing[1] != digest:
            return _refused("MESSAGE_IDEMPOTENCY_CONFLICT")
        return EnqueueResult(
            message_id=int(existing[0]), state=existing[2], existing=True
        )
    chat = con.execute(_CHAT_SQL, (conversation_id,)).fetchone()
    if chat is None:
        return _refused("CONVERSATION_NOT_FOUND")
    harness, state, session_ref = chat[0], chat[1], chat[2]
    close_requested, pending = bool(chat[3]), chat[4]
    active_run, outbox_pending = chat[5], bool(chat[6])
    if not manual_trigger_supported(harness):
        return _refused("COMPACTION_UNSUPPORTED", harness)
    if state == "closed":
        return _refused("CONVERSATION_CLOSED")
    if close_requested:
        return _refused("CONVERSATION_CLOSING")
    if not session_ref:
        return _refused("COMPACTION_NO_SESSION")
    if pending is not None:
        return _refused("COMPACTION_PENDING", pending_message_id=int(pending))
    if (
        state in ("queued", "running", "waiting")
        or active_run is not None
        or outbox_pending
    ):
        return _refused("CONVERSATION_BUSY")
    message_id = int(
        con.execute(
            "INSERT INTO conversation_messages "
            "(conversation_id,sender_kind,sender_ref,message_kind,body,"
            "idempotency_key,request_hash,state) "
            "VALUES (?,'engine',?,'control',?,?,?,'queued')",
            (
                conversation_id,
                MANUAL_COMPACT_SENDER,
                MANUAL_COMPACT_BODY,
                idempotency_key,
                digest,
            ),
        ).lastrowid
    )
    con.execute(
        "INSERT INTO conversation_outbox (conversation_id,message_id) "
        "VALUES (?,?)",
        (conversation_id, message_id),
    )
    con.execute(
        "UPDATE conversations SET state='queued',version=version+1 "
        "WHERE conversation_id=?",
        (conversation_id,),
    )
    payload: dict[str, Any] = {
        "message_id": message_id,
        "initiated_by": "engine",
        "cause": cause,
        "harness": harness,
    }
    if cause == CAUSE_OPERATOR and user_id is not None:
        payload["requested_by_user_id"] = int(user_id)
    for key, value in (extra_payload or {}).items():
        payload.setdefault(key, value)
    # The broker's canonical event writer, as settle_cancelled_requests uses.
    from conversation_broker import BrokerStore

    BrokerStore._append_event(
        con,
        conversation_id=conversation_id,
        event_type=COMPACTION_REQUESTED,
        payload=payload,
        message_id=message_id,
        run_id=None,
    )
    return EnqueueResult(message_id=message_id, state="queued")


def request_compaction(
    con,
    conversation_id: str,
    cause: str,
    *,
    idempotency_key: str,
    user_id: int | None = None,
    extra_payload: Mapping[str, Any] | None = None,
) -> EnqueueResult:
    """``enqueue_compaction`` in a write transaction of its own.

    The caller wakes the broker (``conversation_broker.notify_commit``) and
    the chat's listeners once this returns a queued request.
    """
    import db_driver

    with db_driver.write_transaction(con, "conversation.compaction.enqueue"):
        return enqueue_compaction(
            con,
            conversation_id,
            cause,
            idempotency_key=idempotency_key,
            user_id=user_id,
            extra_payload=extra_payload,
        )


def _payload_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def request_record(con, conversation_id: str, message_id: int) -> dict[str, Any]:
    """What an outcome copies from the request carried by ``message_id``:
    its ``cause`` and, for a threshold request, the ``trigger_tokens`` it was
    made at. A request recorded before causes existed was the button's."""
    row = con.execute(
        "SELECT payload FROM conversation_events WHERE conversation_id=? "
        "AND message_id=? AND event_type=? ORDER BY sequence DESC LIMIT 1",
        (conversation_id, int(message_id), COMPACTION_REQUESTED),
    ).fetchone()
    payload = _payload_dict(row[0]) if row is not None else {}
    cause = payload.get("cause")
    record: dict[str, Any] = {
        "cause": cause if cause in ENGINE_CAUSES else CAUSE_OPERATOR
    }
    trigger = _count(payload.get("trigger_tokens"))
    if record["cause"] == CAUSE_THRESHOLD and trigger:
        record["trigger_tokens"] = trigger
    return record


# ── Threshold compaction: checked by the engine between turns ──────────────


def threshold_compaction(
    con,
    conversation_id: str,
    *,
    run_id: int,
    trigger_message_id: int,
    env: Mapping[str, str] | None = None,
) -> EnqueueResult | None:
    """Queue one ``threshold`` compaction for a turn that ended large.

    Runs in the caller's transaction, the one that records the turn's end,
    so the compaction runs next while the cache is warm and a message sent
    meanwhile waits behind it. None when nothing applies: auto compaction
    off (or no settings for this harness), a turn that already produced a
    ``context.compacted``, or a meter reading under the trigger. At most one
    per turn: the key is the turn's run.
    """
    import compaction_settings

    row = con.execute(
        "SELECT harness FROM conversations WHERE conversation_id=?",
        (conversation_id,),
    ).fetchone()
    if row is None:
        return None
    settings = compaction_settings.resolve(con, row[0], env)
    trigger = settings.effective_trigger if settings is not None else None
    if trigger is None:
        return None
    compacted = con.execute(
        "SELECT 1 FROM conversation_events WHERE conversation_id=? "
        "AND message_id=? AND event_type='context.compacted' LIMIT 1",
        (conversation_id, int(trigger_message_id)),
    ).fetchone()
    if compacted is not None:
        return None
    size = read_meter(con, conversation_id)["context_tokens"]
    if size is None or size < trigger:
        return None
    return enqueue_compaction(
        con,
        conversation_id,
        CAUSE_THRESHOLD,
        idempotency_key=f"threshold:{int(run_id)}",
        extra_payload={"trigger_tokens": trigger, "context_tokens": size},
    )


# ── Idle compaction: the reaper-hosted scheduler ────────────────────────────

# Last model activity for the idle rule: the newest usage or compaction that
# is not an engine-requested compaction's own record. A request's own usage
# is stored under the ``compaction`` scope; its marker names an engine cause
# (or, recorded before causes existed, carries the request's message id).
_IDLE_ACTIVITY_SQL = (
    "SELECT event_id,sequence,created_at FROM conversation_events "
    "WHERE conversation_id=? AND ("
    " (event_type='usage' "
    "  AND COALESCE(json_extract(payload,'$.scope'),'')<>'"
    + COMPACTION_USAGE_SCOPE
    + "') OR (event_type='context.compacted' "
    "  AND COALESCE(json_extract(payload,'$.cause'),'') "
    "      NOT IN ('operator','threshold','idle') "
    "  AND NOT (json_extract(payload,'$.cause') IS NULL "
    "      AND json_extract(payload,'$.request_message_id') IS NOT NULL))"
    ") ORDER BY sequence DESC LIMIT 1"
)


@dataclass(frozen=True)
class IdleVerdict:
    """The idle rule for one chat at one instant.

    ``failed_rule`` is the first of spec #275's rules 1-7 that does not
    hold, or None when every rule holds and the chat is due.
    """

    conversation_id: str
    failed_rule: int | None
    reason: str
    activity_event_id: int | None = None
    context_tokens: int | None = None

    @property
    def due(self) -> bool:
        return self.failed_rule is None


def idle_last_activity(con, conversation_id: str) -> tuple[int, int, str] | None:
    """(event_id, sequence, created_at) of the chat's last model activity
    for the idle rule, or None when its model never answered."""
    row = con.execute(_IDLE_ACTIVITY_SQL, (conversation_id,)).fetchone()
    if row is None:
        return None
    return int(row[0]), int(row[1]), str(row[2])


def idle_verdict(
    con,
    conversation_id: str,
    now: Any,
    env: Mapping[str, str] | None = None,
) -> IdleVerdict:
    """Spec #275's idle rules 1-7 for one chat, read from the database.

    Nothing is held in memory between heartbeats, so the scheduler resumes
    exactly where it was after an engine restart.
    """
    import compaction_settings

    def verdict(rule: int | None, reason: str, **fields: Any) -> IdleVerdict:
        return IdleVerdict(conversation_id, rule, reason, **fields)

    chat = con.execute(_CHAT_SQL, (conversation_id,)).fetchone()
    if chat is None:
        return verdict(2, "no such chat")
    harness, state = chat[0], chat[1]
    settings = compaction_settings.resolve(con, harness, env)
    # 1. The harness's idle wait is not Off, and it has a manual trigger.
    if settings is None or settings.idle_after_minutes is None:
        return verdict(1, "idle compaction is off for this harness")
    if not manual_trigger_supported(harness):
        return verdict(1, f"{harness} has no manual compaction trigger")
    # 2. Open: not closed, closing or displaced (a displaced chat is closed).
    if state == "closed" or bool(chat[3]):
        return verdict(2, "the chat is closed or closing")
    # 3. No turn running and nothing queued.
    if (
        state in ("queued", "running", "waiting")
        or chat[4] is not None
        or chat[5] is not None
        or bool(chat[6])
    ):
        return verdict(3, "a turn is running or work is queued")
    # 4. Large enough: the meter reading is at or above the idle size.
    size = read_meter(con, conversation_id)["context_tokens"]
    if size is None or size < settings.idle_min_tokens:
        return verdict(
            4, "the context is under the idle size", context_tokens=size
        )
    activity = idle_last_activity(con, conversation_id)
    current = _instant(now)
    last = _instant(activity[2]) if activity is not None else None
    if activity is None or last is None or current is None:
        return verdict(5, "the chat has no model activity to time")
    event_id, sequence, _created = activity
    idle_seconds = (current - last).total_seconds()
    timed = {"activity_event_id": event_id, "context_tokens": size}
    # 5. Quiet for at least the wait.
    if idle_seconds < settings.idle_after_minutes * 60:
        return verdict(5, "the idle wait has not passed", **timed)
    # 6. Still inside the cache life: past it the cache has likely gone and
    # compacting would cost a full uncached read.
    if idle_seconds >= settings.cache_life_minutes * 60:
        return verdict(6, "past the provider's cache life", **timed)
    # 7. Once per idle period, whatever the earlier request's outcome.
    asked = con.execute(
        "SELECT 1 FROM conversation_events WHERE conversation_id=? "
        "AND event_type=? AND sequence>? "
        "AND json_extract(payload,'$.cause')=? LIMIT 1",
        (conversation_id, COMPACTION_REQUESTED, sequence, CAUSE_IDLE),
    ).fetchone()
    if asked is not None:
        return verdict(7, "already compacted while idle this period", **timed)
    return verdict(None, "due", **timed)


def idle_candidates(con) -> list[str]:
    """Open chats on a harness whose idle compaction is on."""
    import compaction_settings

    harnesses = [
        harness
        for harness in compaction_settings.harnesses_with_idle(con)
        if manual_trigger_supported(harness)
    ]
    if not harnesses:
        return []
    marks = ",".join("?" for _ in harnesses)
    return [
        str(row[0])
        for row in con.execute(
            "SELECT conversation_id FROM conversations "
            f"WHERE state<>'closed' AND harness IN ({marks}) "
            "ORDER BY conversation_id",
            harnesses,
        ).fetchall()
    ]


def run_idle_compaction(
    connect: Callable[[], Any],
    now: Any,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """One idle heartbeat: queue an ``idle`` compaction for each due chat.

    Every chat is judged from the database: read first, then judged again
    inside the write that queues it. The key is the last-activity event, so
    a repeated heartbeat cannot queue twice for one idle period. A failure
    on one chat is logged and the others still run. Returns the chats
    queued; the caller wakes the broker and their listeners.
    """
    import db_driver

    queued: list[str] = []
    con = connect()
    try:
        for conversation_id in idle_candidates(con):
            try:
                if not idle_verdict(con, conversation_id, now, env).due:
                    continue
                with db_driver.write_transaction(
                    con, "conversation.compaction.idle"
                ):
                    verdict = idle_verdict(con, conversation_id, now, env)
                    if not verdict.due:
                        continue
                    result = enqueue_compaction(
                        con,
                        conversation_id,
                        CAUSE_IDLE,
                        idempotency_key=f"idle:{verdict.activity_event_id}",
                        extra_payload={"context_tokens": verdict.context_tokens},
                    )
                if result.queued:
                    queued.append(conversation_id)
            except Exception as exc:  # noqa: BLE001 - one chat never stops the rest
                LOG.warning(
                    "idle compaction for %s failed: %s", conversation_id, exc
                )
    finally:
        con.close()
    return queued
