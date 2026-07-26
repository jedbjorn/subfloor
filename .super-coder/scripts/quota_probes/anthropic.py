"""Anthropic probe — GET api.anthropic.com/api/oauth/usage.

Two files, and only the first is the credential file:

  ~/.claude/.credentials.json   claudeAiOauth.accessToken + expiresAt — the
                                token, read and never written.
  ~/.claude.json                oauthAccount.accountUuid — the internal key.

Spec #49 and decisions #65/#67 both said `account_ref` comes from "the
credential file's account uuid". It is NOT there: that file holds only
accessToken / refreshToken / expiresAt / refreshTokenExpiresAt / scopes /
subscriptionType / rateLimitTier. The uuid lives in ~/.claude.json under
`oauthAccount`.

That file ALSO carries `emailAddress` in full, and this module used to read
it: decision #69 made the card's label the operator's full address. Decision
#75 dropped account identity entirely, so the address is not read here any
more and the uuid is only ever an upsert key. The payload carries no
identifier of any kind — verified against a live capture, which alongside
five_hour / seven_day* / extra_usage / limits / spend /
member_dashboard_available carries half a dozen provider-internal keys this
probe neither reads nor recognizes (`tangelo`, `nimbus_quill`, `cinder_cove`
and friends). NONE OF THEM NAMES AN ACCOUNT — that is the claim worth making,
and it is the one the fixture supports. The previous sentence here ended "and
nothing else", an exhaustive promise about a payload we do not control, and it
was already false against the capture sitting beside it.

NO `plan` IS COLLECTED HERE, and the reason it is worth a note is what finding
it produced. This module used to read `payload.subscriptionType` — a key the
usage response has never carried. It returned None for every account,
silently, forever, because the fixture had been TRANSCRIBED with that field
invented at the payload's top level, so the test agreed with the bug. The real
value was in the credential file all along.

The read is gone rather than corrected: decision #75 displays no plan and the
API returns none, so the column was dropped (migration 0097) and a correct
read would still have been feeding nothing. Recorded because the same
read-at-the-wrong-level defect turned up in all three providers, every one of
them invisible for this identical reason.
"""
from __future__ import annotations

from pathlib import Path

from . import (account, as_percent, drift, get_json, is_expired, norm_iso,
               now_iso, read_json, response_detail, status_for_http, window)

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
    """The account uuid from ~/.claude.json's oauthAccount — the only place it
    exists, since the usage response carries no identifier at all.

    `emailAddress` sits in this same block and is deliberately NOT read. It was
    the card's label under decision #69; decision #75 dropped identity, so the
    address is no longer collected, stored, or rendered, and the uuid is only
    ever an upsert key."""
    profile = read_json(PROFILE, log, HARNESS_PROVIDER)
    oauth = (profile or {}).get("oauthAccount")
    if not isinstance(oauth, dict):
        return None
    uuid = oauth.get("accountUuid")
    return str(uuid) if uuid else None


def _drift(limits: list) -> "str | None":
    """What the normalizer cannot read, or None when the envelope is intact.

    `limits: []` is an account with no window to report and stays `ok` — the
    length is data. An ENTRY that is not an object is not: this list is where
    every window on the card comes from, so an unreadable entry is a window
    disappearing while the probe reports `ok` (L-614-1, the same finding as
    openai's `additional_rate_limits[]` and moonshot's `limits[]`)."""
    for item in limits:
        if not isinstance(item, dict):
            return f"limits[] entry is {type(item).__name__}, not an object"
    return None


def _windows(limits, captured_at: str, log) -> list[dict]:
    rows = []
    for item in limits:
        if not isinstance(item, dict):
            # Retained guard: `_drift` rejects a non-object entry before this
            # runs, so a mutation that disables the check degrades to the
            # historical silent skip instead of raising here.
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
        return result(status="na")

    uuid = _account_uuid(log)
    if not uuid:
        log(f"{HARNESS_PROVIDER}: no oauthAccount.accountUuid in {PROFILE} — "
            "the usage payload carries no account id, so this reading cannot "
            "be keyed to a row")
        return result(status="error", detail="no account identifier")

    common = dict(account_ref=uuid)
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
        return result(status=status_for_http(code), **common,
                      detail=response_detail(code, payload, HARNESS_PROVIDER, log))

    limits = payload.get("limits")
    if not isinstance(limits, list):
        # The envelope, not its length: `limits: []` is an account with no
        # window to report and stays `ok`.
        return result(status="error", **common, detail=drift(
            HARNESS_PROVIDER, "no limits[] in the usage payload", log))

    missing = _drift(limits)
    if missing:
        return result(status="error", **common,
                      detail=drift(HARNESS_PROVIDER, missing, log))

    return result(windows=_windows(limits, captured_at, log), **common)
