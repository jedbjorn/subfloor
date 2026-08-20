"""Focused local Developer verification and CI-owned full-suite posture."""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
ASSETS = ENGINE / "assets" / "skills"
MIGRATION = ENGINE / "migrations" / "0225_focused_dev_verification.sql"
sys.path.insert(0, str(ENGINE / "scripts"))

import seed_skills  # noqa: E402


POLICY_HEADING = "## TESTING POSTURE"
FULL_SUITE_BOUNDARY = (
    "do not run the repository-wide suite locally merely to duplicate"
)
SHARED_HOST_BOUNDARY = (
    "Never start a competing repository-wide suite on a shared host."
)


class FocusedDeveloperVerificationSourceTest(unittest.TestCase):
    def test_fresh_developer_template_carries_the_complete_boundary(self):
        template = json.loads(
            (ENGINE / "templates" / "shells" / "dev.json").read_text()
        )
        focus = template["focus"]

        self.assertEqual(focus.count(POLICY_HEADING), 1)
        self.assertIn("smallest affected test targets", focus)
        self.assertIn(FULL_SUITE_BOUNDARY, focus)
        self.assertIn("required CI checks are green", focus)
        self.assertIn(SHARED_HOST_BOUNDARY, focus)

    def test_later_loaded_developer_skills_keep_the_same_boundary(self):
        bodies = {
            name: (ASSETS / name / "SKILL.md").read_text()
            for name in ("agents", "spec", "sprint_dev")
        }

        for name, body in bodies.items():
            with self.subTest(skill=name):
                normalized = " ".join(body.split())
                self.assertIn("boot `TESTING POSTURE`", normalized)

        normalized = {
            name: " ".join(body.split()) for name, body in bodies.items()
        }
        self.assertIn("focused local proof", normalized["spec"])
        self.assertIn("green configured CI", normalized["spec"])
        self.assertNotIn("runs focused/full gates", normalized["spec"])
        self.assertIn("never use bare `sc test`", normalized["agents"])
        self.assertIn("smallest affected gate", normalized["sprint_dev"])
        self.assertIn(
            "configured CI green = full-suite proof", normalized["sprint_dev"]
        )
        self.assertIn("red -> diagnose/fix/push/rerun", normalized["sprint_dev"])


class FocusedDeveloperVerificationMigrationTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.executescript(
            """
            CREATE TABLE shells (
                shell_id INTEGER PRIMARY KEY,
                flavor TEXT,
                system_prompt TEXT NOT NULL
            );
            CREATE TABLE skills (
                skill_id INTEGER PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                description TEXT,
                category TEXT,
                command TEXT,
                common INTEGER NOT NULL DEFAULT 1,
                content TEXT,
                is_deleted INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO shells (shell_id, flavor, system_prompt) VALUES
                (1, 'dev', 'Builder intro\n\n## CODE CRAFT\n\nCraft body.'),
                (2, 'dev', 'Legacy developer prompt without craft heading.'),
                (3, 'planner', 'Planner prompt\n\n## CODE CRAFT\n\nUnchanged.');
            INSERT INTO skills
                (name, description, category, command, common, content, is_deleted)
            VALUES
                ('agents', 'drift', 'drift', 'drift', 1, 'drift', 1),
                ('spec', 'drift', 'drift', 'drift', 1, 'drift', 1),
                ('sprint_dev', 'drift', 'drift', 'drift', 1, 'drift', 1),
                ('fork_local', 'local', 'fork', NULL, 0, 'preserve me', 0);
            """
        )

    def tearDown(self):
        self.con.close()

    def test_migration_converges_prompts_and_skills_idempotently(self):
        original_planner = self.con.execute(
            "SELECT system_prompt FROM shells WHERE shell_id=3"
        ).fetchone()[0]
        migration = MIGRATION.read_text()

        self.con.executescript(migration)
        first_prompts = dict(
            self.con.execute(
                "SELECT shell_id, system_prompt FROM shells ORDER BY shell_id"
            ).fetchall()
        )
        first_skills = self._managed_skill_rows()
        self.con.executescript(migration)

        replayed_prompts = dict(
            self.con.execute(
                "SELECT shell_id, system_prompt FROM shells ORDER BY shell_id"
            ).fetchall()
        )
        self.assertEqual(replayed_prompts, first_prompts)
        self.assertEqual(self._managed_skill_rows(), first_skills)
        self.assertEqual(first_prompts[1].count(POLICY_HEADING), 1)
        self.assertLess(
            first_prompts[1].index(POLICY_HEADING),
            first_prompts[1].index("## CODE CRAFT"),
        )
        self.assertEqual(first_prompts[2].count(POLICY_HEADING), 1)
        self.assertEqual(first_prompts[3], original_planner)

        for name in ("agents", "spec", "sprint_dev"):
            expected = seed_skills.parse_skill(ASSETS / name / "SKILL.md")
            self.assertEqual(
                first_skills[name],
                (
                    expected["description"],
                    expected["category"],
                    expected["command"],
                    expected["common"],
                    expected["content"],
                    0,
                ),
            )

        self.assertEqual(
            self.con.execute(
                "SELECT description,category,command,common,content,is_deleted "
                "FROM skills WHERE name='fork_local'"
            ).fetchone(),
            ("local", "fork", None, 0, "preserve me", 0),
        )

    def _managed_skill_rows(self):
        return {
            row[0]: row[1:]
            for row in self.con.execute(
                "SELECT name,description,category,command,common,content,is_deleted "
                "FROM skills WHERE name IN ('agents','spec','sprint_dev') "
                "ORDER BY name"
            ).fetchall()
        }


if __name__ == "__main__":
    unittest.main()
