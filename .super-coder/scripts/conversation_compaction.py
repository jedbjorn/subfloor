"""Browser-chat compaction: the window setting, the meter, the re-ground line,
the manual request and the cold-return rule.

Feature #95. The engine summarises nothing. It asks each harness to compact
earlier, records that a compaction happened, reads how full a chat is from
what the harness reports, tells the shell to re-read its durable state on
the first message after a compaction, lets the operator ask the harness to
compact now, and says when returning to a large idle chat will be expensive.

Everything here is a pure function over values the caller already holds, so
the broker (dispatch) and the API (projection) read one definition.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

LOG = logging.getLogger(__name__)

# The engine-added line. It is appended to the dispatched text only: the
# stored message, the boot file and the system prompt are never touched, so
# the cached prefix of the chat is unchanged.
REGROUND_LINE = (
    "Your context was compacted since your last turn. Before continuing, "
    "re-read your durable state: `sc mem get state`, and the projection for "
    "any task or work unit you are carrying."
)

# One engine setting per harness, read from the server process environment
# like the reaper's. A token count, or ``off`` for the harness default.
DEFAULT_COMPACT_WINDOW = 120_000
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
        "context_tokens": _positive_setting(
            COLD_RETURN_TOKENS_ENV, COLD_RETURN_TOKENS, env
        ),
        "idle_minutes": _positive_setting(
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
    if size < limits["context_tokens"]:
        return False
    return (current - last).total_seconds() > limits["idle_minutes"] * 60


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
