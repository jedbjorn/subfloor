#!/usr/bin/env python3
"""Spec #26's release gate, proven over the real transport.

The gate says the change fails if "any foreign origin can mint or use a
browser session", if a malformed anti-forgery token stops failing closed, or
if a cookie alone can mutate. Those are the cases this file proves, and it
proves them with NOTHING between the assertion and the code under test: a
real socket, the real `transport` multiplex, the real `interface_routes`
authority. No fetch interception, no stubbed responses, no direct call into
the handler.

That constraint is the point. The Interface's browser-facing verification ran
through Playwright with every `/api` response intercepted, which meant the
layer being asserted was the layer being stubbed — and two real defects
(a bind fence that did not exist, a rotation window that let a revoked
session complete a mutation) lived underneath it. A test that mints a session
by writing to a dict proves the dict; a test that mints one by sending bytes
at a listening port proves the boundary.

Scope, deliberately bounded: the release-gate negatives plus the cookie
lifecycle they depend on (mint → rotate → revoke → restart). Rendering,
terminal streaming, and the rest of the UI stay with the existing suites —
this is not a general end-to-end harness.
"""
from __future__ import annotations

import asyncio
import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "api"))

import interface_routes as routes  # noqa: E402
import run as run_mod  # noqa: E402
import transport as transport_mod  # noqa: E402

from test_interface_api import FakeRuntime, build_engine_db  # noqa: E402


class ReleaseGateTest(unittest.TestCase):
    """Every request here crosses a real TCP connection."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db_path = root / "shell_db.db"
        build_engine_db(self.db_path)
        run_dir = root / "run" / "interface"
        self.patches = [
            mock.patch.object(routes, "DB_PATH", self.db_path),
            mock.patch.object(routes, "RUN_DIR", run_dir),
            mock.patch.object(routes, "OPERATOR_TOKEN_PATH",
                              run_dir / "operator.token"),
            mock.patch.object(run_mod, "ensure_worktree"),
            mock.patch.object(routes.shell_liveness, "compute",
                              return_value={"supported": True,
                                            "processes": []}),
        ]
        for p in self.patches:
            p.start()
        routes._browser_sessions.clear()
        routes.ensure_operator_capability()
        (run_dir / "operator.token").write_text("optok")
        routes.bind_runtime(FakeRuntime())
        self._start_transport()

    def tearDown(self):
        self._stop_transport()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    # -- the live server -----------------------------------------------------

    def _start_transport(self):
        """The real multiplex on an ephemeral loopback port, in its own loop.

        `routes.handle` is passed as the HTTP handler exactly as `server.py`
        passes it, so the header parsing under test is the transport's own,
        not a test double's reconstruction of it."""
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(self.loop)
            self.transport = transport_mod.Transport(
                "127.0.0.1", 0, routes.handle, self._no_ws, log=lambda *_: None)
            self.loop.run_until_complete(self.transport.start())
            self.port = self.transport.port
            ready.set()
            self.loop.run_forever()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        self.assertTrue(ready.wait(10), "transport did not start")
        self.origin = f"http://127.0.0.1:{self.port}"

    async def _no_ws(self, reader, writer, head_raw):  # pragma: no cover
        writer.close()

    def _stop_transport(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=10)
        self.loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(self.transport.stop()))
        self.loop.close()

    # -- a real HTTP request -------------------------------------------------

    def http(self, method, path, headers=None, body=None):
        """One request on a fresh connection (responses are Connection:
        close). Returns (status, headers, parsed-json-body)."""
        payload = json.dumps(body).encode() if body is not None else None
        sent = dict(headers or {})
        if payload is not None:
            sent.setdefault("Content-Type", "application/json")
        con = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            con.request(method, path, body=payload, headers=sent)
            resp = con.getresponse()
            raw = resp.read()
            try:
                parsed = json.loads(raw or b"{}")
            except ValueError:
                parsed = {}
            return resp.status, dict(resp.getheaders()), parsed
        finally:
            con.close()

    def browser_headers(self, **extra):
        """What a real Interface page's fetch() puts on the wire."""
        return {"Origin": self.origin, "Sec-Fetch-Site": "same-origin",
                **extra}

    def mint(self, **extra):
        """Bootstrap a session the legitimate way, over the wire."""
        status, headers, body = self.http(
            "POST", "/api/interface/browser-sessions",
            self.browser_headers(**{"Idempotency-Key": "gate-mint", **extra}),
            {})
        self.assertEqual(status, 201, body)
        cookie = headers["Set-Cookie"].split(";")[0]
        return cookie, body["csrf"]

    def chat_count(self):
        import sqlite3
        con = sqlite3.connect(self.db_path)
        try:
            return con.execute(
                "SELECT COUNT(*) FROM interface_sessions").fetchone()[0]
        finally:
            con.close()

    # -- release gate: no foreign origin mints a session ---------------------

    def test_a_foreign_origin_cannot_mint_a_session(self):
        hostile = (
            ("a hostile page's fetch",
             {"Origin": "http://evil.example.com",
              "Sec-Fetch-Site": "cross-site"}),
            ("a hostile page lying about its Origin",
             {"Origin": self.origin, "Sec-Fetch-Site": "cross-site"}),
            ("provenance stripped entirely", {}),
            ("Origin without fetch metadata", {"Origin": self.origin}),
            ("fetch metadata without an Origin",
             {"Sec-Fetch-Site": "same-origin"}),
            # Same machine is NOT same origin: a page served on another port
            # of this host is a different origin and must lose, even though
            # it can honestly claim same-origin fetch metadata for itself.
            ("another port on the same host",
             {"Origin": "http://127.0.0.1:1",
              "Sec-Fetch-Site": "same-origin"}),
            ("the same host under a different name",
             {"Origin": f"http://localhost:{self.port}",
              "Sec-Fetch-Site": "same-origin"}),
            ("an opaque origin (sandboxed frame / redirect)",
             {"Origin": "null", "Sec-Fetch-Site": "same-origin"}),
        )
        for label, headers in hostile:
            with self.subTest(label):
                status, resp_headers, body = self.http(
                    "POST", "/api/interface/browser-sessions",
                    {**headers, "Idempotency-Key": f"gate-{len(label)}"}, {})
                self.assertEqual(status, 403, body)
                self.assertEqual(body["error"]["code"], "not_same_origin")
                self.assertNotIn("Set-Cookie", resp_headers,
                                 f"{label} was handed a session cookie")
        self.assertEqual(routes._browser_sessions, {},
                         "a rejected mint left server-side state behind")

    def test_a_rebound_host_cannot_mint_a_session(self):
        # DNS rebinding: the attacker controls the name, so the Host header is
        # theirs to choose. The allowlist is checked before anything else.
        status, resp_headers, body = self.http(
            "POST", "/api/interface/browser-sessions",
            {"Host": "evil.example.com", "Origin": "http://evil.example.com",
             "Sec-Fetch-Site": "same-origin", "Idempotency-Key": "gate-dns"},
            {})
        self.assertEqual(status, 403, body)
        self.assertEqual(body["error"]["code"], "host_not_allowed")
        self.assertNotIn("Set-Cookie", resp_headers)
        self.assertEqual(routes._browser_sessions, {})

    # -- release gate: no foreign origin USES a session ----------------------

    def test_a_foreign_origin_cannot_use_a_session(self):
        # The strongest form of the case: the attacker is assumed to hold BOTH
        # the cookie and its anti-forgery token, and still loses on
        # provenance. (SameSite=Strict is what actually stops the cookie
        # riding along; this proves the server does not rely on it alone.)
        cookie, csrf = self.mint()
        before = self.chat_count()
        status, _, body = self.http(
            "POST", "/api/interface/sessions",
            {"Cookie": cookie, "X-CSRF": csrf,
             "Origin": "http://evil.example.com",
             "Sec-Fetch-Site": "cross-site",
             "Idempotency-Key": "gate-xsite"},
            {"shell_id": 1})
        self.assertEqual(status, 403, body)
        self.assertEqual(body["error"]["code"], "not_same_origin")
        self.assertEqual(self.chat_count(), before,
                         "a cross-site mutation reached its handler")

    # -- release gate: the anti-forgery gate fails closed --------------------

    def test_a_malformed_anti_forgery_token_fails_closed(self):
        cookie, csrf = self.mint()
        before = self.chat_count()
        for label, value in (("a guessed token", "not-the-token"),
                             ("an empty token", ""),
                             ("another session's token", "0" * 48)):
            with self.subTest(label):
                status, resp_headers, body = self.http(
                    "POST", "/api/interface/sessions",
                    {"Cookie": cookie, "X-CSRF": value,
                     "Idempotency-Key": f"gate-csrf-{len(label)}"},
                    {"shell_id": 1})
                self.assertEqual(status, 403, body)
                self.assertEqual(body["error"]["code"], "csrf")
                # Fails CLOSED: the rejection must not hand back a usable
                # token, or the gate becomes a retry.
                self.assertNotIn("Set-Cookie", resp_headers)
                self.assertNotIn("csrf", body)
        self.assertEqual(self.chat_count(), before)
        # The legitimate token still works — the gate rejects forgery, not
        # everything.
        status, _, body = self.http(
            "POST", "/api/interface/sessions",
            {"Cookie": cookie, "X-CSRF": csrf,
             "Idempotency-Key": "gate-ok"}, {"shell_id": 1})
        self.assertEqual(status, 201, body)
        self.assertEqual(self.chat_count(), before + 1)

    def test_a_cookie_alone_cannot_mutate(self):
        cookie, _ = self.mint()
        before = self.chat_count()
        status, _, body = self.http(
            "POST", "/api/interface/sessions",
            {"Cookie": cookie, "Idempotency-Key": "gate-cookieonly"},
            {"shell_id": 1})
        self.assertEqual(status, 403, body)
        self.assertEqual(body["error"]["code"], "csrf")
        self.assertEqual(self.chat_count(), before)
        # ...while a read on the same cookie is fine: the cookie is an
        # identity, the anti-forgery token is the mutation authority.
        status, _, _ = self.http("GET", "/api/interface/shells",
                                 {"Cookie": cookie})
        self.assertEqual(status, 200)

    def test_no_permissive_cors_on_any_response(self):
        # CORS staying OFF is what keeps a hostile page from READING a
        # response even where it can cause a request. Probe the real
        # responses rather than one route's success path.
        cookie, csrf = self.mint()
        probes = (
            ("GET", "/api/interface/shells", {"Cookie": cookie}, None),
            ("OPTIONS", "/api/interface/sessions",
             {"Origin": "http://evil.example.com",
              "Access-Control-Request-Method": "POST"}, None),
            ("POST", "/api/interface/browser-sessions",
             {"Origin": "http://evil.example.com",
              "Sec-Fetch-Site": "cross-site",
              "Idempotency-Key": "gate-cors"}, {}),
            ("POST", "/api/interface/sessions",
             {"Cookie": cookie, "X-CSRF": csrf,
              "Idempotency-Key": "gate-cors2"}, {"shell_id": 1}),
        )
        banned = ("access-control-allow-origin",
                  "access-control-allow-credentials",
                  "access-control-allow-methods",
                  "access-control-allow-headers")
        for method, path, headers, body in probes:
            with self.subTest(f"{method} {path}"):
                _, resp_headers, _ = self.http(method, path, headers, body)
                present = {k.lower() for k in resp_headers}
                for name in banned:
                    self.assertNotIn(name, present,
                                     f"{method} {path} answered with {name}")

    # -- the cookie lifecycle those fences depend on -------------------------

    def test_the_cookie_lifecycle_holds_over_the_wire(self):
        """Mint → rotate → revoke → restart, on real connections.

        Rotation and restart recovery need a real cookie round-trip to mean
        anything (an in-process call can 'rotate' a dict without ever proving
        the browser is handed a different credential); they do not need
        browser automation."""
        status, headers, body = self.http(
            "POST", "/api/interface/browser-sessions",
            self.browser_headers(**{"Idempotency-Key": "life-1"}), {})
        self.assertEqual(status, 201, body)
        set_cookie = headers["Set-Cookie"]
        # The attributes are the control, so assert them on the wire.
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Strict", set_cookie)
        self.assertIn("Path=/", set_cookie)
        self.assertNotIn("Secure", set_cookie)  # plain http origin
        first, first_csrf = set_cookie.split(";")[0], body["csrf"]
        self.assertNotIn(first_csrf, set_cookie,
                         "the anti-forgery token rode along in the cookie")

        status, _, _ = self.http("GET", "/api/interface/shells",
                                 {"Cookie": first})
        self.assertEqual(status, 200)

        # Rotation: the presented session is replaced, and the replacement is
        # a genuinely different credential delivered to the client.
        status, headers, body = self.http(
            "POST", "/api/interface/browser-sessions",
            self.browser_headers(**{"Cookie": first,
                                    "Idempotency-Key": "life-2"}), {})
        self.assertEqual(status, 201, body)
        second, second_csrf = headers["Set-Cookie"].split(";")[0], body["csrf"]
        self.assertNotEqual(second, first)
        self.assertNotEqual(second_csrf, first_csrf)

        status, _, body = self.http("GET", "/api/interface/shells",
                                    {"Cookie": first})
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "browser_session_expired")
        # The old anti-forgery token is dead with it.
        status, _, body = self.http(
            "POST", "/api/interface/sessions",
            {"Cookie": first, "X-CSRF": first_csrf,
             "Idempotency-Key": "life-3"}, {"shell_id": 1})
        self.assertEqual(status, 401)

        status, _, _ = self.http("GET", "/api/interface/shells",
                                 {"Cookie": second})
        self.assertEqual(status, 200)

        # A service restart drops live-process state: every session dies, and
        # the client is told to bootstrap rather than shown an error.
        routes._browser_sessions.clear()
        status, _, body = self.http("GET", "/api/interface/shells",
                                    {"Cookie": second})
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "browser_session_expired")
        status, headers, body = self.http(
            "POST", "/api/interface/browser-sessions",
            self.browser_headers(**{"Cookie": second,
                                    "Idempotency-Key": "life-4"}), {})
        self.assertEqual(status, 201, body)
        third = headers["Set-Cookie"].split(";")[0]
        status, _, _ = self.http("GET", "/api/interface/shells",
                                 {"Cookie": third})
        self.assertEqual(status, 200)

    def test_expiry_over_the_wire_deletes_the_session(self):
        import time
        cookie, _ = self.mint()
        sid = cookie.split("=", 1)[1]
        routes._browser_sessions[sid]["last_seen"] = (
            time.time() - routes.BROWSER_SESSION_TTL_S - 1)
        status, _, body = self.http("GET", "/api/interface/shells",
                                    {"Cookie": cookie})
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "browser_session_expired")
        self.assertNotIn(sid, routes._browser_sessions)


if __name__ == "__main__":
    unittest.main()
