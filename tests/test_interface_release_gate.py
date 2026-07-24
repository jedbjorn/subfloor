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
import server  # noqa: E402  (the app shell's CSP, served)
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
        routes._browser_bootstraps.clear()
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
        # Shut the transport down while the loop is still RUNNING and wait for
        # it. Scheduling stop() after loop.stop() only creates a task nothing
        # ever executes, so the listening socket outlives the test class.
        asyncio.run_coroutine_threadsafe(
            self.transport.stop(), self.loop).result(timeout=10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=10)
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
        routes._browser_bootstraps.clear()
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

    # -- SC-150: the Host fence and the bind guard must agree ----------------

    def test_an_ipv6_loopback_host_reaches_the_api_it_is_accepted_for(self):
        """`require_loopback_bind()` accepts `::1` and `[::1]`, so a fork may
        legitimately be configured that way — and every call used to come back
        `403 host_not_allowed`, because the Host fence split `[::1]:PORT` at
        the first colon and compared `"["` against the allowlist. A bind the
        engine accepts at startup but that cannot use its own API is not a
        supported bind, it is a broken one.

        The socket still runs over 127.0.0.1: the fence reads the Host HEADER,
        so the header is what this varies.
        """
        v6 = f"[::1]:{self.port}"
        status, headers, body = self.http(
            "POST", "/api/interface/browser-sessions",
            {"Host": v6, "Origin": f"http://{v6}",
             "Sec-Fetch-Site": "same-origin", "Idempotency-Key": "v6-mint"},
            {})
        self.assertEqual(status, 201, body)
        cookie = headers["Set-Cookie"].split(";")[0]
        status, _, body = self.http("GET", "/api/interface/shells",
                                    {"Host": v6, "Cookie": cookie})
        self.assertEqual(status, 200, body)

    def test_a_malformed_bracketed_host_fails_closed(self):
        # Widening the parse must not soften the DNS-rebind fence: anything
        # that is not exactly an allowed authority is still refused.
        # The last two are the port hole: an allowed host with a non-numeric
        # "port" that the old `split(":")[0]` waved through on the IPv4 side.
        for host in ("[::1", "[::1]evil.example.com", "[]", "[evil]:8800",
                     "[::2]:8800", "[::1]:8800.evil.example.com",
                     "127.0.0.1:8800.evil.example.com"):
            with self.subTest(host=host):
                status, _, body = self.http(
                    "GET", "/api/interface/shells", {"Host": host})
                self.assertEqual(status, 403, body)
                self.assertEqual(body["error"]["code"], "host_not_allowed")

    # -- SC-151: the bootstrap honours the key it demands --------------------

    def _boot(self, key, **extra):
        return self.http("POST", "/api/interface/browser-sessions",
                         self.browser_headers(**{"Idempotency-Key": key,
                                                 **extra}), {})

    def test_an_exact_bootstrap_retry_replays_instead_of_minting_again(self):
        """The endpoint required an `Idempotency-Key` and never keyed on it,
        so the retry of a lost `201` minted a SECOND live session with
        DIFFERENT credentials — the client keeping one and the server holding
        two, with the orphan live for its full 24 hours. Demanding a guarantee
        the route does not provide is the defect; this pins the guarantee."""
        first = self._boot("boot-retry")
        second = self._boot("boot-retry")
        self.assertEqual((first[0], second[0]), (201, 201))
        self.assertEqual(first[1]["Set-Cookie"], second[1]["Set-Cookie"])
        self.assertEqual(first[2]["csrf"], second[2]["csrf"])
        self.assertEqual(
            len(routes._browser_sessions), 1,
            "an exact retry minted a second live session")
        # The replayed credential is a working one, not just an equal string.
        status, _, body = self.http(
            "GET", "/api/interface/shells",
            {"Cookie": first[1]["Set-Cookie"].split(";")[0]})
        self.assertEqual(status, 200, body)

    def test_a_fresh_key_still_mints_an_independent_session(self):
        # The counterweight to the replay: a second tab bootstraps with its
        # own key and must get its own session, or the fix has broken
        # concurrent browsers (spec #26 Session Lifecycle).
        first = self._boot("boot-tab-1")
        second = self._boot("boot-tab-2")
        self.assertNotEqual(first[1]["Set-Cookie"], second[1]["Set-Cookie"])
        self.assertNotEqual(first[2]["csrf"], second[2]["csrf"])
        self.assertEqual(len(routes._browser_sessions), 2)

    def test_a_key_reused_from_a_different_origin_conflicts(self):
        # Both authorities are allowed, so this passes the provenance fence
        # and fails on the key alone — matching `_idempotent()`'s contract
        # rather than silently handing origin A's credential to origin B.
        self.assertEqual(self._boot("boot-shared")[0], 201)
        other = f"localhost:{self.port}"
        status, _, body = self.http(
            "POST", "/api/interface/browser-sessions",
            {"Host": other, "Origin": f"http://{other}",
             "Sec-Fetch-Site": "same-origin",
             "Idempotency-Key": "boot-shared"}, {})
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"]["code"], "idempotency_conflict")

    def test_a_replay_whose_session_died_mints_fresh(self):
        # Replaying a revoked or restart-lost credential would be idempotent
        # and useless — an answer that is consistent and wrong. The record
        # falls through to a real mint instead.
        first = self._boot("boot-stale")
        routes._browser_sessions.clear()          # what a restart does
        second = self._boot("boot-stale")
        self.assertEqual(second[0], 201)
        self.assertNotEqual(second[1]["Set-Cookie"], first[1]["Set-Cookie"])
        status, _, body = self.http(
            "GET", "/api/interface/shells",
            {"Cookie": second[1]["Set-Cookie"].split(";")[0]})
        self.assertEqual(status, 200, body)

    def test_a_replayed_bootstrap_never_reaches_the_durable_db(self):
        # Spec #26: browser sessions and their credentials are live-process
        # state only. The general `_idempotent()` path would have persisted
        # the response body — which holds the anti-forgery token — so the
        # replay store deliberately does not use it.
        import sqlite3
        self._boot("boot-durable")
        self._boot("boot-durable")
        con = sqlite3.connect(self.db_path)
        try:
            rows = con.execute(
                "SELECT COUNT(*) FROM interface_idempotency_keys").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(rows, 0,
                         "a browser-session credential reached the durable DB")

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


class AppShellCspTest(unittest.TestCase):
    """The Content-Security-Policy a browser actually receives, read off a
    real socket through the real `server.dispatch_http`.

    Spec #26 Delivery Plan step 6 owed a CSP check and the tree contained
    none, which is how `connect-src 'self' ws: wss:` shipped while the
    comment above it promised same-origin connections only (conformance
    finding SC-152). `CspSourceListTest` in test_server_schema_guard.py pins
    the policy STRING; this pins that a served response carries it, because a
    policy no response emits fences nothing — and the Playwright server that
    rendered the UI never served the production header at all.
    """

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(self.loop)
            self.transport = transport_mod.Transport(
                "127.0.0.1", 0, server.dispatch_http, self._no_ws,
                log=lambda *_: None)
            self.loop.run_until_complete(self.transport.start())
            self.port = self.transport.port
            ready.set()
            self.loop.run_forever()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        self.assertTrue(ready.wait(10), "transport did not start")
        # main() binds the policy to the port it serves; do the same for the
        # ephemeral port here so the assertion below is end-to-end and not a
        # comparison of the module default against itself.
        csp = server._CSP
        self.addCleanup(lambda: setattr(server, "_CSP", csp))
        server._CSP = server._csp(self.port)

    async def _no_ws(self, reader, writer, head_raw):  # pragma: no cover
        writer.close()

    def tearDown(self):
        asyncio.run_coroutine_threadsafe(
            self.transport.stop(), self.loop).result(timeout=10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=10)
        self.loop.close()

    def _get(self, path):
        con = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            con.request("GET", path)
            resp = con.getresponse()
            resp.read()
            return resp.status, dict(resp.getheaders())
        finally:
            con.close()

    def test_the_app_shell_is_served_with_the_policy(self):
        status, headers = self._get("/")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Security-Policy"), server._CSP)

    def test_the_served_connect_src_authorises_no_arbitrary_socket_host(self):
        _, headers = self._get("/")
        connect = next(
            part.split()[1:] for part in
            headers["Content-Security-Policy"].split(";")
            if part.split() and part.split()[0] == "connect-src")
        for src in connect:
            with self.subTest(source=src):
                self.assertFalse(
                    src.endswith(":") and "//" not in src,
                    f"{src!r} is a CSP scheme-source and matches any host")
        # ...and the stream the UI actually opens is still authorised.
        self.assertIn(f"ws://127.0.0.1:{self.port}", connect)


if __name__ == "__main__":
    unittest.main()
