"""Anthropic probe — GET api.anthropic.com/api/oauth/usage.

Two files, and only the first is the credential file:

  ~/.claude/.credentials.json   claudeAiOauth.accessToken + expiresAt — the
                                token, read and never written.
  ~/.claude.json                oauthAccount.accountUuid — the account
                                identifier.

Spec #49 and decisions #65/#67 both say `account_ref` comes from "the
credential file's account uuid". It is NOT there: that file holds only
accessToken / refreshToken / expiresAt / refreshTokenExpiresAt / scopes /
subscriptionType / rateLimitTier. The uuid lives in ~/.claude.json under
`oauthAccount`, which also carries `emailAddress` in full — so the spec's
label ladder ("Anthropic: credential-file account uuid, first 8", written on
the premise that Anthropic returns no email) is reachable but not forced.
Both gaps were reported to the planner before this module was written; the
label below follows the spec AS WRITTEN and is the one line a ruling flips.

The usage payload itself returns no account identifier at all, which is why
the uuid is load-bearing. Whether it survives a re-login is unverified —
confirming it needs an operator re-login (flag against feature 17).
"""
from __future__ import annotations

from pathlib import Path

from . import (account, as_percent, get_json, is_expired, norm_iso, now_iso,
               read_json, status_for_http, window)

HARNESS_PROVIDER = "anthropic"
PROBE_VERSION = "1"

CREDENTIALS = Path.home() / ".claude/.credentials.json"
PROFILE = Path.home() / ".claude.json"
URL = "https://api.anthropic.com/api/oauth/usage"
BETA_HEADER = "oauth-2025-04-20"

# No duration is exposed by this endpoint, so these three labels are the only
# signal available and are pinned against spec #49's Normalization table.
KIND_MAP = {"session": "session", "weekly_all": "weekly",
            "weekly_scoped": "weekly_scoped"}


def _account_uuid(log) -> "str | None":
    profile = read_json(PROFILE, log, HARNESS_PROVIDER)
    oauth = (profile or {}).get("oauthAccount")
    if not isinstance(oauth, dict):
        return None
    uuid = oauth.get("accountUuid")
    return str(uuid) if uuid else None


def _label(uuid: str) -> str:
    """Spec #49's ladder: the account uuid, first 8. ~/.claude.json also holds
    `oauthAccount.emailAddress` in full, so a planner ruling to label Anthropic
    by email changes this function and nothing else."""
    return uuid[:8]


def _windows(limits, captured_at: str, log) -> list[dict]:
    rows = []
    for item in limits:
        if not isinstance(item, dict):
            continue
        raw_kind = item.get("kind")
        kind = KIND_MAP.get(raw_kind)
        scope = None
        if kind == "weekly_scoped":
            model = ((item.get("scope") or {}).get("model") or {})
            scope = model.get("display_name")
        elif kind is None:
            log(f"{HARNESS_PROVIDER}: unrecognized limit kind {raw_kind!r} — "
                "kept under its own row")
            scope = str(raw_kind) if raw_kind else None
        rows.append(window(
            window_kind=kind, scope=scope,
            used_percent=as_percent(item.get("percent")),
            resets_at=norm_iso(item.get("resets_at")),
            captured_at=captured_at, probe_version=PROBE_VERSION))
    return rows


def probe(log, timeout) -> list[dict]:
    captured_at = now_iso()

    def result(**kw):
        return [account(provider=HARNESS_PROVIDER, probe_version=PROBE_VERSION,
                        captured_at=captured_at, **kw)]

    creds = read_json(CREDENTIALS, log, HARNESS_PROVIDER)
    oauth = (creds or {}).get("claudeAiOauth") if creds else None
    token = (oauth or {}).get("accessToken")
    if not token:
        # No credential file (or no token in it): this host has no Claude
        # subscription session. Absence of a limit is not a limit of zero.
        return result(status="na", is_current=0)

    uuid = _account_uuid(log)
    if not uuid:
        log(f"{HARNESS_PROVIDER}: no oauthAccount.accountUuid in {PROFILE} — "
            "the usage payload carries no account id, so this account cannot "
            "be identified")
        return result(status="error", detail="no account identifier")

    common = dict(account_ref=uuid, account_label=_label(uuid))
    if is_expired((oauth or {}).get("expiresAt")):
        # Reported, not repaired: refreshing out-of-band rotates the refresh
        # token and can break the operator's own login.
        return result(status="unauth", detail="access token expired", **common)

    code, payload = get_json(URL, {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": BETA_HEADER,
        "Accept": "application/json",
    }, timeout)
    if code != 200 or not isinstance(payload, dict):
        detail = f"HTTP {code}" if code else f"unreachable: {payload}"
        if code == 200:
            detail = "response was not a JSON object"
            log(f"{HARNESS_PROVIDER}: shape drift — {detail}")
        return result(status=status_for_http(code), detail=detail, **common)

    limits = payload.get("limits")
    if not isinstance(limits, list):
        log(f"{HARNESS_PROVIDER}: shape drift — no limits[] in the usage payload")
        return result(status="error", detail="no limits[] in response", **common)

    return result(plan=payload.get("subscriptionType"),
                  windows=_windows(limits, captured_at, log), **common)
