#!/usr/bin/env python3
"""Engine-ops service — the host-side authority that signs the operator in to
the review GUI and runs engine update, restart, and harness update for it.

On the docker runtime the review API runs inside the sandbox and cannot drive
the host's docker, so it cannot run these verbs; on the host runtime it would
be stopping itself. This service runs ON THE HOST under both runtimes and owns
them. It is a sibling of the vm, ts, pm2, and db brokers (host process, fixed
verbs, refuses to run in the sandbox, started and stopped with `./sc launch`
and `./sc down`), with one deliberate difference in its boundary:

It does NOT listen on a unix socket in the bind-mounted engine directory.
Every sandboxed shell can reach that directory, and these verbs replace the
engine. It listens on its own host loopback TCP port (127.0.0.1 only, one per
fork, `./sc engine-ops-port`). The browser calls it directly. Specs:
.super-coder/docs/engine-ops.md and .super-coder/docs/auth.md.

It is also the host auth authority (Feature #94, spec #271): the only writer
of the sign-in credentials and sessions (`host_auth.py`), which live in its
host-private state. Engine operations require a signed-in session, checked
here against that state, so reaching the port is not enough.

Routes (all JSON):

    GET     /health                 liveness of this service
    GET     /auth/status            is a user enrolled, is this request signed in
    POST    /auth/identify          which form a username gets
    POST    /auth/sign-in           password + authenticator code -> session
    POST    /auth/first/begin       first sign-in: username + claim code
    POST    /auth/first/confirm     first sign-in: password + one code -> session
    POST    /auth/sign-out          end this session
    GET     /auth/sessions          this user's devices           (session)
    POST    /auth/sessions/revoke   sign one device out           (session)
    GET     /status                 runtime, per-op facts, current or last
                                    operation, bounded log tail   (session)
    POST    /ops/<op>               start update | restart |
                                    update-harnesses              (session)
    OPTIONS *                       CORS preflight, for the GUI origin only

Every request must name a loopback Host on this port and must carry no
Authorization header: a shell credential is refused, and the browser operator
has none. A POST must carry an Origin equal to this install's GUI origin; a
GET may omit Origin, but a foreign one is refused. CORS headers are sent for
the GUI origin and for nothing else, with credentials allowed for the pinned
origin only.

A session is the cookie `host_auth.cookie_name()`. It is honoured only on a
request whose Origin is the pinned GUI origin: cookies ignore ports and
`SameSite` does too, so any page on this host's loopback is "same site", and
the Origin is what tells the GUI from another local page.

The port and the accepted GUI origin come from the service's host-private
state. Nothing in the checkout, which a sandbox can write, is consulted, and
the operation status and logs it serves are in that private directory too.

The GUI origin is pinned by the operator in a host terminal (`./sc auth user
add`, or `./sc auth origin`). Until it is, the service starts nothing and
signs nobody in: `GET /status` answers `{pinned: false, reason, pin_cli,
setup_cli}` and every `POST` is `409 gui_origin_unpinned`, readable by any
loopback page so the GUI can say why.

The verb table is fixed (`engine_ops.VERBS`). The path segment selects a row;
for `/ops/*` the request body and query string are never read. An operation
runs detached from this service (`engine_ops.start`), because `./sc restart`
and `./sc down` stop it along with the brokers.

Run on the HOST (never in the sandbox):
    ./sc engine-ops         foreground
    ./sc engine-ops-up      background (pidfile) ; ./sc engine-ops-down to stop
"""
from __future__ import annotations

import json
import re
import signal
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth_sessions  # noqa: E402  (the session cookie, spelled once)
import engine_ops  # noqa: E402  (verb table, status file, detached runner)
import host_auth  # noqa: E402  (credential store, sign-in checks)

# The only address this service binds. Not configurable: a wider bind would
# put engine replacement on the network.
BIND = engine_ops.BIND
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")
OPS_PREFIX = "/ops/"
AUTH_PREFIX = "/auth/"
# The largest request body read (auth routes only; `/ops/*` reads none).
MAX_BODY_BYTES = 8 * 1024
# While unpinned, a refusal may be read by any loopback page (it carries no
# state and starts nothing), so the GUI can show the reason.
_LOOPBACK_ORIGIN = re.compile(r"^http://(127\.0\.0\.1|localhost):[0-9]{1,5}$")


def _error(code: str, message: str, details: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}}}


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0: one request per connection. Only the auth routes read a body,
    # and an unread one must not be parsed as the next request.
    protocol_version = "HTTP/1.0"
    server_version = "sc-engine-ops"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[engine-ops] " + (fmt % args) + "\n")

    # ── responses ────────────────────────────────────────────────────────────

    def _allowed_origin(self, origin: str | None) -> bool:
        """May this origin be ANSWERED: the pinned GUI origin; or, while
        unpinned, any loopback page.

        This admits a request and addresses its CORS headers. It authorises
        nothing: whether an operation starts, and whether status is returned,
        is decided inside `engine_ops.start` and `engine_ops.status`, against
        the pin as it is at that moment.
        """
        if not origin:
            return False
        pinned = engine_ops.pinned_gui_port()
        if pinned is None:
            return bool(_LOOPBACK_ORIGIN.match(origin))
        return engine_ops.origin_allowed(origin, pinned)

    def _foreign(self) -> None:
        self._send(403, _error(
            "same_origin_required",
            "engine operations require the review GUI's origin"))

    def _pinned_origin(self) -> bool:
        """Whether this request's Origin is the pinned GUI origin, now."""
        pinned = engine_ops.pinned_gui_port()
        return pinned is not None and engine_ops.origin_allowed(
            self.headers.get("Origin"), pinned)

    def _cors(self) -> list[tuple[str, str]]:
        """CORS headers, only when the request's Origin is an allowed one.

        Credentials (the session cookie) are allowed for the pinned GUI origin
        and never for the any-loopback-page answers given while unpinned.
        """
        origin = self.headers.get("Origin")
        if not self._allowed_origin(origin):
            return []
        headers = [
            ("Access-Control-Allow-Origin", origin),
            ("Vary", "Origin"),
        ]
        if self._pinned_origin():
            headers.append(("Access-Control-Allow-Credentials", "true"))
        return headers

    # ── session ──────────────────────────────────────────────────────────────

    def _session_token(self) -> str | None:
        """The session token this request proves, or None.

        A cookie counts only on a request from the pinned GUI origin, and only
        if the store, read now, holds its hash for an active user.
        """
        if not self._pinned_origin():
            return None
        for token in auth_sessions.presented_tokens(
                self.headers.get("Cookie"), host_auth.cookie_name()):
            if host_auth.session_for(token) is not None:
                return token
        return None

    def _signed_in(self) -> bool:
        return self._session_token() is not None

    def _sign_in_required(self) -> None:
        self._send(401, _error(
            "sign_in_required",
            "sign in to the review GUI to use the engine controls",
            {"auth_status": "/auth/status"}))

    def _set_cookie(self, token: str) -> list[tuple[str, str]]:
        return [("Set-Cookie", auth_sessions.set_cookie(host_auth.cookie_name(), token))]

    def _clear_cookie(self) -> list[tuple[str, str]]:
        return [("Set-Cookie", auth_sessions.clear_cookie(host_auth.cookie_name()))]

    def _read_body(self) -> bytes | None:
        """The request body, bounded; None once a refusal has been sent.

        Read before anything else is decided about an auth request, so a
        refusal is not sent over a body the client is still writing.
        """
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._send(400, _error("bad_request", "a small JSON body is required"))
            return None
        return self.rfile.read(length)

    def _json_body(self, raw: bytes) -> dict | None:
        """The request's JSON object, or None once a refusal has been sent.

        `Content-Type: application/json` is required: a cross-origin page
        cannot send it without a preflight, which only the GUI origin passes.
        """
        kind = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if kind != "application/json":
            self._send(415, _error(
                "json_required", "send a JSON object as application/json"))
            return None
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            body = None
        if not isinstance(body, dict):
            self._send(400, _error("bad_request", "a JSON object is required"))
            return None
        return body

    def _unpinned(self) -> None:
        self._send(409, _error(
            "gui_origin_unpinned", engine_ops.unpinned_status()["reason"],
            {"pin_cli": engine_ops.PIN_CLI, "setup_cli": engine_ops.SETUP_CLI}))

    def _auth_error(self, exc: host_auth.AuthError) -> None:
        extra = None
        if exc.code == "rate_limited":
            extra = [("Retry-After", str(exc.details.get("retry_after", 1)))]
        self._send(exc.status, _error(exc.code, exc.message, exc.details), extra)

    def _send(self, code: int, payload: dict | None,
              extra: list[tuple[str, str]] | None = None) -> None:
        body = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(code)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in self._cors() + (extra or []):
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    # ── gates ────────────────────────────────────────────────────────────────

    def _gate(self, *, mutation: bool) -> bool:
        """Loopback Host, no credential, and the GUI origin. False = answered."""
        port = self.server.server_address[1]
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in {f"{name}:{port}" for name in LOOPBACK_HOSTS}:
            self._send(403, _error(
                "loopback_host_required",
                "the engine-ops service answers only its own loopback address"))
            return False
        if self.headers.get("Authorization") is not None:
            self._send(403, _error(
                "fnb_operator_required",
                "engine operations are owned by the browser FnB operator; "
                "a shell credential cannot start or read them"))
            return False
        origin = self.headers.get("Origin")
        if origin is None:
            if mutation:
                self._send(403, _error(
                    "same_origin_required",
                    "engine operations require the review GUI's origin"))
                return False
        elif not self._allowed_origin(origin):
            self._send(403, _error(
                "same_origin_required",
                "engine operations require the review GUI's origin"))
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self._send(403, _error(
                "same_origin_required",
                "engine operations require the review GUI's origin"))
            return False
        return True

    # ── routes ───────────────────────────────────────────────────────────────

    def do_OPTIONS(self) -> None:
        if not self._gate(mutation=True):
            return
        extra = [
            ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
            ("Access-Control-Allow-Headers", "Content-Type"),
            ("Access-Control-Max-Age", "600"),
        ]
        if self.headers.get("Access-Control-Request-Private-Network") == "true":
            extra.append(("Access-Control-Allow-Private-Network", "true"))
        self._send(204, None, extra)

    def do_GET(self) -> None:
        if not self._gate(mutation=False):
            return
        path = urlparse(self.path).path
        try:
            if path == "/health":
                return self._send(200, {"ok": True, "service": "engine-ops"})
            if path == "/auth/status":
                return self._auth_status()
            if path == "/auth/sessions":
                token = self._session_token()
                sessions = host_auth.list_sessions(token) if token else None
                if sessions is None:
                    return self._sign_in_required()
                return self._send(200, {"sessions": sessions})
            if path == "/status":
                try:
                    payload = engine_ops.status(
                        self.headers.get("Origin"), self._signed_in)
                except engine_ops.ForeignOrigin:
                    return self._foreign()
                except engine_ops.SignInRequired:
                    return self._sign_in_required()
                return self._send(200, payload)
            return self._send(404, _error("not_found", "not found"))
        except host_auth.AuthError as exc:
            return self._auth_error(exc)
        except Exception as exc:  # noqa: BLE001 — one request must not end the service
            return self._send(500, _error("internal_error", str(exc)))

    def _auth_status(self) -> None:
        """Safe for anyone the gate admits: whether a user exists, and whether
        THIS request is signed in. A signed-in request gets its cookie again
        with a fresh lifetime, which is what keeps a session from lapsing."""
        gui_port = engine_ops.pinned_gui_port()
        token = self._session_token()
        state = host_auth.status(token)
        payload = {
            "ok": True,
            "service": "engine-ops",
            "pinned": gui_port is not None,
            "gui_origin": (f"http://{BIND}:{gui_port}"
                           if gui_port is not None else None),
            **state,
        }
        if gui_port is None:
            payload["reason"] = engine_ops.unpinned_status()["reason"]
        extra = self._set_cookie(token) if token and state["signed_in"] else None
        self._send(200, payload, extra)

    def do_POST(self) -> None:
        if not self._gate(mutation=True):
            return
        path = urlparse(self.path).path
        try:
            if path.startswith(AUTH_PREFIX):
                return self._auth_post(path[len(AUTH_PREFIX):])
        except host_auth.AuthError as exc:
            return self._auth_error(exc)
        except Exception as exc:  # noqa: BLE001 — one request must not end the service
            return self._send(500, _error("internal_error", str(exc)))
        if not path.startswith(OPS_PREFIX):
            return self._send(404, _error("not_found", "not found"))
        op = path[len(OPS_PREFIX):]
        try:
            operation = engine_ops.start(
                op, self.headers.get("Origin"), self._signed_in)
        except engine_ops.ForeignOrigin:
            return self._foreign()
        except engine_ops.SignInRequired:
            return self._sign_in_required()
        except engine_ops.UnknownOp:
            return self._send(404, _error(
                "unknown_engine_op", "no such engine operation",
                {"ops": sorted(engine_ops.VERBS)}))
        except engine_ops.Unpinned:
            return self._unpinned()
        except engine_ops.Busy as exc:
            return self._send(409, {
                "operation": exc.operation,
                **_error("engine_op_running", str(exc),
                         {"op": exc.operation.get("op")}),
            })
        except engine_ops.StartFailed as exc:
            return self._send(500, _error("engine_op_start_failed", str(exc)))
        except Exception as exc:  # noqa: BLE001 — one request must not end the service
            return self._send(500, _error("internal_error", str(exc)))
        return self._send(202, {"started": True, "operation": operation})

    AUTH_POSTS = frozenset({
        "identify", "sign-in", "first/begin", "first/confirm", "sign-out",
        "sessions/revoke"})

    def _auth_post(self, route: str) -> None:
        raw = self._read_body()
        if raw is None:
            return None
        if route not in self.AUTH_POSTS:
            return self._send(404, _error("not_found", "not found"))
        # Sign-in exists for one origin. With no pin there is none, and
        # nothing here may be reached through the any-loopback-page rule.
        if engine_ops.pinned_gui_port() is None:
            return self._unpinned()
        if not self._pinned_origin():
            return self._foreign()
        body = self._json_body(raw)
        if body is None:
            return None
        agent = self.headers.get("User-Agent")
        if route == "identify":
            return self._send(200, host_auth.identify(body.get("username")))
        if route == "sign-in":
            token, payload = host_auth.sign_in(
                body.get("username"), body.get("password"), body.get("code"),
                body.get("device"), agent)
            return self._send(200, payload, self._set_cookie(token))
        if route == "first/begin":
            return self._send(200, host_auth.first_begin(
                body.get("username"), body.get("claim_code")))
        if route == "first/confirm":
            token, payload = host_auth.first_confirm(
                body.get("enrolment"), body.get("password"), body.get("code"),
                body.get("device"), agent)
            return self._send(200, payload, self._set_cookie(token))
        token = self._session_token()
        if route == "sign-out":
            # Idempotent: whatever the cookie was, the browser ends without it.
            if token:
                host_auth.sign_out(token)
            return self._send(200, {"signed_in": False}, self._clear_cookie())
        # sessions/revoke
        result = host_auth.revoke(token, body.get("id")) if token else None
        if result is None:
            return self._sign_in_required()
        return self._send(
            200, result, self._clear_cookie() if result["current"] else None)

    def _method_not_allowed(self) -> None:
        self._send(405, _error("method_not_allowed", "method not allowed"))

    do_PUT = do_PATCH = do_DELETE = do_HEAD = _method_not_allowed


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # The stdlib default is 5 pending connections. A page that opens makes
    # several calls at once, and a burst beyond the queue is reset or stalls
    # for a retransmit instead of being answered (with a 429, if that is
    # what it deserves).
    request_queue_size = 64


def make_server(port: int) -> Server:
    """The service's listener, bound to loopback and nothing else."""
    return Server((BIND, port), Handler)


def serve(port: int) -> int:
    """Listen until stopped. `port` 0 asks the kernel for one (tests)."""
    srv = make_server(port)
    actual = srv.server_address[1]

    def _stop(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    engine_ops.register_service(actual)
    try:
        # The published session-hash list, back in step with the store.
        host_auth.republish()
    except Exception as exc:  # noqa: BLE001 — reported; sign-in will say more
        sys.stderr.write(f"[engine-ops] session-hash list not published: {exc}\n")
    sys.stderr.write(
        f"[engine-ops] listening on http://{BIND}:{actual}\n"
        f"[engine-ops] {engine_ops.pin_summary(engine_ops.pinned_gui_port()).strip()}\n")
    sys.stderr.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        engine_ops.unregister_service()
        srv.server_close()
    return 0


def main(argv: list[str]) -> int:
    if engine_ops.in_sandbox():
        sys.exit("engine-ops must run on the HOST (it runs ./sc update, restart, "
                 "and update-harnesses there), not inside the sandbox. "
                 "Run `./sc engine-ops` on the host.")
    if argv:
        sys.exit("usage: engine_ops_service.py   (no arguments; the port is "
                 "this install's `./sc engine-ops-port`)")
    try:
        config = engine_ops.ensure_config()
    except engine_ops.StateError as exc:
        sys.exit(f"engine-ops: {exc}")
    return serve(config["port"])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
