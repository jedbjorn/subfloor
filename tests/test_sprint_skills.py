"""Stage 9 gates for the five Sprints v2 engine skills."""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
ASSETS = ENGINE / "assets" / "skills"
sys.path.insert(0, str(ENGINE / "scripts"))

import seed_skills  # noqa: E402
import sprint_cli  # noqa: E402

SKILLS = {
    "sprint_prep": "planner",
    "sprint_pln": "planner",
    "sprint_dev": "dev",
    "sprint_rev": "reviewer",
    "sprint_close": "planner",
}


class SprintSkillTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.con = sqlite3.connect(":memory:")
        cls.con.row_factory = sqlite3.Row
        cls.con.executescript((ENGINE / "schema.sql").read_text())
        for migration in sorted((ENGINE / "migrations").glob("*.sql")):
            cls.con.executescript(migration.read_text())

    @classmethod
    def tearDownClass(cls):
        cls.con.close()

    def test_catalogue_bodies_match_assets_and_role_grants_are_exact(self):
        for name, flavor in SKILLS.items():
            with self.subTest(name=name):
                parsed = seed_skills.parse_skill(ASSETS / name / "SKILL.md")
                row = self.con.execute(
                    "SELECT description,category,command,common,content,is_deleted "
                    "FROM skills WHERE name=?",
                    (name,),
                ).fetchone()
                self.assertEqual(
                    (
                        parsed["description"],
                        parsed["category"],
                        parsed["command"],
                        parsed["common"],
                        parsed["content"],
                        0,
                    ),
                    tuple(row),
                )
                grants = [
                    grant[0]
                    for grant in self.con.execute(
                        "SELECT fs.flavor FROM flavor_skills fs "
                        "JOIN skills s ON s.skill_id=fs.skill_id "
                        "WHERE s.name=? ORDER BY fs.flavor",
                        (name,),
                    )
                ]
                self.assertEqual([flavor], grants)

    def test_reviewer_skill_owns_severity_and_conformance_never_fixes(self):
        reviewer = (ASSETS / "sprint_rev" / "SKILL.md").read_text()
        self.assertIn("## Severity rubric", reviewer)
        for severity in ("Critical", "Major", "Medium", "Low"):
            self.assertIn(f"**{severity}**", reviewer)
        self.assertIn("none is fixed inside the Sprint", reviewer)
        for name in SKILLS.keys() - {"sprint_rev"}:
            self.assertNotIn(
                "## Severity rubric", (ASSETS / name / "SKILL.md").read_text()
            )

    def test_skills_use_only_the_shipped_shell_command_surface(self):
        expected = {
            "record-qaqc",
            "declare",
            "plan-unit",
            "replan-unit",
            "arm",
            "inbox",
            "send",
            "accept",
            "decline",
            "complete-unit",
            "cancel-unit",
            "register-pr",
            "pause",
            "resume",
            "complete",
            "abort",
            "request-review",
            "record-review",
            "authorize-merge",
            "dispatch",
            "monitor",
            "record-conformance",
            "disposition-followup",
            "compile-report",
        }
        combined = "\n".join(
            (ASSETS / name / "SKILL.md").read_text() for name in SKILLS
        )
        for command in expected:
            self.assertIn(f"sc sprint {command}", combined)
        dispatcher = (ROOT / "sc").read_text()
        self.assertIn('sprint)       exec "$PY" "$S/sprint_cli.py" "$@" ;;', dispatcher)
        parser = sprint_cli.build_parser()
        commands = next(
            action
            for action in parser._actions
            if isinstance(action, sprint_cli.argparse._SubParsersAction)
        ).choices
        self.assertEqual(expected, set(commands))

    def test_role_skills_cover_every_handoff_contingency_with_real_commands(self):
        role_skills = {"sprint_pln", "sprint_dev", "sprint_rev", "sprint_close"}
        for name in role_skills:
            with self.subTest(name=name):
                body = (ASSETS / name / "SKILL.md").read_text()
                for command in (
                    "sc sprint inbox --sprint <id>",
                    "sc sprint accept --sprint <id> --message <message-id>",
                    "sc sprint decline --sprint <id> --message <message-id>",
                    "sc sprint send --sprint <id> --to <shortname> --body-file <path>",
                ):
                    self.assertIn(command, body)
                normalized = " ".join(body.lower().split())
                for guidance in (
                    "on every wake" if name != "sprint_close" else "on entry or any wake",
                    "incoming question",
                    "blocker",
                    "decision boundary",
                    "duplicate",
                    "command is rejected or transport fails",
                    "6,000 characters",
                    "8,000",
                    "wc -m < <path>",
                    "command exits successfully",
                    "informational message",
                    "marks the message read",
                    "does not change sprint or work-unit state",
                    "re-run `sc sprint inbox --sprint <id>`",
                ):
                    self.assertIn(guidance, normalized)
                self.assertEqual(
                    1,
                    body.count(
                        "sc sprint send --sprint <id> --to <shortname> "
                        "--body-file <path>"
                    ),
                )
                for invented in ("ASK:", "ANSWER:", "BLOCKED:"):
                    self.assertNotIn(invented, body)

    def test_pause_guidance_is_planner_owned_and_workers_report_before_stopping(self):
        pause = "sc sprint pause --sprint <id> --reason <integrity-threat>"
        for name in ("sprint_dev", "sprint_rev"):
            with self.subTest(worker=name):
                body = (ASSETS / name / "SKILL.md").read_text()
                normalized = " ".join(body.lower().split())
                self.assertNotIn(pause, body)
                self.assertIn("evidence, impact", normalized)
                self.assertIn("recommendation", normalized)
                self.assertIn("does not pause the sprint", normalized)
                self.assertIn("planner decides", normalized)
                self.assertIn("relay itself fails", normalized)
                self.assertIn("fnb", normalized)

        for name in ("sprint_pln", "sprint_close"):
            with self.subTest(planner=name):
                body = (ASSETS / name / "SKILL.md").read_text()
                normalized = " ".join(body.lower().split())
                self.assertIn(pause, body)
                self.assertIn("planner or fnb decides", normalized)
                self.assertIn("relay itself fails", normalized)
                self.assertIn("active relay is not available after", normalized)
                self.assertIn("exhausted recovery wake", normalized)
                self.assertIn("do not create recursive", normalized)

    def test_every_affected_file_argument_names_the_hard_ceiling(self):
        parser = sprint_cli.build_parser()
        commands = next(
            action
            for action in parser._actions
            if isinstance(action, sprint_cli.argparse._SubParsersAction)
        ).choices
        for command, arguments in {
            "send": ("--body-file",),
            "complete-unit": ("--result-file",),
            "request-review": ("--readiness-file",),
            "record-review": ("--body-file",),
            "record-conformance": ("--body-file", "--findings-file"),
            "disposition-followup": ("--resolution-file",),
            "complete": ("--report-file",),
        }.items():
            with self.subTest(command=command):
                help_text = commands[command].format_help()
                for argument in arguments:
                    self.assertIn(argument, help_text)
                self.assertIn("8,000 characters", help_text)
