"""What the Harness cards show: compaction settings and card data.

Feature #95, spec #275 "Harness cards" and "Card data". The cards at the top
of the Shells view's Harness tab read three operator-only routes under the
harnesses prefix (served by ``api/server.py``):

* ``GET compaction``: ``cards(con)``, every compacting harness's settings
  with their sources, what applies to the harness, and this week's
  compaction count for the collapsed summary line.
* ``PUT <harness>/compaction``: ``compaction_settings.save``;
  ``card(con, settings)`` is the answer.
* ``GET <harness>/compaction-data?days=7|30``:
  ``card_data(con, harness, days)``.

Every figure is computed on request from this install's own chat records.
Nothing is stored for it and nothing is estimated: a figure the harness does
not report is ``None`` with ``reported: false``, and the card says "not
reported".

Definitions, so a figure can be checked by hand:

* **A request** is a ``usage`` event with scope ``request``. Its time is the
  payload's ``requested_at`` when present. Kimi's events are read after the
  run ends, so a Kimi request without ``requested_at`` has no known time and
  its gap figures are not reported; every other harness is read live, so its
  event's ``created_at`` is the request time.
* **In the window** means the request time (``created_at`` for an untimed
  Kimi request) is at or after ``now - days``.
* **The split** of a request is ``input`` (uncached input), ``cache_read``,
  ``cache_write`` and ``output``, from the payload's ``split`` when an
  adapter stored one, else from the harness's native token names
  (``NATIVE_SPLIT``). OpenCode stores no cache figures, so it has no split.
* **Warm share** is the sum of ``cache_read`` over the sum of the input side
  (``input + cache_read + cache_write``) of the requests with a split.
* **A gap** is the time from a chat's previous request (by sequence, inside
  the window or not) to this one. A request is **cold** when its cache-read
  share of its own input side is under ``COLD_SHARE``.
* **A cold return** is a request whose gap is at or past the cache life and
  whose context is at least the idle size; the tokens re-read are its
  uncached input side (``input + cache_write``).
* **A compaction** is a ``context.compacted`` event; its cause is the
  payload's ``cause``. A record from before causes existed that names its
  request's message is the operator's; one with neither is the harness's.
* **An idle compaction paid off** when a prompt reaches the chat within
  24 hours after it; it did not when 24 hours passed without one, and it is
  pending while the 24 hours are still running.
* **Compaction cost** sums the split of ``usage`` events with scope
  ``compaction``: the engine's own compaction requests.
"""
from __future__ import annotations

import json
import statistics
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

import compaction_settings
import conversation_compaction

DAYS_CHOICES = (7, 30)
SUMMARY_DAYS = 7
# Under half the input side read from the cache: the request missed it.
COLD_SHARE = 0.5
PAYOFF_HOURS = 24

DISPLAY_NAMES = {
    "claude": "Claude",
    "codex": "Codex",
    "kimi": "Kimi",
    "opencode": "OpenCode",
}

# Native token names per harness, as each adapter stores them, mapped onto
# the split. ``input_includes_cached`` is OpenAI's spelling: ``inputTokens``
# already counts the cached part. A part a harness never reports is absent.
NATIVE_SPLIT: Mapping[str, Mapping[str, Any]] = {
    "claude": {
        "input": ("input_tokens",),
        "cache_read": ("cache_read_input_tokens",),
        "cache_write": ("cache_creation_input_tokens",),
        "output": ("output_tokens",),
    },
    "codex": {
        "input": ("inputTokens", "input_tokens"),
        "cache_read": ("cachedInputTokens", "cached_input_tokens"),
        "output": ("outputTokens", "output_tokens"),
        "input_includes_cached": True,
    },
    "kimi": {
        "input": ("input_tokens", "inputOther"),
        "cache_read": ("cache_read_tokens", "inputCacheRead"),
        "cache_write": ("cache_write_tokens", "inputCacheCreation"),
        "output": ("output_tokens", "output"),
    },
    # OpenCode's adapter keeps the numeric totals only; its cache figures
    # are nested and not stored, so it reports no split.
    "opencode": {},
}
SPLIT_PARTS = ("input", "cache_read", "cache_write", "output")


# ── small readers ───────────────────────────────────────────────────────────


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0:
        return None
    return int(value)


def _instant(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        # Epoch milliseconds, as Kimi's wire writes ``time``.
        moment = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    elif isinstance(value, str) and value.strip():
        try:
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is None:
        # The engine database writes UTC without an offset.
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _db_time(moment: datetime) -> str:
    """``moment`` in the engine database's own spelling, for comparisons."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _median(values: list[int]) -> float | int | None:
    if not values:
        return None
    middle = statistics.median(values)
    return int(middle) if float(middle).is_integer() else middle


def token_split(harness: str, payload: Mapping[str, Any]) -> dict[str, int | None] | None:
    """One usage payload's split, or None when the harness reports none.

    A split needs at least the uncached input and the cache read; a part
    the harness does not report is None, never zero.
    """
    stored = payload.get("split")
    if isinstance(stored, Mapping):
        split = {part: _count(stored.get(part)) for part in SPLIT_PARTS}
        if split["input"] is not None and split["cache_read"] is not None:
            return split
    names = NATIVE_SPLIT.get(harness) or {}
    tokens = payload.get("tokens")
    if not names or not isinstance(tokens, Mapping):
        return None

    def read(part: str) -> int | None:
        for name in names.get(part, ()):
            value = _count(tokens.get(name))
            if value is not None:
                return value
        return None

    split = {part: read(part) for part in SPLIT_PARTS}
    if split["input"] is None or split["cache_read"] is None:
        return None
    if names.get("input_includes_cached"):
        split["input"] = max(0, split["input"] - split["cache_read"])
    return split


def _input_side(split: Mapping[str, int | None]) -> int:
    return sum(split.get(part) or 0 for part in ("input", "cache_read", "cache_write"))


def _uncached(split: Mapping[str, int | None]) -> int:
    return (split.get("input") or 0) + (split.get("cache_write") or 0)


def compaction_cause(payload: Mapping[str, Any]) -> str:
    """Why a ``context.compacted`` happened, as the marker names it."""
    cause = payload.get("cause")
    if cause in conversation_compaction.CAUSES:
        return str(cause)
    if payload.get("request_message_id") is not None:
        return conversation_compaction.CAUSE_OPERATOR
    return conversation_compaction.CAUSE_HARNESS


# ── the settings cards ──────────────────────────────────────────────────────


def _harness_version(con, harness: str) -> str | None:
    """The newest version the model catalogue saw for ``harness``, if any.
    Read from the database only; no binary is run for a page load."""
    try:
        row = con.execute(
            "SELECT harness_version FROM model_routes WHERE harness=? "
            "AND harness_version IS NOT NULL ORDER BY last_seen_at DESC LIMIT 1",
            (harness,),
        ).fetchone()
    except Exception:  # noqa: BLE001 - a version is a nicety, never a failure
        return None
    return str(row[0]) if row and row[0] else None


def applicability(con, harness: str) -> dict[str, Any]:
    """Which parts of compaction apply to ``harness`` on this engine.

    The trigger is passed to a harness that takes a token count; the engine
    compacts after a turn and while idle only through a confirmed headless
    trigger (``manual_compaction`` on the adapter). Kimi's gate (task #924)
    sets that attribute when it passes; until then this says why not.
    """
    name = DISPLAY_NAMES.get(harness, harness)
    native = conversation_compaction.compact_window_native(harness)
    manual = conversation_compaction.manual_trigger_supported(harness)
    unavailable = None
    if not manual:
        version = _harness_version(con, harness)
        unavailable = (
            f"not available: no headless compaction trigger in {name}"
            + (f" {version}" if version else "")
        )
    if native and harness == "claude":
        passed = "as --autocompact, from 100,000 to 1,000,000"
    elif native and harness == "codex":
        passed = "as model_auto_compact_token_limit"
    elif native:
        passed = "as a token count"
    else:
        passed = f"not available: {name} takes no token count"
    life = compaction_settings.CACHE_LIFE_MINUTES[harness]
    parts = [
        {
            "part": "trigger_passed",
            "label": "Trigger passed to the harness",
            "available": native,
            "note": passed,
        },
        {
            "part": "after_turn",
            "label": "Engine compaction after a turn",
            "available": manual,
            "note": (
                "when a turn ends at or above the trigger"
                if manual
                else unavailable
            ),
        },
        {
            "part": "idle",
            "label": "Idle compaction",
            "available": manual,
            "note": (
                f"before the {life}-minute cache life" if manual else unavailable
            ),
        },
    ]
    return {
        "manual_trigger": manual,
        "trigger_native": native,
        "unavailable_reason": unavailable,
        "applies": parts,
    }


def compaction_counts(
    con,
    since: datetime,
    harnesses: Iterable[str] = compaction_settings.HARNESSES,
) -> dict[str, int]:
    """``context.compacted`` events per harness since ``since``."""
    wanted = list(harnesses)
    counts = {harness: 0 for harness in wanted}
    if not wanted:
        return counts
    marks = ",".join("?" for _ in wanted)
    for harness, count in con.execute(
        "SELECT c.harness,COUNT(e.event_id) FROM conversations c "
        "JOIN conversation_events e ON e.conversation_id=c.conversation_id "
        "AND e.event_type='context.compacted' "
        f"WHERE c.harness IN ({marks}) AND e.created_at>=? GROUP BY c.harness",
        (*wanted, _db_time(since)),
    ).fetchall():
        counts[str(harness)] = int(count)
    return counts


def card(
    con,
    settings: compaction_settings.HarnessCompactionSettings,
    *,
    week_compactions: int | None = None,
) -> dict[str, Any]:
    """One card's settings payload: the resolver's shape plus what applies."""
    out = settings.as_dict()
    out["display_name"] = DISPLAY_NAMES[settings.harness]
    out.update(applicability(con, settings.harness))
    if week_compactions is not None:
        out["week_compactions"] = week_compactions
    return out


def cards(
    con,
    *,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The settings route's answer: every compacting harness, in card
    order, with this week's compaction count for the summary line."""
    current = now or datetime.now(timezone.utc)
    counts = compaction_counts(con, current - timedelta(days=SUMMARY_DAYS))
    return {
        "install_wide": True,
        "summary_days": SUMMARY_DAYS,
        "harnesses": [
            card(con, settings, week_compactions=counts.get(settings.harness, 0))
            for settings in compaction_settings.resolve_all(con, env)
        ],
    }


# ── card data ───────────────────────────────────────────────────────────────


def _request_time(
    harness: str,
    payload: Mapping[str, Any],
    created_at: Any,
) -> datetime | None:
    if "requested_at" in payload:
        return _instant(payload.get("requested_at"))
    if harness == "kimi":
        return None
    return _instant(created_at)


def _gap_buckets(
    wait: int | None,
    life: int,
) -> list[dict[str, Any]]:
    """Under 5 minutes, 5 to the idle wait, the wait to the cache life, and
    over the cache life. With idle compaction Off the two middle buckets are
    one: 5 minutes to the cache life."""
    edges: list[tuple[str, int, int | None]] = [("under 5 min", 0, 5)]
    if wait is not None and 5 < wait < life:
        edges.append((f"5 to {wait} min", 5, wait))
        edges.append((f"{wait} to {life} min", wait, life))
    else:
        edges.append((f"5 to {life} min", 5, life))
    edges.append((f"over {life} min", life, None))
    return [
        {
            "label": label,
            "from_minutes": low,
            "to_minutes": high,
            "requests": 0,
            "cold": 0,
            "rate": None,
        }
        for label, low, high in edges
    ]


def _bucket_for(buckets: list[dict[str, Any]], minutes: float) -> dict[str, Any]:
    for bucket in buckets:
        high = bucket["to_minutes"]
        if minutes >= bucket["from_minutes"] and (high is None or minutes < high):
            return bucket
    return buckets[-1]


def _previous_request(con, conversation_id: str, sequence: int):
    return con.execute(
        "SELECT sequence,payload,created_at FROM conversation_events "
        "WHERE conversation_id=? AND event_type='usage' AND sequence<? "
        "AND json_extract(payload,'$.scope')='request' "
        "ORDER BY sequence DESC LIMIT 1",
        (conversation_id, int(sequence)),
    ).fetchone()


def card_data(
    con,
    harness: str,
    days: int,
    *,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The card-data route's answer: the expanded card's
    figures over the last ``days`` days. See the module's definitions."""
    settings = compaction_settings.resolve(con, harness, env)
    if settings is None:
        raise compaction_settings.SettingsError(
            "COMPACTION_HARNESS_UNKNOWN",
            f"no compaction settings for harness: {harness}",
        )
    if days not in DAYS_CHOICES:
        raise compaction_settings.SettingsError(
            "COMPACTION_DAYS_INVALID",
            "days must be " + " or ".join(str(choice) for choice in DAYS_CHOICES),
            "days",
        )
    current = now or datetime.now(timezone.utc)
    since = current - timedelta(days=days)
    since_db = _db_time(since)
    life = settings.cache_life_minutes
    wait = settings.idle_after_minutes
    # The values the engine acts on: each raised to the engine floor.
    trigger = max(settings.trigger_tokens, compaction_settings.ENGINE_FLOOR_TOKENS)
    idle_size = settings.effective_idle_min_tokens

    rows = con.execute(
        "SELECT e.conversation_id,e.sequence,e.event_type,e.payload,e.created_at "
        "FROM conversations c JOIN conversation_events e "
        "ON e.conversation_id=c.conversation_id "
        "AND e.event_type IN ('usage','context.compacted') "
        "WHERE c.harness=? AND e.created_at>=? "
        "ORDER BY e.conversation_id,e.sequence",
        (harness, since_db),
    ).fetchall()

    requests_by_chat: dict[str, list[dict[str, Any]]] = {}
    compactions: list[dict[str, Any]] = []
    cost_events: list[dict[str, Any]] = []
    for conversation_id, sequence, event_type, raw, created_at in rows:
        payload = _payload(raw)
        if event_type == "context.compacted":
            compactions.append(
                {
                    "conversation_id": conversation_id,
                    "cause": compaction_cause(payload),
                    "before": _count(payload.get("tokens_before")),
                    "after": _count(payload.get("tokens_after")),
                    "created_at": _instant(created_at),
                }
            )
            continue
        scope = payload.get("scope")
        if scope == conversation_compaction.COMPACTION_USAGE_SCOPE:
            cost_events.append(payload)
            continue
        if scope != "request":
            continue
        requested = _request_time(harness, payload, created_at)
        requests_by_chat.setdefault(conversation_id, []).append(
            {
                "sequence": int(sequence),
                "time": requested,
                # Window membership: the request's own time when known.
                "at": requested or _instant(created_at),
                "context": _count(payload.get("context_tokens")),
                "split": token_split(harness, payload),
            }
        )

    in_window: list[dict[str, Any]] = []
    chats: set[str] = set()
    buckets = _gap_buckets(wait, life)
    timed = 0
    cold_return_count = 0
    cold_return_tokens = 0
    cold_return_unsplit = 0
    for conversation_id, requests in requests_by_chat.items():
        previous: dict[str, Any] | None = None
        prior = _previous_request(con, conversation_id, requests[0]["sequence"])
        if prior is not None:
            previous = {"time": _request_time(harness, _payload(prior[1]), prior[2])}
        for request in requests:
            before, previous = previous, request
            moment = request["at"]
            if moment is None or moment < since:
                continue
            in_window.append(request)
            chats.add(conversation_id)
            if request["time"] is None:
                continue
            timed += 1
            if before is None or before["time"] is None:
                continue
            minutes = (request["time"] - before["time"]).total_seconds() / 60
            if minutes < 0:
                continue
            split = request["split"]
            if split is not None:
                side = _input_side(split)
                bucket = _bucket_for(buckets, minutes)
                bucket["requests"] += 1
                if side and (split["cache_read"] or 0) / side < COLD_SHARE:
                    bucket["cold"] += 1
            if minutes >= life and (request["context"] or 0) >= idle_size:
                cold_return_count += 1
                if split is not None:
                    cold_return_tokens += _uncached(split)
                else:
                    cold_return_unsplit += 1
    for bucket in buckets:
        if bucket["requests"]:
            bucket["rate"] = bucket["cold"] / bucket["requests"]

    sizes = [r["context"] for r in in_window if r["context"] is not None]
    at_trigger = sum(1 for size in sizes if size >= trigger)
    splits = [r["split"] for r in in_window if r["split"] is not None]
    cache_read = sum(split["cache_read"] or 0 for split in splits)
    input_side = sum(_input_side(split) for split in splits)

    # Gaps need request times; the cold rate also needs the cache split.
    times_reported = timed > 0 or not in_window
    split_reported = bool(splits) or (not in_window and bool(NATIVE_SPLIT.get(harness)))
    cold_reported = times_reported and split_reported
    if not times_reported:
        gap_note = "request times not reported"
    elif not split_reported:
        gap_note = f"{DISPLAY_NAMES[harness]} reports no cache split"
    else:
        gap_note = None

    by_cause: dict[str, dict[str, Any]] = {}
    windowed = [c for c in compactions if c["created_at"] and c["created_at"] >= since]
    for cause in conversation_compaction.CAUSES:
        mine = [c for c in windowed if c["cause"] == cause]
        by_cause[cause] = {
            "count": len(mine),
            "median_before": _median([c["before"] for c in mine if c["before"] is not None]),
            "median_after": _median([c["after"] for c in mine if c["after"] is not None]),
        }

    payoff = {"followed": 0, "not_followed": 0, "pending": 0}
    for compaction in windowed:
        if compaction["cause"] != conversation_compaction.CAUSE_IDLE:
            continue
        at = compaction["created_at"]
        deadline = at + timedelta(hours=PAYOFF_HOURS)
        message = con.execute(
            "SELECT 1 FROM conversation_messages WHERE conversation_id=? "
            "AND message_kind='prompt' AND created_at>? AND created_at<=? LIMIT 1",
            (compaction["conversation_id"], _db_time(at), _db_time(deadline)),
        ).fetchone()
        if message is not None:
            payoff["followed"] += 1
        elif deadline > current:
            payoff["pending"] += 1
        else:
            payoff["not_followed"] += 1

    cost_splits = [token_split(harness, payload) for payload in cost_events]
    reported_costs = [split for split in cost_splits if split is not None]
    cost: dict[str, Any] = {
        "requests": len(cost_events),
        "reported": bool(reported_costs)
        or (not cost_events and bool(NATIVE_SPLIT.get(harness))),
    }
    for part in SPLIT_PARTS:
        values = [split[part] for split in reported_costs if split[part] is not None]
        cost[part] = sum(values) if values else None
    cost["total"] = (
        sum(sum(v or 0 for v in split.values()) for split in reported_costs)
        if reported_costs
        else None
    )

    return {
        "harness": harness,
        "days": days,
        "since": since.isoformat(),
        "now": current.isoformat(),
        "request_time": "requested_at" if harness == "kimi" else "created_at",
        "settings": {
            "trigger_tokens": trigger,
            "auto_enabled": settings.auto_enabled,
            "idle_after_minutes": wait,
            "idle_min_tokens": idle_size,
            "cache_life_minutes": life,
            "cache_life_measured": compaction_settings.CACHE_LIFE_MEASURED[harness],
        },
        "chats": len(chats),
        "requests": len(in_window),
        "context": {
            "readings": len(sizes),
            "median_tokens": _median(sizes),
            "at_or_above_trigger": at_trigger,
            "share_at_or_above_trigger": (at_trigger / len(sizes)) if sizes else None,
        },
        "warm_share": {
            "reported": split_reported,
            "cache_read": cache_read if splits else None,
            "input_side": input_side if splits else None,
            "share": (cache_read / input_side) if splits and input_side else None,
        },
        "cold_by_gap": {
            "reported": cold_reported,
            "note": gap_note,
            "cold_share_under": COLD_SHARE,
            "untimed_requests": len(in_window) - timed,
            "buckets": buckets,
        },
        "cold_returns": {
            "reported": times_reported,
            "count": cold_return_count if times_reported else None,
            "tokens_reread": (
                cold_return_tokens
                if times_reported and split_reported and not cold_return_unsplit
                else None
            ),
        },
        "compactions": {
            "total": len(windowed),
            "by_cause": by_cause,
        },
        "idle_payoff": payoff,
        "compaction_cost": cost,
    }
