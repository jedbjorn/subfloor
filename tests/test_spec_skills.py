"""The spec skills moved out of boot, and the single-home rules of the 0282
skills pass: one receipt block, a source-repo branch where fork wording
differed, and the cli_orchestration merge directive as boot's first form."""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
ASSETS = ENGINE / "assets" / "skills"
PACKS = ENGINE / "templates" / "shells"
RESEED = ENGINE / "migrations" / "0282_skills_dedupe_posture.sql"
sys.path.insert(0, str(ENGINE / "scripts"))

import seed_skills

SPEC_SKILLS = {"spec_authoring": "planner", "spec_execution": "dev"}


def summary(description: str) -> str:
    """The boot listing's cut of a description (compose._skill_summary)."""
    line = description.strip().splitlines()[0]
    for stop in (". ", " — "):
        head, found, _ = line.partition(stop)
        if found:
            line = head
    return line.rstrip(".")


def flat(name: str) -> str:
    return " ".join((ASSETS / name / "SKILL.md").read_text().split())


class SpecSkillTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.con = sqlite3.connect(":memory:")
        cls.con.executescript((ENGINE / "schema.sql").read_text())
        for migration in sorted((ENGINE / "migrations").glob("*.sql")):
            cls.con.executescript(migration.read_text())

    @classmethod
    def tearDownClass(cls):
        cls.con.close()

    def test_fresh_build_seeds_both_skills_and_grants_only_their_pack(self):
        for name, flavor in SPEC_SKILLS.items():
            with self.subTest(name=name):
                parsed = seed_skills.parse_skill(ASSETS / name / "SKILL.md")
                self.assertEqual((parsed["category"], parsed["common"]), ("workflow", 0))
                row = self.con.execute(
                    "SELECT description,category,command,common,content,is_deleted "
                    "FROM skills WHERE name=?", (name,)).fetchone()
                self.assertEqual(row, (
                    parsed["description"], parsed["category"], parsed["command"],
                    parsed["common"], parsed["content"], 0))
                granted = {r[0] for r in self.con.execute(
                    "SELECT fs.flavor FROM flavor_skills fs JOIN skills s "
                    "ON s.skill_id=fs.skill_id WHERE s.name=?", (name,))}
                self.assertEqual(granted, {flavor})
                pack = json.loads((PACKS / f"{flavor}.json").read_text())
                self.assertIn(name, pack["skills"])
                self.assertIn("load before", summary(parsed["description"]))

    def test_reseed_replays_idempotently(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(
            "CREATE TABLE skills (skill_id INTEGER PRIMARY KEY, name TEXT UNIQUE, "
            "description TEXT, category TEXT, command TEXT, common INTEGER, "
            "content TEXT, is_deleted INTEGER DEFAULT 0);"
            "CREATE TABLE flavor_skills (flavor TEXT, skill_id INTEGER, "
            "PRIMARY KEY(flavor, skill_id));"
            "INSERT INTO skills VALUES (7,'dev_kit','fork','fork',NULL,0,'fork body',0);"
        )
        sql = RESEED.read_text()
        con.executescript(sql)
        first = con.execute("SELECT * FROM skills ORDER BY name").fetchall()
        grants = con.execute("SELECT * FROM flavor_skills ORDER BY 1,2").fetchall()
        con.executescript(sql)
        self.assertEqual(first, con.execute("SELECT * FROM skills ORDER BY name").fetchall())
        self.assertEqual(
            grants, con.execute("SELECT * FROM flavor_skills ORDER BY 1,2").fetchall())
        self.assertEqual(len(grants), 2)
        # A fork-customized dev_kit is never rewritten.
        self.assertEqual(con.execute(
            "SELECT description, content FROM skills WHERE name='dev_kit'").fetchone(),
            ("fork", "fork body"))

    def test_spec_authoring_carries_the_planner_spec_rules(self):
        body = flat("spec_authoring")
        for needed in (
            "## Features, specs, and docs", "## Posture, then challenge",
            "## Acceptance feasibility", "## The spec contract",
            "## Revise, freeze, document",
            "never author a loose `.md` as the canonical copy",
            "sc mem roadmap add", "sc mem roadmap project", "sc mem project add",
            "never silently re-decide",
            "Documentation and code disagree -> surface both to the FnB",
            "an unconfirmed suggestion is never promoted to a requirement",
            "a named seat, a named action, an observable result",
            "**Can we meet it with available resources?**",
            "**What evidence is worth obtaining?**",
            "**Is post-implementation verification practical in Dev?**",
            "**Can the FnB supply the missing proof?**",
            "**Is a privileged process still justified?**",
            "Never label inference as proof",
            "Do not keep adding code to chase an acceptance claim",
            "`## Current Posture`", "`## Anticipated User Activity`",
            '"not in this delivery", never "never"',
            "Unexpected Participant", "never threat model",
            "Internal-only features still carry the section",
            "sc mem doc add", "--kind spec", "the `themed_markdown` skill",
            "sc mem doc edit", "sc mem doc move", "sc mem doc freeze",
            "read the shipped code, not the spec", "--kind doc",
            "sc mem doc retire <id> [--superseded-by <id>]",
        ):
            with self.subTest(needed=needed):
                self.assertIn(needed, body)

    def test_spec_execution_carries_the_developer_spec_and_testing_rules(self):
        body = flat("spec_execution")
        for needed in (
            "sc context --task <id>", "sc context --work-unit <id>",
            "Never auto-pick the latest document",
            "resume the first unfinished one",
            "a mismatch stops for the Planner/FnB",
            "every Out of Scope item stays out",
            "open one High flag on the feature and stop at its boundary",
            "sc mem roadmap status <feature_id> in_progress",
            "`Preparation` at seq 0", "`Verification` last",
            "No task plan, no build", "Unspec'd quick fixes",
            "sc mem task start <id>", "sc mem task done <id>",
            "sc mem task cancel <id>",
            "never absorb it",
            "unexpected reach, weakened hardening, or crossed tenancy fails",
            "sc mem roadmap status <feature_id> shipped",
            "[Docs] <feature> shipped, doc pending",
            "Never freeze or author the shipped doc yourself",
            "## TESTING POSTURE",
            "every available smallest affected test target",
            # Commands are carried from the Developer template exactly.
            "Use `./sc job start --label gate -- <command and args>` for checks "
            "that may outlive the current tool call or session.",
            "`./sc job status <id>` and `./sc job tail <id>`",
            "A lost outcome is unknown, never a pass.",
            # How to wait is the seat's rule, held only in the boot SEAT section.
            "then wait on it the way your boot SEAT section says",
            "Complete the implementation before using CI fallback",
            "pending -> wait; red -> diagnose, fix, and push; green -> review readiness",
            "incomplete code is a failure",
            "no trustworthy seat remains",
            "browser-capability skip is informational and non-failing",
            "do not run the repository-wide suite locally merely to duplicate",
            "Never start a competing repository-wide suite on a shared host.",
        ):
            with self.subTest(needed=needed):
                self.assertIn(needed, body)


class SingleHomeTest(unittest.TestCase):
    def test_run_receipt_rules_have_one_home_and_skills_point_at_it(self):
        dev_kit = " ".join(
            (ENGINE / "assets" / "seed" / "skills" / "dev_kit" / "SKILL.md")
            .read_text().split())
        # `git` reaches every pack that can attach or read a receipt, so it
        # carries the full rules; the Planner-only dev_kit starter points at it.
        git = flat("git")
        for rule in (
            "## RUN RECEIPTS",
            "sc job attach <id> --pr <number>",
            "The run and target must share an owner",
            "a PR must already be registered",
            "A commit outside the PR head's ancestry is stale",
            "Pruned logs retain their receipt",
            "only GitHub checks and the existing merge grant authorize a merge",
        ):
            self.assertIn(rule, git)
        self.assertIn("sc job attach <id> --pr <number>|--work-unit <id>", dev_kit)
        self.assertIn("the RUN RECEIPTS section of the `git` skill", dev_kit)
        self.assertIn("never authorizes a merge", dev_kit)
        self.assertNotIn("Developer boot", dev_kit)
        self.assertNotIn("must share an owner", dev_kit)

    def test_fork_worded_skills_carry_a_source_repo_branch(self):
        for name in ("git_cleanup", "self_update", "harness_readiness",
                     "issue_reporting", "curate"):
            with self.subTest(name=name):
                self.assertIn("In the Subfloor source repo", flat(name))
        self.assertNotIn(
            "NEVER touch the engine — `.super-coder/` is a gitignored",
            flat("git_cleanup"))

    def test_cli_orchestration_merge_is_the_first_gate_form(self):
        body = flat("cli_orchestration")
        self.assertIn("merge only on an explicit FnB directive that identifies the PR", body)
        self.assertIn(
            "that kind covers only that assignment's PRs, and only once the "
            "stated condition is met", body)
        self.assertNotIn("directive naming the PR", body)
        self.assertIn("not a standing grant or a third form", body)
        self.assertIn("report the PR as ready and wait", body)
        self.assertIn("## Monitors", body)
        self.assertIn("Set up your own monitors", body)

    def test_first_sentence_of_each_description_says_what_and_when(self):
        """The boot listing shows only the first sentence, cut at the first
        ". " or " — " and capped at 140 characters, so that sentence must
        carry the purpose and the trigger on its own."""
        specs = {
            name: seed_skills.parse_skill(ASSETS / name / "SKILL.md")["description"]
            for name in (
                "cli_orchestration", "curate", "git", "git_cleanup",
                "issue_reporting", "remote_seats", "self_update", "snapshot",
                "spec_authoring", "spec_execution", "sprint_protocol",
                "sprint_rev", "tailscale_diagnostics",
            )
        }
        specs["dev_kit"] = seed_skills.parse_skill(
            seed_skills.DEV_KIT_STARTER)["description"]
        for name, description in specs.items():
            with self.subTest(name=name):
                first = summary(description)
                self.assertEqual(first, description.split(". ", 1)[0].rstrip("."))
                self.assertNotIn(" — ", first)
                self.assertLessEqual(len(first), 140)
                self.assertRegex(
                    first, r"\b(load|run it|use) (before|when|on|it|after)\b|the moment")
        self.assertTrue(summary(specs["snapshot"]).startswith("Admin work that"))
        self.assertTrue(summary(specs["git_cleanup"]).startswith("Admin-only cleanup"))


if __name__ == "__main__":
    unittest.main()
