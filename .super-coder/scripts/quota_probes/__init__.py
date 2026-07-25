"""Per-provider quota probes — the plugin seam of account analytics.

Sibling of `scripts/token_parsers/`, deliberately shaped like it: one module
per PROVIDER (not per harness — quota is an account property, five shells on
Claude share one bucket), each a plugin over a private third-party endpoint we
don't control. Version drift is accepted: a drifted probe reports `error` with
its HTTP status and never a zero as if measured (spec doc #49).

Contract — every provider module exposes:

    HARNESS_PROVIDER = "<provider>"
    PROBE_VERSION    = "<pin>"      # bumped when the expected shape changes
    def probe(log, timeout) -> list[dict]

  log      callable(str) — probe-level notices (shape drift, unreadable
           credential file). Loud by design. NEVER receives a token.
  timeout  seconds, per HTTP request.

Each returned dict is one ACCOUNT, carrying its own windows:

    provider, account_ref, account_label, plan, is_current, status,
    detail, probe_version, captured_at, windows[]

`status` is `ok` / `unauth` / `na` / `error` and is the account's own — one
provider's failure never touches another's (see dispatch.probe_all). A
provider with no credential file returns exactly ONE dict with
`account_ref=None, status='na', windows=[]`: the caller can tell
not-configured (`na`) from configured-but-broken (`error`), writes no registry
row for a null ref, and so renders no card — never a card reading 0%.

Invariants every provider module upholds, each checkable from the code:

  * READ-ONLY on credentials. The probe opens the harness's credential file
    and never writes it. Refresh tokens rotate; refreshing out-of-band can
    invalidate the copy the harness holds and break the operator's login.
  * Tokens are never stored, logged, or returned. They exist only in the
    request header — nothing in a returned dict or a log line carries one.
  * Expiry is reported, not repaired: an expired access token yields
    `unauth`, and no request is made with it.
  * No credential file means `na`, not `0%`.
  * Drift is judged at the ENVELOPE, never at the window count. The keys the
    normalizer reads are present and of the expected type -> parse them, and
    an envelope carrying no windows is a legitimate `ok`. The envelope is
    absent or the wrong shape -> `error` with no rows (see `drift`). The two
    states are different and must not collapse: reporting drift as a measured
    zero hides a broken probe behind a healthy card, and reporting an idle
    account as drift teaches the operator to ignore the error state.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROVIDERS = ["anthropic", "openai", "moonshot"]

# Window kind is derived from the OBSERVED DURATION, never from the provider's
# label: labels are marketing text and change, while a 604800-second window is
# a week in any vocabulary. Providers that expose no duration (Anthropic's
# `limits[].kind`, Moonshot's top-level `usage`) are mapped in their own module
# against the table in spec #49's Normalization section.
KIND_BY_SECONDS = {18000: "five_hour", 604800: "weekly"}
SHORT_MAX_SECONDS = 3600

TIME_UNIT_SECONDS = {  # Moonshot's window.timeUnit vocabulary
    "SECOND": 1, "MINUTE": 60, "HOUR": 3600, "DAY": 86400, "WEEK": 604800,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_from_epoch(epoch: "float | None") -> "str | None":
    """Epoch seconds (or milliseconds) → ISO UTC. None stays None — a reset
    time we don't have is never invented."""
    if epoch is None:
        return None
    try:
        secs = float(epoch)
    except (TypeError, ValueError):
        return None
    if secs > 1e11:  # milliseconds, as both claude and kimi store expiry
        secs /= 1000.0
    return datetime.fromtimestamp(secs, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def norm_iso(ts: "str | None") -> "str | None":
    """Provider ISO timestamp → ISO UTC at second precision. Unparseable
    input returns None — never a fake time."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch_now() -> float:
    return datetime.now(timezone.utc).timestamp()


def is_expired(expires_at: "float | None") -> bool:
    """True only when the credential file states an expiry that has passed.
    An absent or unparseable expiry is NOT treated as expired — the request
    is made and the provider's 401 is authoritative."""
    if expires_at is None:
        return False
    try:
        secs = float(expires_at)
    except (TypeError, ValueError):
        return False
    if secs > 1e11:
        secs /= 1000.0
    return secs <= epoch_now()


def kind_for_seconds(seconds: "float | None") -> "str | None":
    """Observed window duration → normalized kind. None = unrecognized; the
    caller stores the duration in `scope` and renders the window under its own
    row rather than dropping it."""
    if seconds is None:
        return None
    try:
        secs = int(float(seconds))
    except (TypeError, ValueError):
        return None
    if secs in KIND_BY_SECONDS:
        return KIND_BY_SECONDS[secs]
    if 0 < secs <= SHORT_MAX_SECONDS:
        return "short"
    return None


def percent_from(used, limit) -> "float | None":
    """Absolute counts → percent. A zero (or absent) limit yields None, never
    a division by zero and never a 0%. A percent is NEVER back-computed into a
    count — that direction has no inverse."""
    try:
        used_n, limit_n = float(used), float(limit)
    except (TypeError, ValueError):
        return None
    if limit_n <= 0:
        return None
    return round(used_n / limit_n * 100, 2)


def as_percent(value) -> "float | None":
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def as_count(value) -> "int | None":
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def read_json(path: Path, log, label: str) -> "dict | None":
    """Read a credential/profile file. Read-only, always: nothing in this
    package opens a credential file for writing. None = absent or unreadable;
    the error text names the path, never the contents."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    except OSError as e:
        log(f"{label}: unreadable {path}: {e.strerror or e}")
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        log(f"{label}: malformed JSON in {path}: line {e.lineno} col {e.colno}")
        return None
    return data if isinstance(data, dict) else None


def get_json(url: str, headers: dict, timeout: float) -> "tuple[int, object]":
    """GET → (http_status, payload). Never raises for a reachable-but-unhappy
    endpoint; a transport failure is status 0 with the reason as the payload.
    `headers` carries the bearer token and is never logged or returned."""
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, None
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, str(getattr(e, "reason", e))
    except ValueError as e:
        # A token carrying a character illegal in an HTTP header — a trailing
        # newline from a hand-copied or truncated credential file is the
        # everyday case. `http.client.putheader` raises ValueError whose
        # message IS the header value, i.e. the token; letting it escape put
        # that token into whatever logged the exception (spec #49 gate line
        # "no token in any log line, API response, or DB row"; flag #195).
        # Caught HERE rather than only scrubbed at the log, so the reason the
        # probe failed is still reported: a bare `error` with no diagnosis
        # trades a leak for a mystery. The value never appears — naming the
        # exception type would re-admit it, since `str(e)` is the token.
        del e
        return 0, "invalid request header (check the credential file)"


def account(*, provider: str, probe_version: str, captured_at: str,
            account_ref=None, account_label=None, plan=None, is_current=1,
            status="ok", detail=None, windows=None) -> dict:
    return {
        "provider": provider, "account_ref": account_ref,
        "account_label": account_label, "plan": plan,
        "is_current": 1 if is_current else 0, "status": status,
        "detail": detail, "probe_version": probe_version,
        "captured_at": captured_at, "windows": list(windows or []),
    }


def window(*, window_kind: "str | None", probe_version: str, captured_at: str,
           scope=None, used_percent=None, used=None, limit_value=None,
           resets_at=None, status="ok") -> dict:
    """One window row. An unrecognized duration keeps its raw duration in
    `scope` (done by the caller) and window_kind 'unknown' — rendered under
    its own row rather than dropped.

    `limit_value`, not `limit`: the column it feeds cannot be called `limit`
    because that is a SQLite reserved word (found by U1's premise check,
    ruled in message #1899). The pair is used/limit_value everywhere."""
    return {
        "window_kind": window_kind or "unknown", "scope": scope,
        "used_percent": used_percent, "used": used, "limit_value": limit_value,
        "resets_at": resets_at, "captured_at": captured_at,
        "status": status, "probe_version": probe_version,
    }


def drift(provider: str, what: str, log) -> str:
    """Log a shape drift loudly and return it as the account's `detail`.

    Drift is the response having lost the STRUCTURE the normalizer reads —
    never the count of windows inside it. A well-formed envelope carrying no
    windows is a real answer (a fresh account, a plan with no metered limit)
    and stays `ok` with zero windows; a missing or wrong-typed envelope is
    `error` with no rows, because a drifted probe must never report a zero as
    if it had been measured (spec #49, Overview + Edge Cases)."""
    log(f"{provider}: shape drift — {what}")
    return what


def response_detail(code: int, payload, provider: str, log) -> str:
    """`detail` for a response the normalizer never gets to read: the HTTP
    status, the transport's reason when the request never landed, or — for a
    200 whose body is not a JSON object at all — shape drift, said plainly
    rather than as a bare 'HTTP 200'."""
    if code == 200:
        return drift(provider, "response was not a JSON object", log)
    return f"HTTP {code}" if code else f"unreachable: {payload}"


def status_for_http(code: int) -> str:
    """HTTP status → account status. 401/403 is the provider telling us the
    token is dead: report it as `unauth` and leave the last known values
    standing. Everything else non-200 is `error` — never a measured zero."""
    if code in (401, 403):
        return "unauth"
    return "error"
