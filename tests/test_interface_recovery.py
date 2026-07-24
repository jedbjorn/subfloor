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

    # -- SC-131: the seam is the FIRST DELIVERED SIGNAL --------------------
    # Every failure mode is placed by which side of that seam it falls on,
    # and each side has one contract. Before it: nothing happened, so it is a
    # refusal (`signaled` False). After it: something irreversible happened,
    # so the result says so instead of escaping as an opaque 500.

    def test_sigkill_raising_after_sigterm_still_names_the_sent_signal(self):
        # REV2's exact probe: SIGTERM lands, the grace expires with the
        # process alive, and the SIGKILL call raises. Before the boundary
        # this left the route with a bare PermissionError, so the operator
        # was told nothing at all about a process that HAD been signalled.
        proc, ticks = self.child(ignore_sigterm=True)
        real = os.killpg
        calls = []

        def killpg(pgid, sig):
            calls.append(sig)
            if sig == signal.SIGKILL:
                raise PermissionError(errno.EPERM, "Operation not permitted")
            return real(pgid, sig)

        with mock.patch.object(recovery.os, "killpg", killpg):
            result = recovery.terminate_process_group(proc.pid, ticks,
                                                      grace_s=0.3)
        self.assertEqual(calls, [signal.SIGTERM, signal.SIGKILL])
        self.assertTrue(result["signaled"])          # and it cannot be unwound
        self.assertFalse(result["dead"])             # never claim absence
        self.assertFalse(result["escalated"])        # SIGKILL never delivered
        self.assertEqual(result["reason"], "signal_failed")
        self.assertEqual(result["phase"], "sigkill")
        self.assertIn("PermissionError", result["error"])
        self.assertIn(str(proc.pid), result["detail"])

    def test_undeliverable_sigterm_is_a_refusal_not_a_partial_act(self):
        # The other side of the seam: nothing was delivered, so this must
        # read exactly like an identity mismatch — signaled False, which the
        # caller maps to a refusal that closes nothing.
        proc, ticks = self.child()

        def killpg(pgid, sig):
            raise PermissionError(errno.EPERM, "Operation not permitted")

        with mock.patch.object(recovery.os, "killpg", killpg):
            result = recovery.terminate_process_group(proc.pid, ticks,
                                                      grace_s=0.2)
        self.assertFalse(result["signaled"])
        self.assertFalse(result["dead"])
        self.assertEqual(result["reason"], "indeterminate")
        self.assertIn("PermissionError", result["detail"])
        # The process is untouched — the refusal was truthful.
        self.assertEqual(recovery._proc_state(proc.pid, ticks), "alive")

    def test_grace_poll_failure_after_sigterm_is_reported_not_raised(self):
        # Not the probe's instance: the boundary covers the whole post-signal
        # sequence, so a failure in the grace poll reports the same contract
        # as one in the SIGKILL call.
        proc, ticks = self.child()
        with mock.patch.object(recovery, "_wait_dead",
                               side_effect=OSError("proc vanished")):
            result = recovery.terminate_process_group(proc.pid, ticks,
                                                      grace_s=0.2)
        self.assertTrue(result["signaled"])
        self.assertFalse(result["dead"])
        self.assertFalse(result["escalated"])
        self.assertEqual(result["reason"], "signal_failed")
        self.assertEqual(result["phase"], "grace_wait")
        self.assertIn("OSError", result["error"])

    def test_malformed_proc_stat_reads_unreadable_never_dead(self):
        # /proc/<pid>/stat is a live snapshot and can be read truncated. That
        # made _read_stat raise ValueError/IndexError straight through every
        # caller — including the post-signal poll. Fixed at the definition:
        # an unusable answer is 'unreadable', which is fail-closed.
        # No ')' at all; truncated before field 22; field 22 unparseable.
        for text in ("", "1 (sh) S", "1 (sh) S " + "0 " * 18 + "notanint"):
            with self.subTest(text=text), \
                    mock.patch("builtins.open",
                               mock.mock_open(read_data=text)):
                self.assertEqual(recovery._proc_state(1234, 999),
                                 "unreadable")

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
        self.assertEqual(result["closed"]["runtime"], {"abandoned": True})

    def test_runtime_abandon_failure_is_reported_not_swallowed(self):
        # The post-commit path that never reaches a 500, and was silent
        # instead. Dropping the runtime generation is best-effort by design —
        # the durable closure is already committed and a runtime that will not
        # let go is not something to fail a recovery over — but the response
        # said only that the shell was available, while a generation may still
        # be attached to it. Handled where something can be done about it, and
        # named either way (SC-128).
        proc, ticks = self.child()
        self.make_session(1, pane_pid=proc.pid, pane_ticks=ticks)

        async def refusing(session_id):
            raise RuntimeError("the runtime will not let go")

        with mock.patch.object(recovery, "_pane_present", return_value=True):
            obj = self.preview(1)
            with mock.patch.object(self.runtime, "abandon", refusing):
                status, result = self.call(
                    "POST", "/api/interface/shells/1/recovery", (OP, IDEM),
                    {"observation_id": obj["observation_id"], "mode": "force",
                     "confirm_force": True})
        self.assertEqual(status, 200, result)
        self.assertFalse(result["closed"]["runtime"]["abandoned"])
        self.assertIn("RuntimeError", result["closed"]["runtime"]["error"])
        # ...and the closure it could not unwind is still reported as done
        self.assertEqual(result["availability"], "available")
        proc.wait(timeout=5)
        con = self.db()
        self.assertEqual(con.execute(
            "SELECT occupancy FROM interface_sessions WHERE shell_id=1"
        ).fetchone()[0], "ended")
        con.close()

    def test_closure_failure_names_the_signal_it_cannot_unwind(self):
        # The mirror of the post-commit rule, one step earlier. The rollback
        # makes the durable half a clean no-op and says nothing about the half
        # that is not undoable: the exact process has already been signalled
        # and proven dead. A bare 500 there leaves the operator unable to tell
        # "the recovery never started" from "your shell is gone and its rows
        # still say it is running".
        proc, ticks = self.child()
        self.make_session(1, pane_pid=proc.pid, pane_ticks=ticks)
        with mock.patch.object(recovery, "_pane_present", return_value=True):
            obj = self.preview(1)
            with mock.patch.object(recovery, "_close_durable_state",
                                   side_effect=RuntimeError("db boom")):
                status, err = self.call(
                    "POST", "/api/interface/shells/1/recovery", (OP, IDEM),
                    {"observation_id": obj["observation_id"], "mode": "force",
                     "confirm_force": True})
        self.assertEqual(status, 500, err)
        self.assertEqual(err["error"]["code"], "recovery_closure_failed")
        details = err["error"]["details"]
        self.assertTrue(details["signaled"]["signaled"])
        self.assertFalse(details["closed"])
        self.assertFalse(details["discarded"])
        self.assertIn("RuntimeError", details["error"])
        self.assertIn("proven dead", err["error"]["message"])
        # ...and both claims are true: the process really is gone, and the
        # durable state really was left alone.
        proc.wait(timeout=5)
        self.assertEqual(recovery._proc_state(proc.pid, ticks), "dead")
        con = self.db()
        self.assertNotEqual(con.execute(
            "SELECT occupancy FROM interface_sessions WHERE shell_id=1"
        ).fetchone()[0], "ended")
        con.close()

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

    def test_broken_signal_sequence_answers_with_what_it_did(self):
        # SC-131 at the API boundary. The sequence delivered SIGTERM and then
        # broke; the response must name the irreversible half AND the two
        # things that did not happen, rather than emitting an opaque 500 that
        # is indistinguishable from a recovery which never started.
        proc, ticks = self.child()
        sid = self.make_session(1, pane_pid=proc.pid, pane_ticks=ticks)
        real = os.killpg

        def killpg(pgid, sig):
            if sig == signal.SIGKILL:
                raise PermissionError(errno.EPERM, "Operation not permitted")
            return real(pgid, sig)

        with mock.patch.object(recovery, "_pane_present", return_value=False):
            obj = self.preview(1)
            self.assertEqual(obj["classification"], "exact_idle_orphan")
            with mock.patch.object(recovery, "_proc_state",
                                   return_value="alive"), \
                    mock.patch.object(recovery.os, "killpg", killpg):
                status, err = self.call(
                    "POST", "/api/interface/shells/1/recovery", (OP, IDEM),
                    {"observation_id": obj["observation_id"],
                     "mode": "recover"})
        # A refusal, NOT a 500 — the durable state is untouched and says so.
        self.assertEqual(status, 409, err)
        self.assertEqual(err["error"]["code"], "recovery_absence_unproven")
        details = err["error"]["details"]
        self.assertIs(details["signaled"], True)
        self.assertIs(details["closed"], False)
        self.assertIs(details["discarded"], False)
        self.assertEqual(details["phase"], "sigkill")
        self.assertIn("PermissionError", details["error"])
        message = err["error"]["message"]
        self.assertIn("sigkill", message)
        self.assertIn("nothing was reset or cleaned", message)
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

    # -- the STAGING INDEX is state a discard destroys too (SC-123) --------
    # Same rule as everything above — if the discard would rewrite it, the
    # digest binds it — applied to the one place work lives that is not a
    # file: the index. Every case here leaves the working file, its lstat and
    # the porcelain line byte-identical, so nothing the FILESYSTEM carries
    # can tell the states apart.

    def git_run(self, wt: str, *args, stdin: str | None = None) -> str:
        return subprocess.run(["git", "-C", wt, *args], input=stdin,
                              capture_output=True, text=True,
                              check=True).stdout

    def stage_blob(self, wt: str, rel: str, text: str) -> None:
        """Put `text` in the INDEX for `rel` WITHOUT touching the worktree
        file — the shape ordinary partial staging (`git add -p`) produces.
        `update-index --cacheinfo` writes only `.git/index`, so the working
        copy's bytes, mode and timestamps are provably untouched."""
        sha = self.git_run(wt, "hash-object", "-w", "--stdin",
                           stdin=text).strip()
        self.git_run(wt, "update-index", "--add", "--cacheinfo",
                     f"100644,{sha},{rel}")

    def staged(self, wt: str, rel: str) -> str:
        return self.git_run(wt, "show", f":{rel}")

    def stamp(self, path: Path) -> tuple:
        return recovery._stamp(os.lstat(path))

    def stage_over_working_copy(self, wt: str, rel: str, *, staged: str,
                                working: str) -> None:
        (Path(wt) / rel).write_text(working)
        self.stage_blob(wt, rel, staged)

    def assert_staged_change_refuses(self, wt: str, rel: str,
                                     expected_line: str) -> None:
        """Re-stage `rel` after the preview and require the refusal — pinning
        that the FILESYSTEM did not move, so the refusal can only come from
        the index being bound."""
        self.assertIn(expected_line, self.porcelain(wt))
        working = (Path(wt) / rel).read_text()
        before = self.stamp(Path(wt) / rel)
        self.assert_mutation_refuses(
            wt, lambda: self.stage_blob(wt, rel, "staged-c-after-preview"))
        self.assertEqual(self.stamp(Path(wt) / rel), before,
                         "the working file moved — this case no longer "
                         "isolates the index")
        self.assertEqual((Path(wt) / rel).read_text(), working)
        self.assertEqual(self.staged(wt, rel), "staged-c-after-preview")

    def test_discard_destroys_staged_content(self):
        # The premise, reproduced rather than argued (as SC-090's was): the
        # discard's `git restore --staged` throws the index back to HEAD, so
        # staged content is work a discard erases — which is what obliges the
        # digest to bind it.
        wt = self.make_git_worktree()
        self.stage_over_working_copy(wt, "tracked.txt", staged="staged-a",
                                     working="working-b")
        recovery._discard_worktree_files(wt, self.plan(wt))
        self.assertEqual(
            self.staged(wt, "tracked.txt"), "v1",
            "restore --staged left the staged blob intact — premise of "
            "SC-123 changed")

    def test_staged_content_change_after_preview_refuses_discard(self):
        # SC-123: an already-tracked path staged with new content. Porcelain
        # stays `MM tracked.txt` and the working copy stays `working-b`, so a
        # digest built from status lines plus filesystem identity reads fresh
        # while the blob the discard is about to erase has been replaced.
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        self.stage_over_working_copy(wt, "tracked.txt", staged="staged-a",
                                     working="working-b")
        self.assert_staged_change_refuses(wt, "tracked.txt", "MM tracked.txt")

    def test_newly_staged_content_change_after_preview_refuses_discard(self):
        # The other half: a path that is not in HEAD at all, staged as an
        # addition (`AM`). The discard drops it from the index and deletes it,
        # so its staged blob is destroyed just as completely.
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        self.stage_over_working_copy(wt, "added.txt", staged="staged-a",
                                     working="working-b")
        self.assert_staged_change_refuses(wt, "added.txt", "AM added.txt")

    def test_staged_change_during_shutdown_refuses_discard(self):
        # The LATE gate, for the index. The re-stage lands after the
        # execute-entry gate has already passed — injected from the signal,
        # exactly where a shell writes on its way out — so only the second
        # gate can catch it. Its 409 must name the signal and closure it
        # cannot unwind, and no work-destroying command may run.
        wt = self.make_git_worktree()
        self.stage_over_working_copy(wt, "tracked.txt", staged="staged-a",
                                     working="working-b")
        proc, _ticks = self.orphan_with_worktree(wt)
        real = recovery.terminate_process_group

        def writing(pid, start_ticks, grace_s):
            result = real(pid, start_ticks, grace_s)
            self.stage_blob(wt, "tracked.txt", "staged-c-after-the-fence")
            return result

        with mock.patch.object(recovery, "_pane_present", return_value=False):
            obj = self.preview(1)
            self.assertEqual(obj["classification"], "exact_idle_orphan")
            with mock.patch.object(
                    recovery, "_discard_worktree_files",
                    return_value={"worktree": wt, "discarded": True,
                                  "completed": ["remove", "restore"],
                                  "failed": None}) as disc, \
                    mock.patch.object(recovery, "terminate_process_group",
                                      writing):
                status, err = self.post(obj, preserve_worktree=False,
                                        discard_worktree=True,
                                        confirm_shortname="s1")
        self.assertEqual(status, 409, err)
        self.assertEqual(err["error"]["code"], "recovery_observation_stale")
        disc.assert_not_called()   # nothing removed, nothing restored
        # the post-fence staged blob and the working copy both survive
        self.assertEqual(self.staged(wt, "tracked.txt"),
                         "staged-c-after-the-fence")
        self.assertEqual((Path(wt) / "tracked.txt").read_text(), "working-b")
        self.assertTrue((Path(wt) / "untracked.txt").exists())
        # ...and the refusal is honest about what it could NOT unwind
        self.assertIn(f"The exact process (PID {proc.pid}) was signalled",
                      err["error"]["message"])
        details = err["error"]["details"]
        self.assertFalse(details["discarded"])
        self.assertTrue(details["closed"])

    # -- the index's DURABLE FLAGS are state a discard rewrites (SC-125) ---
    # The index is a store, not a blob table: alongside mode/object/stage each
    # entry carries skip-worktree and assume-unchanged bits, and `git restore
    # --staged` clears them. Setting one moves NOTHING else — the working
    # file, its lstat, the porcelain line and the entry's own mode/object/
    # stage are byte-identical either side — so only binding the flag can
    # tell the states apart.

    def stage_only(self, wt: str, rel: str, text: str) -> None:
        """Stage `text` with the working file holding the SAME bytes, so the
        entry is `M ` — the one shape where setting a flag leaves the
        porcelain line alone (on `MM` git reports `M ` once the worktree half
        is suppressed, which would move the digest by itself)."""
        (Path(wt) / rel).write_text(text)
        self.git_run(wt, "add", "--", rel)

    def index_tag(self, wt: str, rel: str) -> str:
        """The `ls-files -v` tag letter: `H` plain, `S` skip-worktree,
        lowercase when assume-unchanged is set."""
        return self.git_run(wt, "ls-files", "-v", "--", rel).split()[0]

    def assert_flag_change_refuses(self, wt: str, rel: str, flag: str) -> None:
        self.assertIn(f"M  {rel}", self.porcelain(wt))
        before_stage = self.git_run(wt, "ls-files", "--stage", "--", rel)
        before_stamp = self.stamp(Path(wt) / rel)
        before_text = (Path(wt) / rel).read_text()
        self.assert_mutation_refuses(
            wt, lambda: self.git_run(wt, "update-index", flag, "--", rel))
        self.assertEqual(self.git_run(wt, "ls-files", "--stage", "--", rel),
                         before_stage,
                         "mode/object/stage moved — this case no longer "
                         "isolates the flag")
        self.assertEqual(self.stamp(Path(wt) / rel), before_stamp,
                         "the working file moved — this case no longer "
                         "isolates the index")
        self.assertEqual((Path(wt) / rel).read_text(), before_text)

    def test_discard_clears_the_durable_index_flags(self):
        # The premise, reproduced rather than argued: `restore --staged`
        # throws the entry back to HEAD and drops the bits with it, which is
        # what obliges the digest to bind them.
        wt = self.make_git_worktree()
        self.stage_only(wt, "tracked.txt", "staged-only")
        self.git_run(wt, "update-index", "--skip-worktree", "--",
                     "tracked.txt")
        self.assertEqual(self.index_tag(wt, "tracked.txt"), "S")
        self.git_run(wt, "restore", "--source=HEAD", "--staged", "--worktree",
                     "--", "tracked.txt")
        self.assertEqual(self.index_tag(wt, "tracked.txt"), "H",
                         "restore left the flag standing — premise of SC-125 "
                         "changed")

    def test_skip_worktree_set_after_preview_refuses_discard(self):
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        self.stage_only(wt, "tracked.txt", "staged-only")
        self.assert_flag_change_refuses(wt, "tracked.txt", "--skip-worktree")

    def test_assume_unchanged_set_after_preview_refuses_discard(self):
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        self.stage_only(wt, "tracked.txt", "staged-only")
        self.assert_flag_change_refuses(wt, "tracked.txt",
                                        "--assume-unchanged")

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
            # WHAT IT SAYS, not just that it says something (SC-107). The
            # projection is canonical — browser and CLI render it verbatim —
            # and it used to promise "recovery refused until it can be", which
            # the two-tier gate made false. An operator who believes recovery
            # is impossible does not attempt it, so a stale sentence strands
            # the shell just as effectively as a refusing code path.
            worktree = rows["worktree"]
            self.assertIn("could not be observed completely", worktree)
            self.assertIn("discard declined", worktree)
            self.assertIn("free the shell", worktree)
            self.assertNotIn("recovery refused", worktree)
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
                # the refusal names the escape route that actually works,
                # rather than leaving the operator to guess there is one
                message = err["error"]["message"]
                self.assertIn("DISCARD is refused", message)
                self.assertIn("without discard_worktree", message)
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

    def test_consented_path_restaged_after_the_gate_is_kept(self):
        # The same guarantee for the index (SC-123). Re-staging after the gate
        # passed leaves the working file untouched, so only the per-entry
        # index re-check can tell that the blob about to be thrown away is no
        # longer the one the operator confirmed. Kept and reported, never
        # restored over.
        wt = self.make_git_worktree()
        self.stage_over_working_copy(wt, "tracked.txt", staged="staged-a",
                                     working="working-b")
        self.session_with_worktree(wt)
        obj = self.preview(1)
        real_discard = recovery._discard_worktree_files

        def restage_then_discard(worktree, plan):
            self.stage_blob(wt, "tracked.txt", "staged-c-after-the-gate")
            return real_discard(worktree, plan)

        with mock.patch.object(recovery, "_discard_worktree_files",
                               restage_then_discard):
            status, result = self.post(obj, preserve_worktree=False,
                                       discard_worktree=True,
                                       confirm_shortname="s1")
        self.assertEqual(status, 200, result)
        self.assertEqual(self.staged(wt, "tracked.txt"),
                         "staged-c-after-the-gate")
        self.assertEqual((Path(wt) / "tracked.txt").read_text(), "working-b")
        self.assertEqual(result["worktree"]["kept"], ["tracked.txt"])
        self.assertFalse(result["worktree"]["discarded"])
        # ...and the rest of the consented set still goes: keeping one entry
        # is not a licence to abandon the discard.
        self.assertFalse((Path(wt) / "untracked.txt").exists())

    def test_restage_during_the_removal_loop_is_kept(self):
        # SC-124: the per-entry re-check read the index ONCE, at the top of
        # the discard, and the removal loop then runs for arbitrarily many
        # entries before the destructive `restore`. A stage landing in that
        # window is invisible to a snapshot taken before it, so the restore
        # threw the blob away and still reported discarded=true. The index has
        # to be revalidated where the worktree is — immediately before the
        # destructive call. Injecting from `_open_parent` puts the stage
        # exactly inside the loop: after the snapshot, before the restore.
        wt = self.make_git_worktree()
        self.stage_over_working_copy(wt, "tracked.txt", staged="staged-a",
                                     working="working-b")
        plan = self.plan(wt)
        real_open = recovery._open_parent

        def staging_open(worktree, rel):
            fd = real_open(worktree, rel)
            self.stage_blob(wt, "tracked.txt", "staged-c-mid-removal")
            return fd

        with mock.patch.object(recovery, "_open_parent", staging_open):
            result = recovery._discard_worktree_files(wt, plan)
        self.assertEqual(self.staged(wt, "tracked.txt"),
                         "staged-c-mid-removal")
        self.assertEqual((Path(wt) / "tracked.txt").read_text(), "working-b")
        self.assertEqual(result["kept"], ["tracked.txt"])
        self.assertFalse(result["discarded"])
        # ...and the rest of the consented set still goes.
        self.assertFalse((Path(wt) / "untracked.txt").exists())

    def test_unreadable_index_before_the_restore_keeps_everything(self):
        # The revalidation fails closed the same way the first read does: an
        # index that cannot be read immediately before the destructive call
        # leaves every enumerated entry unverifiable, and an unverifiable
        # identity is never a licence to restore over it.
        wt = self.make_git_worktree()
        plan = self.plan(wt)
        real = recovery._index_identities
        calls = []

        def failing_after_the_first(worktree):
            calls.append(worktree)
            if len(calls) > 1:
                raise recovery._GitEvidenceUnavailable("index gone")
            return real(worktree)

        with mock.patch.object(recovery, "_index_identities",
                               failing_after_the_first):
            result = recovery._discard_worktree_files(wt, plan)
        self.assertGreater(len(calls), 1, "the index was never re-read")
        self.assertFalse(result["discarded"])
        self.assertIn("tracked.txt", result["kept"])
        self.assertEqual((Path(wt) / "tracked.txt").read_text(), "dirty")

    def test_discard_preserves_the_durable_index_flags(self):
        # SC-125, the other half. The bits are not part of HEAD, so "throw the
        # entry back to HEAD" does not say what should happen to them —
        # clearing them is a side effect, not a discarded change. The content
        # goes (that is what was consented to); the flags are put back.
        wt = self.make_git_worktree()
        self.stage_only(wt, "tracked.txt", "staged-only")
        self.git_run(wt, "update-index", "--skip-worktree", "--",
                     "tracked.txt")
        self.git_run(wt, "update-index", "--assume-unchanged", "--",
                     "tracked.txt")
        self.assertEqual(self.index_tag(wt, "tracked.txt"), "s")
        result = recovery._discard_worktree_files(wt, self.plan(wt))
        self.assertTrue(result["discarded"], result)
        self.assertEqual(result["flags_lost"], [])
        self.assertEqual(self.staged(wt, "tracked.txt"), "v1")
        self.assertEqual(self.index_tag(wt, "tracked.txt"), "s")

    def test_flags_that_cannot_be_preserved_are_reported(self):
        # A staged-NEW path leaves the index entirely, so its flag has nowhere
        # to live. Preserve where the store allows it, report where it does
        # not — never clear it silently.
        wt = self.make_git_worktree()
        (Path(wt) / "added.txt").write_text("new")
        self.git_run(wt, "add", "--", "added.txt")
        self.git_run(wt, "update-index", "--skip-worktree", "--", "added.txt")
        result = recovery._discard_worktree_files(wt, self.plan(wt))
        self.assertTrue(result["discarded"], result)
        self.assertEqual(result["flags_lost"], ["added.txt"])
        self.assertEqual(self.git_run(wt, "ls-files", "-v", "--",
                                      "added.txt"), "")

    def test_discard_never_recurses_into_a_submodule(self):
        # The THIRD store. A submodule has its own worktree, index and refs,
        # and none of them are inside this fence: the host sees only a gitlink
        # and a directory, so a commit made inside the submodule after the
        # preview moves neither. With `submodule.recurse` configured, `git
        # restore` follows the gitlink and resets that store too — destroying
        # work the operator was never shown. The discard pins the flag off, so
        # a config setting cannot widen a consented blast radius.
        wt = self.make_git_worktree()
        sub = Path(self.tmp.name) / "submodule-origin"
        sub.mkdir()
        self.git_run(str(sub), "init", "-q", "-b", "main")
        self.git_run(str(sub), "config", "user.email", "t@t")
        self.git_run(str(sub), "config", "user.name", "t")
        (sub / "s.txt").write_text("s1")
        self.git_run(str(sub), "add", "-A")
        self.git_run(str(sub), "commit", "-qm", "s1")
        self.git_run(wt, "-c", "protocol.file.allow=always", "submodule",
                     "add", "-q", str(sub), "sm")
        self.git_run(wt, "commit", "-qm", "add submodule")
        self.git_run(wt, "config", "submodule.recurse", "true")
        # work committed INSIDE the submodule — the host's gitlink now differs
        inner = str(Path(wt) / "sm")
        # its own repo, so its own identity: a clone inherits neither the
        # host worktree's config nor a global one CI does not have.
        self.git_run(inner, "config", "user.email", "t@t")
        self.git_run(inner, "config", "user.name", "t")
        (Path(inner) / "local.txt").write_text("work only the submodule holds")
        self.git_run(inner, "add", "-A")
        self.git_run(inner, "commit", "-qm", "inner work")
        head = self.git_run(inner, "rev-parse", "HEAD")

        result = recovery._discard_worktree_files(wt, self.plan(wt))
        self.assertEqual(self.git_run(inner, "rev-parse", "HEAD"), head)
        self.assertTrue((Path(inner) / "local.txt").exists())
        # ...and say WHY it survived, so this cannot pass vacuously: today the
        # SC-103 guard keeps it, because a checked-out submodule is a
        # directory. That is a coincidence of a different rule.
        self.assertIn("sm", result["kept"])

        # So prove the pin does the work on its own, with that guard out of
        # the way: the restore now really does run on the gitlink.
        with mock.patch.object(recovery, "_is_dir", return_value=False):
            recovery._discard_worktree_files(wt, self.plan(wt))
        self.assertEqual(self.git_run(inner, "rev-parse", "HEAD"), head)
        self.assertTrue((Path(inner) / "local.txt").exists())

    # -- the REPORTED OUTCOME is a guarantee, not a by-product --------------
    # Everything above asserts a safety property: the work survives. These
    # assert the other half — that the result SAYS what actually happened.
    # `discarded` true only if everything consented to was really undone,
    # `kept` naming everything spared, `flags_lost` naming every bit that
    # could not be put back, and a failure after the durable commit reported
    # as a partial discard rather than an opaque 500 (decision #45 ranks
    # misreporting alongside destruction).

    def nested_dirty_tracked(self, wt: str) -> None:
        """A tracked, dirty file one directory down — the shape whose parent
        can be replaced without the entry's own identity moving."""
        (Path(wt) / "d").mkdir()
        (Path(wt) / "d" / "f.txt").write_text("v1")
        self.git_run(wt, "add", "-A")
        self.git_run(wt, "commit", "-qm", "nested")
        (Path(wt) / "d" / "f.txt").write_text("dirty")

    def test_restore_that_exits_zero_without_working_is_not_claimed(self):
        # SC-127's class: an exit code is not an outcome. `discarded` must
        # mean "verified undone", not "the command did not complain". Forced
        # deterministically — the restore is replaced by a no-op that exits 0
        # — because what must hold is independent of which git versions can be
        # provoked into skipping a path.
        wt = self.make_git_worktree()
        plan = self.plan(wt)
        real_run = subprocess.run

        def lying_restore(args, **kw):
            if "restore" in args:
                return subprocess.CompletedProcess(args, 0, b"", b"")
            return real_run(args, **kw)

        with mock.patch.object(recovery.subprocess, "run", lying_restore):
            result = recovery._discard_worktree_files(wt, plan)
        self.assertIn("tracked.txt", result["kept"])
        self.assertFalse(result["discarded"])
        self.assertIsNone(result["failed"])
        self.assertEqual((Path(wt) / "tracked.txt").read_text(), "dirty")

    def test_parent_swapped_for_a_same_inode_symlink_is_reported_truthfully(
            self):
        # The shape SC-127 was reported as: after the late gate the entry's
        # parent is moved out of the worktree and a symlink to the SAME inode
        # put in its place, so the per-entry re-check still matches — it
        # lstats the leaf, and the leaf is the same file.
        #
        # On git 2.47.3 the restore does NOT skip: it removes the symlink,
        # recreates the real directory and writes HEAD content, so the
        # worktree entry genuinely IS discarded and `discarded=true` is
        # truthful. The dirty bytes that survive are the copy the operator
        # moved OUTSIDE the worktree, which no discard was ever scoped to
        # touch. Pinned because the report and the safety property are easy to
        # confuse here, and because a future git that really did skip would
        # now be caught by the verification above rather than mis-reported.
        wt = self.make_git_worktree()
        self.nested_dirty_tracked(wt)
        plan = self.plan(wt)
        moved = Path(self.tmp.name) / "moved-parent"
        os.rename(Path(wt) / "d", moved)
        os.symlink(moved, Path(wt) / "d")

        result = recovery._discard_worktree_files(wt, plan)
        self.assertEqual((moved / "f.txt").read_text(), "dirty")  # outside
        self.assertEqual((Path(wt) / "d" / "f.txt").read_text(), "v1")
        self.assertFalse(Path(Path(wt) / "d").is_symlink())
        self.assertTrue(result["discarded"], result)
        self.assertEqual(result["kept"], [])

    def test_parent_swapped_for_a_different_symlink_is_kept(self):
        # ...and when the swap DOES make the entry unreadable as observed, it
        # is kept and named — the existing per-entry check already covers it,
        # which is the other half of why the same-inode case above is the only
        # one that reaches the restore at all.
        wt = self.make_git_worktree()
        self.nested_dirty_tracked(wt)
        (Path(wt) / "other").mkdir()
        plan = self.plan(wt)
        os.rename(Path(wt) / "d", Path(self.tmp.name) / "elsewhere")
        os.symlink("other", Path(wt) / "d")

        result = recovery._discard_worktree_files(wt, plan)
        self.assertIn("d/f.txt", result["kept"])
        self.assertFalse(result["discarded"])

    def test_flag_restoration_failure_is_reported_not_raised(self):
        # SC-126. `_restore_index_flags` runs AFTER the destructive restore
        # and after the durable closure is committed, so an exception there
        # cannot be unwound and must not escape: it would surface as an opaque
        # 500 while the staged content is already reset and the flag already
        # cleared — the truth absent exactly where it matters most. The
        # failure is absorbed and the real loss is reported, verified by
        # re-reading the index rather than trusted from an exit code.
        wt = self.make_git_worktree()
        self.stage_only(wt, "tracked.txt", "staged-only")
        self.git_run(wt, "update-index", "--skip-worktree", "--",
                     "tracked.txt")
        plan = self.plan(wt)
        real_run = subprocess.run

        def timeout_on_update_index(args, **kw):
            if "update-index" in args:
                raise subprocess.TimeoutExpired(args, 60)
            return real_run(args, **kw)

        with mock.patch.object(recovery.subprocess, "run",
                               timeout_on_update_index):
            result = recovery._discard_worktree_files(wt, plan)
        self.assertEqual(result["flags_lost"], ["tracked.txt"])
        self.assertIsNone(result["failed"])   # the discard itself completed
        self.assertTrue(result["discarded"])
        self.assertEqual(self.index_tag(wt, "tracked.txt"), "H")

    def test_unexpected_failure_after_the_commit_reports_a_partial_discard(
            self):
        # The net, at the endpoint. Whatever goes wrong once the durable
        # closure is committed, the operator gets a result describing their
        # files — never a 500 that says only that something broke. Reported,
        # not swallowed: the exception is named in `failed`.
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        obj = self.preview(1)
        with mock.patch.object(recovery, "_restore_index_flags",
                               side_effect=RuntimeError("boom")):
            status, result = self.post(obj, preserve_worktree=False,
                                       discard_worktree=True,
                                       confirm_shortname="s1")
        self.assertEqual(status, 200, result)
        self.assertFalse(result["worktree"]["discarded"])
        self.assertEqual(result["worktree"]["failed"]["step"], "unexpected")
        self.assertIn("RuntimeError", result["worktree"]["failed"]["error"])
        # the closure still happened and is still reported honestly
        self.assertEqual(result["availability"], "available")

    def test_unreadable_index_at_the_discard_keeps_everything(self):
        # An index that cannot be read between the gate and the delete means
        # no entry's identity can be verified — and an unverifiable identity
        # is never a licence to delete. Everything is kept and reported;
        # nothing is silently erased on a half-check.
        wt = self.make_git_worktree()
        plan = self.plan(wt)
        with mock.patch.object(
                recovery, "_index_identities",
                side_effect=recovery._GitEvidenceUnavailable("index gone")):
            result = recovery._discard_worktree_files(wt, plan)
        self.assertFalse(result["discarded"])
        self.assertEqual(sorted(result["kept"]),
                         ["tracked.txt", "untracked.txt"])
        self.assertEqual((Path(wt) / "tracked.txt").read_text(), "dirty")
        self.assertTrue((Path(wt) / "untracked.txt").exists())

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

    # -- the outcome is checked against the CONTRACT, not a proxy (SC-129) --
    # "Verified undone" was verified by asking whether the entry still held
    # the identity the operator was shown. The contract implies that, so the
    # right answer satisfies it — and so does a restore that writes some THIRD
    # state, which is neither the consented content nor HEAD. The check now
    # re-runs the enumeration that DEFINED the delete set and requires the
    # entry to be absent from it, which is the contract itself.

    def third_state_restore(self, wt: str, rel: str, text: str, *,
                            stage: bool = False):
        """A `git restore` replaced by one that exits 0 having put `rel` in a
        state that is neither what the operator consented to discarding nor
        HEAD. `stage=True` also does the INDEX half for real, so only the
        worktree half is wrong."""
        real_run = subprocess.run

        def stub(args, **kw):
            if "restore" not in args:
                return real_run(args, **kw)
            if stage:
                real_run(["git", "-C", wt, "restore", "--source=HEAD",
                          "--staged", "--", rel], capture_output=True,
                         check=True)
            (Path(wt) / rel).write_text(text)
            return subprocess.CompletedProcess(args, 0, b"", b"")
        return stub

    def test_restore_that_writes_a_third_state_is_not_claimed_discarded(self):
        # SC-129 itself. The entry changed, so the old check passed it and the
        # discard was reported complete over content that was never committed
        # and never consented to — the operator is told their work is gone AND
        # that the tree is at HEAD, and neither is true.
        wt = self.make_git_worktree()
        plan = self.plan(wt)
        with mock.patch.object(
                recovery.subprocess, "run",
                self.third_state_restore(wt, "tracked.txt", "NEITHER")):
            result = recovery._discard_worktree_files(wt, plan)
        # the proxy is SATISFIED — not the consented state, and it moved...
        self.assertNotIn((Path(wt) / "tracked.txt").read_text(),
                         ("dirty", "v1"))
        # ...and the contract is not: the entry still differs from HEAD.
        self.assertIn("tracked.txt", result["kept"])
        self.assertFalse(result["discarded"])
        self.assertIsNone(result["failed"])

    def test_destaged_file_left_in_the_worktree_is_not_claimed_discarded(self):
        # The same hole one entity over. A staged-NEW path is undone by
        # leaving the index AND the worktree; drop the index entry, rewrite
        # the file and the proxy sees a changed entry and reports it
        # discarded, while the file sits there as untracked work. The
        # enumeration names it exactly as it would have at plan time.
        wt = self.make_git_worktree()
        (Path(wt) / "added.txt").write_text("new")
        self.git_run(wt, "add", "--", "added.txt")
        plan = self.plan(wt)
        real_run = subprocess.run

        def destage_only(args, **kw):
            if "restore" not in args:
                return real_run(args, **kw)
            real_run(["git", "-C", wt, "rm", "--cached", "--quiet", "--",
                      "added.txt"], capture_output=True, check=True)
            (Path(wt) / "added.txt").write_text("NEITHER")
            return subprocess.CompletedProcess(args, 0, b"", b"")

        with mock.patch.object(recovery.subprocess, "run", destage_only):
            result = recovery._discard_worktree_files(wt, plan)
        self.assertEqual((Path(wt) / "added.txt").read_text(), "NEITHER")
        self.assertIn("added.txt", result["kept"])
        self.assertFalse(result["discarded"])

    def test_staged_then_deleted_path_is_enumerated_and_discarded(self):
        # The same question asked of the ENUMERATION rather than of the check
        # that reads it. `git add` then `rm` leaves a blob in the index and
        # nothing on disk, so `git diff HEAD` — HEAD against the WORKING TREE
        # — sees no change at all: the entry was in no preview and no delete
        # set, survived the discard, and `discarded=true` was returned over
        # staged work the operator believed they had thrown away. `reset
        # --hard`, the operation this replaces, destroyed it.
        wt = self.make_git_worktree()
        (Path(wt) / "added.txt").write_text("staged then deleted")
        self.git_run(wt, "add", "--", "added.txt")
        (Path(wt) / "added.txt").unlink()
        self.assertIn("AD added.txt", self.porcelain(wt))
        self.assertEqual(self.git_run(wt, "diff", "HEAD", "--name-only",
                                      "--", "added.txt"), "",
                         "the worktree diff names it — this case no longer "
                         "isolates the index-only difference")
        plan = self.plan(wt)
        self.assertIn("added.txt", plan["tracked"])
        result = recovery._discard_worktree_files(wt, plan)
        self.assertTrue(result["discarded"], result)
        self.assertEqual(result["kept"], [])
        self.assertEqual(self.git_run(wt, "ls-files", "--", "added.txt"), "")

    # -- SC-130: the preview is DERIVED from the enumeration ---------------

    def projected_worktree(self, wt: str) -> str:
        """The canonical worktree row both clients render, for `wt`."""
        rows = recovery.evidence_projection(
            {"shell": {}, "process": {}, "git": recovery._git_facts(wt)},
            "exact_idle_orphan", ["recover"])
        return next(row["value"] for row in rows if row["key"] == "worktree")

    def test_index_only_entry_is_previewed_unlike_a_plain_deletion(self):
        # The ratification condition. Widening the blast radius to the `AD`
        # path is legitimate only because enumerated means previewed means
        # consented — so if the operator cannot SEE that a staged blob will
        # go, the consent is fictional and the widening does not stand.
        #
        # It was invisible because the preview was a THIRD STATEMENT of the
        # plan: it counted porcelain lines instead of deriving from the set
        # the destruction is built from, and porcelain renders both of these
        # as exactly one dirty tracked line.
        deleted = self.make_git_worktree()
        (Path(deleted) / "tracked.txt").unlink()
        (Path(deleted) / "untracked.txt").unlink()

        staged = self.make_git_worktree()
        (Path(staged) / "added.txt").write_text("staged then deleted")
        self.git_run(staged, "add", "--", "added.txt")
        (Path(staged) / "added.txt").unlink()
        (Path(staged) / "tracked.txt").write_text("v1")   # back to HEAD
        (Path(staged) / "untracked.txt").unlink()

        # The premise, reproduced rather than argued: to porcelain — the old
        # source of these counts — the two worktrees are the same shape.
        self.assertEqual(len(self.porcelain(deleted).splitlines()), 1)
        self.assertEqual(len(self.porcelain(staged).splitlines()), 1)
        self.assertIn("AD added.txt", self.porcelain(staged))
        self.assertIn(" D tracked.txt", self.porcelain(deleted))

        deleted_row = self.projected_worktree(deleted)
        staged_row = self.projected_worktree(staged)
        self.assertNotEqual(
            deleted_row, staged_row,
            "a discard destroying an index-only staged blob still reads "
            "byte-identically to one deleting a file on disk")
        # The exact negative: an ordinary deletion must not acquire the
        # staged-content wording, or the distinction says nothing.
        self.assertNotIn("staged-only", deleted_row)
        self.assertIn("1 tracked", deleted_row)
        # And the index-only entry is named for what it actually is.
        self.assertIn("1 of them staged-only", staged_row)
        self.assertIn("the working tree does not show", staged_row)
        self.assertIn("a discard destroys it", staged_row)

    def test_preview_counts_come_from_the_enumeration_not_porcelain(self):
        # One definition, three consumers. An untracked DIRECTORY is the case
        # where the two sources visibly disagree: porcelain collapses it to a
        # single `?? dir/` line, while the enumeration — which is what the
        # discard acts on — names each file inside it plus the directory.
        wt = self.make_git_worktree()
        (Path(wt) / "untracked.txt").unlink()
        (Path(wt) / "tracked.txt").write_text("v1")      # back to HEAD
        nested = Path(wt) / "scratch"
        nested.mkdir()
        (nested / "a.txt").write_text("a")
        (nested / "b.txt").write_text("b")
        self.assertEqual(self.porcelain(wt).splitlines(), ["?? scratch/"])

        plan = self.plan(wt)
        self.assertEqual(sorted(plan["untracked_files"]),
                         ["scratch/a.txt", "scratch/b.txt"])
        self.assertEqual(plan["untracked_dirs"], ["scratch/"])
        row = self.projected_worktree(wt)
        self.assertIn("2 untracked file(s)", row)
        self.assertIn("1 untracked dir(s)", row)

    def test_unborn_staged_then_deleted_entry_is_index_only_too(self):
        # The same CLASS one step out, tested where no bug has visited: with
        # no HEAD to differ from, an index entry is invisible on disk exactly
        # when there is no file. The born-HEAD case is the one that was
        # reported; this one follows from the rule and would otherwise ship
        # rendering a destroyed staged blob as an ordinary dirty entry.
        wt = Path(self.tmp.name) / "unborn-index-only"
        wt.mkdir()
        self.git_run(str(wt), "init", "-q", "-b", "feat/x", ".")
        (wt / "gone.txt").write_text("staged then deleted")
        (wt / "kept.txt").write_text("staged and present")
        self.git_run(str(wt), "add", "--", "gone.txt", "kept.txt")
        (wt / "gone.txt").unlink()

        plan = self.plan(str(wt))
        self.assertEqual(sorted(plan["tracked"]), ["gone.txt", "kept.txt"])
        self.assertEqual(plan["index_only"], ["gone.txt"])
        row = self.projected_worktree(str(wt))
        self.assertIn("2 tracked", row)
        self.assertIn("1 of them staged-only", row)

    def test_unborn_entry_left_on_disk_is_not_claimed_discarded(self):
        # On an unborn HEAD "undone" means gone from the index AND gone from
        # disk, and only the first half was checked — `git rm --cached`
        # succeeds either way, so a file the removal loop failed to unlink was
        # reported discarded while standing there as untracked work. One
        # enumeration answers both halves.
        wt = Path(self.tmp.name) / "unborn-third-state"
        wt.mkdir()
        self.git_run(str(wt), "init", "-q", "-b", "feat/x", ".")
        (wt / "staged.txt").write_text("staged")
        self.git_run(str(wt), "add", "--", "staged.txt")
        plan = self.plan(str(wt))
        self.assertIn("staged.txt", plan["tracked"])

        def unlink_nothing(_name, **_kw):
            return None

        with mock.patch.object(recovery.os, "unlink", unlink_nothing):
            result = recovery._discard_worktree_files(str(wt), plan)
        self.assertEqual(self.git_run(str(wt), "ls-files"), "")  # index: gone
        self.assertTrue((wt / "staged.txt").exists())            # disk: not
        self.assertIn("staged.txt", result["kept"])
        self.assertFalse(result["discarded"])

    def test_entry_still_flagged_after_the_restore_is_not_verified(self):
        # The enumeration reads a flagged entry's worktree half THROUGH the
        # index — `git diff` trusts skip-worktree and will not look at the
        # file — so an entry still carrying the bit here cannot be verified by
        # it. Forced: the index half is done for real (so the entry no longer
        # differs from HEAD there) while the worktree is left in a third state
        # and the bit left standing, which is the one shape the enumeration
        # cannot see.
        wt = self.make_git_worktree()
        self.stage_only(wt, "tracked.txt", "staged-only")
        self.git_run(wt, "update-index", "--skip-worktree", "--",
                     "tracked.txt")
        plan = self.plan(wt)
        with mock.patch.object(
                recovery.subprocess, "run",
                self.third_state_restore(wt, "tracked.txt", "NEITHER",
                                         stage=True)):
            result = recovery._discard_worktree_files(wt, plan)
        # the premise, reproduced rather than argued: git reports nothing.
        self.assertEqual(self.index_tag(wt, "tracked.txt"), "S")
        self.assertEqual(self.git_run(wt, "diff", "HEAD", "--name-only"), "")
        self.assertEqual((Path(wt) / "tracked.txt").read_text(), "NEITHER")
        self.assertIn("tracked.txt", result["kept"])
        self.assertFalse(result["discarded"])

    def test_kept_entry_keeps_the_durable_flags_the_restore_cleared(self):
        # `restore --staged` clears the bits whether or not it goes on to do
        # what it promised, and they were only put back for the entries that
        # verified. An entry reported as SPARED whose durable bit was silently
        # dropped is spared in name only — the flags follow the set the
        # command ran over, not the set it satisfied.
        wt = self.make_git_worktree()
        self.stage_only(wt, "tracked.txt", "staged-only")
        self.git_run(wt, "update-index", "--skip-worktree", "--",
                     "tracked.txt")
        plan = self.plan(wt)
        real_run = subprocess.run

        def restore_then_redirty(args, **kw):
            out = real_run(args, **kw)
            if "restore" in args:
                (Path(wt) / "tracked.txt").write_text("NEITHER")
            return out

        with mock.patch.object(recovery.subprocess, "run",
                               restore_then_redirty):
            result = recovery._discard_worktree_files(wt, plan)
        self.assertIn("tracked.txt", result["kept"])
        self.assertFalse(result["discarded"])
        self.assertEqual(result["flags_lost"], [])
        self.assertEqual(self.index_tag(wt, "tracked.txt"), "S")

    # -- a HIDDEN entry is still the operator's work (SC-132) --------------
    # `assume-unchanged` is a promise the operator makes to git, not a fact
    # about the file: git stops stat'ing the entry, so `diff HEAD`, `status`
    # and porcelain all call it clean while its bytes really differ. The
    # enumeration trusted that hint, so the work was in no preview, no gate and
    # no delete set — and the discard reported success over it. `reset --hard`,
    # the operation being replaced, destroys it. Every premise below is
    # reproduced against real git rather than argued.

    def hidden_dirty(self, wt: str, rel: str, text: str) -> None:
        """Write `text` to a tracked, currently-clean `rel` behind the
        assume-unchanged hint, and pin the premise: git reports NOTHING."""
        self.git_run(wt, "update-index", "--assume-unchanged", "--", rel)
        (Path(wt) / rel).write_text(text)
        self.assertNotIn(rel, self.git_run(wt, "diff", "HEAD",
                                           "--name-only").split(),
                         "git sees the change — the hint no longer hides it "
                         "and this case tests nothing")
        self.assertEqual(self.index_tag(wt, rel), "h")

    def commit_pushed(self, wt: str, rel: str, text: str) -> None:
        """A second tracked, clean, PUSHED file — pushed because an unpushed
        commit refuses the discard on its own and would mask the case."""
        (Path(wt) / rel).write_text(text)
        self.git_run(wt, "add", "--", rel)
        self.git_run(wt, "commit", "-qm", f"add {rel}")
        self.git_run(wt, "push", "-q", "origin", "feat/x")

    def test_reset_hard_destroys_hidden_work_but_spares_skip_worktree(self):
        # THE PREMISE, and the reason the two bits are treated differently.
        # This discard stands in for `reset --hard`, so what that command does
        # is the boundary: under-discarding leaves consented work alive and
        # reports it destroyed, over-discarding destroys work the operator
        # never put inside the radius. Reproduced, because the whole fix turns
        # on which side of that line each bit falls.
        wt = self.make_git_worktree()
        self.commit_pushed(wt, "sparse.txt", "s1")
        self.hidden_dirty(wt, "clean.txt", "HIDDEN")
        self.git_run(wt, "update-index", "--skip-worktree", "--", "sparse.txt")
        (Path(wt) / "sparse.txt").write_text("SPARSE-DIRTY")

        self.git_run(wt, "reset", "--hard", "HEAD")
        self.assertEqual((Path(wt) / "clean.txt").read_text(), "c1",
                         "reset --hard spared the assume-unchanged file — it "
                         "is outside the replaced blast radius after all")
        self.assertEqual((Path(wt) / "sparse.txt").read_text(), "SPARSE-DIRTY",
                         "reset --hard destroyed the skip-worktree file — "
                         "excluding it now under-discards")
        # ...and the bits themselves are durable across it, which is why the
        # discard has to put them back rather than treat them as content.
        self.assertEqual(self.index_tag(wt, "clean.txt"), "h")
        self.assertEqual(self.index_tag(wt, "sparse.txt"), "S")

    def test_preview_exposes_hidden_work_as_tracked(self):
        # The consent half. The operator is shown what the discard will
        # destroy, so a preview that renders this worktree "clean" is asking
        # for consent to something it has not disclosed.
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        self.hidden_dirty(wt, "clean.txt", "HIDDEN")
        obj = self.preview(1)
        self.assertEqual(obj["evidence"]["git"]["dirty_tracked"], 2,
                         "the hidden file is missing from the preview")
        self.assertIn("clean.txt", self.plan(wt)["tracked"])

    def test_public_discard_restores_hidden_work_and_keeps_the_flag(self):
        # The whole contract end-to-end on the PUBLIC path: previewed, so
        # consented; restored to HEAD, so the report is true; and the
        # operator's standing instruction about the path survives, because
        # clearing the bit is a side effect of the command rather than a change
        # they confirmed (SC-125).
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        self.hidden_dirty(wt, "clean.txt", "HIDDEN")
        obj = self.preview(1)
        status, result = self.post(obj, preserve_worktree=False,
                                   discard_worktree=True,
                                   confirm_shortname="s1")
        self.assertEqual(status, 200, result)
        self.assertEqual((Path(wt) / "clean.txt").read_text(), "c1")
        self.assertTrue(result["worktree"]["discarded"], result["worktree"])
        self.assertEqual(result["worktree"]["kept"], [])
        self.assertEqual(result["worktree"]["flags_lost"], [])
        self.assertEqual(self.index_tag(wt, "clean.txt"), "h")

    def test_hidden_deletion_is_enumerated_and_restored(self):
        # The same hint hides a DELETION, and `reset --hard` puts the file
        # back. This is the case that rules out `update-index --really-refresh`
        # as the fix: it ignores the hint but only re-stats paths that still
        # exist, so it never names this one.
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        self.git_run(wt, "update-index", "--assume-unchanged", "--",
                     "clean.txt")
        (Path(wt) / "clean.txt").unlink()
        self.assertNotIn("clean.txt", self.git_run(wt, "diff", "HEAD",
                                                   "--name-only").split())
        obj = self.preview(1)
        self.assertEqual(obj["evidence"]["git"]["dirty_tracked"], 2)
        status, result = self.post(obj, preserve_worktree=False,
                                   discard_worktree=True,
                                   confirm_shortname="s1")
        self.assertEqual(status, 200, result)
        self.assertEqual((Path(wt) / "clean.txt").read_text(), "c1")
        self.assertTrue(result["worktree"]["discarded"], result["worktree"])

    def test_hidden_bytes_changed_after_preview_refuse_the_discard(self):
        # The freshness fence over the newly-enumerated class. The porcelain
        # line set stays byte-identical here — it is EMPTY either side, since
        # the hint suppresses both states — so nothing but binding the entry
        # itself can tell the two worlds apart.
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        self.hidden_dirty(wt, "clean.txt", "HIDDEN")
        self.assert_content_edit_refuses(wt, "clean.txt", "HIDDEN-AGAIN")

    def test_hidden_bytes_changed_before_the_late_gate_are_preserved(self):
        # Past the execute-entry gate, the per-entry re-check immediately
        # before the destructive command is the last thing standing between
        # late work and deletion. An entry that moved since the plan is spared
        # and NAMED, and the result must not claim a full discard.
        wt = self.make_git_worktree()
        self.hidden_dirty(wt, "clean.txt", "HIDDEN")
        plan = self.plan(wt)
        self.assertIn("clean.txt", plan["tracked"])
        (Path(wt) / "clean.txt").write_text("WRITTEN-AFTER-THE-GATE")

        result = recovery._discard_worktree_files(wt, plan)
        self.assertEqual((Path(wt) / "clean.txt").read_text(),
                         "WRITTEN-AFTER-THE-GATE")
        self.assertIn("clean.txt", result["kept"])
        self.assertFalse(result["discarded"])

    def test_skip_worktree_dirty_file_is_left_outside_the_discard(self):
        # The audit's conclusion, pinned as behaviour. skip-worktree is NOT
        # unhidden: `reset --hard` spares such an entry (above), `git restore`
        # refuses the pathspec outright, and clearing the bit would re-
        # materialise a sparse checkout's deliberately absent files. So it is
        # excluded on purpose — and the report stays true, because an entry
        # outside the enumerated set is outside every claim made about it.
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        self.commit_pushed(wt, "sparse.txt", "s1")
        self.git_run(wt, "update-index", "--skip-worktree", "--", "sparse.txt")
        (Path(wt) / "sparse.txt").write_text("SPARSE-DIRTY")

        obj = self.preview(1)
        self.assertNotIn("sparse.txt", self.plan(wt)["tracked"])
        status, result = self.post(obj, preserve_worktree=False,
                                   discard_worktree=True,
                                   confirm_shortname="s1")
        self.assertEqual(status, 200, result)
        self.assertEqual((Path(wt) / "sparse.txt").read_text(),
                         "SPARSE-DIRTY")
        self.assertEqual(self.index_tag(wt, "sparse.txt"), "S")
        # The entry was never enumerated, so it is not a kept obstruction and
        # the entries that WERE consented did complete.
        self.assertNotIn("sparse.txt", result["worktree"]["kept"])
        self.assertTrue(result["worktree"]["discarded"], result["worktree"])

    def test_restored_hidden_entry_is_not_reported_kept(self):
        # The inverse misreport, and the reason the verification guard had to
        # move with the enumeration. The real restore command puts a hidden
        # entry back to HEAD and LEAVES THE BIT STANDING, so a guard that reads
        # "any durable bit still set" as "unverifiable" would report `kept`
        # over work it had in fact discarded — a false claim in the other
        # direction, which decision #45 ranks the same. Only skip-worktree, the
        # bit the enumeration really cannot see through, may do that.
        wt = self.make_git_worktree()
        self.hidden_dirty(wt, "clean.txt", "HIDDEN")
        plan = self.plan(wt)

        result = recovery._discard_worktree_files(wt, plan)
        self.assertEqual((Path(wt) / "clean.txt").read_text(), "c1")
        self.assertEqual(self.index_tag(wt, "clean.txt"), "h",
                         "the restore cleared the bit — this case no longer "
                         "reproduces the guard's input")
        self.assertEqual(result["kept"], [])
        self.assertTrue(result["discarded"], result)

    def test_the_operators_index_is_never_written_by_enumeration(self):
        # The unhiding runs against a THROWAWAY COPY. The operator's own index
        # — flags, staged content and all — must come through untouched, or the
        # fence would be mutating the very state it exists to protect.
        wt = self.make_git_worktree()
        self.hidden_dirty(wt, "clean.txt", "HIDDEN")
        self.stage_only(wt, "tracked.txt", "staged-only")
        before = self.git_run(wt, "ls-files", "-v", "--stage")

        self.assertIn("clean.txt", self.plan(wt)["tracked"])
        self.assertEqual(self.git_run(wt, "ls-files", "-v", "--stage"), before)
        # ...and the hint is still doing its job for everyone else afterwards.
        self.assertNotIn("clean.txt", self.git_run(wt, "diff", "HEAD",
                                                   "--name-only").split())

    def test_unhiding_failure_is_a_gap_not_a_clean_worktree(self):
        # Fail closed, the SC-087 way. If the unhiding cannot run, the honest
        # answer is "this worktree could not be observed completely" — which
        # declines the discard while still freeing the shell (SC-106). The
        # tempting alternative, falling back to the hint-trusting diff, would
        # hand back the incomplete set as though it were the whole truth, which
        # is precisely the defect being fixed.
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        self.hidden_dirty(wt, "clean.txt", "HIDDEN")
        real_run = subprocess.run

        def fail_update_index(args, **kw):
            if "update-index" in args:
                return subprocess.CompletedProcess(args, 1, b"", b"boom")
            return real_run(args, **kw)

        self.assert_gap_refuses_discard_but_frees_the_shell(
            wt, mock.patch.object(recovery.subprocess, "run",
                                  fail_update_index))

    def test_temp_index_creation_failure_is_a_gap_not_a_500(self):
        # SC-133. The unhiding needs a throwaway index, and CREATING it can
        # fail for reasons that have nothing to do with git: a full or
        # unwritable temp dir, an exhausted fd table. That OSError used to be
        # raised outside the translation, so it escaped the observation
        # entirely and the public endpoint answered a sanitized 500 — an
        # operator told "internal error" learns nothing about their files,
        # where "could not be observed completely" tells them the discard is
        # declined and the shell can still be freed. Same gap, same refusal,
        # whichever step could not run.
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        self.hidden_dirty(wt, "clean.txt", "HIDDEN")

        self.assert_gap_refuses_discard_but_frees_the_shell(
            wt, mock.patch.object(
                recovery.tempfile, "mkstemp",
                side_effect=OSError(errno.ENOSPC, "No space left on device")))
        # ...and the hidden file is untouched by the refusal, hint and all.
        self.assertEqual((Path(wt) / "clean.txt").read_text(), "HIDDEN")
        self.assertEqual(self.index_tag(wt, "clean.txt"), "h")

    def test_temp_index_close_failure_is_a_gap_not_a_500(self):
        # SC-133, the second failing step. `mkstemp` returns a descriptor AND
        # a path already on disk, so closing it is the one step whose failure
        # leaves a file behind: raised outside the translation it escaped as a
        # sanitized 500 *and* leaked the copy the `finally` exists to remove.
        # The mkstemp negative above cannot see that — moving `os.close` back
        # out of the try leaves every one of its assertions green — so the
        # step gets its own proof, taken through the public endpoint.
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        self.hidden_dirty(wt, "clean.txt", "HIDDEN")

        real_mkstemp, real_close = recovery.tempfile.mkstemp, recovery.os.close
        made, ours = [], set()

        def watched_mkstemp(*args, **kwargs):
            fd, path = real_mkstemp(*args, **kwargs)
            made.append(path)
            ours.add(fd)
            return fd, path

        def refusing_close(fd, *args, **kwargs):
            # ONLY the descriptor we handed out: `recovery.os` is the real os
            # module, so an unconditional raise would break subprocess's own
            # fd bookkeeping and prove nothing about the recovery path. Fire
            # once and forget the number — freed fds get reused.
            mine = fd in ours
            ours.discard(fd)
            real_close(fd, *args, **kwargs)   # the descriptor still goes back
            if mine:
                raise OSError(errno.EIO, "input/output error")

        @contextlib.contextmanager
        def closing_the_copy_fails():
            with mock.patch.object(recovery.tempfile, "mkstemp",
                                   watched_mkstemp), \
                    mock.patch.object(recovery.os, "close", refusing_close):
                yield

        self.assert_gap_refuses_discard_but_frees_the_shell(
            wt, closing_the_copy_fails())
        # ...the hidden file and its hint are untouched by the refusal...
        self.assertEqual((Path(wt) / "clean.txt").read_text(), "HIDDEN")
        self.assertEqual(self.index_tag(wt, "clean.txt"), "h")
        # ...and the copy the close aborted over is gone, which only holds
        # while the close sits inside the try whose `finally` unlinks it.
        self.assertTrue(made, "no throwaway index was created — the case "
                              "never reached the code it claims to test")
        for path in made:
            self.assertFalse(os.path.exists(path),
                             f"leaked throwaway index {path}")

    # -- NOTHING after the durable commit may surface as a 500 (SC-128) -----

    def test_late_gate_failure_after_the_commit_reports_a_partial_discard(
            self):
        # SC-128. The guarantee was built INSIDE the discard sequence, and the
        # late gate runs after the same commit and outside it: anything it
        # threw that was not its own refusal reached the operator as an opaque
        # internal error, with the session already ended and nothing said
        # about their files. Caught twice now by the path just outside the
        # structure, so the boundary is at the seam — everything past the
        # commit returns a result naming what happened.
        wt = self.make_git_worktree()
        self.session_with_worktree(wt)
        obj = self.preview(1)
        with mock.patch.object(recovery, "_assert_worktree_unchanged",
                               side_effect=RuntimeError("gate boom")):
            status, result = self.post(obj, preserve_worktree=False,
                                       discard_worktree=True,
                                       confirm_shortname="s1")
        self.assertEqual(status, 200, result)
        self.assertFalse(result["worktree"]["discarded"])
        self.assertEqual(result["worktree"]["completed"], [])
        self.assertEqual(result["worktree"]["failed"]["step"],
                         "worktree_gate")
        self.assertIn("RuntimeError", result["worktree"]["failed"]["error"])
        # ...and every other claim in that response is true: the gate ran
        # before anything destructive, so the files are untouched, and the
        # closure it could not unwind is still reported as done.
        self.assertEqual((Path(wt) / "tracked.txt").read_text(), "dirty")
        self.assertTrue((Path(wt) / "untracked.txt").exists())
        self.assertEqual(result["availability"], "available")
        con = self.db()
        self.assertEqual(con.execute(
            "SELECT occupancy FROM interface_sessions WHERE shell_id=1"
        ).fetchone()[0], "ended")
        con.close()


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

    def test_runtime_that_would_not_let_go_is_printed(self):
        # The operator-facing half of SC-128. Naming the failure in the
        # payload and rendering nothing is the silence it was named to end:
        # the recovery still succeeds (the durable rows ARE closed), so this
        # belongs on stderr as a follow-up, not as a failed exit.
        result = dict(self.RESULT)
        result["closed"] = dict(result["closed"],
                                runtime={"abandoned": False,
                                         "error": "RuntimeError: no"})
        self.script(dict(self.PREVIEW_ORPHAN), result)
        code, out, err = self.run_cli(["recover", "s3", "--yes"])
        self.assertEqual(code, 0)
        self.assertIn("S3 is available", out)
        self.assertIn("would not release the session generation", err)
        self.assertIn("RuntimeError: no", err)

    def test_runtime_released_cleanly_says_nothing(self):
        result = dict(self.RESULT)
        result["closed"] = dict(result["closed"], runtime={"abandoned": True})
        self.script(dict(self.PREVIEW_ORPHAN), result)
        code, _out, err = self.run_cli(["recover", "s3", "--yes"])
        self.assertEqual(code, 0)
        self.assertNotIn("generation", err)

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
