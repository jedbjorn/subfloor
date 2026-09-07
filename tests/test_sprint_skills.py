"""Gates for the Sprints v2 skill set after the boot-first reconciliation (F72).

Assertions favor commands, section presence, grant sets, byte budgets, and
migration convergence over sentences. The shared protocol lives once in
`sprint_protocol`; role skills open by loading it and keep only role steps.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
ASSETS = ENGINE / "assets" / "skills"
TEMPLATES = ENGINE / "templates"
sys.path.insert(0, str(ENGINE / "scripts"))

import seed_skills
import shell_factory
import sprint_cli
import sprint_message_delivery

PROTOCOL = "sprint_protocol"
ROLE_SKILLS = {
    "sprint_prep": {"planner"},
    "sprint_pln": {"planner"},
    "sprint_dev": {"dev"},
    "sprint_rev": {"reviewer"},
}
SKILLS = {PROTOCOL: {"planner", "dev", "reviewer"}, **ROLE_SKILLS}
# Every reseed since the guidance reconciliation, applied in order: the test
# proves the trailing migrations converge on the current asset text.
RECONCILIATION = (
    ENGINE / "migrations" / "0257_guidance_reconciliation.sql",
    ENGINE / "migrations" / "0258_reseed_non_sprint_green_wake.sql",
    ENGINE / "migrations" / "0260_reseed_sprint_closeout_scope.sql",
)
RETIRED = (
    "memory", "db_map", "bootstrap", "surface_catalogue", "messaging", "flags",
    "spec", "review", "docs", "admin_git", "cartographer", "sprint_close",
)
COMMON = {"curate", "issue_reporting", "web_search"}

# Guidance bytes a shell of each flavor reads: boot template + rendered system
# prompt (focus + procedure body + mandate) + every granted skill body. The
# ceilings are the pre-F72 sums for the same flavor (boot + prompt + granted
# skills at origin/main 5159c861); the reconciliation must not grow any flavor.
GUIDANCE_BYTE_CEILING = {
    "admin": 76_806,
    "cartographer": 65_449,
    "dev": 103_980,
    "devops": 81_757,
    "planner": 128_275,
    "reviewer": 79_809,
}


def skill_text(name: str) -> str:
    return (ASSETS / name / "SKILL.md").read_text()


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
        for name, flavors in SKILLS.items():
            with self.subTest(name=name):
                parsed = seed_skills.parse_skill(ASSETS / name / "SKILL.md")
                row = self.con.execute(
                    "SELECT description,category,command,common,content,is_deleted "
                    "FROM skills WHERE name=?",
                    (name,),
                ).fetchone()
                self.assertEqual(
                    tuple(row),
                    (parsed["description"], parsed["category"], parsed["command"],
                     parsed["common"], parsed["content"], 0),
                )
                granted = {
                    r[0] for r in self.con.execute(
                        "SELECT fs.flavor FROM flavor_skills fs "
                        "JOIN skills s ON s.skill_id=fs.skill_id WHERE s.name=?",
                        (name,),
                    )
                }
                self.assertEqual(granted, flavors)

    def test_retired_skills_are_absent_after_a_fresh_build(self):
        for name in RETIRED:
            with self.subTest(name=name):
                self.assertIsNone(self.con.execute(
                    "SELECT 1 FROM skills WHERE name=?", (name,)).fetchone())
        common = {
            r[0] for r in self.con.execute(
                "SELECT name FROM skills WHERE common=1 AND is_deleted=0")
        }
        self.assertEqual(common, COMMON)

    def test_role_skills_load_the_protocol_first_and_hold_no_copy_of_it(self):
        protocol = skill_text(PROTOCOL)
        for heading in ("## Lifecycle", "## Wake types", "## Inbox, accept, decline",
                        "## Relay", "## Receipt recovery", "## Artifacts",
                        "## Authority"):
            self.assertIn(heading, protocol)
        for name in ROLE_SKILLS:
            with self.subTest(name=name):
                body = skill_text(name)
                self.assertIn("Load `sprint_protocol` first", body)
                # Protocol material has one home.
                self.assertNotIn("| `force-new` |", body)
                self.assertNotIn("Stable key =", body)
                self.assertNotIn("unusable success receipt", body)
                self.assertNotIn("decision #46", body)
                self.assertNotIn("--intent information --reply-to <message-id> --key", body)

    def test_protocol_wake_types_match_the_delivery_literals(self):
        protocol = skill_text(PROTOCOL)
        for literal in sorted(sprint_message_delivery.DECLARED_TYPES):
            self.assertIn(f"| `{literal}` |", protocol)
        boot = (TEMPLATES / "boot.md").read_text()
        self.assertIn("## WAKES", boot)
        for literal in sprint_message_delivery.DECLARED_TYPES:
            self.assertNotIn(f"`{literal}`", boot)
        self.assertNotIn("ACTIVE CHAT DELIVERY", boot)
        self.assertNotIn("reaper", boot)

    def test_skills_use_only_the_shipped_shell_command_surface(self):
        expected = {
            "declare", "plan-unit", "replan-unit", "recall-unit",
            "reroute-participant", "arm", "inbox", "spec-revision", "rebind-spec",
            "send", "accept", "decline", "complete-unit", "cancel-unit",
            "resolve-unit", "register-pr", "reconcile-pr", "pause", "resume",
            "complete", "abort", "request-review", "record-review",
            "authorize-merge", "dispatch", "monitor", "watcher-state",
            "record-conformance", "disposition-followup", "compile-report",
            "cleanup-status", "cleanup", "show",
        }
        combined = "\n".join(skill_text(name) for name in SKILLS)
        for command in expected - {"monitor", "spec-revision", "complete",
                                   "disposition-followup"}:
            self.assertIn(f"sc sprint {command}", combined)
        for gone in ("sc sprint monitor", "record-qaqc"):
            self.assertNotIn(gone, combined)
        parser = sprint_cli.build_parser()
        commands = next(
            action for action in parser._actions
            if isinstance(action, sprint_cli.argparse._SubParsersAction)
        ).choices
        self.assertEqual(expected, set(commands))

    def test_qaqc_has_one_write_form(self):
        for name in ("sprint_prep", "sprint_rev"):
            with self.subTest(name=name):
                self.assertIn("sc mem doc qaqc <spec-document-id> --verdict pass|fail",
                              skill_text(name))

    def test_role_skills_keep_their_stateful_routes(self):
        dev = skill_text("sprint_dev")
        for command in ("sc context --work-unit <id>", "sc sprint register-pr",
                        "sc sprint request-review", "sc sprint authorize-merge",
                        "--intent handoff", "sc sprint complete-unit",
                        "sc sprint watcher-state"):
            self.assertIn(command, dev)
        self.assertIn("never wait for a separate FnB\ndirective", dev)
        rev = skill_text("sprint_rev")
        for section in ("### Red-check doctrine", "## Severity rubric",
                        "## Delivery-terminal closeout", "## Whole-Sprint conformance"):
            self.assertIn(section, rev)
        red = " ".join(rev.split())
        self.assertLess(red.index("In-scope failure"), red.index("Out-of-scope failure"))
        self.assertIn("sc sprint record-conformance", rev)
        pln = skill_text("sprint_pln")
        for command in ("sc sprint dispatch", "sc sprint pause", "sc sprint resume",
                        "sc sprint replan-unit", "sc sprint recall-unit",
                        "sc sprint resolve-unit", "sc sprint reroute-participant",
                        "sc sprint reconcile-pr", "sc sprint rebind-spec",
                        "sc sprint cleanup", "sc sprint abort"):
            self.assertIn(command, pln)
        prep = skill_text("sprint_prep")
        for command in ("sc sprint declare", "sc sprint plan-unit", "sc sprint arm",
                        "--merge-grant", "sc models resolve"):
            self.assertIn(command, prep)

    def test_every_affected_file_argument_names_the_hard_ceiling(self):
        parser = sprint_cli.build_parser()
        commands = next(
            action for action in parser._actions
            if isinstance(action, sprint_cli.argparse._SubParsersAction)
        ).choices
        for command, arguments in {
            "send": ("--body-file",),
            "complete-unit": ("--result-file",),
            "request-review": ("--readiness-file",),
            "record-review": ("--body-file",),
            "record-conformance": ("--body-file", "--findings-file",
                                   "--final-report-file"),
            "disposition-followup": ("--resolution-file",),
            "complete": ("--report-file",),
        }.items():
            with self.subTest(command=command):
                for argument in arguments:
                    action = next(
                        action for action in commands[command]._actions
                        if argument in action.option_strings
                    )
                    self.assertIn("8,000 characters", action.help)

    def test_guidance_bytes_per_flavor_do_not_exceed_the_pre_reconciliation_sum(self):
        boot = len((TEMPLATES / "boot.md").read_bytes())
        for path in sorted((TEMPLATES / "shells").glob("*.json")):
            template = json.loads(path.read_text())
            flavor = template["flavor"]
            with self.subTest(flavor=flavor):
                prompt = shell_factory.render_prompt(
                    "Shell", template["role"], "repo",
                    template["focus"].replace("{{repo}}", "repo"),
                    template["mandate"].replace("{{repo}}", "repo"),
                    shell_factory.load_procedure(flavor),
                )
                granted = set(template["skills"]) | COMMON
                skills = sum(
                    len((ASSETS / name / "SKILL.md").read_bytes())
                    for name in granted if (ASSETS / name / "SKILL.md").exists()
                )
                total = boot + len(prompt.encode()) + skills
                self.assertLessEqual(total, GUIDANCE_BYTE_CEILING[flavor],
                                     f"{flavor}: {total} bytes")

    def test_reconciliation_reseed_converges_dirty_rows_and_replays_idempotently(self):
        con = sqlite3.connect(":memory:")
        con.executescript(
            "CREATE TABLE skills (skill_id INTEGER PRIMARY KEY, name TEXT UNIQUE, "
            "description TEXT, category TEXT, command TEXT, common INTEGER, "
            "content TEXT, is_deleted INTEGER DEFAULT 0);"
            "CREATE TABLE shell_skills (shell_id INTEGER, skill_id INTEGER);"
            "CREATE TABLE flavor_skills (flavor TEXT, skill_id INTEGER, "
            "PRIMARY KEY(flavor, skill_id));"
        )
        for index, name in enumerate(RETIRED + tuple(SKILLS) + ("fork_only",), 1):
            con.execute(
                "INSERT INTO skills VALUES (?,?,?,?,?,?,?,?)",
                (index, name, "stale", "stale", None, 1, "stale body", 1),
            )
            con.execute("INSERT INTO shell_skills VALUES (7, ?)", (index,))
            con.execute("INSERT INTO flavor_skills VALUES ('dev', ?)", (index,))
        con.execute("UPDATE skills SET is_deleted=0, category='fork' WHERE name='fork_only'")
        migration = "\n".join(path.read_text() for path in RECONCILIATION)
        con.executescript(migration)
        first = con.execute(
            "SELECT name, description, category, command, common, content, is_deleted "
            "FROM skills ORDER BY name").fetchall()
        con.executescript(migration)
        self.assertEqual(first, con.execute(
            "SELECT name, description, category, command, common, content, is_deleted "
            "FROM skills ORDER BY name").fetchall())
        names = {row[0] for row in first}
        self.assertFalse(names & set(RETIRED))
        for name in SKILLS:
            parsed = seed_skills.parse_skill(ASSETS / name / "SKILL.md")
            row = con.execute(
                "SELECT description,category,command,common,content,is_deleted "
                "FROM skills WHERE name=?", (name,)).fetchone()
            self.assertEqual(tuple(row), (parsed["description"], parsed["category"],
                                          parsed["command"], parsed["common"],
                                          parsed["content"], 0), name)
        # The fork-local row keeps its body and its grants.
        self.assertEqual(
            con.execute("SELECT category, content, is_deleted FROM skills "
                        "WHERE name='fork_only'").fetchone(),
            ("fork", "stale body", 0))
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM flavor_skills fs JOIN skills s USING (skill_id) "
            "WHERE s.name='fork_only'").fetchone()[0], 1)
        # dev pack converged to protocol + role skill; planner-only skills gone.
        dev = {r[0] for r in con.execute(
            "SELECT s.name FROM flavor_skills fs JOIN skills s USING (skill_id) "
            "WHERE fs.flavor='dev'")}
        self.assertIn("sprint_protocol", dev)
        self.assertIn("sprint_dev", dev)
        self.assertNotIn("sprint_pln", dev)
        con.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
