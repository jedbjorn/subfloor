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

import seed_skills
import sprint_cli

SKILLS = {
    "sprint_prep": "planner",
    "sprint_pln": "planner",
    "sprint_dev": "dev",
    "sprint_rev": "reviewer",
    "sprint_close": "planner",
}
RESEEDED_SKILLS = set(SKILLS) | {"db_map"}
AUTHORITY_SPLIT_SKILLS = {"sprint_pln", "sprint_rev"}
V21_ROLE_SKILLS = set(SKILLS)
HANDOFF_ROLE_SKILLS = {"sprint_dev", "sprint_rev", "sprint_pln"}
CLOSEOUT_ROLE_SKILLS = {"sprint_close", "sprint_dev", "sprint_pln", "sprint_rev"}

ARTIFACT_PATH_RULE = """## Sprint artifact paths

Sprint working artifacts (per-unit review notes, raw diffs, evidence packets,
report drafts, and Dev scratch proof) go to the gitignored
`shared/sprints/sprint-<n>/` directory. They are never committed, branched, or
PR'd in the work repo; a review-notes commit is a finding.

DB rows stay the durable record: judgments via `record-review`, report bodies in
`sprint_reports`, and decisions in the durable relay. Files in the Sprint
artifact directory are working material only."""


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

    def test_handoff_migration_converges_a_drifted_existing_skill_body(self):
        con = sqlite3.connect(":memory:")
        reference = sqlite3.connect(":memory:")
        try:
            for target in (con, reference):
                target.executescript((ENGINE / "schema.sql").read_text())
                for migration in sorted((ENGINE / "migrations").glob("*.sql")):
                    if migration.name >= "0153_harden_sprint_handoff_skills.sql":
                        break
                    target.executescript(migration.read_text())
            con.execute(
                "UPDATE skills SET content='fork-local drift' WHERE name='sprint_dev'"
            )

            migration = (
                ENGINE / "migrations" / "0153_harden_sprint_handoff_skills.sql"
            ).read_text()
            con.executescript(migration)
            reference.executescript(migration)

            self.assertEqual(
                reference.execute(
                    "SELECT content FROM skills WHERE name='sprint_dev'"
                ).fetchone()[0],
                con.execute(
                    "SELECT content FROM skills WHERE name='sprint_dev'"
                ).fetchone()[0],
            )
        finally:
            con.close()
            reference.close()

    def test_native_wake_reseed_converges_dirty_rows_and_replays_idempotently(self):
        con = sqlite3.connect(":memory:")
        reference = sqlite3.connect(":memory:")
        try:
            for target in (con, reference):
                target.executescript((ENGINE / "schema.sql").read_text())
                for migration in sorted((ENGINE / "migrations").glob("*.sql")):
                    if migration.name >= "0159_reseed_sprint_native_wake_skills.sql":
                        break
                    target.executescript(migration.read_text())
            placeholders = ",".join("?" for _ in RESEEDED_SKILLS)
            con.execute(
                f"UPDATE skills SET content='stale pre-0159 body' "
                f"WHERE name IN ({placeholders})",
                tuple(sorted(RESEEDED_SKILLS)),
            )

            migration = (
                ENGINE / "migrations" / "0159_reseed_sprint_native_wake_skills.sql"
            ).read_text()
            con.executescript(migration)
            con.executescript(migration)
            reference.executescript(migration)

            for name in sorted(RESEEDED_SKILLS):
                with self.subTest(name=name):
                    rows = con.execute(
                        "SELECT content, is_deleted FROM skills WHERE name=?", (name,)
                    ).fetchall()
                    expected = reference.execute(
                        "SELECT content, is_deleted FROM skills WHERE name=?", (name,)
                    ).fetchone()
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(tuple(rows[0]), tuple(expected))
        finally:
            con.close()
            reference.close()

    def test_authority_split_reseed_converges_dirty_rows_and_replays_idempotently(self):
        con = sqlite3.connect(":memory:")
        try:
            con.executescript((ENGINE / "schema.sql").read_text())
            for migration in sorted((ENGINE / "migrations").glob("*.sql")):
                if migration.name >= "0167_reseed_sprint_authority_split.sql":
                    break
                con.executescript(migration.read_text())
            placeholders = ",".join("?" for _ in AUTHORITY_SPLIT_SKILLS)
            con.execute(
                f"UPDATE skills SET content='stale pre-0167 authority' "
                f"WHERE name IN ({placeholders})",
                tuple(sorted(AUTHORITY_SPLIT_SKILLS)),
            )

            migration = (
                ENGINE / "migrations" / "0167_reseed_sprint_authority_split.sql"
            ).read_text()
            con.executescript(migration)
            con.executescript(migration)

            for name in sorted(AUTHORITY_SPLIT_SKILLS):
                with self.subTest(name=name):
                    rows = con.execute(
                        "SELECT description,category,command,common,content,is_deleted "
                        "FROM skills WHERE name=?",
                        (name,),
                    ).fetchall()
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(0, rows[0][5])
                    self.assertIn("Reviewer decides", rows[0][4])
                    self.assertIn("Planner", rows[0][4])
        finally:
            con.close()

    def test_terminal_handoff_reseed_converges_dirty_rows_and_replays_idempotently(self):
        con = sqlite3.connect(":memory:")
        reference = sqlite3.connect(":memory:")
        try:
            for target in (con, reference):
                target.executescript((ENGINE / "schema.sql").read_text())
                for migration in sorted((ENGINE / "migrations").glob("*.sql")):
                    if migration.name >= "0170_reseed_sprint_handoff_order.sql":
                        break
                    target.executescript(migration.read_text())
            placeholders = ",".join("?" for _ in HANDOFF_ROLE_SKILLS)
            con.execute(
                f"UPDATE skills SET content='stale pre-0170 guidance' "
                f"WHERE name IN ({placeholders})",
                tuple(sorted(HANDOFF_ROLE_SKILLS)),
            )

            migration = (
                ENGINE / "migrations" / "0170_reseed_sprint_handoff_order.sql"
            ).read_text()
            con.executescript(migration)
            con.executescript(migration)
            reference.executescript(migration)

            for name in sorted(HANDOFF_ROLE_SKILLS):
                with self.subTest(name=name):
                    rows = con.execute(
                        "SELECT description,category,command,common,content,is_deleted "
                        "FROM skills WHERE name=?",
                        (name,),
                    ).fetchall()
                    expected = reference.execute(
                        "SELECT description,category,command,common,content,is_deleted "
                        "FROM skills WHERE name=?",
                        (name,),
                    ).fetchone()
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(tuple(rows[0]), tuple(expected))
        finally:
            con.close()
            reference.close()

    def test_closeout_reseed_converges_dirty_rows_and_replays_idempotently(self):
        con = sqlite3.connect(":memory:")
        try:
            con.executescript((ENGINE / "schema.sql").read_text())
            for migration in sorted((ENGINE / "migrations").glob("*.sql")):
                if migration.name >= "0171_reseed_sprint_closeout_skills.sql":
                    break
                con.executescript(migration.read_text())
            placeholders = ",".join("?" for _ in CLOSEOUT_ROLE_SKILLS)
            con.execute(
                f"UPDATE skills SET content='stale pre-0171 closeout guidance', "
                f"is_deleted=1 WHERE name IN ({placeholders})",
                tuple(sorted(CLOSEOUT_ROLE_SKILLS)),
            )

            migration = (
                ENGINE / "migrations" / "0171_reseed_sprint_closeout_skills.sql"
            ).read_text()
            con.executescript(migration)
            con.executescript(migration)

            for name in sorted(CLOSEOUT_ROLE_SKILLS):
                with self.subTest(name=name):
                    expected = seed_skills.parse_skill(
                        ASSETS / name / "SKILL.md"
                    )
                    rows = con.execute(
                        "SELECT description,category,command,common,content,is_deleted "
                        "FROM skills WHERE name=?",
                        (name,),
                    ).fetchall()
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(
                        tuple(rows[0]),
                        (
                            expected["description"],
                            expected["category"],
                            expected["command"],
                            expected["common"],
                            expected["content"],
                            0,
                        ),
                    )
        finally:
            con.close()

    def test_flags_output_reseed_matches_fresh_seed_and_replays_idempotently(self):
        upgraded = sqlite3.connect(":memory:")
        fresh = sqlite3.connect(":memory:")
        try:
            for con in (upgraded, fresh):
                con.executescript((ENGINE / "schema.sql").read_text())
            fresh.executescript(
                (ENGINE / "migrations" / "0001_seed_skills.sql").read_text()
            )
            for migration in sorted((ENGINE / "migrations").glob("*.sql")):
                if migration.name >= "0161_reseed_flags_output_guidance.sql":
                    break
                upgraded.executescript(migration.read_text())
            upgraded.execute(
                "UPDATE skills SET content='stale flags guidance', is_deleted=1 "
                "WHERE name='flags'"
            )

            migration = (
                ENGINE / "migrations" / "0161_reseed_flags_output_guidance.sql"
            ).read_text()
            upgraded.executescript(migration)
            upgraded.executescript(migration)

            expected = seed_skills.parse_skill(
                ASSETS / "flags" / "SKILL.md"
            )["content"]
            for con in (fresh, upgraded):
                rows = con.execute(
                    "SELECT content, is_deleted FROM skills WHERE name='flags'"
                ).fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(tuple(rows[0]), (expected, 0))
        finally:
            upgraded.close()
            fresh.close()

    def test_reviewer_skill_owns_severity_and_closeout_timing_judgment(self):
        reviewer = (ASSETS / "sprint_rev" / "SKILL.md").read_text()
        self.assertIn("## Severity rubric", reviewer)
        for severity in ("Critical", "Major", "Medium", "Low"):
            self.assertIn(f"**{severity}**", reviewer)
        self.assertIn("severity does not decide timing", reviewer)
        self.assertIn("requires in-Sprint patching", reviewer)
        for name in SKILLS.keys() - {"sprint_rev"}:
            self.assertNotIn(
                "## Severity rubric", (ASSETS / name / "SKILL.md").read_text()
            )

    def test_reviewer_delivery_terminal_section_branches_before_recording(self):
        reviewer = (ASSETS / "sprint_rev" / "SKILL.md").read_text()
        section = reviewer[
            reviewer.index("## Delivery-terminal closeout"):
            reviewer.index("## Whole-Sprint conformance")
        ]
        normalized = " ".join(section.lower().split())
        for guidance in (
            "sc sprint compile-report",
            "if any non-terminal unit is visible, the wake is stale",
            "`abort`, not `conclude`",
            "do not run `record-conformance`",
            "title and description",
            "grouping, waves, dependencies",
            "after three re-entry episodes",
            "clean or post-sprint-only findings",
        ):
            self.assertIn(guidance, normalized)
        self.assertLess(
            normalized.index("do not run `record-conformance`"),
            normalized.index("clean or post-sprint-only findings"),
        )

    def test_planner_executes_reenter_and_does_not_initiate_conformance(self):
        planner = (ASSETS / "sprint_pln" / "SKILL.md").read_text()
        reenter = planner[
            planner.index("### Re-enter after conformance"):
            planner.index("### Conclude or abort")
        ]
        for command in (
            "sc mem task add",
            "sc sprint plan-unit",
            "--depends-on <work-unit-id>",
            "sc sprint dispatch --sprint <id>",
        ):
            self.assertIn(command, reenter)
        self.assertIn("FnB-directed fallback", planner)
        self.assertNotIn(
            "When all planned delivery work is terminal and merged or explicitly no-code",
            planner,
        )

    def test_closeout_role_skills_share_the_exact_artifact_path_rule(self):
        for name in sorted(CLOSEOUT_ROLE_SKILLS):
            with self.subTest(name=name):
                body = (ASSETS / name / "SKILL.md").read_text()
                self.assertEqual(body.count(ARTIFACT_PATH_RULE), 1)

    def test_close_skill_enters_on_wake_and_defaults_compile_to_reviewer(self):
        close = (ASSETS / "sprint_close" / "SKILL.md").read_text()
        normalized = " ".join(close.split())
        self.assertIn("## Delivery-terminal entry", close)
        self.assertIn("sends the delivery-terminal wake directly", normalized)
        self.assertIn("The participating Reviewer generates the packet", close)
        self.assertIn("Planner and FnB compilation remain valid", close)
        self.assertIn("shared/sprints/sprint-<n>/evidence.json", close)

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
                    "sc sprint send --sprint <id> --to <shortname> --body-file <path> \\",
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
                        "--body-file <path> \\\n  --key <stable-key>"
                    ),
                )
                self.assertIn("reuse it only", normalized)
                self.assertIn("recipient or body changes", normalized)
                for invented in ("ASK:", "ANSWER:", "BLOCKED:"):
                    self.assertNotIn(invented, body)

    def test_authority_split_assigns_reviewer_decisions_and_planner_actions(self):
        planner = (ASSETS / "sprint_pln" / "SKILL.md").read_text()
        reviewer = (ASSETS / "sprint_rev" / "SKILL.md").read_text()
        normalized_planner = " ".join(planner.lower().split())
        normalized_reviewer = " ".join(reviewer.lower().split())

        self.assertIn("## Reviewer decision actions", planner)
        self.assertIn(
            "pause, cancel, re-enter, and conclude are reviewer decisions",
            normalized_planner,
        )
        self.assertIn("planner actions", normalized_planner)
        for command in (
            "sc sprint pause --sprint <id>",
            "sc sprint cancel-unit --sprint <id>",
            "sc sprint complete --sprint <id>",
        ):
            self.assertIn(command, planner)
        self.assertIn("reviewer-authored body", normalized_planner)
        self.assertIn("does not author a second report", normalized_planner)
        self.assertNotIn("you decide scope, sequencing, and recovery", normalized_planner)

        self.assertIn("## Control and conclude decisions", reviewer)
        self.assertIn(
            "owns all pause, cancel, and conclude decisions", normalized_reviewer
        )
        self.assertIn("author the final Sprint report", reviewer)
        self.assertIn("sc sprint record-conformance", reviewer)
        self.assertIn("sc sprint compile-report", reviewer)
        self.assertNotIn("the planner decides whether", normalized_reviewer)
        self.assertNotIn("sc sprint pause --sprint <id>", reviewer)

        for body in (planner, reviewer):
            normalized = " ".join(body.lower().split())
            self.assertIn("fnb board-level override", normalized)
            self.assertIn("decision #46", normalized)

    def test_developer_reports_integrity_concerns_without_taking_pause_action(self):
        developer = (ASSETS / "sprint_dev" / "SKILL.md").read_text()
        normalized = " ".join(developer.lower().split())
        self.assertNotIn("sc sprint pause --sprint <id>", developer)
        self.assertIn("evidence, impact", normalized)
        self.assertIn("recommendation", normalized)
        self.assertIn("does not pause the sprint", normalized)
        self.assertIn("relay itself fails", normalized)

    def test_close_skill_routes_decisions_without_owning_judgment(self):
        close = (ASSETS / "sprint_close" / "SKILL.md").read_text()
        normalized = " ".join(close.lower().split())
        self.assertIn("sc sprint pause --sprint <id>", close)
        self.assertIn("the reviewer decides", normalized)
        self.assertIn("the planner executes", normalized)
        self.assertIn("decision #46", normalized)
        self.assertIn("exhausted recovery wake", normalized)
        self.assertIn("do not create recursive", normalized)

    def test_v21_delivery_contract_is_folded_into_roles_and_boot(self):
        bodies = {
            name: (ASSETS / name / "SKILL.md").read_text()
            for name in SKILLS
        }
        combined = "\n".join(bodies.values())
        for phrase in (
            "active-chat registry",
            "natural boundary",
            "inactivity ceiling",
            "reaper",
            "coordinate mode",
            "PR-event wakes",
            "Defaults satisfy the gate",
        ):
            self.assertIn(phrase, combined)

        developer = bodies["sprint_dev"]
        reviewer = bodies["sprint_rev"]
        planner = bodies["sprint_pln"]
        self.assertIn("outside an armed Sprint", developer)
        self.assertIn("Reviewer decides", developer)
        self.assertIn("replan, cancel", reviewer)
        self.assertIn("Compile the bounded evidence packet first", reviewer)
        self.assertIn("Developer-owned subscriptions", planner)

        boot = (ENGINE / "templates" / "boot.md").read_text()
        self.assertIn("## ACTIVE CHAT DELIVERY", boot)
        self.assertIn("Every `wake_message` creates durable delivery intent", boot)
        self.assertIn("verified live turn", boot)
        self.assertIn("defaults satisfy the gate", boot)

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
                for argument in arguments:
                    action = next(
                        action
                        for action in commands[command]._actions
                        if argument in action.option_strings
                    )
                    self.assertIn("8,000 characters", action.help)

    def test_role_contracts_assign_scheduled_coordination_to_native_wakes(self):
        bodies = {
            name: (ASSETS / name / "SKILL.md").read_text()
            for name in SKILLS
        }
        self.assertIn(
            "participant pickup belongs to native delivery", bodies["sprint_prep"]
        )
        planner = " ".join(bodies["sprint_pln"].split())
        for fact in (
            "scheduled dispatch",
            "unread wake recovery",
            "liveness evaluation",
            "registered-PR observation",
        ):
            self.assertIn(fact, planner)
        self.assertIn(
            "Run `monitor` once for concrete evidence", bodies["sprint_pln"]
        )
        self.assertIn(
            "After `register-pr` succeeds", bodies["sprint_dev"]
        )
        self.assertIn(
            "stop and await the native verdict wake", bodies["sprint_dev"]
        )
        close = " ".join(bodies["sprint_close"].split())
        self.assertIn("sends the delivery-terminal wake directly", close)
        self.assertIn("the Planner does not need to notice terminal state", close)

        combined = "\n".join(bodies.values()).lower()
        for shell_owned_loop in (
            "while true",
            "./sc watch pr",
            "gh pr checks --watch",
            "sc job start",
        ):
            self.assertNotIn(shell_owned_loop, combined)

    def test_role_handoffs_are_explicitly_ordered_and_message_last(self):
        developer = (ASSETS / "sprint_dev" / "SKILL.md").read_text()
        post_merge = developer[
            developer.index("## Post-merge handoff"):
            developer.index("## Report and stop")
        ]
        self.assertLess(
            post_merge.index("Clean the worktree"),
            post_merge.index("Re-run `sc sprint inbox"),
        )
        self.assertLess(
            post_merge.index("Re-run `sc sprint inbox"),
            post_merge.index("sc sprint send --sprint <id>"),
        )
        self.assertLess(
            post_merge.index("sc sprint send --sprint <id>"),
            post_merge.index("Run no trailing Git"),
        )

        reviewer = (ASSETS / "sprint_rev" / "SKILL.md").read_text()
        unit_verdict = reviewer[
            reviewer.index("Complete a unit verdict in this exact order"):
            reviewer.index("## Whole-Sprint conformance")
        ]
        self.assertLess(
            unit_verdict.index("Re-run `sc sprint inbox"),
            unit_verdict.index("sc sprint record-review"),
        )
        self.assertLess(
            unit_verdict.index("sc sprint record-review"),
            unit_verdict.index("Run no trailing command"),
        )

        planner = (ASSETS / "sprint_pln" / "SKILL.md").read_text()
        wave_handoff = planner[
            planner.index("Never dispatch the next wave"):
            planner.index("On receipt, re-run `sc sprint inbox")
        ]
        self.assertIn(
            "merged-work handoff wake is the only normal next-wave dispatch trigger",
            wave_handoff,
        )
        self.assertLess(
            wave_handoff.index("Run `sc sprint inbox"),
            wave_handoff.index("sc sprint dispatch --sprint <id>"),
        )
        self.assertLess(
            wave_handoff.index("sc sprint dispatch --sprint <id>"),
            wave_handoff.index("Run no trailing command"),
        )

    def test_reviewer_entry_separates_predeclaration_qaqc_from_armed_inbox(self):
        reviewer = (ASSETS / "sprint_rev" / "SKILL.md").read_text()
        normalized = " ".join(reviewer.split())
        qaqc = reviewer.index("sc sprint record-qaqc")
        inbox = reviewer.index("sc sprint inbox --sprint <id>")
        self.assertLess(qaqc, inbox)
        self.assertIn(
            "there is no Sprint id or Sprint inbox to inspect yet", normalized
        )
        self.assertIn("sc mem get flags <flag-id>", reviewer)
        self.assertIn(
            "sc mem get flags --feature <feature-id> --resolved", reviewer
        )

    def test_close_drains_before_complete_and_runs_nothing_after_success(self):
        close = (ASSETS / "sprint_close" / "SKILL.md").read_text()
        drain = close.index("Immediately before `complete`, re-run")
        complete = close.index("sc sprint complete --sprint <id>")
        self.assertLess(drain, complete)
        after_success = close.split("After `complete` succeeds", 1)[1]
        normalized = " ".join(after_success.lower().split())
        self.assertIn("run no further sprint command", normalized)
        self.assertNotIn("sc sprint ", after_success.lower())
