"""Lifecycle convergence for upstream skill tombstones."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
MIGRATIONS = ENGINE / "migrations"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import rebuild
import seed_skills
import snapshot
import update
from skill_convergence_fixtures import (
    LOCAL_SKILL_CONTENT,
    LOCAL_SKILL_DESCRIPTION,
    LOCAL_SKILL_NAME,
    TOMBSTONE_SKILLS,
    build_dirty_skill_fork,
)

TRAILING_MIGRATIONS = (
    MIGRATIONS / "0154_remove_tombstoned_skills.sql",
    MIGRATIONS / "0239_global_skill_simplification.sql",
)


def logical_dump(path: Path) -> str:
    with closing(sqlite3.connect(path)) as con:
        return "\n".join(con.iterdump())


class SkillLifecycleConvergenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sc-skill-lifecycle-")
        self.addCleanup(self.tmp.cleanup)
        self.fixture = build_dirty_skill_fork(Path(self.tmp.name) / "dos-arch")

    def assert_converged(self) -> None:
        placeholders = ",".join("?" for _ in TOMBSTONE_SKILLS)
        with closing(sqlite3.connect(self.fixture.database)) as con:
            self.assertEqual(
                con.execute(
                    f"SELECT COUNT(*) FROM skills WHERE name IN ({placeholders})",
                    TOMBSTONE_SKILLS,
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM shell_skills ss JOIN skills s "
                    "USING (skill_id) "
                    f"WHERE s.name IN ({placeholders})",
                    TOMBSTONE_SKILLS,
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM flavor_skills fs JOIN skills s "
                    "USING (skill_id) "
                    f"WHERE s.name IN ({placeholders})",
                    TOMBSTONE_SKILLS,
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                con.execute(
                    "SELECT description, category, content, command, common, "
                    "is_deleted FROM skills WHERE name=?",
                    (LOCAL_SKILL_NAME,),
                ).fetchone(),
                (
                    LOCAL_SKILL_DESCRIPTION,
                    "fork",
                    LOCAL_SKILL_CONTENT.decode(),
                    None,
                    0,
                    0,
                ),
            )
            self.assertEqual(
                con.execute(
                    "SELECT ss.shell_id, s.name FROM shell_skills ss "
                    "JOIN skills s USING (skill_id) WHERE s.name=?",
                    (LOCAL_SKILL_NAME,),
                ).fetchall(),
                [(self.fixture.bespoke_shell_id, LOCAL_SKILL_NAME)],
            )
            self.assertEqual(
                con.execute(
                    "SELECT fs.flavor, s.name FROM flavor_skills fs "
                    "JOIN skills s USING (skill_id) WHERE s.name=?",
                    (LOCAL_SKILL_NAME,),
                ).fetchall(),
                [("dev", LOCAL_SKILL_NAME)],
            )
            self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])

    @contextmanager
    def patched_rebuild(self):
        with mock.patch.multiple(
            rebuild,
            ENGINE=self.fixture.engine,
            REPO_ROOT=self.fixture.root,
            DB_PATH=self.fixture.database,
            SCHEMA_SQLITE=ENGINE / "schema.sql",
            SNAPSHOT=self.fixture.snapshot,
            SNAPSHOT_LEGACY=self.fixture.root / "missing-content.sql",
        ), mock.patch.object(
            rebuild.migrate_mod, "MIGRATIONS_DIR", MIGRATIONS
        ), mock.patch.object(
            rebuild.seed_skills, "apply_retired", return_value=[]
        ), mock.patch.object(rebuild.map_repo, "main"):
            yield

    def test_trailing_migration_converges_dirty_database_twice(self) -> None:
        with closing(sqlite3.connect(self.fixture.database)) as con:
            for migration in TRAILING_MIGRATIONS:
                con.executescript(migration.read_text())
                con.executescript(migration.read_text())
        self.assert_converged()

    def test_rebuild_reconciles_stale_snapshot_twice_before_publish(self) -> None:
        with self.patched_rebuild():
            self.assertEqual(rebuild.main(["--no-backup"]), 0)
            self.assert_converged()
            self.assertEqual(rebuild.main(["--no-backup"]), 0)
        self.assert_converged()

    def test_rebuild_reconciliation_failure_preserves_outgoing_database(self) -> None:
        before = logical_dump(self.fixture.database)
        with self.patched_rebuild(), mock.patch.object(
            rebuild.seed_skills,
            "reconcile_tombstoned_skills",
            side_effect=RuntimeError("reconcile failed"),
        ), self.assertRaisesRegex(RuntimeError, "reconcile failed"):
            rebuild.main(["--no-backup"])
        self.assertEqual(logical_dump(self.fixture.database), before)
        self.assertFalse(Path(str(self.fixture.database) + ".rebuild").exists())

    def test_update_sync_reconciles_after_seed_before_regrant(self) -> None:
        with mock.patch.object(update, "DB_PATH", self.fixture.database), \
                mock.patch.object(update.seed_skills, "apply_retired", return_value=[]):
            update.sync_skills()
            self.assert_converged()
            update.sync_skills()
        self.assert_converged()

    def test_boot_render_shared_heal_reconciles_even_when_active_rows_are_fresh(
        self,
    ) -> None:
        with closing(sqlite3.connect(self.fixture.database)) as con, \
                mock.patch.object(seed_skills, "apply_retired", return_value=[]):
            seed_skills.sync_engine_skills(con)
        self.assert_converged()

    def test_snapshot_omits_tombstone_rows_and_both_grant_types(self) -> None:
        with closing(sqlite3.connect(self.fixture.database)) as con:
            rendered = "\n".join(
                [
                    *snapshot.dump_local_skills(con),
                    *snapshot.dump_shell_skills(con),
                    *snapshot.dump_flavor_skills(con),
                ]
            )
        self.assertIn(f"VALUES ('{LOCAL_SKILL_NAME}',", rendered)
        self.assertIn(f"WHERE name='{LOCAL_SKILL_NAME}'", rendered)
        for name in TOMBSTONE_SKILLS:
            self.assertNotIn(f"VALUES ('{name}',", rendered)
            self.assertNotIn(f"WHERE name='{name}'", rendered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
