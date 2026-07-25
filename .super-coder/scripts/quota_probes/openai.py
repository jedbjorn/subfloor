"""OpenAI probe — GET chatgpt.com/backend-api/codex/usage.

Credential: ~/.codex/auth.json → tokens.access_token, plus tokens.account_id
for the `chatgpt-account-id` header. Read and never written.

That file records no expiry (its keys are id_token / access_token /
refresh_token / account_id / last_refresh), so expiry cannot be pre-checked
here as it is for the other two providers — the endpoint's 401 is the
authoritative signal and maps to `unauth`.

This is the ONLY one of the three whose usage response returns an email, and
that fact is the reason the account-label design was wrong rather than a
reason to keep it: a rule reasoned from the one provider that cooperates left
the other two rendering opaque identifiers. The address is not read here
(decision #75). `account_id` is the ref, and it is an upsert key only.
"""
from __future__ import annotations

import re
from pathlib import Path

import harness_versions

from . import (account, as_percent, drift, duration_label, get_json,
               iso_from_epoch, kind_for_seconds, now_iso, read_json,
               response_detail, status_for_http, window)

HARNESS_PROVIDER = "openai"
PROBE_VERSION = "2"

CREDENTIALS = Path.home() / ".codex/auth.json"
URL = "https://chatgpt.com/backend-api/codex/usage"

# ── Client identity, and why the version is derived rather than pinned ───────
#
# This endpoint answers 403 — an HTML anti-bot page, not a JSON auth error — to
# any request it does not recognize as the codex CLI. Proven by execution twice:
# the shipped Authorization + Accept + chatgpt-account-id request returns 403,
# and the identical request plus these three headers returns 200 (flag #196,
# re-run here against the installed 0.145.0).
#
# THE VERSION COMES FROM THE INSTALLED CLI. This fork installs harnesses at
# --latest on every dos-u, so a baked constant is stale by design — it would
# start claiming a version this host has not run since the next update.
#
# The endpoint accepted a FABRICATED 0.20.0 during diagnosis while 0.145.0 was
# installed, so as of 2026-07-25 it gates on the SHAPE of the client identity
# and not on an exact version. That is an observation and not a contract, and
# nothing here depends on it: we send the version we actually have, and if the
# endpoint tightens, `status_for_http` makes the 403 loud instead of blaming the
# operator's session.
ORIGINATOR = "codex_cli_rs"


def _client_version() -> "str | None":
    """The installed codex CLI's dotted version — `codex-cli 0.145.0` →
    `0.145.0` — or None when there is no codex CLI to ask."""
    match = re.search(r"\d+\.\d+\.\d+", harness_versions.probe("codex") or "")
    return match.group(0) if match else None


def _window_row(entry, captured_at: str, log, scope=None) -> "dict | None":
    """One rate-limit window, from a block carrying its own duration."""
    if not isinstance(entry, dict):
        return None
    seconds = entry.get("limit_window_seconds")
    kind = kind_for_seconds(seconds)
    if kind is None:
        # Unrecognized duration: keep the window under its own row with the
        # duration in scope rather than dropping a real limit on the floor.
        log(f"{HARNESS_PROVIDER}: unrecognized window duration {seconds!r} — "
            "kept under its own row")
        # duration_label, not an f-string: `limit_window_seconds` is a value
        # we do not control, and interpolating it raw is the same class of
        # defect moonshot shipped — a container reaching operator text.
        duration = duration_label(seconds)
        scope = f"{scope} {duration}" if scope else duration
    return window(window_kind=kind, scope=scope,
                  used_percent=as_percent(entry.get("used_percent")),
                  resets_at=iso_from_epoch(entry.get("reset_at")),
                  captured_at=captured_at, probe_version=PROBE_VERSION)


def _drift(rate_limit: dict, payload: dict) -> "str | None":
    """What the normalizer cannot trust, or None when the envelope is intact.

    `additional_rate_limits` is a COLLECTION, and moonshot's asymmetry applies
    unchanged: absent or `[]` is a real answer (a plan with no scoped window),
    so erroring on it would cry wolf at an idle account. A present-but-wrong
    TYPE has no such reading — the type is part of the envelope (spec #49), and
    silently skipping it would vanish every scoped window while reporting `ok`.
    Both containers are checked because the reader coalesces across them.

    An ENTRY that carries no `rate_limit{}` is drift for the same reason, one
    level down. This is the check the envelope rule was missing: the shipped
    probe read `used_percent` / `limit_window_seconds` / `reset_at` at entry
    level, found none of them on the live wire, and emitted a row of NULLs
    under a `weekly` label no duration had confirmed — with `limits` still a
    list, so status stayed `ok` and nothing was logged. An entry whose numbers
    we cannot find is a payload that has moved, not an idle account, and spec
    #49's own rule is that a drifted probe reports `error` rather than a zero
    as if it had been measured.
    """
    for container in (rate_limit, payload):
        extra = container.get("additional_rate_limits")
        if extra is None:
            continue
        if not isinstance(extra, list):
            return f"additional_rate_limits is {type(extra).__name__}, not a list"
        for entry in extra:
            if isinstance(entry, dict) and not isinstance(entry.get("rate_limit"), dict):
                return "additional_rate_limits[] entry carries no rate_limit{}"
    return None


def _windows(rate_limit: dict, payload: dict, captured_at: str, log) -> list[dict]:
    rows = []
    for key in ("primary_window", "secondary_window"):
        row = _window_row(rate_limit.get(key), captured_at, log)
        if row:
            rows.append(row)
    extra = rate_limit.get("additional_rate_limits") \
        or payload.get("additional_rate_limits") or []
    # Unreachable-as-false in intact code: `_drift` rejects every
    # present-but-non-list before this runs, and a falsy wrong type coalesces
    # to []. RETAINED DELIBERATELY — mutation leg
    # `openai-wrong-typed-collection-swallowed` disables that drift check, and
    # this guard is what makes the probe then degrade to the historical defect
    # (scoped windows silently skipped, status `ok`) instead of raising on a
    # non-iterable. Delete it and the leg still reds, and its non-iterable
    # case reds via TypeError.
    if isinstance(extra, list):
        for entry in extra:
            if not isinstance(entry, dict):
                continue
            # An entry is NOT itself a window. It carries `limit_name` plus its
            # OWN rate_limit block holding primary/secondary windows, in the
            # same shape as the top-level one — so it is read with the same
            # reader rather than a second, entry-shaped one.
            nested = entry.get("rate_limit")
            if not isinstance(nested, dict):
                # Same retained-guard shape as the isinstance above: `_drift`
                # has already rejected this, so it is unreachable intact, and
                # it is what lets the mutation leg that disables that check
                # degrade to the historical defect instead of raising.
                continue
            for key in ("primary_window", "secondary_window"):
                row = _window_row(nested.get(key), captured_at, log,
                                  scope=entry.get("limit_name"))
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
        return result(status="na")

    version = _client_version()
    if not version:
        # No fabricated version, and no bare request either: without a client
        # identity this endpoint answers 403, and a 403 we provoked ourselves
        # would be indistinguishable from the endpoint tightening. Say which
        # one it is.
        return result(status="error", detail=(
            "no codex CLI installed to derive the client version from — this "
            "endpoint refuses requests that do not identify as its own client"))

    file_account_id = (tokens or {}).get("account_id")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
               "User-Agent": f"{ORIGINATOR}/{version}",
               "originator": ORIGINATOR, "version": version}
    if file_account_id:
        headers["chatgpt-account-id"] = str(file_account_id)

    code, payload = get_json(URL, headers, timeout)
    if code != 200 or not isinstance(payload, dict):
        # NO account_ref ON A FAILED PROBE — moonshot's rule, applied here for
        # the reason flag #196 found the hard way. The file's account_id is a
        # GUESS at who the endpoint will name, and it is a wrong one: the file
        # says `4747d225-…` while a 200 says `user-0kbAQ…`. Writing the guess
        # is what put a permanent second registry row under one account, which
        # then rendered forever as a signed-out card. A failed probe learned
        # nothing about identity, so it claims none; the last successful
        # probe's rows stand untouched with their own age, which is exactly
        # what the card is supposed to show.
        return result(status=status_for_http(code),
                      detail=response_detail(code, payload, HARNESS_PROVIDER, log))

    ref = str(payload.get("account_id") or file_account_id or "") or None
    if not ref:
        log(f"{HARNESS_PROVIDER}: no account_id in {CREDENTIALS} or the usage "
            "payload — this account cannot be identified")
        return result(status="error", detail="no account identifier")

    common = dict(account_ref=ref)
    rate_limit = payload.get("rate_limit")
    if not isinstance(rate_limit, dict):
        # The envelope, not its contents: `rate_limit: {}` is an account with
        # no window to report and stays `ok`; no `rate_limit` at all is the
        # response having changed shape under us.
        return result(status="error", **common, detail=drift(
            HARNESS_PROVIDER, "no rate_limit{} in the usage payload", log))

    missing = _drift(rate_limit, payload)
    if missing:
        return result(status="error", **common,
                      detail=drift(HARNESS_PROVIDER, missing, log))

    return result(plan=payload.get("plan_type"), **common,
                  windows=_windows(rate_limit, payload, captured_at, log))
