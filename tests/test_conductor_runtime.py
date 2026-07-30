"""Conductor Step 8 transition, wake, doctor, and synthetic-sprint gates."""

from __future__ import annotations

import concurrent.futures
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


class ConductorConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="sc_conductor_config_")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "instance.json"

    def test_existing_instance_without_block_uses_operational_default(self):
        self.path.write_text('{"repo":"legacy-fork","port":8800}\n')

        self.assertEqual(
            runtime.load_config(self.path),
            runtime.ConductorConfig(
                True, "CON1", runtime.DEFAULT_CONDUCTOR_MODEL
            ),
        )

    def test_missing_instance_config_still_fails_closed(self):
        self.assertEqual(
            runtime.load_config(self.path),
            runtime.ConductorConfig(),
        )

    def test_explicit_disabled_block_remains_disabled(self):
        self.path.write_text(json.dumps({
            "conductor": {
                "enabled": False,
                "shell": "CON2",
                "model": "openai/custom",
            }
        }))

        self.assertEqual(
            runtime.load_config(self.path),
            runtime.ConductorConfig(False, "CON2", "openai/custom"),
        )

    def test_reconcile_config_persists_default_without_clobbering(self):
        original = {"repo": "legacy-fork", "port": 8800}
        self.path.write_text(json.dumps(original))

        self.assertTrue(runtime.reconcile_config(self.path))
        configured = json.loads(self.path.read_text())
        self.assertEqual(
            configured["conductor"],
            {
                "enabled": True,
                "shell": "CON1",
                "model": runtime.DEFAULT_CONDUCTOR_MODEL,
            },
        )
        self.assertEqual(
            {key: configured[key] for key in original},
            original,
        )

        configured["conductor"] = {"enabled": False}
        self.path.write_text(json.dumps(configured))
        self.assertFalse(runtime.reconcile_config(self.path))
        self.assertEqual(
            json.loads(self.path.read_text())["conductor"],
            {"enabled": False},
        )


class RuntimeFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sc_conductor8_")
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "runtime.db"
        self.con = build_db(self.db_path)
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

    def test_browser_binding_limits_conductor_boot_to_its_exact_sprint(self):
        sprint_directive = self.emit(
            "system",
            "stall",
            {"evidence": "bounded"},
        )
        other_directive = self.con.execute(
            "INSERT INTO directives "
            "(issuer_flavor,kind,payload,target) "
            "VALUES ('system','stall','{}','conductor')"
        ).lastrowid
        self.con.commit()
        shell = self.con.execute(
            "SELECT * FROM shells WHERE shell_id=1"
        ).fetchone()
        context = {
            "binding_id": 901,
            "role": "conductor",
            "lifecycle": "persistent",
            "slot": "CON1",
            "sprint_doc_id": 100,
            "sprint_title": "SPRINT: synthetic",
            "spec_doc_id": 99,
            "spec_title": "Synthetic spec",
            "skill_body": "SPRINT CONDUCTOR SKILL BODY",
        }

        rendered = runtime.render_boot(
            self.con,
            shell,
            slot_context=context,
        )

        self.assertIn("## Browser Sprint binding", rendered)
        self.assertIn("`sc directives list --status pending --sprint 100`", rendered)
        self.assertIn(f"| {sprint_directive} |", rendered)
        self.assertNotIn(f"| {other_directive} |", rendered)
        self.assertIn("SPRINT CONDUCTOR SKILL BODY", rendered)

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

    def test_process_wake_path_is_retired(self):
        self.assertFalse(hasattr(runtime, "maybe_wake"))
        self.assertFalse(hasattr(runtime, "_launch_is_live"))

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
            "sprint-armed",
            "pending",
            {},
            False,
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
        (
            "system",
            "worker-failed",
            "working",
            {
                "binding_id": 42,
                "role": "developer",
                "run_outcome": "unknown",
                "assignment_outcome": "unknown",
                "error_code": "HARNESS_OUTCOME_UNKNOWN",
            },
            True,
        ),
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
                    sprint_state = "active"
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
                    result = runtime.act(con, directive_id, 1)
                    self.assertEqual(result["status"], "executed")
                    for assignment in result["assignments"]:
                        self.assertEqual(
                            con.execute(
                                "SELECT COUNT(*) FROM "
                                "sprint_conversation_bindings "
                                "WHERE conversation_id=?",
                                (assignment["conversation_id"],),
                            ).fetchone()[0],
                            1,
                        )
                        message = con.execute(
                            "SELECT body FROM conversation_messages "
                            "WHERE conversation_id=? "
                            "ORDER BY message_id LIMIT 1",
                            (assignment["conversation_id"],),
                        ).fetchone()
                        packet = json.loads(message["body"])
                        self.assertIn(
                            "emitting exactly one appropriate directive",
                            packet["completion_contract"],
                        )
                        self.assertIn(
                            "immediately send exactly one correlated",
                            packet["completion_contract"],
                        )
                        if kind == "sprint-armed":
                            self.assertIn(
                                "emit ready-for-review as the one workflow "
                                "directive",
                                packet["instruction"],
                            )
                            self.assertIn(
                                "Do not emit unit-report as the workflow "
                                "directive",
                                packet["instruction"],
                            )
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
        result = runtime.act(self.con, directive_id, 1)
        self.assertEqual(result["status"], "refused")
        self.assertIn("payload.pr_number", result["reason"])
        self.assertEqual(result["escalation"]["role"], "planner")
        self.assertEqual(result["escalation"]["slot"], "PLN1")
        self.assertEqual(
            self.con.execute(
                "SELECT status FROM directives WHERE directive_id=?",
                (directive_id,),
            ).fetchone()[0],
            "refused",
        )

    def test_report_only_unit_is_reviewed_then_terminal_and_reported(self):
        self.set_unit("working")
        ready_id = self.emit(
            "dev",
            "ready-for-review",
            {
                "report_only": True,
                "pr_number": None,
                "head": "main-abc",
                "branch": None,
                "checks": "report-only",
                "verification": ["re-executed documented command"],
            },
        )
        self.con.commit()

        ready = runtime.act(self.con, ready_id, 1)

        self.assertEqual(ready["status"], "executed")
        self.assertEqual(len(ready["assignments"]), 1)
        self.assertEqual(ready["assignments"][0]["role"], "reviewer")
        unit = self.con.execute(
            "SELECT state,pr_number,branch FROM sprint_units WHERE unit_id=10"
        ).fetchone()
        self.assertEqual(tuple(unit), ("in_review", None, None))

        clean_id = self.emit(
            "reviewer",
            "review-clean",
            {"head": "main-abc", "findings": [], "mutation": "failed then passed"},
        )
        self.con.commit()

        clean = runtime.act(self.con, clean_id, 1)

        self.assertEqual(clean["status"], "executed")
        self.assertEqual(len(clean["assignments"]), 1)
        prompt = self.con.execute(
            "SELECT body FROM conversation_messages WHERE conversation_id=?",
            (clean["assignments"][0]["conversation_id"],),
        ).fetchone()[0]
        self.assertIn('"report_only": true', prompt)
        unit = self.con.execute(
            "SELECT state,review_head FROM sprint_units WHERE unit_id=10"
        ).fetchone()
        self.assertEqual(tuple(unit), ("merged", "main-abc"))

        report_id = self.emit(
            "dev",
            "unit-report",
            {"shipped": "verified requested state already present"},
        )
        self.con.commit()
        report = runtime.act(self.con, report_id, 1)
        self.assertEqual(report["status"], "executed")
        self.assertEqual(len(report["assignments"]), 1)
        self.assertEqual(report["assignments"][0]["slot"], "PLN1")
        planner_prompt = self.con.execute(
            "SELECT body FROM conversation_messages "
            "WHERE conversation_id=? ORDER BY message_id LIMIT 1",
            (report["assignments"][0]["conversation_id"],),
        ).fetchone()
        packet = json.loads(planner_prompt["body"])
        self.assertIn(
            "This is a conformance handoff, not a question",
            packet["instruction"],
        )
        self.assertIn("do not emit answer", packet["instruction"])

    def test_report_only_requires_explicit_null_contract_and_evidence(self):
        cases = (
            {
                "report_only": True,
                "pr_number": 7,
                "head": "abc",
                "branch": None,
                "checks": "report-only",
                "verification": ["gate"],
            },
            {
                "report_only": True,
                "pr_number": None,
                "head": "abc",
                "branch": "feat/not-report-only",
                "checks": "report-only",
                "verification": ["gate"],
            },
            {
                "report_only": True,
                "pr_number": None,
                "head": "abc",
                "branch": None,
                "checks": "green",
                "verification": ["gate"],
            },
            {
                "report_only": True,
                "pr_number": None,
                "head": "abc",
                "branch": None,
                "checks": "report-only",
                "verification": [],
            },
        )
        for payload in cases:
            with self.subTest(payload=payload):
                self.set_unit("working")
                directive_id = self.emit("dev", "ready-for-review", payload)
                self.con.commit()
                result = runtime.act(self.con, directive_id, 1)
                self.assertEqual(result["status"], "refused")
                self.assertEqual(
                    self.con.execute(
                        "SELECT state FROM sprint_units WHERE unit_id=10"
                    ).fetchone()[0],
                    "working",
                )

    def test_retask_returns_reviewed_unit_to_working_and_clears_review_head(self):
        self.set_unit("working")
        self.set_unit("in_review", review_head="approved-old-head")
        directive_id = self.emit(
            "planner",
            "re-task",
            {
                "to": "DEV1",
                "instruction": "revise the implementation",
                "reason": "new evidence",
            },
        )
        self.con.commit()

        result = runtime.act(self.con, directive_id, 1)

        self.assertEqual(result["status"], "executed")
        unit = self.con.execute(
            "SELECT state,review_head FROM sprint_units WHERE unit_id=10"
        ).fetchone()
        self.assertEqual(tuple(unit), ("working", None))
        self.assertEqual(result["assignments"][0]["slot"], "DEV1")

    def test_retask_refuses_merged_unit_before_worker_launch(self):
        self.set_unit("working")
        self.set_unit("merged", review_head="approved-head")
        directive_id = self.emit(
            "planner",
            "re-task",
            {
                "to": "DEV1",
                "instruction": "revise the merged implementation",
                "reason": "integrated conformance finding",
            },
        )
        self.con.commit()

        result = runtime.act(self.con, directive_id, 1)

        self.assertEqual(result["status"], "refused")
        self.assertIn("add a follow-up unit", result["reason"])
        self.assertEqual(
            self.con.execute(
                "SELECT state FROM sprint_units WHERE unit_id=10"
            ).fetchone()[0],
            "merged",
        )
        self.assertEqual(result["escalation"]["slot"], "PLN1")

    def test_refusal_persists_when_planner_route_is_unavailable(self):
        self.set_unit("working")
        self.set_unit("merged", review_head="approved-head")
        directive_id = self.emit(
            "planner",
            "re-task",
            {
                "to": "DEV1",
                "instruction": "revise the merged implementation",
                "reason": "integrated conformance finding",
            },
        )
        self.con.commit()

        with mock.patch.object(
            run,
            "load_adapter",
            side_effect=ValueError("route temporarily unavailable"),
        ):
            result = runtime.act(self.con, directive_id, 1)

        self.assertEqual(result["status"], "refused")
        self.assertIn("add a follow-up unit", result["reason"])
        self.assertEqual(
            result["escalation"],
            {"error": "target shell 'PLN1' route is not runnable: "
             "route temporarily unavailable"},
        )
        row = self.con.execute(
            "SELECT status,refusal_reason FROM directives "
            "WHERE directive_id=?",
            (directive_id,),
        ).fetchone()
        self.assertEqual(row["status"], "refused")
        self.assertIn("add a follow-up unit", row["refusal_reason"])

    def test_assignment_persistence_failure_rolls_back_the_whole_action(self):
        self.set_unit("working")
        self.set_unit("blocked")
        directive_id = self.emit(
            "planner",
            "answer",
            {
                "to": "DEV1",
                "question_directive_id": 42,
                "answer": "resume the unit",
            },
        )
        self.con.commit()
        with (
            mock.patch.object(
                runtime.sprint_conversations,
                "create_sprint_conversation",
                side_effect=RuntimeError("forced persistence failure"),
            ),
            self.assertRaisesRegex(RuntimeError, "forced persistence failure"),
        ):
            runtime.act(self.con, directive_id, 1)

        self.assertEqual(
            self.con.execute(
                "SELECT state FROM sprint_units WHERE unit_id=10"
            ).fetchone()[0],
            "blocked",
        )
        row = self.con.execute(
            "SELECT status,refusal_reason FROM directives "
            "WHERE directive_id=?",
            (directive_id,),
        ).fetchone()
        self.assertEqual(tuple(row), ("pending", None))
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM conversations WHERE mode='sprint'"
            ).fetchone()[0],
            0,
        )

    def test_action_does_no_external_work_inside_the_transaction(self):
        directive_id = self.emit(
            "planner",
            "kickoff",
            {"to": "DEV1", "instruction": "build the bounded unit"},
        )
        self.con.commit()
        load_adapter = run.load_adapter
        transaction_states = []

        def checked_adapter(harness):
            transaction_states.append(self.con.in_transaction)
            return load_adapter(harness)

        with (
            mock.patch.object(run, "load_adapter", side_effect=checked_adapter),
            mock.patch.object(
                runtime.subprocess,
                "Popen",
                side_effect=AssertionError("action attempted process launch"),
            ),
        ):
            result = runtime.act(self.con, directive_id, 1)

        self.assertEqual(result["status"], "executed")
        self.assertTrue(transaction_states)
        self.assertEqual(set(transaction_states), {False})
        prompt = self.con.execute(
            "SELECT body FROM conversation_messages WHERE conversation_id=?",
            (result["conversation_ids"][0],),
        ).fetchone()[0]
        self.assertNotIn("await-sprint-active", prompt)

    def test_parallel_duplicate_action_commits_one_complete_assignment(self):
        directive_id = self.emit(
            "planner",
            "kickoff",
            {"to": "DEV1", "instruction": "build the bounded unit"},
        )
        self.con.commit()

        def act_once():
            con = runtime.db_driver.connect(self.db_path)
            try:
                return runtime._act_locked(con, directive_id, 1)
            finally:
                con.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = [
                future.result(timeout=10)
                for future in (pool.submit(act_once), pool.submit(act_once))
            ]

        self.assertEqual([item["status"] for item in results], [
            "executed",
            "executed",
        ])
        self.assertEqual(
            sum(bool(item.get("replayed")) for item in results),
            1,
        )
        self.assertEqual(
            self.con.execute(
                "SELECT state FROM sprint_units WHERE unit_id=10"
            ).fetchone()[0],
            "working",
        )
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_conversation_bindings "
                "WHERE source_directive_id=?",
                (directive_id,),
            ).fetchone()[0],
            1,
        )
        conversation_id = self.con.execute(
            "SELECT conversation_id FROM sprint_conversation_bindings "
            "WHERE source_directive_id=?",
            (directive_id,),
        ).fetchone()[0]
        self.assertEqual(
            tuple(self.con.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM conversation_messages "
                " WHERE conversation_id=?),"
                "(SELECT COUNT(*) FROM conversation_outbox "
                " WHERE conversation_id=?),"
                "(SELECT COUNT(*) FROM conversation_events "
                " WHERE conversation_id=?)",
                (conversation_id, conversation_id, conversation_id),
            ).fetchone()),
            (1, 1, 2),
        )

    def test_runtime_backstop_refuses_retired_handoff(self):
        self.set_declared()
        directive_id = self.emit("planner", "handoff", {}, unit=False)
        self.con.commit()

        result = runtime.act(self.con, directive_id, 1)

        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["reason"], "no transition for planner:handoff")
        self.assertNotIn("launches", result)

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

        result = runtime.act(self.con, directive_id, 1)

        self.assertEqual(result["status"], "refused")
        unit = self.con.execute(
            "SELECT state,pr_number,branch FROM sprint_units WHERE unit_id=10"
        ).fetchone()
        self.assertEqual(tuple(unit), ("working", 7, "feat/u1"))

    def test_non_conductor_cannot_act(self):
        directive_id = self.emit("dev", "unit-report", {"shipped": "x"})
        self.con.commit()
        with self.assertRaisesRegex(PermissionError, "conductor"):
            runtime.act(self.con, directive_id, 2)

    def test_declared_zero_unit_and_dependency_cycle_arming_is_refused(self):
        self.set_declared()
        self.con.execute("DELETE FROM sprint_units WHERE sprint_doc_id=100")
        self.con.commit()
        with self.assertRaisesRegex(runtime.DirectiveRefused, "non-empty"):
            runtime.validate_arm_board(self.con, 100)
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
        self.con.commit()
        with self.assertRaisesRegex(runtime.DirectiveRefused, "dependency cycle"):
            runtime.validate_arm_board(self.con, 100)

    def test_two_planners_route_questions_only_to_recorded_owner(self):
        self.set_unit("working")
        directive_id = self.emit(
            "dev",
            "ask-planner",
            {"question": "choose", "alternatives": ["a", "b"]},
        )
        self.con.commit()
        result = runtime.act(self.con, directive_id, 1)
        self.assertEqual(result["status"], "executed")
        self.assertEqual(len(result["assignments"]), 1)
        self.assertEqual(result["assignments"][0]["slot"], "PLN1")
        self.assertEqual(
            self.con.execute(
                "SELECT model FROM conversations WHERE conversation_id=?",
                (result["assignments"][0]["conversation_id"],),
            ).fetchone()[0],
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
        refused = runtime.act(self.con, nonowner, 1)
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
        result = runtime.act(self.con, directive_id, 1)
        self.assertEqual(result["status"], "executed")
        self.assertEqual(
            self.con.execute(
                "SELECT state FROM sprint_units WHERE unit_id=20"
            ).fetchone()[0],
            "working",
        )
        self.assertEqual(len(result["assignments"]), 1)
        self.assertEqual(result["assignments"][0]["slot"], "DEV1")
        prompt = self.con.execute(
            "SELECT body FROM conversation_messages WHERE conversation_id=?",
            (result["assignments"][0]["conversation_id"],),
        ).fetchone()[0]
        self.assertIn(
            "emit ready-for-review as the one workflow directive",
            json.loads(prompt)["instruction"],
        )

    def test_unit_report_boots_planner_only_after_all_units_terminal(self):
        self.set_unit("working")
        first = self.emit("dev", "unit-report", {"shipped": "partial"})
        self.con.commit()
        ordinary = runtime.act(self.con, first, 1)
        self.assertEqual(ordinary["assignments"], [])

        self.con.execute(
            "UPDATE sprint_units SET state='merged' WHERE unit_id=10"
        )
        last = self.emit("dev", "unit-report", {"shipped": "complete"})
        self.con.commit()
        terminal = runtime.act(self.con, last, 1)
        self.assertEqual(len(terminal["assignments"]), 1)
        self.assertEqual(terminal["assignments"][0]["slot"], "PLN1")

    def test_conformance_kickoff_is_unitless_and_typed_conformance(self):
        self.set_unit("working")
        self.set_unit("in_review")
        self.set_unit("merged")
        linked = self.emit(
            "planner",
            "kickoff",
            {
                "to": "REV1",
                "mode": "conformance",
                "main_sha": "abc",
                "scope": "all requirements",
                "ratified_deviations": [],
            },
        )
        self.con.commit()
        refused = runtime.act(self.con, linked, 1)
        self.assertEqual(refused["status"], "refused")
        self.assertIn("must be unitless", refused["reason"])

        unitless = self.emit(
            "planner",
            "kickoff",
            {
                "to": "REV1",
                "mode": "conformance",
                "main_sha": "abc",
                "scope": "all requirements",
                "ratified_deviations": [],
            },
            unit=False,
        )
        self.con.commit()
        result = runtime.act(self.con, unitless, 1)
        self.assertEqual(result["status"], "executed")
        binding = self.con.execute(
            "SELECT role,required_result_kind FROM "
            "sprint_conversation_bindings WHERE conversation_id=?",
            (result["assignments"][0]["conversation_id"],),
        ).fetchone()
        self.assertEqual(tuple(binding), ("conformance", "conformance-verdict"))


class ConductorSyntheticSprintTests(RuntimeFixture):
    def act_one(self, issuer, kind, payload, *, unit=True):
        directive_id = self.emit(issuer, kind, payload, unit=unit)
        self.con.commit()
        result = runtime.act(self.con, directive_id, 1)
        self.assertEqual(result["status"], "executed")
        return directive_id

    def test_kickoff_to_merge_to_conformance_to_close_without_human_input(self):
        self.set_declared()
        runtime.validate_arm_board(self.con, 100)
        runtime.sprint_lifecycle.transition(self.con, 100, "active")
        self.con.execute(
            "UPDATE sprint_units SET state='working' WHERE unit_id=10"
        )
        self.con.execute(
            "INSERT INTO conversations "
            "(conversation_id,shell_id,mode,sprint_doc_id,harness,worktree,"
            "state,creation_idempotency_key,creation_request_hash) "
            "VALUES ('cv_synthetic_conductor',1,'sprint',100,'opencode',"
            "'/tmp/conductor','idle','synthetic-conductor',"
            "'synthetic-conductor-hash')"
        )
        self.con.execute(
            "INSERT INTO sprint_conversation_bindings "
            "(conversation_id,sprint_doc_id,role,lifecycle,slot,state,"
            "started_at) VALUES "
            "('cv_synthetic_conductor',100,'conductor','persistent','CON1',"
            "'active',datetime('now'))"
        )
        self.con.commit()
        ids = [
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
            tuple(self.con.execute(
                "SELECT c.state,b.state,b.outcome "
                "FROM conversations c JOIN sprint_conversation_bindings b "
                "ON b.conversation_id=c.conversation_id "
                "WHERE c.conversation_id='cv_synthetic_conductor'"
            ).fetchone()),
            ("closed", "terminal", "closed"),
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
            5,
        )
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM sentinel_events "
                "WHERE event_kind='conductor-executed'"
            ).fetchone()[0],
            5,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
