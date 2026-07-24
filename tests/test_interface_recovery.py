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
- freshness at the DESTRUCTIVE COMMIT, not merely at execute entry: work
  written while the preconditions run refuses before anything happens, and
  work written while the shell shuts down (after the fence, during SIGTERM)
  refuses the discard with nothing reset or cleaned — the refusal naming the
  signal and closure it cannot unwind;
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
import errno
import io
import json
import os
import shutil
import signal
import sqlite3
import stat
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

    def make_git_worktree(self, *, unpushed: bool = False,
                          links: bool = False) -> str:
        """A real git repo standing in for the shell worktree, with a bare
        origin so 'unpushed' is exact: one pushed commit, one dirty tracked
        change, one untracked file (+ one local-only commit when unpushed).
        `clean.txt` is committed and left clean so a test can dirty it AFTER
        the preview.

        `links=True` adds the entity-identity fixture: `a/b/c.txt` all hold
        the SAME bytes and `dirty_target.txt` holds the same bytes as dirty
        `tracked.txt`, so every post-preview mutation below can be made
        without the *resolved* content ever changing — a digest that reads
        through a link, or ignores type, cannot tell the states apart.
        Pre-dirtied: `tlink` (tracked symlink, retargeted a->b), `ulink`
        (untracked symlink -> b), `ufile.txt` (untracked regular file)."""
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
        if links:
            for name in ("a.txt", "b.txt", "c.txt"):
                (wt / name).write_text("same")
            (wt / "dirty_target.txt").write_text("dirty")
            (wt / "tlink").symlink_to("a.txt")
        git("add", ".")
        git("commit", "-qm", "base")
        git("push", "-q", "-u", "origin", "feat/x")
        if unpushed:
            (wt / "tracked.txt").write_text("v2")
            git("commit", "-qam", "local only")
        (wt / "tracked.txt").write_text("dirty")
        (wt / "untracked.txt").write_text("new")
        if links:
            (wt / "tlink").unlink()
            (wt / "tlink").symlink_to("b.txt")     # tracked, now dirty
            (wt / "ulink").symlink_to("b.txt")     # untracked symlink
            (wt / "ufile.txt").write_text("dirty")  # untracked regular file
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

    def plan(self, wt: str) -> dict:
        """The enumerated discard set for `wt`, as the final gate hands it to
        `_discard_worktree_files`."""
        return recovery._observe_stable(wt)[1]

    def porcelain(self, wt: str) -> str:
        return subprocess.run(["git", "-C", wt, "status", "--porcelain"],
                              capture_output=True, text=True,
                              check=True).stdout

    def assert_mutation_refuses(self, wt, mutate, *,
                                porcelain_stable: bool = True):
        """Mutate the worktree AFTER the preview, then confirm the discard:
        the fence must refuse before anything runs.

        `porcelain_stable` pins the regression that makes the case worth
        testing — the status lines stay byte-identical, so any digest built
        from them alone still reads fresh while the work underneath moved.
        Set it False only where git's own line set already reflects the
        change (a TRACKED typechange reports ' T'); the refusal is still
        required, it just is not the line set that is being trusted."""
        obj = self.preview(1)
        before = self.porcelain(wt)
        mutate()
        if porcelain_stable:
            self.assertEqual(self.porcelain(wt), before)
        with mock.patch.object(
                recovery, "_discard_worktree_files",
                return_value={"worktree": wt, "discarded": True,
                              "completed": ["reset", "clean"],
                              "failed": None}) as disc, \
                mock.patch.object(recovery,
                                  "terminate_process_group") as term:
            status, err = self.post(obj, preserve_worktree=False,
                                    discard_worktree=True,
                                    confirm_shortname="s1")
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "recovery_observation_stale")
        disc.assert_not_called()   # no reset, no clean
        term.assert_not_called()   # no signal
        con = self.db()
        try:  # no closure
            self.assertEqual(con.execute(
                "SELECT occupancy FROM interface_sessions WHERE shell_id=1"
            ).fetchone()[0], "reserved")
        finally:
            con.close()

    def assert_content_edit_refuses(self, wt, rel: str, text: str):
        """Rewrite `rel` after the preview, path list untouched."""
        self.assert_mutation_refuses(
            wt, lambda: (Path(wt) / rel).write_text(text))
        self.assertEqual((Path(wt) / rel).read_text(), text)

    def retarget(self, wt, rel: str, target: str):
        """Repoint an existing symlink — the link's own identity changes, the
        bytes it resolves to do not."""
        link = Path(wt) / rel
        self.assertTrue(link.is_symlink())
        resolved_before = link.read_text()

        def mutate():
            link.unlink()
            link.symlink_to(target)
        self.assert_mutation_refuses(wt, mutate)
        self.assertEqual(os.readlink(link), target)
        self.assertEqual(link.read_text(), resolved_before)  # content equal

    def test_tracked_content_edit_after_preview_refuses_discard(self):
        # Same already-dirty tracked path, new contents: SC-086 — the operator
        # confirmed erasing what the preview showed, not this.
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        self.assert_content_edit_refuses(wt, "tracked.txt",
                                         "rewritten after the preview")

    def test_untracked_content_edit_after_preview_refuses_discard(self):
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        self.assert_content_edit_refuses(wt, "untracked.txt",
                                         "rewritten after the preview")

    def test_same_size_content_edit_after_preview_refuses_discard(self):
        # "dirty" -> "drity": identical length, so a size/mtime-based digest
        # would miss it. The fence is bound to the bytes.
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        self.assertEqual((Path(wt) / "tracked.txt").read_text(), "dirty")
        self.assert_content_edit_refuses(wt, "tracked.txt", "drity")

    # -- entity identity: link vs target, and type transitions (SC-086) ----
    # Every case below leaves the *resolved* bytes identical, so a digest
    # that reads through a link or ignores an entity's type reads fresh.

    def test_tracked_symlink_retarget_after_preview_refuses_discard(self):
        # An already-dirty tracked symlink repointed b.txt -> c.txt. Both
        # targets hold "same", so hashing what the link resolves to cannot
        # see it; git reports ' M tlink' either way. Only the link's own
        # target string moved — and a discard would reset it.
        wt = self.make_git_worktree(links=True)
        self.session_with_worktree(wt)
        self.retarget(wt, "tlink", "c.txt")

    def test_untracked_symlink_retarget_after_preview_refuses_discard(self):
        # Same defect on an untracked link: '?? ulink' is fixed, the
        # resolved bytes are equal, and a discard would clean it away.
        wt = self.make_git_worktree(links=True)
        self.session_with_worktree(wt)
        self.retarget(wt, "ulink", "c.txt")

    def test_untracked_file_becoming_symlink_refuses_discard(self):
        # Regular file -> symlink resolving to identical bytes. Untracked, so
        # porcelain shows '?? ufile.txt' before and after: git's line set
        # cannot distinguish the types at all — only the digest's type
        # prefix can.
        wt = self.make_git_worktree(links=True)
        self.session_with_worktree(wt)
        path = Path(wt) / "ufile.txt"
        self.assertEqual(path.read_text(), "dirty")

        def mutate():
            path.unlink()
            path.symlink_to("dirty_target.txt")
        self.assert_mutation_refuses(wt, mutate)
        self.assertTrue(path.is_symlink())
        self.assertEqual(path.read_text(), "dirty")  # content equal

    def test_untracked_symlink_becoming_file_refuses_discard(self):
        # The reverse transition, same invariant.
        wt = self.make_git_worktree(links=True)
        self.session_with_worktree(wt)
        path = Path(wt) / "ulink"
        self.assertEqual(path.read_text(), "same")

        def mutate():
            path.unlink()
            path.write_text("same")
        self.assert_mutation_refuses(wt, mutate)
        self.assertFalse(path.is_symlink())
        self.assertEqual(path.read_text(), "same")

    def test_tracked_file_becoming_symlink_refuses_discard(self):
        # Tracked typechange: git DOES move the line (' M' -> ' T'), so the
        # line set already fences this one — pinned anyway so the refusal
        # survives a future digest that stops reading porcelain.
        wt = self.make_git_worktree(links=True)
        self.session_with_worktree(wt)
        path = Path(wt) / "tracked.txt"

        def mutate():
            path.unlink()
            path.symlink_to("dirty_target.txt")
        self.assert_mutation_refuses(wt, mutate, porcelain_stable=False)
        self.assertTrue(path.is_symlink())
        self.assertEqual(path.read_text(), "dirty")

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

    # -- the digest binds every attribute a discard REWRITES (SC-090) ------

    def mode_of(self, path: Path) -> int:
        return stat.S_IMODE(os.lstat(path).st_mode)

    def test_discard_destroys_permissions(self):
        # The premise, reproduced rather than argued: `reset --hard` does not
        # edit a dirty file in place — it recreates it from the index, so the
        # mode comes back umask-derived and a tightened one is simply gone.
        # Permissions are work a discard destroys; that is why they are bound.
        wt = self.make_git_worktree()
        path = Path(wt) / "tracked.txt"
        os.chmod(path, 0o640)
        recovery._discard_worktree_files(wt, self.plan(wt))
        self.assertNotEqual(
            self.mode_of(path), 0o640,
            "reset --hard left the mode intact — premise of SC-090 changed")

    def test_mode_tightening_after_preview_refuses_discard(self):
        # SC-090: same bytes, same porcelain, tighter permissions. Git records
        # only the exec bit, but a discard rewrites ALL of them, so the digest
        # must move on any of them.
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        path = Path(wt) / "tracked.txt"
        os.chmod(path, 0o640)
        self.assert_mutation_refuses(wt, lambda: os.chmod(path, 0o600))
        self.assertEqual(self.mode_of(path), 0o600)
        self.assertEqual(path.read_text(), "dirty")

    def test_untracked_mode_change_after_preview_refuses_discard(self):
        # `clean -fd` deletes untracked files outright, so their mode is work
        # a discard destroys just as completely.
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        path = Path(wt) / "untracked.txt"
        os.chmod(path, 0o644)
        self.assert_mutation_refuses(wt, lambda: os.chmod(path, 0o600))
        self.assertEqual(self.mode_of(path), 0o600)

    @unittest.skipUnless(os.geteuid() == 0, "chown requires root")
    def test_owner_change_after_preview_refuses_discard(self):
        # Same recreate-from-index step hands the file to whoever runs the
        # recovery, so ownership is destroyed by a discard too.
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        path = Path(wt) / "tracked.txt"
        self.assert_mutation_refuses(wt, lambda: os.chown(path, 1, 1))
        self.assertEqual(os.lstat(path).st_uid, 1)

    def test_empty_untracked_dir_after_preview_refuses_discard(self):
        # `clean -fd` removes untracked DIRECTORIES, and an empty one is named
        # by neither porcelain (git ignores empty dirs) nor a file listing —
        # the digest enumerates directories in their own right.
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        late = Path(wt) / "late_dir"
        self.assert_mutation_refuses(wt, late.mkdir)
        self.assertTrue(late.is_dir())

    # -- incomplete evidence REFUSES, it never degrades (SC-087) -----------

    def assert_gap_refuses_discard_but_frees_the_shell(self, wt, patcher=None):
        """A gap in the worktree evidence must refuse the DISCARD at execute —
        and must NOT hold the shell hostage.

        The danger is precisely that a gap is DETERMINISTIC: the same
        undecodable output or unreadable entry yields the same absent facts at
        preview and at execute, so it compares EQUAL and would ride through as
        'nothing changed'. Both the preview and the execute run under the
        patch here for exactly that reason.

        The gap gates the DISCARD and nothing else (SC-106). A plain recovery
        touches no file, so unreadable files tell it nothing — and refusing it
        left a shell whose lock was already proven absent stranded, which
        inverts what a recovery is for. So this asserts BOTH halves: the
        discard refuses, and the same broken worktree still frees the shell
        with every file untouched.
        """
        with patcher or contextlib.nullcontext():
            obj = self.preview(1)
            self.assertIn("indeterminate", obj["evidence"]["git"])
            rows = {r["key"]: r["value"] for r in obj["evidence_projection"]}
            self.assertIn("could not be observed completely", rows["worktree"])
            with mock.patch.object(recovery,
                                   "_discard_worktree_files") as disc, \
                    mock.patch.object(recovery,
                                      "terminate_process_group") as term:
                status, err = self.post(obj, preserve_worktree=False,
                                        discard_worktree=True,
                                        confirm_shortname="s1")
                self.assertEqual(status, 409, err)
                self.assertEqual(err["error"]["code"],
                                 "recovery_observation_stale")
                self.assert_nothing_discarded(wt)   # not even a closure
                # ...and now the core path, on the same broken worktree.
                # (Fresh idempotency key — a different body under the old one
                # is a conflict.)
                plain_status, plain = self.call(
                    "POST", "/api/interface/shells/1/recovery",
                    (OP, "Idempotency-Key: k-2"),
                    {"observation_id": obj["observation_id"],
                     "mode": "recover"})
        self.assertEqual(plain_status, 200, plain)
        self.assertEqual(plain["availability"], "available")
        self.assertEqual(plain["worktree"], {"preserved": True})
        self.assertTrue(plain["closed"]["session"]["session_id"])
        disc.assert_not_called()   # nothing removed, nothing restored
        term.assert_not_called()   # a stale lock has no process to signal
        # the lock is gone AND every file is exactly where it was
        con = self.db()
        try:
            self.assertEqual(con.execute(
                "SELECT occupancy FROM interface_sessions WHERE shell_id=1"
            ).fetchone()[0], "ended")
        finally:
            con.close()
        self.assertEqual((Path(wt) / "tracked.txt").read_text(), "dirty")
        self.assertTrue((Path(wt) / "untracked.txt").exists())

    def test_corrupt_git_still_unstrands_an_absence_proved_lock(self):
        # SC-106, the core objective end to end: a stale durable lock with no
        # process identity — absence already proven — and a repository that
        # cannot be read at all. Eight cycles of hardening the OPTIONAL discard
        # had made worktree evidence a precondition of the closure itself, so
        # a corrupt `.git` left this shell reported busy forever with no file
        # operation ever requested. Unstranding is priority one; the files are
        # protected by not touching them, not by refusing to help.
        wt = self.make_git_worktree()
        (Path(wt) / ".git" / "HEAD").write_text("not a ref\n")
        self.session_with_worktree(wt)
        obj = self.preview(1)
        self.assertEqual(obj["classification"], "stale_durable_lock")
        self.assertIn("recover", obj["legal_actions"])
        self.assertIn("indeterminate", obj["evidence"]["git"])

        status, result = self.post(obj)          # the default: preserve
        self.assertEqual(status, 200, result)
        self.assertEqual(result["availability"], "available")
        self.assertEqual(result["worktree"], {"preserved": True})
        self.assertIsNone(result["signaled"])    # nothing to signal
        # the lock is actually gone — not merely reported closed
        con = self.db()
        try:
            self.assertEqual(con.execute(
                "SELECT occupancy FROM interface_sessions WHERE shell_id=1"
            ).fetchone()[0], "ended")
        finally:
            con.close()
        # and every file survived, dirty and untracked alike
        self.assertEqual((Path(wt) / "tracked.txt").read_text(), "dirty")
        self.assertEqual((Path(wt) / "untracked.txt").read_text(), "new")

    def test_worktree_change_after_preview_does_not_block_plain_recovery(self):
        # The milder half of the same coupling: the shell keeps writing while
        # it is stranded, so its worktree moves between preview and execute.
        # Nothing is being destroyed, so that must not refuse — while the
        # process/pane and durable evidence still gate every recovery.
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        obj = self.preview(1)
        late = Path(wt) / "written_while_stranded.txt"
        late.write_text("the shell was still writing")
        (Path(wt) / "tracked.txt").write_text("and editing")

        status, result = self.post(obj)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["availability"], "available")
        self.assertEqual(late.read_text(), "the shell was still writing")
        self.assertEqual((Path(wt) / "tracked.txt").read_text(), "and editing")

    def test_non_utf8_path_is_observed_not_degraded(self):
        # A valid non-UTF-8 filename is worktree STATE, not a failure. Reading
        # git's NUL-delimited output as strict text raised on it and collapsed
        # the whole observation to 'no facts' — which then compared equal at
        # execute and let a discard run over post-preview work (SC-087).
        wt = self.make_git_worktree()
        (Path(wt) / os.fsdecode(b"caf\xe9.txt")).write_bytes(b"latin-1 name")
        self.session_with_worktree(wt)
        obj = self.preview(1)
        self.assertNotIn("indeterminate", obj["evidence"]["git"])
        self.assertEqual(obj["evidence"]["git"]["untracked"], 2)
        # ...and the fence still fences: unrelated tracked work written after
        # the preview refuses the confirmed discard.
        (Path(wt) / "clean.txt").write_text("work written after the preview")
        status, err = self.post(obj, preserve_worktree=False,
                                discard_worktree=True, confirm_shortname="s1")
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "recovery_observation_stale")
        self.assertEqual((Path(wt) / "clean.txt").read_text(),
                         "work written after the preview")
        self.assert_nothing_discarded(wt)

    def test_git_command_failure_refuses(self):
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        real = recovery._git_out

        def failing(worktree, *args, **kwargs):
            if args[0] == "status":
                raise recovery._GitEvidenceUnavailable("git status: exit 128")
            return real(worktree, *args, **kwargs)

        self.assert_gap_refuses_discard_but_frees_the_shell(
            wt, mock.patch.object(recovery, "_git_out", failing))

    def test_git_timeout_refuses(self):
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        real = recovery.subprocess.run

        def timing_out(cmd, *args, **kwargs):
            if cmd[0] == "git" and "ls-files" in cmd:
                raise subprocess.TimeoutExpired(cmd, 30)
            return real(cmd, *args, **kwargs)

        self.assert_gap_refuses_discard_but_frees_the_shell(
            wt, mock.patch.object(recovery.subprocess, "run", timing_out))

    def test_unreadable_entry_refuses(self):
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        real = os.lstat

        def denied(path, *args, **kwargs):
            if str(path).endswith("untracked.txt"):
                raise PermissionError(13, "permission denied")
            return real(path, *args, **kwargs)

        self.assert_gap_refuses_discard_but_frees_the_shell(
            wt, mock.patch.object(recovery.os, "lstat", denied))

    def test_corrupt_repository_refuses(self):
        # No patching at all: a repository whose HEAD does not resolve. Every
        # observation the fence depends on is unavailable, so recovery refuses
        # instead of reading the gap as an unchanged worktree.
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        (Path(wt) / ".git" / "HEAD").write_text("not-a-ref\n")
        self.assert_gap_refuses_discard_but_frees_the_shell(wt)

    def test_unborn_head_is_complete_evidence_not_a_gap(self):
        # A repo with no commits is fully observable — nothing to diff
        # against, nothing that can be unpushed. It must NOT be refused as an
        # evidence gap, and its untracked side is still fenced.
        wt = Path(self.tmp.name) / "unborn"
        wt.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "feat/x", str(wt)],
                       check=True, capture_output=True)
        (wt / "untracked.txt").write_text("new")
        (wt / "tracked.txt").write_text("dirty")
        self.session_with_worktree(str(wt))
        obj = self.preview(1)
        self.assertNotIn("indeterminate", obj["evidence"]["git"])
        self.assertEqual(obj["evidence"]["git"]["unpushed_commits"], 0)
        self.assertEqual(obj["evidence"]["git"]["branch"], "feat/x")
        (wt / "late.txt").write_text("written after the preview")
        status, err = self.post(obj, preserve_worktree=False,
                                discard_worktree=True, confirm_shortname="s1")
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "recovery_observation_stale")
        self.assertEqual((wt / "late.txt").read_text(),
                         "written after the preview")

    def test_unborn_head_discard_actually_discards(self):
        # ...and having let it past the unpushed gate, the discard must DO
        # something: `reset --hard HEAD` is fatal where nothing has ever been
        # committed, so the reset failed and the clean never ran — an
        # authorised discard that silently left everything in place.
        # Bounding the discard to the enumerated set (SC-100) keeps that live:
        # a STAGED path on an unborn HEAD is invisible to `ls-files -o`, so
        # unless the plan enumerates the index too it is not in the delete set
        # and the authorised discard quietly leaves it again.
        wt = Path(self.tmp.name) / "unborn-discard"
        wt.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "feat/x", str(wt)],
                       check=True, capture_output=True)
        (wt / "staged.txt").write_text("staged")
        subprocess.run(["git", "-C", str(wt), "add", "staged.txt"],
                       check=True, capture_output=True)
        (wt / "untracked.txt").write_text("new")
        self.session_with_worktree(str(wt))
        obj = self.preview(1)
        status, result = self.post(obj, preserve_worktree=False,
                                   discard_worktree=True,
                                   confirm_shortname="s1")
        self.assertEqual(status, 200, result)
        self.assertTrue(result["worktree"]["discarded"], result["worktree"])
        self.assertEqual(result["worktree"]["completed"], ["remove", "restore"])
        self.assertFalse((wt / "staged.txt").exists())
        self.assertFalse((wt / "untracked.txt").exists())
        self.assertTrue(wt.is_dir())   # the worktree itself is never deleted

    # -- freshness must hold at the DESTRUCTIVE COMMIT, not just at entry
    #    (SC-091) -----------------------------------------------------------

    def test_work_written_during_preconditions_refuses(self):
        # The check-then-act gap: the fence used to run at execute ENTRY while
        # the clean ran at the END, so everything in between — the confirmation
        # gates and the `git rev-list` behind the unpushed check, real
        # wall-clock — was unprotected. Work written there was deleted and 200
        # returned. Injecting from _unpushed_count writes strictly inside that
        # old gap; the fence is now the LAST precondition, so it refuses.
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        obj = self.preview(1)
        late = Path(wt) / "late_during_preconditions.txt"
        real = recovery._unpushed_count

        def writing(worktree):
            late.write_text("written while the preconditions ran")
            return real(worktree)

        with mock.patch.object(recovery, "_unpushed_count", writing), \
                mock.patch.object(
                    recovery, "_discard_worktree_files",
                    return_value={"worktree": wt, "discarded": True,
                                  "completed": ["reset", "clean"],
                                  "failed": None}) as disc, \
                mock.patch.object(recovery,
                                  "terminate_process_group") as term:
            status, err = self.post(obj, preserve_worktree=False,
                                    discard_worktree=True,
                                    confirm_shortname="s1")
        self.assertEqual(status, 409, err)
        self.assertEqual(err["error"]["code"], "recovery_observation_stale")
        self.assertEqual(late.read_text(),
                         "written while the preconditions ran")
        disc.assert_not_called()   # no reset, no clean
        term.assert_not_called()   # no signal
        self.assert_nothing_discarded(wt)   # no closure either

    def orphan_with_worktree(self, wt: str):
        """A shell whose exact process is alive but whose pane is gone —
        `recover` is legal and DOES signal, so the discard runs on the far
        side of a real termination."""
        proc, ticks = self.child()
        self.make_session(1, pane_pid=proc.pid, pane_ticks=ticks, worktree=wt)
        return proc, ticks

    def test_work_written_during_shutdown_refuses_discard(self):
        # The window no entry fence can cover: the signal is what makes the
        # shell shut down, and a shell can write on its way out — after every
        # earlier check has already passed. The discard must not run on top of
        # it. Injecting from terminate_process_group reproduces exactly that.
        wt = self.make_git_worktree()
        proc, _ticks = self.orphan_with_worktree(wt)
        late = Path(wt) / "late_during_shutdown.txt"
        real = recovery.terminate_process_group

        def writing(pid, start_ticks, grace_s):
            result = real(pid, start_ticks, grace_s)
            late.write_text("flushed while the shell shut down")
            return result

        with mock.patch.object(recovery, "_pane_present", return_value=False):
            obj = self.preview(1)
            self.assertEqual(obj["classification"], "exact_idle_orphan")
            with mock.patch.object(recovery, "terminate_process_group",
                                   writing):
                status, err = self.post(obj, preserve_worktree=False,
                                        discard_worktree=True,
                                        confirm_shortname="s1")
        self.assertEqual(status, 409, err)
        self.assertEqual(err["error"]["code"], "recovery_observation_stale")
        # Nothing was deleted: the late write, the dirty tracked file and the
        # untracked file all survive.
        self.assertEqual(late.read_text(), "flushed while the shell shut down")
        self.assertEqual((Path(wt) / "tracked.txt").read_text(), "dirty")
        self.assertTrue((Path(wt) / "untracked.txt").exists())
        # ...and the refusal is honest about what it could NOT unwind: the
        # signal was sent and the durable closure committed before this point.
        # It names the process it signalled — this half of the wording is only
        # truthful because the no-signal half says the opposite (below).
        self.assertIn(f"The exact process (PID {proc.pid}) was signalled",
                      err["error"]["message"])
        details = err["error"]["details"]
        self.assertFalse(details["discarded"])
        self.assertTrue(details["closed"])
        self.assertTrue(details["signaled"]["signaled"])
        con = self.db()
        self.assertEqual(con.execute(
            "SELECT occupancy FROM interface_sessions WHERE shell_id=1"
        ).fetchone()[0], "ended")
        con.close()

    def test_discard_after_real_termination_still_runs(self):
        # The other side of the same gate: a termination that changes nothing
        # in the worktree must NOT be read as a change. The second fence
        # re-reads only the worktree precisely because the signal and the
        # closure legitimately move everything else.
        wt = self.make_git_worktree()
        proc, ticks = self.orphan_with_worktree(wt)
        with mock.patch.object(recovery, "_pane_present", return_value=False):
            obj = self.preview(1)
            status, result = self.post(obj, preserve_worktree=False,
                                       discard_worktree=True,
                                       confirm_shortname="s1")
        self.assertEqual(status, 200, result)
        self.assertTrue(result["signaled"]["signaled"])
        self.assertTrue(result["worktree"]["discarded"])
        proc.wait(timeout=5)
        self.assertEqual(recovery._proc_state(proc.pid, ticks), "dead")
        self.assertEqual((Path(wt) / "tracked.txt").read_text(), "v1")
        self.assertFalse((Path(wt) / "untracked.txt").exists())

    # -- the observation itself must not be TORN (SC-092) -------------------

    def test_mode_change_during_the_gates_own_read_refuses_discard(self):
        # SC-092: the observation was torn. _path_identity lstat'ed a path and
        # THEN opened and read it, so a chmod landing in between produced an
        # identity that was never true at any instant — the old mode against
        # the current bytes. That identity compared EQUAL to the preview, so
        # the last gate passed and reset/clean erased the change while the API
        # returned 200. Injecting from os.open reproduces exactly that window;
        # each observation is now self-consistent, so it refuses instead.
        wt = self.make_git_worktree()
        tracked = Path(wt) / "tracked.txt"
        os.chmod(tracked, 0o640)
        self.orphan_with_worktree(wt)
        # Armed only once the signal has been sent, so the injection fires
        # inside the LAST gate's read — the one window where a torn
        # observation still ends in a discard.
        armed: list[bool] = []
        fired: list[bool] = []
        real_term = recovery.terminate_process_group
        real_open = recovery.os.open

        def arming(pid, start_ticks, grace_s):
            result = real_term(pid, start_ticks, grace_s)
            armed.append(True)
            return result

        def chmod_inside_the_read(path, *args, **kwargs):
            fd = real_open(path, *args, **kwargs)
            if armed and not fired and str(path) == str(tracked):
                os.chmod(tracked, 0o600)   # after the lstat, before the read
                fired.append(True)
            return fd

        with mock.patch.object(recovery, "_pane_present", return_value=False):
            obj = self.preview(1)
            self.assertEqual(obj["classification"], "exact_idle_orphan")
            with mock.patch.object(recovery, "terminate_process_group",
                                   arming), \
                    mock.patch.object(recovery.os, "open",
                                      chmod_inside_the_read):
                status, err = self.post(obj, preserve_worktree=False,
                                        discard_worktree=True,
                                        confirm_shortname="s1")
        self.assertTrue(fired, "the injection never ran — repro is stale")
        self.assertEqual(status, 409, err)
        self.assertEqual(err["error"]["code"], "recovery_observation_stale")
        # The tightened mode, the dirty content and the untracked file all
        # survive: nothing was reset, nothing was cleaned.
        self.assertEqual(self.mode_of(tracked), 0o600)
        self.assertEqual(tracked.read_text(), "dirty")
        self.assertTrue((Path(wt) / "untracked.txt").exists())

    def test_worktree_that_will_not_hold_still_refuses(self):
        # The other half of stability: when the tree keeps moving under the
        # read, there is no true answer to give. Re-reading forever is not an
        # option and approximating is the SC-092 defect — so it becomes a gap,
        # and a gap refuses (SC-087) with nothing signalled, closed or removed.
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        tracked = Path(wt) / "tracked.txt"
        real_open = recovery.os.open
        modes = [0o640, 0o600]

        def never_settles(path, *args, **kwargs):
            fd = real_open(path, *args, **kwargs)
            if str(path) == str(tracked):
                modes.append(modes.pop(0))
                os.chmod(tracked, modes[0])
            return fd

        self.assert_gap_refuses_discard_but_frees_the_shell(
            wt, mock.patch.object(recovery.os, "open", never_settles))

    # -- the discard is bounded by the OBSERVATION, not by the tree (SC-100) --

    def late_write_inside_the_final_gate(self, wt, write):
        """Run a discard whose LAST gate is blind by construction: `write`
        fires after the accepted pass has finished enumerating.

        This is the window no amount of re-reading can close. `_observe_stable`
        needs two passes that agree; a path created after the SECOND pass's own
        enumeration is missed by both, so the digests match, the gate concludes
        "unchanged" and the destructive step runs. Freshness cannot see work
        that does not exist yet — only bounding the delete set can protect it.
        """
        armed, passes, fired = [], [], []
        real_term = recovery.terminate_process_group
        real_plan = recovery._discard_plan

        def arming(pid, start_ticks, grace_s):
            result = real_term(pid, start_ticks, grace_s)
            armed.append(True)      # every pass from here is the LAST gate's
            return result

        def write_after_enumerating(*args, **kwargs):
            plan = real_plan(*args, **kwargs)
            if armed:
                passes.append(True)
                if len(passes) == 2 and not fired:
                    write()      # after THIS pass listed the tree, before the
                    fired.append(True)   # gate compares it to the previous one
            return plan

        with mock.patch.object(recovery, "_pane_present", return_value=False):
            obj = self.preview(1)
            self.assertEqual(obj["classification"], "exact_idle_orphan")
            with mock.patch.object(recovery, "terminate_process_group",
                                   arming), \
                    mock.patch.object(recovery, "_discard_plan",
                                      write_after_enumerating):
                status, result = self.post(obj, preserve_worktree=False,
                                           discard_worktree=True,
                                           confirm_shortname="s1")
        self.assertTrue(fired, "the injection never ran — repro is stale")
        return status, result

    def test_ordinary_save_during_the_final_gate_is_not_erased(self):
        # SC-100, REV1's repro: an ORDINARY new-file save — an editor, a tool,
        # anything — landing during the gate's own second pass. Both passes
        # enumerated before it existed, so the composite digests agreed, the
        # gate accepted, and `git clean -fd` (which acts on the tree at delete
        # time, not on the set that was consented to) erased it while the API
        # returned 200 / discarded=true.
        wt = self.make_git_worktree()
        late = Path(wt) / "ordinary_editor_save.txt"
        self.orphan_with_worktree(wt)
        status, result = self.late_write_inside_the_final_gate(
            wt, lambda: late.write_text("an ordinary save"))
        self.assertEqual(status, 200, result)
        # The file was never in the delete set, so it is not deleted — and it
        # is not "preserved" either: the discard did exactly what it was
        # confirmed to do, and the new work is simply outside that.
        self.assertEqual(late.read_text(), "an ordinary save")
        self.assertTrue(result["worktree"]["discarded"], result["worktree"])
        self.assertEqual(result["worktree"]["kept"], [])
        # ...and the work the operator DID confirm erasing is gone.
        self.assertEqual((Path(wt) / "tracked.txt").read_text(), "v1")
        self.assertFalse((Path(wt) / "untracked.txt").exists())

    def test_late_file_inside_an_enumerated_dir_survives_with_its_dir(self):
        # The same save one level down. The directory IS in the delete set, so
        # a recursive removal of it would take the new file with it; the
        # removal is rmdir-only, so a non-empty directory survives and carries
        # the new work with it.
        wt = self.make_git_worktree()
        (Path(wt) / "untracked_dir").mkdir()
        (Path(wt) / "untracked_dir" / "old.txt").write_text("consented")
        late = Path(wt) / "untracked_dir" / "late.txt"
        self.orphan_with_worktree(wt)
        status, result = self.late_write_inside_the_final_gate(
            wt, lambda: late.write_text("an ordinary save"))
        self.assertEqual(status, 200, result)
        self.assertEqual(late.read_text(), "an ordinary save")
        self.assertFalse((Path(wt) / "untracked_dir" / "old.txt").exists())
        self.assertEqual(result["worktree"]["kept"], ["untracked_dir/"])
        self.assertTrue(result["worktree"]["discarded"], result["worktree"])

    def test_consented_path_rewritten_after_the_gate_is_kept(self):
        # The narrower case the bound does NOT cover on its own: a path that
        # IS in the delete set, rewritten after the gate read it. Each entry is
        # re-verified against the identity the gate observed immediately before
        # it is touched, so the rewrite is left alone and named — never removed
        # on the strength of an identity that no longer holds.
        wt = self.make_git_worktree()
        untracked = Path(wt) / "untracked.txt"
        self.session_with_worktree(wt)
        obj = self.preview(1)
        real_discard = recovery._discard_worktree_files

        def rewrite_then_discard(worktree, plan):
            untracked.write_text("saved again after the gate passed")
            return real_discard(worktree, plan)

        with mock.patch.object(recovery, "_discard_worktree_files",
                               rewrite_then_discard):
            status, result = self.post(obj, preserve_worktree=False,
                                       discard_worktree=True,
                                       confirm_shortname="s1")
        self.assertEqual(status, 200, result)
        self.assertEqual(untracked.read_text(),
                         "saved again after the gate passed")
        self.assertEqual(result["worktree"]["kept"], ["untracked.txt"])
        self.assertEqual(result["worktree"]["kept_count"], 1)
        self.assertFalse(result["worktree"]["discarded"])
        self.assertEqual((Path(wt) / "tracked.txt").read_text(), "v1")

    def test_discard_touches_nothing_outside_the_plan(self):
        # The bound stated directly: everything absent from the plan survives a
        # discard, whatever the tree holds when it runs.
        wt = self.make_git_worktree()
        plan = self.plan(wt)
        (Path(wt) / "not_in_plan.txt").write_text("later")
        result = recovery._discard_worktree_files(wt, plan)
        self.assertTrue(result["discarded"], result)
        self.assertEqual((Path(wt) / "tracked.txt").read_text(), "v1")
        self.assertFalse((Path(wt) / "untracked.txt").exists())
        self.assertEqual((Path(wt) / "not_in_plan.txt").read_text(), "later")

    def test_late_empty_dir_inside_an_enumerated_dir_survives(self):
        # The same bound one entity-type over. Pruning by walking the tree
        # would rmdir an EMPTY directory created after the observation — it is
        # removable and it is under an enumerated root, but nobody consented
        # to it. Only directories the plan covers are candidates.
        wt = self.make_git_worktree()
        (Path(wt) / "unt" / "sub").mkdir(parents=True)
        (Path(wt) / "unt" / "sub" / "old.txt").write_text("consented")
        plan = self.plan(wt)
        late = Path(wt) / "unt" / "late_empty"
        late.mkdir()
        result = recovery._discard_worktree_files(wt, plan)
        self.assertTrue(late.is_dir())
        self.assertFalse((Path(wt) / "unt" / "sub").exists())  # plan's own
        self.assertEqual(result["kept"], ["unt/"])
        self.assertIsNone(result["failed"])
        # a surviving DIRECTORY is reported, but every consented FILE is gone
        self.assertTrue(result["discarded"], result)

    def test_symlinked_ancestor_cannot_redirect_the_delete_outside(self):
        # SC-105: O_NOFOLLOW on the FINAL component says nothing about the
        # ancestors. Move `d` out of the worktree and drop a symlink to it at
        # `d`, and `d/u.txt` still stats as the very same inode the plan
        # recorded — so the identity check passes and a path-based unlink
        # follows the symlink and deletes the file at its new home OUTSIDE the
        # worktree, while the result reports discarded=true. Resolving each
        # component with O_NOFOLLOW refuses instead of redirecting.
        wt = self.make_git_worktree()
        (Path(wt) / "d").mkdir()
        (Path(wt) / "d" / "u.txt").write_text("moved out of the worktree")
        plan = self.plan(wt)
        self.assertIn("d/u.txt", plan["untracked_files"])
        outside = Path(self.tmp.name) / "moved-away"
        (Path(wt) / "d").rename(outside)              # same inodes, new home
        (Path(wt) / "d").symlink_to(outside)
        result = recovery._discard_worktree_files(wt, plan)
        self.assertIsNone(result["failed"], result)
        self.assertEqual((outside / "u.txt").read_text(),
                         "moved out of the worktree")
        self.assertIn("d/u.txt", result["kept"])
        self.assertFalse(result["discarded"])
        # the rest of the confirmed set still went
        self.assertFalse((Path(wt) / "untracked.txt").exists())

    def test_symlinked_ancestor_cannot_redirect_the_restore_outside(self):
        # The same trap on the other seam. git refuses to write through a
        # symlinked leading path — measured, not assumed, because the restore
        # is the one destructive step this module does not perform itself.
        wt = self.make_git_worktree()

        def git(*args):
            subprocess.run(["git", "-C", wt, *args], check=True,
                           capture_output=True)

        (Path(wt) / "dir").mkdir()
        (Path(wt) / "dir" / "node").write_text("committed")
        git("add", "dir/node")
        git("commit", "-qm", "node")
        (Path(wt) / "dir" / "node").write_text("dirty")
        plan = self.plan(wt)
        self.assertIn("dir/node", plan["tracked"])
        outside = Path(self.tmp.name) / "moved-dir"
        (Path(wt) / "dir").rename(outside)
        (Path(wt) / "dir").symlink_to(outside)
        recovery._discard_worktree_files(wt, plan)
        self.assertEqual((outside / "node").read_text(), "dirty")

    def test_nothing_outside_the_plan_survives_adversarial_shapes(self):
        """The boundary claim, tested the way the closure claim is: build every
        shape that could let the discard reach outside the enumerated set, run
        it, and only then say the boundary holds.

        'Nothing outside the enumerated set is exposed' was stated three times
        before it was true — ignored descendants of a file/directory conflict
        were exposed the whole time (SC-103). The claim now has a test that
        would go red if any of these leaked.
        """
        wt = self.make_git_worktree()
        outside = Path(self.tmp.name) / "outside-the-worktree"
        outside.mkdir()
        (outside / "victim.txt").write_text("outside the worktree entirely")

        def git(*args):
            subprocess.run(["git", "-C", wt, *args], check=True,
                           capture_output=True)

        (Path(wt) / ".gitignore").write_text("*.pyc\n")
        (Path(wt) / "node").write_text("committed")
        (Path(wt) / "steady.txt").write_text("clean and tracked")
        git("add", ".gitignore", "node", "steady.txt")
        git("commit", "-qm", "boundary base")

        # (a) tracked FILE replaced by a directory hiding an ignored descendant
        #     several levels down — the bulk restore's own footprint (SC-103).
        (Path(wt) / "node").unlink()
        (Path(wt) / "node" / "deep").mkdir(parents=True)
        (Path(wt) / "node" / "deep" / "keep.pyc").write_text("ignored deep")
        # (b) an ignored file inside a directory that IS in the delete set
        (Path(wt) / "untdir").mkdir()
        (Path(wt) / "untdir" / "u.txt").write_text("consented")
        (Path(wt) / "untdir" / "skip.pyc").write_text("ignored, inside it")
        # (c) somebody else's repository inside that same directory
        nested = Path(wt) / "untdir" / "nested"
        nested.mkdir()
        subprocess.run(["git", "init", "-q", str(nested)], check=True,
                       capture_output=True)
        (nested / "theirs.txt").write_text("another repo's work")
        # (d) an enumerated untracked symlink AIMED out of the worktree: the
        #     link goes, never the thing it points at
        (Path(wt) / "ulink_out").symlink_to(outside / "victim.txt")
        # (e) an ignored file at the top, and a clean tracked file — neither is
        #     enumerated, so neither may move
        (Path(wt) / "top.pyc").write_text("ignored at the root")

        result = recovery._discard_worktree_files(wt, self.plan(wt))
        self.assertIsNone(result["failed"], result)
        for path, text in (
                (outside / "victim.txt", "outside the worktree entirely"),
                (Path(wt) / "node" / "deep" / "keep.pyc", "ignored deep"),
                (Path(wt) / "untdir" / "skip.pyc", "ignored, inside it"),
                (nested / "theirs.txt", "another repo's work"),
                (Path(wt) / "top.pyc", "ignored at the root"),
                (Path(wt) / "steady.txt", "clean and tracked")):
            self.assertEqual(path.read_text(), text,
                             f"{path} is outside the plan and was touched")
        # ...while everything that WAS enumerated went, and the report says so
        self.assertFalse((Path(wt) / "untdir" / "u.txt").exists())
        self.assertFalse((Path(wt) / "untracked.txt").exists())
        self.assertFalse((Path(wt) / "ulink_out").exists())
        self.assertEqual((Path(wt) / "tracked.txt").read_text(), "v1")
        self.assertIn("node", result["kept"])       # refused, not exceeded
        self.assertFalse(result["discarded"])

    def df_worktree(self, obstruction: str, *, ignored: bool) -> str:
        """HEAD tracks the FILE `node`; the worktree has replaced it with a
        DIRECTORY holding `obstruction`. `git diff HEAD` names `node`, so the
        plan enumerates it — as one entity, `dir:…`, which says nothing about
        what is inside."""
        wt = self.make_git_worktree()

        def git(*args):
            subprocess.run(["git", "-C", wt, *args], check=True,
                           capture_output=True)

        (Path(wt) / ".gitignore").write_text("*.pyc\n")
        (Path(wt) / "node").write_text("the committed file")
        git("add", ".gitignore", "node")
        git("commit", "-qm", "node")
        (Path(wt) / "node").unlink()
        (Path(wt) / "node").mkdir()
        (Path(wt) / "node" / obstruction).write_text("not in the delete set"
                                                     if ignored else "consented")
        return wt

    def test_tracked_path_now_a_dir_of_ignored_files_is_not_restored_over(self):
        # SC-103, and no concurrent writer is needed. `git restore` on a path
        # the worktree turned into a directory deletes the DIRECTORY — here
        # taking an ignored file the plan never enumerated, so the bounded-set
        # claim failed on the command's own footprint rather than on a race.
        wt = self.df_worktree("cache.pyc", ignored=True)
        plan = self.plan(wt)
        self.assertIn("node", plan["tracked"])
        self.assertNotIn("node/cache.pyc", plan["untracked_files"])
        result = recovery._discard_worktree_files(wt, plan)
        self.assertIsNone(result["failed"], result)
        self.assertEqual((Path(wt) / "node" / "cache.pyc").read_text(),
                         "not in the delete set")
        self.assertEqual(result["kept"], ["node"])
        self.assertFalse(result["discarded"])
        # the rest of the confirmed set still went
        self.assertFalse((Path(wt) / "untracked.txt").exists())

    def test_tracked_path_now_a_dir_of_enumerated_files_is_restored(self):
        # The other half, and the truth bug: with the obstruction ENUMERATED,
        # restoring first deleted it as collateral and the removal loop then
        # saw it gone, decided it had "changed", and reported it kept — a file
        # named as spared that the discard had in fact just erased. Removing
        # first empties the directory, so the restore is bounded and the
        # report is true.
        wt = self.df_worktree("u.txt", ignored=False)
        plan = self.plan(wt)
        self.assertIn("node/u.txt", plan["untracked_files"])
        result = recovery._discard_worktree_files(wt, plan)
        self.assertIsNone(result["failed"], result)
        self.assertEqual((Path(wt) / "node").read_text(), "the committed file")
        self.assertEqual(result["kept"], [])
        self.assertTrue(result["discarded"], result)

    def test_tracked_dir_now_a_file_is_restored(self):
        # The mirror: HEAD tracks `d/x`, the worktree replaced `d` with a
        # FILE. Restoring first cannot create `d/x` through it — and under the
        # old order the restore deleted the untracked file `d` and the removal
        # loop then reported it kept. Removal first clears the path.
        wt = self.make_git_worktree()

        def git(*args):
            subprocess.run(["git", "-C", wt, *args], check=True,
                           capture_output=True)

        (Path(wt) / "d").mkdir()
        (Path(wt) / "d" / "x").write_text("committed")
        git("add", "d/x")
        git("commit", "-qm", "d")
        shutil.rmtree(Path(wt) / "d")
        (Path(wt) / "d").write_text("a file now")
        plan = self.plan(wt)
        self.assertIn("d/x", plan["tracked"])
        self.assertIn("d", plan["untracked_files"])
        result = recovery._discard_worktree_files(wt, plan)
        self.assertIsNone(result["failed"], result)
        self.assertEqual((Path(wt) / "d" / "x").read_text(), "committed")
        self.assertEqual(result["kept"], [])
        self.assertTrue(result["discarded"], result)

    def test_dir_emptied_by_the_restore_goes_too(self):
        # Parity with the operation this replaces: `clean -fd` removed the
        # directory a staged-new file left behind. Git tracks no directories,
        # so `git restore` alone leaves it standing — pruning ancestors of the
        # enumerated entries, not just the enumerated directories, keeps a
        # discard from littering empty directories through the tree.
        wt = self.make_git_worktree()
        (Path(wt) / "staged").mkdir()
        (Path(wt) / "staged" / "new.txt").write_text("staged")
        subprocess.run(["git", "-C", wt, "add", "staged/new.txt"], check=True,
                       capture_output=True)
        result = recovery._discard_worktree_files(wt, self.plan(wt))
        self.assertTrue(result["discarded"], result)
        self.assertFalse((Path(wt) / "staged").exists())

    def test_untracked_dir_of_only_ignored_files_is_not_reported_kept(self):
        # `clean -fd` leaves such a directory standing too, so its survival is
        # not an incomplete discard. Reporting it would cry wolf on a very
        # common shape (a build dir, __pycache__) and bury the survivor that
        # does matter — one held open by work written after the confirmation.
        wt = self.make_git_worktree()
        (Path(wt) / ".gitignore").write_text("*.pyc\n")
        subprocess.run(["git", "-C", wt, "add", ".gitignore"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", wt, "commit", "-qm", "ignore"],
                       check=True, capture_output=True)
        cache = Path(wt) / "__pycache__"
        cache.mkdir()
        (cache / "m.pyc").write_bytes(b"\x00")
        plan = self.plan(wt)
        result = recovery._discard_worktree_files(wt, plan)
        self.assertTrue((cache / "m.pyc").exists())   # ignored: never touched
        self.assertEqual(result["kept"], ["__pycache__/"])
        self.assertTrue(result["discarded"], result)   # no FILE was left

    def test_nested_untracked_repository_is_not_unlinked(self):
        # git names a nested repository ONCE, with a trailing slash, in the
        # plain FILE listing — it never descends. Treating that entry as a
        # file unlinks a directory: EISDIR, and the whole discard reports a
        # failure. It is a directory, it survives (as it did under
        # `clean -fd`), and it is reported because git still lists it.
        wt = self.make_git_worktree()
        nested = Path(wt) / "nested"
        nested.mkdir()
        subprocess.run(["git", "init", "-q", str(nested)], check=True,
                       capture_output=True)
        (nested / "inner.txt").write_text("someone else's repo")
        plan = self.plan(wt)
        self.assertIn("nested/", plan["untracked_dirs"])
        self.assertNotIn("nested/", plan["untracked_files"])
        result = recovery._discard_worktree_files(wt, plan)
        self.assertIsNone(result["failed"], result)
        self.assertEqual((nested / "inner.txt").read_text(),
                         "someone else's repo")
        self.assertEqual(result["kept"], ["nested/"])

    # -- a refusal names what it actually did, in BOTH directions -----------

    def test_no_signal_refusal_does_not_claim_a_signal(self):
        # The last gate's refusal used to state that the exact process had been
        # signalled — inherited boilerplate. A stale durable lock has no
        # process to signal (details signaled=null), so that sentence was
        # false: reporting a signal that never happened is the same defect as
        # implying a clean no-op after having closed. With nothing to signal,
        # the closure is the only thing between the two gates, so that is
        # where the late write is injected.
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        obj = self.preview(1)
        self.assertEqual(obj["classification"], "stale_durable_lock")
        late = Path(wt) / "late_after_closure.txt"
        real = recovery._close_durable_state

        def writing(con, shell_id, evidence, end_reason):
            changed = real(con, shell_id, evidence, end_reason)
            late.write_text("written after the closure committed")
            return changed

        with mock.patch.object(recovery, "_close_durable_state", writing), \
                mock.patch.object(recovery,
                                  "terminate_process_group") as term:
            status, err = self.post(obj, preserve_worktree=False,
                                    discard_worktree=True,
                                    confirm_shortname="s1")
        self.assertEqual(status, 409, err)
        self.assertEqual(err["error"]["code"], "recovery_observation_stale")
        term.assert_not_called()
        message = err["error"]["message"]
        self.assertIn("NO process was signalled", message)
        self.assertNotIn("The exact process", message)
        self.assertIsNone(err["error"]["details"]["signaled"])
        # ...and what it DOES claim is true: the closure committed, and the
        # files — the late write included — are untouched.
        self.assertTrue(err["error"]["details"]["closed"])
        con = self.db()
        self.assertEqual(con.execute(
            "SELECT occupancy FROM interface_sessions WHERE shell_id=1"
        ).fetchone()[0], "ended")
        con.close()
        self.assertEqual(late.read_text(), "written after the closure "
                                           "committed")
        self.assertEqual((Path(wt) / "tracked.txt").read_text(), "dirty")
        self.assertTrue((Path(wt) / "untracked.txt").exists())

    def test_discard_git_failure_reports_exactly_what_completed(self):
        # the restore fails AFTER the removal succeeded and the closure
        # committed: the response stays 200 and names the completed/failed
        # steps — never a bare 500 that hides a partial discard.
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        obj = self.preview(1)
        real_run = recovery.subprocess.run

        def failing_restore(*args, **kwargs):
            if "restore" in args[0]:
                return subprocess.CompletedProcess(
                    args=args[0], returncode=1, stdout=b"",
                    stderr=b"fatal: restore boom")
            return real_run(*args, **kwargs)

        with mock.patch.object(recovery.subprocess, "run",
                               side_effect=failing_restore):
            status, result = self.post(obj, preserve_worktree=False,
                                       discard_worktree=True,
                                       confirm_shortname="s1")
        self.assertEqual(status, 200, result)
        wt_result = result["worktree"]
        self.assertFalse(wt_result["discarded"])
        self.assertEqual(wt_result["completed"], ["remove"])
        self.assertEqual(wt_result["failed"]["step"], "restore")
        self.assertIn("restore boom", wt_result["failed"]["error"])
        # the removal ran (untracked gone), the restore did not (tracked is
        # still dirty), and the durable closure still committed.
        self.assertFalse((Path(wt) / "untracked.txt").exists())
        self.assertEqual((Path(wt) / "tracked.txt").read_text(), "dirty")
        con = self.db()
        self.assertEqual(con.execute(
            "SELECT occupancy FROM interface_sessions WHERE shell_id=1"
        ).fetchone()[0], "ended")
        con.close()

    def test_discard_remove_failure_stops_before_the_restore(self):
        # The first step failing stops the sequence: nothing is restored, so a
        # partial discard never reads as a completed one.
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        obj = self.preview(1)

        def denied(_name, **_kw):
            raise OSError(errno.EACCES, "denied")

        with mock.patch.object(recovery.os, "unlink", side_effect=denied):
            status, result = self.post(obj, preserve_worktree=False,
                                       discard_worktree=True,
                                       confirm_shortname="s1")
        self.assertEqual(status, 200, result)
        wt_result = result["worktree"]
        self.assertFalse(wt_result["discarded"])
        self.assertEqual(wt_result["completed"], [])
        self.assertEqual(wt_result["failed"]["step"], "remove")
        self.assertIn("untracked.txt", wt_result["failed"]["error"])
        self.assertTrue((Path(wt) / "untracked.txt").exists())
        self.assertEqual((Path(wt) / "tracked.txt").read_text(), "dirty")

    def test_discard_git_timeout_reported_not_raised(self):
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        obj = self.preview(1)
        real_run = recovery.subprocess.run

        def timeout_restore(*args, **kwargs):
            if "restore" in args[0]:
                raise subprocess.TimeoutExpired(cmd=args[0], timeout=60)
            return real_run(*args, **kwargs)

        with mock.patch.object(recovery.subprocess, "run",
                               side_effect=timeout_restore):
            status, result = self.post(obj, preserve_worktree=False,
                                       discard_worktree=True,
                                       confirm_shortname="s1")
        self.assertEqual(status, 200, result)
        self.assertFalse(result["worktree"]["discarded"])
        self.assertEqual(result["worktree"]["completed"], ["remove"])
        self.assertEqual(result["worktree"]["failed"]["step"], "restore")


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

    def test_discard_that_kept_entries_says_so(self):
        # A bounded discard can complete with entries left intact (SC-100).
        # That is neither "changes discarded" nor "worktree preserved" — both
        # would be false, and the operator needs the names to finish by hand.
        self.script(dict(self.PREVIEW_ORPHAN), dict(
            self.RESULT, worktree={"worktree": "/w", "discarded": False,
                                   "completed": ["restore", "remove"],
                                   "failed": None, "kept": ["late.txt"],
                                   "kept_count": 1}))
        code, out, err = self.run_cli(
            ["recover", "s3", "--discard-worktree", "--yes"])
        self.assertEqual(code, 0)
        self.assertIn("discarded, EXCEPT 1 entry left intact", out)
        self.assertIn("changed after the confirmation", out)
        self.assertIn("late.txt", out)
        self.assertNotIn("worktree preserved", out)
        self.assertEqual(err, "")

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
