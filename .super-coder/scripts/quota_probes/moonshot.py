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

from . import (TIME_UNIT_SECONDS, account, as_count, get_json, is_expired,
               kind_for_seconds, norm_iso, now_iso, percent_from, read_json,
               status_for_http, window)

HARNESS_PROVIDER = "moonshot"
PROBE_VERSION = "1"

CREDENTIALS = Path.home() / ".kimi-code/credentials/kimi-code.json"
URL = "https://api.kimi.com/coding/v1/usages"


def _window_seconds(spec) -> "float | None":
    """`{"duration": 300, "timeUnit": "MINUTE"}` → 18000. An unknown time unit
    yields None and the window keeps its raw duration in scope."""
    if not isinstance(spec, dict):
        return None
    unit = TIME_UNIT_SECONDS.get(str(spec.get("timeUnit") or "").upper())
    try:
        duration = float(spec.get("duration"))
    except (TypeError, ValueError):
        return None
    return duration * unit if unit else None


def _counts_window(entry: dict, captured_at: str, kind, scope) -> dict:
    used, limit = as_count(entry.get("used")), as_count(entry.get("limit"))
    return window(window_kind=kind, scope=scope, used=used, limit=limit,
                  used_percent=percent_from(used, limit),
                  resets_at=norm_iso(entry.get("resetTime")),
                  captured_at=captured_at, probe_version=PROBE_VERSION)


def _windows(payload: dict, captured_at: str, log) -> list[dict]:
    rows = []
    usage = payload.get("usage")
    if isinstance(usage, dict):
        # Pinned by spec #49's Normalization table: the top-level usage block
        # carries no window spec, and it is the account-wide weekly figure.
        rows.append(_counts_window(usage, captured_at, "weekly", None))
    limits = payload.get("limits")
    for entry in limits if isinstance(limits, list) else []:
        if not isinstance(entry, dict):
            continue
        seconds = _window_seconds(entry.get("window"))
        kind = kind_for_seconds(seconds)
        scope = entry.get("name") or entry.get("scope")
        if kind is None:
            raw = entry.get("window")
            log(f"{HARNESS_PROVIDER}: unrecognized window {raw!r} — kept under "
                "its own row")
            duration = f"{int(seconds)}s" if seconds else str(raw)
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
        return result(status="na", is_current=0)
    if is_expired((creds or {}).get("expires_at")):
        # Reported, not repaired — this package never refreshes a token.
        return result(status="unauth", detail="access token expired")

    code, payload = get_json(URL, {
        "Authorization": f"Bearer {token}", "Accept": "application/json",
    }, timeout)
    if code != 200 or not isinstance(payload, dict):
        detail = f"HTTP {code}" if code else f"unreachable: {payload}"
        return result(status=status_for_http(code), detail=detail)

    user_id = ((payload.get("user") or {}) if isinstance(payload.get("user"), dict)
               else {}).get("userId")
    ref = str(user_id) if user_id else None
    if not ref:
        log(f"{HARNESS_PROVIDER}: no user.userId in the usages payload — this "
            "account cannot be identified")
        return result(status="error", detail="no account identifier")

    membership = payload.get("membership")
    plan = membership.get("level") if isinstance(membership, dict) else None
    return result(account_ref=ref, account_label=ref, plan=plan,
                  windows=_windows(payload, captured_at, log))
