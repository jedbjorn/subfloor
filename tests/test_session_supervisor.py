#!/usr/bin/env python3
"""Tests for harness supervision and exact session-ownership leases."""
from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"
sys.path.insert(0, str(ENGINE / "scripts"))
import run  # noqa: E402
import session_control  # noqa: E402
import session_supervisor as supervisor  # noqa: E402


BINDING_SCHEMA = """
CREATE TABLE shells (
  shell_id INTEGER PRIMARY KEY,
  shortname TEXT,
  flavor TEXT,
  active_archive_id INTEGER
);
CREATE TABLE shell_memory_archives (
  archive_id INTEGER PRIMARY KEY,
  shell_id INTEGER NOT NULL,
  session_id TEXT NOT NULL,
  date TEXT,
  full_narrative TEXT,
  started_at TEXT,
  harness TEXT,
  provider TEXT,
  model TEXT,
  sprint_ref TEXT
);
CREATE TABLE session_token_usage (archive_id INTEGER);
CREATE TABLE shell_session_bindings (
  binding_id INTEGER PRIMARY KEY,
  archive_id INTEGER NOT NULL UNIQUE,
  shell_id INTEGER NOT NULL,
  harness TEXT NOT NULL,
  native_session_id TEXT,
  control_endpoint TEXT,
  control_capabilities TEXT NOT NULL DEFAULT '{}',
  cli_version TEXT,
  state TEXT NOT NULL,
  managed INTEGER NOT NULL DEFAULT 0,
  lease_pid INTEGER,
  lease_start_ticks INTEGER,
  supervisor_pid INTEGER,
  supervisor_start_ticks INTEGER,
  active_channel_pid INTEGER,
  active_channel_start_ticks INTEGER,
  active_channel_heartbeat_at TEXT,
  lease_generation INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (harness, native_session_id)
);
CREATE TABLE session_wake_jobs (
  wake_id INTEGER PRIMARY KEY,
  binding_id INTEGER NOT NULL,
  trigger_message_id INTEGER NOT NULL,
  state TEXT NOT NULL DEFAULT 'queued',
  attempt_count INTEGER NOT NULL DEFAULT 0,
  available_at TEXT,
  started_at TEXT,
  finished_at TEXT,
  last_error TEXT
);
"""


def make_db(path: str = ":memory:") -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=5)
    con.row_factory = sqlite3.Row
    con.executescript(BINDING_SCHEMA)
    con.execute("INSERT INTO shells VALUES (1, 'DEV1', 'planner', NULL)")
    con.execute(
        "INSERT INTO shell_memory_archives "
        "(archive_id, shell_id, session_id, date, full_narrative, harness, "
        "provider, model) VALUES "
        "(10, 1, '0007', '2026-07-21', "
        "'# 0007\\n[00:00] Session start.\\n[00:01] Work happened.', "
        "'claude', 'anthropic', 'opus')"
    )
    con.commit()
    return con


class FakeProc:
    def __init__(self, case: unittest.TestCase):
        self.tmp = tempfile.TemporaryDirectory()
        case.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def add(self, pid: int, *, ticks: int, pgrp: int | None = None,
            command: tuple[str, ...] = ("claude",), cwd: Path) -> None:
        proc = self.root / str(pid)
        proc.mkdir(exist_ok=True)
        fields = ["S", "1", str(pgrp if pgrp is not None else pid)]
        fields += ["0"] * 16
        fields.append(str(ticks))
        (proc / "stat").write_text(f"{pid} (harness worker) " + " ".join(fields))
        (proc / "cmdline").write_bytes(b"\0".join(p.encode() for p in command) + b"\0")
        cwd.mkdir(parents=True, exist_ok=True)
        (proc / "cwd").symlink_to(cwd, target_is_directory=True)

    def remove(self, pid: int) -> None:
        proc = self.root / str(pid)
        for child in proc.iterdir():
            child.unlink()
        proc.rmdir()


class ProcessIdentityTest(unittest.TestCase):
    def setUp(self):
        self.proc = FakeProc(self)
        self.worktree = self.proc.root / "repo" / ".sc-worktrees" / "dev1"

    def test_exact_identity_requires_ticks_command_and_worktree(self):
        self.proc.add(100, ticks=44, command=("node", "/opt/claude/cli.js"),
                      cwd=self.worktree / "src")
        self.assertTrue(supervisor.process_matches(
            100, 44, expected_command="claude", expected_worktree=self.worktree,
            proc_root=self.proc.root))
        self.assertFalse(supervisor.process_matches(
            100, 45, expected_command="claude", expected_worktree=self.worktree,
            proc_root=self.proc.root), "PID reuse must invalidate old start ticks")
        self.assertFalse(supervisor.process_matches(
            100, 44, expected_command="kimi", expected_worktree=self.worktree,
            proc_root=self.proc.root))
        self.assertFalse(supervisor.process_matches(
            100, 44, expected_command="claude",
            expected_worktree=self.proc.root / "other", proc_root=self.proc.root))

    def test_dead_leader_with_live_descendant_retains_group_evidence(self):
        self.proc.add(101, ticks=45, pgrp=100, command=("python", "helper.py"),
                      cwd=self.worktree)
        members = supervisor.process_group_members(
            100, expected_worktree=self.worktree, expected_command="claude",
            proc_root=self.proc.root)
        self.assertEqual([101], [p.pid for p in members])
        self.assertEqual(100, members[0].process_group)


class BindingTest(unittest.TestCase):
    def setUp(self):
        self.con = make_db()
        self.addCleanup(self.con.close)
        self.proc = FakeProc(self)
        self.repo = self.proc.root / "repo"
        self.worktree = self.repo / ".sc-worktrees" / "dev1"
        self.binding = supervisor.ensure_binding(
            self.con, archive_id=10, shell_id=1, harness="claude")

    def row(self) -> sqlite3.Row:
        return self.con.execute(
            "SELECT * FROM shell_session_bindings WHERE binding_id=?",
            (self.binding["binding_id"],),
        ).fetchone()

    def test_native_id_registration_is_idempotent_but_never_rebinds(self):
        supervisor.register_native_session(
            self.con, self.binding["binding_id"], "native-a",
            control_endpoint="/run/claude.sock", capabilities='{"resume":true}')
        supervisor.register_native_session(
            self.con, self.binding["binding_id"], "native-a")
        with self.assertRaisesRegex(ValueError, "different native session"):
            supervisor.register_native_session(
                self.con, self.binding["binding_id"], "native-b")
        row = self.row()
        self.assertEqual("native-a", row["native_session_id"])
        self.assertEqual("/run/claude.sock", row["control_endpoint"])
        self.assertEqual(1, self.con.execute(
            "SELECT COUNT(*) FROM shell_session_bindings").fetchone()[0])

    def test_live_owner_blocks_a_second_claim(self):
        self.proc.add(100, ticks=10, command=("claude",), cwd=self.worktree)
        self.proc.add(101, ticks=11, command=("claude",), cwd=self.worktree)
        generation = supervisor.claim_lease(
            self.con, self.binding["binding_id"], 100, repo_root=self.repo,
            state="foreground", proc_root=self.proc.root)
        with self.assertRaisesRegex(supervisor.LeaseConflict, r"not vacant \(live\)"):
            supervisor.preflight_lease(
                self.con, self.binding["binding_id"], repo_root=self.repo,
                proc_root=self.proc.root)
        with self.assertRaisesRegex(supervisor.LeaseConflict, "live owner pid 100"):
            supervisor.claim_lease(
                self.con, self.binding["binding_id"], 101, repo_root=self.repo,
                state="dispatching", proc_root=self.proc.root)
        row = self.row()
        self.assertEqual((100, 10, generation, "foreground"),
                         (row["lease_pid"], row["lease_start_ticks"],
                          row["lease_generation"], row["state"]))

    def test_pid_reuse_is_stale_and_old_generation_cannot_release_new_owner(self):
        self.proc.add(100, ticks=10, command=("claude",), cwd=self.worktree)
        first = supervisor.claim_lease(
            self.con, self.binding["binding_id"], 100, repo_root=self.repo,
            state="foreground", proc_root=self.proc.root)
        self.proc.remove(100)
        self.proc.add(100, ticks=99, command=("sleep", "999"), cwd=self.worktree)
        self.proc.add(101, ticks=11, command=("claude",), cwd=self.worktree)
        second = supervisor.claim_lease(
            self.con, self.binding["binding_id"], 101, repo_root=self.repo,
            state="dispatching", proc_root=self.proc.root)
        self.assertEqual(first + 1, second)
        self.assertFalse(supervisor.release_lease(
            self.con, self.binding["binding_id"], 100, 10, first))
        row = self.row()
        self.assertEqual((101, 11, second, "dispatching"),
                         (row["lease_pid"], row["lease_start_ticks"],
                          row["lease_generation"], row["state"]))

    def test_orphaned_process_group_fails_closed(self):
        self.proc.add(100, ticks=10, command=("claude",), cwd=self.worktree)
        supervisor.claim_lease(
            self.con, self.binding["binding_id"], 100, repo_root=self.repo,
            state="foreground", proc_root=self.proc.root)
        self.proc.remove(100)
        self.proc.add(101, ticks=11, pgrp=100, command=("python", "worker.py"),
                      cwd=self.worktree)
        self.proc.add(102, ticks=12, command=("claude",), cwd=self.worktree)
        self.assertEqual("orphan-group", supervisor.reconcile_binding(
            self.con, self.binding["binding_id"], repo_root=self.repo,
            proc_root=self.proc.root))
        with self.assertRaisesRegex(supervisor.LeaseConflict, "surviving"):
            supervisor.claim_lease(
                self.con, self.binding["binding_id"], 102, repo_root=self.repo,
                state="dispatching", proc_root=self.proc.root)
        row = self.row()
        self.assertEqual((100, 10, "error"),
                         (row["lease_pid"], row["lease_start_ticks"], row["state"]))
        self.assertEqual("recorded owner exited but process group survives: 101",
                         row["last_error"])
        self.assertTrue(supervisor.release_lease(
            self.con, self.binding["binding_id"], 100, 10,
            row["lease_generation"]))
        row = self.row()
        self.assertEqual((None, None, "error",
                          "recorded owner exited but process group survives: 101"),
                         (row["lease_pid"], row["lease_start_ticks"], row["state"],
                          row["last_error"]))

    def test_live_supervisor_authorizes_cleanup_after_leader_exit(self):
        self.proc.add(90, ticks=9, command=("python", "/engine/run.py"),
                      cwd=self.worktree)
        self.proc.add(100, ticks=10, command=("claude",), cwd=self.worktree)
        supervisor.register_native_session(
            self.con, self.binding["binding_id"], "native-a")
        generation = supervisor.claim_lease(
            self.con, self.binding["binding_id"], 100, repo_root=self.repo,
            state="foreground", supervisor_pid=90, proc_root=self.proc.root)
        self.proc.remove(100)
        self.proc.add(101, ticks=11, pgrp=100, command=("python", "worker.py"),
                      cwd=self.worktree)

        self.assertEqual("cleanup", supervisor.reconcile_binding(
            self.con, self.binding["binding_id"], repo_root=self.repo,
            proc_root=self.proc.root))
        row = self.row()
        self.assertEqual(("foreground", 100, 90),
                         (row["state"], row["lease_pid"], row["supervisor_pid"]))
        self.assertTrue(supervisor.release_lease(
            self.con, self.binding["binding_id"], 100, 10, generation))
        row = self.row()
        self.assertEqual(("dormant", None, None, None),
                         (row["state"], row["lease_pid"], row["supervisor_pid"],
                          row["last_error"]))

    def test_release_clears_lease_without_leaving_released_state(self):
        self.proc.add(100, ticks=10, command=("claude",), cwd=self.worktree)
        generation = supervisor.claim_lease(
            self.con, self.binding["binding_id"], 100, repo_root=self.repo,
            state="foreground", proc_root=self.proc.root)
        session_control.transition_binding(
            self.con, self.binding["binding_id"], expected="foreground",
            target="released")
        self.con.commit()

        self.assertTrue(supervisor.release_lease(
            self.con, self.binding["binding_id"], 100, 10, generation,
            error="ignored terminal error"))
        row = self.row()
        self.assertEqual(("released", None, None, None),
                         (row["state"], row["lease_pid"], row["lease_start_ticks"],
                          row["last_error"]))

    def test_active_channel_pid_reuse_is_cleared_not_heartbeated(self):
        self.proc.add(200, ticks=20, command=("python", "watcher.py"),
                      cwd=self.worktree)
        ticks = supervisor.register_active_channel(
            self.con, self.binding["binding_id"], 200, repo_root=self.repo,
            proc_root=self.proc.root)
        self.proc.remove(200)
        self.proc.add(200, ticks=21, command=("python", "other.py"),
                      cwd=self.worktree)
        self.assertFalse(supervisor.heartbeat_active_channel(
            self.con, self.binding["binding_id"], 200, ticks + 1))
        self.assertEqual("vacant", supervisor.reconcile_binding(
            self.con, self.binding["binding_id"], repo_root=self.repo,
            proc_root=self.proc.root))
        row = self.row()
        self.assertEqual((None, None, None),
                         (row["active_channel_pid"],
                          row["active_channel_start_ticks"],
                          row["active_channel_heartbeat_at"]))


class ConcurrentLeaseTest(unittest.TestCase):
    def test_only_one_concurrent_claim_wins(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = str(Path(tmp.name) / "leases.db")
        seed = make_db(path)
        binding = supervisor.ensure_binding(
            seed, archive_id=10, shell_id=1, harness="claude")
        seed.close()
        proc = FakeProc(self)
        repo = proc.root / "repo"
        worktree = repo / ".sc-worktrees" / "dev1"
        proc.add(301, ticks=31, command=("claude",), cwd=worktree)
        proc.add(302, ticks=32, command=("claude",), cwd=worktree)
        barrier = threading.Barrier(2)
        results: list[tuple[str, int]] = []

        def claim(pid: int) -> None:
            con = sqlite3.connect(path, timeout=5)
            con.row_factory = sqlite3.Row
            try:
                barrier.wait(timeout=5)
                generation = supervisor.claim_lease(
                    con, binding["binding_id"], pid, repo_root=repo,
                    state="foreground", proc_root=proc.root)
                results.append(("won", generation))
            except supervisor.LeaseConflict:
                results.append(("blocked", pid))
            finally:
                con.close()

        threads = [threading.Thread(target=claim, args=(pid,)) for pid in (301, 302)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(["blocked", "won"], sorted(kind for kind, _ in results))
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        self.addCleanup(con.close)
        row = con.execute("SELECT * FROM shell_session_bindings").fetchone()
        self.assertIn(row["lease_pid"], (301, 302))
        self.assertEqual(1, row["lease_generation"])
        self.assertEqual("foreground", row["state"])


class MigratedStateMachineIntegrationTest(unittest.TestCase):
    def test_lease_edges_use_migrated_compare_and_set_state_machine(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        self.addCleanup(con.close)
        con.executescript(SCHEMA.read_text())
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            con.executescript(migration.read_text())
        con.executescript(
            "INSERT INTO users (user_id, username, is_active) VALUES (1, 'T', 1);"
            "INSERT INTO shells (shell_id, display_name, shortname, flavor, "
            "system_prompt, user_id) VALUES (1, 'Planner', 'DEV1', 'planner', 'x', 1);"
            "INSERT INTO shell_memory_archives "
            "(archive_id, shell_id, session_id, date, harness, provider, model) "
            "VALUES (10, 1, '0007', '2026-07-21', 'claude', 'anthropic', 'opus');"
        )
        binding = supervisor.ensure_binding(
            con, archive_id=10, shell_id=1, harness="claude")
        proc = FakeProc(self)
        repo = proc.root / "repo"
        worktree = repo / ".sc-worktrees" / "dev1"
        proc.add(401, ticks=41, command=("claude",), cwd=worktree)

        generation = supervisor.claim_lease(
            con, binding["binding_id"], 401, repo_root=repo,
            state="foreground", proc_root=proc.root)
        row = con.execute(
            "SELECT state, lease_pid, lease_start_ticks, lease_generation "
            "FROM shell_session_bindings WHERE binding_id=?",
            (binding["binding_id"],),
        ).fetchone()
        self.assertEqual(("foreground", 401, 41, generation), tuple(row))

        self.assertTrue(supervisor.release_lease(
            con, binding["binding_id"], 401, 41, generation))
        row = con.execute(
            "SELECT state, lease_pid, lease_start_ticks, last_error "
            "FROM shell_session_bindings WHERE binding_id=?",
            (binding["binding_id"],),
        ).fetchone()
        self.assertEqual(
            ("error", None, None, "native session id unavailable when owner exited"),
            tuple(row),
        )

        proc.add(402, ticks=42, command=("claude",), cwd=worktree)
        with self.assertRaises(session_control.InvalidStateTransition):
            supervisor.claim_lease(
                con, binding["binding_id"], 402, repo_root=repo,
                state="foreground", proc_root=proc.root)
        row = con.execute(
            "SELECT state, lease_pid, lease_generation FROM shell_session_bindings "
            "WHERE binding_id=?", (binding["binding_id"],),
        ).fetchone()
        self.assertEqual(("error", None, generation), tuple(row))


class SupersedeForEnterTest(unittest.TestCase):
    def _managed_binding(self, con):
        binding = supervisor.ensure_binding(
            con, archive_id=10, shell_id=1, harness="claude",
            native_session_id="native-7")
        con.execute(
            "UPDATE shell_session_bindings SET state='dormant', managed=1 "
            "WHERE binding_id=?", (binding["binding_id"],))
        con.commit()
        return binding

    def _binding_row(self, con, binding_id):
        return con.execute(
            "SELECT state, managed, last_error, native_session_id "
            "FROM shell_session_bindings WHERE binding_id=?",
            (binding_id,)).fetchone()

    def test_bare_enter_supersedes_dormant_managed_binding(self):
        con = make_db()
        self.addCleanup(con.close)
        binding = self._managed_binding(con)
        con.execute(
            "INSERT INTO session_wake_jobs (binding_id, trigger_message_id, state) "
            "VALUES (?, 1, 'queued'), (?, 2, 'failed'), (?, 3, 'done')",
            (binding["binding_id"],) * 3)
        con.commit()

        superseded = supervisor.supersede_for_enter(con, 1, repo_root=Path("/none"))
        self.assertEqual(
            (binding["binding_id"], 10, "0007", "claude", "native-7", "dormant", 1),
            (superseded["binding_id"], superseded["archive_id"],
             superseded["session_id"], superseded["harness"],
             superseded["native_session_id"], superseded["state"],
             superseded["managed"]),
        )
        self.assertEqual(("released", 0, None, "native-7"),
                         tuple(self._binding_row(con, binding["binding_id"])))
        jobs = con.execute(
            "SELECT state, last_error FROM session_wake_jobs "
            "WHERE binding_id=? ORDER BY trigger_message_id",
            (binding["binding_id"],)).fetchall()
        self.assertEqual(
            [("cancelled", "superseded by interactive enter"),
             ("cancelled", "superseded by interactive enter"),
             ("done", None)],
            [tuple(job) for job in jobs])

        # The fresh session opens a NEW archive and rebinds managed=1 on it.
        session_id, archive_id = run.open_session(
            con, 1, lifecycle={"harness": "claude", "provider": "anthropic",
                               "model": "opus"}, force_new=True)
        self.assertEqual(("0008", 11), (session_id, archive_id))
        fresh = supervisor.ensure_binding(
            con, archive_id=archive_id, shell_id=1, harness="claude", managed=True)
        managed = con.execute(
            "SELECT binding_id, archive_id, state FROM shell_session_bindings "
            "WHERE managed=1").fetchall()
        self.assertEqual([(fresh["binding_id"], 11, "starting")],
                         [tuple(row) for row in managed])

    def test_error_binding_is_superseded_with_error_preserved(self):
        # #484 regression: a failed bootstrap must never wedge the next enter.
        con = make_db()
        self.addCleanup(con.close)
        binding = self._managed_binding(con)
        con.execute(
            "UPDATE shell_session_bindings SET state='error', "
            "last_error='Codex 0.145.0 is not validated' WHERE binding_id=?",
            (binding["binding_id"],))
        con.commit()

        superseded = supervisor.supersede_for_enter(con, 1, repo_root=Path("/none"))
        self.assertEqual(binding["binding_id"], superseded["binding_id"])
        self.assertEqual(
            ("released", 0, "Codex 0.145.0 is not validated", "native-7"),
            tuple(self._binding_row(con, binding["binding_id"])))

    def test_unmanaged_binding_on_active_archive_is_superseded(self):
        con = make_db()
        self.addCleanup(con.close)
        binding = supervisor.ensure_binding(
            con, archive_id=10, shell_id=1, harness="claude",
            native_session_id="native-7")
        con.execute(
            "UPDATE shell_session_bindings SET state='dormant' WHERE binding_id=?",
            (binding["binding_id"],))
        con.execute("UPDATE shells SET active_archive_id=10 WHERE shell_id=1")
        con.commit()

        superseded = supervisor.supersede_for_enter(con, 1, repo_root=Path("/none"))
        self.assertEqual(binding["binding_id"], superseded["binding_id"])
        self.assertEqual(("released", 0, None, "native-7"),
                         tuple(self._binding_row(con, binding["binding_id"])))

    def test_unmanaged_binding_off_active_archive_is_left_alone(self):
        con = make_db()
        self.addCleanup(con.close)
        supervisor.ensure_binding(
            con, archive_id=10, shell_id=1, harness="claude",
            native_session_id="native-7")
        con.commit()
        self.assertIsNone(
            supervisor.supersede_for_enter(con, 1, repo_root=Path("/none")))
        row = con.execute(
            "SELECT state, managed FROM shell_session_bindings").fetchone()
        self.assertEqual(("starting", 0), tuple(row))

    def test_no_binding_returns_none_and_writes_nothing(self):
        con = make_db()
        self.addCleanup(con.close)
        self.assertIsNone(
            supervisor.supersede_for_enter(con, 1, repo_root=Path("/none")))
        self.assertEqual(0, con.execute(
            "SELECT COUNT(*) FROM shell_session_bindings").fetchone()[0])

    def test_live_owner_refuses_supersede_without_touching_the_binding(self):
        con = make_db()
        self.addCleanup(con.close)
        binding = self._managed_binding(con)
        proc = FakeProc(self)
        repo = proc.root / "repo"
        worktree = repo / ".sc-worktrees" / "dev1"
        proc.add(501, ticks=51, command=("claude",), cwd=worktree)
        con.execute(
            "UPDATE shell_session_bindings SET lease_pid=501, "
            "lease_start_ticks=51, state='foreground' WHERE binding_id=?",
            (binding["binding_id"],))
        con.commit()

        with self.assertRaisesRegex(ValueError, "live owner"):
            supervisor.supersede_for_enter(
                con, 1, repo_root=repo, proc_root=proc.root)
        row = con.execute(
            "SELECT state, managed, lease_pid FROM shell_session_bindings "
            "WHERE binding_id=?", (binding["binding_id"],)).fetchone()
        self.assertEqual(("foreground", 1, 501), tuple(row))

    def test_resume_reuses_exact_archive_without_creating_or_rewriting_it(self):
        con = make_db()
        self.addCleanup(con.close)
        session_id, archive_id = run.open_session(
            con, 1, lifecycle={"model": "different"}, reuse_archive_id=10)
        self.assertEqual(("0007", 10), (session_id, archive_id))
        self.assertEqual(1, con.execute(
            "SELECT COUNT(*) FROM shell_memory_archives").fetchone()[0])
        row = con.execute(
            "SELECT model FROM shell_memory_archives WHERE archive_id=10").fetchone()
        self.assertEqual("opus", row["model"])
        self.assertEqual(10, con.execute(
            "SELECT active_archive_id FROM shells WHERE shell_id=1").fetchone()[0])

    def test_resume_rejects_another_shells_archive_without_changing_active(self):
        con = make_db()
        self.addCleanup(con.close)
        con.execute("INSERT INTO shells VALUES (2, 'DEV2', 'planner', NULL)")
        con.commit()
        with self.assertRaisesRegex(ValueError, "selected shell"):
            run.open_session(con, 2, reuse_archive_id=10)
        self.assertIsNone(con.execute(
            "SELECT active_archive_id FROM shells WHERE shell_id=2").fetchone()[0])


class EnterMainFlowTest(unittest.TestCase):
    class OpenReached(Exception):
        def __init__(self, call_args, call_kwargs):
            super().__init__("open_session reached")
            self.call_args = call_args
            self.call_kwargs = call_kwargs

    def _managed_binding(self, con):
        binding = supervisor.ensure_binding(
            con, archive_id=10, shell_id=1, harness="claude",
            native_session_id="native-7")
        con.execute(
            "UPDATE shell_session_bindings SET state='dormant', managed=1 "
            "WHERE binding_id=?", (binding["binding_id"],))
        con.commit()
        return binding

    def _run_until_open(self, con, argv, *, render_only=False):
        chosen = {"shell_id": 1, "shortname": "DEV1", "flavor": "planner"}
        defaults = {
            "planner": {"default_harness": "codex", "models": {
                "claude": "default-claude", "codex": "default-codex",
            }}
        }
        def capture(*args, **kwargs):
            raise self.OpenReached(args, kwargs)

        env = {"RENDER_ONLY": "1"} if render_only else {}
        with mock.patch.dict(run.os.environ, env, clear=True), \
                mock.patch.object(run.sys, "argv", ["run.py", *argv]), \
                mock.patch.object(run.sys, "stdin", mock.Mock(isatty=lambda: False)), \
                mock.patch.object(run, "open_db", return_value=con), \
                mock.patch.object(run, "authenticate", return_value={"user_id": 1}), \
                mock.patch.object(run, "flavor_defaults", return_value=defaults), \
                mock.patch.object(run, "list_shells", return_value=[chosen]), \
                mock.patch.object(run, "ensure_harness_path"), \
                mock.patch.object(run.seed_skills, "sync_engine_skills",
                                  return_value=[]), \
                mock.patch("analytics.sweep",
                          return_value={"inserted": 0, "updated": 0}), \
                mock.patch.object(run, "load_adapter", return_value={
                    "launch": ["claude"], "emit": [], "env": {},
                    "session_control": {"capabilities": {}},
                }), \
                mock.patch.object(run, "open_session", side_effect=capture):
            run.main()

    def test_bare_enter_supersedes_and_opens_fresh_archive(self):
        con = make_db()
        self.addCleanup(con.close)
        binding = self._managed_binding(con)

        with self.assertRaises(self.OpenReached) as reached:
            self._run_until_open(con, ["DEV1"])
        self.assertEqual((con, 1), reached.exception.call_args[:2])
        self.assertIsNone(reached.exception.call_kwargs["reuse_archive_id"])
        self.assertTrue(reached.exception.call_kwargs["force_new"])
        row = con.execute(
            "SELECT state, managed FROM shell_session_bindings WHERE binding_id=?",
            (binding["binding_id"],)).fetchone()
        self.assertEqual(("released", 0), tuple(row))

    def test_new_session_flag_supersedes_instead_of_refusing(self):
        con = make_db()
        self.addCleanup(con.close)
        binding = self._managed_binding(con)

        with self.assertRaises(self.OpenReached) as reached:
            self._run_until_open(con, ["DEV1", "--new-session"])
        self.assertTrue(reached.exception.call_kwargs["force_new"])
        row = con.execute(
            "SELECT state, managed FROM shell_session_bindings WHERE binding_id=?",
            (binding["binding_id"],)).fetchone()
        self.assertEqual(("released", 0), tuple(row))

    def test_error_binding_is_superseded_not_refused(self):
        # #484 regression at the main() level: the next enter starts fresh.
        con = make_db()
        self.addCleanup(con.close)
        binding = self._managed_binding(con)
        con.execute(
            "UPDATE shell_session_bindings SET state='error', "
            "last_error='transport failed' WHERE binding_id=?",
            (binding["binding_id"],),
        )
        con.commit()

        with self.assertRaises(self.OpenReached) as reached:
            self._run_until_open(con, ["DEV1"])
        self.assertIsNone(reached.exception.call_kwargs["reuse_archive_id"])
        self.assertTrue(reached.exception.call_kwargs["force_new"])
        row = con.execute(
            "SELECT state, managed, lease_pid, last_error "
            "FROM shell_session_bindings WHERE binding_id=?",
            (binding["binding_id"],),
        ).fetchone()
        self.assertEqual(("released", 0, None, "transport failed"), tuple(row))

    def test_render_only_never_supersedes(self):
        # Headless verify must not mutate: the binding survives untouched.
        con = make_db()
        self.addCleanup(con.close)
        binding = self._managed_binding(con)

        with self.assertRaises(self.OpenReached) as reached:
            self._run_until_open(con, ["DEV1"], render_only=True)
        self.assertIsNone(reached.exception.call_kwargs["reuse_archive_id"])
        self.assertFalse(reached.exception.call_kwargs["force_new"])
        row = con.execute(
            "SELECT state, managed FROM shell_session_bindings WHERE binding_id=?",
            (binding["binding_id"],)).fetchone()
        self.assertEqual(("dormant", 1), tuple(row))

    def test_no_binding_enter_opens_without_supersede(self):
        con = make_db()
        self.addCleanup(con.close)

        with self.assertRaises(self.OpenReached) as reached:
            self._run_until_open(con, ["DEV1", "--harness", "claude"])
        self.assertIsNone(reached.exception.call_kwargs["reuse_archive_id"])
        self.assertFalse(reached.exception.call_kwargs["force_new"])


class SuperviseTest(unittest.TestCase):
    def test_preflight_refuses_before_popen(self):
        spawned = False

        def refuse() -> None:
            raise supervisor.LeaseConflict("live owner")

        def popen(*args, **kwargs):
            nonlocal spawned
            spawned = True
            raise AssertionError("Popen must not run after a failed preflight")

        with self.assertRaisesRegex(supervisor.LeaseConflict, "live owner"):
            supervisor.supervise(
                ["claude"], cwd=Path.cwd(), env=dict(os.environ),
                on_pre_spawn=refuse, popen=popen)
        self.assertFalse(spawned)

    def test_forwarded_signal_targets_process_group_and_controls_exit_status(self):
        calls: list[tuple[int, int]] = []
        exited: list[tuple[int, int]] = []

        class Child:
            pid = 4321
            returncode = None

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                self.returncode = 0
                return 0

        def started(_pid: int) -> None:
            os.kill(os.getpid(), signal.SIGTERM)

        rc = supervisor.supervise(
            ["claude"], cwd=Path.cwd(), env=dict(os.environ),
            on_started=started, on_exited=lambda pid, code: exited.append((pid, code)),
            popen=lambda *args, **kwargs: Child(),
            killpg=lambda pid, sig: calls.append((pid, sig)), group_grace=0)
        self.assertEqual(128 + signal.SIGTERM, rc)
        self.assertEqual((4321, signal.SIGTERM), calls[0])
        self.assertIn((4321, signal.SIGKILL), calls)
        self.assertEqual([(4321, 0)], exited)

    def test_signal_arriving_inside_spawn_window_is_relayed_after_pid_capture(self):
        calls: list[tuple[int, int]] = []

        class Child:
            pid = 5432
            returncode = None

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                self.returncode = -signal.SIGTERM
                return self.returncode

        def spawning(*args, **kwargs):
            os.kill(os.getpid(), signal.SIGTERM)
            return Child()

        rc = supervisor.supervise(
            ["claude"], cwd=Path.cwd(), env=dict(os.environ), popen=spawning,
            killpg=lambda pid, sig: calls.append((pid, sig)), group_grace=0)
        self.assertEqual(128 + signal.SIGTERM, rc)
        self.assertEqual((5432, signal.SIGTERM), calls[0])
        self.assertNotIn((os.getpid(), signal.SIGTERM), calls)

    def test_cancelling_supervisor_terminates_real_descendant_group(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        pidfile = Path(tmp.name) / "pids"
        child_code = (
            "import os,subprocess,sys,time; "
            "g=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            f"open({str(pidfile)!r},'w').write(f'{{os.getpid()}} {{g.pid}}'); "
            "time.sleep(60)"
        )
        supervisor_code = (
            f"import os,sys; sys.path.insert(0,{str(ENGINE / 'scripts')!r}); "
            "import session_supervisor as s; "
            f"raise SystemExit(s.supervise([{sys.executable!r},'-c',{child_code!r}], "
            f"cwd=s.Path({tmp.name!r}), env=dict(os.environ)))"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", supervisor_code], start_new_session=True)

        def cleanup() -> None:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            if pidfile.exists():
                child_pid = int(pidfile.read_text().split()[0])
                try:
                    os.killpg(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        self.addCleanup(cleanup)
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(pidfile.exists(), "harness descendant never started")
        child_pid, grandchild_pid = map(int, pidfile.read_text().split())

        proc.terminate()
        self.assertEqual(128 + signal.SIGTERM, proc.wait(timeout=5))

        def still_running(pid: int) -> bool:
            try:
                stat = Path(f"/proc/{pid}/stat").read_text()
            except OSError:
                return False
            end = stat.rfind(")")
            state = stat[end + 2:].split()[0] if end >= 0 else "?"
            return state != "Z"

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (
                still_running(child_pid) or still_running(grandchild_pid)):
            time.sleep(0.02)
        self.assertFalse(still_running(child_pid))
        self.assertFalse(still_running(grandchild_pid),
                         "signal reached only the harness leader, recreating #439")


if __name__ == "__main__":
    unittest.main()
