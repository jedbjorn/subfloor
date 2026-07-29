"""Conductor Step 8 transition, wake, doctor, and synthetic-sprint gates."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"
LUNA_MIGRATION = MIGRATIONS / "0127_conductor_luna_default.sql"
SCRIPTS = ENGINE / "scripts"
RENDER = ENGINE / "render"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(RENDER))

import compose
import conductor_policy
import conductor_runtime as runtime
import run


def build_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA.read_text())
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")
    return con


class RuntimeFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sc_conductor8_")
        self.addCleanup(self.tmp.cleanup)
        self.con = build_db(Path(self.tmp.name) / "runtime.db")
        self.addCleanup(self.con.close)
        shells = (
            (1, "Conductor", "CON1", "conductor", "con-token"),
            (2, "Planner", "PLN1", "planner", "plan-token"),
            (3, "Developer", "DEV1", "dev", "dev-token"),
            (4, "Reviewer", "REV1", "reviewer", "rev-token"),
            (5, "Other Planner", "PLN2", "planner", "plan2-token"),
        )
        self.con.executemany(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,api_key) "
            "VALUES (?,?,?,?,?,?)",
            [
                (sid, name, short, flavor, "x", token)
                for sid, name, short, flavor, token in shells
            ],
        )
        self.con.execute(
            "INSERT INTO documents "
            "(document_id,kind,title,body,frozen) "
            "VALUES (100,'doc','SPRINT: synthetic','status: ACTIVE',0)"
        )
        self.con.execute(
            "INSERT INTO sprints "
            "(sprint_doc_id,state,legacy,planner_shell_id,"
            "planner_route,dev_route,reviewer_route) "
            "VALUES (100,'active',1,2,"
            "'claude/sonnet','claude/sonnet','codex/gpt-5.3-codex')"
        )
        self.con.execute(
            "INSERT INTO sprint_units "
            "(unit_id,sprint_doc_id,seq,unit_title,dev_shell_id,"
            "reviewer_shell_id,state,branch,pr_number,review_head) "
            "VALUES (10,100,'U1','synthetic unit',3,4,"
            "'pending','feat/u1',7,NULL)"
        )
        self.con.execute(
            "INSERT OR REPLACE INTO model_routes "
            "(harness,selector,provider,provider_model,source,availability,"
            "stale,headless_supported,high_effort_supported,"
            "supported_efforts,last_seen_at) "
            "VALUES ('opencode',?,'openai','gpt-5.6-luna',"
            "'opencode-cli','available',0,1,0,'[]',datetime('now'))",
            (runtime.DEFAULT_CONDUCTOR_MODEL,),
        )
        self.con.commit()
        runtime._launching_until = 0.0
        self.commands: list[list[str]] = []

    def launcher(self, command: list[str]) -> int:
        self.commands.append(command)
        return 9000 + len(self.commands)

    def set_unit(self, state: str, *, review_head=None) -> None:
        self.con.execute(
            "UPDATE sprint_units SET state=?,review_head=? WHERE unit_id=10",
            (state, review_head),
        )
        self.con.execute("UPDATE documents SET frozen=0 WHERE document_id=100")
        self.con.commit()

    def set_declared(self) -> None:
        self.con.execute("DELETE FROM sprints WHERE sprint_doc_id=100")
        self.con.execute(
            "INSERT INTO sprints "
            "(sprint_doc_id,state,legacy,planner_shell_id,"
            "planner_route,dev_route,reviewer_route) "
            "VALUES (100,'declared',1,2,"
            "'claude/sonnet','claude/sonnet','codex/gpt-5.3-codex')"
        )
        self.con.commit()

    def emit(self, issuer: str, kind: str, payload: dict, *, unit: bool = True) -> int:
        shell_id = {
            "dev": 3,
            "reviewer": 4,
            "planner": 2,
            "system": None,
        }[issuer]
        return self.con.execute(
            "INSERT INTO directives "
            "(issuer_shell_id,issuer_flavor,kind,payload,target,"
            "sprint_doc_id,unit_id) VALUES (?,?,?,?, 'conductor',100,?)",
            (
                shell_id,
                issuer,
                kind,
                json.dumps(payload),
                10 if unit else None,
            ),
        ).lastrowid


class ConductorFlavorAndDoctorTests(RuntimeFixture):
    @staticmethod
    def model_probe(models: str = runtime.DEFAULT_CONDUCTOR_MODEL):
        def probe(*_args, **_kwargs):
            return subprocess.CompletedProcess(
                ["opencode", "models"],
                0,
                stdout=f"{models}\n",
                stderr="",
            )

        return probe

    def test_template_migration_and_boot_have_exact_skill_and_are_exhaustive(self):
        self.assertEqual(runtime.CONDUCTOR_HARNESS, "opencode")
        self.assertEqual(
            runtime.DEFAULT_CONDUCTOR_MODEL,
            "openai/gpt-5.6-luna",
        )
        template = json.loads((ENGINE / "templates/shells/conductor.json").read_text())
        self.assertEqual(template["flavor"], "conductor")
        self.assertEqual(template["skills"], ["sprint_cond"])
        default = self.con.execute(
            "SELECT harness,model,is_default FROM flavor_defaults "
            "WHERE flavor='conductor'"
        ).fetchone()
        self.assertEqual(
            tuple(default),
            ("opencode", runtime.DEFAULT_CONDUCTOR_MODEL, 1),
        )
        shell = self.con.execute("SELECT * FROM shells WHERE shell_id=1").fetchone()
        rendered = runtime.render_boot(self.con, shell)
        for issuer, kind, action, success in runtime.TRANSITIONS:
            self.assertIn(
                f"| `{issuer}` | `{kind}` | {action} | {success} |",
                rendered,
            )
        self.assertNotIn("## MEMORY", rendered)
        self.assertNotIn("## SKILLS", rendered)
        skill = (ENGINE / "assets/skills/sprint_cond/SKILL.md").read_text()
        for phrase in (
            "recorded originating Planner",
            "stored role route",
            "normal merge",
            "Never invent",
        ):
            self.assertIn(phrase, rendered)
            self.assertIn(phrase, skill)
        composed = compose.compose_boot(
            self.con,
            shell,
            {"username": "Jed", "user_id": 1},
            "0001",
            1,
        )
        self.assertEqual(composed, rendered)

    def test_conductor_harness_policy_rejects_every_non_opencode_route(self):
        conductor_policy.require_harness("conductor", "opencode")
        conductor_policy.require_harness("dev", "codex")
        for harness in ("claude", "codex", "kimi", "vibe"):
            with (
                self.subTest(harness=harness),
                self.assertRaisesRegex(ValueError, "requires harness 'opencode'"),
            ):
                conductor_policy.require_harness("conductor", harness)

    def test_luna_migration_upgrades_only_the_shipped_conductor_route(self):
        self.con.execute(
            "UPDATE flavor_defaults SET model='ollama-cloud/gpt-oss:20b' "
            "WHERE flavor='conductor' AND harness='opencode'"
        )
        self.con.commit()
        self.con.executescript(LUNA_MIGRATION.read_text())
        self.assertEqual(
            self.con.execute(
                "SELECT model FROM flavor_defaults "
                "WHERE flavor='conductor' AND harness='opencode'"
            ).fetchone()[0],
            runtime.DEFAULT_CONDUCTOR_MODEL,
        )

        self.con.execute(
            "UPDATE flavor_defaults SET model='ollama-cloud/gpt-oss:120b' "
            "WHERE flavor='conductor' AND harness='opencode'"
        )
        self.con.commit()
        self.con.executescript(LUNA_MIGRATION.read_text())
        self.assertEqual(
            self.con.execute(
                "SELECT model FROM flavor_defaults "
                "WHERE flavor='conductor' AND harness='opencode'"
            ).fetchone()[0],
            "ollama-cloud/gpt-oss:120b",
        )

    def test_doctor_proves_cli_shell_exact_skill_and_route(self):
        config = runtime.ConductorConfig(True, "CON1", runtime.DEFAULT_CONDUCTOR_MODEL)
        result = runtime.doctor(
            self.con,
            config,
            which=lambda binary: f"/bin/{binary}",
            run=self.model_probe(),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["harness"], "opencode")

        with self.assertRaisesRegex(
            runtime.ConductorConfigError, "not locally runnable"
        ):
            runtime.doctor(
                self.con,
                config,
                which=lambda binary: f"/bin/{binary}",
                run=self.model_probe("ollama-cloud/another-model"),
            )

    def test_wake_is_config_gated_and_single_flight(self):
        self.emit("dev", "unit-report", {"shipped": "x"})
        disabled = runtime.maybe_wake(
            self.con,
            config=runtime.ConductorConfig(),
            launcher=self.launcher,
        )
        self.assertFalse(disabled["launched"])

        config = runtime.ConductorConfig(True, "CON1", runtime.DEFAULT_CONDUCTOR_MODEL)
        with (
            mock.patch.object(runtime, "_launch_is_live", return_value=False),
            mock.patch.object(
                runtime,
                "doctor",
                return_value={
                    "enabled": True,
                    "ok": True,
                    "shell_id": 1,
                    "shell": "CON1",
                    "harness": "opencode",
                    "model": runtime.DEFAULT_CONDUCTOR_MODEL,
                },
            ),
        ):
            first = runtime.maybe_wake(
                self.con, config=config, launcher=self.launcher, now=lambda: 100.0
            )
            second = runtime.maybe_wake(
                self.con, config=config, launcher=self.launcher, now=lambda: 101.0
            )
        self.assertTrue(first["launched"])
        self.assertEqual(second["reason"], "launching")
        self.assertEqual(len(self.commands), 1)
        self.assertIn("--harness", self.commands[0])
        self.assertIn("opencode", self.commands[0])
        self.assertIn(runtime.DEFAULT_CONDUCTOR_MODEL, self.commands[0])

    def test_opencode_headless_route_does_not_invent_effort(self):
        adapter = run.load_adapter("opencode")
        self.assertIsNone(run.default_headless_effort(adapter))
        command = run.headless_command(
            adapter,
            "act",
            runtime.DEFAULT_CONDUCTOR_MODEL,
            effort=run.default_headless_effort(adapter),
        )
        self.assertEqual(command[:2], ["opencode", "run"])


class ConductorDirectiveMatrixTests(RuntimeFixture):
    CASES = (
        (
            "dev",
            "ready-for-review",
            "working",
            {"pr_number": 7, "head": "abc", "branch": "feat/u1", "checks": "green"},
            True,
        ),
        (
            "dev",
            "ask-planner",
            "working",
            {"question": "which?", "alternatives": ["a", "b"], "evidence": ["x"]},
            True,
        ),
        (
            "dev",
            "merged",
            "in_review",
            {"pr_number": 7, "head": "abc", "merge_sha": "def"},
            True,
        ),
        (
            "dev",
            "unit-report",
            "working",
            {"shipped": "behavior", "judgements": []},
            True,
        ),
        (
            "reviewer",
            "review-clean",
            "in_review",
            {"head": "abc", "findings": [], "mutation": "failed then passed"},
            True,
        ),
        (
            "reviewer",
            "findings",
            "in_review",
            {"head": "abc", "findings": [{"severity": "Major"}]},
            True,
        ),
        (
            "reviewer",
            "ask-planner",
            "in_review",
            {"question": "scope?", "alternatives": ["a", "b"]},
            True,
        ),
        (
            "planner",
            "handoff",
            "pending",
            {},
            False,
        ),
        (
            "planner",
            "kickoff",
            "pending",
            {"to": "DEV1", "instruction": "build", "model": "sonnet"},
            True,
        ),
        (
            "planner",
            "hold",
            "working",
            {"reason": "missing evidence", "next": "supply it"},
            True,
        ),
        (
            "planner",
            "re-scope",
            "blocked",
            {"to": "DEV1", "scope": "smaller", "reason": "evidence"},
            True,
        ),
        (
            "planner",
            "re-task",
            "blocked",
            {"to": "DEV1", "instruction": "new path", "reason": "evidence"},
            True,
        ),
        (
            "planner",
            "close",
            "merged",
            {"main_sha": "abc", "conformance_directive_id": 77, "summary": "done"},
            False,
        ),
        (
            "planner",
            "answer",
            "blocked",
            {"to": "DEV1", "question_directive_id": 42, "answer": "choose a"},
            True,
        ),
        (
            "system",
            "pr-green",
            "in_review",
            {"head_sha": "abc", "transition": "checks:SUCCESS"},
            True,
        ),
        (
            "system",
            "pr-red",
            "in_review",
            {"head_sha": "abc", "transition": "checks:FAILURE"},
            True,
        ),
        (
            "system",
            "pr-merged",
            "in_review",
            {"head_sha": "abc", "transition": "merged:MERGED"},
            True,
        ),
        ("system", "stall", "working", {"dwell_seconds": 99}, True),
        ("system", "dead-shell", "working", {"process": {"live": False}}, True),
    )

    def test_every_kind_and_issuer_has_one_executable_action(self):
        self.assertEqual(
            {(issuer, kind) for issuer, kind, _action, _pass in runtime.TRANSITIONS},
            {(issuer, kind) for issuer, kind, _state, _payload, _unit in self.CASES},
        )
        for issuer, kind, state, payload, linked_unit in self.CASES:
            with (
                self.subTest(issuer=issuer, kind=kind),
                tempfile.TemporaryDirectory(prefix="sc_conductor_case_") as td,
            ):
                con = build_db(Path(td) / "case.db")
                try:
                    con.executemany(
                        "INSERT INTO shells "
                        "(shell_id,display_name,shortname,flavor,"
                        "system_prompt,api_key) VALUES (?,?,?,?,?,?)",
                        (
                            (1, "Conductor", "CON1", "conductor", "x", "con-token"),
                            (2, "Planner", "PLN1", "planner", "x", "plan-token"),
                            (3, "Developer", "DEV1", "dev", "x", "dev-token"),
                            (4, "Reviewer", "REV1", "reviewer", "x", "rev-token"),
                        ),
                    )
                    con.execute(
                        "INSERT INTO documents "
                        "(document_id,kind,title,body,frozen) VALUES "
                        "(100,'doc','SPRINT: case','status: ACTIVE',0)"
                    )
                    sprint_state = "declared" if kind == "handoff" else "active"
                    con.execute(
                        "INSERT INTO sprints "
                        "(sprint_doc_id,state,legacy,planner_shell_id,"
                        "planner_route,dev_route,reviewer_route) "
                        "VALUES (100,?,1,2,"
                        "'claude/sonnet','claude/sonnet','codex/gpt-5.3-codex')",
                        (sprint_state,),
                    )
                    con.execute(
                        "INSERT INTO sprint_units "
                        "(unit_id,sprint_doc_id,seq,unit_title,"
                        "dev_shell_id,reviewer_shell_id,state,branch,"
                        "pr_number,review_head) "
                        "VALUES (10,100,'U1','case',3,4,?,?,7,?)",
                        (
                            state,
                            "feat/u1",
                            "abc" if kind == "merged" else None,
                        ),
                    )
                    shell_id = {
                        "dev": 3,
                        "reviewer": 4,
                        "planner": 2,
                        "system": None,
                    }[issuer]
                    if kind == "close":
                        con.execute(
                            "INSERT INTO directives "
                            "(directive_id,issuer_shell_id,issuer_flavor,kind,payload,"
                            "target,sprint_doc_id,unit_id,status,executed_at) "
                            "VALUES (77,4,'reviewer','review-clean',?,"
                            "'conductor',100,NULL,'executed',datetime('now'))",
                            (
                                json.dumps(
                                    {
                                        "mode": "conformance",
                                        "main_sha": "abc",
                                        "findings": [],
                                    }
                                ),
                            ),
                        )
                    directive_id = con.execute(
                        "INSERT INTO directives "
                        "(issuer_shell_id,issuer_flavor,kind,payload,"
                        "target,sprint_doc_id,unit_id) "
                        "VALUES (?,?,?,?, 'conductor',100,?)",
                        (
                            shell_id,
                            issuer,
                            kind,
                            json.dumps(payload),
                            10 if linked_unit else None,
                        ),
                    ).lastrowid
                    con.commit()
                    result = runtime.act(con, directive_id, 1, launcher=self.launcher)
                    self.assertEqual(result["status"], "executed")
                    row = con.execute(
                        "SELECT status,refusal_reason FROM directives "
                        "WHERE directive_id=?",
                        (directive_id,),
                    ).fetchone()
                    self.assertEqual(tuple(row), ("executed", None))
                    event = con.execute(
                        "SELECT event_kind FROM sentinel_events WHERE directive_id=?",
                        (directive_id,),
                    ).fetchone()
                    self.assertEqual(event[0], "conductor-executed")
                finally:
                    con.close()

    def test_malformed_payload_is_refused_and_escalated(self):
        directive_id = self.emit("dev", "ready-for-review", {"pr_number": "not-int"})
        self.con.commit()
        result = runtime.act(self.con, directive_id, 1, launcher=self.launcher)
        self.assertEqual(result["status"], "refused")
        self.assertIn("payload.pr_number", result["reason"])
        self.assertIn("command", result["escalation"])
        self.assertIn("PLN1", result["escalation"]["command"])
        self.assertEqual(
            self.con.execute(
                "SELECT status FROM directives WHERE directive_id=?",
                (directive_id,),
            ).fetchone()[0],
            "refused",
        )

    def test_handoff_slot_waits_for_the_act_transaction_to_commit(self):
        self.set_declared()
        directive_id = self.emit("planner", "handoff", {}, unit=False)
        self.con.commit()

        result = runtime.act(
            self.con, directive_id, 1, launcher=self.launcher
        )

        self.assertEqual(result["status"], "executed")
        self.assertEqual(len(result["launches"]), 1)
        self.assertIn("--await-sprint-active", result["launches"][0])

    def test_refusal_rolls_back_partial_board_changes(self):
        self.set_unit("working")
        self.con.execute("UPDATE shells SET is_deleted=1 WHERE shell_id=4")
        directive_id = self.emit(
            "dev",
            "ready-for-review",
            {
                "pr_number": 99,
                "head": "new-head",
                "branch": "feat/new",
                "checks": "green",
            },
        )
        self.con.commit()

        result = runtime.act(self.con, directive_id, 1, launcher=self.launcher)

        self.assertEqual(result["status"], "refused")
        unit = self.con.execute(
            "SELECT state,pr_number,branch FROM sprint_units WHERE unit_id=10"
        ).fetchone()
        self.assertEqual(tuple(unit), ("working", 7, "feat/u1"))

    def test_non_conductor_cannot_act(self):
        directive_id = self.emit("dev", "unit-report", {"shipped": "x"})
        self.con.commit()
        with self.assertRaisesRegex(PermissionError, "conductor"):
            runtime.act(self.con, directive_id, 2, launcher=self.launcher)

    def test_declared_zero_unit_and_dependency_cycle_handoffs_are_refused(self):
        self.set_declared()
        self.con.execute("DELETE FROM sprint_units WHERE sprint_doc_id=100")
        empty_id = self.emit("planner", "handoff", {}, unit=False)
        self.con.commit()
        empty = runtime.act(self.con, empty_id, 1, launcher=self.launcher)
        self.assertEqual(empty["status"], "refused")
        self.assertIn("non-empty", empty["reason"])
        self.assertEqual(
            self.con.execute(
                "SELECT state FROM sprints WHERE sprint_doc_id=100"
            ).fetchone()[0],
            "declared",
        )

        self.con.executemany(
            "INSERT INTO sprint_units "
            "(unit_id,sprint_doc_id,seq,unit_title,dev_shell_id,"
            "reviewer_shell_id,state,depends_on) "
            "VALUES (?,100,?,?,3,4,'pending',?)",
            (
                (20, "U1", "cycle one", "U2"),
                (21, "U2", "cycle two", "U1"),
            ),
        )
        cycle_id = self.emit("planner", "handoff", {}, unit=False)
        self.con.commit()
        cycle = runtime.act(self.con, cycle_id, 1, launcher=self.launcher)
        self.assertEqual(cycle["status"], "refused")
        self.assertIn("dependency cycle", cycle["reason"])

    def test_two_planners_route_questions_only_to_recorded_owner(self):
        self.set_unit("working")
        directive_id = self.emit(
            "dev",
            "ask-planner",
            {"question": "choose", "alternatives": ["a", "b"]},
        )
        self.con.commit()
        result = runtime.act(
            self.con, directive_id, 1, launcher=self.launcher
        )
        self.assertEqual(result["status"], "executed")
        self.assertEqual(len(result["launches"]), 1)
        command = result["launches"][0]
        self.assertIn("PLN1", command)
        self.assertNotIn("PLN2", command)
        self.assertEqual(
            command[command.index("--model") + 1],
            "sonnet",
        )

        nonowner = self.con.execute(
            "INSERT INTO directives "
            "(issuer_shell_id,issuer_flavor,kind,payload,target,"
            "sprint_doc_id,unit_id) "
            "VALUES (5,'planner','hold',?,'conductor',100,10)",
            (json.dumps({"reason": "not mine"}),),
        ).lastrowid
        self.con.commit()
        refused = runtime.act(
            self.con, nonowner, 1, launcher=self.launcher
        )
        self.assertEqual(refused["status"], "refused")
        self.assertIn("originating Planner", refused["reason"])

    def test_merge_releases_ready_dependencies_without_booting_planner(self):
        self.set_unit("working")
        self.set_unit("in_review", review_head="abc")
        self.con.execute(
            "INSERT INTO sprint_units "
            "(unit_id,sprint_doc_id,seq,unit_title,dev_shell_id,"
            "reviewer_shell_id,state,depends_on) "
            "VALUES (20,100,'U2','downstream',3,4,'pending','U1')"
        )
        directive_id = self.emit(
            "dev",
            "merged",
            {"pr_number": 7, "head": "abc", "merge_sha": "def"},
        )
        self.con.commit()
        result = runtime.act(
            self.con, directive_id, 1, launcher=self.launcher
        )
        self.assertEqual(result["status"], "executed")
        self.assertEqual(
            self.con.execute(
                "SELECT state FROM sprint_units WHERE unit_id=20"
            ).fetchone()[0],
            "working",
        )
        self.assertEqual(len(result["launches"]), 1)
        self.assertIn("DEV1", result["launches"][0])
        self.assertNotIn("PLN1", result["launches"][0])

    def test_unit_report_boots_planner_only_after_all_units_terminal(self):
        self.set_unit("working")
        first = self.emit("dev", "unit-report", {"shipped": "partial"})
        self.con.commit()
        ordinary = runtime.act(self.con, first, 1, launcher=self.launcher)
        self.assertEqual(ordinary["launches"], [])

        self.con.execute(
            "UPDATE sprint_units SET state='merged' WHERE unit_id=10"
        )
        last = self.emit("dev", "unit-report", {"shipped": "complete"})
        self.con.commit()
        terminal = runtime.act(self.con, last, 1, launcher=self.launcher)
        self.assertEqual(len(terminal["launches"]), 1)
        self.assertIn("PLN1", terminal["launches"][0])


class ConductorSyntheticSprintTests(RuntimeFixture):
    def act_one(self, issuer, kind, payload, *, unit=True):
        directive_id = self.emit(issuer, kind, payload, unit=unit)
        self.con.commit()
        result = runtime.act(self.con, directive_id, 1, launcher=self.launcher)
        self.assertEqual(result["status"], "executed")
        return directive_id

    def test_kickoff_to_merge_to_conformance_to_close_without_human_input(self):
        self.set_declared()
        ids = [
            self.act_one(
                "planner",
                "handoff",
                {"verified": True},
                unit=False,
            ),
            self.act_one(
                "dev",
                "ready-for-review",
                {"pr_number": 7, "head": "abc", "branch": "feat/u1", "checks": "green"},
            ),
            self.act_one(
                "reviewer",
                "review-clean",
                {"head": "abc", "findings": [], "mutation": "failed then passed"},
            ),
            self.act_one(
                "dev", "merged", {"pr_number": 7, "head": "abc", "merge_sha": "def"}
            ),
            self.act_one(
                "reviewer",
                "review-clean",
                {
                    "mode": "conformance",
                    "main_sha": "def",
                    "verdicts": [{"requirement": "all", "verdict": "as-specced"}],
                    "findings": [],
                },
                unit=False,
            ),
        ]
        ids.append(
            self.act_one(
                "planner",
                "close",
                {
                    "main_sha": "def",
                    "conformance_directive_id": ids[-1],
                    "summary": "synthetic sprint complete",
                },
                unit=False,
            )
        )

        unit = self.con.execute(
            "SELECT state,review_head,updated_by_shell_id "
            "FROM sprint_units WHERE unit_id=10"
        ).fetchone()
        self.assertEqual(tuple(unit), ("merged", "abc", 1))
        self.assertEqual(
            self.con.execute(
                "SELECT frozen FROM documents WHERE document_id=100"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.con.execute(
                "SELECT state FROM sprints WHERE sprint_doc_id=100"
            ).fetchone()[0],
            "closed",
        )
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM directives WHERE issuer_flavor='conductor'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM directives WHERE status='executed'"
            ).fetchone()[0],
            6,
        )
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM sentinel_events "
                "WHERE event_kind='conductor-executed'"
            ).fetchone()[0],
            6,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
