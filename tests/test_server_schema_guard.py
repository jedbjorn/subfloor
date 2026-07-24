#!/usr/bin/env python3
"""Startup refusals: a materialized-engine / unmigrated-DB half floor, and a
non-loopback bind (spec #26)."""
from __future__ import annotations

import os
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
            self.assertIn("`./sc rollback --engine-only`", message)
            self.assertIn("preserving this unchanged DB", message)
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


class LoopbackBindGuardTest(unittest.TestCase):
    """Spec #26 Failure Modes: a non-loopback bind refuses to start.

    This is the fence behind the automatic browser bootstrap: a session mints
    for any caller that can present an allowed `Host` and a same-origin
    `Origin`, both of which a remote client chooses freely. Unreachability is
    therefore the control, and it has to be enforced rather than assumed.
    """

    def test_host_refuses_a_non_loopback_bind(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SC_SANDBOX", None)
            for bind in ("0.0.0.0", "", "::", "192.168.1.10",
                         "10.0.0.5", "example.com"):
                with self.subTest(bind=bind):
                    with self.assertRaises(SystemExit) as caught:
                        server.require_loopback_bind(bind)
                    self.assertIn("loopback", str(caught.exception))

    def test_host_accepts_loopback_binds(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SC_SANDBOX", None)
            for bind in ("127.0.0.1", "localhost", "LocalHost", "::1",
                         "[::1]", "127.0.0.53"):
                with self.subTest(bind=bind):
                    server.require_loopback_bind(bind)

    def test_sandbox_keeps_the_wide_bind_docker_publishes(self):
        # `./sc launch` sets SC_BIND=0.0.0.0 in the container ON PURPOSE so
        # docker can publish the port; the boundary there is the
        # `-p 127.0.0.1:PORT:PORT` mapping, which is loopback-only on the
        # host whatever the container binds. Refusing here would make the
        # sandbox unlaunchable while removing no exposure — so the guard
        # stands down, and only here.
        with mock.patch.dict(os.environ, {"SC_SANDBOX": "1"}):
            server.require_loopback_bind("0.0.0.0")


if __name__ == "__main__":
    unittest.main()
