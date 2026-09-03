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
MIGRATIONS = (
    ENGINE / "migrations" / "0225_focused_dev_verification.sql",
    ENGINE / "migrations" / "0231_suppress_aborted_sprint_pr_wakes.sql",
    ENGINE / "migrations" / "0233_reseed_sprint_aware_pr_notifications.sql",
    ENGINE / "migrations" / "0234_reseed_ci_fallback_authority.sql",
    ENGINE / "migrations" / "0240_reseed_sprint_spec_rebinding.sql",
    ENGINE / "migrations" / "0248_reseed_sprint_pln_orientation.sql",
)
sys.path.insert(0, str(ENGINE / "scripts"))

import seed_skills

POLICY_HEADING = "## TESTING POSTURE"
FULL_SUITE_BOUNDARY = "do not run the repository-wide suite locally merely to duplicate"
SHARED_HOST_BOUNDARY = "Never start a competing repository-wide suite on a shared host."


class FocusedDeveloperVerificationSourceTest(unittest.TestCase):
    def test_fresh_developer_template_carries_the_complete_boundary(self):
        template = json.loads(
            (ENGINE / "templates" / "shells" / "dev.json").read_text()
        )
        focus = template["focus"]

        self.assertEqual(focus.count(POLICY_HEADING), 1)
        self.assertIn("every available smallest affected test target", focus)
        self.assertIn("Complete the implementation before using CI fallback", focus)
        self.assertIn(
            "Required checks pending -> wait; red -> diagnose, fix, and push; "
            "green -> review readiness",
            focus,
        )
        self.assertIn("incomplete code is a failure", focus)
        self.assertIn("no trustworthy seat remains", focus)
        self.assertIn("browser-capability skip is informational and non-failing", focus)
        self.assertIn(FULL_SUITE_BOUNDARY, focus)
        self.assertIn(SHARED_HOST_BOUNDARY, focus)

    def test_role_guidance_keeps_ci_fallback_and_authority_boundaries(self):
        bodies = {
            name: (ASSETS / name / "SKILL.md").read_text()
            for name in ("spec", "sprint_dev", "sprint_pln")
        }
        bodies["dev_kit"] = (
            ENGINE / "assets" / "seed" / "skills" / "dev_kit" / "SKILL.md"
        ).read_text()

        for name in ("spec", "sprint_dev"):
            with self.subTest(skill=name):
                normalized = " ".join(bodies[name].split())
                self.assertIn("boot `TESTING POSTURE`", normalized)

        normalized = {name: " ".join(body.split()) for name, body in bodies.items()}
        self.assertIn("available focused proof", normalized["spec"])
        self.assertIn("observed registered-PR checks", normalized["spec"])
        self.assertIn("pending -> wait", normalized["spec"])
        self.assertIn("red -> fix", normalized["spec"])
        self.assertIn("green -> review", normalized["spec"])
        self.assertIn("optional browser skip is non-failing", normalized["spec"])
        self.assertNotIn("runs focused/full gates", normalized["spec"])

        self.assertIn(
            "Register complete code even when a local gate is unavailable",
            normalized["sprint_dev"],
        )
        for state in (
            "pending ->",
            "red ->",
            "green ->",
            "none or untrustworthy watcher",
        ):
            self.assertIn(state, normalized["sprint_dev"])
        self.assertIn("incomplete code = failure", normalized["sprint_dev"])
        self.assertIn("Optional browser skip = non-failing", normalized["sprint_dev"])

        self.assertIn("## Canonical states", bodies["dev_kit"])
        self.assertIn("`deps`, `test`, `lint`, and `typecheck`", normalized["dev_kit"])
        self.assertIn("Host hooks use the host checkout", normalized["dev_kit"])
        self.assertIn("Container hooks use the", normalized["dev_kit"])

        self.assertIn("pending wait", normalized["sprint_pln"])
        self.assertIn("red fix", normalized["sprint_pln"])
        self.assertIn("green review", normalized["sprint_pln"])
        self.assertIn(
            "Planner NEVER mutates packages/toolchains", normalized["sprint_pln"]
        )
        self.assertIn(
            "No checks/untrustworthy watcher after one read -> blocker",
            normalized["sprint_pln"],
        )


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
                ('sprint_pln', 'drift', 'drift', 'drift', 1, 'drift', 1),
                ('dev_kit', 'drift', 'drift', 'drift', 1, 'drift', 1),
                ('fork_local', 'local', 'fork', NULL, 0, 'preserve me', 0);
            """
        )

    def tearDown(self):
        self.con.close()

    def test_migration_converges_prompts_and_skills_idempotently(self):
        original_planner = self.con.execute(
            "SELECT system_prompt FROM shells WHERE shell_id=3"
        ).fetchone()[0]
        migration = "\n".join(path.read_text() for path in MIGRATIONS)

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
        source_focus = json.loads(
            (ENGINE / "templates" / "shells" / "dev.json").read_text()
        )["focus"]
        source_policy = self._testing_posture(source_focus)
        self.assertEqual(self._testing_posture(first_prompts[1]), source_policy)
        self.assertEqual(self._testing_posture(first_prompts[2]), source_policy)

        paths = {
            name: ASSETS / name / "SKILL.md"
            for name in ("sprint_dev", "sprint_pln")
        }
        for name, path in paths.items():
            expected = seed_skills.parse_skill(path)
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
                "FROM skills WHERE name IN "
                "('agents','spec','sprint_dev','sprint_pln','dev_kit') "
                "ORDER BY name"
            ).fetchall()
        }

    @staticmethod
    def _testing_posture(text: str) -> str:
        policy = text.split(POLICY_HEADING, 1)[1].lstrip("\n")
        return policy.split("\n\n## CODE CRAFT", 1)[0].rstrip()


if __name__ == "__main__":
    unittest.main()
