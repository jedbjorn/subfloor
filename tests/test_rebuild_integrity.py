#!/usr/bin/env python3
"""Candidate-DB integrity and outgoing-preservation tests for rebuild (#533)."""
from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"
sys.path.insert(0, str(ENGINE / "scripts"))
import rebuild  # noqa: E402


def apply_engine_schema(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text())
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(migration.read_text())
    con.commit()
    con.execute("PRAGMA journal_mode=WAL").fetchone()
    con.close()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RebuildIntegrityTest(unittest.TestCase):
    def test_cleanup_migration_removes_orphans_and_terminal_volatile_state(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            db = Path(raw_tmp) / "shell_db.db"
            apply_engine_schema(db)
            con = sqlite3.connect(db)
            con.execute(
                "INSERT INTO users (user_id, username, is_active) "
                "VALUES (1,'test',1)")
            con.execute(
                "INSERT INTO shells (shell_id, display_name, shortname, mandate, "
                "system_prompt, user_id, is_shared, has_identity, bootstrapped) "
                "VALUES (1,'S1','s1','test','sp',1,0,1,1)")
            con.execute(
                "INSERT INTO interface_generations "
                "(shell_id, generation, ended_at) VALUES (1,1,datetime('now'))")
            con.execute(
                "INSERT INTO interface_sessions "
                "(session_id, shell_id, generation, occupancy, lifecycle, ended_at) "
                "VALUES (1,1,1,'ended','ended',datetime('now'))")
            con.execute(
                "INSERT INTO interface_input_state "
                "(session_id, shell_id, generation, composer, pending_seq) "
                "VALUES (1,1,1,'unknown',7)")
            con.execute(
                "INSERT INTO interface_writer_leases "
                "(session_id, shell_id, generation, client_id, token_hash) "
                "VALUES (1,1,1,'client','hash')")
            con.execute(
                "INSERT INTO planner_alerts "
                "(alert_id, session_id, severity, reason, dedupe_key) "
                "VALUES (41,999,'warning','legacy-orphan','orphan')")
            con.commit()

            con.executescript(
                (MIGRATIONS / "0083_interface_integrity_cleanup.sql").read_text())
            self.assertIsNone(con.execute(
                "SELECT 1 FROM interface_input_state WHERE session_id=1"
            ).fetchone())
            revoked_at, reason = con.execute(
                "SELECT revoked_at, revoke_reason FROM interface_writer_leases "
                "WHERE session_id=1").fetchone()
            self.assertIsNotNone(revoked_at)
            self.assertEqual(reason, "session_end")
            self.assertIsNone(con.execute(
                "SELECT 1 FROM planner_alerts WHERE alert_id=41"
            ).fetchone())
            con.close()

    def test_valid_candidate_atomically_replaces_outgoing_db(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            outgoing = tmp / "shell_db.db"
            snapshot = tmp / "content.sql"
            apply_engine_schema(outgoing)
            con = sqlite3.connect(outgoing)
            con.execute(
                "INSERT INTO users (user_id, username, is_active) "
                "VALUES (1,'before',1)")
            con.execute(
                "INSERT INTO shells (shell_id, display_name, shortname, mandate, "
                "system_prompt, user_id, is_shared, has_identity, bootstrapped, "
                "api_key) VALUES (1,'Before','s1','test','sp',1,0,1,1,'keep-key')")
            con.commit()
            con.close()

            snapshot.write_text(
                "PRAGMA foreign_keys=OFF;\n"
                "BEGIN;\n"
                "DELETE FROM users;\n"
                "INSERT INTO users (user_id, username, is_active) "
                "VALUES (1,'after',1);\n"
                "DELETE FROM shells;\n"
                "INSERT INTO shells (shell_id, display_name, shortname, mandate, "
                "system_prompt, user_id, is_shared, has_identity, bootstrapped) "
                "VALUES (1,'After','s1','test','sp',1,0,1,1);\n"
                "COMMIT;\n"
                "PRAGMA foreign_keys=ON;\n"
            )

            with mock.patch.multiple(
                rebuild,
                ENGINE=tmp / ".super-coder",
                DB_PATH=outgoing,
                REPO_ROOT=tmp,
                SNAPSHOT=snapshot,
                SNAPSHOT_LEGACY=tmp / "missing-content.sql",
            ), mock.patch.object(rebuild.map_repo, "main"):
                self.assertEqual(rebuild.main(["--no-backup"]), 0)

            con = sqlite3.connect(outgoing)
            try:
                row = con.execute(
                    "SELECT u.username, s.display_name, s.api_key "
                    "FROM users u JOIN shells s ON s.user_id=u.user_id"
                ).fetchone()
            finally:
                con.close()
            self.assertEqual(row, ("after", "After", "keep-key"))
            self.assertFalse(Path(str(outgoing) + ".rebuild").exists())

    def test_orphan_snapshot_refuses_without_replacing_outgoing_db_or_backup(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            outgoing = tmp / "shell_db.db"
            snapshot = tmp / "content.sql"
            backups = tmp / "backups"
            backups.mkdir()

            apply_engine_schema(outgoing)
            con = sqlite3.connect(outgoing)
            con.execute(
                "INSERT INTO planner_alerts "
                "(alert_id, session_id, severity, reason, dedupe_key) "
                "VALUES (41,999,'warning','legacy-orphan','orphan')")
            con.commit()
            con.close()
            outgoing_before = digest(outgoing)

            snapshot.write_text(
                "PRAGMA foreign_keys=OFF;\n"
                "BEGIN;\n"
                "DELETE FROM planner_alerts;\n"
                "INSERT INTO planner_alerts "
                "(alert_id, session_id, severity, reason, dedupe_key) "
                "VALUES (41,999,'warning','legacy-orphan','orphan');\n"
                "COMMIT;\n"
                "PRAGMA foreign_keys=ON;\n"
            )

            with mock.patch.multiple(
                rebuild,
                DB_PATH=outgoing,
                REPO_ROOT=tmp,
                SNAPSHOT=snapshot,
                SNAPSHOT_LEGACY=tmp / "missing-content.sql",
            ), mock.patch.object(
                rebuild, "backup_dir", return_value=backups
            ), mock.patch.object(rebuild.map_repo, "main"):
                with self.assertRaises(SystemExit) as ctx:
                    rebuild.main([])

            message = str(ctx.exception)
            self.assertIn("foreign-key check failed", message)
            self.assertIn("table planner_alerts row 41", message)
            self.assertEqual(digest(outgoing), outgoing_before)
            self.assertFalse(Path(str(outgoing) + ".rebuild").exists())

            backup_files = list(backups.glob("shell_db.prerebuild.*.db"))
            self.assertEqual(len(backup_files), 1)
            backup = sqlite3.connect(backup_files[0])
            try:
                row = backup.execute(
                    "SELECT session_id, reason FROM planner_alerts "
                    "WHERE alert_id=41").fetchone()
            finally:
                backup.close()
            self.assertEqual(row, (999, "legacy-orphan"))


if __name__ == "__main__":
    unittest.main()
