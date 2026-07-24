#!/usr/bin/env python3
"""End-to-end recovery for a materialized-engine / unchanged-DB half floor."""
from __future__ import annotations

import contextlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
import rollback  # noqa: E402


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def write_db(
    path: Path,
    value: str,
    migrations: tuple[str, ...] = ("0001_old.sql",),
) -> None:
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE state (value TEXT)")
        con.execute("INSERT INTO state VALUES (?)", (value,))
        con.execute(
            "CREATE TABLE schema_migrations "
            "(filename TEXT PRIMARY KEY, applied_at TEXT)")
        con.executemany(
            "INSERT INTO schema_migrations (filename) VALUES (?)",
            ((filename,) for filename in migrations),
        )
        con.commit()
    finally:
        con.close()


def read_db(path: Path) -> str:
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT value FROM state").fetchone()[0]
    finally:
        con.close()


class HalfFloorRollbackTest(unittest.TestCase):
    def test_engine_only_refuses_previous_floor_with_unrelated_extra(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            engine = root / ".super-coder"
            state = root / ".sc-state"
            migrations = engine / "migrations"
            migrations.mkdir(parents=True)
            state.mkdir()
            (migrations / "0001_old.sql").write_text("old\n")
            (migrations / "0002_new.sql").write_text("new\n")
            db_path = engine / "shell_db.db"
            write_db(
                db_path,
                "unknown schema floor",
                ("0001_old.sql", "9999_unrelated.sql"),
            )
            (state / "engine.ref").write_text("new-sha\n")
            (state / "engine.ref.prev").write_text("old-sha\n")

            previous_tree = subprocess.CompletedProcess(
                args=(), returncode=0,
                stdout=".super-coder/migrations/0001_old.sql\n",
            )
            with mock.patch.multiple(
                rollback,
                ENGINE=engine,
                DB_PATH=db_path,
                ENGINE_REF=state / "engine.ref",
                ENGINE_REF_PREV=state / "engine.ref.prev",
            ), mock.patch.multiple(
                rollback.update_mod,
                EJECTED_MARKER=state / "ejected",
                git=mock.Mock(return_value=previous_tree),
            ), mock.patch.object(
                rollback, "backup_current_db"
            ) as backup, mock.patch.object(
                rollback, "restore_engine"
            ) as restore, self.assertRaises(SystemExit) as refused:
                rollback.main(["--engine-only"])

            self.assertIn(
                "does not exactly retain the previous engine migration floor",
                str(refused.exception),
            )
            self.assertIn("missing=[]", str(refused.exception))
            self.assertIn(
                "unexpected=['9999_unrelated.sql']",
                str(refused.exception),
            )
            backup.assert_not_called()
            restore.assert_not_called()

    def test_engine_only_preserves_db_with_no_or_older_restore_point(self):
        for older_backup in (False, True):
            with self.subTest(older_backup=older_backup), \
                    tempfile.TemporaryDirectory() as raw_tmp:
                root = Path(raw_tmp)
                engine = root / ".super-coder"
                state = root / ".sc-state"
                backups = state / "db_backups"
                scripts = engine / "scripts"
                migrations = engine / "migrations"
                scripts.mkdir(parents=True)
                migrations.mkdir()
                state.mkdir()
                backups.mkdir()

                git(root, "init", "-b", "main")
                git(root, "config", "user.name", "Rollback Test")
                git(root, "config", "user.email",
                    "rollback@example.invalid")
                (root / "sc").write_text("old dispatcher\n")
                (scripts / "floor.txt").write_text("old engine\n")
                (migrations / "0001_old.sql").write_text(
                    "CREATE TABLE old_floor(x);\n")
                git(root, "add", ".")
                git(root, "commit", "-m", "old floor")
                old_sha = git(root, "rev-parse", "HEAD")

                (root / "sc").write_text("new dispatcher\n")
                (scripts / "floor.txt").write_text("new engine\n")
                (migrations / "0002_new.sql").write_text(
                    "ALTER TABLE state ADD COLUMN new_floor TEXT;\n")
                git(root, "add", ".")
                git(root, "commit", "-m", "new floor")
                new_sha = git(root, "rev-parse", "HEAD")

                db_path = engine / "shell_db.db"
                write_db(db_path, "current unchanged DB")
                (state / "engine.ref").write_text(new_sha + "\n")
                (state / "engine.ref.prev").write_text(old_sha + "\n")
                if older_backup:
                    write_db(
                        backups / "shell_db.prerebuild.20000101_000000.db",
                        "stale older backup",
                    )

                db_before = db_path.read_bytes()
                with contextlib.ExitStack() as stack:
                    stack.enter_context(mock.patch.multiple(
                        rollback,
                        REPO_ROOT=root,
                        ENGINE=engine,
                        DB_PATH=db_path,
                        STATE_DIR=state,
                        ENGINE_REF=state / "engine.ref",
                        ENGINE_REF_PREV=state / "engine.ref.prev",
                    ))
                    stack.enter_context(mock.patch.multiple(
                        rollback.update_mod,
                        REPO_ROOT=root,
                        ENGINE=engine,
                    ))
                    stack.enter_context(mock.patch.multiple(
                        rollback.rebuild_mod,
                        REPO_ROOT=root,
                        ENGINE=engine,
                        DB_PATH=db_path,
                    ))
                    stack.enter_context(mock.patch.object(
                        rollback.rebuild_mod,
                        "backup_dir",
                        return_value=backups,
                    ))
                    stack.enter_context(mock.patch.multiple(
                        rollback.engine_manifest,
                        REPO_ROOT=root,
                        ENGINE=engine,
                        MANIFEST=engine / "engine.manifest",
                    ))
                    self.assertEqual(
                        rollback.main(["--engine-only"]), 0)

                self.assertEqual(db_path.read_bytes(), db_before)
                self.assertEqual(read_db(db_path), "current unchanged DB")
                self.assertEqual((root / "sc").read_text(), "old dispatcher\n")
                self.assertEqual(
                    (scripts / "floor.txt").read_text(), "old engine\n")
                self.assertFalse((migrations / "0002_new.sql").exists())
                self.assertEqual(
                    (state / "engine.ref").read_text(), old_sha + "\n")
                self.assertFalse((state / "engine.ref.prev").exists())
                self.assertEqual(
                    read_db(next(backups.glob("shell_db.prerollback.*.db"))),
                    "current unchanged DB",
                )


if __name__ == "__main__":
    unittest.main()
