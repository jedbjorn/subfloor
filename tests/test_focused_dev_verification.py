"""Focused local Developer verification and CI-owned full-suite posture."""

from __future__ import annotations

import sqlite3
import json
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
    ENGINE / "migrations" / "0252_reseed_universal_pr_owner_wakes.sql",
    ENGINE / "migrations" / "0253_reseed_sprint_review_flexibility.sql",
    ENGINE / "migrations" / "0254_reseed_task_context_projection.sql",
    ENGINE / "migrations" / "0255_reseed_merge_gate_one_rule.sql",
    ENGINE / "migrations" / "0257_guidance_reconciliation.sql",
    ENGINE / "migrations" / "0258_reseed_non_sprint_green_wake.sql",
    ENGINE / "migrations" / "0260_reseed_sprint_closeout_scope.sql",
    ENGINE / "migrations" / "0261_dev_long_job_guidance.sql",
    ENGINE / "migrations" / "0262_reseed_sprint_resume_reentry.sql",
    ENGINE / "migrations" / "0280_remove_sprint_followups.sql",
    ENGINE / "migrations" / "0282_skills_dedupe_posture.sql",
    ENGINE / "migrations" / "0284_reseed_sprint_consistency.sql",
)
sys.path.insert(0, str(ENGINE / "scripts"))

import seed_skills

POLICY_HEADING = "## TESTING POSTURE"
FULL_SUITE_BOUNDARY = "do not run the repository-wide suite locally merely to duplicate"
SHARED_HOST_BOUNDARY = "Never start a competing repository-wide suite on a shared host."
LONG_JOB_BOUNDARY = "Use `./sc job start --label gate -- <command and args>`"


class FocusedDeveloperVerificationSourceTest(unittest.TestCase):
    def test_fresh_developer_template_carries_the_complete_boundary(self):
        # F72: the posture lives in the dev flavor's procedure body, rendered
        # into the system prompt after the JSON focus.
        # The boot keeps the boundary every Developer turn needs and names
        # `spec_execution`; the long-job, CI-fallback and check-state rules
        # live in that skill.
        focus = " ".join(
            (ENGINE / "templates" / "shells" / "dev.md").read_text().split()
        )

        self.assertEqual(focus.count(POLICY_HEADING), 1)
        self.assertIn("every available smallest affected test target", focus)
        self.assertIn("incomplete code is a failure", focus)
        self.assertIn("a lost outcome is unknown, never a pass", focus)
        self.assertIn(FULL_SUITE_BOUNDARY, focus)
        self.assertIn(SHARED_HOST_BOUNDARY, focus)
        posture = focus.split(POLICY_HEADING, 1)[1].split("## ", 1)[0]
        self.assertIn("`spec_execution`", posture)
        # Spec #276: seat-specific waiting lives in the boot SEAT section.
        self.assertIn("Wait on a long check the way your SEAT section says.", posture)
        probes = focus.split("## BOUNDED PROBES", 1)[1].split("## ", 1)[0]
        self.assertIn("Its completion wakes you;", probes)
        self.assertIn(
            "the PR watcher sends the shell that opened a PR its red, green, "
            "closed and merged facts",
            probes,
        )
        self.assertIn("do not replace those with a polling job.", probes)
        self.assertIn("`sc job watch pr --pr <n>` is one bounded watch", probes)
        self.assertIn("`sc job watch timer --after <seconds>`", probes)
        self.assertIn("it grants no merge authority", probes)
        self.assertIn(
            "How these wakes reach you, and when you watch for yourself instead: SEAT.",
            probes,
        )
        for split in ("headless seat", "terminal seat"):
            self.assertNotIn(split, focus.lower())
        # Spec #273: a shell never loops on `gh`; that rule now rides SEAT — GUI.
        seat = " ".join((ENGINE / "scripts" / "run_seat.py").read_text().split())
        self.assertIn("Never loop on `gh` in your turn.", seat)

        skill = ASSETS / "spec_execution" / "SKILL.md"
        detail = " ".join(skill.read_text().split())
        self.assertIn("Complete the implementation before using CI fallback", detail)
        self.assertIn(
            "Required checks pending -> wait; red -> diagnose, fix, and push; "
            "green -> review readiness",
            detail,
        )
        self.assertIn("no trustworthy seat remains", detail)
        self.assertIn(
            "browser-capability skip is informational and non-failing", detail
        )
        self.assertIn(LONG_JOB_BOUNDARY, detail)
        self.assertIn("A lost outcome is unknown, never a pass.", detail)

    def test_role_guidance_keeps_ci_fallback_and_authority_boundaries(self):
        bodies = {
            name: (ASSETS / name / "SKILL.md").read_text()
            for name in ("sprint_dev", "sprint_pln")
        }
        bodies["dev_kit"] = (
            ENGINE / "assets" / "seed" / "skills" / "dev_kit" / "SKILL.md"
        ).read_text()
        # F72: the spec execution loop lives in the dev flavor body.
        bodies["dev"] = (ENGINE / "templates" / "shells" / "dev.md").read_text()

        normalized = {name: " ".join(body.split()) for name, body in bodies.items()}
        dev_pack = json.loads(
            (ENGINE / "templates" / "shells" / "dev.json").read_text())
        planner_pack = json.loads(
            (ENGINE / "templates" / "shells" / "planner.json").read_text())
        self.assertIn("TESTING POSTURE", normalized["sprint_dev"])
        self.assertIn("Verification follows TESTING POSTURE", normalized["dev"])
        self.assertIn("Verification", normalized["dev"])

        self.assertIn(
            "Register complete code even when a local gate is unavailable",
            normalized["sprint_dev"],
        )
        # The full pending/red/green rule and the failure definitions have one
        # home, `spec_execution` (granted to the Developer pack); sprint_dev
        # keeps only the Sprint-specific actions and points there.
        self.assertIn("spec_execution", dev_pack["skills"])
        self.assertIn("TESTING POSTURE's (`spec_execution`)", normalized["sprint_dev"])
        for state in ("pending -> stop for the native wake",
                      "green -> judge, then request review",
                      "blocked gate -> report it to the Planner as a blocker"):
            self.assertIn(state, normalized["sprint_dev"])
        for moved in ("incomplete code = failure",
                      "Optional browser skip = non-failing"):
            self.assertNotIn(moved, normalized["sprint_dev"])
        posture = " ".join(
            (ASSETS / "spec_execution" / "SKILL.md").read_text().split())
        for rule in (
            "Required checks pending -> wait; red -> diagnose, fix, and push; "
            "green -> review readiness.",
            "red CI result, or incomplete code is a failure",
            "No configured checks or an untrustworthy watcher after one bounded "
            "read -> block",
            "browser-capability skip is informational and non-failing",
        ):
            self.assertIn(rule, posture)
        # The Planner pack has no `spec_execution`, so sprint_pln keeps its own
        # statement (asserted below).
        self.assertNotIn("spec_execution", planner_pack["skills"])

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
            CREATE TABLE shell_skills (shell_id INTEGER, skill_id INTEGER);
            CREATE TABLE flavor_skills (
                flavor TEXT, skill_id INTEGER, PRIMARY KEY (flavor, skill_id)
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
        # This fixture contains only guidance tables. The Runs integration
        # suite applies the complete ledger migration; include its prompt
        # statement here to prove convergence with the current template.
        runs_migration = (ENGINE / "migrations" / "0275_runs_ledger.sql").read_text()
        guidance = [
            line for line in runs_migration.splitlines()
            if line.startswith("UPDATE shells SET system_prompt=")
        ]
        self.assertEqual(len(guidance), 1)
        migration += "\n" + guidance[0]

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
        # These migrations wrote the full posture into every Developer prompt.
        # The template has since shortened it to a trigger for `spec_execution`
        # and `refresh_standard_prompts` converges live rows on the template,
        # so prove here that both migrated rows agree and carry the boundary.
        migrated_policy = self._testing_posture(first_prompts[1])
        self.assertEqual(self._testing_posture(first_prompts[2]), migrated_policy)
        for boundary in (FULL_SUITE_BOUNDARY, SHARED_HOST_BOUNDARY, LONG_JOB_BOUNDARY):
            self.assertIn(boundary, migrated_policy)
        source_focus = (ENGINE / "templates" / "shells" / "dev.md").read_text()
        self.assertEqual(source_focus.count(POLICY_HEADING), 1)

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
        # Compare this section only; neighboring Developer sections can grow.
        return policy.split("\n\n## ", 1)[0].rstrip()


if __name__ == "__main__":
    unittest.main()
