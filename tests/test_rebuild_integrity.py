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
import seed_skills  # noqa: E402


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


def legacy_dev_kit() -> tuple:
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(SCHEMA.read_text())
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            if migration.name >= "0239_":
                break
            con.executescript(migration.read_text())
        return con.execute(
            "SELECT description,category,command,common,content,is_deleted "
            "FROM skills WHERE name='dev_kit'"
        ).fetchone()
    finally:
        con.close()


class RebuildIntegrityTest(unittest.TestCase):
    def test_post_snapshot_reconciles_only_untouched_legacy_dev_kit(self):
        legacy = legacy_dev_kit()
        desired = seed_skills.parse_skill(seed_skills.DEV_KIT_STARTER)
        for customization in ("", "\nFork-owned customization."):
            with self.subTest(
                customization=bool(customization)
            ), tempfile.TemporaryDirectory() as raw_tmp:
                tmp = Path(raw_tmp)
                outgoing = tmp / "shell_db.db"
                snapshot = tmp / "content.sql"
                apply_engine_schema(outgoing)
                body = legacy[4] + customization
                values = (*legacy[:4], body, legacy[5])
                snapshot.write_text(
                    "BEGIN;\n"
                    "INSERT INTO skills "
                    "(name,description,category,command,common,content,is_deleted) "
                    "VALUES ('dev_kit',{},{},{},{},{},{}) "
                    "ON CONFLICT(name) DO UPDATE SET "
                    "description=excluded.description,category=excluded.category,"
                    "command=excluded.command,common=excluded.common,"
                    "content=excluded.content,is_deleted=excluded.is_deleted;\n"
                    "COMMIT;\n".format(
                        *(seed_skills.sql_str(value) for value in values)
                    )
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
                    actual = con.execute(
                        "SELECT description,category,command,common,content,"
                        "is_deleted FROM skills WHERE name='dev_kit'"
                    ).fetchone()
                finally:
                    con.close()
                expected = (
                    values
                    if customization
                    else tuple(desired[field] for field in seed_skills.SEED_FIELDS)
                    + (0,)
                )
                self.assertEqual(actual, expected)

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
                "INSERT INTO conversation_messages "
                "(message_id,conversation_id,sender_kind,sender_ref,"
                "message_kind,body,idempotency_key,request_hash) "
                "VALUES (41,'cv_missing','engine','rebuild','notice',"
                "'orphan','orphan','orphan-hash')")
            con.commit()
            con.close()
            outgoing_before = digest(outgoing)

            snapshot.write_text(
                "PRAGMA foreign_keys=OFF;\n"
                "BEGIN;\n"
                "DELETE FROM conversation_messages;\n"
                "INSERT INTO conversation_messages "
                "(message_id,conversation_id,sender_kind,sender_ref,"
                "message_kind,body,idempotency_key,request_hash) "
                "VALUES (41,'cv_missing','engine','rebuild','notice',"
                "'orphan','orphan','orphan-hash');\n"
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
            self.assertIn("table conversation_messages row 41", message)
            self.assertEqual(digest(outgoing), outgoing_before)
            self.assertFalse(Path(str(outgoing) + ".rebuild").exists())

            backup_files = list(backups.glob("shell_db.prerebuild.*.db"))
            self.assertEqual(len(backup_files), 1)
            backup = sqlite3.connect(backup_files[0])
            try:
                row = backup.execute(
                    "SELECT conversation_id, body FROM conversation_messages "
                    "WHERE message_id=41").fetchone()
            finally:
                backup.close()
            self.assertEqual(row, ("cv_missing", "orphan"))


if __name__ == "__main__":
    unittest.main()
