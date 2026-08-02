"""Migration scaffold and migration-number guardrails."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))

import migration  # noqa: E402
import seed_skills  # noqa: E402


class MigrationNumberGuardTest(unittest.TestCase):
    def test_current_tree_has_only_the_exact_frozen_0155_collision(self):
        migration.validate_unique_numbers(ENGINE / "migrations")

    def test_synthetic_duplicate_number_is_rejected_with_both_files_named(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            (directory / "0160_first.sql").write_text("BEGIN; COMMIT;\n")
            (directory / "0160_second.sql").write_text("BEGIN; COMMIT;\n")

            with self.assertRaisesRegex(
                migration.MigrationScaffoldError,
                r"duplicate migration number 0160: 0160_first\.sql, 0160_second\.sql",
            ):
                migration.validate_unique_numbers(directory)

    def test_frozen_number_rejects_an_unallowlisted_third_file(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            names = (*migration.FROZEN_NUMBER_COLLISIONS["0155"], "0155_third.sql")
            for name in names:
                (directory / name).write_text("BEGIN; COMMIT;\n")

            with self.assertRaisesRegex(
                migration.MigrationScaffoldError,
                "duplicate migration number 0155",
            ):
                migration.validate_unique_numbers(directory)


class MigrationScaffoldTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.migrations = self.root / ".super-coder" / "migrations"
        self.migrations.mkdir(parents=True)
        self.manifest = (
            self.root
            / "tests"
            / "fixtures"
            / "sprint_removal"
            / "manifest.json"
        )
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text(
            json.dumps({"allowed_reference_files": ["existing"]}) + "\n"
        )
        (self.migrations / "0159_existing.sql").write_text("BEGIN; COMMIT;\n")

    def tearDown(self):
        self.temporary.cleanup()

    def test_new_allocates_writes_skeleton_and_allowlists_in_one_call(self):
        created = migration.new_migration(
            "reseed_migration_management",
            migrations_dir=self.migrations,
            manifest_path=self.manifest,
        )

        self.assertEqual(created.name, "0160_reseed_migration_management.sql")
        self.assertEqual(
            created.read_text(),
            "-- 0160 — reseed migration management.\n"
            "-- Intent: describe the durable schema or system-content change here.\n"
            "-- Keep every statement idempotent (for example: IF NOT EXISTS or "
            "INSERT OR IGNORE).\n\n"
            "BEGIN;\n\n"
            "-- Migration statements go here.\n\n"
            "COMMIT;\n",
        )
        allowed = json.loads(self.manifest.read_text())["allowed_reference_files"]
        self.assertEqual(
            allowed,
            [
                "existing",
                ".super-coder/migrations/0160_reseed_migration_management.sql",
            ],
        )
        self.assertEqual(list(self.migrations.glob("0160_*.sql")), [created])

    def test_invalid_slug_creates_neither_migration_nor_manifest_entry(self):
        before = self.manifest.read_text()
        with self.assertRaisesRegex(
            migration.MigrationScaffoldError,
            "lowercase snake_case",
        ):
            migration.new_migration(
                "Bad-Slug",
                migrations_dir=self.migrations,
                manifest_path=self.manifest,
            )

        self.assertEqual(self.manifest.read_text(), before)
        self.assertEqual(list(self.migrations.glob("0160_*.sql")), [])

    def test_fork_without_source_test_manifest_still_gets_the_migration(self):
        missing_manifest = self.root / "not-shipped" / "manifest.json"
        created = migration.new_migration(
            "fork_local_table",
            migrations_dir=self.migrations,
            manifest_path=missing_manifest,
        )

        self.assertEqual(created.name, "0160_fork_local_table.sql")
        self.assertTrue(created.exists())
        self.assertFalse(missing_manifest.exists())

    def test_manifest_write_failure_removes_the_unallowlisted_migration(self):
        with mock.patch.object(
            migration,
            "_write_manifest",
            side_effect=OSError("disk full"),
        ), self.assertRaisesRegex(OSError, "disk full"):
            migration.new_migration(
                "reseed_migration_management",
                migrations_dir=self.migrations,
                manifest_path=self.manifest,
            )

        self.assertEqual(list(self.migrations.glob("0160_*.sql")), [])
        self.assertEqual(
            json.loads(self.manifest.read_text())["allowed_reference_files"],
            ["existing"],
        )

    def test_exclusive_create_loser_does_not_delete_the_winners_file(self):
        winner = self.migrations / "0160_reseed_migration_management.sql"
        winner.write_text("winner\n")
        with mock.patch.object(
            migration,
            "next_free_number",
            return_value="0160",
        ), self.assertRaises(FileExistsError):
            migration.new_migration(
                "reseed_migration_management",
                migrations_dir=self.migrations,
                manifest_path=self.manifest,
            )

        self.assertEqual(winner.read_text(), "winner\n")
        self.assertEqual(
            json.loads(self.manifest.read_text())["allowed_reference_files"],
            ["existing"],
        )


class MigrationCliTest(unittest.TestCase):
    def test_help_uses_the_callers_engine_from_a_linked_worktree(self):
        completed = subprocess.run(
            [str(ROOT / "sc"), "migration", "new", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("usage: ./sc migration new", completed.stdout)
        self.assertEqual(completed.stderr, "")


class MigrationManagementReseedTest(unittest.TestCase):
    def test_scaffold_created_reseed_is_allowlisted(self):
        manifest = json.loads(
            (ROOT / "tests/fixtures/sprint_removal/manifest.json").read_text()
        )
        self.assertIn(
            ".super-coder/migrations/0160_reseed_migration_management.sql",
            manifest["allowed_reference_files"],
        )

    def test_terminal_reseed_matches_the_authoritative_skill_asset(self):
        expected = seed_skills.parse_skill(
            ENGINE / "assets/skills/migration_management/SKILL.md"
        )
        con = sqlite3.connect(":memory:")
        try:
            con.executescript(
                "CREATE TABLE skills ("
                "skill_id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT, "
                "category TEXT, command TEXT, common INTEGER, content TEXT, "
                "is_deleted INTEGER DEFAULT 0);"
            )
            con.executescript(
                (
                    ENGINE
                    / "migrations/0160_reseed_migration_management.sql"
                ).read_text()
            )
            actual = con.execute(
                "SELECT name,description,category,command,common,content,is_deleted "
                "FROM skills WHERE name='migration_management'"
            ).fetchone()
        finally:
            con.close()

        self.assertEqual(
            actual,
            (
                expected["name"],
                expected["description"],
                expected["category"],
                expected["command"],
                expected["common"],
                expected["content"],
                0,
            ),
        )
        self.assertIn("./sc migration new <slug>", actual[5])
        self.assertIn("WAL-safe `premigrate` backup", actual[5])
        self.assertIn("separate `preupdate` backup", actual[5])


if __name__ == "__main__":
    unittest.main()
