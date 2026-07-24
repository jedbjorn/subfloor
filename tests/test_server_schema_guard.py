#!/usr/bin/env python3
"""Startup refusal for a materialized-engine / unmigrated-DB half floor."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"
ACK_MIGRATION = "0083_planner_alert_acknowledgement.sql"

sys.path.insert(0, str(ENGINE / "api"))
import server  # noqa: E402


def build_pre_acknowledgement_db(path: Path) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript(SCHEMA.read_text())
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            if migration.name == ACK_MIGRATION:
                break
            con.executescript(migration.read_text())
            con.execute(
                "INSERT INTO schema_migrations (filename) VALUES (?)",
                (migration.name,))
        con.commit()
    finally:
        con.close()


class ServerSchemaGuardTest(unittest.TestCase):
    def test_new_code_old_schema_refuses_startup_with_rollback_recovery(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            db_path = Path(raw_tmp) / "shell_db.db"
            build_pre_acknowledgement_db(db_path)
            con = sqlite3.connect(db_path)
            try:
                with self.assertRaises(sqlite3.OperationalError) as raw:
                    con.execute(
                        "SELECT acknowledged_at FROM planner_alerts").fetchall()
            finally:
                con.close()
            self.assertIn("no such column", str(raw.exception))

            with mock.patch.object(
                server, "DB_PATH", db_path
            ), mock.patch.object(
                server.ports_mod, "resolve", return_value={"port": 8800}
            ), mock.patch.object(
                server.backfill_shell_api_keys, "backfill"
            ) as backfill, self.assertRaises(SystemExit) as refused:
                server.main([])

            message = str(refused.exception)
            self.assertIn("installed engine/DB schema mismatch", message)
            self.assertIn(ACK_MIGRATION, message)
            self.assertIn("before first DB use", message)
            self.assertIn("`./sc rollback`", message)
            self.assertNotIn("no such column", message)
            backfill.assert_not_called()

    def test_current_migration_ledger_passes(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            db_path = Path(raw_tmp) / "shell_db.db"
            con = sqlite3.connect(db_path)
            try:
                con.executescript(SCHEMA.read_text())
                for migration in sorted(MIGRATIONS.glob("*.sql")):
                    con.execute(
                        "INSERT INTO schema_migrations (filename) VALUES (?)",
                        (migration.name,))
                con.commit()
            finally:
                con.close()

            server.require_current_schema(db_path, MIGRATIONS)


if __name__ == "__main__":
    unittest.main()
