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

    provider, account_ref, status, detail, probe_version,
    captured_at, windows[]

`account_ref` is an INTERNAL KEY and nothing else: it is what lets a repeated
probe update a row instead of minting a duplicate. It is a provider-issued
identifier, it is never an email, and it is never rendered — the panel shows
one card per PROVIDER and says nothing about who is signed in (decision #75).
No probe in this package collects an operator label of any kind.

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


def duration_label(seconds) -> str:
    """Operator-visible text for a window duration we could not map to a kind.

    A CONTAINER IS NEVER INTERPOLATED INTO OPERATOR-VISIBLE TEXT, and this
    function is where that rule is enforced for every provider. moonshot's
    fallback used to read `str(raw)` with `raw` the window dict, which put
    `{'duration': 300, 'timeUnit': 'TIME_UNIT_MINUTE'}` on the card AND
    persisted it to harness_quota_window.scope (flag #198). openai's read
    `f"{seconds}s"` straight from the payload, which would do the same thing
    the day that key nests.

    A repr is not a label. Anything that will not coerce to a number is
    reported as unrecognized rather than rendered — the row is still kept, so
    a window we cannot name is visible rather than dropped, which is what spec
    #49 asks for. It lives here, not in the module that shipped the defect,
    because it is a class and not a typo."""
    try:
        return f"{int(float(seconds))}s"
    except (TypeError, ValueError):
        return "unrecognized window"


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
            account_ref=None, status="ok", detail=None,
            windows=None) -> dict:
    """One account's reading. There is no `account_label`, no `is_current` and
    no `plan`: the first was the operator's identity and is not collected at
    all any more, the second existed only to tell one account's card from
    another's, and the third described WHICH ACCOUNT — the question a
    provider-level panel stops asking (decision #75, migration 0097).

    `plan` is worth a line because it was the one that looked harmless. It is
    not identity in the way an email is, and each probe could read it
    correctly — but nothing displays it and nothing returns it, so every read
    was feeding a column no one queried. Two of the three were reading it from
    the wrong place, undetected, for exactly that reason."""
    return {
        "provider": provider, "account_ref": account_ref,
        "status": status, "detail": detail, "probe_version": probe_version,
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
    if code == 403:
        # Named rather than left as a bare status, because the whole point of
        # flag #196 is that "HTTP 403" was read as an auth verdict by the layer
        # above and rendered as a claim about the operator. Say what it is.
        return ("HTTP 403 — the endpoint rejected this request as an "
                "unrecognized client, not as a bad credential")
    return f"HTTP {code}" if code else f"unreachable: {payload}"


def status_for_http(code: int) -> str:
    """HTTP status → account status.

    401 is the provider's AUTH VERDICT — the token is dead — and is the only
    code that yields `unauth`.

    403 IS NOT AN AUTH VERDICT, and mapping it to one shipped a defect. These
    endpoints are private CLI endpoints: they answer 403, with an HTML anti-bot
    page rather than a JSON auth error, to any request they do not recognize as
    their own client. Under the old mapping that rendered as "signed out — last
    known" while the operator was signed in and actively using the harness
    (flag #196). A 403 is `error` with a diagnosis, so a probe the endpoint has
    started refusing is visibly broken rather than quietly blamed on the
    operator's session. Everything else non-200 is `error` too — never a
    measured zero."""
    return "unauth" if code == 401 else "error"
