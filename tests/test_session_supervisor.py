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
  harness TEXT,
  provider TEXT,
  model TEXT
);
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
  active_channel_pid INTEGER,
  active_channel_start_ticks INTEGER,
  active_channel_heartbeat_at TEXT,
  lease_generation INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (harness, native_session_id)
);
"""


def make_db(path: str = ":memory:") -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=5)
    con.row_factory = sqlite3.Row
    con.executescript(BINDING_SCHEMA)
    con.execute("INSERT INTO shells VALUES (1, 'DEV1', 'planner', NULL)")
    con.execute(
        "INSERT INTO shell_memory_archives VALUES "
        "(10, 1, '0007', 'claude', 'anthropic', 'opus')"
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


class ArchiveReuseTest(unittest.TestCase):
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


class SuperviseTest(unittest.TestCase):
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
