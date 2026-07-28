#!/usr/bin/env python3
"""Mutation sentinel for update's installed live-state preflight (#528)."""
from __future__ import annotations

import contextlib
import hashlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"
sys.path.insert(0, str(ENGINE / "scripts"))
import update  # noqa: E402


def fingerprint(paths: list[Path]) -> dict[str, tuple[int, int, str | None]]:
    out = {}
    for path in paths:
        stat = path.stat()
        digest = None
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        out[str(path)] = (stat.st_mtime_ns, stat.st_size, digest)
    return out


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def build_live_db(path: Path) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript(SCHEMA.read_text())
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            con.executescript(migration.read_text())
        con.execute(
            "INSERT INTO users (user_id, username, is_active) "
            "VALUES (1,'operator',1)")
        con.execute(
            "INSERT INTO shells "
            "(shell_id, display_name, shortname, mandate, system_prompt, "
            "user_id, is_shared, has_identity, bootstrapped) "
            "VALUES (1,'Admin','AMI','test','test',1,0,1,1)")
        con.execute(
            "INSERT INTO documents (document_id, kind, title, frozen) "
            "VALUES (59,'doc','SPRINT: update guard',0)")
        con.execute(
            "INSERT INTO sprint_units "
            "(sprint_doc_id, seq, unit_title, state) "
            "VALUES (59,'U1','guard proof','working')")
        con.commit()
    finally:
        con.close()


class UpdatePreflightSentinelTest(unittest.TestCase):
    def test_live_refusal_precedes_every_installed_floor_mutation(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            engine = root / ".super-coder"
            state = root / ".sc-state"
            workflow = root / ".github" / "workflows"
            engine.mkdir()
            state.mkdir()
            workflow.mkdir(parents=True)

            paths = [
                root / "sc",
                engine,
                engine / "schema.sql",
                engine / "scripts",
                engine / "scripts" / "update.py",
                workflow,
                workflow / "subfloor-visual-qa.yml",
                root / ".gitignore",
                state / "engine.ref.prev",
                state / "engine.ref",
            ]
            for path in paths:
                if path.suffix == "" and path.name in {"scripts"}:
                    path.mkdir()
                elif path in {engine, workflow}:
                    continue
                else:
                    path.write_text(f"sentinel:{path.name}\n")

            before = fingerprint(paths)
            with mock.patch.multiple(
                update,
                REPO_ROOT=root,
                ENGINE=engine,
                DB_PATH=engine / "shell_db.db",
                STATE_DIR=state,
                ENGINE_REF=state / "engine.ref",
                ENGINE_REF_PREV=state / "engine.ref.prev",
                EJECTED_MARKER=state / "ejected",
            ), mock.patch.object(
                update, "is_source_repo", return_value=False
            ), mock.patch.object(
                update, "fetch_update_ref", return_value="a" * 40
            ) as fetch, mock.patch.object(
                update, "active_sprint_ids", return_value={59}
            ), mock.patch.object(
                update, "migrate_engine_untrack"
            ) as untrack, mock.patch.object(
                update.install_mod, "ensure_gitignore"
            ) as gitignore, mock.patch.object(
                update, "materialize_fetched_engine"
            ) as materialize, mock.patch.object(
                update, "ensure_workflows"
            ) as workflows:
                with self.assertRaises(SystemExit) as ctx:
                    update.main([])

            self.assertIn("refusing", str(ctx.exception))
            fetch.assert_called_once()
            untrack.assert_not_called()
            gitignore.assert_not_called()
            materialize.assert_not_called()
            workflows.assert_not_called()
            self.assertEqual(
                fingerprint(paths),
                before,
                "update refusal changed installed hashes or mtimes",
            )

    def test_real_fork_fetch_refusal_preserves_dispatcher_pins_db_and_engine(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            upstream_work = root / "upstream-work"
            upstream_remote = root / "subfloor.git"
            fork = root / "fork"
            upstream_work.mkdir()
            fork.mkdir()

            git(upstream_work, "init", "-b", "main")
            git(upstream_work, "config", "user.name", "Update Test")
            git(upstream_work, "config", "user.email", "update@example.invalid")
            (upstream_work / ".super-coder" / "migrations").mkdir(parents=True)
            (upstream_work / "sc").write_text("target dispatcher\n")
            (upstream_work / ".super-coder" / "schema.sql").write_text(
                "target engine schema\n")
            (upstream_work / ".super-coder" / "migrations"
             / "9999_target.sql").write_text("CREATE TABLE target_new(x);\n")
            git(upstream_work, "add", ".")
            git(upstream_work, "commit", "-m", "target floor")
            subprocess.run(
                ["git", "clone", "--bare", str(upstream_work),
                 str(upstream_remote)],
                check=True,
                capture_output=True,
                text=True,
            )

            git(fork, "init", "-b", "main")
            git(fork, "config", "user.name", "Update Test")
            git(fork, "config", "user.email", "update@example.invalid")
            git(fork, "remote", "add", "origin", str(root / "ami-fork.git"))
            git(fork, "remote", "add", "super-coder", str(upstream_remote))
            engine = fork / ".super-coder"
            state = fork / ".sc-state"
            scripts = engine / "scripts"
            migrations = engine / "migrations"
            scripts.mkdir(parents=True)
            migrations.mkdir()
            state.mkdir()
            installed = {
                fork / "sc": "installed dispatcher\n",
                engine / "schema.sql": "installed engine schema\n",
                engine / "engine.manifest": "{\"installed\": true}\n",
                scripts / "update.py": "installed updater\n",
                migrations / "0084_installed.sql": "installed migration\n",
                state / "engine.ref": "1" * 40 + "\n",
                state / "engine.ref.prev": "0" * 40 + "\n",
            }
            for path, content in installed.items():
                path.write_text(content)
            db_path = engine / "shell_db.db"
            build_live_db(db_path)
            git(fork, "add", ".")
            git(fork, "commit", "-m", "installed fork floor")

            watched = list(installed)
            before_files = fingerprint(watched)
            with contextlib.closing(sqlite3.connect(db_path)) as con:
                before_ledger = con.execute(
                    "SELECT filename, applied_at FROM schema_migrations "
                    "ORDER BY filename").fetchall()
                before_schema = con.execute(
                    "SELECT type, name, sql FROM sqlite_master "
                    "WHERE sql IS NOT NULL ORDER BY type, name").fetchall()

            with mock.patch.multiple(
                update,
                REPO_ROOT=fork,
                ENGINE=engine,
                DB_PATH=db_path,
                STATE_DIR=state,
                ENGINE_REF=state / "engine.ref",
                ENGINE_REF_PREV=state / "engine.ref.prev",
                EJECTED_MARKER=state / "ejected",
            ), mock.patch.object(
                sys.stdin, "isatty", return_value=False
            ), self.assertRaises(SystemExit) as ctx:
                update.main([])

            self.assertIn("refusing", str(ctx.exception))
            self.assertIn("ACTIVE sprint", str(ctx.exception))
            self.assertEqual(fingerprint(watched), before_files)
            with contextlib.closing(sqlite3.connect(db_path)) as con:
                self.assertEqual(con.execute(
                    "SELECT filename, applied_at FROM schema_migrations "
                    "ORDER BY filename").fetchall(), before_ledger)
                self.assertEqual(con.execute(
                    "SELECT type, name, sql FROM sqlite_master "
                    "WHERE sql IS NOT NULL ORDER BY type, name").fetchall(),
                    before_schema)


class UpdateActiveSprintGuardTest(unittest.TestCase):
    def test_active_sprint_refuses_without_consent_path(self):
        with mock.patch.object(
            update, "active_sprint_ids", return_value={59, 60}
        ), self.assertRaises(SystemExit) as ctx:
            update.preflight_live_state()
        self.assertIn("59, 60", str(ctx.exception))
        self.assertIn("Close or freeze", str(ctx.exception))

    def test_no_active_sprint_proceeds(self):
        with mock.patch.object(update, "active_sprint_ids", return_value=set()):
            self.assertIsNone(update.preflight_live_state())


if __name__ == "__main__":
    unittest.main()
