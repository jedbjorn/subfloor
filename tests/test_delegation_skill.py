"""The `delegation` engine skill and the wording that goes with it (Feature
#96, spec #273 "The delegation skill" and "Wording changes").

The skill is seeded by migration 0287, granted to every non-Admin flavor, and
rendered to a shell's skill directory. The Claude GUI-seat hook's two refusal
texts, the GUI seat block and the `cli_orchestration` skill name `sc job` for
subagents and watches.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
ASSETS = ENGINE / "assets" / "skills"
PACKS = ENGINE / "templates" / "shells"
RESEED = ENGINE / "migrations" / "0287_seed_delegation_skill.sql"
NON_ADMIN = {"planner", "dev", "reviewer", "devops", "cartographer"}
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "render"))

import compose
import flat as flat_render
import run_seat
import runs
import seat_convert
import seed_skills


def flat(name: str) -> str:
    return " ".join((ASSETS / name / "SKILL.md").read_text().split())


def summary(description: str) -> str:
    """The boot listing's cut of a description (compose._skill_summary)."""
    line = description.strip().splitlines()[0]
    for stop in (". ", " — "):
        head, found, _ = line.partition(stop)
        if found:
            line = head
    return line.rstrip(".")


class DelegationSkillTest(unittest.TestCase):
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

    def shell(self, flavor: str) -> int:
        return self.con.execute(
            "INSERT INTO shells (display_name, shortname, system_prompt, flavor) "
            "VALUES (?, ?, 'prompt', ?)", (f"{flavor} shell", f"{flavor[:4]}x", flavor),
        ).lastrowid

    def test_a_fresh_build_seeds_the_skill_and_grants_every_non_admin_flavor(self):
        parsed = seed_skills.parse_skill(ASSETS / "delegation" / "SKILL.md")
        self.assertEqual((parsed["category"], parsed["common"]), ("workflow", 0))
        row = self.con.execute(
            "SELECT description,category,command,common,content,is_deleted "
            "FROM skills WHERE name='delegation'").fetchone()
        self.assertEqual(tuple(row), (
            parsed["description"], parsed["category"], parsed["command"],
            parsed["common"], parsed["content"], 0))
        granted = {r[0] for r in self.con.execute(
            "SELECT fs.flavor FROM flavor_skills fs JOIN skills s "
            "ON s.skill_id=fs.skill_id WHERE s.name='delegation'")}
        self.assertEqual(granted, NON_ADMIN)
        for flavor in NON_ADMIN:
            self.assertIn(
                "delegation", json.loads((PACKS / f"{flavor}.json").read_text())["skills"])
        self.assertNotIn(
            "delegation", json.loads((PACKS / "admin.json").read_text())["skills"])
        # The packs name every standard flavor, so none is left without a ruling.
        self.assertEqual(
            {path.stem for path in PACKS.glob("*.json")}, NON_ADMIN | {"admin"})

    def test_the_reseed_carries_both_bodies_and_replays_idempotently(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(
            "CREATE TABLE skills (skill_id INTEGER PRIMARY KEY, name TEXT UNIQUE, "
            "description TEXT, category TEXT, command TEXT, common INTEGER, "
            "content TEXT, is_deleted INTEGER DEFAULT 0);"
            "CREATE TABLE flavor_skills (flavor TEXT, skill_id INTEGER, "
            "PRIMARY KEY(flavor, skill_id));"
            "INSERT INTO skills VALUES (4,'cli_orchestration','old','workflow',NULL,0,'old',0);"
            "INSERT INTO flavor_skills VALUES ('planner',4);"
        )
        sql = RESEED.read_text()
        con.executescript(sql)
        first = con.execute("SELECT * FROM skills ORDER BY name").fetchall()
        grants = con.execute("SELECT * FROM flavor_skills ORDER BY 1,2").fetchall()
        con.executescript(sql)
        self.assertEqual(first, con.execute("SELECT * FROM skills ORDER BY name").fetchall())
        self.assertEqual(
            grants, con.execute("SELECT * FROM flavor_skills ORDER BY 1,2").fetchall())
        for name in ("delegation", "cli_orchestration"):
            parsed = seed_skills.parse_skill(ASSETS / name / "SKILL.md")
            self.assertEqual(con.execute(
                "SELECT description, content FROM skills WHERE name=?", (name,)).fetchone(),
                (parsed["description"], parsed["content"]))
        # The reseeded skill keeps its id, and so its grant.
        self.assertEqual(con.execute(
            "SELECT skill_id FROM skills WHERE name='cli_orchestration'").fetchone()[0], 4)
        delegated = con.execute(
            "SELECT flavor FROM flavor_skills fs JOIN skills s USING(skill_id) "
            "WHERE s.name='delegation'").fetchall()
        self.assertEqual({row[0] for row in delegated}, NON_ADMIN)

    def test_it_renders_for_a_non_admin_shell_and_not_for_admin(self):
        dev, admin = self.shell("dev"), self.shell("admin")
        self.assertIn("**delegation**", compose.render_skills(self.con, dev))
        self.assertNotIn("delegation", compose.render_skills(self.con, admin))
        with tempfile.TemporaryDirectory() as tmp:
            flat_render.render_skill_md(self.con, dev, work_dir=Path(tmp) / "dev")
            flat_render.render_skill_md(self.con, admin, work_dir=Path(tmp) / "admin")
            rendered = Path(tmp) / "dev" / ".claude" / "skills" / "delegation" / "SKILL.md"
            self.assertTrue(rendered.is_file())
            text = rendered.read_text()
            self.assertFalse(
                (Path(tmp) / "admin" / ".claude" / "skills" / "delegation").exists())
        self.assertIn("name: delegation", text)
        self.assertIn("# delegation — subagents and watchers through `sc job`", text)
        self.assertIn("sc job agent --harness <name> --model <selector>", text)

    def test_the_first_sentence_says_what_and_when(self):
        description = seed_skills.parse_skill(
            ASSETS / "delegation" / "SKILL.md")["description"]
        first = summary(description)
        self.assertEqual(first, description.split(". ", 1)[0].rstrip("."))
        self.assertLessEqual(len(first), 140)
        self.assertIn("load before", first)

    def test_the_skill_states_the_rules_a_launching_shell_needs(self):
        body = flat("delegation")
        for rule in (
            # identity, authority, accountability
            "A subagent holds exactly your identity and your authority, no more and no less",
            "You answer for everything it does",
            "The engine adds no refusal for what a subagent writes",
            "Its report is input to you, never proof",
            # model required, effort optional
            "`--harness` and `--model` are required. `--effort` is optional",
            "never substitute another model silently",
            # who may launch
            "Every non-Admin flavor may launch a subagent, on the GUI seat and on a terminal seat",
            "Developers and the Planner may launch. A Reviewer of that Sprint may not",
            "A subagent cannot launch or continue a subagent",
            # the worktree rule
            "the engine removes the worktree",
            "do not count as work and are not kept",
            "`.gitignore`", "`.git/info/exclude`",
            # continue
            "A continue is a new run in the same worktree and the same harness session",
            "It takes a slot and has its own time limit",
            # result, stop, clean
            "sc job result <id>", "sc job continue <id>", "sc job kill <id>",
            "sc job clean <id> --discard",
            "It carries no part of the subagent's final message",
            # watchers
            'sc job start --until "<shell command>"', "sc job watch pr --pr <n>",
            "sc job watch timer --after <seconds>", "green is not a directive",
            # one launch per call, then end the turn
            "One launch per tool call. Then end your turn",
        ):
            with self.subTest(rule=rule):
                self.assertIn(rule, body)

    def test_the_limits_in_the_skill_are_the_limits_the_engine_holds(self):
        body = flat("delegation")
        self.assertEqual(
            (runs.AGENT_LIVE_PER_SHELL, runs.AGENT_LIVE_PER_INSTALL,
             runs.AGENT_DEFAULT_TIMEOUT, runs.AGENT_MAX_TIMEOUT,
             runs.WATCH_LIVE_PER_SHELL, runs.WATCH_TIMER_MAX, runs.AGENT_BRIEF_MAX),
            (3, 6, 2 * 3600, 8 * 3600, 10, 24 * 3600, 64 * 1024))
        for row in (
            "| Live subagents for one shell | 3 |",
            "| Live subagents on the install | 6 |",
            "| Run time of one subagent run | 2 hours by default; `--timeout` up to 8 hours |",
            "| Nesting | none: a subagent cannot launch or continue a subagent |",
            "| Live watchers for one shell | 10 |",
            "| Brief size | 64 KiB |",
            "It reads every 60 seconds for at most 1 hour",
            "the delay, which is at most 24 hours",
        ):
            with self.subTest(row=row):
                self.assertIn(row, body)

    def test_cli_orchestration_points_a_gui_seat_at_delegation_and_resumes_kimi(self):
        body = flat("cli_orchestration")
        self.assertIn("Load the `delegation` skill instead", body)
        self.assertNotIn("Tell the FnB that this work needs a terminal session", body)
        self.assertIn('kimi -m <alias> -S <session_id> -p "<follow-up>"', body)
        self.assertNotIn("start a fresh child", body)


class RefusalTextTest(unittest.TestCase):
    """Decision #425's hook texts, refined: small work is an `sc job` subagent."""

    def convert(self, event: dict, tier: str) -> dict | None:
        with mock.patch.dict(
                seat_convert.os.environ, {"SC_SEAT": "gui", "SC_SHELL_FLAVOR": "dev"}):
            return seat_convert.convert(event, tier)

    def reason(self, event: dict, tier: str) -> str:
        output = self.convert(event, tier)["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")
        return output["permissionDecisionReason"]

    def test_a_scheduling_tool_is_refused_with_the_three_bounded_watches(self):
        for tool in sorted(seat_convert.SCHEDULERS):
            for tier in ("rewrite", "deny-only"):
                with self.subTest(tool=tool, tier=tier):
                    reason = self.reason({"tool_name": tool, "tool_input": {}}, tier)
                    self.assertEqual(reason, seat_convert.SCHEDULER_REFUSAL)
        for named in ('`sc job start --until "<command>"`', "`sc job watch pr --pr <n>`",
                      "`sc job watch timer --after <seconds>`", "end your turn"):
            self.assertIn(named, seat_convert.SCHEDULER_REFUSAL)

    def test_a_background_agent_refusal_names_sc_job_agent_and_the_big_work_route(self):
        event = {"tool_name": "Agent", "tool_input": {"prompt": "x", "run_in_background": True}}
        reason = self.reason(event, "deny-only")
        self.assertEqual(reason, seat_convert.AGENT_REFUSAL)
        # Decision #428: the confusion it prevents comes first.
        self.assertTrue(reason.startswith(
            "A background Agent dies when your turn ends, and its work with it. "
            "Run Agent in the foreground."))
        self.assertIn("`sc job agent`", reason)
        self.assertIn("`delegation` skill", reason)
        self.assertIn("Big multi-shell work is a Sprint", reason)
        self.assertNotIn("lanes for background agents", reason)

    def test_the_rewrite_of_a_background_agent_call_is_unchanged(self):
        event = {"tool_name": "Agent", "tool_input": {"prompt": "x", "run_in_background": True}}
        output = self.convert(event, "rewrite")["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "allow")
        self.assertEqual(output["updatedInput"], {"prompt": "x", "run_in_background": False})
        # A foreground Agent call, and any call outside the GUI seat, is left alone.
        self.assertIsNone(self.convert(
            {"tool_name": "Agent", "tool_input": {"prompt": "x"}}, "rewrite"))
        with mock.patch.dict(seat_convert.os.environ, {"SC_SEAT": "tui"}):
            self.assertIsNone(seat_convert.convert(event, "deny-only"))

    def test_the_gui_seat_block_sends_subagents_and_watches_through_sc_job(self):
        block = run_seat.boot_block("dev", "gui")
        self.assertIn(
            "Long work goes through `sc job`, and so do subagents (`sc job agent`) "
            "and bounded watches (`sc job watch`)", block)
        self.assertIn("Do not schedule in-session timers", block)
        self.assertNotIn("sc job agent", run_seat.boot_block("dev", "tui"))
        self.assertEqual(run_seat.boot_block("admin", "gui"), "")

    def test_the_registration_refusals_name_what_they_prevent(self):
        # Decision #428: each refusal names the confusion it prevents, first.
        self.assertEqual(
            runs.NESTING_CONFUSION, "delegated work whose owner can no longer be traced")
        self.assertEqual(
            runs.REVIEWER_CONFUSION, "a review verdict that rests on a reader nobody assigned")
        self.assertEqual(
            runs.ADMIN_CONFUSION,
            "a subagent launched from the Admin seat, which the delegation rules do not cover")
        self.assertEqual(
            runs.RESERVED_NAME_CONFUSION, "a subagent worktree that is also a shell's worktree")


if __name__ == "__main__":
    unittest.main()
