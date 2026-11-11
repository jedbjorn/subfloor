#!/usr/bin/env python3
"""The review server's sign-in gate (Feature #94, spec #271).

Acceptance 1: no browser route answers without a session. The route set is
DERIVED from the source of the three route modules, so a route added later is
walked without anyone listing it here.
Acceptance 3 (review-server half): a session passes; revocation holds from the
next request and from an open stream's next authorisation point; a published
hash is not a token; no readable list means signed out.
Acceptance 4: every audited CLI caller succeeds against an isolated server with
its credential; a shell with no credential gets 401; a shell credential on an
operator route is refused.

Nothing here touches a live engine: the DB is a temporary file, the session
list is a temporary directory, and the one real listener binds 127.0.0.1 on a
free port and is stopped by the test that started it.

Run:
    python3 -m pytest tests/test_signin_gate.py -q
"""
from __future__ import annotations

import ast
import asyncio
import http.client
import io
import itertools
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / ".super-coder"
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(ENGINE / "api"))

sys.path.insert(0, str(REPO))
import server  # noqa: E402  (adds scripts/ to the path on import)
import auth_sessions  # noqa: E402
import browser_gate  # noqa: E402
import cli_credential  # noqa: E402
import conversation_routes  # noqa: E402
import mem  # noqa: E402
import models as models_cli  # noqa: E402
import review_routes  # noqa: E402
import sandbox_devkit  # noqa: E402
import snapshot as snapshot_cli  # noqa: E402
import transport as transport_mod  # noqa: E402
from signin_fixture import COOKIE_NAME, publish, sign_in, signed_out  # noqa: E402
from maintainer import capture_public_docs  # noqa: E402
from maintainer import dos_app_sprint_canary as canary  # noqa: E402

# The one module object the two tools hold (they import it by its bare name).
tool_session = canary.tool_session

HOST = "127.0.0.1:8800"
CLI_PORT = 8800          # the port the in-process "server" of these tests holds
ROUTE_MODULES = ("server.py", "conversation_routes.py", "review_routes.py")
# Every module under api/ that names a `/api` or `/_sc` path at all, pinned.
# The walk reads ALL of api/*.py, so a path named in a new module is walked
# without anyone listing it; this pin makes that module's arrival a failure
# someone has to look at (`test_the_walk_knows_every_module_that_names_a_route`).
MODULES_NAMING_ROUTES = {"browser_gate.py", *ROUTE_MODULES}
# The modules `dispatch_http` hands a request to, pinned for the same reason.
DISPATCH_TARGETS = {"request_timing.handle", "review_routes.handle",
                    "conversation_routes.handle"}
METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
SHELL_TOKEN = "shell-token"
CONVERSATION = "cv_" + "0" * 32

# THE open list, pinned. Widening `browser_gate.OPEN_ROUTES` must be a change
# to this test too, made by someone looking at what they are opening.
EXPECTED_OPEN = {
    ("GET", "/api/health"),
    ("GET", "/api/auth/status"),
    ("GET", "/signin.html"),
    ("GET", "/signin.js"),
    ("GET", "/signin.css"),
    ("GET", "/theme.js"),
    ("GET", "/style.css"),
    ("GET", "/vendor/qrcode.js"),
    ("HEAD", "/vendor/qrcode.js"),
}
EXPECTED_CLI = {
    ("POST", "/api/scripts/snapshot", ""),
    ("GET", "/api/models", "refresh=1"),
}

_DB_DIR: tempfile.TemporaryDirectory | None = None
_DB_PATH: Path | None = None


def setUpModule() -> None:
    global _DB_DIR, _DB_PATH
    _DB_DIR = tempfile.TemporaryDirectory(prefix="sc-gate-db-")
    _DB_PATH = Path(_DB_DIR.name) / "engine.db"
    source = sqlite3.connect(":memory:")
    source.executescript((ENGINE / "schema.sql").read_text())
    for migration in sorted((ENGINE / "migrations").glob("*.sql")):
        source.executescript(migration.read_text())
    source.execute(
        "INSERT INTO users (user_id,username,is_active) VALUES (1,'operator',1)")
    source.execute(
        "INSERT INTO users (user_id,username,is_active) VALUES (2,'gone',0)")
    source.execute(
        "INSERT INTO shells (display_name, system_prompt, flavor, shortname, api_key) "
        "VALUES ('Dev', 'x', 'dev', 'dev', ?)", (SHELL_TOKEN,))
    source.commit()
    target = sqlite3.connect(_DB_PATH)
    source.backup(target)
    target.close()
    source.close()


def tearDownModule() -> None:
    if _DB_DIR is not None:
        _DB_DIR.cleanup()


def _connect():
    con = sqlite3.connect(_DB_PATH)
    con.row_factory = sqlite3.Row
    return con


class GateCase(unittest.TestCase):
    """The three route modules on a temporary DB; no CLI token unless set."""

    def setUp(self) -> None:
        for module, name in ((server, "db"), (conversation_routes, "_db"),
                             (review_routes, "_db")):
            patcher = mock.patch.object(module, name, side_effect=_connect)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(browser_gate, "CLI_TOKEN", None)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(server.engine_ops, "discovery", return_value={
            "port": 9137, "url": "http://127.0.0.1:9137", "up": True,
            "up_source": "probe", "start_cli": "./sc launch"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, method: str, path: str, headers: dict | str | None = None,
             body: bytes = b""):
        if headers is None:
            headers = {"Host": HOST}
        raw = headers if isinstance(headers, str) else "\r\n".join(
            f"{key}: {value}" for key, value in headers.items())
        status, out_headers, out = server.dispatch_http(method, path, raw, body)
        return status, dict(out_headers), out

    def json(self, method: str, path: str, headers=None, body: bytes = b""):
        status, out_headers, out = self.call(method, path, headers, body)
        return status, out_headers, json.loads(out)

    def assert_sign_in_required(self, status: int, payload: dict) -> None:
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "sign_in_required")
        self.assertEqual(payload["error"]["details"]["sign_in"], "/signin.html")


# ── Acceptance 1: the walk ───────────────────────────────────────────────────

def _sample_pattern(pattern: str) -> list[str]:
    """Concrete paths for one of the route regexes, every alternative taken.

    Understands exactly what the route modules use: literals, `[0-9a-f]{n}`,
    and `(a|b|c)` groups. Anything else fails the `fullmatch` below, which is
    the point: a new kind of route pattern must be taught here, not skipped.
    """
    body = pattern.lstrip("^").rstrip("$")
    pieces: list[list[str]] = []
    position = 0
    token = re.compile(r"\(([^()]*)\)|\[0-9a-f\]\{(\d+)\}")
    for match in token.finditer(body):
        pieces.append([body[position:match.start()]])
        if match.group(2):
            pieces.append(["0" * int(match.group(2))])
        else:
            inner = match.group(1)
            hexrun = re.fullmatch(r"(.*)\[0-9a-f\]\{(\d+)\}", inner)
            if hexrun:
                pieces.append([hexrun.group(1) + "0" * int(hexrun.group(2))])
            else:
                pieces.append(inner.split("|"))
        position = match.end()
    pieces.append([body[position:]])
    samples = ["".join(parts) for parts in itertools.product(*pieces)]
    for sample in samples:
        if re.fullmatch(pattern, sample) is None:
            raise AssertionError(
                f"route pattern {pattern!r} is not understood by this test's "
                f"sampler (produced {sample!r}); teach _sample_pattern")
    return samples


def _route_literals(source: str) -> list[str]:
    return [node.value for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and re.match(r"\^?/(api|_sc)(/|$)", node.value)]


def derive_paths() -> set[str]:
    """Every path any module under api/ names, read from its source.

    Each string literal that is a path under `/api` or `/_sc`, each route
    regex, each key of the static table, and the vendor prefix. A prefix
    literal (`/api/shells/`) is also walked with something under it, in the
    shapes the handlers split on.

    The limit of this enumeration, stated: it finds a route by the path
    LITERAL that names it. A route whose path is assembled at run time, or
    matched only by splitting (`parts[1:3] == ["api", "shells"]`), is not
    named and is reached here only through the prefix expansions and strays
    below. That does not leave such a route open: the gate decides by default
    deny before any route is chosen, and `test_nothing_in_dispatch_runs_
    before_the_gate` holds that. What enumeration adds is the proof, route by
    route, for every route that is named. Two pins keep it honest as the
    server grows: the set of modules that name a route, and the set of
    modules `dispatch_http` hands a request to.
    """
    paths: set[str] = set(server._STATIC) | {"/vendor/", "/vendor/marked.umd.js",
                                              "/vendor/purify.min.js"}
    for module in sorted((ENGINE / "api").glob("*.py")):
        tree = ast.parse(module.read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            value = node.value
            if re.match(r"\^/(api|_sc)/", value):
                paths.update(_sample_pattern(value))
            elif re.fullmatch(r"/(api|_sc)(/[A-Za-z0-9_.\-/]*)?", value):
                paths.add(value)
                if value.endswith("/"):
                    for tail in ("1", "snapshot", "1/open", "1/skills/1",
                                 "1/inbox/drain", "dev/skills/1", "1/up",
                                 "retire/x", CONVERSATION,
                                 CONVERSATION + "/events"):
                        paths.add(value + tail)
    # Shapes no literal spells, and plain strays: default deny covers them too.
    paths.update({"/api", "/api/flavors/dev/skills/1", "/api/nothing-here",
                  "/nothing-here", "/nothing/here.js", "/index.html/x",
                  "//api/health", "/api/health/", "/API/health",
                  "/api/health%2f", "/signin.html/", "/vendor/qrcode.js/x",
                  "/vendor/../index.html", "/vendor/%71rcode.js"})
    return paths


def walk(case: GateCase, headers: dict | None = None) -> list[str]:
    """Request every derived path with every method; return what was wrong.

    Without a session a request must be refused: `401 sign_in_required`, or,
    for a `GET` of a page, `302` to the sign-in page. `/_sc/*` keeps its own
    Bearer rule: `401`, or `404`/`405` where no such route exists.
    """
    wrong: list[str] = []
    for path in sorted(derive_paths()):
        routed = browser_gate.request_path(path)
        for method in METHODS:
            status, out_headers, out = case.call(method, path, headers)
            label = f"{method} {path} -> {status}"
            if (method, routed) in EXPECTED_OPEN:
                if status in (401, 302):
                    wrong.append(label + " (on the open list, but refused)")
                continue
            if browser_gate.is_shell_api(routed):
                if status not in (401, 404, 405):
                    wrong.append(label + " (a /_sc route answered with no Bearer)")
                continue
            if method == "GET" and browser_gate.is_document(routed):
                location = out_headers.get("Location", "")
                if status != 302 or urlparse(location).path != "/signin.html":
                    wrong.append(label + f" (expected 302 to sign-in, got {location!r})")
                continue
            if method == "HEAD":
                # The same refusal, and no body after the headers.
                if status != 401 or out != b"":
                    wrong.append(label + f" (expected a bodiless 401, body={out[:40]!r})")
                continue
            try:
                code = json.loads(out)["error"]["code"]
            except Exception:  # noqa: BLE001 — any other body is the failure
                code = None
            if status != 401 or code != "sign_in_required":
                wrong.append(label + f" (expected 401 sign_in_required, code={code!r})")
    return wrong


class NoRouteAnswersWithoutASession(GateCase):
    def setUp(self) -> None:
        super().setUp()
        signed_out(self)

    def test_the_open_list_is_exactly_this(self) -> None:
        self.assertEqual(set(browser_gate.OPEN_ROUTES), EXPECTED_OPEN)
        self.assertEqual(set(browser_gate.CLI_ROUTES), EXPECTED_CLI)

    def test_the_route_set_is_read_from_the_source(self) -> None:
        paths = derive_paths()
        for known in ("/api/shells", "/api/flags", "/api/snapshot", "/api/publish",
                      "/api/engine-ops", "/api/scripts/snapshot", "/api/models",
                      "/api/diagnostics/request-timing", "/api/conversations",
                      f"/api/conversations/{CONVERSATION}/events",
                      f"/api/conversations/{CONVERSATION}/review-targets",
                      "/api/review-targets/gt_" + "0" * 32 + "/diff",
                      "/_sc/mem/whoami", "/", "/index.html", "/app.js"):
            self.assertIn(known, paths)
        self.assertGreater(len(paths), 250)

    def test_the_walk_knows_every_module_that_names_a_route(self) -> None:
        """A new module that names `/api` or `/_sc` paths, or a new branch in
        `dispatch_http`, fails here until someone has looked at it."""
        naming = {module.name for module in (ENGINE / "api").glob("*.py")
                  if _route_literals(module.read_text())}
        self.assertEqual(naming, MODULES_NAMING_ROUTES)
        tree = ast.parse((ENGINE / "api" / "server.py").read_text())
        dispatch = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.FunctionDef)
                        and node.name == "dispatch_http")
        handed_to = {ast.unparse(node.func) for node in ast.walk(dispatch)
                     if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Attribute)
                     and node.func.attr == "handle"}
        self.assertEqual(handed_to, DISPATCH_TARGETS)
        # Each of those modules is one the walk reads, and each path prefix
        # `dispatch_http` peels off is a literal the walk found.
        for target in DISPATCH_TARGETS - {"request_timing.handle"}:
            self.assertIn(target.split(".")[0] + ".py", ROUTE_MODULES)
        paths = derive_paths()
        for literal in _route_literals(ast.unparse(dispatch)):
            self.assertIn(literal, paths)
        # And the transport is given exactly these two ways in.
        serve = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                     and ast.unparse(node.func) == "transport.serve")
        self.assertEqual(ast.unparse(serve.args[2]), "dispatch_http")
        self.assertEqual(
            {keyword.arg: ast.unparse(keyword.value) for keyword in serve.keywords},
            {"on_bound": "_on_bound", "on_started": "_on_listening",
             "stream_handler": "conversation_routes.stream_events"})

    def test_every_route_refuses_without_a_cookie(self) -> None:
        self.assertEqual(walk(self), [])

    def test_every_route_refuses_a_cookie_nobody_issued(self) -> None:
        stranger = "A" * 43
        self.assertEqual(walk(self, {
            "Host": HOST, "Origin": f"http://{HOST}",
            "Cookie": f"sc_session_0f1e2d3c={stranger}"}), [])

    def test_a_route_opened_without_being_pinned_fails_the_walk(self) -> None:
        """The walk is not vacuous, and it judges against the pin above, not
        against the constant it is checking: open one route and it says so."""
        opened = browser_gate.OPEN_ROUTES | {("GET", "/api/shells")}
        with mock.patch.object(browser_gate, "OPEN_ROUTES", opened):
            wrong = walk(self)
        self.assertEqual(wrong, [
            "GET /api/shells -> 200 (expected 401 sign_in_required, code=None)"])

    def test_a_route_placed_before_the_gate_fails_the_walk(self) -> None:
        """A route that answers ahead of the gate is caught by the walk."""
        real = server.dispatch_http

        def leaky(method, path, headers_raw, body):
            if urlparse(path).path == "/api/shells" and method == "GET":
                return 200, [("Content-Type", "application/json")], b"{}"
            return real(method, path, headers_raw, body)

        with mock.patch.object(server, "dispatch_http", leaky):
            wrong = walk(self)
        self.assertEqual(wrong, [
            "GET /api/shells -> 200 (expected 401 sign_in_required, code=None)"])

    def test_nothing_in_dispatch_runs_before_the_gate(self) -> None:
        tree = ast.parse((ENGINE / "api" / "server.py").read_text())
        function = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.FunctionDef)
                        and node.name == "dispatch_http")
        body = [node for node in function.body
                if not (isinstance(node, ast.Expr)
                        and isinstance(node.value, ast.Constant))]
        first, second = body[0], body[1]
        self.assertEqual(ast.unparse(first),
                         "refusal = _gate(method, path, headers_raw)")
        self.assertIsInstance(second, ast.If)
        self.assertEqual(ast.unparse(second.test), "refusal is not None")
        self.assertEqual(ast.unparse(second.body[0]), "return refusal")
        gate = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == "_gate")
        calls = [ast.unparse(node.func) for node in ast.walk(gate)
                 if isinstance(node, ast.Call)]
        self.assertIn("browser_gate.authorize", calls)
        self.assertEqual(ast.unparse(gate.body[-1]),
                         "return None if verdict.allowed else verdict.response")

    def test_a_gate_that_cannot_decide_refuses(self) -> None:
        """Fail closed: a DB fault during the check is a 503, never a pass."""
        browser = sign_in(self)

        def broken():
            raise sqlite3.OperationalError("database is locked")

        for headers in (browser.headers(),
                        {"Host": HOST, "Authorization": f"Bearer {SHELL_TOKEN}"}):
            for method, path in (("GET", "/api/shells"), ("POST", "/api/flags"),
                                 ("POST", "/api/scripts/snapshot"), ("GET", "/")):
                with self.subTest(method=method, path=path), \
                        mock.patch.object(server, "db", side_effect=broken), \
                        mock.patch.object(server, "run_script") as ran, \
                        mock.patch.object(server.traceback, "print_exc"):
                    status, out_headers, payload = self.json(method, path, headers)
                self.assertEqual(status, 503)
                self.assertEqual(payload["error"]["code"], "gate_unavailable")
                self.assertEqual(out_headers["Retry-After"], "2")
                ran.assert_not_called()
        # What needs no lookup is still decided: open stays open, and a
        # visitor is still refused.
        with mock.patch.object(server, "db", side_effect=broken):
            self.assertEqual(self.call("GET", "/api/auth/status")[0], 200)
            self.assertEqual(self.call("GET", "/api/shells")[0], 401)

    def test_the_transport_has_two_entries_and_both_are_gated(self) -> None:
        """`dispatch_http` and the stream are the only ways in."""
        source = (ENGINE / "api" / "server.py").read_text()
        serve = source[source.index("await transport.serve("):]
        serve = serve[:serve.index(")\n") + 1]
        self.assertIn("dispatch_http,", serve)
        self.assertIn("_ws_unavailable,", serve)
        self.assertIn("stream_handler=conversation_routes.stream_events", serve)

    def test_the_event_stream_refuses_without_a_cookie(self) -> None:
        for headers in (f"Host: {HOST}",
                        f"Host: {HOST}\r\nCookie: sc_session_0f1e2d3c={'A' * 43}"):
            writer = _Writer()
            claimed = asyncio.run(conversation_routes.stream_events(
                "GET", f"/api/conversations/{CONVERSATION}/events", headers, writer))
            self.assertTrue(claimed)
            head, _, body = writer.data.partition(b"\r\n\r\n")
            self.assertTrue(head.startswith(b"HTTP/1.1 401 "), head)
            self.assertEqual(json.loads(body)["error"]["code"], "sign_in_required")
            self.assertTrue(writer.closed)

    def test_only_the_stream_path_bypasses_dispatch(self) -> None:
        """Every other request the stream handler is offered, it declines."""
        for path in sorted(derive_paths()):
            if conversation_routes._EVENTS_PATH.fullmatch(urlparse(path).path):
                continue
            writer = _Writer()
            self.assertFalse(asyncio.run(conversation_routes.stream_events(
                "GET", path, f"Host: {HOST}", writer)), path)
            self.assertEqual(writer.data, b"")

    def test_health_is_open_and_says_nothing_new(self) -> None:
        status, _, payload = self.json("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_no_authorization_header_is_not_the_operator_at_the_routes(self) -> None:
        """The inference is gone where it was made, not only at the gate."""
        with self.assertRaises(conversation_routes.ApiError) as raised:
            con = _connect()
            try:
                conversation_routes._operator(
                    con, conversation_routes._parse_headers(f"Host: {HOST}"))
            finally:
                con.close()
        self.assertEqual(raised.exception.status, 401)
        self.assertEqual(raised.exception.code, "sign_in_required")
        handler = server._ShimHandler("GET", "/api/sprints", f"Host: {HOST}", b"")
        con = _connect()
        try:
            self.assertFalse(handler._require_browser_operator(con))
        finally:
            con.close()
        self.assertEqual(handler._shim_status, 401)
        self.assertEqual(json.loads(handler.wfile.getvalue())["error"]["code"],
                         "sign_in_required")
        for module in ROUTE_MODULES + ("request_timing.py",):
            source = (ENGINE / "api" / module).read_text()
            self.assertNotIn("WHERE is_active=1 ORDER BY user_id LIMIT 1", source,
                             f"{module} still picks 'the' operator without a session")


class _Writer:
    """What `stream_events` writes to, captured."""

    def __init__(self) -> None:
        self.data = b""
        self.closed = False
        self.wrote = asyncio.Event()

    def write(self, data: bytes) -> None:
        self.data += data
        self.wrote.set()

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


# ── Acceptance 3: the session ────────────────────────────────────────────────

class SessionLifecycle(GateCase):
    def setUp(self) -> None:
        super().setUp()
        self.browser = sign_in(self)

    def test_a_valid_session_passes(self) -> None:
        status, _, payload = self.json("GET", "/api/shells", self.browser.headers())
        self.assertEqual(status, 200)
        self.assertIn("shells", payload)
        # An operator-only route, and a route that used to check nothing.
        status, _, _ = self.json("GET", "/api/sprints", self.browser.headers())
        self.assertEqual(status, 200)
        status, _, _ = self.json("GET", "/api/flags", self.browser.headers())
        self.assertEqual(status, 200)
        status, _, payload = self.json(
            "GET", "/api/conversations", self.browser.headers())
        self.assertEqual(status, 200, payload)

    def test_revocation_holds_from_the_next_request(self) -> None:
        headers = self.browser.headers()
        self.assertEqual(self.call("GET", "/api/shells", headers)[0], 200)
        self.browser.revoke()
        status, _, payload = self.json("GET", "/api/shells", headers)
        self.assert_sign_in_required(status, payload)
        status, _, payload = self.json("GET", "/api/conversations", headers)
        self.assert_sign_in_required(status, payload)
        status, out_headers, _ = self.call("GET", "/", headers)
        self.assertEqual(status, 302)
        self.assertEqual(out_headers["Location"], "/signin.html")

    def test_the_hash_itself_is_not_a_token(self) -> None:
        published = json.loads(self.browser.path().read_text())
        digest = published["sessions"][0]["hash"]
        self.assertEqual(digest, self.browser.hash)
        for value in (digest, digest[:43]):
            headers = {"Host": HOST, "Origin": f"http://{HOST}",
                       "Cookie": f"{self.browser.cookie_name}={value}"}
            status, _, payload = self.json("GET", "/api/shells", headers)
            self.assert_sign_in_required(status, payload)
        self.assertNotIn(self.browser.token, self.browser.path().read_text())

    def test_no_readable_list_is_signed_out(self) -> None:
        headers = self.browser.headers()
        path = self.browser.path()
        good = path.read_text()
        for label, damage in (
            ("absent", lambda: path.unlink()),
            ("not json", lambda: path.write_text("{not json")),
            ("not an object", lambda: path.write_text("[]")),
            ("another version", lambda: path.write_text(
                good.replace('"version": 1', '"version": 2'))),
            ("no cookie name", lambda: path.write_text(
                good.replace(self.browser.cookie_name, "session"))),
            ("sessions not a list", lambda: path.write_text(json.dumps(
                {**json.loads(good), "sessions": {"hash": self.browser.hash}}))),
            ("a directory", lambda: (path.unlink(), path.mkdir())),
        ):
            with self.subTest(label):
                self.browser.publish()
                self.assertEqual(self.call("GET", "/api/shells", headers)[0], 200)
                damage()
                status, _, payload = self.json("GET", "/api/shells", headers)
                self.assert_sign_in_required(status, payload)
                if path.is_dir():
                    path.rmdir()

    def test_a_link_in_place_of_the_list_is_signed_out(self) -> None:
        headers = self.browser.headers()
        path = self.browser.path()
        elsewhere = path.with_name("elsewhere.json")
        path.rename(elsewhere)
        path.symlink_to(elsewhere)
        status, _, payload = self.json("GET", "/api/shells", headers)
        self.assert_sign_in_required(status, payload)

    def test_a_session_for_a_user_who_is_not_active_is_signed_out(self) -> None:
        for user_id in (2, 99):
            with self.subTest(user_id=user_id):
                publish(self.browser.directory, [(self.browser.token, user_id)])
                status, _, payload = self.json(
                    "GET", "/api/shells", self.browser.headers())
                self.assert_sign_in_required(status, payload)
                status, _, payload = self.json(
                    "GET", "/api/conversations", self.browser.headers())
                self.assert_sign_in_required(status, payload)
        for not_an_id in ("1", True, 1.0, None, [1]):
            with self.subTest(user_id=not_an_id):
                publish(self.browser.directory, [(self.browser.token, not_an_id)])
                status, _, payload = self.json(
                    "GET", "/api/shells", self.browser.headers())
                self.assert_sign_in_required(status, payload)

    def test_a_cookie_counts_only_on_a_loopback_host(self) -> None:
        for host in ("example.test:8800", "evil.127.0.0.1.example:8800", ""):
            with self.subTest(host=host):
                headers = self.browser.headers(host)
                if not host:
                    del headers["Host"]
                status, _, payload = self.json("GET", "/api/shells", headers)
                self.assert_sign_in_required(status, payload)
        for host in ("localhost:8800", "[::1]:8800", "127.0.0.1"):
            with self.subTest(host=host):
                status, _, _ = self.json(
                    "GET", "/api/shells", self.browser.headers(host))
                self.assertEqual(status, 200)

    def test_a_cookie_beside_an_authorization_header_is_not_a_session(self) -> None:
        headers = self.browser.headers()
        headers["Authorization"] = "Bearer not-a-shell"
        status, _, payload = self.json("GET", "/api/shells", headers)
        self.assert_sign_in_required(status, payload)
        headers["Authorization"] = ""
        status, _, payload = self.json("GET", "/api/shells", headers)
        self.assert_sign_in_required(status, payload)
        headers["Authorization"] = f"Bearer {SHELL_TOKEN}"
        status, _, payload = self.json("GET", "/api/shells", headers)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "fnb_operator_required")

    def test_another_installs_cookie_is_not_this_installs(self) -> None:
        headers = {"Host": HOST, "Origin": f"http://{HOST}",
                   "Cookie": f"sc_session_ffffffff={self.browser.token}"}
        status, _, payload = self.json("GET", "/api/shells", headers)
        self.assert_sign_in_required(status, payload)
        # Both present: this install's is the one read.
        headers["Cookie"] += f"; {self.browser.cookie}"
        self.assertEqual(self.call("GET", "/api/shells", headers)[0], 200)

    # -- the open stream -----------------------------------------------------

    def _stream(self, script):
        """Run the stream against a conversation that always exists, with an
        empty event log and a short heartbeat; `script(writer)` drives it."""
        async def main():
            writer = _Writer()
            task = asyncio.ensure_future(conversation_routes.stream_events(
                "GET", f"/api/conversations/{CONVERSATION}/events",
                self.browser.raw(origin=False), writer))
            await script(writer)
            claimed = await asyncio.wait_for(task, 10)
            return claimed, writer

        with mock.patch.object(conversation_routes, "_require_conversation"), \
                mock.patch.object(conversation_routes, "_event_batch",
                                  return_value=[]), \
                mock.patch.object(conversation_routes, "SSE_HEARTBEAT_SECONDS", 0.05):
            return asyncio.run(main())

    def test_an_open_stream_ends_at_its_next_authorisation_point(self) -> None:
        async def script(writer):
            # The stream is open and idling on heartbeats, signed in.
            while writer.data.count(b": keepalive") < 2:
                writer.wrote.clear()
                await asyncio.wait_for(writer.wrote.wait(), 10)
            self.assertTrue(writer.data.startswith(b"HTTP/1.1 200 OK"))
            self.assertNotIn(b"sign_in_required", writer.data)
            self.assertFalse(writer.closed)
            self.browser.revoke()

        claimed, writer = self._stream(script)
        self.assertTrue(claimed)
        self.assertTrue(writer.closed)
        tail = writer.data.rsplit(b"\n\n", 2)[-2]
        self.assertTrue(tail.startswith(b"event: sign_in_required\ndata: "), tail)
        payload = json.loads(tail.split(b"data: ", 1)[1])
        self.assertEqual(payload["error"]["code"], "sign_in_required")
        # And the reconnect an EventSource makes meets the gate.
        writer = _Writer()
        asyncio.run(conversation_routes.stream_events(
            "GET", f"/api/conversations/{CONVERSATION}/events",
            self.browser.raw(origin=False), writer))
        self.assertTrue(writer.data.startswith(b"HTTP/1.1 401 "))

    def test_an_open_stream_ends_when_its_user_is_deactivated(self) -> None:
        """The stream makes the gate's whole decision at each turn: the cookie
        still verifies here, and the stream ends all the same."""
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "engine.db"
            source = sqlite3.connect(_DB_PATH)
            target = sqlite3.connect(copy)
            source.backup(target)
            source.close()
            target.close()

            def connect():
                con = sqlite3.connect(copy)
                con.row_factory = sqlite3.Row
                return con

            async def script(writer):
                while writer.data.count(b": keepalive") < 2:
                    writer.wrote.clear()
                    await asyncio.wait_for(writer.wrote.wait(), 10)
                self.assertFalse(writer.closed)
                with connect() as con:
                    con.execute("UPDATE users SET is_active=0 WHERE user_id=1")
                # Several heartbeats pass; a stream that only looked at the
                # cookie would still be open after them.
                for _ in range(40):
                    if writer.closed:
                        break
                    await asyncio.sleep(0.05)
                self.assertTrue(
                    writer.closed,
                    "the stream outlived its user: it re-checked the cookie "
                    "but not users.is_active")

            with mock.patch.object(conversation_routes, "_db", side_effect=connect), \
                    mock.patch.object(server, "db", side_effect=connect):
                # A fresh request is refused, and so is the open stream.
                claimed, writer = self._stream(script)
                self.assertEqual(
                    self.call("GET", "/api/shells", self.browser.headers())[0], 401)
        self.assertTrue(claimed)
        self.assertIsNotNone(
            auth_sessions.session_from_cookie_header(self.browser.cookie),
            "the cookie itself was never revoked in this test")
        tail = writer.data.rsplit(b"\n\n", 2)[-2]
        self.assertTrue(tail.startswith(b"event: sign_in_required\ndata: "), tail)

    def test_a_stream_that_cannot_check_ends(self) -> None:
        async def script(writer):
            while writer.data.count(b": keepalive") < 1:
                writer.wrote.clear()
                await asyncio.wait_for(writer.wrote.wait(), 10)
            self.broken.start()

        self.broken = mock.patch.object(
            conversation_routes, "_db",
            side_effect=sqlite3.OperationalError("database is locked"))
        try:
            claimed, writer = self._stream(script)
        finally:
            self.broken.stop()
        self.assertTrue(writer.closed)
        self.assertIn(b"event: sign_in_required", writer.data)

    def test_one_decision_at_every_authorisation_point(self) -> None:
        """No second copy of "signed in": each point calls operator_session."""
        for module, function in (
            ("conversation_routes.py", "_operator"),
            ("conversation_routes.py", "_stream_still_signed_in"),
            ("server.py", "_require_browser_operator"),
            ("browser_gate.py", "_authorize"),
            ("browser_gate.py", "auth_status"),
        ):
            tree = ast.parse((ENGINE / "api" / module).read_text())
            node = next(n for n in ast.walk(tree)
                        if isinstance(n, ast.FunctionDef) and n.name == function)
            calls = {ast.unparse(c.func) for c in ast.walk(node)
                     if isinstance(c, ast.Call)}
            self.assertTrue(
                calls & {"browser_gate.operator_session", "operator_session"},
                f"{module}:{function} decides without operator_session")
            self.assertFalse(
                calls & {"browser_gate.session", "session",
                         "auth_sessions.session_from_cookie_header"},
                f"{module}:{function} makes a cookie-only decision")

    def test_an_open_stream_checks_before_every_batch(self) -> None:
        """Events are flowing, not idling: the check still runs each turn."""
        sequence = itertools.count(1)

        def batch(conversation_id, after):
            number = next(sequence)
            if number == 3:
                self.browser.revoke()
            return [{"sequence": number, "event_type": "note", "payload": {}}]

        async def script(writer):
            return None

        with mock.patch.object(conversation_routes, "_event_batch", batch):
            # `_stream` patches `_event_batch` itself; apply ours inside it.
            async def main():
                writer = _Writer()
                with mock.patch.object(conversation_routes, "_require_conversation"):
                    claimed = await asyncio.wait_for(
                        conversation_routes.stream_events(
                            "GET", f"/api/conversations/{CONVERSATION}/events",
                            self.browser.raw(origin=False), writer), 10)
                return claimed, writer
            claimed, writer = asyncio.run(main())
        self.assertTrue(claimed)
        self.assertTrue(writer.closed)
        self.assertEqual(writer.data.count(b"event: note\n"), 3)
        self.assertTrue(writer.data.rstrip().split(b"\n\n")[-1].startswith(
            b"event: sign_in_required"))

    def test_a_cross_origin_stream_is_refused(self) -> None:
        writer = _Writer()
        asyncio.run(conversation_routes.stream_events(
            "GET", f"/api/conversations/{CONVERSATION}/events",
            self.browser.raw(origin=False) + "\r\nOrigin: http://127.0.0.1:9999",
            writer))
        self.assertTrue(writer.data.startswith(b"HTTP/1.1 403 "), writer.data[:80])


# ── the origin rule ──────────────────────────────────────────────────────────

class OriginRule(GateCase):
    def setUp(self) -> None:
        super().setUp()
        self.browser = sign_in(self)

    def assert_foreign(self, status: int, payload: dict) -> None:
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "same_origin_required")

    def test_a_mutation_needs_this_origin(self) -> None:
        body = json.dumps({"description": "x"}).encode()
        for label, change in (
            ("no Origin", lambda h: h.pop("Origin")),
            ("another port", lambda h: h.update(Origin="http://127.0.0.1:9999")),
            ("localhost for 127.0.0.1", lambda h: h.update(Origin="http://localhost:8800")),
            ("null", lambda h: h.update(Origin="null")),
            ("a path", lambda h: h.update(Origin=f"http://{HOST}/")),
            ("cross-site metadata", lambda h: h.update({"Sec-Fetch-Site": "cross-site"})),
            ("same-site metadata", lambda h: h.update({"Sec-Fetch-Site": "same-site"})),
        ):
            for method, path in (("POST", "/api/flags"), ("PATCH", "/api/flags/1"),
                                 ("PUT", "/api/web-search"), ("DELETE", "/api/shells/1"),
                                 ("POST", "/api/conversations"),
                                 ("POST", "/api/scripts/snapshot")):
                with self.subTest(label=label, method=method, path=path):
                    headers = self.browser.headers()
                    change(headers)
                    with mock.patch.object(server, "run_script") as ran:
                        status, _, payload = self.json(method, path, headers, body)
                    self.assert_foreign(status, payload)
                    ran.assert_not_called()

    def test_a_mutation_from_this_origin_passes_the_gate(self) -> None:
        headers = self.browser.headers(**{"Sec-Fetch-Site": "same-origin"})
        with mock.patch.object(server, "run_script",
                               return_value={"ok": True, "output": "done"}) as ran:
            status, _, payload = self.json("POST", "/api/scripts/snapshot", headers)
        self.assertEqual((status, payload["ok"]), (200, True))
        ran.assert_called_once_with("snapshot")

    def test_an_api_read_must_not_be_cross_origin(self) -> None:
        for label, extra in (
            ("another local page", {"Origin": "http://127.0.0.1:9999"}),
            ("same-site subresource", {"Sec-Fetch-Site": "same-site"}),
            ("cross-site subresource", {"Sec-Fetch-Site": "cross-site"}),
        ):
            with self.subTest(label):
                headers = self.browser.headers(origin=False, **extra)
                status, _, payload = self.json("GET", "/api/shells", headers)
                self.assert_foreign(status, payload)
        for label, extra in (
            ("a same-origin fetch", {"Sec-Fetch-Site": "same-origin"}),
            ("typed into the address bar", {"Sec-Fetch-Site": "none"}),
            ("no metadata at all", {}),
        ):
            with self.subTest(label):
                headers = self.browser.headers(origin=False, **extra)
                self.assertEqual(self.call("GET", "/api/shells", headers)[0], 200)

    def test_a_page_or_static_file_needs_only_the_cookie(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "index.html").write_text("<!doctype html><title>app</title>")
            (Path(tmp) / "app.js").write_text("// app\n")
            with mock.patch.object(server, "UI_DIR", Path(tmp)):
                for path in ("/", "/index.html", "/app.js"):
                    headers = self.browser.headers(
                        origin=False, **{"Sec-Fetch-Site": "cross-site"})
                    status, _, _ = self.call("GET", path, headers)
                    self.assertEqual(status, 200, path)


# ── the visitor: 302 or 401, `next`, and the status route ────────────────────

class Visitor(GateCase):
    def setUp(self) -> None:
        super().setUp()
        self.directory = signed_out(self)

    def test_a_page_redirects_and_the_api_never_does(self) -> None:
        for path, location in (
            ("/", "/signin.html"),
            ("/index.html", "/signin.html?next=%2Findex.html"),
            ("/?tab=chats", "/signin.html?next=%2F%3Ftab%3Dchats"),
            ("/some/page", "/signin.html?next=%2Fsome%2Fpage"),
        ):
            with self.subTest(path):
                status, headers, body = self.call("GET", path)
                self.assertEqual(status, 302)
                self.assertEqual(headers["Location"], location)
                self.assertEqual(body, b"")
        for method, path in (("GET", "/api/shells"), ("GET", "/api/"),
                             ("GET", "/api"), ("POST", "/"), ("GET", "/app.js"),
                             ("GET", "/vendor/marked.umd.js"),
                             ("GET", "/api/documents/1/open")):
            with self.subTest(method=method, path=path):
                status, headers, payload = self.json(method, path)
                self.assert_sign_in_required(status, payload)
                self.assertNotIn("Location", headers)
                self.assertEqual(headers["Cache-Control"], "no-store")

    def test_a_refused_head_has_no_body(self) -> None:
        for path in ("/", "/app.js", "/api/shells", "/vendor/marked.umd.js"):
            with self.subTest(path):
                status, headers, body = self.call("HEAD", path)
                self.assertEqual((status, body), (401, b""))
                self.assertNotIn("Location", headers)
                self.assertGreater(int(headers["Content-Length"]), 0)

    def test_a_browser_navigation_is_still_told_by_path_not_by_headers(self) -> None:
        """Headers a client chooses do not turn an API 401 into a redirect."""
        status, headers, payload = self.json("GET", "/api/shells", {
            "Host": HOST, "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate", "Accept": "text/html"})
        self.assert_sign_in_required(status, payload)
        self.assertNotIn("Location", headers)

    def test_the_redirect_cannot_leave_this_origin(self) -> None:
        for target in ("//evil.example/x", "http://evil.example/x",
                       "/\\evil.example", "/%09/evil.example",
                       "//evil.example", "/signin.html/../x?next=//evil.example",
                       "/x?next=https://evil.example"):
            with self.subTest(target):
                status, headers, _ = self.call("GET", target)
                if status == 401:
                    continue
                self.assertEqual(status, 302)
                location = headers["Location"]
                parsed = urlparse(location)
                self.assertEqual((parsed.scheme, parsed.netloc), ("", ""))
                self.assertEqual(parsed.path, "/signin.html")
                wanted = (parse_qs(parsed.query).get("next") or ["/"])[0]
                self.assertEqual(browser_gate.safe_next(wanted), wanted)
                self.assertTrue(wanted.startswith("/") and not wanted.startswith("//"))

    def test_next_is_a_same_origin_relative_path_or_the_root(self) -> None:
        good = ("/", "/index.html", "/?tab=chats", "/a/b?c=d&e=f",
                "/x?next=https://evil.example")
        for value in good:
            self.assertEqual(browser_gate.safe_next(value), value)
        bad = ("", None, 7, "x", "index.html", "//evil.example", "///evil.example",
               "http://evil.example", "https://evil.example/", "javascript:alert(1)",
               "/\\evil.example", "\\\\evil.example", "/\t/evil.example",
               "/\n/evil.example", "/\r", "/\x00", "/a\x7f", " /a",
               "/signin.html", "/signin.html?next=/signin.html",
               "/" + "a" * 2048)
        for value in bad:
            with self.subTest(value=value):
                self.assertEqual(browser_gate.safe_next(value), "/")

    def test_status_validates_next_for_the_page(self) -> None:
        for raw, expected in (
            ("%2Findex.html", "/index.html"),
            ("%2F%3Ftab%3Dchats", "/?tab=chats"),
            ("%2F%2Fevil.example", "/"),
            ("https%3A%2F%2Fevil.example", "/"),
            ("%2F%09%2Fevil.example", "/"),
            ("%2F%5Cevil.example", "/"),
            ("%2Fsignin.html", "/"),
        ):
            with self.subTest(raw):
                status, _, payload = self.json(
                    "GET", f"/api/auth/status?next={raw}")
                self.assertEqual(status, 200)
                self.assertEqual(payload["next"], expected)
        status, _, payload = self.json("GET", "/api/auth/status")
        self.assertEqual(payload["next"], "/")

    def test_status_before_any_user_exists(self) -> None:
        """Existing installs after the update: nothing published, gate closed."""
        status, headers, payload = self.json("GET", "/api/auth/status")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Set-Cookie", headers)
        self.assertEqual(payload, {
            "enrolled": False,
            "signed_in": False,
            "authority": {"url": "http://127.0.0.1:9137", "up": True,
                          "up_source": "probe", "start_cli": "./sc launch"},
            "sign_in": "/signin.html",
            "setup_cli": "./sc auth user add <username>",
            "next": "/",
        })
        # No grace period: not enrolled is not signed in, anywhere.
        self.assertEqual(walk(self), [])
        publish(self.directory, [], enrolled=False)
        status, _, payload = self.json("GET", "/api/auth/status")
        self.assertFalse(payload["enrolled"])
        self.assertEqual(walk(self), [])

    def test_status_when_a_user_exists_and_nobody_is_signed_in(self) -> None:
        publish(self.directory, [], enrolled=True)
        status, _, payload = self.json("GET", "/api/auth/status")
        self.assertEqual((payload["enrolled"], payload["signed_in"]), (True, False))
        self.assertNotIn("user_id", payload)

    def test_the_authority_url_is_for_the_host_the_page_came_from(self) -> None:
        for host, expected in (
            ("127.0.0.1:8800", "http://127.0.0.1:9137"),
            ("localhost:8800", "http://localhost:9137"),
            ("[::1]:8800", "http://127.0.0.1:9137"),
            ("evil.example:8800", "http://127.0.0.1:9137"),
            ("evil.example", "http://127.0.0.1:9137"),
        ):
            with self.subTest(host):
                _, _, payload = self.json("GET", "/api/auth/status", {"Host": host})
                self.assertEqual(payload["authority"]["url"], expected)

    def test_the_authority_not_yet_allocated(self) -> None:
        server.engine_ops.discovery.return_value = {
            "port": None, "url": None, "up": False, "up_source": "unallocated",
            "start_cli": "./sc launch"}
        _, _, payload = self.json("GET", "/api/auth/status")
        self.assertEqual(payload["authority"], {
            "url": None, "up": False, "up_source": "unallocated",
            "start_cli": "./sc launch"})

    def test_the_sign_in_files_are_served_to_a_visitor_and_nothing_else_is(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ui = Path(tmp)
            for name in ("index.html", "app.js", "signin.html", "signin.js",
                         "signin.css", "theme.js", "style.css"):
                (ui / name).write_text(f"/* {name} */")
            (ui / "vendor").mkdir()
            (ui / "vendor" / "qrcode.js").write_text("// qr")
            (ui / "vendor" / "marked.umd.js").write_text("// marked")
            with mock.patch.object(server, "UI_DIR", ui):
                for path in ("/signin.html", "/signin.js", "/signin.css",
                             "/theme.js", "/style.css", "/vendor/qrcode.js"):
                    status, headers, body = self.call("GET", path)
                    self.assertEqual(status, 200, path)
                    self.assertIn(path.rsplit("/", 1)[1].encode(), body + b"qrcode.js")
                    if path == "/signin.html":
                        self.assertEqual(
                            headers["Content-Security-Policy"], server._CSP)
                    self.assertNotIn("Set-Cookie", headers)
                self.assertEqual(self.call("GET", "/index.html")[0], 302)
                self.assertEqual(self.call("GET", "/app.js")[0], 401)
                self.assertEqual(self.call("GET", "/vendor/marked.umd.js")[0], 401)
                # The same file by another spelling is not on the list.
                for path in ("/vendor/qrcode.js/", "/vendor/./qrcode.js",
                             "/vendor/%71rcode.js", "/vendor//qrcode.js",
                             "/vendor/qrcode.js%00", "/VENDOR/qrcode.js"):
                    status, _, body = self.call("GET", path)
                    self.assertIn(status, (401, 302), path)
                    self.assertNotIn(b"// qr", body)

    def test_the_sign_in_page_may_reach_the_authority(self) -> None:
        """The CSP on both pages names the authority's two loopback origins."""
        policy = server._csp(8800, 9137)
        connect = next(part for part in policy.split(";")
                       if part.strip().startswith("connect-src"))
        self.assertIn("http://127.0.0.1:9137", connect.split())
        self.assertIn("http://localhost:9137", connect.split())
        self.assertNotIn("*", connect)
        self.assertIn("script-src 'self'", policy)


class SignedInStatusAndRenewal(GateCase):
    def setUp(self) -> None:
        super().setUp()
        self.browser = sign_in(self)

    def test_status_when_signed_in(self) -> None:
        status, headers, payload = self.json(
            "GET", "/api/auth/status", self.browser.headers(origin=False))
        self.assertEqual(status, 200)
        self.assertEqual((payload["enrolled"], payload["signed_in"],
                          payload["user_id"]), (True, True, 1))
        self.assertEqual(
            headers["Set-Cookie"],
            f"{self.browser.cookie_name}={self.browser.token}; "
            "Max-Age=34560000; Path=/; HttpOnly; SameSite=Strict")

    def test_the_app_shell_renews_the_cookie_and_nothing_else_does(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "index.html").write_text("<!doctype html><title>app</title>")
            (Path(tmp) / "app.js").write_text("// app\n")
            with mock.patch.object(server, "UI_DIR", Path(tmp)):
                expected = auth_sessions.set_cookie(
                    self.browser.cookie_name, self.browser.token)
                for path in ("/", "/index.html"):
                    status, headers, _ = self.call(
                        "GET", path, self.browser.headers(origin=False))
                    self.assertEqual(status, 200)
                    self.assertEqual(headers["Set-Cookie"], expected)
                    etag = headers["ETag"]
                    status, headers, _ = self.call("GET", path, self.browser.headers(
                        origin=False, **{"If-None-Match": etag}))
                    self.assertEqual(status, 304)
                    self.assertEqual(headers["Set-Cookie"], expected)
                status, headers, _ = self.call(
                    "GET", "/app.js", self.browser.headers(origin=False))
                self.assertEqual(status, 200)
                self.assertNotIn("Set-Cookie", headers)
        status, headers, _ = self.call("GET", "/api/shells", self.browser.headers())
        self.assertNotIn("Set-Cookie", headers)

    def test_renewal_writes_nothing(self) -> None:
        before = sorted(p.name for p in self.browser.directory.iterdir())
        stamp = self.browser.path().stat().st_mtime_ns
        self.call("GET", "/api/auth/status", self.browser.headers(origin=False))
        self.assertEqual(sorted(p.name for p in self.browser.directory.iterdir()), before)
        self.assertEqual(self.browser.path().stat().st_mtime_ns, stamp)

    FOREIGN = (
        ("another local page", {"Origin": "http://127.0.0.1:9999",
                                "Sec-Fetch-Site": "same-site"}),
        ("another port, no metadata", {"Origin": "http://127.0.0.1:9999"}),
        ("localhost for 127.0.0.1", {"Origin": "http://localhost:8800"}),
        ("a same-site subresource", {"Sec-Fetch-Site": "same-site"}),
        ("a cross-site subresource", {"Sec-Fetch-Site": "cross-site"}),
        ("an opaque origin", {"Origin": "null"}),
    )

    def test_a_cross_origin_status_call_is_answered_as_a_stranger(self) -> None:
        """Open, so it is answered; but a valid cookie on a request from
        another origin buys nothing: no user, no renewal."""
        _, _, anonymous = self.json("GET", "/api/auth/status", {"Host": HOST})
        self.assertFalse(anonymous["signed_in"])
        for label, extra in self.FOREIGN:
            with self.subTest(label):
                status, headers, payload = self.json(
                    "GET", "/api/auth/status",
                    self.browser.headers(origin=False, **extra))
                self.assertEqual(status, 200)
                self.assertNotIn("Set-Cookie", headers)
                self.assertEqual(payload, anonymous)
                self.assertNotIn("user_id", payload)

    def test_the_guis_own_status_poll_is_unaffected(self) -> None:
        """What the sign-in page does after signing in: same-origin polls."""
        expected = auth_sessions.set_cookie(
            self.browser.cookie_name, self.browser.token)
        for label, extra in (
            ("a same-origin fetch", {"Sec-Fetch-Site": "same-origin"}),
            ("with its own Origin", {"Origin": f"http://{HOST}",
                                     "Sec-Fetch-Site": "same-origin"}),
            ("typed into the address bar", {"Sec-Fetch-Site": "none"}),
            ("no metadata at all", {}),
        ):
            with self.subTest(label):
                status, headers, payload = self.json(
                    "GET", "/api/auth/status",
                    self.browser.headers(origin=False, **extra))
                self.assertEqual((status, payload["signed_in"], payload["user_id"]),
                                 (200, True, 1))
                self.assertEqual(headers["Set-Cookie"], expected)
        # The poll itself: signed out, then the authority publishes, then in.
        self.browser.revoke()
        poll = self.browser.headers(origin=False, **{"Sec-Fetch-Site": "same-origin"})
        self.assertFalse(self.json("GET", "/api/auth/status", poll)[2]["signed_in"])
        self.browser.publish()
        self.assertTrue(self.json("GET", "/api/auth/status", poll)[2]["signed_in"])

    def test_a_cross_origin_page_load_is_served_but_not_renewed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "index.html").write_text("<!doctype html><title>app</title>")
            with mock.patch.object(server, "UI_DIR", Path(tmp)):
                _, own, _ = self.call("GET", "/", self.browser.headers(origin=False))
                etag = own["ETag"]
                self.assertIn("Set-Cookie", own)
                for label, extra in self.FOREIGN:
                    for path in ("/", "/index.html"):
                        with self.subTest(label, path=path):
                            status, headers, _ = self.call(
                                "GET", path,
                                self.browser.headers(origin=False, **extra))
                            self.assertEqual(status, 200)
                            self.assertNotIn("Set-Cookie", headers)
                            status, headers, _ = self.call(
                                "GET", path, self.browser.headers(
                                    origin=False, **extra,
                                    **{"If-None-Match": etag}))
                            self.assertEqual(status, 304)
                            self.assertNotIn("Set-Cookie", headers)

    def test_status_is_the_gates_answer_for_an_inactive_user(self) -> None:
        publish(self.browser.directory, [(self.browser.token, 2)])
        status, headers, payload = self.json(
            "GET", "/api/auth/status", self.browser.headers(origin=False))
        self.assertEqual(status, 200)
        self.assertFalse(payload["signed_in"])
        self.assertNotIn("user_id", payload)
        self.assertNotIn("Set-Cookie", headers)

    def test_a_revoked_cookie_is_not_renewed(self) -> None:
        self.browser.revoke()
        status, headers, payload = self.json(
            "GET", "/api/auth/status", self.browser.headers(origin=False))
        self.assertEqual(status, 200)
        self.assertFalse(payload["signed_in"])
        self.assertNotIn("Set-Cookie", headers)


# ── where the list is read from, in both runtimes ────────────────────────────

class WhereTheListIsRead(GateCase):
    def _resolved(self, env: dict) -> str:
        """`auth_sessions.sessions_dir()` as a fresh process on the review
        server's import path resolves it, with exactly this environment."""
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import auth_sessions; "
                "print(auth_sessions.in_sandbox(), auth_sessions.sessions_dir())")
        result = subprocess.run(
            [sys.executable, "-c", code, str(ENGINE / "api")],
            env={"PATH": os.environ.get("PATH", ""), **env},
            capture_output=True, text=True, timeout=60, check=True)
        return result.stdout.strip()

    def test_in_the_sandbox_it_is_the_read_only_mount(self) -> None:
        self.assertEqual(str(auth_sessions.SANDBOX_DIR),
                         sandbox_devkit.AUTH_SESSIONS_TARGET)
        self.assertEqual(sandbox_devkit.AUTH_SESSIONS_TARGET,
                         "/run/super-coder/auth-sessions")
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(
                self._resolved({"SC_SANDBOX": "1", "HOME": home,
                                "XDG_STATE_HOME": str(Path(home) / "state")}),
                "True /run/super-coder/auth-sessions")

    @unittest.skipIf(any(os.path.exists(m) for m in ("/.dockerenv", "/run/.containerenv")),
                     "this test process is itself in a container")
    def test_on_the_host_it_is_the_derived_directory(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            state = Path(home) / "state"
            resolved = self._resolved({"HOME": home, "XDG_STATE_HOME": str(state)})
            with mock.patch.dict(os.environ, {"HOME": home, "XDG_STATE_HOME": str(state)}):
                expected = sandbox_devkit.auth_sessions_dir(REPO)
            self.assertEqual(resolved, f"False {expected}")
            self.assertTrue(str(expected).startswith(str(state)))
            self.assertNotEqual(expected, auth_sessions.SANDBOX_DIR)

    def test_the_review_server_reads_the_mount_when_sandboxed(self) -> None:
        """Through the gate: a session published at the in-sandbox path signs
        a request in, and one published at the host path does not."""
        browser = sign_in(self)
        mount = browser.directory
        with tempfile.TemporaryDirectory() as host_dir, \
                mock.patch.object(auth_sessions, "DIR", None), \
                mock.patch.object(auth_sessions, "SANDBOX_DIR", mount), \
                mock.patch.object(sandbox_devkit, "auth_sessions_dir",
                                  return_value=Path(host_dir)):
            with mock.patch.dict(os.environ, {"SC_SANDBOX": "1"}):
                self.assertEqual(auth_sessions.sessions_dir(), mount)
                self.assertEqual(
                    self.call("GET", "/api/shells", browser.headers())[0], 200)
            with mock.patch.object(auth_sessions, "in_sandbox", return_value=False):
                self.assertEqual(auth_sessions.sessions_dir(), Path(host_dir))
                self.assertEqual(
                    self.call("GET", "/api/shells", browser.headers())[0], 401)
                publish(Path(host_dir), [(browser.token, 1)])
                self.assertEqual(
                    self.call("GET", "/api/shells", browser.headers())[0], 200)

    def test_the_gate_cannot_write(self) -> None:
        """It imports nothing that touches a file, and opens none itself."""
        tree = ast.parse((ENGINE / "api" / "browser_gate.py").read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)
        self.assertEqual(imported, {"__future__", "hmac", "json", "typing",
                                    "urllib.parse", "auth_sessions"})
        called = {node.func.id for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        self.assertFalse(called & {"open", "exec", "eval", "__import__"})


# ── Acceptance 4: shell and CLI paths ────────────────────────────────────────

class ShellAndCliCredentials(GateCase):
    def setUp(self) -> None:
        super().setUp()
        signed_out(self)
        self.run_dir = Path(tempfile.mkdtemp(prefix="sc-cli-token-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.run_dir, True))
        self.token = cli_credential.provision(CLI_PORT, self.run_dir)
        patcher = mock.patch.object(browser_gate, "CLI_TOKEN", self.token)
        patcher.start()
        self.addCleanup(patcher.stop)

    def bearer(self, token: str) -> dict:
        return {"Host": HOST, "Authorization": f"Bearer {token}"}

    def test_a_shell_with_no_credential_gets_401(self) -> None:
        for method, path in (("GET", "/api/models"), ("GET", "/api/models?refresh=1"),
                             ("POST", "/api/scripts/snapshot"), ("GET", "/api/shells"),
                             ("POST", "/api/flags"), ("POST", "/api/snapshot")):
            with self.subTest(method=method, path=path):
                with mock.patch.object(server, "run_script") as ran, \
                        mock.patch.object(server.model_catalog, "catalog") as catalog:
                    status, _, payload = self.json(method, path)
                self.assert_sign_in_required(status, payload)
                ran.assert_not_called()
                catalog.assert_not_called()

    def test_a_credential_nobody_issued_gets_401(self) -> None:
        for token in ("nope", "sccli_" + "A" * 43, self.token[:-1], self.token + "x", ""):
            for method, path in (("POST", "/api/scripts/snapshot"),
                                 ("GET", "/api/models"), ("GET", "/api/shells")):
                with self.subTest(token=token, path=path):
                    with mock.patch.object(server, "run_script") as ran:
                        status, _, payload = self.json(method, path, self.bearer(token))
                    self.assert_sign_in_required(status, payload)
                    ran.assert_not_called()
        status, _, payload = self.json("GET", "/api/models", {
            "Host": HOST, "Authorization": f"Basic {SHELL_TOKEN}"})
        self.assert_sign_in_required(status, payload)

    def test_the_cli_routes_take_either_credential(self) -> None:
        for token in (self.token, SHELL_TOKEN):
            with self.subTest(token=token[:6]):
                with mock.patch.object(server, "run_script", return_value={
                        "ok": True, "output": "snapshot: wrote"}) as ran:
                    status, _, payload = self.json(
                        "POST", "/api/scripts/snapshot", self.bearer(token))
                self.assertEqual((status, payload["ok"]), (200, True))
                ran.assert_called_once_with("snapshot")
                with mock.patch.object(server.model_catalog, "catalog", return_value={
                        "harnesses": {}, "sources": ["test"]}) as catalog:
                    status, _, payload = self.json(
                        "GET", "/api/models?refresh=1", self.bearer(token))
                self.assertEqual(status, 200)
                self.assertTrue(catalog.call_args.kwargs["refresh"])

    def test_a_credential_does_not_make_its_holder_the_operator(self) -> None:
        """Operator routes, engine and user management first, refuse both."""
        operator_routes = (
            ("GET", "/api/engine-ops"), ("GET", "/api/shells"),
            ("POST", "/api/shells"), ("DELETE", "/api/shells/1"),
            ("PATCH", "/api/shells/1"), ("PUT", "/api/shells/1/skills/1"),
            ("POST", "/api/scripts/update"), ("POST", "/api/scripts/snapshot/x"),
            ("GET", "/api/scripts"), ("POST", "/api/snapshot"),
            ("POST", "/api/publish"), ("POST", "/api/services/engine-ops/up"),
            ("GET", "/api/sprints"), ("GET", "/api/browser"),
            ("POST", "/api/browser"), ("PUT", "/api/web-search"),
            ("GET", "/api/conversations"), ("POST", "/api/conversations"),
            ("GET", "/api/diagnostics/request-timing"),
            ("GET", "/api/runs"), ("POST", "/api/models"),
            ("GET", "/api/models/x"), ("GET", "/"), ("GET", "/app.js"),
            # The verb's requests, and nothing near them (Codex pass 1, 5).
            ("GET", "/api/models"), ("GET", "/api/models?"),
            ("GET", "/api/models?refresh=0"), ("GET", "/api/models?refresh=true"),
            ("GET", "/api/models?refresh=1&x=1"), ("GET", "/api/models?x=1&refresh=1"),
            ("GET", "/api/models?refresh=1&refresh=1"),
            ("GET", "/api/models?refresh=1&refresh=0"),
            ("GET", "/api/models?refresh=%31"), ("GET", "/api/models?REFRESH=1"),
            ("GET", "/api/models;x?refresh=1"), ("GET", "/api/models?refresh=1#x"),
            ("HEAD", "/api/models?refresh=1"),
            ("POST", "/api/scripts/snapshot?x=1"),
            ("POST", "/api/scripts/snapshot;x"), ("GET", "/api/scripts/snapshot"),
            ("PUT", "/api/scripts/snapshot"),
        )
        for token in (self.token, SHELL_TOKEN):
            for method, path in operator_routes:
                with self.subTest(token=token[:6], method=method, path=path):
                    with mock.patch.object(server, "run_script") as ran, \
                            mock.patch.object(server.model_catalog, "catalog") as catalog:
                        status, headers, out = self.call(
                            method, path, self.bearer(token))
                    self.assertEqual(status, 403)
                    if method != "HEAD":
                        self.assertEqual(json.loads(out)["error"]["code"],
                                         "fnb_operator_required")
                    self.assertNotIn("Set-Cookie", headers)
                    ran.assert_not_called()
                    catalog.assert_not_called()

    def test_the_authority_is_not_on_the_review_server(self) -> None:
        """No sign-in or user-management route exists here to be reached."""
        for method, path in (("POST", "/auth/sign-in"), ("POST", "/api/auth/sign-in"),
                             ("GET", "/auth/sessions"), ("POST", "/ops/update"),
                             ("POST", "/api/auth/status")):
            for headers in ({"Host": HOST}, self.bearer(SHELL_TOKEN),
                            self.bearer(self.token)):
                status, _, _ = self.call(method, path, headers)
                self.assertIn(status, (401, 403, 302), (method, path))

    def test_sc_routes_keep_their_bearer_rules(self) -> None:
        status, _, payload = self.json("GET", "/_sc/mem/whoami", self.bearer(SHELL_TOKEN))
        self.assertEqual(status, 200, payload)
        status, _, _ = self.json("GET", "/_sc/mem/whoami")
        self.assertEqual(status, 401)
        # The engine CLI token is not a shell.
        status, _, _ = self.json("GET", "/_sc/mem/whoami", self.bearer(self.token))
        self.assertEqual(status, 401)
        # Nor is a browser session.
        browser = sign_in(self)
        status, _, _ = self.json("GET", "/_sc/mem/whoami", browser.headers())
        self.assertEqual(status, 401)

    def test_the_token_is_this_boots_and_the_file_grants_nothing(self) -> None:
        old = self.token
        new = cli_credential.provision(CLI_PORT, self.run_dir)
        self.assertNotEqual(old, new)
        self.assertEqual(cli_credential.read(CLI_PORT, self.run_dir), new)
        # The server still holds `old` in memory: what is on disk is not believed.
        with mock.patch.object(server, "run_script") as ran:
            status, _, _ = self.json("POST", "/api/scripts/snapshot", self.bearer(new))
        self.assertEqual(status, 401)
        ran.assert_not_called()
        with mock.patch.object(browser_gate, "CLI_TOKEN", None), \
                mock.patch.object(server, "run_script") as ran:
            status, _, _ = self.json("POST", "/api/scripts/snapshot", self.bearer(old))
        self.assertEqual(status, 401)
        ran.assert_not_called()

    def test_the_published_token_is_owner_only_and_checked_on_read(self) -> None:
        path = cli_credential.path_for(CLI_PORT, self.run_dir)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.run_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(cli_credential.read(CLI_PORT, self.run_dir), self.token)
        self.assertEqual(cli_credential.authorization(CLI_PORT, self.run_dir),
                         {"Authorization": f"Bearer {self.token}"})
        path.chmod(0o640)
        self.assertIsNone(cli_credential.read(CLI_PORT, self.run_dir))
        path.chmod(0o600)
        real = path.with_name("real")
        path.rename(real)
        path.symlink_to(real)
        self.assertIsNone(cli_credential.read(CLI_PORT, self.run_dir))
        self.assertEqual(cli_credential.authorization(CLI_PORT, self.run_dir), {})
        # A boot replaces a planted link; it does not write through it.
        again = cli_credential.provision(CLI_PORT, self.run_dir)
        self.assertFalse(path.is_symlink())
        self.assertEqual(real.read_text(), self.token)
        self.assertEqual(cli_credential.read(CLI_PORT, self.run_dir), again)
        path.unlink()
        self.assertEqual(cli_credential.authorization(CLI_PORT, self.run_dir), {})

    def test_the_token_is_minted_before_the_bind_and_published_after(self) -> None:
        tree = ast.parse((ENGINE / "api" / "server.py").read_text())
        boot = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == "_run_server")
        calls = [ast.unparse(node.func) for node in ast.walk(boot)
                 if isinstance(node, ast.Call)]
        self.assertIn("cli_credential.mint", calls)
        for writer in ("cli_credential.provision", "cli_credential.publish",
                       "publish_cli_credential"):
            self.assertNotIn(
                writer, calls,
                "the boot path writes the credential file before the bind")
        self.assertEqual(cli_credential.RUN_DIR, ENGINE / "run" / "api")
        # `_on_listening` is the transport's on_started: it publishes, then
        # starts the runtime services.
        with mock.patch.object(cli_credential, "RUN_DIR", self.run_dir), \
                mock.patch.object(browser_gate, "CLI_TOKEN", "sccli_this-process"), \
                mock.patch.object(server, "_LISTEN_PORTS", (8811,)), \
                mock.patch.object(server, "start_runtime_services") as services:
            server._on_listening()
        services.assert_called_once_with()
        self.assertEqual(cli_credential.read(8811, self.run_dir), "sccli_this-process")
        # Under its own port, and no other file was touched.
        self.assertEqual(cli_credential.read(CLI_PORT, self.run_dir), self.token)
        # The boot path does not decide the port the file is named for: the
        # transport reports it, after the bind.
        self.assertIn("_LISTEN_PORTS = ()", ast.unparse(boot))
        self.assertNotIn("port,)", ast.unparse(boot))
        with mock.patch.object(server, "_LISTEN_PORTS", ()):
            server._on_bound([8812, 8813])
            self.assertEqual(server._LISTEN_PORTS, (8812, 8813))

    def _boot(self, port: int, publish_patch=None, bind: str = "127.0.0.1"):
        """Run the real `_run_server(port)` on the real transport, in a
        thread, with everything outside the listener stubbed. Returns
        (bound port or None, what `_run_server` raised or None, stop)."""
        started = threading.Event()
        state: dict = {}

        real_start = transport_mod.Transport.start

        async def recording_start(transport):
            # What the listener REALLY holds, read off its sockets, so the
            # test does not take the server's word for it.
            await real_start(transport)
            state["bound"] = sorted(
                {sock.getsockname()[1] for sock in transport._tcp.sockets})
            state["sockets"] = [sock.getsockname()[:2]
                                for sock in transport._tcp.sockets]

        def services():
            state["loop"] = asyncio.get_running_loop()
            state["port"] = state["bound"][0]
            started.set()

        def run():
            try:
                server._run_server(port)
            except BaseException as exc:  # noqa: BLE001 — reported to the test
                state["raised"] = exc
            finally:
                started.set()

        patches = [
            mock.patch.object(cli_credential, "RUN_DIR", self.run_dir),
            mock.patch.object(server, "DB_PATH", _DB_PATH),
            mock.patch.object(server, "_CSP", server._CSP),
            mock.patch.object(server, "_LISTEN_PORTS", (), create=True),
            mock.patch.object(transport_mod.Transport, "start", recording_start),
            mock.patch.object(server, "require_current_schema"),
            mock.patch.object(server.backfill_shell_api_keys, "backfill"),
            mock.patch.object(server.mem_credentials, "provision"),
            mock.patch.object(server.ports_mod, "stored_engine_ops_port",
                              return_value=None),
            mock.patch.object(server, "in_container", return_value=False),
            mock.patch.object(server, "start_runtime_services", services),
            mock.patch.object(server.conversation_reaper, "stop_service"),
            mock.patch.dict(os.environ, {"SC_BIND": bind}),
            mock.patch("builtins.print", lambda *a, **k: state.setdefault(
                "printed", []).append(" ".join(str(x) for x in a))),
        ]
        if publish_patch is not None:
            patches.append(publish_patch)
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.assertTrue(started.wait(20), "the server neither started nor failed")

        def stop():
            loop = state.get("loop")
            if loop is not None and thread.is_alive():
                loop.call_soon_threadsafe(
                    lambda: [task.cancel() for task in asyncio.all_tasks(loop)])
            thread.join(timeout=10)

        self.addCleanup(stop)
        return state, stop

    def test_port_zero_publishes_under_the_port_the_socket_holds(self) -> None:
        """Codex pass 3: `--port 0` binds an ephemeral port. The credential is
        named for THAT port, the server starts, and its caller is admitted."""
        state, stop = self._boot(0)
        self.assertNotIn(
            "raised", state,
            f"startup aborted on port 0: {state.get('raised')!r}")
        port = state["port"]
        self.assertIsInstance(port, int)
        self.assertNotEqual(port, 0)
        token = browser_gate.CLI_TOKEN
        self.assertEqual(
            sorted(p.name for p in self.run_dir.iterdir()),
            sorted([f"cli-token.{CLI_PORT}", f"cli-token.{port}"]))
        self.assertEqual(cli_credential.read(port, self.run_dir), token)
        self.assertEqual(
            cli_credential.path_for(port, self.run_dir).stat().st_mode & 0o777, 0o600)
        # The listener is up on that port, and the caller for it is admitted.
        con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            con.request("GET", "/api/health")
            self.assertEqual(con.getresponse().status, 200)
        finally:
            con.close()
        with mock.patch.object(snapshot_cli.ports, "resolve",
                               return_value={"port": port}), \
                mock.patch.object(snapshot_cli, "_owns_this_checkout",
                                  return_value=True), \
                mock.patch.object(server, "run_script", return_value={
                    "ok": True, "output": "snapshot: wrote"}) as ran:
            self.assertEqual(snapshot_cli._snapshot_via_runtime_api(),
                             "snapshot: wrote")
        ran.assert_called_once_with("snapshot")
        stop()

    def _dual_stack(self):
        """Boot on `localhost`, port 0. Skips unless that gave this host an
        IPv4 and an IPv6 socket on two different ports."""
        state, stop = self._boot(0, bind="localhost")
        self.assertNotIn("raised", state, repr(state.get("raised")))
        if len(state["bound"]) < 2:
            stop()
            self.skipTest(
                "localhost with port 0 did not bind two ports here "
                f"(sockets: {state['sockets']}): no IPv6 loopback")
        return state, stop

    def _post_snapshot(self, host: str, port: int):
        """`POST /api/scripts/snapshot` as the CLI caller for `port` does."""
        con = http.client.HTTPConnection(host, port, timeout=10)
        try:
            con.request("POST", "/api/scripts/snapshot", body=b"", headers=(
                cli_credential.authorization(port, self.run_dir)))
            response = con.getresponse()
            return response.status, response.read()
        finally:
            con.close()

    def test_every_bound_port_gets_the_credential(self) -> None:
        """Codex pass 4: `SC_BIND=localhost --port 0` binds IPv4 and IPv6 on
        different ephemeral ports. Both are published; both callers are
        admitted."""
        state, stop = self._dual_stack()
        token = browser_gate.CLI_TOKEN
        hosts = {port: host for host, port in state["sockets"]}
        self.assertEqual(len(hosts), 2, state["sockets"])
        for port in state["bound"]:
            self.assertEqual(
                cli_credential.read(port, self.run_dir), token,
                f"no credential published for bound port {port} "
                f"(sockets: {state['sockets']})")
        self.assertEqual(
            sorted(p.name for p in self.run_dir.iterdir()),
            sorted([f"cli-token.{CLI_PORT}"]
                   + [f"cli-token.{port}" for port in state["bound"]]))
        with mock.patch.object(server, "run_script", return_value={
                "ok": True, "output": "snapshot: wrote"}) as ran:
            for port, host in hosts.items():
                status, body = self._post_snapshot(host, port)
                self.assertEqual(
                    status, 200,
                    f"the caller for {host} port {port} was refused: {body!r}")
        self.assertEqual(ran.call_count, 2)
        stop()

    def test_one_ports_failure_does_not_cost_the_other_its_credential(self) -> None:
        real = cli_credential.publish
        failed: list[int] = []

        def first_fails(token, port, run_dir=None):
            if not failed:
                failed.append(port)
                raise OSError("disk full")
            return real(token, port, run_dir)

        state, stop = self._boot(0, mock.patch.object(
            cli_credential, "publish", side_effect=first_fails), bind="localhost")
        self.assertNotIn("raised", state, repr(state.get("raised")))
        if len(state["bound"]) < 2:
            stop()
            self.skipTest("localhost with port 0 did not bind two ports here")
        lost, kept = failed[0], next(p for p in state["bound"] if p != failed[0])
        self.assertIsNone(cli_credential.read(lost, self.run_dir))
        self.assertEqual(cli_credential.read(kept, self.run_dir),
                         browser_gate.CLI_TOKEN)
        loud = [line for line in state.get("printed", [])
                if "could NOT publish the engine CLI credential" in line]
        self.assertEqual(len(loud), 1, state.get("printed"))
        self.assertIn(f"port {lost}", loud[0])
        self.assertIn("OSError: disk full", loud[0])
        hosts = {port: host for host, port in state["sockets"]}
        with mock.patch.object(server, "run_script", return_value={
                "ok": True, "output": "snapshot: wrote"}):
            self.assertEqual(self._post_snapshot(hosts[kept], kept)[0], 200)
            self.assertEqual(self._post_snapshot(hosts[lost], lost)[0], 401)
        stop()

    def test_a_fixed_port_on_both_families_is_one_file(self) -> None:
        """The ordinary dual-stack case: one port number, published once."""
        probe = __import__("socket").socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        state, stop = self._boot(port, bind="localhost")
        if "raised" in state:
            self.skipTest(f"could not bind localhost:{port}: {state['raised']!r}")
        self.assertEqual(state["bound"], [port])
        self.assertEqual(server._LISTEN_PORTS, (port,))
        self.assertEqual(
            sorted(p.name for p in self.run_dir.iterdir()),
            sorted([f"cli-token.{CLI_PORT}", f"cli-token.{port}"]))
        stop()

    def test_the_transport_reports_every_distinct_port(self) -> None:
        async def main():
            seen = []

            async def no_ws(reader, writer, head_raw):  # pragma: no cover
                writer.close()

            task = asyncio.ensure_future(transport_mod.serve(
                "localhost", 0, server.dispatch_http, no_ws,
                log=lambda *_: None, on_bound=seen.append))
            for _ in range(200):
                if seen:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return seen

        seen = asyncio.run(main())
        self.assertEqual(len(seen), 1)
        ports = seen[0]
        self.assertIsInstance(ports, tuple)
        self.assertEqual(list(ports), sorted(set(ports)))
        self.assertTrue(all(type(port) is int and port > 0 for port in ports))

    def test_a_publication_that_fails_is_loud_and_the_server_runs(self) -> None:
        """Any failure, not only OSError: the listener and the runtime
        services start, the failure is printed, and the caller is told that
        no credential is published for the port."""
        for failure in (ValueError("not a TCP port"), OSError("read-only"),
                        RuntimeError("anything")):
            with self.subTest(failure=type(failure).__name__):
                state, stop = self._boot(0, mock.patch.object(
                    cli_credential, "publish", side_effect=failure))
                self.assertNotIn("raised", state, repr(state.get("raised")))
                port = state["port"]
                self.assertNotEqual(port, 0)
                self.assertIsNone(cli_credential.read(port, self.run_dir))
                loud = [line for line in state.get("printed", [])
                        if "could NOT publish the engine CLI credential" in line]
                self.assertEqual(len(loud), 1, state.get("printed"))
                self.assertIn(f"port {port}", loud[0])
                self.assertIn(type(failure).__name__, loud[0])
                con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                try:
                    con.request("GET", "/api/health")
                    self.assertEqual(con.getresponse().status, 200)
                finally:
                    con.close()
                with mock.patch.object(snapshot_cli.ports, "resolve",
                                       return_value={"port": port}), \
                        mock.patch.object(snapshot_cli, "_owns_this_checkout",
                                          return_value=True), \
                        mock.patch.object(server, "run_script") as ran, \
                        self.assertRaises(SystemExit) as raised:
                    snapshot_cli._snapshot_via_runtime_api()
                ran.assert_not_called()
                self.assertIn("none is published for that port",
                              str(raised.exception))
                stop()

    def test_no_bound_port_reported_is_loud_not_fatal(self) -> None:
        printed: list[str] = []
        with mock.patch.object(cli_credential, "RUN_DIR", self.run_dir), \
                mock.patch.object(server, "_LISTEN_PORTS", ()), \
                mock.patch.object(server, "start_runtime_services") as services, \
                mock.patch("builtins.print",
                           lambda *a, **k: printed.append(" ".join(map(str, a)))):
            server._on_listening()
        services.assert_called_once_with()
        self.assertEqual(len(printed), 1)
        self.assertIn("did not report the bound port", printed[0])

    def test_a_startup_that_loses_the_bind_leaves_the_survivors_token(self) -> None:
        """Two servers, one port. The loser never publishes, so a CLI caller
        still reads the token the listening server accepts."""
        holder = __import__("socket").socket()
        self.addCleanup(holder.close)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        survivor = cli_credential.provision(port, self.run_dir)

        async def no_ws(reader, writer, head_raw):  # pragma: no cover
            writer.close()

        with mock.patch.object(cli_credential, "RUN_DIR", self.run_dir), \
                mock.patch.object(browser_gate, "CLI_TOKEN", cli_credential.mint()), \
                mock.patch.object(server, "_LISTEN_PORTS", (port,)), \
                mock.patch.object(server, "start_runtime_services") as services:
            loser = browser_gate.CLI_TOKEN
            with self.assertRaises(OSError):
                asyncio.run(transport_mod.serve(
                    "127.0.0.1", port, server.dispatch_http, no_ws,
                    log=lambda *_: None, on_started=server._on_listening,
                    stream_handler=conversation_routes.stream_events))
        services.assert_not_called()
        self.assertNotEqual(loser, survivor)
        self.assertEqual(cli_credential.read(port, self.run_dir), survivor)

    def test_a_port_is_a_port(self) -> None:
        for bad in (0, -1, 65536, "8800", 8800.0, True, None, "../x"):
            with self.subTest(port=bad), self.assertRaises(ValueError):
                cli_credential.path_for(bad, self.run_dir)
        self.assertEqual(cli_credential.path_for(8800, self.run_dir),
                         self.run_dir / "cli-token.8800")
        self.assertEqual(cli_credential.path_for(8800),
                         ENGINE / "run" / "api" / "cli-token.8800")

    def test_nothing_removes_the_credential_file(self) -> None:
        for name in ("api/server.py", "scripts/cli_credential.py"):
            tree = ast.parse((ENGINE / name).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and ast.unparse(node.func).endswith(
                        ("unlink", "remove")):
                    self.assertNotIn("FILE_NAME", ast.unparse(node), name)
                    self.assertNotIn("cli", ast.unparse(node).lower().replace(
                        "os.unlink(tmp)", ""), name)


class _Listener:
    """One more real listener on a free loopback port, stopped with the test."""

    def __init__(self, case) -> None:
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()

        async def no_ws(reader, writer, head_raw):  # pragma: no cover
            writer.close()

        def run():
            asyncio.set_event_loop(self.loop)
            self.transport = transport_mod.Transport(
                "127.0.0.1", 0, server.dispatch_http, no_ws, log=lambda *_: None,
                stream_handler=conversation_routes.stream_events)
            self.loop.run_until_complete(self.transport.start())
            self.port = self.transport.port
            ready.set()
            self.loop.run_forever()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        case.assertTrue(ready.wait(10), "second transport did not start")
        case.addCleanup(self.stop)

    def stop(self) -> None:
        asyncio.run_coroutine_threadsafe(
            self.transport.stop(), self.loop).result(timeout=10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=10)
        self.loop.close()


class AuditedCliCallers(GateCase):
    """Each `sc` caller of `/api/*`, against a real listener on a free port."""

    def setUp(self) -> None:
        super().setUp()
        signed_out(self)
        self.run_dir = Path(tempfile.mkdtemp(prefix="sc-cli-token-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.run_dir, True))
        self.token = cli_credential.mint()
        for target, name, value in (
            (browser_gate, "CLI_TOKEN", self.token),
            (cli_credential, "RUN_DIR", self.run_dir),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.seen: list[tuple[str, str, str | None]] = []
        real = server.dispatch_http

        def recording(method, path, headers_raw, body):
            headers = conversation_routes._parse_headers(headers_raw)
            self.seen.append((method, path, headers.get("Authorization")))
            return real(method, path, headers_raw, body)

        self.loop = asyncio.new_event_loop()
        ready = threading.Event()

        async def no_ws(reader, writer, head_raw):  # pragma: no cover
            writer.close()

        def run():
            asyncio.set_event_loop(self.loop)
            self.transport = transport_mod.Transport(
                "127.0.0.1", 0, recording, no_ws, log=lambda *_: None,
                stream_handler=conversation_routes.stream_events)
            self.loop.run_until_complete(self.transport.start())
            self.port = self.transport.port
            ready.set()
            self.loop.run_forever()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        self.assertTrue(ready.wait(10), "transport did not start")
        self.addCleanup(self._stop)
        self.base = f"http://127.0.0.1:{self.port}"
        # As `_on_listening` does: published once the port is held, under it.
        with mock.patch.object(server, "_LISTEN_PORTS", (self.port,)), \
                mock.patch.object(server, "start_runtime_services"):
            server._on_listening()

    def _stop(self) -> None:
        asyncio.run_coroutine_threadsafe(
            self.transport.stop(), self.loop).result(timeout=10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=10)
        self.loop.close()

    def http(self, method: str, path: str, headers: dict | None = None):
        con = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            con.request(method, path, headers=headers or {})
            response = con.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            con.close()

    def test_health_needs_nothing(self) -> None:
        """`./sc launch`, `./sc update`, `./sc health`, `./sc snapshot`: curl
        or urlopen of `/api/health`, with no credential."""
        status, _, body = self.http("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(snapshot_cli._runtime_health(self.base)["ok"], True)

    def test_sc_snapshot_presents_the_engine_cli_credential(self) -> None:
        with mock.patch.object(snapshot_cli.ports, "resolve",
                               return_value={"port": self.port}), \
                mock.patch.object(snapshot_cli, "_owns_this_checkout",
                                  return_value=True), \
                mock.patch.object(server, "run_script", return_value={
                    "ok": True, "output": "snapshot: wrote content.sql"}) as ran, \
                mock.patch.dict(os.environ, {"SC_API_TOKEN": "someone-elses",
                                             "SC_API_BASE": "http://127.0.0.1:1"}):
            output = snapshot_cli._snapshot_via_runtime_api()
        self.assertEqual(output, "snapshot: wrote content.sql")
        ran.assert_called_once_with("snapshot")
        self.assertIn(("POST", "/api/scripts/snapshot", f"Bearer {self.token}"),
                      self.seen)

    def test_sc_snapshot_without_a_credential_is_refused_and_says_why(self) -> None:
        """No file for the port being called: one clear message, which says
        what that means. Another port's file is not borrowed."""
        cli_credential.path_for(self.port, self.run_dir).unlink()
        cli_credential.publish(self.token, self.port + 1 if self.port < 65535
                               else self.port - 1, self.run_dir)
        with mock.patch.object(snapshot_cli.ports, "resolve",
                               return_value={"port": self.port}), \
                mock.patch.object(snapshot_cli, "_owns_this_checkout",
                                  return_value=True), \
                mock.patch.object(server, "run_script") as ran:
            with self.assertRaises(SystemExit) as raised:
                snapshot_cli._snapshot_via_runtime_api()
        ran.assert_not_called()
        message = str(raised.exception)
        self.assertEqual(message, "snapshot: " + cli_credential.refused(
            self.port, 401, self.run_dir))
        self.assertIn(f"none is published for that port at "
                      f"{self.run_dir / ('cli-token.' + str(self.port))}", message)
        self.assertIn("no server of this checkout is running on that port, or "
                      "the server answering there is not this checkout's", message)
        self.assertIn(("POST", "/api/scripts/snapshot", None), self.seen)

    def test_a_stale_file_for_the_port_is_named_as_such(self) -> None:
        cli_credential.publish(cli_credential.mint(), self.port, self.run_dir)
        with mock.patch.object(snapshot_cli.ports, "resolve",
                               return_value={"port": self.port}), \
                mock.patch.object(snapshot_cli, "_owns_this_checkout",
                                  return_value=True), \
                mock.patch.object(server, "run_script") as ran:
            with self.assertRaises(SystemExit) as raised:
                snapshot_cli._snapshot_via_runtime_api()
        ran.assert_not_called()
        self.assertIn("refused the engine CLI credential published at",
                      str(raised.exception))
        self.assertIn("no longer the one listening there", str(raised.exception))

    def test_two_servers_of_one_checkout_each_keep_their_caller(self) -> None:
        """Codex pass 2: a second server for the same checkout, on another
        port, publishes without taking the first server's credential away."""
        first_port, first_token = self.port, self.token
        second = _Listener(self)                       # a second real listener
        second_token = cli_credential.mint()
        with mock.patch.object(browser_gate, "CLI_TOKEN", second_token), \
                mock.patch.object(server, "_LISTEN_PORTS", (second.port,)), \
                mock.patch.object(server, "start_runtime_services"):
            server._on_listening()
        self.assertNotEqual(first_port, second.port)
        self.assertEqual(sorted(p.name for p in self.run_dir.iterdir()),
                         sorted([f"cli-token.{first_port}",
                                 f"cli-token.{second.port}"]))
        self.assertEqual(cli_credential.read(first_port, self.run_dir), first_token)
        self.assertEqual(cli_credential.read(second.port, self.run_dir), second_token)

        def snapshot(port, token):
            # Each "server" holds its own token in memory, as two processes do.
            with mock.patch.object(browser_gate, "CLI_TOKEN", token), \
                    mock.patch.object(snapshot_cli.ports, "resolve",
                                      return_value={"port": port}), \
                    mock.patch.object(snapshot_cli, "_owns_this_checkout",
                                      return_value=True), \
                    mock.patch.object(server, "run_script", return_value={
                        "ok": True, "output": f"snapshot: wrote on {port}"}):
                return snapshot_cli._snapshot_via_runtime_api()

        self.assertEqual(snapshot(first_port, first_token),
                         f"snapshot: wrote on {first_port}")
        self.assertEqual(snapshot(second.port, second_token),
                         f"snapshot: wrote on {second.port}")
        # And in the other order, after the second server restarts (a new
        # token under its own port): the first caller is still served.
        third_token = cli_credential.mint()
        with mock.patch.object(browser_gate, "CLI_TOKEN", third_token), \
                mock.patch.object(server, "_LISTEN_PORTS", (second.port,)), \
                mock.patch.object(server, "start_runtime_services"):
            server._on_listening()
        self.assertEqual(snapshot(first_port, first_token),
                         f"snapshot: wrote on {first_port}")
        self.assertEqual(snapshot(second.port, third_token),
                         f"snapshot: wrote on {second.port}")
        # One server's token is not the other's.
        status, _, _ = self.http("POST", "/api/scripts/snapshot",
                                 {"Authorization": f"Bearer {third_token}"})
        self.assertEqual(status, 401)

    def test_sc_models_refresh_presents_the_shells_own_token(self) -> None:
        out = io.StringIO()
        with mock.patch.object(mem, "SC_API_TOKEN", SHELL_TOKEN), \
                mock.patch.object(mem, "SC_API_BASE", self.base), \
                mock.patch.object(server.model_catalog, "catalog", return_value={
                    "harnesses": {}, "sources": ["fixture"], "stale": False}) as catalog, \
                redirect_stdout(out):
            code = models_cli.main(["refresh"])
        self.assertEqual(code, 0)
        self.assertIn("refreshed from fixture", out.getvalue())
        self.assertTrue(catalog.call_args.kwargs["refresh"])
        self.assertIn(("GET", "/api/models?refresh=1", f"Bearer {SHELL_TOKEN}"),
                      self.seen)

    def test_sc_models_refresh_with_a_dead_shell_token_is_refused(self) -> None:
        with mock.patch.object(mem, "SC_API_TOKEN", "retired-shell"), \
                mock.patch.object(mem, "SC_API_BASE", self.base), \
                mock.patch.object(server.model_catalog, "catalog") as catalog, \
                mock.patch.object(mem.sys, "stderr", io.StringIO()), \
                self.assertRaises(SystemExit):
            models_cli.main(["refresh"])
        catalog.assert_not_called()

    def test_a_shell_over_the_socket(self) -> None:
        status, _, body = self.http("GET", "/api/models?refresh=1")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"]["code"], "sign_in_required")
        status, _, body = self.http(
            "GET", "/api/engine-ops", {"Authorization": f"Bearer {SHELL_TOKEN}"})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "fnb_operator_required")
        status, _, body = self.http(
            "GET", "/_sc/mem/whoami", {"Authorization": f"Bearer {SHELL_TOKEN}"})
        self.assertEqual(status, 200)

    def test_a_browser_over_the_socket_and_its_stream(self) -> None:
        browser = sign_in(self, host=f"127.0.0.1:{self.port}")
        status, _, _ = self.http("GET", "/api/shells", browser.headers())
        self.assertEqual(status, 200)
        status, _, body = self.http("GET", "/")
        self.assertEqual(status, 302)
        # The stream entry, over the socket: no cookie is 401, not a hung stream.
        status, _, body = self.http(
            "GET", f"/api/conversations/{CONVERSATION}/events")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"]["code"], "sign_in_required")
        browser.revoke()
        status, _, _ = self.http("GET", "/api/shells", browser.headers())
        self.assertEqual(status, 401)

    # ── host-side maintainer tools (tool sessions) ───────────────────────────

    def install(self, **options) -> "FakeInstall":
        return FakeInstall(self, auth_sessions.DIR, **options)

    def test_a_tool_session_opens_the_gate_and_is_revoked_on_the_way_out(self) -> None:
        install = self.install()
        self.assertTrue(tool_session.requires_sign_in(self.base))
        with mock.patch.dict(os.environ, {"SC_API_TOKEN": "home-engine",
                                          "SC_API_BASE": "http://127.0.0.1:1"}):
            with tool_session.operator_session(
                    install.root, self.base, "a tool", enrol="canary",
                    run=install.run) as session:
                headers = session.headers()
                token = install.token()
                self.assertEqual(headers, {
                    "Cookie": f"{COOKIE_NAME}={token}", "Origin": self.base})
                self.assertEqual(self.http("GET", "/api/shells", headers)[0], 200)
                # A mutation passes the origin rule with what the helper
                # gives, and not with the cookie alone.
                self.assertNotIn(
                    self.http("POST", "/api/no-such-route", headers)[0], (401, 403))
                self.assertEqual(self.http(
                    "POST", "/api/no-such-route", {"Cookie": headers["Cookie"]})[0], 403)
                self.assertNotIn(token, repr(session))
        # The install's own verbs, in its checkout, labelled with the tool:
        # a GUI user for the engine user first, because it had none.
        self.assertEqual(install.calls, [
            ["user", "add", "canary", "--port", str(self.port)],
            ["sessions", "mint", "--label", "a tool", "--json", "--user", "canary"],
            ["sessions", "revoke", session.session_id]])
        for options in install.options:
            self.assertEqual(options["cwd"], str(install.root))
            self.assertNotIn("SC_API_TOKEN", options["env"])
            self.assertNotIn("SC_API_BASE", options["env"])
            self.assertEqual(options["stdin"], subprocess.DEVNULL)
        # Revoked on the normal way out: the next request is signed out.
        self.assertEqual(install.tokens, {})
        self.assertEqual(self.http("GET", "/api/shells", headers)[0], 401)
        self.assertEqual(session.headers(), {})
        self.assertTrue(session.revoke())                 # once is enough
        self.assertEqual(len(install.calls), 3)

        # And on the other ways out: a failure, and Ctrl-C.
        for leaving in (RuntimeError, KeyboardInterrupt):
            with self.subTest(leaving.__name__):
                with self.assertRaises(leaving):
                    with tool_session.operator_session(
                            install.root, self.base, "a tool", run=install.run) as held:
                        headers = held.headers()
                        self.assertEqual(
                            self.http("GET", "/api/shells", headers)[0], 200)
                        raise leaving()
                self.assertEqual(install.calls[-1], ["sessions", "revoke", held.session_id])
                self.assertEqual(install.tokens, {})
                self.assertEqual(self.http("GET", "/api/shells", headers)[0], 401)

    def test_an_interrupted_acquisition_revokes_what_it_minted(self) -> None:
        """The session exists before `open_session` returns it. Whatever
        stops the check that follows the mint, it is revoked first."""
        import http.client as http_client
        import urllib.request as urllib_request

        for leaving in (KeyboardInterrupt(), http_client.IncompleteRead(b"{"),
                        MemoryError()):
            for opening in ("open_session", "operator_session", "canary"):
                with self.subTest(type(leaving).__name__, opening=opening):
                    install = self.install(user=True)
                    asked: list[str] = []

                    def opener(request, timeout=None, _leaving=leaving,
                               _asked=asked):
                        _asked.append(request.get_header("Cookie") or "")
                        if _asked[-1]:              # the check after the mint
                            raise _leaving
                        return urllib_request.urlopen(request, timeout=timeout)

                    err = io.StringIO()
                    with redirect_stderr(err), self.assertRaises(type(leaving)):
                        if opening == "open_session":
                            tool_session.open_session(
                                install.root, self.base, "a tool",
                                run=install.run, opener=opener)
                        elif opening == "operator_session":
                            with tool_session.operator_session(
                                    install.root, self.base, "a tool",
                                    run=install.run, opener=opener):
                                self.fail("no session was to be handed out")
                        else:
                            canary.open_operator_session(
                                install.root, self.base, run=install.run,
                                opener=opener)
                    token = install.token()
                    self.assertEqual(asked, ["", f"{COOKIE_NAME}={token}"])
                    self.assertEqual(
                        [call[:2] for call in install.calls],
                        [["sessions", "mint"], ["sessions", "revoke"]])
                    self.assertEqual(install.tokens, {})
                    self.assertEqual(self.http("GET", "/api/shells", {
                        "Cookie": f"{COOKIE_NAME}={token}"})[0], 401)
                    self.assertNotIn(token, err.getvalue())

    def test_a_tool_that_cannot_get_a_session_says_so(self) -> None:
        # No GUI user, and the tool may not make one.
        install = self.install()
        with self.assertRaises(tool_session.SessionUnavailable) as raised:
            tool_session.open_session(install.root, self.base, "a tool", run=install.run)
        self.assertIn("has no GUI user", str(raised.exception))
        self.assertIn("./sc auth user add <username>", str(raised.exception))
        self.assertEqual(install.calls, [])
        # The store is not reachable from here (the sandbox): the verb's own
        # refusal is what the tool reports.
        install = self.install(user=True, refuses="✗ ./sc auth refused: sign-in is "
                               "managed on the HOST, and this is the sandbox.")
        with self.assertRaises(tool_session.SessionUnavailable) as raised:
            tool_session.open_session(install.root, self.base, "a tool", run=install.run)
        self.assertIn("this is the sandbox", str(raised.exception))
        # No checkout to run a verb in.
        for missing in (None, install.root / "absent"):
            with self.assertRaises(tool_session.SessionUnavailable) as raised:
                tool_session.open_session(missing, self.base, "a tool", run=install.run)
            self.assertIn("requires GUI sign-in", str(raised.exception))
        # The verb cannot be run at all.
        def broken(argv, **_options):
            raise FileNotFoundError(argv[0])
        with self.assertRaises(tool_session.SessionUnavailable):
            tool_session.open_session(install.root, self.base, "a tool", run=broken)
        # A checkout that is not the one this server serves: its cookie is
        # one this gate has never heard of. Refused, and not left behind.
        other = Path(tempfile.mkdtemp(prefix="sc-other-install-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(other, True))
        publish(auth_sessions.DIR, [], enrolled=True)
        elsewhere = FakeInstall(self, other, user=True)
        with self.assertRaises(tool_session.SessionUnavailable) as raised:
            tool_session.open_session(
                elsewhere.root, self.base, "a tool", run=elsewhere.run)
        self.assertIn("did not accept", str(raised.exception))
        self.assertEqual(elsewhere.calls[-1][:2], ["sessions", "revoke"])
        self.assertEqual(elsewhere.tokens, {})
        # What the verb printed is never repeated, whatever it was.
        install = self.install(user=True, garbles=True)
        with self.assertRaises(tool_session.SessionUnavailable) as raised:
            tool_session.open_session(install.root, self.base, "a tool", run=install.run)
        self.assertNotIn(install.token(), str(raised.exception))

    def test_the_canary_plays_the_operator_with_its_own_session(self) -> None:
        install = self.install()
        root = Path(tempfile.mkdtemp(prefix="sc-canary-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, True))
        config = canary.CanaryConfig(
            source_repo=root / "source", engine_ref="candidate",
            dos_app_repo=root / "dos-app", dos_app_ref="origin/main",
            repository="acme/dos-app", receipt_path=root / "receipts" / "run.json",
            temp_parent=root, run_id="unit-test-0001")
        deadline = canary.Deadline(30, 30)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            session = canary.open_operator_session(
                install.root, self.base, run=install.run)
            token = install.token()
            # The disposable install had no GUI user: one is made for the
            # engine user the canary installs it with, and the session is
            # labelled with the canary's name.
            self.assertEqual(install.calls, [
                ["user", "add", canary.ENGINE_USER, "--port", str(self.port)],
                ["sessions", "mint", "--label", canary.TOOL_NAME, "--json",
                 "--user", canary.ENGINE_USER]])
            api = canary.JsonHttp(self.base, deadline, session)
            self.assertIsInstance(api.request("GET", "/api/shells"), dict)
            # Cleanup revokes it, first, and says so in its actions.
            backend = canary.HostBackend(deadline)
            backend._operator = session
            actions = backend.cleanup(config, None, canary.ResourceLedger())
            self.assertEqual(actions[0], {
                "action": "revoke_operator_session", "ok": True, "returncode": 0})
            self.assertEqual(install.tokens, {})
            # Signed out from the next request: the one stable failure.
            with self.assertRaises(canary.CanaryError) as raised:
                canary.JsonHttp(self.base, deadline, session).request(
                    "GET", "/api/shells")
            self.assertEqual(raised.exception.code, "CANARY_SIGN_IN_REQUIRED")
            with self.assertRaises(canary.CanaryError) as bare:
                canary.JsonHttp(self.base, deadline).request("GET", "/api/shells")
            self.assertEqual(bare.exception.code, "CANARY_SIGN_IN_REQUIRED")
            receipt = canary.Receipt(config.receipt_path, config)
            receipt.data["cleanup"]["actions"] = canary.sanitize(actions)
            receipt.data["failure"] = canary.sanitize(
                {"code": raised.exception.code, "message": raised.exception.message,
                 "details": raised.exception.details})
            receipt.event("cleanup.completed")
            receipt.write()
        # The cookie is in nothing the canary prints or keeps.
        for text in (out.getvalue(), err.getvalue(), config.receipt_path.read_text(),
                     json.dumps(actions), repr(session), raised.exception.message):
            self.assertNotIn(token, text)
        self.assertIn("revoke_operator_session", config.receipt_path.read_text())

        # A revocation that fails makes cleanup incomplete, and says which
        # session to end by hand, without the cookie.
        install = self.install(user=True)
        err = io.StringIO()
        with redirect_stderr(err):
            backend = canary.HostBackend(deadline)
            backend._operator = canary.open_operator_session(
                install.root, self.base, run=install.run)
            token, install.refuses = install.token(), "✗ auth: a write failed"
            with self.assertRaises(canary.CanaryError) as failed:
                backend.cleanup(config, None, canary.ResourceLedger())
        self.assertEqual(failed.exception.details["failed_actions"],
                         ["revoke_operator_session"])
        self.assertIn(f"./sc auth sessions revoke {backend._operator.session_id}",
                      err.getvalue())
        self.assertNotIn(token, err.getvalue())

        # No session to be had: the canary's one failure, with the reason.
        install = self.install(refuses="✗ auth: 'canary' is not an active engine user")
        with self.assertRaises(canary.CanaryError) as raised:
            canary.open_operator_session(install.root, self.base, run=install.run)
        self.assertEqual(raised.exception.code, "CANARY_SIGN_IN_REQUIRED")
        self.assertIn("not an active engine user", raised.exception.message)
        # A candidate from before sign-in: nothing is minted, nothing is sent.
        def not_found(request, timeout=None):
            raise __import__("urllib.error").error.HTTPError(
                request.full_url, 404, "x", {}, None)
        plain = canary.open_operator_session(
            install.root, "http://127.0.0.1:1", run=install.run, opener=not_found)
        self.assertEqual((plain.gated, plain.headers(), plain.revoke()), (False, {}, True))
        self.assertEqual(len(install.calls), 1)

        # The session is obtained where the canary first has a runtime to
        # ask, before it creates anything in it; every later call carries it.
        source = ast.parse((REPO / "maintainer" / "dos_app_sprint_canary.py").read_text())
        launch = next(
            node for node in ast.walk(source)
            if isinstance(node, ast.FunctionDef) and node.name == "launch"
            and "open_operator_session" in ast.unparse(node))
        body = ast.unparse(launch)
        self.assertLess(body.index("'/api/health'"), body.index("open_operator_session"))
        self.assertLess(body.index("open_operator_session"), body.index("status = "))
        orchestrate = next(
            node for node in ast.walk(source)
            if isinstance(node, ast.FunctionDef) and node.name == "orchestrate"
            and "JsonHttp" in ast.unparse(node))
        self.assertIn("self._operator)", ast.unparse(orchestrate))

    def test_the_canary_gives_its_session_back_on_ctrl_c(self) -> None:
        backend = mock.Mock()
        config = mock.Mock(whole_timeout_s=30.0, stage_timeout_s=30.0)
        with mock.patch.object(canary, "parser") as parser, \
                mock.patch.object(canary, "build_config", return_value=config), \
                mock.patch.object(canary, "Receipt"), \
                mock.patch.object(canary, "HostBackend", return_value=backend), \
                mock.patch.object(canary, "CanaryController") as controller:
            parser.return_value.parse_args.return_value = mock.Mock(command="run")
            controller.return_value.run.side_effect = KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):
                canary.main([])
        backend.release_operator.assert_called_once_with()

    def test_the_docs_capture_signs_its_browser_in_and_out(self) -> None:
        install = self.install()
        seen: dict = {}

        def gallery(config, base, output, *, capture_factory):
            session = capture_factory()._session
            cookie = session.browser_cookies()[0]
            seen["cookie"] = cookie
            seen["status"] = self.http("GET", "/api/shells", {
                "Cookie": f"{cookie['name']}={cookie['value']}"})[0]
            if seen.get("interrupt"):
                raise KeyboardInterrupt
            return {"routes_total": 5, "routes_failed": 1, "routes": [
                {"error": f"capture: the page said {cookie['value']}"}]}

        def run(*extra: str):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(server, "health_payload", return_value={
                        "ok": True, "repo": "subfloor-docs-f73-dos-app"}), \
                    mock.patch.object(capture_public_docs, "capture_gallery",
                                      side_effect=gallery) as capture, \
                    mock.patch.object(capture_public_docs, "write_gallery"), \
                    mock.patch.object(tool_session, "_sc", install.sc), \
                    mock.patch.object(sys, "argv", [
                        "capture_public_docs.py", "--port", str(self.port),
                        "--output", str(output), "--install", str(install.root),
                        *extra]), \
                    redirect_stdout(out), redirect_stderr(err):
                try:
                    code = capture_public_docs.main()
                except KeyboardInterrupt:
                    code = "interrupted"
            return code, out.getvalue(), err.getvalue(), capture

        output = Path(tempfile.mkdtemp(prefix="sc-gallery-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(output, True))
        # No GUI user and no leave to make one: exit 2, one message, no page.
        code, out, err, capture = run()
        self.assertEqual(code, 2)
        capture.assert_not_called()
        self.assertIn("has no GUI user", err)
        self.assertEqual(list(output.iterdir()), [])

        code, out, err, capture = run("--enrol-user", "canary")
        self.assertEqual(code, 1)                     # one route failed
        self.assertEqual(seen["status"], 200)         # signed in while it ran
        self.assertEqual(seen["cookie"]["url"], self.base)
        self.assertEqual(install.calls, [
            ["user", "add", "canary", "--port", str(self.port)],
            ["sessions", "mint", "--label", "capture_public_docs", "--json",
             "--user", "canary"],
            ["sessions", "revoke", install.calls[-1][2]]])
        self.assertEqual(install.tokens, {})          # and signed out after
        token = seen["cookie"]["value"]
        written = (output / "capture.json").read_text()
        self.assertIn(tool_session.REDACTED, written)
        for text in (written, out, err):
            self.assertNotIn(token, text)

        # Ctrl-C in the middle of a capture still revokes.
        seen["interrupt"] = True
        code, out, err, capture = run()
        self.assertEqual(code, "interrupted")
        self.assertEqual(seen["status"], 200)
        self.assertEqual(install.calls[-1][:2], ["sessions", "revoke"])
        self.assertEqual(install.tokens, {})
        self.assertNotIn(seen["cookie"]["value"], out + err)

    def test_the_capture_browser_holds_the_cookie_and_never_reports_it(self) -> None:
        install = self.install(user=True)
        session = tool_session.open_session(
            install.root, self.base, "capture_public_docs", run=install.run)
        self.addCleanup(session.revoke)
        token = install.token()
        browser = mock.MagicMock()
        context = browser.new_context.return_value
        context.new_page.return_value.goto.return_value.status = 200
        capture = capture_public_docs.DocumentationCapture(session)
        capture._browser = browser
        with tempfile.TemporaryDirectory() as out:
            result = capture.capture(
                self.base + "/#worktrees", {"width": 1440, "height": 1000},
                Path(out) / "shot.png", settle_ms=0, timeout_ms=1000)
            self.assertTrue(result["ok"])
            context.add_cookies.assert_called_once_with([
                {"name": COOKIE_NAME, "value": token, "url": self.base}])
            context.close.assert_called_once_with()
            # A browser error that quotes the cookie is scrubbed before the
            # gallery, which writes the message down, ever sees it.
            context.new_page.return_value.goto.side_effect = RuntimeError(
                f"net::ERR with Cookie {COOKIE_NAME}={token}")
            with self.assertRaises(RuntimeError) as raised:
                capture.capture(
                    self.base + "/#worktrees", {"width": 1440, "height": 1000},
                    Path(out) / "shot.png", settle_ms=0, timeout_ms=1000)
        self.assertNotIn(token, str(raised.exception))
        self.assertIn(tool_session.REDACTED, str(raised.exception))
        # Without sign-in the capture runs as it always did: no cookie.
        plain = capture_public_docs.DocumentationCapture(
            tool_session.ToolSession(self.base, "capture_public_docs"))
        plain._browser = mock.MagicMock()
        plain._browser.new_context.return_value.new_page.return_value \
            .goto.return_value.status = 200
        with tempfile.TemporaryDirectory() as out:
            plain.capture(self.base + "/#worktrees", {"width": 1440, "height": 1000},
                          Path(out) / "shot.png", settle_ms=0, timeout_ms=1000)
        plain._browser.new_context.return_value.add_cookies.assert_not_called()

    def test_the_maintainer_guard_lets_other_installs_through(self) -> None:
        class Answer:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return self.body

        def not_found(request, timeout=None):
            raise __import__("urllib.error").error.HTTPError(
                request.full_url, 404, "x", {}, None)

        def down(url, timeout=None):
            raise ConnectionRefusedError()

        cases = (
            ("an engine from before sign-in", not_found, False),
            ("nothing listening", down, False),
            ("not JSON", lambda url, timeout=None: Answer(b"<html>"), False),
            ("another JSON route", lambda url, timeout=None: Answer(b'{"ok":true}'), False),
            ("a signed-in caller", lambda url, timeout=None: Answer(
                b'{"enrolled":true,"signed_in":true}'), False),
            ("gated, a user exists", lambda url, timeout=None: Answer(
                b'{"enrolled":true,"signed_in":false}'), True),
            ("gated, no user yet", lambda url, timeout=None: Answer(
                b'{"enrolled":false,"signed_in":false}'), True),
        )
        for label, opener, expected in cases:
            with self.subTest(label):
                self.assertIs(tool_session.requires_sign_in(
                    "http://127.0.0.1:1", opener=opener), expected)
                if not expected:
                    # Nothing to sign in to: no verb is run, nothing is sent.
                    session = tool_session.open_session(
                        None, "http://127.0.0.1:1", "a tool", opener=opener)
                    self.assertEqual((session.gated, session.headers()), (False, {}))

    def test_no_other_sc_script_calls_the_api_over_http(self) -> None:
        """The audit, held. Every `/api/...` the `sc` entry point, a script, a
        hook, an adapter, a renderer or a template names is listed here as an
        audited caller or as not a request at all. A new one fails this test
        until someone decides which it is, and what it presents."""
        audited = {
            # GET /api/health (open) and POST /api/scripts/snapshot (engine
            # CLI credential).
            "scripts/snapshot.py": ["/api/", "/api/health", "/api/scripts/snapshot"],
            # GET /api/models?refresh=1 through `mem._api` (shell Bearer).
            "scripts/models.py": ["/api/models?refresh=1"],
            # GET /api/health (open).
            "scripts/update.py": ["/api/health"],
            "scripts/dispatch.sh": ["/api/health"],
        }
        not_requests = {
            # Links and URLs handed to the GUI as data.
            "scripts/run_attachments.py": ["/api/runs/"],
            "scripts/sprint_close.py": ["/api/conversations/"],
            # Prose: docstrings, operator hints, a README.
            "scripts/shell_factory.py": ["/api/shells"],
            "scripts/services.py": ["/api/pm2", "/api/pm2/status",
                                    "/api/pm2/validate/", "/api/ts/status"],
            "scripts/preview.py": ["/api/"],
            "scripts/cli_credential.py": ["/api/", "/api/cli-token."],
            "adapters/claude/README.md": ["/api/harnesses/claude/conversion/verify"],
            # Another service's URL.
            "scripts/quota_probes/anthropic.py": ["/api/oauth/usage",
                                                  "/api/oauth/usage."],
        }
        found: dict[str, list[str]] = {}
        files = [REPO / "sc"]
        for name in ("scripts", "hooks", "adapters", "render", "templates"):
            files.extend(path for path in (ENGINE / name).rglob("*")
                         if path.is_file() and "__pycache__" not in path.parts)
        for path in files:
            try:
                text = path.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            # Not the engine's own `api/` directory (`$ENGINE/api/x.py`).
            hits = set(re.findall(
                r"(?<!ENGINE)(?<!coder)(/api/(?![a-z_0-9]+\.py)[A-Za-z0-9_\-/?=.]*)",
                text))
            if hits:
                found[path.relative_to(ENGINE).as_posix()
                      if path != REPO / "sc" else "sc"] = sorted(hits)
        self.maxDiff = None
        self.assertEqual(found, {**audited, **not_requests})
        # `mem._api` is the one HTTP helper the scripts share; nothing but
        # `models.py` hands it an `/api/` path (it serves `/_sc/`).
        for path in (ENGINE / "scripts").rglob("*.py"):
            for match in re.finditer(r"_api\(\s*[\"'](?:GET|POST|PUT|PATCH|DELETE)[\"'],"
                                     r"\s*f?[\"'](/[a-z_]+)", path.read_text()):
                if match.group(1) == "/api":
                    self.assertEqual(path.name, "models.py")



class FakeInstall:
    """An install's host-side `./sc auth`, for a tool under test.

    `user add`, `sessions mint` and `sessions revoke` act on a published
    session list the way the real verbs do, so the review server under test
    accepts and refuses what a real install's would. `run` stands in for
    `subprocess.run`; `sc` for the helper's own runner.
    """

    def __init__(self, case, sessions_dir: Path, *, user: bool = False,
                 refuses: str | None = None, garbles: bool = False) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="sc-install-"))
        case.addCleanup(lambda: __import__("shutil").rmtree(self.root, True))
        (self.root / "sc").write_text("#!/bin/sh\n")
        self.sessions_dir = Path(sessions_dir)
        self.user = user
        self.refuses = refuses
        self.garbles = garbles
        self.tokens: dict[str, str] = {}
        self.minted: list[str] = []
        self.calls: list[list[str]] = []
        self.options: list[dict] = []
        self._publish()

    def token(self) -> str:
        return self.minted[-1]

    def _publish(self) -> None:
        publish(self.sessions_dir, [(token, 1) for token in self.tokens.values()],
                enrolled=self.user)

    def sc(self, install, argv, *, run=None, timeout=0):
        return self.run([str(Path(install) / "sc"), "auth", *argv])

    def run(self, argv, **options):
        assert argv[:2] == [str(self.root / "sc"), "auth"], argv
        verb = argv[2:]
        self.calls.append(verb)
        self.options.append(options)

        def done(code, out="", err=""):
            return subprocess.CompletedProcess(argv, code, out, err)

        if self.refuses is not None:
            return done(1, err=self.refuses + "\n")
        if verb[:2] == ["user", "add"]:
            self.user = True
            self._publish()
            return done(0, out="  one-time claim code:  ABCD-EFGH\n")
        if verb[:2] == ["sessions", "mint"]:
            if not self.user:
                return done(1, err="✗ auth: no GUI user yet\n")
            token, session_id = secrets.token_urlsafe(32), secrets.token_hex(6)
            self.minted.append(token)
            if self.garbles:
                return done(0, out=f"{COOKIE_NAME}={token}\n")
            self.tokens[session_id] = token
            self._publish()
            return done(0, err=f"→ tool session {session_id} minted\n", out=json.dumps({
                "id": session_id, "kind": "tool", "label": verb[verb.index("--label") + 1],
                "username": "canary", "user_id": 1,
                "cookie_name": COOKIE_NAME, "cookie_value": token}) + "\n")
        if verb[:2] == ["sessions", "revoke"]:
            if self.tokens.pop(verb[2], None) is None:
                return done(1, err=f"✗ no session with id {verb[2]}\n")
            self._publish()
            return done(0, out="→ 1 session(s) revoked\n")
        return done(2, err="usage\n")


if __name__ == "__main__":
    unittest.main()
