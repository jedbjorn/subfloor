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
  New chat available again after durable closure;
- lifecycle convergence (spec #30): session_end hook runs the one closure
  helper, cancel start on a reservation (#519), hook/terminate races end
  in one terminal record (#532), repeated ends are idempotent successes,
  and route/state/server errors stay distinct (#523).

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
import threading
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"

sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))
import interface_routes as routes  # noqa: E402
import run as run_mod  # noqa: E402


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
            # New chat provisions a missing shell worktree through the CLI
            # boot's ensure_worktree (flag #61) — a real git mutation.
            # Hermetic default: stub it; the provisioning tests below drive
            # the stub explicitly.
            mock.patch.object(run_mod, "ensure_worktree"),
        ]
        for p in self.patches:
            p.start()
        self.ensure_wt = run_mod.ensure_worktree
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
        self.assertEqual(body["error"]["details"]["occupancy"], "reserved")

    def test_unknown_fields_rejected(self):
        status, _, body = self.create_session(shell_id=1, bogus=True)
        self.assertEqual(status, 422)

    # -- worktree provisioning (flag #61) --------------------------------------

    def _launch_token(self, session_id):
        return json.loads(
            (routes.RUN_DIR / f"launch-{session_id}.json").read_text())

    def test_new_chat_provisions_missing_worktree(self):
        """A shell never CLI-booted (e.g. a planner woken only through the
        Interface) has no .sc-worktrees/<shortname>: New chat must provision
        it through the CLI boot's ensure_worktree BEFORE the reservation —
        the old failure was a raw 'not a directory' at exec time."""
        root = Path(self.tmp.name)
        with mock.patch.object(run_mod, "REPO_ROOT", root):
            expected = str(root / ".sc-worktrees" / "s1")
            status, _, body = self.create_session()
        self.assertEqual(status, 201, body)
        self.ensure_wt.assert_called_once_with(Path(expected), "s1")
        self.assertEqual(self._launch_token(body["session_id"])["worktree"],
                         expected)
        self.assertEqual(self.runtime.spawned[0]["worktree"], expected)

    def test_new_chat_existing_worktree_not_reprovisioned(self):
        root = Path(self.tmp.name)
        wt = root / ".sc-worktrees" / "s1"
        wt.mkdir(parents=True)
        (wt / ".git").touch()  # a real git worktree carries a .git file
        with mock.patch.object(run_mod, "REPO_ROOT", root):
            status, _, body = self.create_session()
        self.assertEqual(status, 201, body)
        self.ensure_wt.assert_not_called()

    def test_new_chat_admin_resolves_repo_root(self):
        """The admin flavor boots at the repo root (the CLI boot's rule) —
        the Interface must resolve it there and provision nothing."""
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE shells SET flavor='admin' WHERE shell_id=1")
        con.execute("UPDATE flavor_defaults SET model=NULL "
                    "WHERE flavor='admin' AND harness='claude'")
        con.commit()
        con.close()
        root = Path(self.tmp.name)
        with mock.patch.object(run_mod, "REPO_ROOT", root):
            status, _, body = self.create_session()
        self.assertEqual(status, 201, body)
        self.ensure_wt.assert_not_called()
        self.assertEqual(self._launch_token(body["session_id"])["worktree"],
                         str(root))

    def test_new_chat_provision_failure_is_actionable(self):
        """A failed provision refuses cleanly: an actionable 500, and no
        reservation row, token, or pane left behind."""
        self.ensure_wt.side_effect = SystemExit(
            "FATAL: could not create worktree at /x:\nfatal: branch conflict")
        root = Path(self.tmp.name)
        with mock.patch.object(run_mod, "REPO_ROOT", root):
            status, _, body = self.create_session()
        self.assertEqual(status, 500)
        err = body["error"]
        self.assertEqual(err["code"], "worktree_provision_failed")
        self.assertIn("branch conflict", err["message"])
        self.assertIn("git worktree list", err["message"])
        self.assertNotIn("FATAL", err["message"])
        con = sqlite3.connect(self.db_path)
        count = con.execute("SELECT COUNT(*) FROM interface_sessions"
                            ).fetchone()[0]
        con.close()
        self.assertEqual(count, 0)
        self.assertEqual(list(routes.RUN_DIR.glob("launch-*.json")), [])
        self.assertEqual(self.runtime.spawned, [])

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
            "SELECT resolved_at FROM planner_alerts WHERE session_id=? AND "
            "reason='turn_failure'", (sid,)).fetchone()
        con.close()
        self.assertIsNotNone(alert)
        self.assertIsNone(alert[0])
        hook(7, "prompt_submit")
        hook(8, "turn_stop")
        con = sqlite3.connect(self.db_path)
        resolved = con.execute(
            "SELECT resolved_at FROM planner_alerts WHERE session_id=? AND "
            "reason='turn_failure'", (sid,)).fetchone()[0]
        con.close()
        self.assertIsNotNone(resolved)

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
        status, _, detail = self.call(
            "GET", f"/api/interface/sessions/{sid}", (OP,))
        self.assertEqual(status, 200)
        self.assertEqual(detail["alerts"], 0,
                         "capability information is outside warning counts")

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
        status, _, held = self.acquire_lease(
            sid, client_id="web-2", key="k-l2")
        self.assertEqual(status, 409)
        self.assertEqual(held["error"]["code"], "writer_held")
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

    def test_missing_identity_never_attaches_cached_terminal(self):
        sid = self.occupy()
        con = sqlite3.connect(self.db_path)
        con.execute(
            "UPDATE interface_sessions SET tmux_pane_id=NULL, pane_pid=NULL, "
            "pane_start_ticks=NULL WHERE session_id=?", (sid,))
        con.commit()
        con.close()
        status, _, body = self.call(
            "GET", "/api/interface/shells", (OP,))
        shell = next(s for s in body["shells"] if s["shell_id"] == 1)
        self.assertEqual(shell["availability"], "unreconciled")
        self.assertFalse(shell["attachable"])
        self.assertFalse(shell["identity_verified"])
        status, _, body = self.acquire_lease(sid)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "identity_unverified")
        status, _, body = self.call(
            "POST", "/api/interface/stream-tickets",
            (OP, "Idempotency-Key: no-cache-view"),
            {"session_id": sid, "role": "viewer", "client_id": "web-view"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "identity_unverified")

    def test_incompatible_lifecycle_refuses_writer_and_viewer(self):
        sid = self.occupy()
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE interface_sessions SET lifecycle='lost' "
                    "WHERE session_id=?", (sid,))
        con.commit()
        con.close()
        status, _, lease = self.acquire_lease(sid)
        self.assertEqual(status, 409)
        self.assertEqual(lease["error"]["code"], "not_attachable")
        status, _, ticket = self.call(
            "POST", "/api/interface/stream-tickets",
            (OP, "Idempotency-Key: lost-view"),
            {"session_id": sid, "role": "viewer", "client_id": "web-view"})
        self.assertEqual(status, 409)
        self.assertEqual(ticket["error"]["code"], "not_attachable")

    def test_stale_stored_default_blocks_before_reservation(self):
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE shells SET flavor='admin' WHERE shell_id=1")
        con.execute(
            "UPDATE flavor_defaults SET model='missing-route' "
            "WHERE flavor='admin' AND harness='claude'")
        con.commit()
        con.close()
        status, _, body = self.create_session(harness="claude")
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_model_route")
        self.assertIn("Harness default", body["error"]["details"]["action"])
        con = sqlite3.connect(self.db_path)
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM interface_sessions").fetchone()[0], 0)
        con.close()

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

    def test_ended_occupancy_with_live_lifecycle_needs_reconciliation(self):
        session_id = self.occupy()
        con = sqlite3.connect(self.db_path)
        con.execute(
            "UPDATE interface_sessions SET occupancy='ended', "
            "ended_at=datetime('now') WHERE session_id=?",
            (session_id,))
        con.commit()
        con.close()

        status, _, body = self.call("GET", "/api/interface/shells", (OP,))
        self.assertEqual(status, 200)
        shell = body["shells"][0]
        self.assertEqual(shell["session_id"], session_id)
        self.assertEqual(shell["availability"], "unreconciled")

    def test_terminal_state_without_ended_at_needs_reconciliation(self):
        session_id = self.occupy()
        con = sqlite3.connect(self.db_path)
        con.execute(
            "UPDATE interface_sessions SET lifecycle='stopping' "
            "WHERE session_id=?", (session_id,))
        con.execute(
            "UPDATE interface_sessions SET lifecycle='ended' "
            "WHERE session_id=?", (session_id,))
        con.execute(
            "UPDATE interface_sessions SET occupancy='ended' "
            "WHERE session_id=?", (session_id,))
        con.commit()
        con.close()

        status, _, body = self.call("GET", "/api/interface/shells", (OP,))
        self.assertEqual(status, 200)
        shell = body["shells"][0]
        self.assertEqual(shell["session_id"], session_id)
        self.assertEqual(shell["availability"], "unreconciled")

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

    # -- lifecycle convergence (sprint 31 unit 1, spec #30 / #519 #523 #532) --

    def test_session_end_hook_converges_full_closure(self):
        """#532: the provider's session_end hook runs the ONE closure helper —
        occupancy, lifecycle, generation, and leases converge atomically; the
        occupied/ended divergence no route could close is gone."""
        sid = self.occupy()
        self.acquire_lease(sid)
        tok = "Authorization: Bearer " + self.hook_token(sid)
        status, _, body = self.call(
            "POST", "/api/interface/hook-callbacks", (tok,),
            {"shell_id": 1, "generation": 1, "hook_seq": 3,
             "event": "session_end", "pid": 4321})
        self.assertEqual(status, 200, body)
        con = sqlite3.connect(self.db_path)
        sess = con.execute(
            "SELECT occupancy, lifecycle, end_reason FROM interface_sessions "
            "WHERE session_id=?", (sid,)).fetchone()
        self.assertEqual(sess, ("ended", "ended", "provider_session_end"))
        gen = con.execute(
            "SELECT ended_at FROM interface_generations "
            "WHERE shell_id=1 AND generation=1").fetchone()[0]
        self.assertIsNotNone(gen)
        leases = con.execute(
            "SELECT COUNT(*) FROM interface_writer_leases "
            "WHERE session_id=? AND revoked_at IS NULL", (sid,)).fetchone()[0]
        self.assertEqual(leases, 0)
        con.close()
        # New chat immediately — no service restart, no reconcile (#532's
        # workaround is gone).
        status, _, body = self.call("GET", "/api/interface/shells", (OP,))
        self.assertEqual(body["shells"][0]["availability"], "available")
        status, _, body = self.create_session(key="k-after-end")
        self.assertEqual(status, 201, body)
        # A duplicate session_end acknowledges without reopening …
        status, _, body = self.call(
            "POST", "/api/interface/hook-callbacks", (tok,),
            {"shell_id": 1, "generation": 1, "hook_seq": 4,
             "event": "session_end", "pid": 4321})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["already_ended"])
        # …but any other event on the ended generation is still rejected.
        status, _, _ = self.call(
            "POST", "/api/interface/hook-callbacks", (tok,),
            {"shell_id": 1, "generation": 1, "hook_seq": 5,
             "event": "turn_stop", "pid": 4321})
        self.assertEqual(status, 403)

    def test_terminate_on_ended_lifecycle_converges(self):
        """The #532 legacy state (occupied + lifecycle ended): termination
        completes durable closure idempotently — success, never a transition
        back to stopping, never a false no_such_route."""
        sid = self.occupy()
        self.acquire_lease(sid)
        con = sqlite3.connect(self.db_path)
        routes.interface_state.transition(con, "lifecycle", sid, "stopping")
        routes.interface_state.transition(con, "lifecycle", sid, "ended")
        con.execute(
            "UPDATE interface_generations SET ended_at=datetime('now') "
            "WHERE shell_id=1 AND generation=1")
        con.commit()
        con.close()
        status, _, body = self.call(
            "POST", "/api/interface/termination-requests",
            (OP, "Idempotency-Key: x-leg"),
            {"session_id": sid, "force": False})
        self.assertEqual(status, 202, body)
        self.assertTrue(body["terminated"])
        self.assertTrue(body["already_ended"])
        self.assertEqual(self.runtime.terminated, [],
                         "nothing live to signal — closure only")
        con = sqlite3.connect(self.db_path)
        sess = con.execute(
            "SELECT occupancy, lifecycle FROM interface_sessions "
            "WHERE session_id=?", (sid,)).fetchone()
        self.assertEqual(sess, ("ended", "ended"))
        leases = con.execute(
            "SELECT COUNT(*) FROM interface_writer_leases "
            "WHERE session_id=? AND revoked_at IS NULL", (sid,)).fetchone()[0]
        self.assertEqual(leases, 0)
        con.close()

    def test_terminate_race_session_end_hook_wins(self):
        """The exact #532 interleaving: the harness exits DURING the graceful
        window — its session_end hook lands while the termination request is
        in flight. One clean terminal record, success response, no 404."""
        sid = self.occupy()
        tok = "Authorization: Bearer " + self.hook_token(sid)
        test = self

        async def race_terminate(session_id, force=False):
            status, _, b = test.call(
                "POST", "/api/interface/hook-callbacks", (tok,),
                {"shell_id": 1, "generation": 1, "hook_seq": 3,
                 "event": "session_end", "pid": 4321})
            assert status == 200, b
            return {"terminated": False, "reason": "graceful_timeout",
                    "pid": 4321, "generation": 1}

        self.runtime.terminate = race_terminate
        status, _, body = self.call(
            "POST", "/api/interface/termination-requests",
            (OP, "Idempotency-Key: x-race"),
            {"session_id": sid, "force": False})
        self.assertEqual(status, 202, body)
        self.assertTrue(body["terminated"])
        self.assertTrue(body["already_ended"])
        con = sqlite3.connect(self.db_path)
        sess = con.execute(
            "SELECT occupancy, lifecycle, end_reason FROM interface_sessions "
            "WHERE session_id=?", (sid,)).fetchone()
        self.assertEqual(sess[:2], ("ended", "ended"))
        con.close()
        # New chat immediately — the race leaves a converged terminal record.
        status, _, body = self.create_session(key="k-after-race")
        self.assertEqual(status, 201, body)

    def test_repeated_end_chat_fresh_key_semantic_success(self):
        """Spec Lifecycle Contract: a repeated request races the completed
        closure — the same idempotency key replays the original response; a
        FRESH key against the ended session returns the same semantic
        success without a second signal."""
        sid = self.occupy()
        status, _, body = self.call(
            "POST", "/api/interface/termination-requests",
            (OP, "Idempotency-Key: x-repeat"),
            {"session_id": sid, "force": False})
        self.assertEqual(status, 202)
        # Same key: exact replay of the stored response.
        status, _, replay = self.call(
            "POST", "/api/interface/termination-requests",
            (OP, "Idempotency-Key: x-repeat"),
            {"session_id": sid, "force": False})
        self.assertEqual(status, 202)
        self.assertEqual(replay, body)
        # Fresh key: semantic success, no second signal to the runtime.
        status, _, body = self.call(
            "POST", "/api/interface/termination-requests",
            (OP, "Idempotency-Key: x-repeat-2"),
            {"session_id": sid, "force": False})
        self.assertEqual(status, 202, body)
        self.assertTrue(body["terminated"])
        self.assertTrue(body["already_ended"])
        self.assertEqual(len(self.runtime.terminated), 1)

    # -- cancel start (#519) ----------------------------------------------------

    def test_cancel_start_without_identity(self):
        """#519: End chat on a reservation that never established pane or
        harness identity cancels it — cancelled_before_spawn, no signal, the
        shell available again."""
        status, _, body = self.create_session()
        assert status == 201
        sid = body["session_id"]
        con = sqlite3.connect(self.db_path)
        con.execute(
            "UPDATE interface_sessions SET tmux_pane_id=NULL, pane_pid=NULL, "
            "pane_start_ticks=NULL WHERE session_id=?", (sid,))
        con.commit()
        con.close()
        status, _, body = self.call(
            "POST", "/api/interface/termination-requests",
            (OP, "Idempotency-Key: cx1"),
            {"session_id": sid, "force": False})
        self.assertEqual(status, 202, body)
        self.assertTrue(body["terminated"])
        self.assertEqual(body["end_reason"], "cancelled_before_spawn")
        self.assertEqual(self.runtime.terminated, [],
                         "no identity ever established — nothing to signal")
        con = sqlite3.connect(self.db_path)
        sess = con.execute(
            "SELECT occupancy, lifecycle, end_reason FROM interface_sessions "
            "WHERE session_id=?", (sid,)).fetchone()
        self.assertEqual(sess, ("ended", "ended", "cancelled_before_spawn"))
        gen = con.execute(
            "SELECT ended_at FROM interface_generations "
            "WHERE shell_id=1 AND generation=1").fetchone()[0]
        self.assertIsNotNone(gen)
        con.close()
        # The reservation no longer blocks New chat (#519's actual complaint).
        status, _, body = self.create_session(key="k-after-cancel")
        self.assertEqual(status, 201, body)

    def test_cancel_start_verified_identity_runs_stop_path(self):
        """Cancel start with a verified live pane identity signals the exact
        generation and converges through normal closure."""
        status, _, body = self.create_session()
        assert status == 201
        sid = body["session_id"]
        status, _, body = self.call(
            "POST", "/api/interface/termination-requests",
            (OP, "Idempotency-Key: cx2"),
            {"session_id": sid, "force": False})
        self.assertEqual(status, 202, body)
        self.assertTrue(body["terminated"])
        self.assertEqual(self.runtime.terminated, [(sid, False)],
                         "verified identity — the exact pane is signalled")
        con = sqlite3.connect(self.db_path)
        sess = con.execute(
            "SELECT occupancy, lifecycle, end_reason FROM interface_sessions "
            "WHERE session_id=?", (sid,)).fetchone()
        self.assertEqual(sess, ("ended", "ended", "operator_end"))
        con.close()

    def test_cancel_start_unverifiable_identity_parks_unreconciled(self):
        """Spawn outcome uncertain: cancel start never silently ends — the
        session becomes unreconciled and requires absence proof."""
        async def no_verify(session_id):
            return False
        self.runtime.verify_identity = no_verify
        status, _, body = self.create_session()
        assert status == 201
        sid = body["session_id"]
        status, _, body = self.call(
            "POST", "/api/interface/termination-requests",
            (OP, "Idempotency-Key: cx3"),
            {"session_id": sid, "force": False})
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"]["code"], "identity_unverified")
        con = sqlite3.connect(self.db_path)
        sess = con.execute(
            "SELECT occupancy, lifecycle, error_detail FROM "
            "interface_sessions WHERE session_id=?", (sid,)).fetchone()
        self.assertEqual(sess[:2], ("unreconciled", "lost"))
        self.assertIn("cancel start", sess[2])
        con.close()
        # The road out: prove absence, then reconcile-close.
        status, _, body = self.call(
            "POST", "/api/interface/reconciliations",
            (OP, "Idempotency-Key: cx4"),
            {"session_id": sid, "action": "close"})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["closed"])

    def test_cancel_during_inflight_spawn_tears_down_pane(self):
        """SC-064: a cancel start landing while the runtime spawn is still
        in flight (reservation committed, pane identity never persisted)
        must not conclude as a live harness on an ended row — the #519
        wound with no API path out. The create call converges instead: no
        201, the pane torn down by exact identity, and the row stays the
        cancel's terminal record with NULL identity."""
        started = threading.Event()
        release = threading.Event()
        real_spawn = self.runtime.spawn

        async def slow_spawn(**kw):
            started.set()
            release.wait(10)
            return await real_spawn(**kw)

        self.runtime.spawn = slow_spawn
        outcome = {}

        def create():
            outcome["result"] = self.create_session()

        t = threading.Thread(target=create)
        t.start()
        self.assertTrue(started.wait(10), "spawn in flight")
        con = sqlite3.connect(self.db_path)
        sid = con.execute(
            "SELECT session_id FROM interface_sessions WHERE shell_id=1"
        ).fetchone()[0]
        con.close()
        # The SC-064 window: reserved/starting, pane identity still NULL.
        status, _, body = self.call(
            "POST", "/api/interface/termination-requests",
            (OP, "Idempotency-Key: cx-race"),
            {"session_id": sid, "force": False})
        self.assertEqual(status, 202, body)
        self.assertEqual(body["end_reason"], "cancelled_before_spawn")
        release.set()
        t.join(10)
        status, _, body = outcome["result"]
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"]["code"], "session_cancelled",
                         "never a 201 + live harness on an ended row")
        self.assertIn(sid, self.runtime.abandoned,
                      "the just-spawned pane is torn down by exact identity")
        con = sqlite3.connect(self.db_path)
        sess = con.execute(
            "SELECT occupancy, lifecycle, end_reason, tmux_pane_id, pane_pid "
            "FROM interface_sessions WHERE session_id=?", (sid,)).fetchone()
        self.assertEqual(sess[:3], ("ended", "ended",
                                    "cancelled_before_spawn"))
        self.assertEqual(sess[3:], (None, None),
                         "no pane identity persisted onto the ended row")
        con.close()
        # The shell is immediately available — no unmanaged-harness wound.
        status, _, body = self.create_session(key="k-after-race-cancel")
        self.assertEqual(status, 201, body)

    def test_terminate_unreconciled_points_at_reconcile(self):
        """An unreconciled session still refuses termination — but with a
        truthful code and the supported next action, not a bare 'not
        occupied'."""
        sid = self._unreconciled()
        status, _, body = self.call(
            "POST", "/api/interface/termination-requests",
            (OP, "Idempotency-Key: x-unrec"),
            {"session_id": sid, "force": False})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "not_occupied")
        self.assertIn("reconcile", body["error"]["message"])

    def test_terminate_not_running_proves_absence_and_closes(self):
        """The runtime holding no live generation is absence, not a graceful
        timeout: prove it and converge — never a phantom
        graceful_timed_out_at."""
        sid = self.occupy()
        self.runtime.terminate_result = {"terminated": False,
                                         "reason": "not_running"}
        status, _, body = self.call(
            "POST", "/api/interface/termination-requests",
            (OP, "Idempotency-Key: x-nr"),
            {"session_id": sid, "force": False})
        self.assertEqual(status, 202, body)
        self.assertTrue(body["terminated"])
        self.assertEqual(body["reason"], "already_absent")
        con = sqlite3.connect(self.db_path)
        sess = con.execute(
            "SELECT occupancy, graceful_timed_out_at FROM interface_sessions "
            "WHERE session_id=?", (sid,)).fetchone()
        self.assertEqual(sess[0], "ended")
        self.assertIsNone(sess[1], "absence is not a graceful timeout")
        con.close()

    def test_terminate_not_running_without_absence_fails_closed(self):
        sid = self.occupy()
        self.runtime.terminate_result = {"terminated": False,
                                         "reason": "not_running"}
        self.runtime.absence_proved = False
        status, _, body = self.call(
            "POST", "/api/interface/termination-requests",
            (OP, "Idempotency-Key: x-nr2"),
            {"session_id": sid, "force": False})
        self.assertEqual(status, 409, body)
        self.assertEqual(body["reason"], "not_running")
        con = sqlite3.connect(self.db_path)
        sess = con.execute(
            "SELECT occupancy, lifecycle FROM interface_sessions "
            "WHERE session_id=?", (sid,)).fetchone()
        self.assertEqual(sess, ("unreconciled", "lost"))
        con.close()

    # -- API error mapping (#523, spec req 4) ------------------------------------

    def test_bad_path_id_is_422_never_no_such_route(self):
        status, _, body = self.call("GET", "/api/interface/sessions/abc",
                                    (OP,))
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_path_id")

    def test_unknown_route_still_404(self):
        status, _, body = self.call("GET", "/api/interface/bogus", (OP,))
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "no_such_route")

    def test_escaped_state_conflict_is_409_never_no_such_route(self):
        """#523: an illegal transition raised inside a handler was rewritten
        to a false 404 no_such_route by the broad `except ValueError`. State
        conflicts now map to 409 with a stable code."""
        sid = self._unreconciled()
        with mock.patch.object(
                routes.interface_state, "transition",
                side_effect=routes.interface_state.InterfaceTransitionError(
                    "illegal transition: ended -> stopping")):
            status, _, body = self.call(
                "POST", "/api/interface/reconciliations",
                (OP, "Idempotency-Key: em1"),
                {"session_id": sid, "action": "verify"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "state_conflict")
        self.assertIn("ended -> stopping", body["error"]["message"])

    def test_unexpected_failure_is_sanitized_500_with_correlation(self):
        """Unexpected handler failures: a sanitized 500 whose correlation id
        matches a server-side record — internals never cross the wire."""
        self.create_session()
        with mock.patch.object(
                routes.interface_broker, "current_writer",
                side_effect=RuntimeError("db on fire")):
            status, _, body = self.call("GET", "/api/interface/sessions/1",
                                        (OP,))
        self.assertEqual(status, 500)
        self.assertEqual(body["error"]["code"], "internal")
        self.assertNotIn("db on fire", json.dumps(body),
                         "internals never leak into the response")
        self.assertTrue(body["error"]["details"]["correlation"])

    # -- worktree path validation + launcher exception curation (#526 lows) -----

    def test_worktree_path_not_directory_refused(self):
        """A stray FILE at the worktree path is a distinct, actionable
        refusal — provisioning would no-op on it and the pane would die."""
        root = Path(self.tmp.name)
        wt = root / ".sc-worktrees" / "s1"
        wt.parent.mkdir(parents=True)
        wt.touch()
        with mock.patch.object(run_mod, "REPO_ROOT", root):
            status, _, body = self.create_session()
        self.assertEqual(status, 500)
        self.assertEqual(body["error"]["code"], "worktree_not_directory")
        self.assertEqual(body["error"]["details"]["reason"], "non_directory")
        self.ensure_wt.assert_not_called()

    def test_worktree_plain_directory_unusable_refused(self):
        """A bare directory without git backing is unusable, not
        'existing' — provisioning assumes an existing dir is intact."""
        root = Path(self.tmp.name)
        (root / ".sc-worktrees" / "s1").mkdir(parents=True)
        with mock.patch.object(run_mod, "REPO_ROOT", root):
            status, _, body = self.create_session()
        self.assertEqual(status, 500)
        self.assertEqual(body["error"]["code"], "worktree_unusable")
        self.assertEqual(body["error"]["details"]["reason"], "not_a_worktree")
        self.ensure_wt.assert_not_called()

    def test_provision_oserror_curated(self):
        """Expected launcher failures (git binary missing, mkdir refused)
        are curated into the actionable 500, never a raw 500."""
        self.ensure_wt.side_effect = FileNotFoundError(
            "No such file or directory: 'git'")
        root = Path(self.tmp.name)
        with mock.patch.object(run_mod, "REPO_ROOT", root):
            status, _, body = self.create_session()
        self.assertEqual(status, 500)
        self.assertEqual(body["error"]["code"], "worktree_provision_failed")
        self.assertIn("git", body["error"]["message"])

    def test_provision_launch_error_curated(self):
        self.ensure_wt.side_effect = run_mod.LaunchError(
            "shell is not launchable")
        root = Path(self.tmp.name)
        with mock.patch.object(run_mod, "REPO_ROOT", root):
            status, _, body = self.create_session()
        self.assertEqual(status, 500)
        self.assertEqual(body["error"]["code"], "worktree_provision_failed")
        self.assertIn("LaunchError", body["error"]["message"])


if __name__ == "__main__":
    unittest.main()
