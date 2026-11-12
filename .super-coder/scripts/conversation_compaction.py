"""Browser-chat compaction: the window setting, the meter, the re-ground line.

Feature #95. The engine summarises nothing. It asks each harness to compact
earlier, records that a compaction happened, reads how full a chat is from
what the harness reports, and tells the shell to re-read its durable state
on the first message after a compaction.

Everything here is a pure function over values the caller already holds, so
the broker (dispatch) and the API (projection) read one definition.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Mapping
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
