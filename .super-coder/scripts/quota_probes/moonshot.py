"""Moonshot probe — GET api.kimi.com/coding/v1/usages.

Credential: ~/.kimi-code/credentials/kimi-code.json → access_token, with
expires_at for the pre-flight expiry check. Read and never written.

The only provider that reports ABSOLUTE counts (used / limit / remaining), so
its percent is derived rather than given — and derived only when the limit is
a positive number. Identity is `user.userId`; nothing on disk identifies the
account, so a failed probe leaves the account unidentified rather than
guessing at a ref.
"""
from __future__ import annotations

from pathlib import Path

from . import (TIME_UNIT_SECONDS, account, as_count, drift, duration_label,
               get_json, is_expired, kind_for_seconds, norm_iso, now_iso,
               percent_from, read_json, response_detail, status_for_http,
               window)

HARNESS_PROVIDER = "moonshot"
PROBE_VERSION = "2"

CREDENTIALS = Path.home() / ".kimi-code/credentials/kimi-code.json"
URL = "https://api.kimi.com/coding/v1/usages"


def _window_seconds(spec) -> "float | None":
    """`{"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"}` → 18000. An unknown
    time unit yields None and the window is kept under its own row.

    THE ENUM ARRIVES PREFIXED. Spec #49's field table recorded the unit bare
    (`MINUTE`) and the shipped map was keyed that way, so every live window
    missed the lookup — including this one, where 300 minutes is 18000 seconds,
    i.e. `five_hour`, a kind the vocabulary already carries. A recognizable
    window was filed as unrecognized and rendered as a dict repr.

    The prefix is STRIPPED rather than both spellings enumerated: a second
    vocabulary is a second thing to keep in sync with a provider we do not
    control, and the bare form still resolves for free."""
    if not isinstance(spec, dict):
        return None
    unit_name = str(spec.get("timeUnit") or "").upper().removeprefix("TIME_UNIT_")
    unit = TIME_UNIT_SECONDS.get(unit_name)
    try:
        duration = float(spec.get("duration"))
    except (TypeError, ValueError):
        return None
    return duration * unit if unit else None


def _counts_window(entry: dict, captured_at: str, kind, scope) -> dict:
    """One row of counts, from either shape this payload uses.

    The top-level `usage` block carries used / limit / remaining directly. A
    `limits[]` entry nests them one level down under `detail` AND CARRIES NO
    `used` KEY AT ALL — it gives limit and remaining, so used is derived. The
    shipped reader looked only at entry level, found nothing, and rendered a
    structurally empty row as though the account were idle (flag #198).

    One reader for both, because both shapes are in the same payload: unwrap
    `detail` when it is there, else read the entry itself."""
    detail = entry.get("detail")
    source = detail if isinstance(detail, dict) else entry
    limit = as_count(source.get("limit"))
    used = as_count(source.get("used"))
    if used is None:
        remaining = as_count(source.get("remaining"))
        if limit is not None and remaining is not None:
            used = limit - remaining
    return window(window_kind=kind, scope=scope, used=used, limit_value=limit,
                  used_percent=percent_from(used, limit),
                  resets_at=norm_iso(source.get("resetTime")),
                  captured_at=captured_at, probe_version=PROBE_VERSION)


def _drift(payload: dict) -> "str | None":
    """What the normalizer cannot find, or None when the envelope is intact.

    The two keys are not symmetrical, and collapsing them would be the bug.
    `usage` is a BLOCK — the account-wide weekly figure every payload carries;
    an account with nothing spent reports `used: 0`, it does not drop the key,
    so its absence is the shape having changed. `limits` is a COLLECTION —
    absent or `[]` is a real answer (a plan with no metered sub-window), and
    calling that drift would cry wolf at an idle account. A `limits` that is
    present but not a list has no such reading: that is drift.

    Neither has an ENTRY inside it that is not an object. The list is a place
    this reader iterates, so an entry it cannot read is a metered sub-window
    disappearing from the card while the probe reports `ok` — the same finding
    as the wrong-typed container, one level down (L-614-1).
    """
    if not isinstance(payload.get("usage"), dict):
        return "no usage{} in the usages payload"
    limits = payload.get("limits")
    if limits is None:
        return None
    if not isinstance(limits, list):
        return f"limits is {type(limits).__name__}, not a list"
    for entry in limits:
        if not isinstance(entry, dict):
            return f"limits[] entry is {type(entry).__name__}, not an object"
    return None


def _windows(payload: dict, captured_at: str, log) -> list[dict]:
    rows = []
    usage = payload.get("usage")
    if isinstance(usage, dict):
        # Pinned by spec #49's Normalization table: the top-level usage block
        # carries no window spec, and it is the account-wide weekly figure.
        rows.append(_counts_window(usage, captured_at, "weekly", None))
    for entry in payload.get("limits") or []:
        if not isinstance(entry, dict):
            # Retained guard: `_drift` rejects a non-object entry before this
            # runs, so this is unreachable intact — it is what makes a mutation
            # that disables the drift check degrade to the historical defect
            # (the entry silently skipped) instead of raising here.
            continue
        seconds = _window_seconds(entry.get("window"))
        kind = kind_for_seconds(seconds)
        scope = entry.get("name") or entry.get("scope")
        if kind is None:
            # The LOG may carry the raw window — it is a diagnostic and never
            # reaches the operator. `scope` may not: it renders on the card and
            # is persisted. Hence duration_label, which cannot emit a repr.
            log(f"{HARNESS_PROVIDER}: unrecognized window "
                f"{entry.get('window')!r} — kept under its own row")
            duration = duration_label(seconds)
            scope = f"{scope} {duration}" if scope else duration
        rows.append(_counts_window(entry, captured_at, kind, scope))
    return rows


def probe(log, timeout) -> list[dict]:
    captured_at = now_iso()

    def result(**kw):
        return [account(provider=HARNESS_PROVIDER, probe_version=PROBE_VERSION,
                        captured_at=captured_at, **kw)]

    creds = read_json(CREDENTIALS, log, HARNESS_PROVIDER)
    token = (creds or {}).get("access_token")
    if not token:
        return result(status="na")
    if is_expired((creds or {}).get("expires_at")):
        # Reported, not repaired — this package never refreshes a token.
        return result(status="unauth", detail="access token expired")

    code, payload = get_json(URL, {
        "Authorization": f"Bearer {token}", "Accept": "application/json",
    }, timeout)
    if code != 200 or not isinstance(payload, dict):
        return result(status=status_for_http(code),
                      detail=response_detail(code, payload, HARNESS_PROVIDER, log))

    user_id = ((payload.get("user") or {}) if isinstance(payload.get("user"), dict)
               else {}).get("userId")
    ref = str(user_id) if user_id else None
    if not ref:
        log(f"{HARNESS_PROVIDER}: no user.userId in the usages payload — this "
            "account cannot be identified")
        return result(status="error", detail="no account identifier")

    common = dict(account_ref=ref)
    missing = _drift(payload)
    if missing:
        return result(status="error", **common,
                      detail=drift(HARNESS_PROVIDER, missing, log))

    return result(**common, windows=_windows(payload, captured_at, log))
