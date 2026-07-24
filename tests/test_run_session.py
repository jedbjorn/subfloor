#!/usr/bin/env python3
"""Regression tests for atomic, contention-safe launcher session opening."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import db_driver  # noqa: E402
import run  # noqa: E402


SCHEMA = """
CREATE TABLE shells (
    shell_id INTEGER PRIMARY KEY,
    active_archive_id INTEGER
);
CREATE TABLE shell_memory_archives (
    archive_id INTEGER PRIMARY KEY AUTOINCREMENT,
    shell_id INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    date TEXT NOT NULL,
    full_narrative TEXT NOT NULL,
    started_at TEXT,
    harness TEXT,
    provider TEXT,
    model TEXT,
    sprint_ref TEXT,
    UNIQUE (shell_id, session_id)
);
CREATE TABLE session_token_usage (
    archive_id INTEGER
);
CREATE TABLE lock_probe (
    probe_id INTEGER PRIMARY KEY,
    value INTEGER NOT NULL
);
INSERT INTO shells (shell_id, active_archive_id) VALUES (1, NULL);
INSERT INTO lock_probe (probe_id, value) VALUES (1, 0);
"""


class _FailAfterArchive:
    """Connection proxy that injects a lock after the archive INSERT."""

    def __init__(self, con: sqlite3.Connection) -> None:
        self._con = con

    def execute(self, sql: str, parameters=()):
        if sql.startswith("UPDATE shells SET active_archive_id"):
            raise sqlite3.OperationalError("database is locked")
        return self._con.execute(sql, parameters)

    def __getattr__(self, name: str):
        return getattr(self._con, name)


class _RollbackSignal:
    """Connection proxy that exposes the first failed attempt to the test."""

    def __init__(self, con: sqlite3.Connection, rolled_back: threading.Event) -> None:
        self._con = con
        self._rolled_back = rolled_back
        self.rollback_count = 0

    def rollback(self) -> None:
        self._con.rollback()
        self.rollback_count += 1
        self._rolled_back.set()

    def __getattr__(self, name: str):
        return getattr(self._con, name)


class OpenSessionContentionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "shell.db"
        con = db_driver.connect(self.path)
        con.executescript(SCHEMA)
        con.commit()
        con.close()

    def _connect(self, busy_timeout_ms: int = 5000):
        con = db_driver.connect(self.path)
        con.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        self.addCleanup(con.close)
        return con

    def test_retries_from_clean_boundary_after_concurrent_writer(self) -> None:
        holder = self._connect()
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("UPDATE lock_probe SET value=1 WHERE probe_id=1")
        outcome: list[tuple[str, int]] = []
        errors: list[BaseException] = []
        first_attempt_rolled_back = threading.Event()
        contender_proxy: list[_RollbackSignal] = []

        def open_contender() -> None:
            contender = db_driver.connect(self.path)
            contender.execute("PRAGMA busy_timeout=30")
            proxy = _RollbackSignal(contender, first_attempt_rolled_back)
            contender_proxy.append(proxy)
            try:
                with mock.patch.object(
                        run, "SESSION_OPEN_RETRY_DELAYS_S", (0.1,)):
                    outcome.append(run.open_session(
                        proxy, 1, lifecycle={"harness": "claude"}))
            except BaseException as exc:
                errors.append(exc)
            finally:
                contender.close()

        contender_thread = threading.Thread(target=open_contender)
        contender_thread.start()
        self.assertTrue(
            first_attempt_rolled_back.wait(2),
            "the first bounded SQLite wait should expire while the writer holds",
        )
        holder.commit()
        contender_thread.join(2)

        self.assertFalse(contender_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(contender_proxy[0].rollback_count, 1)
        self.assertEqual(outcome, [("0001", 1)])
        verifier = self._connect()
        archive = verifier.execute(
            "SELECT shell_id, session_id, harness FROM shell_memory_archives"
        ).fetchone()
        self.assertEqual(tuple(archive), (1, "0001", "claude"))
        self.assertEqual(
            verifier.execute(
                "SELECT active_archive_id FROM shells WHERE shell_id=1"
            ).fetchone()[0],
            1,
        )

    def test_terminal_busy_failure_rolls_back_partial_archive(self) -> None:
        con = self._connect(busy_timeout_ms=1)
        failing = _FailAfterArchive(con)

        with mock.patch.object(run, "SESSION_OPEN_RETRY_DELAYS_S", (0,)), \
                self.assertRaises(run.SessionOpenError) as raised:
            run.open_session(failing, 1)

        message = str(raised.exception)
        self.assertIn("2 bounded session-open attempt(s)", message)
        self.assertIn("no session or archive was created", message)
        self.assertIn("Retry after the concurrent engine write finishes", message)
        self.assertEqual(
            con.execute("SELECT COUNT(*) FROM shell_memory_archives").fetchone()[0],
            0,
        )
        self.assertIsNone(
            con.execute(
                "SELECT active_archive_id FROM shells WHERE shell_id=1"
            ).fetchone()[0]
        )
        self.assertFalse(con.in_transaction)


class HeadlessSessionFailureTest(unittest.TestCase):
    def test_sc_run_exits_before_worktree_or_harness_artifacts(self) -> None:
        con = mock.Mock()
        chosen = {"shell_id": 1, "shortname": "DEV1", "flavor": "dev"}
        fdefaults = {
            "dev": {"default_harness": "claude", "models": {"claude": "sonnet"}}
        }
        analytics = mock.Mock()
        analytics.sweep.return_value = {"inserted": 0, "updated": 0}
        ensure_worktree = mock.Mock()
        atomic_write = mock.Mock()
        execvpe = mock.Mock()

        @contextmanager
        def spinner(_label: str, *, enabled: bool):
            self.assertFalse(enabled)
            yield SimpleNamespace(label="")

        with mock.patch.dict(run.os.environ, {}, clear=True), \
                mock.patch.dict(sys.modules, {"analytics": analytics}), \
                mock.patch.object(
                    run.sys, "argv",
                    ["run.py", "--headless", "DEV1", "--harness", "claude"]), \
                mock.patch.object(run, "open_db", return_value=con), \
                mock.patch.object(
                    run.seed_skills, "sync_engine_skills", return_value=[]), \
                mock.patch.object(
                    run, "authenticate", return_value={"user_id": 1}), \
                mock.patch.object(run, "flavor_defaults", return_value=fdefaults), \
                mock.patch.object(run, "list_shells", return_value=[chosen]), \
                mock.patch.object(run, "pick_shell", return_value=chosen), \
                mock.patch.object(
                    run.shell_liveness, "compute",
                    return_value={"supported": False, "indeterminate": 0}), \
                mock.patch.object(run, "ensure_harness_path"), \
                mock.patch.object(run, "load_adapter", return_value={}), \
                mock.patch.object(run, "validate_headless_request"), \
                mock.patch.object(run.style, "spinner", side_effect=spinner), \
                mock.patch.object(
                    run, "open_session",
                    side_effect=run.SessionOpenError(
                        "engine DB remained busy; no session or archive was created")), \
                mock.patch.object(run, "ensure_worktree", ensure_worktree), \
                mock.patch.object(run, "atomic_write", atomic_write), \
                mock.patch.object(run.os, "execvpe", execvpe):
            with self.assertRaises(SystemExit) as raised:
                run.main()

        self.assertIn("sc run: engine DB remained busy", str(raised.exception))
        self.assertIn("no session or archive was created", str(raised.exception))
        con.close.assert_called_once_with()
        ensure_worktree.assert_not_called()
        atomic_write.assert_not_called()
        execvpe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
