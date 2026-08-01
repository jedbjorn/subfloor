#!/usr/bin/env python3
"""Contract tests for the reusable feature-33 dirty downstream fixture."""
from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from skill_convergence_fixtures import (
    BASELINE_LAST_MIGRATION,
    BASELINE_SHA,
    LOCAL_SKILL_NAME,
    NATIVE_SKILL_DIRS,
    TOMBSTONE_SKILLS,
    build_dirty_skill_fork,
)


class DirtySkillForkFixtureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sc-skill-convergence-")
        self.addCleanup(self.tmp.cleanup)
        self.fixture = build_dirty_skill_fork(Path(self.tmp.name) / "dos-arch")

    def test_database_and_snapshot_carry_every_dirty_authority(self) -> None:
        con = sqlite3.connect(self.fixture.database)
        try:
            names = (*TOMBSTONE_SKILLS, LOCAL_SKILL_NAME)
            placeholders = ",".join("?" for _ in names)
            rows = con.execute(
                f"SELECT name, content FROM skills WHERE name IN ({placeholders}) "
                "ORDER BY name",
                names,
            ).fetchall()
            shell_grants = con.execute(
                f"SELECT s.name FROM shell_skills ss JOIN skills s USING (skill_id) "
                f"WHERE s.name IN ({placeholders}) ORDER BY s.name",
                names,
            ).fetchall()
            flavor_grants = con.execute(
                f"SELECT s.name FROM flavor_skills fs JOIN skills s USING (skill_id) "
                f"WHERE fs.flavor='dev' AND s.name IN ({placeholders}) "
                "ORDER BY s.name",
                names,
            ).fetchall()
            violations = con.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            con.close()

        expected = sorted(names)
        self.assertEqual([name for name, _ in rows], expected)
        self.assertEqual([name for (name,) in shell_grants], expected)
        self.assertEqual([name for (name,) in flavor_grants], expected)
        self.assertEqual(violations, [])
        self.assertEqual(
            dict(rows)[LOCAL_SKILL_NAME].encode(),
            self.fixture.expected_local_content,
        )

        snapshot = self.fixture.snapshot.read_text()
        for name in names:
            self.assertIn(f"WHERE name='{name}'", snapshot)
            self.assertIn(f"VALUES ('{name}',", snapshot)

    def test_stale_snapshot_resurrects_tombstones_twice_without_losing_local(self) -> None:
        con = sqlite3.connect(self.fixture.database)
        try:
            placeholders = ",".join("?" for _ in TOMBSTONE_SKILLS)
            con.execute(
                "DELETE FROM shell_skills WHERE skill_id IN "
                f"(SELECT skill_id FROM skills WHERE name IN ({placeholders}))",
                TOMBSTONE_SKILLS,
            )
            con.execute(
                "DELETE FROM flavor_skills WHERE skill_id IN "
                f"(SELECT skill_id FROM skills WHERE name IN ({placeholders}))",
                TOMBSTONE_SKILLS,
            )
            con.execute(
                f"DELETE FROM skills WHERE name IN ({placeholders})",
                TOMBSTONE_SKILLS,
            )
            con.commit()

            stale = self.fixture.snapshot.read_text()
            con.executescript(stale)
            con.executescript(stale)
            restored = [
                row[0]
                for row in con.execute(
                    f"SELECT name FROM skills WHERE name IN ({placeholders}) "
                    "ORDER BY name",
                    TOMBSTONE_SKILLS,
                )
            ]
            local = con.execute(
                "SELECT content FROM skills WHERE name=?", (LOCAL_SKILL_NAME,)
            ).fetchone()[0]
            violations = con.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            con.close()

        self.assertEqual(restored, sorted(TOMBSTONE_SKILLS))
        self.assertEqual(local.encode(), self.fixture.expected_local_content)
        self.assertEqual(violations, [])

    def test_pin_and_checkout_shape_match_the_pre_feature_downstream(self) -> None:
        self.assertEqual(
            (self.fixture.root / ".sc-state" / "engine.ref").read_text(),
            BASELINE_SHA + "\n",
        )
        con = sqlite3.connect(self.fixture.database)
        try:
            applied = [
                row[0]
                for row in con.execute(
                    "SELECT filename FROM schema_migrations ORDER BY filename"
                )
            ]
        finally:
            con.close()
        self.assertEqual(applied[-1], BASELINE_LAST_MIGRATION)
        self.assertTrue(self.fixture.dormant_worktree.joinpath(".git").is_file())
        registered = subprocess.run(
            ["git", "-C", str(self.fixture.root), "worktree", "list", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn(str(self.fixture.dormant_worktree), registered)
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(self.fixture.root), "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout,
            "",
        )
        self.assertEqual(
            self.fixture.root.joinpath("host-owned.txt").read_text(),
            "dos-arch host content\n",
        )

    def test_disk_carries_root_and_dormant_stale_projections_with_controls(self) -> None:
        self.assertEqual(
            len(self.fixture.native_skill_roots),
            len(self.fixture.checkouts) * len(NATIVE_SKILL_DIRS),
        )
        for skills_root in self.fixture.native_skill_roots:
            for name in (*TOMBSTONE_SKILLS, LOCAL_SKILL_NAME):
                body = skills_root.joinpath(name, "SKILL.md").read_text()
                self.assertIn(f"name: {name}\n", body)

        for legacy_root, control in zip(
            self.fixture.legacy_skill_roots, self.fixture.control_files
        ):
            for name in (*TOMBSTONE_SKILLS, LOCAL_SKILL_NAME):
                self.assertIn(
                    "rendered_by: super-coder",
                    legacy_root.joinpath(f"{name}.md").read_text(),
                )
            self.assertEqual(control.read_bytes(), self.fixture.expected_control_file)
            self.assertNotIn(b"rendered_by: super-coder", control.read_bytes())

        for name in (*TOMBSTONE_SKILLS, LOCAL_SKILL_NAME):
            self.assertTrue(self.fixture.catalogue_root.joinpath(f"{name}.md").is_file())
        self.assertEqual(
            self.fixture.local_asset.read_bytes(), self.fixture.expected_local_asset
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
