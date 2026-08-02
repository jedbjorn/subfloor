#!/usr/bin/env python3
"""Shared SQLite write-transaction policy contracts."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))

import db_driver  # noqa: E402


class WriteTransactionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "engine.db"
        con = db_driver.connect(self.path)
        con.execute("CREATE TABLE writes (value TEXT NOT NULL)")
        con.commit()
        con.close()

    def test_commits_and_reports_transaction_timing(self) -> None:
        timings: list[db_driver.WriteTransactionTiming] = []
        con = db_driver.connect(self.path)
        try:
            with db_driver.write_transaction(
                con,
                "test.commit",
                observer=timings.append,
            ):
                con.execute("INSERT INTO writes VALUES ('committed')")
        finally:
            con.close()

        check = db_driver.connect(self.path)
        try:
            self.assertEqual(
                check.execute("SELECT value FROM writes").fetchone()[0],
                "committed",
            )
        finally:
            check.close()
        self.assertEqual(len(timings), 1)
        self.assertTrue(timings[0].acquired)
        self.assertTrue(timings[0].committed)
        self.assertEqual(timings[0].operation, "test.commit")

    def test_connect_retries_busy_wal_configuration_and_restores_timeout(
        self,
    ) -> None:
        con = mock.Mock()
        attempts = 0

        def execute(statement: str):
            nonlocal attempts
            if statement == "PRAGMA journal_mode=WAL":
                attempts += 1
                if attempts < 3:
                    raise sqlite3.OperationalError("database is locked")
            return mock.Mock()

        con.execute.side_effect = execute
        sleep = mock.Mock()
        enable_wal = db_driver._enable_wal
        with (
            mock.patch.object(db_driver.sqlite3, "connect", return_value=con),
            mock.patch.object(
                db_driver,
                "_enable_wal",
                side_effect=lambda connection: enable_wal(
                    connection,
                    sleep=sleep,
                ),
            ),
        ):
            connected = db_driver.connect(self.path)

        self.assertIs(connected, con)
        self.assertEqual(attempts, 3)
        self.assertEqual(sleep.call_count, 2)
        con.execute.assert_any_call(
            f"PRAGMA busy_timeout={db_driver.DEFAULT_BEGIN_ATTEMPT_TIMEOUT_MS}"
        )
        con.execute.assert_called_with(
            f"PRAGMA busy_timeout={db_driver.DEFAULT_BUSY_TIMEOUT_MS}"
        )
        con.close.assert_not_called()

    def test_connect_closes_connection_after_non_busy_wal_failure(self) -> None:
        con = mock.Mock()

        def execute(statement: str):
            if statement == "PRAGMA journal_mode=WAL":
                raise sqlite3.OperationalError("disk I/O error")
            return mock.Mock()

        con.execute.side_effect = execute
        with (
            mock.patch.object(db_driver.sqlite3, "connect", return_value=con),
            self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O"),
        ):
            db_driver.connect(self.path)

        con.close.assert_called_once_with()

    def test_rolls_back_the_whole_body_on_failure(self) -> None:
        timings: list[db_driver.WriteTransactionTiming] = []
        con = db_driver.connect(self.path)
        try:
            with self.assertRaisesRegex(RuntimeError, "stop"):
                with db_driver.write_transaction(
                    con,
                    "test.rollback",
                    observer=timings.append,
                ):
                    con.execute("INSERT INTO writes VALUES ('rolled back')")
                    raise RuntimeError("stop")
        finally:
            con.close()

        check = db_driver.connect(self.path)
        try:
            self.assertEqual(
                check.execute("SELECT COUNT(*) FROM writes").fetchone()[0],
                0,
            )
        finally:
            check.close()
        self.assertTrue(timings[0].acquired)
        self.assertFalse(timings[0].committed)

    def test_retries_only_write_lock_acquisition_until_owner_releases(self) -> None:
        ready = threading.Event()
        release = threading.Event()

        def hold_writer() -> None:
            holder = db_driver.connect(self.path)
            try:
                holder.execute("BEGIN IMMEDIATE")
                holder.execute("INSERT INTO writes VALUES ('holder')")
                ready.set()
                release.wait(2)
                holder.commit()
            finally:
                holder.close()

        thread = threading.Thread(target=hold_writer)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.assertTrue(ready.wait(1))

        timings: list[db_driver.WriteTransactionTiming] = []
        releaser = threading.Timer(0.12, release.set)
        releaser.start()
        self.addCleanup(releaser.cancel)
        con = db_driver.connect(self.path)
        try:
            with db_driver.write_transaction(
                con,
                "test.contended",
                max_wait_seconds=1,
                attempt_timeout_ms=20,
                retry_base_seconds=0.005,
                retry_max_seconds=0.02,
                random_fraction=lambda: 0.5,
                observer=timings.append,
            ):
                con.execute("INSERT INTO writes VALUES ('contender')")
            self.assertEqual(
                con.execute("PRAGMA busy_timeout").fetchone()[0],
                db_driver.DEFAULT_BUSY_TIMEOUT_MS,
            )
        finally:
            con.close()
        thread.join(2)

        self.assertGreater(timings[0].attempts, 1)
        check = db_driver.connect(self.path)
        try:
            self.assertEqual(
                [
                    row[0]
                    for row in check.execute(
                        "SELECT value FROM writes ORDER BY rowid"
                    ).fetchall()
                ],
                ["holder", "contender"],
            )
        finally:
            check.close()

    def test_busy_deadline_never_enters_or_replays_the_body(self) -> None:
        holder = db_driver.connect(self.path)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO writes VALUES ('holder')")
        body_calls = 0
        timings: list[db_driver.WriteTransactionTiming] = []
        contender = db_driver.connect(self.path)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                with db_driver.write_transaction(
                    contender,
                    "test.timeout",
                    max_wait_seconds=0.05,
                    attempt_timeout_ms=10,
                    retry_base_seconds=0.005,
                    retry_max_seconds=0.01,
                    random_fraction=lambda: 0.5,
                    observer=timings.append,
                ):
                    body_calls += 1
            self.assertEqual(
                contender.execute("PRAGMA busy_timeout").fetchone()[0],
                db_driver.DEFAULT_BUSY_TIMEOUT_MS,
            )
        finally:
            contender.close()
            holder.rollback()
            holder.close()

        self.assertEqual(body_calls, 0)
        self.assertFalse(timings[0].acquired)
        self.assertFalse(timings[0].committed)
        self.assertGreater(timings[0].attempts, 1)

    def test_concurrent_shell_writers_all_commit_without_body_replay(self) -> None:
        workers = 6
        writes_per_worker = 12
        barrier = threading.Barrier(workers)
        errors: list[BaseException] = []
        body_calls: dict[int, int] = {}
        lock = threading.Lock()

        def write_for_shell(shell_id: int) -> None:
            con = db_driver.connect(self.path)
            try:
                barrier.wait(2)
                for sequence in range(writes_per_worker):
                    with db_driver.write_transaction(
                        con,
                        "test.multi_shell",
                        max_wait_seconds=2,
                        attempt_timeout_ms=10,
                        retry_base_seconds=0.001,
                        retry_max_seconds=0.01,
                    ):
                        with lock:
                            body_calls[shell_id] = body_calls.get(shell_id, 0) + 1
                        con.execute(
                            "INSERT INTO writes VALUES (?)",
                            (f"{shell_id}:{sequence}",),
                        )
            except BaseException as exc:
                errors.append(exc)
            finally:
                con.close()

        threads = [
            threading.Thread(target=write_for_shell, args=(shell_id,))
            for shell_id in range(workers)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(
            body_calls,
            {shell_id: writes_per_worker for shell_id in range(workers)},
        )
        con = db_driver.connect(self.path)
        try:
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM writes").fetchone()[0],
                workers * writes_per_worker,
            )
        finally:
            con.close()

    def test_refuses_nested_transactions(self) -> None:
        con = db_driver.connect(self.path)
        try:
            con.execute("BEGIN")
            with self.assertRaisesRegex(RuntimeError, "cannot nest"):
                with db_driver.write_transaction(con, "test.nested"):
                    self.fail("nested transaction body must not run")
        finally:
            con.rollback()
            con.close()


if __name__ == "__main__":
    unittest.main()
