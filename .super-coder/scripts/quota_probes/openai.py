"""OpenAI probe — GET chatgpt.com/backend-api/codex/usage.

Credential: ~/.codex/auth.json → tokens.access_token, plus tokens.account_id
for the `chatgpt-account-id` header. Read and never written.

That file records no expiry (its keys are id_token / access_token /
refresh_token / account_id / last_refresh), so expiry cannot be pre-checked
here as it is for the other two providers — the endpoint's 401 is the
authoritative signal and maps to `unauth`.

The only provider that returns an email, so the only one whose card carries a
real address; `account_id` is the stable ref either way.
"""
from __future__ import annotations

from pathlib import Path

from . import (account, as_percent, get_json, iso_from_epoch, kind_for_seconds,
               now_iso, read_json, status_for_http, window)

HARNESS_PROVIDER = "openai"
PROBE_VERSION = "1"

CREDENTIALS = Path.home() / ".codex/auth.json"
URL = "https://chatgpt.com/backend-api/codex/usage"


def _window_row(entry, captured_at: str, log, scope=None,
                fallback_kind=None) -> "dict | None":
    """One rate-limit window. The kind comes from the observed duration;
    `fallback_kind` covers only the entries spec #49 pins by name because no
    duration is exposed for them."""
    if not isinstance(entry, dict):
        return None
    seconds = entry.get("limit_window_seconds")
    kind = kind_for_seconds(seconds)
    if kind is None:
        kind = fallback_kind
    if kind is None:
        # Unrecognized duration: keep the window under its own row with the
        # duration in scope rather than dropping a real limit on the floor.
        log(f"{HARNESS_PROVIDER}: unrecognized window duration {seconds!r} — "
            "kept under its own row")
        duration = f"{seconds}s" if seconds is not None else "no duration"
        scope = f"{scope} {duration}" if scope else duration
    return window(window_kind=kind, scope=scope,
                  used_percent=as_percent(entry.get("used_percent")),
                  resets_at=iso_from_epoch(entry.get("reset_at")),
                  captured_at=captured_at, probe_version=PROBE_VERSION)


def _windows(payload: dict, captured_at: str, log) -> list[dict]:
    rate_limit = payload.get("rate_limit")
    rate_limit = rate_limit if isinstance(rate_limit, dict) else {}
    rows = []
    for key in ("primary_window", "secondary_window"):
        row = _window_row(rate_limit.get(key), captured_at, log)
        if row:
            rows.append(row)
    extra = rate_limit.get("additional_rate_limits") \
        or payload.get("additional_rate_limits") or []
    if isinstance(extra, list):
        for entry in extra:
            row = _window_row(entry, captured_at, log,
                              scope=(entry or {}).get("limit_name")
                              if isinstance(entry, dict) else None,
                              fallback_kind="weekly")
            if row:
                rows.append(row)
    return rows


def probe(log, timeout) -> list[dict]:
    captured_at = now_iso()

    def result(**kw):
        return [account(provider=HARNESS_PROVIDER, probe_version=PROBE_VERSION,
                        captured_at=captured_at, **kw)]

    creds = read_json(CREDENTIALS, log, HARNESS_PROVIDER)
    tokens = (creds or {}).get("tokens") if creds else None
    token = (tokens or {}).get("access_token")
    if not token:
        # An API-key shell (auth_mode=apikey, OPENAI_API_KEY set) lands here
        # too: it has no subscription window, and no window is `na`, not 0%.
        return result(status="na", is_current=0)

    file_account_id = (tokens or {}).get("account_id")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if file_account_id:
        headers["chatgpt-account-id"] = str(file_account_id)

    code, payload = get_json(URL, headers, timeout)
    ref = str(file_account_id) if file_account_id else None
    if code != 200 or not isinstance(payload, dict):
        detail = f"HTTP {code}" if code else f"unreachable: {payload}"
        return result(status=status_for_http(code), detail=detail,
                      account_ref=ref, account_label=ref)

    ref = str(payload.get("account_id") or file_account_id or "") or None
    if not ref:
        log(f"{HARNESS_PROVIDER}: no account_id in {CREDENTIALS} or the usage "
            "payload — this account cannot be identified")
        return result(status="error", detail="no account identifier")

    return result(account_ref=ref,
                  account_label=payload.get("email") or ref,
                  plan=payload.get("plan_type"),
                  windows=_windows(payload, captured_at, log))
