#!/usr/bin/env python3
"""Interface HTTP API — hermetic route/authority proofs (spec #20, sprint 25
seq 5). Covers the vertical-slice contract WITHOUT tmux (the runtime is a
fake implementing the facade; tmux integration lives in
tests/test_interface_runtime.py):

- authority: host allowlist, operator bearer, browser bootstrap same-origin
  fence, CSRF on browser mutations, cross-site Origin rejection;
- idempotency: missing key → 422, exact replay returns the original
  resource with NO second side effect, key + different body → 409;
- New chat: legacy/unmanaged harness refusal (409 unmanaged_harness),
  occupied-shell race (409 shell_occupied), reservation rows + launch token
  (mode 0600) + spawn identity persisted;
- hook callback: generation-token auth, exact pid identity proof, reserved →
  occupied promotion, replay rejection;
- writer leases, stream tickets, clean certification, explicit end — and
  New chat available again after durable closure.

Run:
    python3 tests/test_interface_api.py
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"

sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))
import interface_routes as routes  # noqa: E402


def build_engine_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text())
    for p in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(p.read_text())
    con.execute(
        "INSERT INTO users (user_id, username, is_active) VALUES (1,'T',1)")
    for sid in (1, 2):
        con.execute(
            "INSERT INTO shells (shell_id, display_name, shortname, mandate, "
            "system_prompt, user_id, is_shared, has_identity, bootstrapped) "
            "VALUES (?,?,?,'test','sp',1,0,1,1)", (sid, f"S{sid}", f"s{sid}"))
        con.execute(
            "INSERT INTO shell_memory_archives (archive_id, shell_id, date) "
            "VALUES (?, ?, '2026-07-22')", (sid * 10, sid))
    con.commit()
    con.close()


class FakeRuntime:
    """The runtime facade contract, with the tmux layer faked."""

    available = True
    unavailable_reason = None

    def __init__(self):
        self.on_unexpected_exit = None
        self.spawned = []
        self.terminated = []
        self.abandoned = []
        self.absence_proved = True
        self.terminate_result = {"terminated": True}

    def call(self, coro):
        return asyncio.run(coro)

    async def spawn(self, **kw):
        self.spawned.append(kw)
        return {"pane_id": "%1", "pane_pid": 4321, "pane_start_ticks": 999,
                "tmux_socket": "/run/if/tmux.sock",
                "tmux_session": "sc-interface",
                "tmux_window": f"s{kw['session_id']}"}

    async def terminate(self, session_id, force=False):
        self.terminated.append((session_id, force))
        return dict(self.terminate_result,
                    pid=4321, generation=1)

    async def verify_identity(self, session_id):
        return True

    async def prove_absence(self, session_id):
        return self.absence_proved

    async def abandon(self, session_id):
        self.abandoned.append(session_id)

    def mint_ticket(self, **kw):
        return {"ticket": f"tk-{kw['role']}-{kw['session_id']}",
                "expires_in": 60}

    def runtime_state(self, session_id):
        return {"attached_clients": 0}


def hdrs(*lines) -> str:
    return "\r\n".join(("Host: 127.0.0.1:8800", *lines))


OP = "Authorization: Bearer optok"


class InterfaceApiTest(unittest.TestCase):
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
        ]
        for p in self.patches:
            p.start()
        routes.ensure_operator_capability()
        (run_dir / "operator.token").write_text("optok")
        self.runtime = FakeRuntime()
        routes.bind_runtime(self.runtime)
        # Liveness: no unmanaged processes unless a test says otherwise.
        self.liveness = mock.patch.object(
            routes.shell_liveness, "compute",
            return_value={"supported": True, "processes": []})
        self.liveness.start()

    def tearDown(self):
        self.liveness.stop()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------------

    def call(self, method, path, header_lines=(), body=None):
        raw = hdrs(*header_lines)
        payload = json.dumps(body).encode() if body is not None else b""
        status, headers, resp = routes.handle(method, path, raw, payload)
        return status, dict(headers), json.loads(resp or b"{}")

    def create_session(self, shell_id=1, key="k-create", **extra):
        return self.call("POST", "/api/interface/sessions",
                         (OP, f"Idempotency-Key: {key}"),
                         {"shell_id": shell_id, **extra})

    def occupy(self, shell_id=1):
        """Drive a session to occupied+idle+clean: the entrypoint's
        session_start (identity, promotes reserved→occupied) then the
        provider's session_start (real readiness → idle+clean)."""
        status, _, body = self.create_session(shell_id)
        assert status == 201, body
        sid = body["session_id"]
        status, _, b = self.call(
            "POST", "/api/interface/hook-callbacks",
            ("Authorization: Bearer " + self.hook_token(sid),),
            {"shell_id": shell_id, "generation": 1, "hook_seq": 1,
             "event": "session_start", "source": "entrypoint",
             "archive_id": 10, "pid": 4321, "start_ticks": 999})
        assert status == 200, (sid, status, b)
        status, _, b = self.call(
            "POST", "/api/interface/hook-callbacks",
            ("Authorization: Bearer " + self.hook_token(sid),),
            {"shell_id": shell_id, "generation": 1, "hook_seq": 2,
             "event": "session_start", "source": "provider", "pid": 4321})
        assert status == 200, (sid, status, b)
        return sid

    def hook_token(self, session_id):
        token_file = routes.RUN_DIR / f"launch-{session_id}.json"
        return json.loads(token_file.read_text())["hook_token"]

    def acquire_lease(self, session_id, client_id="web-1", takeover=False,
                      key=None):
        return self.call(
            "POST", "/api/interface/writer-leases",
            (OP, f"Idempotency-Key: {key or f'k-lease-{takeover}'}"
                   f"-{client_id}"),
            {"session_id": session_id, "client_id": client_id,
             "takeover": takeover})

    # -- authority ---------------------------------------------------------------

    def test_host_allowlist(self):
        raw = "Host: evil.example.com\r\n" + OP
        status, _, body = routes.handle("GET", "/api/interface/shells", raw, b"")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "bad_host")

    def test_auth_required(self):
        status, _, _ = self.call("GET", "/api/interface/shells")
        self.assertEqual(status, 401)
        status, _, _ = self.call("GET", "/api/interface/shells",
                                 ("Authorization: Bearer wrong",))
        self.assertEqual(status, 401)

    def test_operator_bearer_lists_shells(self):
        status, _, body = self.call("GET", "/api/interface/shells", (OP,))
        self.assertEqual(status, 200)
        self.assertEqual(len(body["shells"]), 2)
        self.assertEqual(body["shells"][0]["availability"], "available")

    def test_browser_bootstrap_same_origin_fence(self):
        # Cross-site Origin cannot mint a session (rejected before the
        # capability is even consulted).
        status, _, _ = self.call(
            "POST", "/api/interface/browser-sessions",
            ("Origin: http://evil.example.com", "Idempotency-Key: b1"), {})
        self.assertEqual(status, 403)
        # Missing idempotency key → 422.
        status, _, _ = self.call(
            "POST", "/api/interface/browser-sessions",
            ("Origin: http://127.0.0.1:8800", OP), {})
        self.assertEqual(status, 422)
        # Same-origin proof + the operator capability mints cookie +
        # anti-forgery token.
        status, headers, body = self.call(
            "POST", "/api/interface/browser-sessions",
            ("Origin: http://127.0.0.1:8800", OP, "Idempotency-Key: b2"), {})
        self.assertEqual(status, 201)
        self.assertIn("csrf", body)
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_browser_bootstrap_requires_operator_capability(self):
        # Same-origin alone mints NOTHING (flag #43): a local process without
        # the mode-0600 operator token cannot self-mint browser authority.
        status, _, body = self.call(
            "POST", "/api/interface/browser-sessions",
            ("Origin: http://127.0.0.1:8800", "Idempotency-Key: b5"), {})
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "operator_capability_required")
        # A wrong token is refused the same way.
        status, _, _ = self.call(
            "POST", "/api/interface/browser-sessions",
            ("Origin: http://127.0.0.1:8800", "Authorization: Bearer wrong",
             "Idempotency-Key: b6"), {})
        self.assertEqual(status, 401)

    def _browser(self):
        status, headers, body = self.call(
            "POST", "/api/interface/browser-sessions",
            ("Origin: http://127.0.0.1:8800", OP, "Idempotency-Key: b3"), {})
        assert status == 201
        cookie = headers["Set-Cookie"].split(";")[0]
        return cookie, body["csrf"]

    def test_browser_mutation_needs_csrf(self):
        cookie, csrf = self._browser()
        # Cookie alone: reads pass, mutations refuse.
        status, _, _ = self.call("GET", "/api/interface/shells",
                                 (f"Cookie: {cookie}",))
        self.assertEqual(status, 200)
        status, _, body = self.call(
            "POST", "/api/interface/sessions",
            (f"Cookie: {cookie}", "Idempotency-Key: c1"), {"shell_id": 1})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "csrf")
        # Cookie + anti-forgery token: mutation proceeds.
        status, _, body = self.call(
            "POST", "/api/interface/sessions",
            (f"Cookie: {cookie}", f"X-CSRF: {csrf}",
             "Idempotency-Key: c2"), {"shell_id": 1})
        self.assertEqual(status, 201, body)

    def test_cross_site_mutation_rejected(self):
        status, _, _ = self.call(
            "POST", "/api/interface/sessions",
            (OP, "Idempotency-Key: c3", "Origin: http://evil.example.com"),
            {"shell_id": 1})
        self.assertEqual(status, 403)

    # -- idempotency ---------------------------------------------------------------

    def test_idempotency_replay_and_conflict(self):
        status, _, body = self.create_session()
        self.assertEqual(status, 201)
        first = body["session_id"]
        # Exact replay: same response, no second session row.
        status, _, body = self.create_session()
        self.assertEqual(status, 201)
        self.assertEqual(body["session_id"], first)
        self.assertEqual(len(self.runtime.spawned), 1)
        con = sqlite3.connect(self.db_path)
        count = con.execute(
            "SELECT COUNT(*) FROM interface_sessions WHERE shell_id=1"
        ).fetchone()[0]
        con.close()
        self.assertEqual(count, 1)
        # Same key, different body → 409.
        status, _, body = self.call(
            "POST", "/api/interface/sessions",
            (OP, "Idempotency-Key: k-create"),
            {"shell_id": 2})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "idempotency_conflict")
        # Missing key → 422.
        status, _, _ = self.call("POST", "/api/interface/sessions", (OP,),
                                 {"shell_id": 2})
        self.assertEqual(status, 422)

    # -- New chat refusal ---------------------------------------------------------------

    def test_unmanaged_harness_refusal(self):
        self.liveness.stop()
        self.liveness = mock.patch.object(
            routes.shell_liveness, "compute",
            return_value={"supported": True, "processes": [
                {"pid": 777, "comm": "kimi", "cwd": "/x/.sc-worktrees/s1",
                 "region": "worktree", "shortname": "s1",
                 "display_name": "S1", "is_self": False, "orphaned": False}]})
        self.liveness.start()
        status, _, body = self.create_session()
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "unmanaged_harness")
        # Nothing was reserved — the shell stays clean for a later attempt.
        con = sqlite3.connect(self.db_path)
        count = con.execute("SELECT COUNT(*) FROM interface_sessions"
                            ).fetchone()[0]
        con.close()
        self.assertEqual(count, 0)
        # …and the rail projects unreconciled, not available.
        status, _, body = self.call("GET", "/api/interface/shells", (OP,))
        self.assertEqual(body["shells"][0]["availability"], "unreconciled")

    def test_shell_occupied_race(self):
        status, _, _ = self.create_session()
        self.assertEqual(status, 201)
        status, _, body = self.create_session(key="k-second")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "shell_occupied")
        self.assertEqual(body["error"]["details"]["session_id"], 1)

    def test_unknown_fields_rejected(self):
        status, _, body = self.create_session(shell_id=1, bogus=True)
        self.assertEqual(status, 422)

    # -- reservation + spawn ---------------------------------------------------------------

    def test_create_reserves_and_persists_identity(self):
        status, _, body = self.create_session()
        self.assertEqual(status, 201)
        self.assertEqual(body["occupancy"], "reserved")
        con = sqlite3.connect(self.db_path)
        sess = con.execute(
            "SELECT occupancy, lifecycle, tmux_pane_id, pane_pid, "
            "pane_start_ticks, generation FROM interface_sessions "
            "WHERE session_id=1").fetchone()
        self.assertEqual(sess, ("reserved", "starting", "%1", 4321, 999, 1))
        composer = con.execute(
            "SELECT composer FROM interface_input_state WHERE session_id=1"
        ).fetchone()[0]
        self.assertEqual(composer, "unknown")
        con.close()
        token_file = routes.RUN_DIR / "launch-1.json"
        self.assertTrue(token_file.exists())
        self.assertEqual(stat.S_IMODE(token_file.stat().st_mode), 0o600)

    def test_spawn_definite_failure_closes_reservation(self):
        async def boom(**kw):
            raise ValueError("worktree missing")
        self.runtime.spawn = boom
        status, _, body = self.create_session()
        self.assertEqual(status, 503)
        self.assertEqual(body["occupancy"], "ended")
        con = sqlite3.connect(self.db_path)
        occ, reason = con.execute(
            "SELECT occupancy, end_reason FROM interface_sessions "
            "WHERE session_id=1").fetchone()
        self.assertEqual((occ, reason), ("ended", "spawn_failed"))
        # The shell is available again after a definite close.
        con.close()

    # -- hook callback ---------------------------------------------------------------

    def test_hook_auth_and_identity(self):
        self.create_session()
        base = {"shell_id": 1, "generation": 1, "hook_seq": 1,
                "event": "session_start", "archive_id": 10, "pid": 4321,
                "start_ticks": 999}
        # Wrong token → 403.
        status, _, _ = self.call(
            "POST", "/api/interface/hook-callbacks",
            ("Authorization: Bearer wrong",), base)
        self.assertEqual(status, 403)
        # Right token, wrong pid (identity mismatch) → 403, still reserved.
        status, _, _ = self.call(
            "POST", "/api/interface/hook-callbacks",
            ("Authorization: Bearer " + self.hook_token(1),),
            {**base, "pid": 9999})
        self.assertEqual(status, 403)
        con = sqlite3.connect(self.db_path)
        occ = con.execute("SELECT occupancy FROM interface_sessions "
                          "WHERE session_id=1").fetchone()[0]
        con.close()
        self.assertEqual(occ, "reserved")

    def test_hook_session_start_promotes(self):
        self.create_session()
        # Phase 1 — the entrypoint's identity claim: promotes reserved →
        # occupied, but is NOT readiness: lifecycle stays 'starting' and
        # the composer stays 'unknown' (seq 7 hardening).
        status, _, _ = self.call(
            "POST", "/api/interface/hook-callbacks",
            ("Authorization: Bearer " + self.hook_token(1),),
            {"shell_id": 1, "generation": 1, "hook_seq": 1,
             "event": "session_start", "source": "entrypoint",
             "archive_id": 10, "pid": 4321, "start_ticks": 999})
        self.assertEqual(status, 200)
        con = sqlite3.connect(self.db_path)
        sess = con.execute(
            "SELECT occupancy, lifecycle, archive_id FROM interface_sessions "
            "WHERE session_id=1").fetchone()
        self.assertEqual(sess, ("occupied", "starting", 10))
        composer = con.execute(
            "SELECT composer FROM interface_input_state WHERE session_id=1"
        ).fetchone()[0]
        self.assertEqual(composer, "unknown")
        con.close()
        # Phase 2 — the provider's own session_start: real readiness.
        status, _, _ = self.call(
            "POST", "/api/interface/hook-callbacks",
            ("Authorization: Bearer " + self.hook_token(1),),
            {"shell_id": 1, "generation": 1, "hook_seq": 2,
             "event": "session_start", "source": "provider", "pid": 4321})
        self.assertEqual(status, 200)
        con = sqlite3.connect(self.db_path)
        sess = con.execute(
            "SELECT occupancy, lifecycle FROM interface_sessions "
            "WHERE session_id=1").fetchone()
        self.assertEqual(sess, ("occupied", "idle"))
        composer = con.execute(
            "SELECT composer FROM interface_input_state WHERE session_id=1"
        ).fetchone()[0]
        self.assertEqual(composer, "clean")
        con.close()
        # Replay of the same hook sequence → 409, no state churn.
        status, _, body = self.call(
            "POST", "/api/interface/hook-callbacks",
            ("Authorization: Bearer " + self.hook_token(1),),
            {"shell_id": 1, "generation": 1, "hook_seq": 1,
             "event": "session_start", "source": "entrypoint",
             "archive_id": 10, "pid": 4321, "start_ticks": 999})
        self.assertEqual(status, 409)

    def test_hook_contract_validation(self):
        self.create_session()
        tok = "Authorization: Bearer " + self.hook_token(1)
        # Unknown event → 422, audited (nothing recorded).
        status, _, body = self.call(
            "POST", "/api/interface/hook-callbacks", (tok,),
            {"shell_id": 1, "generation": 1, "hook_seq": 1,
             "event": "screenshot", "pid": 4321})
        self.assertEqual(status, 422)
        # Unknown source → 422.
        status, _, _ = self.call(
            "POST", "/api/interface/hook-callbacks", (tok,),
            {"shell_id": 1, "generation": 1, "hook_seq": 1,
             "event": "session_start", "source": "moon", "pid": 4321})
        self.assertEqual(status, 422)
        # Unknown payload fields → 422 (spec: unknown fields are rejected).
        status, _, _ = self.call(
            "POST", "/api/interface/hook-callbacks", (tok,),
            {"shell_id": 1, "generation": 1, "hook_seq": 1,
             "event": "turn_stop", "prompt": "never content"})
        self.assertEqual(status, 422)
        # session_start without pid identity → 422.
        status, _, _ = self.call(
            "POST", "/api/interface/hook-callbacks", (tok,),
            {"shell_id": 1, "generation": 1, "hook_seq": 1,
             "event": "session_start", "source": "entrypoint"})
        self.assertEqual(status, 422)

    def test_hook_pid_fence_on_every_event(self):
        sid = self.occupy()
        # A pid on ANY event must be the pane's pid (exec-chain identity).
        status, _, _ = self.call(
            "POST", "/api/interface/hook-callbacks",
            ("Authorization: Bearer " + self.hook_token(sid),),
            {"shell_id": 1, "generation": 1, "hook_seq": 3,
             "event": "turn_stop", "pid": 9999})
        self.assertEqual(status, 403)

    def test_hook_approval_and_user_input_lifecycle(self):
        sid = self.occupy()
        tok = "Authorization: Bearer " + self.hook_token(sid)

        def hook(seq, event):
            status, _, body = self.call(
                "POST", "/api/interface/hook-callbacks", (tok,),
                {"shell_id": 1, "generation": 1, "hook_seq": seq,
                 "event": event, "pid": 4321})
            assert status == 200, (event, status, body)

        def lifecycle():
            con = sqlite3.connect(self.db_path)
            row = con.execute("SELECT lifecycle FROM interface_sessions "
                              "WHERE session_id=?", (sid,)).fetchone()[0]
            con.close()
            return row

        hook(3, "prompt_submit")
        self.assertEqual(lifecycle(), "busy")
        hook(4, "approval_wait")
        self.assertEqual(lifecycle(), "approval")
        # The wait raised an alert; the result resolves it.
        con = sqlite3.connect(self.db_path)
        alert = con.execute(
            "SELECT 1 FROM planner_alerts WHERE session_id=? AND "
            "reason='approval_wait' AND resolved_at IS NULL",
            (sid,)).fetchone()
        con.close()
        self.assertIsNotNone(alert)
        hook(5, "approval_result")
        self.assertEqual(lifecycle(), "busy")
        con = sqlite3.connect(self.db_path)
        alert = con.execute(
            "SELECT 1 FROM planner_alerts WHERE session_id=? AND "
            "reason='approval_wait' AND resolved_at IS NULL",
            (sid,)).fetchone()
        con.close()
        self.assertIsNone(alert)
        hook(6, "user_input_wait")
        self.assertEqual(lifecycle(), "user_input")
        # turn_stop from a wait state walks back through busy to idle.
        hook(7, "turn_stop")
        self.assertEqual(lifecycle(), "idle")

    def test_hook_interrupt_and_failure_end_the_turn(self):
        sid = self.occupy()
        tok = "Authorization: Bearer " + self.hook_token(sid)

        def hook(seq, event):
            status, _, body = self.call(
                "POST", "/api/interface/hook-callbacks", (tok,),
                {"shell_id": 1, "generation": 1, "hook_seq": seq,
                 "event": event, "pid": 4321})
            assert status == 200, (event, status, body)

        def lifecycle():
            con = sqlite3.connect(self.db_path)
            row = con.execute("SELECT lifecycle FROM interface_sessions "
                              "WHERE session_id=?", (sid,)).fetchone()[0]
            con.close()
            return row

        hook(3, "prompt_submit")
        hook(4, "interrupt")  # kimi Interrupt: Stop never fires on cancel
        self.assertEqual(lifecycle(), "idle")
        hook(5, "prompt_submit")
        hook(6, "failure")  # claude StopFailure: Stop never fires on error
        self.assertEqual(lifecycle(), "idle")
        con = sqlite3.connect(self.db_path)
        alert = con.execute(
            "SELECT 1 FROM planner_alerts WHERE session_id=? AND "
            "reason='turn_failure'", (sid,)).fetchone()
        con.close()
        self.assertIsNotNone(alert)

    def test_hook_capability_alerts_at_readiness(self):
        # claude (no approval-result/user-input events) → degraded info
        # alert; the chat itself is unaffected.
        status, _, body = self.create_session(1, harness="claude")
        assert status == 201, body
        sid = body["session_id"]
        tok = "Authorization: Bearer " + self.hook_token(sid)
        status, _, b = self.call(
            "POST", "/api/interface/hook-callbacks", (tok,),
            {"shell_id": 1, "generation": 1, "hook_seq": 1,
             "event": "session_start", "source": "entrypoint",
             "archive_id": 10, "pid": 4321, "start_ticks": 999,
             "cli_version": "2.1.217 (Claude Code)"})
        assert status == 200, b
        status, _, b = self.call(
            "POST", "/api/interface/hook-callbacks", (tok,),
            {"shell_id": 1, "generation": 1, "hook_seq": 2,
             "event": "session_start", "source": "provider", "pid": 4321})
        assert status == 200, b
        con = sqlite3.connect(self.db_path)
        alert = con.execute(
            "SELECT severity FROM planner_alerts WHERE session_id=? AND "
            "reason='hooks_degraded'", (sid,)).fetchone()
        con.close()
        self.assertIsNotNone(alert)
        self.assertEqual(alert[0], "info")

    def test_hook_mandatory_gap_alerts_not_armable(self):
        # An unknown harness (no adapter) can chat, but provider readiness
        # flags the mandatory-hook gap: wake can never arm on it.
        status, _, body = self.create_session(1, harness="ed")
        assert status == 201, body
        sid = body["session_id"]
        tok = "Authorization: Bearer " + self.hook_token(sid)
        self.call("POST", "/api/interface/hook-callbacks", (tok,),
                  {"shell_id": 1, "generation": 1, "hook_seq": 1,
                   "event": "session_start", "source": "entrypoint",
                   "archive_id": 10, "pid": 4321, "start_ticks": 999})
        status, _, b = self.call(
            "POST", "/api/interface/hook-callbacks", (tok,),
            {"shell_id": 1, "generation": 1, "hook_seq": 2,
             "event": "session_start", "source": "provider", "pid": 4321})
        assert status == 200, b
        con = sqlite3.connect(self.db_path)
        alert = con.execute(
            "SELECT severity FROM planner_alerts WHERE session_id=? AND "
            "reason='wake_not_armable'", (sid,)).fetchone()
        con.close()
        self.assertIsNotNone(alert)
        self.assertEqual(alert[0], "warning")

    def test_provider_readiness_never_cleans_after_human_input(self):
        sid = self.occupy()
        # A human frame is accepted (composer dirty), THEN a second provider
        # session_start arrives (e.g. a resume): it must NOT manufacture
        # clean — only submit/certify can.
        status, _, body = self.acquire_lease(sid)
        assert status == 201, body
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE interface_input_state SET composer='dirty', "
                    "forwarded_seq=1 WHERE session_id=?", (sid,))
        con.commit()
        con.close()
        status, _, _ = self.call(
            "POST", "/api/interface/hook-callbacks",
            ("Authorization: Bearer " + self.hook_token(sid),),
            {"shell_id": 1, "generation": 1, "hook_seq": 3,
             "event": "session_start", "source": "provider", "pid": 4321})
        self.assertEqual(status, 200)
        con = sqlite3.connect(self.db_path)
        composer = con.execute(
            "SELECT composer FROM interface_input_state WHERE session_id=?",
            (sid,)).fetchone()[0]
        con.close()
        self.assertEqual(composer, "dirty")

    def test_hook_illegal_transition_rejected(self):
        self.create_session()
        # prompt_submit from lifecycle 'starting' is an illegal edge
        # (starting → busy) — rejected + audited, no state churn.
        status, _, body = self.call(
            "POST", "/api/interface/hook-callbacks",
            ("Authorization: Bearer " + self.hook_token(1),),
            {"shell_id": 1, "generation": 1, "hook_seq": 1,
             "event": "prompt_submit", "pid": 4321})
        self.assertEqual(status, 409)
        con = sqlite3.connect(self.db_path)
        lifecycle = con.execute(
            "SELECT lifecycle FROM interface_sessions WHERE session_id=1"
        ).fetchone()[0]
        con.close()
        self.assertEqual(lifecycle, "starting")

    # -- leases + tickets + certification ---------------------------------------------------------------

    def test_writer_lease_flow(self):
        sid = self.occupy()
        status, _, body = self.acquire_lease(sid)
        self.assertEqual(status, 201)
        self.assertEqual(body["next_input_seq"], 1)
        token = body["lease_token"]
        # Held lease refuses a second writer; takeover succeeds and reseeds
        # from the SESSION's forwarded sequence (still 1 here).
        status, _, _ = self.acquire_lease(sid, client_id="web-2", key="k-l2")
        self.assertEqual(status, 409)
        status, _, body = self.acquire_lease(sid, client_id="web-2",
                                             takeover=True)
        self.assertEqual(status, 201)
        self.assertEqual(body["next_input_seq"], 1)
        token2 = body["lease_token"]
        # Writer ticket needs the CURRENT lease token.
        status, _, _ = self.call(
            "POST", "/api/interface/stream-tickets",
            (OP, "Idempotency-Key: t1"),
            {"session_id": sid, "role": "writer", "client_id": "web-2",
             "lease_token": token})
        self.assertEqual(status, 403)
        status, _, body = self.call(
            "POST", "/api/interface/stream-tickets",
            (OP, "Idempotency-Key: t2"),
            {"session_id": sid, "role": "writer", "client_id": "web-2",
             "lease_token": token2})
        self.assertEqual(status, 201)
        self.assertIn("ticket", body)
        # Viewer ticket needs no lease.
        status, _, body = self.call(
            "POST", "/api/interface/stream-tickets",
            (OP, "Idempotency-Key: t3"),
            {"session_id": sid, "role": "viewer", "client_id": "web-3"})
        self.assertEqual(status, 201)

    def test_certify_clean(self):
        sid = self.occupy()
        _, _, lease = self.acquire_lease(sid)
        # Dirty the composer through the durable broker path (fake write).
        sys.path.insert(0, str(ENGINE / "scripts"))
        import interface_broker
        con = sqlite3.connect(self.db_path)
        interface_broker.accept_human_input(con, sid, 1, 3, lambda n: None)
        con.close()
        # A non-writer cannot certify.
        status, _, _ = self.call(
            "POST", "/api/interface/clean-certifications",
            (OP, "Idempotency-Key: cc1"),
            {"session_id": sid, "client_id": "someone-else", "client_seq": 1})
        self.assertEqual(status, 409)
        # The certifying writer clears it.
        status, _, body = self.call(
            "POST", "/api/interface/clean-certifications",
            (OP, "Idempotency-Key: cc2"),
            {"session_id": sid, "client_id": "web-1", "client_seq": 1})
        self.assertEqual(status, 201)
        self.assertEqual(body["composer"], "clean")

    # -- terminate + New chat again ---------------------------------------------------------------

    def test_terminate_and_new_chat_available_again(self):
        sid = self.occupy()
        self.acquire_lease(sid)
        status, _, body = self.call(
            "POST", "/api/interface/termination-requests",
            (OP, "Idempotency-Key: x1"),
            {"session_id": sid, "force": False})
        self.assertEqual(status, 202)
        self.assertTrue(body["terminated"])
        con = sqlite3.connect(self.db_path)
        sess = con.execute(
            "SELECT occupancy, lifecycle, end_reason FROM interface_sessions "
            "WHERE session_id=?", (sid,)).fetchone()
        self.assertEqual(sess, ("ended", "ended", "operator_end"))
        gen = con.execute(
            "SELECT ended_at FROM interface_generations "
            "WHERE shell_id=1 AND generation=1").fetchone()[0]
        self.assertIsNotNone(gen)
        leases = con.execute(
            "SELECT COUNT(*) FROM interface_writer_leases "
            "WHERE session_id=? AND revoked_at IS NULL", (sid,)).fetchone()[0]
        self.assertEqual(leases, 0)
        con.close()
        # Availability is derived only after durable closure: New chat again.
        status, _, body = self.call("GET", "/api/interface/shells", (OP,))
        self.assertEqual(body["shells"][0]["availability"], "available")
        status, _, body = self.create_session(key="k-again")
        self.assertEqual(status, 201, body)

    def test_terminate_graceful_timeout_then_force(self):
        sid = self.occupy()
        self.runtime.terminate_result = {"terminated": False,
                                         "reason": "graceful_timeout"}
        status, _, body = self.call(
            "POST", "/api/interface/termination-requests",
            (OP, "Idempotency-Key: x2"),
            {"session_id": sid, "force": False})
        self.assertEqual(status, 200)
        self.assertFalse(body["terminated"])
        self.assertEqual(body["reason"], "graceful_timeout")
        self.assertEqual(body["pid"], 4321)
        # The timeout is recorded durably — it is what unlocks force.
        con = sqlite3.connect(self.db_path)
        stamped = con.execute(
            "SELECT graceful_timed_out_at FROM interface_sessions "
            "WHERE session_id=?", (sid,)).fetchone()[0]
        con.close()
        self.assertIsNotNone(stamped)
        # Force is the separate, explicit follow-up.
        self.runtime.terminate_result = {"terminated": True}
        status, _, body = self.call(
            "POST", "/api/interface/termination-requests",
            (OP, "Idempotency-Key: x3"),
            {"session_id": sid, "force": True})
        self.assertEqual(status, 202)
        con = sqlite3.connect(self.db_path)
        reason = con.execute(
            "SELECT end_reason FROM interface_sessions WHERE session_id=?",
            (sid,)).fetchone()[0]
        con.close()
        self.assertEqual(reason, "operator_force")

    def test_force_requires_prior_graceful_timeout(self):
        # Spec Workflow 9 (flag #42): force first-touch is refused — the API,
        # not just the UI, gates force on a prior graceful timeout.
        sid = self.occupy()
        status, _, body = self.call(
            "POST", "/api/interface/termination-requests",
            (OP, "Idempotency-Key: x5"),
            {"session_id": sid, "force": True})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"],
                         "force_requires_graceful_timeout")
        self.assertEqual(self.runtime.terminated, [],
                         "no signal may be sent on a refused force")

    def test_terminate_identity_mismatch_fails_closed(self):
        sid = self.occupy()
        # Earn the force gate first: graceful attempt times out.
        self.runtime.terminate_result = {"terminated": False,
                                         "reason": "graceful_timeout"}
        status, _, _ = self.call(
            "POST", "/api/interface/termination-requests",
            (OP, "Idempotency-Key: x6"),
            {"session_id": sid, "force": False})
        self.assertEqual(status, 200)
        # Now the force follow-up hits an identity mismatch → fail closed.
        self.runtime.terminate_result = {"terminated": False,
                                         "reason": "identity_mismatch"}
        status, _, body = self.call(
            "POST", "/api/interface/termination-requests",
            (OP, "Idempotency-Key: x4"),
            {"session_id": sid, "force": True})
        self.assertEqual(status, 409)
        con = sqlite3.connect(self.db_path)
        occ = con.execute("SELECT occupancy FROM interface_sessions "
                          "WHERE session_id=?", (sid,)).fetchone()[0]
        con.close()
        self.assertEqual(occ, "unreconciled")

    def test_unexpected_exit_transition_marks_lost(self):
        # The DB transition itself (unit). The LIVE trigger — real pane death
        # → pump EOF → this callback — is proven end-to-end in
        # tests/test_interface_runtime.py::test_pane_death_drives_real_lost_transition.
        sid = self.occupy()
        routes._on_unexpected_exit(sid)
        con = sqlite3.connect(self.db_path)
        sess = con.execute(
            "SELECT occupancy, lifecycle FROM interface_sessions "
            "WHERE session_id=?", (sid,)).fetchone()
        self.assertEqual(sess, ("unreconciled", "lost"))
        con.close()
        status, _, body = self.call("GET", "/api/interface/shells", (OP,))
        self.assertEqual(body["shells"][0]["availability"], "lost")

    # -- close: the road out of unreconciled (flag #41) -------------------------------

    def _unreconciled(self, shell_id=1):
        sid = self.occupy(shell_id)
        routes._on_unexpected_exit(sid)   # occupied → unreconciled/lost
        return sid

    def test_close_unreconciled_after_proved_absence(self):
        sid = self._unreconciled()
        status, _, body = self.call(
            "POST", "/api/interface/reconciliations",
            (OP, "Idempotency-Key: cl1"),
            {"session_id": sid, "action": "close"})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["closed"])
        self.assertEqual(self.runtime.abandoned, [sid])
        con = sqlite3.connect(self.db_path)
        sess = con.execute(
            "SELECT occupancy, lifecycle, end_reason FROM interface_sessions "
            "WHERE session_id=?", (sid,)).fetchone()
        self.assertEqual(sess, ("ended", "ended", "operator_close"))
        gen = con.execute(
            "SELECT ended_at FROM interface_generations "
            "WHERE shell_id=1 AND generation=1").fetchone()[0]
        self.assertIsNotNone(gen)
        con.close()
        # The road THROUGH: the shell offers New chat again (fresh generation).
        status, _, body = self.call("GET", "/api/interface/shells", (OP,))
        self.assertEqual(body["shells"][0]["availability"], "available")
        status, _, body = self.create_session(key="k-after-close")
        self.assertEqual(status, 201, body)

    def test_close_refused_without_proved_absence(self):
        sid = self._unreconciled()
        self.runtime.absence_proved = False
        status, _, body = self.call(
            "POST", "/api/interface/reconciliations",
            (OP, "Idempotency-Key: cl2"),
            {"session_id": sid, "action": "close"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "absence_not_proved")
        con = sqlite3.connect(self.db_path)
        occ = con.execute("SELECT occupancy FROM interface_sessions "
                          "WHERE session_id=?", (sid,)).fetchone()[0]
        con.close()
        self.assertEqual(occ, "unreconciled", "a refused close changes nothing")

    def test_close_refused_on_occupied(self):
        sid = self.occupy()
        status, _, body = self.call(
            "POST", "/api/interface/reconciliations",
            (OP, "Idempotency-Key: cl3"),
            {"session_id": sid, "action": "close"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "not_unreconciled")

    def test_reconcile_verify_still_default(self):
        sid = self._unreconciled()
        status, _, body = self.call(
            "POST", "/api/interface/reconciliations",
            (OP, "Idempotency-Key: cl4"),
            {"session_id": sid})
        self.assertEqual(status, 200)
        self.assertTrue(body["verified"])
        self.assertEqual(body["occupancy"], "occupied")


if __name__ == "__main__":
    unittest.main()
