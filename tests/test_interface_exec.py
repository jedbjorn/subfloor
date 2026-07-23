#!/usr/bin/env python3
"""Interface pane entrypoint proofs (spec #20, sprint 25 seq 5).

Hermetic against a real engine DB (schema.sql + all migrations, the
build_engine_db pattern from test_interface_crash_window) with the launch
pipeline, the exec, and (where the contract allows) the HTTP layer stubbed:

1. The reservation capability gates EVERYTHING: a missing, unparsable, or
   under-fielded token file refuses with exit 2 before any archive row
   exists (prepare_launch is never even called).
2. The token is single-use: consumed on a successful read, so a second
   invocation of the same path refuses.
3. A prepared launch confirms identity to the API BEFORE exec: one POST to
   /api/interface/hook-callbacks carrying event session_start, hook_seq 1,
   this pid, the archive id, and the bearer hook_token — then, and only
   then, the process execs the harness argv.
4. Fail closed: an HTTP rejection (403) or an unreachable API exits 4 and
   the harness NEVER starts — an unpromoted reservation expires into
   unreconciled for the operator. The hook_token never appears on stderr.

Run:
    python3 tests/test_interface_exec.py
"""
from __future__ import annotations

import contextlib
import http.server
import io
import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"

sys.path.insert(0, str(ENGINE / "scripts"))
import interface_exec  # noqa: E402
import run as run_mod  # noqa: E402

HOOK_TOKEN = "tok-secret-deadbeef"


def build_engine_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text())
    for p in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(p.read_text())
    con.execute(
        "INSERT INTO users (user_id, username, is_active) VALUES (1,'T',1)")
    con.execute(
        "INSERT INTO shells (shell_id, display_name, shortname, mandate, "
        "system_prompt, user_id, is_shared, has_identity, bootstrapped) "
        "VALUES (1,'S1','s1','test','sp',1,0,1,1)")
    con.commit()
    con.close()


class StubHandler(http.server.BaseHTTPRequestHandler):
    """Records every POST; replies with the server's configured status."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        self.server.requests.append({
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "body": json.loads(raw or b"{}"),
        })
        self.send_response(self.server.status)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class InterfaceExecTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.worktree = self.tmp / "wt"
        self.worktree.mkdir()
        self.db = self.tmp / "shell_db.db"
        build_engine_db(self.db)
        self.cwd = os.getcwd()
        self.plan_calls = []
        self.exec_calls = []
        self._patches = [
            mock.patch.object(run_mod, "prepare_launch", self._fake_prepare),
            mock.patch.object(interface_exec, "_exec", self._fake_exec),
        ]
        for p in self._patches:
            p.start()
        # Local API stub; tests point the token's api_port at it (or at a
        # closed port for the unreachable case).
        self.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), StubHandler)
        self.server.requests = []
        self.server.status = 200
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for p in reversed(self._patches):
            p.stop()
        os.chdir(self.cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── stubs ───────────────────────────────────────────────────────────
    def _fake_prepare(self, **kwargs):
        self.plan_calls.append(kwargs)
        return run_mod.LaunchPlan(
            argv=["/bin/true"], env=dict(os.environ), cwd=str(self.worktree),
            session_id="0001", archive_id=4242, harness="claude",
            model=None, effort=None, cli_version="test-cli 1.0")

    def _fake_exec(self, argv, env):
        self.exec_calls.append((argv, env))

    # ── helpers ─────────────────────────────────────────────────────────
    def _write_token(self, **overrides):
        token = {"session_id": 7, "shell_id": 1, "generation": 3,
                 "hook_token": HOOK_TOKEN, "api_port": self.port,
                 "worktree": str(self.worktree), "harness": "claude",
                 "model": None, "effort": None}
        token.update(overrides)
        path = self.tmp / "launch-7.json"
        path.write_text(json.dumps(token))
        return path

    def _run(self, path) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = interface_exec.main([str(path)])
        return code, err.getvalue()

    def _archive_count(self) -> int:
        con = sqlite3.connect(self.db)
        n = con.execute("SELECT COUNT(*) FROM shell_memory_archives").fetchone()[0]
        con.close()
        return n

    # ── 1: the capability gates the archive ─────────────────────────────
    def test_missing_token_refuses_before_archive(self):
        code, err = self._run(self.tmp / "nope.json")
        self.assertEqual(code, 2)
        self.assertEqual(self.plan_calls, [],
                         "refusal must precede prepare_launch")
        self.assertEqual(self._archive_count(), 0,
                         "no archive row may exist without the capability")
        self.assertEqual(self.exec_calls, [])

    def test_unparsable_token_refuses_before_archive(self):
        path = self.tmp / "launch-7.json"
        path.write_text("{not json")
        code, _ = self._run(path)
        self.assertEqual(code, 2)
        self.assertEqual(self.plan_calls, [])
        self.assertEqual(self._archive_count(), 0)

    def test_missing_fields_refuse_before_archive(self):
        for drop in ("session_id", "shell_id", "generation", "hook_token",
                     "api_port", "worktree"):
            token = json.loads(self._write_token().read_text())
            del token[drop]
            path = self.tmp / f"launch-drop-{drop}.json"
            path.write_text(json.dumps(token))
            with self.subTest(dropped=drop):
                code, _ = self._run(path)
                self.assertEqual(code, 2)
        self.assertEqual(self.plan_calls, [])
        self.assertEqual(self._archive_count(), 0)

    def test_bad_worktree_refuses(self):
        path = self._write_token(worktree=str(self.tmp / "ghost"))
        code, _ = self._run(path)
        self.assertEqual(code, 2)
        self.assertEqual(self.plan_calls, [])
        self.assertEqual(self._archive_count(), 0)

    # ── 2: single use ───────────────────────────────────────────────────
    def test_token_consumed_on_read(self):
        path = self._write_token()
        code, _ = self._run(path)
        self.assertEqual(code, 0)
        self.assertFalse(path.exists(),
                         "the token must be deleted after a successful read")
        code, _ = self._run(path)
        self.assertEqual(code, 2, "second use of the same capability refuses")
        self.assertEqual(len(self.exec_calls), 1)

    # ── 3: confirm identity before exec ─────────────────────────────────
    def test_session_start_post_contract(self):
        code, _ = self._run(self._write_token())
        self.assertEqual(code, 0)
        self.assertEqual(len(self.server.requests), 1)
        req = self.server.requests[0]
        self.assertEqual(req["path"], "/api/interface/hook-callbacks")
        self.assertEqual(req["authorization"], f"Bearer {HOOK_TOKEN}")
        body = req["body"]
        self.assertEqual(body["event"], "session_start")
        self.assertEqual(body["hook_seq"], 1)
        self.assertEqual(body["shell_id"], 1)
        self.assertEqual(body["generation"], 3)
        self.assertEqual(body["archive_id"], 4242,
                         "the archive id comes from prepare_launch")
        self.assertEqual(body["pid"], os.getpid())
        self.assertIsInstance(body["start_ticks"], int)
        self.assertEqual(body["cli_version"], "test-cli 1.0")
        self.assertEqual(self.exec_calls[0][0], ["/bin/true"])
        self.assertEqual(self.plan_calls[0]["shell_id"], 1)
        self.assertEqual(self.plan_calls[0]["harness"], "claude")

    # ── 4: fail closed ──────────────────────────────────────────────────
    def test_rejected_hook_never_execs(self):
        self.server.status = 403
        code, err = self._run(self._write_token())
        self.assertEqual(code, 4)
        self.assertEqual(self.exec_calls, [],
                         "a rejected session_start must never exec")
        self.assertNotIn(HOOK_TOKEN, err,
                         "the hook_token must never reach stderr")

    def test_unreachable_api_retries_then_fails_closed(self):
        calls = []
        real_urlopen = urllib.request.urlopen

        def counting_urlopen(req, timeout=None):
            calls.append(req.full_url)
            return real_urlopen(req, timeout=timeout)

        orig_post = interface_exec._post_session_start

        def fast_post(api_port, hook_token, body, **kw):
            kw.setdefault("backoff", 0)
            return orig_post(api_port, hook_token, body, **kw)

        with mock.patch.object(interface_exec.urllib.request, "urlopen",
                               counting_urlopen), \
             mock.patch.object(interface_exec, "_post_session_start",
                               fast_post):
            code, err = self._run(self._write_token(api_port=closed_port()))
        self.assertEqual(code, 4)
        self.assertEqual(len(calls), interface_exec.POST_RETRIES,
                         "unreachable API exhausts its retries")
        self.assertEqual(self.exec_calls, [])
        self.assertNotIn(HOOK_TOKEN, err)

    def test_prepare_failure_exits_3_without_exec(self):
        def refusing_prepare(**kwargs):
            raise run_mod.LaunchError("shell_id 9 is not launchable")

        with mock.patch.object(run_mod, "prepare_launch", refusing_prepare):
            code, _ = self._run(self._write_token())
        self.assertEqual(code, 3)
        self.assertEqual(self.exec_calls, [])
        self.assertEqual(self.server.requests, [],
                         "no identity claim without a prepared launch")


if __name__ == "__main__":
    unittest.main()
