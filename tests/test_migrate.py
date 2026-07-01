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

import shutil
import sys
import tempfile
import unittest
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

    def _run(self):
        with mock.patch.object(migrate, "MIGRATIONS_DIR", self.migdir):
            return migrate.migrate(self.db)

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


if __name__ == "__main__":
    unittest.main()
