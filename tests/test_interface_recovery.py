#!/usr/bin/env python3
"""Unified stranded-shell recovery — hermetic proofs (spec #30 req 24 /
task #95, sprint 31 unit 8).

Covers the preview/execute contract WITHOUT tmux (pane presence is patched
at the interface_recovery seam) and WITHOUT the websockets runtime (the
module is stdlib-only by design — HTTP-only recovery):

- classification table: available / stale durable lock (no identity, dead
  process, open active archive) / exact idle orphan (pane gone, exact pid
  alive; residual process after session end) / verified live / indeterminate
  (pane unknown, /proc unreadable);
- observation fencing: expiry, changed durable state, a process/pane
  transition after the preview, or tracked/untracked worktree work appearing
  after the preview → 409 recovery_observation_stale with NO signal, closure,
  reset or clean; unknown id → 404; replay via Idempotency-Key returns the
  original response with no second side effect;
- action legality: recover refused on verified_live, force refused without
  confirm_force or against non-verified-live classifications;
- exact process-group signaling against REAL child processes: SIGTERM
  kills, SIGKILL only after the grace with the same identity, identity loss
  performs no signal and closes nothing, and closure follows ONLY on
  /proc-proven absence — an unreadable grace or a SIGKILL survivor refuses
  with a named next action;
- pane presence: tmux list-panes membership proves BOTH ways — a dead pane
  is provably gone (classification can reach pane-gone), only an
  unanswering server is unknown;
- atomic closure: session+generation+leases, archive close with the
  active_archive_id guard, alert resolution, generation-bound binding
  release, ambiguous binding parked with a named next action, unread
  messages left unread;
- worktree: preserved by default; discard requires the typed shortname,
  refuses unpushed commits (fail closed), never deletes the worktree, and a
  mid-discard git failure after the committed closure is reported
  step-by-step — never a 500 that hides what completed;
- CLI parity: ./sc interface recover drives GET preview → POST execute with
  the spec's flags (--force/--discard-worktree/--yes/--json).

Run:
    python3 tests/test_interface_recovery.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import ClassVar
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
TESTS = Path(__file__).resolve().parent

sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))

import interface_cli as ic  # noqa: E402
import interface_recovery as recovery  # noqa: E402
import interface_routes as routes  # noqa: E402
import run as run_mod  # noqa: E402
from test_interface_api import FakeRuntime, build_engine_db  # noqa: E402
from test_interface_cli import SHELLS, FakeResp  # noqa: E402


def hdrs(*lines) -> str:
    return "\r\n".join(("Host: 127.0.0.1:8800", *lines))


OP = "Authorization: Bearer optok"
IDEM = "Idempotency-Key: k-1"

DEAD_PID = 2 ** 22 - 1  # pid_max on stock Linux; nothing lives there


def spawn_child(*, ignore_sigterm: bool = False):
    """A real process in its OWN process group (pgid == pid), plus its exact
    /proc start ticks — the identity recovery fences on. The SIGTERM-ignoring
    variant acks handler installation before returning (no signal race)."""
    if ignore_sigterm:
        argv = [sys.executable, "-c",
                ("import signal,time;"
                 "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                 "print('ready',flush=True);time.sleep(60)")]
        proc = subprocess.Popen(argv, start_new_session=True,
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
        assert proc.stdout.readline().strip() == "ready"
    else:
        argv = [sys.executable, "-c", "import time;time.sleep(60)"]
        proc = subprocess.Popen(argv, start_new_session=True,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    ticks, _state = recovery._read_stat(proc.pid)
    return proc, ticks


class RecoveryCase(unittest.TestCase):
    """Shared hermetic rig: tmp engine DB (schema + every migration, incl.
    0083), patched route paths, no runtime (HTTP-only by default)."""

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
        ]
        for p in self.patches:
            p.start()
        routes.ensure_operator_capability()
        (run_dir / "operator.token").write_text("optok")
        self.runtime = FakeRuntime()
        routes.bind_runtime(self.runtime)
        self.liveness = mock.patch.object(
            routes.shell_liveness, "compute",
            return_value={"supported": True, "processes": []})
        self.liveness.start()
        self.children = []

    def tearDown(self):
        for proc in getattr(self, "children", []):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001, S110 — teardown best-effort
                pass
        self.liveness.stop()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------------

    def db(self):
        con = sqlite3.connect(self.db_path)
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def call(self, method, path, header_lines=(), body=None):
        payload = json.dumps(body).encode() if body is not None else b""
        status, _headers, resp = routes.handle(
            method, path, hdrs(*header_lines), payload)
        return status, json.loads(resp or b"{}")

    def child(self, **kw):
        proc, ticks = spawn_child(**kw)
        self.children.append(proc)
        return proc, ticks

    def make_session(self, shell_id, *, occupancy="occupied", lifecycle="idle",
                     generation=1, pane_pid=None, pane_ticks=None,
                     pane_id="%1", socket="/run/if/tmux.sock",
                     archive_id=None, worktree=None) -> int:
        con = self.db()
        try:
            con.execute(
                "INSERT OR IGNORE INTO interface_generations "
                "(shell_id, generation, hook_token_hash) VALUES (?,?,'h')",
                (shell_id, generation))
            cur = con.execute(
                "INSERT INTO interface_sessions "
                "(shell_id, generation, archive_id, harness, worktree, "
                " tmux_socket, tmux_pane_id, pane_pid, pane_start_ticks, "
                " occupancy, lifecycle) "
                "VALUES (?,?,?,'claude',?,?,?,?,?,?,?)",
                (shell_id, generation, archive_id, worktree, socket,
                 pane_id, pane_pid, pane_ticks, occupancy, lifecycle))
            con.execute(
                "INSERT INTO interface_input_state "
                "(session_id, shell_id, generation) VALUES (?,?,?)",
                (cur.lastrowid, shell_id, generation))
            con.commit()
            return cur.lastrowid
        finally:
            con.close()

    def end_session_rows(self, session_id: int) -> None:
        """Drive a session to its terminal record through the ONE closure
        helper (as a completed recovery or hook would)."""
        import interface_broker
        con = self.db()
        try:
            interface_broker.close_session(con, session_id, "operator_end")
            con.commit()
        finally:
            con.close()

    def preview(self, shell_id):
        status, obj = self.call("GET",
                                f"/api/interface/shells/{shell_id}/recovery",
                                (OP,))
        assert status == 200, obj
        return obj


# ------------------------------------------------------------------ classify

class ClassificationTest(RecoveryCase):

    def test_available_when_nothing_live(self):
        con = self.db()
        con.execute("UPDATE shell_memory_archives SET ended_at='t' "
                    "WHERE archive_id=10")
        con.commit()
        con.close()
        obj = self.preview(1)
        self.assertEqual(obj["classification"], "available")
        self.assertEqual(obj["legal_actions"], [])
        self.assertTrue(obj["observation_id"])
        self.assertEqual(
            [row["key"] for row in obj["evidence_projection"]],
            ["shell", "classification", "legal_actions", "session",
             "generation", "archive", "sprint_binding", "process", "tmux",
             "unread_messages", "worktree"])
        projected = {
            row["key"]: row["value"] for row in obj["evidence_projection"]
        }
        self.assertEqual(projected["classification"], "available")
        self.assertEqual(projected["legal_actions"], "none")
        self.assertEqual(projected["session"], "no Interface session")
        self.assertEqual(projected["process"],
                         "no recorded process identity")

    def test_stale_lock_reservation_without_identity(self):
        self.make_session(1, occupancy="reserved", lifecycle="starting",
                          pane_pid=None, pane_ticks=None, pane_id=None)
        obj = self.preview(1)
        self.assertEqual(obj["classification"], "stale_durable_lock")
        self.assertEqual(obj["legal_actions"], ["recover"])

    def test_stale_lock_dead_process(self):
        self.make_session(1, pane_pid=DEAD_PID, pane_ticks=1)
        with mock.patch.object(recovery, "_pane_present", return_value=False):
            obj = self.preview(1)
        self.assertEqual(obj["classification"], "stale_durable_lock")
        self.assertEqual(obj["legal_actions"], ["recover"])

    def test_exact_idle_orphan_pane_gone_process_alive(self):
        proc, ticks = self.child()
        self.make_session(1, pane_pid=proc.pid, pane_ticks=ticks)
        with mock.patch.object(recovery, "_pane_present", return_value=False):
            obj = self.preview(1)
        self.assertEqual(obj["classification"], "exact_idle_orphan")
        self.assertEqual(obj["legal_actions"], ["recover"])
        self.assertEqual(obj["evidence"]["process"]["pgid"], proc.pid)

    def test_verified_live(self):
        proc, ticks = self.child()
        self.make_session(1, pane_pid=proc.pid, pane_ticks=ticks)
        with mock.patch.object(recovery, "_pane_present", return_value=True):
            obj = self.preview(1)
        self.assertEqual(obj["classification"], "verified_live")
        self.assertEqual(obj["legal_actions"], ["force"])

    def test_indeterminate_when_tmux_cant_answer(self):
        proc, ticks = self.child()
        self.make_session(1, pane_pid=proc.pid, pane_ticks=ticks)
        with mock.patch.object(recovery, "_pane_present", return_value=None):
            obj = self.preview(1)
        self.assertEqual(obj["classification"], "indeterminate")
        self.assertEqual(obj["legal_actions"], [])

    def test_indeterminate_when_proc_unreadable(self):
        self.make_session(1, pane_pid=DEAD_PID, pane_ticks=1)
        with mock.patch.object(recovery, "_pane_present", return_value=False), \
                mock.patch.object(recovery, "_proc_state",
                                  return_value="unreadable"):
            obj = self.preview(1)
        self.assertEqual(obj["classification"], "indeterminate")

    def test_residual_process_after_session_end_is_orphan(self):
        proc, ticks = self.child()
        sid = self.make_session(1, pane_pid=proc.pid, pane_ticks=ticks)
        self.end_session_rows(sid)
        obj = self.preview(1)
        self.assertEqual(obj["classification"], "exact_idle_orphan")
        self.assertEqual(obj["legal_actions"], ["recover"])

    def test_open_active_archive_is_stale_lock(self):
        con = self.db()
        con.execute("UPDATE shells SET active_archive_id=10 WHERE shell_id=1")
        con.commit()
        con.close()
        obj = self.preview(1)
        self.assertEqual(obj["classification"], "stale_durable_lock")
        self.assertEqual(obj["legal_actions"], ["recover"])

    def test_spec_prefix_alias(self):
        con = self.db()
        con.execute("UPDATE shell_memory_archives SET ended_at='t' "
                    "WHERE archive_id=10")
        con.commit()
        con.close()
        status, obj = self.call("GET",
                                "/_sc/interface/shells/1/recovery", (OP,))
        self.assertEqual(status, 200)
        self.assertEqual(obj["classification"], "available")


# ------------------------------------------------------------------ pane presence

class PanePresentTest(unittest.TestCase):
    """_pane_present against a mocked tmux: list-panes membership proves
    BOTH ways — a dead pane is provably gone, only an unanswering server is
    unknown (regression: the old targeted display-message probe mapped a
    dead pane's nonzero exit to unknown, leaving pane-gone unreachable)."""

    def run_tmux(self, returncode=0, stdout="", stderr=""):
        return mock.patch.object(
            recovery.subprocess, "run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=returncode, stdout=stdout,
                stderr=stderr))

    def test_pane_listed_is_present(self):
        with self.run_tmux(stdout="%1\n%2\n") as run:
            self.assertIs(recovery._pane_present("/s.sock", "%2"), True)
        argv = run.call_args[0][0]
        self.assertEqual(argv[:4], ["tmux", "-S", "/s.sock", "list-panes"])

    def test_pane_missing_from_listing_is_proven_gone(self):
        with self.run_tmux(stdout="%1\n%3\n"):
            self.assertIs(recovery._pane_present("/s.sock", "%2"), False)

    def test_unreachable_server_is_unknown(self):
        with self.run_tmux(returncode=1, stderr="no server running"):
            self.assertIsNone(recovery._pane_present("/s.sock", "%1"))

    def test_tmux_failure_is_unknown(self):
        with mock.patch.object(recovery.subprocess, "run",
                               side_effect=OSError("no tmux binary")):
            self.assertIsNone(recovery._pane_present("/s.sock", "%1"))

    def test_no_socket_is_unknown(self):
        self.assertIsNone(recovery._pane_present(None, "%1"))


# ------------------------------------------------------------------ signaling

class ProcessGroupSignalTest(unittest.TestCase):
    """terminate_process_group against REAL processes (no API)."""

    def setUp(self):
        self.children = []

    def tearDown(self):
        for proc in self.children:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001, S110 — teardown best-effort
                pass

    def child(self, **kw):
        proc, ticks = spawn_child(**kw)
        self.children.append(proc)
        return proc, ticks

    def test_sigterm_kills_exact_group(self):
        proc, ticks = self.child()
        result = recovery.terminate_process_group(proc.pid, ticks, grace_s=2)
        self.assertTrue(result["signaled"])
        self.assertTrue(result["dead"])
        self.assertFalse(result["escalated"])
        self.assertEqual(result["pgid"], proc.pid)
        proc.wait(timeout=5)
        self.assertEqual(recovery._proc_state(proc.pid, ticks), "dead")

    def test_sigkill_only_after_grace_with_same_identity(self):
        proc, ticks = self.child(ignore_sigterm=True)
        result = recovery.terminate_process_group(proc.pid, ticks, grace_s=0.3)
        self.assertTrue(result["signaled"])
        self.assertTrue(result["dead"])
        self.assertTrue(result["escalated"])
        proc.wait(timeout=5)
        self.assertEqual(recovery._proc_state(proc.pid, ticks), "dead")

    def test_unreadable_during_grace_is_not_absence(self):
        proc, ticks = self.child()
        states = mock.Mock(side_effect=["alive"] + ["unreadable"] * 50)
        with mock.patch.object(recovery, "_proc_state", states):
            result = recovery.terminate_process_group(proc.pid, ticks,
                                                      grace_s=0.3)
        self.assertTrue(result["signaled"])  # SIGTERM really sent
        self.assertFalse(result["dead"])
        self.assertFalse(result["escalated"])
        self.assertEqual(result["reason"], "absence_unproven")

    def test_sigkill_survivor_is_not_absence(self):
        # D-state analog: the process never leaves /proc even after SIGKILL.
        proc, ticks = self.child(ignore_sigterm=True)
        with mock.patch.object(recovery, "_proc_state",
                               return_value="alive"):
            result = recovery.terminate_process_group(proc.pid, ticks,
                                                      grace_s=0.3)
        self.assertTrue(result["signaled"])
        self.assertTrue(result["escalated"])
        self.assertFalse(result["dead"])
        self.assertEqual(result["reason"], "absence_unproven")

    def test_no_signal_on_identity_mismatch(self):
        proc, ticks = self.child()
        # Wrong ticks = a recycled pid is NOT our process — never signaled.
        result = recovery.terminate_process_group(proc.pid, ticks + 1,
                                                  grace_s=0.2)
        self.assertFalse(result["signaled"])
        self.assertEqual(result["reason"], "indeterminate")
        self.assertEqual(recovery._proc_state(proc.pid, ticks), "alive")

    def test_no_signal_on_dead_pid(self):
        result = recovery.terminate_process_group(DEAD_PID, 1, grace_s=0.2)
        self.assertFalse(result["signaled"])

    def test_zombie_counts_as_dead(self):
        proc, ticks = self.child()
        proc.terminate()
        proc.wait(timeout=5)  # reaped by us: gone
        self.assertEqual(recovery._proc_state(proc.pid, ticks), "dead")


# ------------------------------------------------------------------ execute

class ExecuteTest(RecoveryCase):

    def test_extra_segment_recovery_leaves_session_unchanged(self):
        sid = self.make_session(
            1, occupancy="reserved", lifecycle="starting",
            pane_pid=None, pane_ticks=None, pane_id=None)
        observation = self.preview(1)

        status, body = self.call(
            "POST", "/api/interface/shells/999/1/recovery", (OP, IDEM),
            {"observation_id": observation["observation_id"],
             "mode": "recover"})

        self.assertEqual(status, 404, body)
        self.assertEqual(body["error"]["code"], "no_such_route")
        with contextlib.closing(self.db()) as con:
            session = con.execute(
                "SELECT occupancy, lifecycle, ended_at, end_reason "
                "FROM interface_sessions WHERE session_id=?",
                (sid,)).fetchone()
            generation = con.execute(
                "SELECT ended_at FROM interface_generations "
                "WHERE shell_id=1 AND generation=1").fetchone()
        self.assertEqual(session, ("reserved", "starting", None, None))
        self.assertEqual(generation, (None,))

    def test_recover_closes_everything_atomically(self):
        sid = self.make_session(1, occupancy="reserved", lifecycle="starting",
                                pane_pid=None, pane_ticks=None, pane_id=None,
                                archive_id=10)
        con = self.db()
        con.execute("UPDATE shells SET active_archive_id=10 WHERE shell_id=1")
        con.execute(
            "INSERT INTO interface_writer_leases "
            "(session_id, shell_id, generation, client_id, token_hash) "
            "VALUES (?,1,1,'web-1','th')", (sid,))
        con.execute(
            "INSERT INTO documents (document_id, kind, title) "
            "VALUES (50,'doc','SPRINT: t')")
        con.execute(
            "INSERT INTO sprint_planner_bindings "
            "(sprint_doc_id, planner_shell_id, session_id, shell_id, "
            " generation) VALUES (50,1,?,1,1)", (sid,))
        con.execute(
            "INSERT INTO planner_alerts (session_id, severity, reason, "
            "dedupe_key) VALUES (?,'critical','wake_x','k1')", (sid,))
        con.execute(
            "INSERT INTO shell_messages (from_shell_id, to_shell_id, body) "
            "VALUES (2,1,'hello')")
        con.commit()
        con.close()

        obj = self.preview(1)
        self.assertEqual(obj["classification"], "stale_durable_lock")
        status, result = self.call(
            "POST", "/api/interface/shells/1/recovery", (OP, IDEM),
            {"observation_id": obj["observation_id"], "mode": "recover"})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["availability"], "available")
        self.assertIsNone(result["signaled"])
        self.assertEqual(result["closed"]["session"]["session_id"], sid)
        self.assertEqual(result["closed"]["archive"],
                         {"archive_id": 10, "closed": True})
        self.assertEqual(result["closed"]["binding"],
                         {"binding_id": 1, "released": True})
        self.assertTrue(result["closed"]["alerts_resolved"] >= 1)
        self.assertEqual(result["worktree"], {"preserved": True})
        self.assertEqual(result["unread_messages"], 1)

        con = self.db()
        sess = con.execute(
            "SELECT occupancy, lifecycle, end_reason FROM interface_sessions "
            "WHERE session_id=?", (sid,)).fetchone()
        self.assertEqual(sess, ("ended", "ended", "operator_recovery"))
        self.assertIsNotNone(con.execute(
            "SELECT ended_at FROM interface_generations "
            "WHERE shell_id=1 AND generation=1").fetchone()[0])
        self.assertIsNotNone(con.execute(
            "SELECT revoked_at FROM interface_writer_leases "
            "WHERE session_id=?", (sid,)).fetchone()[0])
        arch = con.execute(
            "SELECT ended_at FROM shell_memory_archives "
            "WHERE archive_id=10").fetchone()[0]
        self.assertIsNotNone(arch)
        self.assertIsNone(con.execute(
            "SELECT active_archive_id FROM shells WHERE shell_id=1"
        ).fetchone()[0])
        self.assertIsNotNone(con.execute(
            "SELECT resolved_at FROM planner_alerts WHERE session_id=?",
            (sid,)).fetchone()[0])
        self.assertIsNotNone(con.execute(
            "SELECT released_at FROM sprint_planner_bindings "
            "WHERE binding_id=1").fetchone()[0])
        # Unread inbox messages remain unread.
        self.assertIsNone(con.execute(
            "SELECT read_at FROM shell_messages WHERE message_id=1"
        ).fetchone()[0])
        con.close()

    def test_stale_observation_on_durable_change(self):
        sid = self.make_session(1, occupancy="reserved", lifecycle="starting",
                                pane_pid=None, pane_ticks=None, pane_id=None)
        obj = self.preview(1)
        self.end_session_rows(sid)  # another client recovers first
        status, err = self.call(
            "POST", "/api/interface/shells/1/recovery", (OP, IDEM),
            {"observation_id": obj["observation_id"], "mode": "recover"})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "recovery_observation_stale")

    def test_expired_observation(self):
        self.make_session(1, occupancy="reserved", lifecycle="starting",
                          pane_pid=None, pane_ticks=None, pane_id=None)
        obj = self.preview(1)
        con = self.db()
        con.execute(
            "UPDATE interface_recovery_observations "
            "SET expires_at='2000-01-01 00:00:00' WHERE observation_id=?",
            (obj["observation_id"],))
        con.commit()
        con.close()
        status, err = self.call(
            "POST", "/api/interface/shells/1/recovery", (OP, IDEM),
            {"observation_id": obj["observation_id"], "mode": "recover"})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "recovery_observation_stale")

    def test_unknown_observation(self):
        status, err = self.call(
            "POST", "/api/interface/shells/1/recovery", (OP, IDEM),
            {"observation_id": "nope", "mode": "recover"})
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "no_such_observation")

    def test_recover_refused_on_verified_live(self):
        proc, ticks = self.child()
        self.make_session(1, pane_pid=proc.pid, pane_ticks=ticks)
        with mock.patch.object(recovery, "_pane_present", return_value=True):
            obj = self.preview(1)
            status, err = self.call(
                "POST", "/api/interface/shells/1/recovery", (OP, IDEM),
                {"observation_id": obj["observation_id"], "mode": "recover"})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "recovery_action_not_legal")
        self.assertEqual(recovery._proc_state(proc.pid, ticks), "alive")

    def test_force_requires_confirmation(self):
        proc, ticks = self.child()
        self.make_session(1, pane_pid=proc.pid, pane_ticks=ticks)
        with mock.patch.object(recovery, "_pane_present", return_value=True):
            obj = self.preview(1)
            status, err = self.call(
                "POST", "/api/interface/shells/1/recovery", (OP, IDEM),
                {"observation_id": obj["observation_id"], "mode": "force"})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "force_confirmation_required")
        self.assertEqual(recovery._proc_state(proc.pid, ticks), "alive")

    def test_force_terminates_verified_live_and_closes(self):
        proc, ticks = self.child()
        sid = self.make_session(1, pane_pid=proc.pid, pane_ticks=ticks,
                                archive_id=10)
        with mock.patch.object(recovery, "_pane_present", return_value=True):
            obj = self.preview(1)
            self.assertEqual(obj["classification"], "verified_live")
            status, result = self.call(
                "POST", "/api/interface/shells/1/recovery", (OP, IDEM),
                {"observation_id": obj["observation_id"], "mode": "force",
                 "confirm_force": True})
        self.assertEqual(status, 200, result)
        self.assertTrue(result["signaled"]["signaled"])
        self.assertEqual(result["closed"]["session"]["end_reason"],
                         "operator_recovery_force")
        proc.wait(timeout=5)
        self.assertEqual(recovery._proc_state(proc.pid, ticks), "dead")
        con = self.db()
        self.assertEqual(con.execute(
            "SELECT occupancy FROM interface_sessions WHERE session_id=?",
            (sid,)).fetchone()[0], "ended")
        con.close()
        self.assertIn(sid, self.runtime.abandoned)

    def test_orphan_recover_signals_then_closes(self):
        proc, ticks = self.child()
        sid = self.make_session(1, pane_pid=proc.pid, pane_ticks=ticks)
        with mock.patch.object(recovery, "_pane_present", return_value=False):
            obj = self.preview(1)
            self.assertEqual(obj["classification"], "exact_idle_orphan")
            status, result = self.call(
                "POST", "/api/interface/shells/1/recovery", (OP, IDEM),
                {"observation_id": obj["observation_id"], "mode": "recover"})
        self.assertEqual(status, 200, result)
        self.assertTrue(result["signaled"]["signaled"])
        proc.wait(timeout=5)
        con = self.db()
        self.assertEqual(con.execute(
            "SELECT occupancy FROM interface_sessions WHERE session_id=?",
            (sid,)).fetchone()[0], "ended")
        con.close()

    def test_process_exit_after_preview_is_stale_not_indeterminate(self):
        # The exact process vanishes (exit, or a pid reused by a stranger)
        # between preview and execute. The durable rows never moved, so the
        # freshness fence is what must catch it — BEFORE any signal.
        proc, ticks = self.child()
        sid = self.make_session(1, pane_pid=proc.pid, pane_ticks=ticks)
        with mock.patch.object(recovery, "_pane_present", return_value=False):
            obj = self.preview(1)
            self.assertEqual(obj["classification"], "exact_idle_orphan")
            with mock.patch.object(recovery, "_proc_state",
                                   return_value="dead") as proc_state, \
                    mock.patch.object(recovery,
                                      "terminate_process_group") as term:
                status, err = self.call(
                    "POST", "/api/interface/shells/1/recovery", (OP, IDEM),
                    {"observation_id": obj["observation_id"],
                     "mode": "recover"})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "recovery_observation_stale")
        self.assertTrue(proc_state.called)  # the fence re-read /proc
        term.assert_not_called()            # and refused before signalling
        con = self.db()
        self.assertEqual(con.execute(
            "SELECT occupancy FROM interface_sessions WHERE session_id=?",
            (sid,)).fetchone()[0], "occupied")
        con.close()

    def test_pane_exit_after_preview_is_stale(self):
        # Pane membership is part of what the operator was shown: a pane that
        # disappears from the tmux server after the preview re-classifies the
        # shell, so the old observation may not act.
        proc, ticks = self.child()
        sid = self.make_session(1, pane_pid=proc.pid, pane_ticks=ticks)
        with mock.patch.object(recovery, "_pane_present", return_value=True):
            obj = self.preview(1)
            self.assertEqual(obj["classification"], "verified_live")
        with mock.patch.object(recovery, "_pane_present", return_value=False), \
                mock.patch.object(recovery,
                                  "terminate_process_group") as term:
            status, err = self.call(
                "POST", "/api/interface/shells/1/recovery", (OP, IDEM),
                {"observation_id": obj["observation_id"], "mode": "force",
                 "confirm_force": True})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "recovery_observation_stale")
        term.assert_not_called()
        self.assertEqual(recovery._proc_state(proc.pid, ticks), "alive")
        con = self.db()
        self.assertEqual(con.execute(
            "SELECT occupancy FROM interface_sessions WHERE session_id=?",
            (sid,)).fetchone()[0], "occupied")
        con.close()

    def test_closure_refused_when_absence_unproven(self):
        # A signal was sent but /proc never proved the process gone (an
        # unkillable D-state survivor): closure is refused with a named
        # next action and NO durable state is touched.
        proc, ticks = self.child()
        sid = self.make_session(1, pane_pid=proc.pid, pane_ticks=ticks)
        with mock.patch.object(recovery, "_pane_present", return_value=False):
            obj = self.preview(1)
            self.assertEqual(obj["classification"], "exact_idle_orphan")
            with mock.patch.object(
                    recovery, "terminate_process_group",
                    return_value={"signaled": True, "dead": False,
                                  "escalated": True, "pid": proc.pid,
                                  "pgid": proc.pid,
                                  "reason": "absence_unproven",
                                  "detail": "survives SIGKILL"}):
                status, err = self.call(
                    "POST", "/api/interface/shells/1/recovery", (OP, IDEM),
                    {"observation_id": obj["observation_id"],
                     "mode": "recover"})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "recovery_absence_unproven")
        self.assertIn("preview again", err["error"]["message"])
        con = self.db()
        self.assertEqual(con.execute(
            "SELECT occupancy FROM interface_sessions WHERE session_id=?",
            (sid,)).fetchone()[0], "occupied")
        con.close()

    def test_idempotent_replay_no_second_side_effect(self):
        proc, ticks = self.child()
        self.make_session(1, pane_pid=proc.pid, pane_ticks=ticks)
        with mock.patch.object(recovery, "_pane_present", return_value=True):
            obj = self.preview(1)
            status1, r1 = self.call(
                "POST", "/api/interface/shells/1/recovery", (OP, IDEM),
                {"observation_id": obj["observation_id"], "mode": "force",
                 "confirm_force": True})
            self.assertEqual(status1, 200, r1)
            # Same key + same body: the STORED response replays — no second
            # signal attempt against the now-dead process.
            status2, r2 = self.call(
                "POST", "/api/interface/shells/1/recovery", (OP, IDEM),
                {"observation_id": obj["observation_id"], "mode": "force",
                 "confirm_force": True})
        self.assertEqual(status2, 200)
        self.assertEqual(r1, r2)

    def test_ambiguous_binding_parked_with_named_action(self):
        sid = self.make_session(1, occupancy="reserved", lifecycle="starting",
                                pane_pid=None, pane_ticks=None, pane_id=None)
        con = self.db()
        con.execute(
            "INSERT INTO documents (document_id, kind, title) "
            "VALUES (50,'doc','SPRINT: t')")
        # A binding on a DIFFERENT (earlier) generation: not owned by this
        # recovery — parked, never force-released.
        con.execute(
            "INSERT INTO interface_generations "
            "(shell_id, generation, hook_token_hash, ended_at) "
            "VALUES (1,99,'h','2026-01-01')")
        con.execute(
            "INSERT INTO sprint_planner_bindings "
            "(sprint_doc_id, planner_shell_id, session_id, shell_id, "
            " generation) VALUES (50,1,?,1,99)", (sid,))
        con.commit()
        con.close()
        obj = self.preview(1)
        status, result = self.call(
            "POST", "/api/interface/shells/1/recovery", (OP, IDEM),
            {"observation_id": obj["observation_id"], "mode": "recover"})
        self.assertEqual(status, 200, result)
        self.assertIsNone(result["closed"]["binding"])
        self.assertEqual(result["closed"]["parked"][0]["binding_id"], 1)
        self.assertIn("release", result["closed"]["parked"][0]["next_action"])
        con = self.db()
        self.assertIsNone(con.execute(
            "SELECT released_at FROM sprint_planner_bindings "
            "WHERE binding_id=1").fetchone()[0])
        alert = con.execute(
            "SELECT reason FROM planner_alerts WHERE binding_id=1 AND "
            "resolved_at IS NULL").fetchone()
        self.assertIn("recovery_ambiguous_binding", alert[0])
        con.close()

    def test_missing_idempotency_key(self):
        obj = self.preview(1)
        status, err = self.call(
            "POST", "/api/interface/shells/1/recovery", (OP,),
            {"observation_id": obj["observation_id"], "mode": "recover"})
        self.assertEqual(status, 422)
        self.assertEqual(err["error"]["code"], "idempotency_key_required")

    def test_no_such_shell(self):
        status, err = self.call("GET",
                                "/api/interface/shells/99/recovery", (OP,))
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "no_such_shell")


# ------------------------------------------------------------------ worktree

class WorktreeTest(RecoveryCase):

    def make_git_worktree(self, *, unpushed: bool = False) -> str:
        """A real git repo standing in for the shell worktree, with a bare
        origin so 'unpushed' is exact: one pushed commit, one dirty tracked
        change, one untracked file (+ one local-only commit when unpushed).
        `clean.txt` is committed and left clean so a test can dirty it AFTER
        the preview."""
        origin = Path(self.tmp.name) / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)],
                       check=True, capture_output=True)
        wt = Path(self.tmp.name) / f"wt-{len(list(Path(self.tmp.name).glob('wt-*')))}"
        wt.mkdir()

        def git(*args):
            return subprocess.run(["git", "-C", str(wt), *args],
                                  capture_output=True, text=True, check=True)
        git("init", "-q", "-b", "feat/x")
        git("config", "user.email", "t@t")
        git("config", "user.name", "t")
        git("remote", "add", "origin", str(origin))
        (wt / "tracked.txt").write_text("v1")
        (wt / "clean.txt").write_text("c1")
        git("add", ".")
        git("commit", "-qm", "base")
        git("push", "-q", "-u", "origin", "feat/x")
        if unpushed:
            (wt / "tracked.txt").write_text("v2")
            git("commit", "-qam", "local only")
        (wt / "tracked.txt").write_text("dirty")
        (wt / "untracked.txt").write_text("new")
        return str(wt)

    def session_with_worktree(self, wt: str) -> int:
        return self.make_session(
            1, occupancy="reserved", lifecycle="starting", pane_pid=None,
            pane_ticks=None, pane_id=None, worktree=wt)

    def post(self, obj, **extra):
        body = {"observation_id": obj["observation_id"], "mode": "recover"}
        body.update(extra)
        return self.call("POST", "/api/interface/shells/1/recovery",
                         (OP, IDEM), body)

    def test_default_preserves_worktree(self):
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        obj = self.preview(1)
        self.assertEqual(obj["evidence"]["git"]["dirty_tracked"], 1)
        self.assertEqual(obj["evidence"]["git"]["untracked"], 1)
        status, result = self.post(obj)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["worktree"], {"preserved": True})
        self.assertEqual((Path(wt) / "tracked.txt").read_text(), "dirty")
        self.assertTrue((Path(wt) / "untracked.txt").exists())

    def test_discard_requires_typed_shortname(self):
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        obj = self.preview(1)
        status, err = self.post(obj, preserve_worktree=False,
                                discard_worktree=True,
                                confirm_shortname="WRONG")
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "discard_confirmation_required")
        self.assertEqual((Path(wt) / "tracked.txt").read_text(), "dirty")

    def test_discard_never_implied_by_preserve_default(self):
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        obj = self.preview(1)
        status, _err = self.post(obj, discard_worktree=True,
                                 confirm_shortname="s1")
        self.assertEqual(status, 422)  # preserve_worktree defaults true        self.assertEqual((Path(wt) / "tracked.txt").read_text(), "dirty")

    def test_discard_refuses_unpushed_commits(self):
        wt = self.make_git_worktree(unpushed=True)
        self.session_with_worktree(wt)
        obj = self.preview(1)
        self.assertEqual(obj["evidence"]["git"]["unpushed_commits"], 1)
        status, err = self.post(obj, preserve_worktree=False,
                                discard_worktree=True, confirm_shortname="s1")
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "unpushed_commits")
        # Nothing discarded; the local commit and dirty files survive.
        self.assertEqual((Path(wt) / "tracked.txt").read_text(), "dirty")
        self.assertTrue((Path(wt) / "untracked.txt").exists())

    def test_discard_removes_changes_keeps_worktree(self):
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        obj = self.preview(1)
        status, result = self.post(obj, preserve_worktree=False,
                                   discard_worktree=True,
                                   confirm_shortname="s1")
        self.assertEqual(status, 200, result)
        self.assertTrue(result["worktree"]["discarded"])
        self.assertEqual((Path(wt) / "tracked.txt").read_text(), "v1")
        self.assertFalse((Path(wt) / "untracked.txt").exists())
        self.assertTrue(Path(wt).is_dir())  # worktree itself never deleted

    def assert_nothing_discarded(self, wt: str) -> None:
        """No reset, no clean, no closure — the previewed state is intact."""
        self.assertEqual((Path(wt) / "tracked.txt").read_text(), "dirty")
        self.assertTrue((Path(wt) / "untracked.txt").exists())
        con = self.db()
        try:
            self.assertEqual(con.execute(
                "SELECT occupancy FROM interface_sessions WHERE shell_id=1"
            ).fetchone()[0], "reserved")
        finally:
            con.close()

    def test_untracked_file_after_preview_refuses_discard(self):
        # New work appears between the preview and the confirmed discard: the
        # operator confirmed erasing what the preview listed, not this.
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        obj = self.preview(1)
        self.assertEqual(obj["evidence"]["git"]["untracked"], 1)
        (Path(wt) / "late.txt").write_text("work written after the preview")
        status, err = self.post(obj, preserve_worktree=False,
                                discard_worktree=True, confirm_shortname="s1")
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "recovery_observation_stale")
        self.assertEqual((Path(wt) / "late.txt").read_text(),
                         "work written after the preview")
        self.assert_nothing_discarded(wt)

    def test_tracked_change_after_preview_refuses_discard(self):
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        obj = self.preview(1)
        self.assertEqual(obj["evidence"]["git"]["dirty_tracked"], 1)
        (Path(wt) / "clean.txt").write_text("edited after the preview")
        status, err = self.post(obj, preserve_worktree=False,
                                discard_worktree=True, confirm_shortname="s1")
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "recovery_observation_stale")
        self.assertEqual((Path(wt) / "clean.txt").read_text(),
                         "edited after the preview")
        self.assert_nothing_discarded(wt)

    def test_equal_count_churn_after_preview_refuses_discard(self):
        # One file cleaned, another dirtied: dirty_tracked/untracked counts
        # are unchanged, but it is not the same work — the digest catches it.
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        obj = self.preview(1)
        (Path(wt) / "tracked.txt").write_text("v1")  # back to HEAD: clean
        (Path(wt) / "clean.txt").write_text("newly dirty")
        status, err = self.post(obj, preserve_worktree=False,
                                discard_worktree=True, confirm_shortname="s1")
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "recovery_observation_stale")
        self.assertEqual((Path(wt) / "clean.txt").read_text(), "newly dirty")
        self.assertTrue((Path(wt) / "untracked.txt").exists())

    def test_discard_git_failure_reports_exactly_what_completed(self):
        # clean fails AFTER reset succeeded and the closure committed: the
        # response stays 200 and names the completed/failed steps — never a
        # bare 500 that hides a partial discard.
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        obj = self.preview(1)
        real_run = recovery.subprocess.run

        def flaky_clean(*args, **kwargs):
            if "clean" in args[0]:
                return subprocess.CompletedProcess(
                    args=args[0], returncode=1, stdout="",
                    stderr="fatal: clean boom")
            return real_run(*args, **kwargs)

        with mock.patch.object(recovery.subprocess, "run",
                               side_effect=flaky_clean):
            status, result = self.post(obj, preserve_worktree=False,
                                       discard_worktree=True,
                                       confirm_shortname="s1")
        self.assertEqual(status, 200, result)
        wt_result = result["worktree"]
        self.assertFalse(wt_result["discarded"])
        self.assertEqual(wt_result["completed"], ["reset"])
        self.assertEqual(wt_result["failed"]["step"], "clean")
        self.assertIn("clean boom", wt_result["failed"]["error"])
        # reset ran (tracked restored), clean did not (untracked survives),
        # and the durable closure still committed.
        self.assertEqual((Path(wt) / "tracked.txt").read_text(), "v1")
        self.assertTrue((Path(wt) / "untracked.txt").exists())
        con = self.db()
        self.assertEqual(con.execute(
            "SELECT occupancy FROM interface_sessions WHERE shell_id=1"
        ).fetchone()[0], "ended")
        con.close()

    def test_discard_git_timeout_reported_not_raised(self):
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        obj = self.preview(1)
        real_run = recovery.subprocess.run

        def timeout_reset(*args, **kwargs):
            if "reset" in args[0]:
                raise subprocess.TimeoutExpired(cmd=args[0], timeout=60)
            return real_run(*args, **kwargs)

        with mock.patch.object(recovery.subprocess, "run",
                               side_effect=timeout_reset):
            status, result = self.post(obj, preserve_worktree=False,
                                       discard_worktree=True,
                                       confirm_shortname="s1")
        self.assertEqual(status, 200, result)
        self.assertFalse(result["worktree"]["discarded"])
        self.assertEqual(result["worktree"]["completed"], [])
        self.assertEqual(result["worktree"]["failed"]["step"], "reset")


# ------------------------------------------------------------------ CLI parity

class CliRecoverTest(unittest.TestCase):
    """The verb end-to-end against the fake transport: request shape,
    confirmation gating, refusal mapping, --json output."""

    PREVIEW_ORPHAN: ClassVar = {
        "observation_id": "obs-1", "expires_in_s": 120,
        "classification": "exact_idle_orphan", "legal_actions": ["recover"],
        "evidence": {
            "shell": {"shell_id": 3, "shortname": "S3",
                      "active_archive_id": None},
            "session": {"session_id": 9, "generation": 1,
                        "occupancy": "occupied", "lifecycle": "lost",
                        "harness": "claude", "worktree": "/x/s3",
                        "archive_id": None, "created_at": "t"},
            "generation": None, "archive": None, "sprint_binding": None,
            "process": {"pane_id": "%1", "pane_pid": 4321,
                        "pane_start_ticks": 999, "pane_present": False,
                        "pid_state": "alive", "pgid": 4321},
            "tmux": None, "unread_messages": 2,
            "git": {"worktree": "/x/s3", "branch": "fix/x",
                    "dirty_tracked": 1, "untracked": 0,
                    "unpushed_commits": 0},
            "live_session": True}}

    PREVIEW_LIVE: ClassVar = dict(PREVIEW_ORPHAN,
                                  classification="verified_live",
                                  legal_actions=["force"])

    RESULT: ClassVar = {"shell_id": 3, "shortname": "S3",
              "classification": "exact_idle_orphan", "mode": "recover",
              "signaled": {"signaled": True, "escalated": False, "pid": 4321,
                           "pgid": 4321},
              "closed": {"session": {"session_id": 9,
                                     "end_reason": "operator_recovery",
                                     "already_ended": False},
                         "archive": None, "alerts_resolved": 0,
                         "binding": None, "parked": []},
              "worktree": {"preserved": True}, "unread_messages": 2,
              "availability": "available"}

    def setUp(self):
        self.requests = []
        self.routes = {}

        def fake_http(req):
            path = req.full_url.replace(ic.API_BASE, "")
            body = json.loads(req.data) if req.data else None
            self.requests.append((req.get_method(), path, body,
                                  dict(req.headers)))
            key = (req.get_method(), path)
            responder = self.routes.get(key)
            if responder is None:
                raise AssertionError(f"unexpected request: {key}")
            outcome = responder.pop(0) if isinstance(responder, list) \
                else responder
            if isinstance(outcome, urllib.error.HTTPError):
                raise outcome
            return FakeResp(outcome)

        self.patch_http = mock.patch.object(ic, "_http", side_effect=fake_http)
        self.patch_http.start()
        self.patch_token = mock.patch.object(ic, "_operator_token",
                                             return_value="optok")
        self.patch_token.start()
        # Non-tty by default: confirmations must refuse, never hang.
        self.patch_stdin = mock.patch.object(ic.sys, "stdin",
                                             io.StringIO(""))
        self.patch_stdin.start()
        self.routes[("GET", "/api/interface/shells")] = SHELLS

    def tearDown(self):
        self.patch_stdin.stop()
        self.patch_token.stop()
        self.patch_http.stop()

    def script(self, preview, result):
        preview = dict(preview)
        preview["evidence_projection"] = recovery.evidence_projection(
            preview["evidence"], preview["classification"],
            preview["legal_actions"])
        self.routes[("GET", "/api/interface/shells/3/recovery")] = preview
        if result is not None:
            self.routes[("POST", "/api/interface/shells/3/recovery")] = result

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            try:
                code = ic.main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_recover_happy_path(self):
        self.script(dict(self.PREVIEW_ORPHAN), dict(self.RESULT))
        code, out, _ = self.run_cli(["recover", "s3", "--yes"])
        self.assertEqual(code, 0)
        get = self.requests[1]
        post = self.requests[2]
        self.assertEqual(get[0], "GET")
        self.assertEqual(post[0], "POST")
        self.assertEqual(post[1], "/api/interface/shells/3/recovery")
        self.assertEqual(post[2], {"observation_id": "obs-1",
                                   "mode": "recover",
                                   "preserve_worktree": True})
        header_keys = {k.lower() for k in post[3]}
        self.assertIn("idempotency-key", header_keys)
        self.assertIn("authorization", header_keys)
        self.assertIn("classification: exact_idle_orphan", out)
        self.assertIn("S3 is available", out)

    def test_force_confirms_exact_identity(self):
        self.script(dict(self.PREVIEW_LIVE), dict(self.RESULT, mode="force"))
        code, _out, _err = self.run_cli(["recover", "s3", "--force", "--yes"])
        self.assertEqual(code, 0)
        body = self.requests[2][2]
        self.assertEqual(body["mode"], "force")
        self.assertIs(body["confirm_force"], True)

    def test_force_without_yes_refuses_off_tty(self):
        self.script(dict(self.PREVIEW_LIVE), None)
        code, _out, err = self.run_cli(["recover", "s3", "--force"])
        self.assertEqual(code, 1)
        self.assertIn("--yes", err)
        self.assertEqual(len(self.requests), 2)  # no POST was sent

    def test_discard_sends_independent_confirmation(self):
        self.script(dict(self.PREVIEW_ORPHAN), dict(self.RESULT))
        code, _out, _ = self.run_cli(
            ["recover", "s3", "--discard-worktree", "--yes"])
        self.assertEqual(code, 0)
        body = self.requests[2][2]
        self.assertIs(body["discard_worktree"], True)
        self.assertIs(body["preserve_worktree"], False)
        self.assertEqual(body["confirm_shortname"], "S3")

    def test_stale_observation_maps_to_refusal(self):
        from test_interface_cli import http_error
        preview = dict(self.PREVIEW_ORPHAN)
        preview["evidence_projection"] = recovery.evidence_projection(
            preview["evidence"], preview["classification"],
            preview["legal_actions"])
        self.routes[("GET", "/api/interface/shells/3/recovery")] = preview
        self.routes[("POST", "/api/interface/shells/3/recovery")] = \
            http_error(409, "recovery_observation_stale", "changed")
        code, _out, err = self.run_cli(["recover", "s3", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("recovery_observation_stale", err)
        self.assertIn("preview again", err)

    def test_available_shell_no_post(self):
        self.script(dict(self.PREVIEW_ORPHAN, classification="available",
                         legal_actions=[]), None)
        code, out, _ = self.run_cli(["recover", "s3", "--yes"])
        self.assertEqual(code, 0)
        self.assertIn("nothing to recover", out)
        self.assertEqual(len(self.requests), 2)

    def test_json_output(self):
        self.script(dict(self.PREVIEW_ORPHAN), dict(self.RESULT))
        code, out, _ = self.run_cli(["recover", "s3", "--yes", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["preview"]["observation_id"], "obs-1")
        self.assertEqual(payload["result"]["availability"], "available")


if __name__ == "__main__":
    unittest.main()
