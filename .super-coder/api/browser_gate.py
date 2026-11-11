#!/usr/bin/env python3
"""The review server's sign-in gate (Feature #94, spec #271).

Default deny. Every request the review server receives is decided here before
any route runs: `server.dispatch_http` calls `authorize` first, and the
conversation event stream, which bypasses `dispatch_http`, calls `session`
when it opens and again at every turn of its loop.

A request is let through for exactly one of four reasons:

* `open`     its method and path are in `OPEN_ROUTES`;
* `shell_api` its path is under `/_sc/`, whose routes keep their own Bearer
               rules, unchanged;
* `session`  it carries this install's session cookie, verified against the
               list the host auth authority publishes (`auth_sessions`), and
               it passes the origin rule below;
* `cli`      it carries a Bearer credential, and its method and path are in
               `CLI_ROUTES`.

Everything else is refused. "No `Authorization` header" proves nothing.

Origin rule for a session (`site_ok`). Cookies and `SameSite` both ignore
ports, so every page on this machine's loopback is "same site" and the browser
sends it the cookie. The request's own headers are what tell the GUI from
another local page:

* a mutation (anything but GET and HEAD) must carry an `Origin` equal to the
  origin it was sent to (`http://<Host>`). No `Origin` means not the GUI;
* a `GET` under `/api/` must not be cross-origin: an `Origin` that is present
  must be this one, and `Sec-Fetch-Site`, when the browser sends it, must be
  `same-origin` or `none`. Same-origin `GET`s carry no `Origin`, so one is
  not demanded;
* a `GET` for a page or a static file needs only the cookie: following a link
  to the GUI is a cross-site navigation, and the files are the checkout's
  public source.

A cookie is counted only on a loopback `Host`, and never beside an
`Authorization` header: a request that names a Bearer credential is judged as
that credential.

Stdlib only. Nothing here writes.
"""
from __future__ import annotations

import hmac
import json
from typing import Callable, NamedTuple
from urllib.parse import parse_qs, quote, urlparse

import auth_sessions

SIGN_IN_PAGE = "/signin.html"
SIGN_IN_REQUIRED = "sign_in_required"
SETUP_CLI = "./sc auth user add <username>"
# The confusion every shell-facing browser refusal prevents, named first
# (Feature #91 E5/E6); the route modules reuse it.
BROWSER_OWNERSHIP_CONFUSION = (
    "the browser profile and its chats belong to the operator; a shell drives, "
    "it does not own"
)

# THE open list: what answers without a session. Exact method and path; no
# prefix, no pattern. The sign-in page and the files it loads are listed by
# name, so nothing else under `ui/` or `ui/vendor/` is served to a visitor.
OPEN_ROUTES = frozenset({
    ("GET", "/api/health"),
    ("GET", "/api/auth/status"),
    ("GET", SIGN_IN_PAGE),
    ("GET", "/signin.js"),
    ("GET", "/signin.css"),
    ("GET", "/theme.js"),
    ("GET", "/style.css"),
    ("GET", "/vendor/qrcode.js"),
    # The one vendored script the sign-in page loads may be probed with HEAD,
    # as the app shell probes its own (`server.do_HEAD`).
    ("HEAD", "/vendor/qrcode.js"),
})

# `/api/*` requests an `sc` verb makes over HTTP. They accept a Bearer
# credential the calling shell or host already holds: a live shell's API key,
# or this install's engine CLI token (`scripts/cli_credential.py`). Neither
# makes the caller the operator: off this list a Bearer credential is refused.
#
# Exact, to the query string: method, path, and the raw query the verb sends.
# `GET /api/models` without `refresh=1`, with another value, or with anything
# added or repeated, is the GUI's request and not the verb's.
CLI_ROUTES = frozenset({
    ("POST", "/api/scripts/snapshot", ""),      # ./sc snapshot, ./sc update
    ("GET", "/api/models", "refresh=1"),        # ./sc models refresh
})

LOOPBACK_HOSTS = frozenset(("127.0.0.1", "localhost", "::1"))
# The hostnames a page can be loaded from and be given an authority URL for.
AUTHORITY_HOSTS = ("127.0.0.1", "localhost")
MAX_NEXT = 2048

# This boot's engine CLI token. Set once by the server at startup; compared in
# memory, so nothing written to the published file is ever believed.
CLI_TOKEN: str | None = None


class Verdict(NamedTuple):
    kind: str                       # open | shell_api | session | cli | deny
    session: dict | None = None     # {"user_id", "hash"} for kind == "session"
    response: tuple | None = None   # (status, headers, body) for kind == "deny"

    @property
    def allowed(self) -> bool:
        return self.kind != "deny"


# ── request facts ────────────────────────────────────────────────────────────

def request_path(target: str) -> str:
    """The path a request target routes on: the same parse every route uses."""
    return urlparse(target).path


def _hostname(host_header) -> str:
    host = (host_header or "").strip()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    if ":" in host:
        return host.rsplit(":", 1)[0]
    return host


def host_ok(headers) -> bool:
    return _hostname(headers.get("Host")) in LOOPBACK_HOSTS


def _same_origin(origin: str, host: str) -> bool:
    parsed = urlparse(origin)
    return (
        parsed.scheme in ("http", "https")
        and bool(host)
        and parsed.netloc == host
        and not (parsed.path or parsed.params or parsed.query or parsed.fragment)
    )


def is_cli_route(method: str, target: str) -> bool:
    """Whether this is exactly one of the requests an `sc` verb makes."""
    parsed = urlparse(target)
    if parsed.params or parsed.fragment:
        return False
    return (method, parsed.path, parsed.query) in CLI_ROUTES


def is_api(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def is_shell_api(path: str) -> bool:
    return path == "/_sc" or path.startswith("/_sc/")


def is_document(path: str) -> bool:
    """Whether a GET for this path is a page, not an API call or a subresource.

    Decided from the path alone, so the answer does not depend on headers a
    client chooses: a page is `/`, a `.html` file, or a last segment with no
    extension. `/app.js` and `/vendor/x.css` are subresources.
    """
    if is_api(path) or is_shell_api(path) or not path.startswith("/"):
        return False
    leaf = path.rsplit("/", 1)[1]
    return "." not in leaf or leaf.endswith(".html")


def bearer(headers) -> str:
    """The Bearer token of the `Authorization` header, or ''."""
    value = headers.get("Authorization") or ""
    return value[7:].strip() if value[:7].lower() == "bearer " else ""


def _cookie_header(headers) -> str:
    values = headers.get_all("Cookie") if hasattr(headers, "get_all") else None
    if values:
        return "; ".join(values)
    return headers.get("Cookie") or ""


# ── the session check ────────────────────────────────────────────────────────

def session(headers) -> dict | None:
    """The browser session this request proves, or None.

    THE test for "signed in" on the review server. None when the request names
    an `Authorization` header (it is then a shell or CLI request, never a
    browser), when `Host` is not a loopback name, and whenever the published
    list does not recognise the cookie: revoked, never issued, a published
    hash presented as if it were a token, another install's cookie, or no
    readable list at all.
    """
    if headers.get("Authorization") is not None:
        return None
    if not host_ok(headers):
        return None
    found = auth_sessions.session_from_cookie_header(_cookie_header(headers))
    # A user id is an integer and nothing that merely compares equal to one.
    if found is None or type(found.get("user_id")) is not int:
        return None
    return found


def operator_session(headers, user_active: Callable[[int], bool]) -> dict | None:
    """THE decision "this request is the signed-in operator", or None.

    A verified cookie (`session`) whose user is still an active engine user.
    Every authorisation point uses this one function: the gate, the status
    route, the route-level checks, and each turn of an open event stream.
    There is no second, weaker copy.
    """
    found = session(headers)
    if found is None or not user_active(found["user_id"]):
        return None
    return found


def cross_origin(headers) -> bool:
    """Whether the request says it came from another origin.

    An `Origin` that is present and is not this one, or a `Sec-Fetch-Site`
    that is present and is neither `same-origin` nor `none`. A same-origin
    `GET` sends no `Origin`, so the absence of both is not cross-origin.
    """
    origin = headers.get("Origin")
    if origin is not None and not _same_origin(origin, headers.get("Host") or ""):
        return True
    return headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none")


def site_ok(method: str, path: str, headers) -> bool:
    """The origin rule for a request that carries a session (module docstring)."""
    if method in ("GET", "HEAD"):
        return not is_api(path) or not cross_origin(headers)
    return headers.get("Origin") is not None and not cross_origin(headers)


def renewal_headers(headers) -> dict:
    """`Set-Cookie` that re-issues the presented session cookie, or {}.

    The engine never expires a session, but a browser drops a cookie after
    about 400 days. Re-issuing the same token with a fresh `Max-Age` keeps a
    browser that is in use signed in. Nothing is written: the token is the one
    the request carried, and the name is the published one.

    Only for the GUI's own request: a cross-origin one is never answered with
    a `Set-Cookie`, whatever cookie the browser attached to it. The caller
    has already established that the session is the operator's.
    """
    if session(headers) is None or cross_origin(headers):
        return {}
    name = auth_sessions.cookie_name()
    for token in auth_sessions.presented_tokens(_cookie_header(headers), name):
        if auth_sessions.verify_token(token) is not None:
            return {"Set-Cookie": auth_sessions.set_cookie(name, token)}
    return {}


# ── responses ────────────────────────────────────────────────────────────────

def _json(status: int, payload: dict, extra=None) -> tuple:
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = [("Content-Type", "application/json"),
               ("Content-Length", str(len(body))),
               ("Cache-Control", "no-store")]
    headers.extend(extra or [])
    return status, headers, body


def error_payload(code: str, message: str, details: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def sign_in_required_payload() -> dict:
    """The one body every "not signed in" answer carries, on every route."""
    return error_payload(
        SIGN_IN_REQUIRED,
        "sign in to the review GUI",
        {"sign_in": SIGN_IN_PAGE, "auth_status": "/api/auth/status"})


def sign_in_required() -> tuple:
    return _json(401, sign_in_required_payload())


# What a refused credential is told it reached, by path prefix: the names the
# routes themselves use. Wording only; it decides nothing.
_OPERATOR_SURFACES = (
    ("/api/browser", "browser configuration"),
    ("/api/web-search", "web search config"),
    ("/api/sprints", "the Sprint board"),
    ("/api/runs", "runs"),
    ("/api/engine-ops", "engine operations"),
    ("/api/harnesses", "harness conversion verification"),
)


def operator_required_payload(path: str = "") -> dict:
    """The refusal a credential gets on a route that belongs to the operator.

    On the browser profile and chat surfaces it names, first, what a shell
    there would get wrong (Feature #91 E6), in the words those routes use.
    """
    if path.startswith(("/api/conversations", "/api/review-")):
        message = (f"{BROWSER_OWNERSHIP_CONFUSION}: conversations are owned "
                   "by the browser operator")
    else:
        what = next((name for prefix, name in _OPERATOR_SURFACES
                     if path == prefix or path.startswith(prefix + "/")),
                    "the review GUI's API")
        message = f"{what} is owned by the browser FnB operator"
        if path == "/api/browser":
            message = f"{BROWSER_OWNERSHIP_CONFUSION}: {message}"
    return error_payload("fnb_operator_required", message)


def same_origin_required() -> tuple:
    return _json(403, error_payload(
        "same_origin_required",
        "a signed-in request must come from the review GUI's own origin"))


def safe_next(value) -> str:
    """A same-origin relative path to return to after sign-in, or `/`.

    Accepts only a path on this origin: one leading `/`, not `//` (a
    scheme-relative URL), no backslash (browsers read `\\` as `/`), no control
    character (browsers strip tab and newline, turning `/\\t/host` into
    `//host`), no scheme or authority, a bounded length, and not the sign-in
    page itself. Anything else is `/`.
    """
    if not isinstance(value, str) or not value or len(value) > MAX_NEXT:
        return "/"
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value):
        return "/"
    try:
        parsed = urlparse(value)
    except ValueError:
        return "/"
    if parsed.scheme or parsed.netloc:
        return "/"
    if parsed.path == SIGN_IN_PAGE:
        return "/"
    return value


def sign_in_redirect(target: str) -> tuple:
    """302 to the sign-in page, carrying where the visitor was going."""
    parsed = urlparse(target)
    wanted = safe_next(parsed.path + (f"?{parsed.query}" if parsed.query else ""))
    location = SIGN_IN_PAGE
    if wanted != "/":
        location += "?next=" + quote(wanted, safe="")
    return 302, [("Location", location), ("Content-Length", "0"),
                 ("Cache-Control", "no-store")], b""


# ── the decision ─────────────────────────────────────────────────────────────

def _cli_token_matches(token: str) -> bool:
    expected = CLI_TOKEN
    return bool(expected) and hmac.compare_digest(
        token.encode("utf-8", "surrogatepass"), expected.encode("utf-8"))


def authorize(method: str, target: str, headers,
              shell_exists: Callable[[str], bool],
              user_active: Callable[[int], bool]) -> Verdict:
    """Decide one request; a refusal of a `HEAD` carries no body."""
    verdict = _authorize(method, target, headers, shell_exists, user_active)
    if method == "HEAD" and verdict.response is not None:
        # HEAD is the same answer minus the body, Content-Length included
        # (RFC 9110 9.3.2).
        status, out_headers, _body = verdict.response
        return verdict._replace(response=(status, out_headers, b""))
    return verdict


def _authorize(method: str, target: str, headers,
               shell_exists: Callable[[str], bool],
               user_active: Callable[[int], bool]) -> Verdict:
    """Decide one request.

    `shell_exists(token)` says whether a Bearer token is a live shell's API
    key; it is consulted only for a request that sent one. `user_active(id)`
    says whether a session's user is still an active engine user; it is
    consulted only for a request whose cookie verified.
    """
    path = request_path(target)
    if is_shell_api(path):
        return Verdict("shell_api")
    if (method, path) in OPEN_ROUTES:
        return Verdict("open")
    if headers.get("Authorization") is not None:
        token = bearer(headers)
        known = bool(token) and (_cli_token_matches(token) or shell_exists(token))
        if not known:
            return Verdict("deny", response=sign_in_required())
        if is_cli_route(method, target):
            return Verdict("cli")
        # A credential this engine issued, on a route that belongs to the
        # operator: refused, and told why. It does not become a session.
        return Verdict("deny", response=_json(403, operator_required_payload(path)))
    found = operator_session(headers, user_active)
    if found is None:
        if method == "GET" and is_document(path):
            return Verdict("deny", response=sign_in_redirect(target))
        return Verdict("deny", response=sign_in_required())
    if not site_ok(method, path, headers):
        return Verdict("deny", response=same_origin_required())
    return Verdict("session", session=found)


# ── GET /api/auth/status ─────────────────────────────────────────────────────

def auth_status(target: str, headers, discovery: dict,
                user_active: Callable[[int], bool]) -> dict:
    """What the sign-in page and the app shell need before anything else.

    `discovery` is `engine_ops.discovery(...)`: the host auth authority is the
    engine-ops service. Its URL is built for the hostname the page was loaded
    from, because the session cookie is bound to that exact host.
    `signed_in` is the gate's own answer for a `GET` under `/api/`: the
    operator's session (`operator_session`) on a request that is not
    cross-origin. The route is open, so a cross-origin caller is answered,
    but as a stranger: `signed_in` false and no `user_id`, whatever cookie
    its browser attached.
    """
    found = None if cross_origin(headers) else operator_session(headers, user_active)
    hostname = _hostname(headers.get("Host"))
    if hostname not in AUTHORITY_HOSTS:
        hostname = AUTHORITY_HOSTS[0]
    port = discovery.get("port")
    payload = {
        "enrolled": auth_sessions.enrolled(),
        "signed_in": found is not None,
        "authority": {
            "url": f"http://{hostname}:{port}" if port is not None else None,
            "up": bool(discovery.get("up")),
            "up_source": discovery.get("up_source"),
            "start_cli": discovery.get("start_cli"),
        },
        "sign_in": SIGN_IN_PAGE,
        "setup_cli": SETUP_CLI,
        "next": safe_next((parse_qs(urlparse(target).query).get("next") or ["/"])[0]),
    }
    if found is not None:
        payload["user_id"] = found["user_id"]
    return payload
