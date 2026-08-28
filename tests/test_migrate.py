#!/usr/bin/env python3
"""Atomicity tests for the migration runner (scripts/migrate.py).

Stdlib `unittest`, no pytest — matching the engine's no-dependency style. These
drive `migrate.migrate()` against a throwaway DB with synthetic migrations in a
temp dir (MIGRATIONS_DIR monkeypatched), to prove a mid-file failure rolls back
whole and never wedges the chain. The REAL 36-file chain is exercised end to end
by `./sc render-check` (hermetic rebuild), so it isn't re-run here.

Run:
    python3 tests/test_migrate.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))

import db_driver  # noqa: E402
import migrate  # noqa: E402


class AtomicMigrateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sc_mig_"))
        self.db = str(self.tmp / "t.db")
        con = db_driver.connect(self.db)
        con.execute("CREATE TABLE t (a)")
        con.commit()
        con.close()
        self.migdir = self.tmp / "migrations"
        self.migdir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name: str, sql: str) -> None:
        (self.migdir / name).write_text(sql)

    def _run(
        self,
        *,
        backup: bool = False,
        fresh_build: bool = False,
        source_checkout: bool = False,
    ):
        with mock.patch.object(
            migrate, "MIGRATIONS_DIR", self.migdir
        ), mock.patch.object(
            migrate.update_cutover,
            "source_repo_checkout",
            return_value=source_checkout,
        ):
            return migrate.migrate(
                self.db, backup=backup, fresh_build=fresh_build
            )

    def _stamped(self) -> set[str]:
        con = db_driver.connect(self.db)
        try:
            return {r[0] for r in con.execute("SELECT filename FROM schema_migrations")}
        finally:
            con.close()

    def _cols(self) -> list[str]:
        con = db_driver.connect(self.db)
        try:
            return [r[1] for r in con.execute("PRAGMA table_info(t)")]
        finally:
            con.close()

    def test_bare_multistmt_failure_rolls_back_and_is_not_wedged(self):
        # A bare (no BEGIN) migration whose 2nd statement fails.
        self._write("0001_bad.sql",
                    "ALTER TABLE t ADD COLUMN b;\nALTER TABLE t ADD COLUMN b;\n")
        with self.assertRaises(Exception):
            self._run()
        # Rolled back whole: no partial column, no ledger stamp -> re-runnable.
        self.assertNotIn("b", self._cols())
        self.assertNotIn("0001_bad.sql", self._stamped())
        # Fix the file; the chain applies cleanly (it was never wedged).
        self._write("0001_bad.sql", "ALTER TABLE t ADD COLUMN b;\n")
        self._run()
        self.assertIn("b", self._cols())
        self.assertIn("0001_bad.sql", self._stamped())

    def test_ledger_stamp_is_atomic_with_body(self):
        self._write("0001_ok.sql", "ALTER TABLE t ADD COLUMN b;\n")
        self._run()
        self.assertIn("0001_ok.sql", self._stamped())
        # Re-run is a no-op (already stamped), not a duplicate-column crash.
        self._run()
        self.assertEqual(self._cols().count("b"), 1)

    def test_file_with_its_own_begin_commit_is_stripped_and_applies(self):
        self._write("0001_wrapped.sql",
                    "BEGIN;\nALTER TABLE t ADD COLUMN b;\nCOMMIT;\n")
        self._run()
        self.assertIn("b", self._cols())
        self.assertIn("0001_wrapped.sql", self._stamped())

    def test_trigger_body_begin_end_survives_the_strip(self):
        # A CREATE TRIGGER's BEGIN/END must not be mistaken for txn control.
        self._write("0001_trig.sql",
                    "BEGIN;\n"
                    "CREATE TABLE log (m TEXT);\n"
                    "CREATE TRIGGER trg AFTER INSERT ON t\n"
                    "BEGIN\n"
                    "  INSERT INTO log (m) VALUES ('hit');\n"
                    "END;\n"
                    "COMMIT;\n")
        self._run()
        self.assertIn("0001_trig.sql", self._stamped())
        con = db_driver.connect(self.db)
        try:
            con.execute("INSERT INTO t (a) VALUES (1)")
            con.commit()
            n = con.execute("SELECT COUNT(*) FROM log").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(n, 1)  # trigger fired -> it was created intact

    def test_foreign_keys_off_marker_allows_parent_swap_and_restores_enforcement(
        self,
    ):
        con = db_driver.connect(self.db)
        try:
            con.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
            con.execute(
                "CREATE TABLE child ("
                "parent_id INTEGER NOT NULL REFERENCES parent(id))"
            )
            con.execute("INSERT INTO parent (id) VALUES (1)")
            con.execute("INSERT INTO child (parent_id) VALUES (1)")
            con.commit()
        finally:
            con.close()

        self._write(
            "0001_swap.sql",
            "-- migrate: foreign-keys-off\n"
            "BEGIN;\n"
            "CREATE TABLE parent_new (id INTEGER PRIMARY KEY);\n"
            "INSERT INTO parent_new SELECT * FROM parent;\n"
            "DROP TABLE parent;\n"
            "ALTER TABLE parent_new RENAME TO parent;\n"
            "COMMIT;\n",
        )
        self._run()

        con = db_driver.connect(self.db)
        try:
            self.assertEqual(con.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            con.close()
        self.assertIn("0001_swap.sql", self._stamped())

    def test_bare_migrate_takes_wal_safe_premigrate_backup_and_prunes_its_class(self):
        self._write("0001_add_b.sql", "ALTER TABLE t ADD COLUMN b;\n")
        backup_dir = self.tmp / "backups"
        backup_dir.mkdir()
        old_backups = []
        for index in range(6):
            old = backup_dir / f"shell_db.premigrate.20000101_00000{index}.db"
            old.write_bytes(b"old")
            old_backups.append(old)
        preupdate = backup_dir / "shell_db.preupdate.20000101_000000.db"
        preupdate.write_bytes(b"separate lifecycle")

        writer = sqlite3.connect(self.db)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("INSERT INTO t (a) VALUES (41)")
            writer.commit()
            with mock.patch.dict(
                os.environ,
                {"SC_DB_BACKUP_DIR": str(backup_dir)},
            ):
                self._run(backup=True)
        finally:
            writer.close()

        backups = sorted(backup_dir.glob("shell_db.premigrate.*.db"))
        self.assertEqual(len(backups), migrate.db_backup.KEEP_BACKUPS)
        self.assertFalse(old_backups[0].exists())
        self.assertFalse(old_backups[1].exists())
        created = [path for path in backups if path not in old_backups]
        self.assertEqual(len(created), 1)
        self.assertTrue(preupdate.exists())

        with closing(sqlite3.connect(created[0])) as restored:
            self.assertEqual(restored.execute("SELECT a FROM t").fetchall(), [(41,)])
            self.assertEqual(
                [row[1] for row in restored.execute("PRAGMA table_info(t)")],
                ["a"],
            )
        self.assertIn("b", self._cols())

    def test_backup_failure_prevents_migration_application(self):
        self._write("0001_add_b.sql", "ALTER TABLE t ADD COLUMN b;\n")
        with mock.patch.object(
            migrate.db_backup,
            "select_backup_dir",
            side_effect=migrate.db_backup.BackupDestinationError("no destination"),
        ), self.assertRaisesRegex(
            migrate.db_backup.BackupDestinationError,
            "no destination",
        ):
            self._run(backup=True)

        self.assertNotIn("b", self._cols())
        with closing(sqlite3.connect(self.db)) as con:
            ledger = con.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='schema_migrations'"
            ).fetchall()
        self.assertEqual(ledger, [])

    def test_purge_floor_barrier_defers_itself_and_later_migrations(self):
        self._write(
            "0001_purge.sql",
            "-- migrate: requires-dsh-purge-floor\n"
            "ALTER TABLE t ADD COLUMN purged;\n",
        )
        self._write("0002_later.sql", "ALTER TABLE t ADD COLUMN later;\n")

        with mock.patch.object(
            migrate.update_cutover, "purge_floor_declared", return_value=False
        ), mock.patch.object(
            migrate.update_cutover, "require_purge_floor"
        ) as require:
            self._run()

        self.assertEqual(["a"], self._cols())
        self.assertEqual(set(), self._stamped())
        require.assert_not_called()

    def test_declared_purge_floor_refuses_before_body_when_receipt_is_invalid(self):
        self._write(
            "0001_purge.sql",
            "-- migrate: requires-dsh-purge-floor\n"
            "ALTER TABLE t ADD COLUMN purged;\n",
        )
        with mock.patch.object(
            migrate.update_cutover, "purge_floor_declared", return_value=True
        ), mock.patch.object(
            migrate.update_cutover,
            "require_purge_floor",
            side_effect=migrate.update_cutover.CutoverError("receipt mismatch"),
        ), self.assertRaisesRegex(
            migrate.update_cutover.CutoverError, "receipt mismatch"
        ):
            self._run()

        self.assertEqual(["a"], self._cols())
        self.assertEqual(set(), self._stamped())

    def test_tracked_source_checkout_defers_without_installed_receipts(self):
        self._write(
            "0001_purge.sql",
            "-- migrate: requires-dsh-purge-floor\n"
            "ALTER TABLE t ADD COLUMN purged;\n",
        )
        with mock.patch.object(
            migrate.update_cutover, "purge_floor_declared"
        ) as declared, mock.patch.object(
            migrate.update_cutover, "require_purge_floor"
        ) as require:
            self._run(source_checkout=True)

        self.assertEqual(["a"], self._cols())
        self.assertEqual(set(), self._stamped())
        declared.assert_not_called()
        require.assert_not_called()

    def test_fresh_build_explicitly_authorizes_purge_replay(self):
        self._write(
            "0001_purge.sql",
            "-- migrate: requires-dsh-purge-floor\n"
            "ALTER TABLE t ADD COLUMN purged;\n",
        )
        with mock.patch.object(
            migrate.update_cutover, "purge_floor_declared"
        ) as declared, mock.patch.object(
            migrate.update_cutover, "require_purge_floor"
        ) as require:
            self._run(fresh_build=True)

        self.assertEqual(["a", "purged"], self._cols())
        self.assertEqual({"0001_purge.sql"}, self._stamped())
        declared.assert_not_called()
        require.assert_not_called()

    def test_fresh_build_stamp_only_never_executes_destructive_body(self):
        self._write(
            "0001_historical_purge.sql",
            "-- migrate: fresh-build-stamp-only\n"
            "DELETE FROM t;\n"
            "INSERT INTO missing_table VALUES (1);\n",
        )
        con = db_driver.connect(self.db)
        try:
            con.execute("INSERT INTO t (a) VALUES (41)")
            con.commit()
        finally:
            con.close()

        self._run(fresh_build=True)

        with closing(db_driver.connect(self.db)) as con:
            self.assertEqual(
                [(41,)],
                [tuple(row) for row in con.execute("SELECT a FROM t")],
            )
        self.assertEqual({"0001_historical_purge.sql"}, self._stamped())

    def test_rebaseline_waits_for_immediately_prior_floor(self):
        self._write(
            "0238_rebaseline.sql",
            "-- migrate: requires-rebaseline-floor\n"
            "ALTER TABLE t ADD COLUMN final_shape;\n",
        )
        self._write(
            "0239_later.sql",
            "ALTER TABLE t ADD COLUMN later;\n",
        )

        self._run()

        self.assertEqual(["a"], self._cols())
        self.assertEqual(set(), self._stamped())

    def test_rebaseline_applies_after_prior_floor_stamp(self):
        self._write(
            "0238_rebaseline.sql",
            "-- migrate: requires-rebaseline-floor\n"
            "ALTER TABLE t ADD COLUMN final_shape;\n",
        )
        con = db_driver.connect(self.db)
        try:
            migrate.applied_set(con)
            con.execute(
                "INSERT INTO schema_migrations (filename) VALUES (?)",
                (migrate._REBASELINE_FLOOR_MIGRATION,),
            )
            con.commit()
        finally:
            con.close()

        self._run()

        self.assertEqual(["a", "final_shape"], self._cols())
        self.assertEqual(
            {migrate._REBASELINE_FLOOR_MIGRATION, "0238_rebaseline.sql"},
            self._stamped(),
        )

    def test_direct_apply_rejects_unproven_rebaseline_floor(self):
        path = self.migdir / "0238_rebaseline.sql"
        self._write(
            path.name,
            "-- migrate: requires-rebaseline-floor\n"
            "ALTER TABLE t ADD COLUMN final_shape;\n",
        )
        with closing(db_driver.connect(self.db)) as con, self.assertRaisesRegex(
            migrate.MigrationPreconditionError, "prior migration floor"
        ):
            migrate.apply(con, path)

        self.assertEqual(["a"], self._cols())

    def test_direct_apply_rejects_unproven_purge_floor(self):
        path = self.migdir / "0001_purge.sql"
        self._write(
            path.name,
            "-- migrate: requires-dsh-purge-floor\n"
            "ALTER TABLE t ADD COLUMN purged;\n",
        )
        with closing(db_driver.connect(self.db)) as con, self.assertRaisesRegex(
            migrate.MigrationPreconditionError, "validated DSH purge floor"
        ):
            migrate.apply(con, path)

        self.assertEqual(["a"], self._cols())
        with closing(db_driver.connect(self.db)) as con:
            self.assertEqual(
                [],
                con.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='schema_migrations'"
                ).fetchall(),
            )


if __name__ == "__main__":
    unittest.main()
