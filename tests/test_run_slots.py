#!/usr/bin/env python3
"""Conductor Step 7 deterministic slot-launch contract."""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
RENDER = ROOT / ".super-coder" / "render"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(RENDER))

import compose  # noqa: E402
import run  # noqa: E402


SCHEMA = """
CREATE TABLE documents (
    document_id INTEGER PRIMARY KEY,
    title TEXT,
    kind TEXT NOT NULL DEFAULT 'doc',
    frozen INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE shells (
    shell_id INTEGER PRIMARY KEY,
    shortname TEXT NOT NULL,
    flavor TEXT
);
CREATE TABLE skills (
    skill_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    content TEXT NOT NULL,
    is_deleted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE sprint_units (
    unit_id INTEGER PRIMARY KEY,
    sprint_doc_id INTEGER NOT NULL,
    seq TEXT NOT NULL,
    unit_title TEXT NOT NULL,
    dev_shell_id INTEGER,
    reviewer_shell_id INTEGER,
    state TEXT NOT NULL,
    depends_on TEXT,
    overlap TEXT,
    branch TEXT,
    pr_number INTEGER
);
"""


def slot_connection() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.executemany(
        "INSERT INTO shells (shell_id,shortname,flavor) VALUES (?,?,?)",
        (
            (1, "PLN1", "planner"),
            (2, "DEV1", "dev"),
            (3, "REV1", "reviewer"),
            (4, "DEV2", "dev"),
        ),
    )
    con.executemany(
        "INSERT INTO skills (name,content) VALUES (?,?)",
        (
            ("plan_sprint", "PLAN BODY\n\n`./sc directives emit kickoff`"),
            ("dev_sprint", "DEV BODY\n\n`./sc directives emit ready-for-review`"),
            ("rev_sprint", "REV BODY\n\n`./sc directives emit review-clean`"),
        ),
    )
    con.execute(
        "INSERT INTO documents (document_id,title) "
        "VALUES (25,'SPRINT: Conductor trial')")
    con.executemany(
        "INSERT INTO sprint_units "
        "(unit_id,sprint_doc_id,seq,unit_title,dev_shell_id,"
        "reviewer_shell_id,state,depends_on,branch,pr_number) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            (10, 25, "U1", "slot launcher", 2, 3, "working",
             None, "feat/u1", 77),
            (11, 25, "U2", "skill rewrite", 4, 3, "pending",
             "U1", None, None),
        ),
    )
    con.commit()
    return con


class SlotContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.con = slot_connection()
        self.addCleanup(self.con.close)

    def shell(self, shell_id: int):
        return self.con.execute(
            "SELECT * FROM shells WHERE shell_id=?", (shell_id,)).fetchone()

    def test_dev_slot_loads_only_its_assigned_unit_and_skill(self) -> None:
        ctx = run.resolve_slot_context(
            self.con, self.shell(2), "dev", 25)
        self.assertEqual(ctx["skill_name"], "dev_sprint")
        self.assertEqual([u["seq"] for u in ctx["units"]], ["U1"])
        self.assertEqual(ctx["units"][0]["branch"], "feat/u1")

    def test_reviewer_can_focus_one_of_several_assigned_units(self) -> None:
        ctx = run.resolve_slot_context(
            self.con, self.shell(3), "rev", 25, "U2")
        self.assertEqual(ctx["skill_name"], "rev_sprint")
        self.assertEqual([u["seq"] for u in ctx["units"]], ["U2"])

    def test_planner_slot_loads_the_live_board_without_a_binding(self) -> None:
        ctx = run.resolve_slot_context(
            self.con, self.shell(1), "plan", 25)
        self.assertEqual(ctx["skill_name"], "plan_sprint")
        self.assertEqual([u["seq"] for u in ctx["units"]], ["U1", "U2"])

    def test_reviewer_without_unit_is_the_close_time_conformance_slot(self) -> None:
        self.con.execute(
            "UPDATE sprint_units SET state='merged' WHERE sprint_doc_id=25")
        ctx = run.resolve_slot_context(
            self.con, self.shell(3), "rev", 25)
        self.assertEqual(ctx["skill_name"], "rev_sprint")
        self.assertEqual([u["state"] for u in ctx["units"]],
                         ["merged", "merged"])

    def test_wrong_flavor_fails_before_a_slot_can_open(self) -> None:
        with self.assertRaisesRegex(
                run.SlotRequestError, "requires a planner shell"):
            run.resolve_slot_context(
                self.con, self.shell(2), "plan", 25)

    def test_unassigned_unit_is_not_bootable(self) -> None:
        with self.assertRaisesRegex(
                run.SlotRequestError, "no live dev assignment"):
            run.resolve_slot_context(
                self.con, self.shell(2), "dev", 25, "U2")

    def test_frozen_sprint_and_retired_skill_fail_closed(self) -> None:
        self.con.execute(
            "UPDATE documents SET frozen=1 WHERE document_id=25")
        with self.assertRaisesRegex(run.SlotRequestError, "is frozen"):
            run.resolve_slot_context(
                self.con, self.shell(2), "dev", 25)

        self.con.execute(
            "UPDATE documents SET frozen=0 WHERE document_id=25")
        self.con.execute(
            "UPDATE skills SET is_deleted=1 WHERE name='dev_sprint'")
        with self.assertRaisesRegex(run.SlotRequestError, "is unavailable"):
            run.resolve_slot_context(
                self.con, self.shell(2), "dev", 25)

    def test_non_sprint_document_fails_closed(self) -> None:
        self.con.execute(
            "UPDATE documents SET kind='spec' WHERE document_id=25")
        with self.assertRaisesRegex(run.SlotRequestError, "is not live"):
            run.resolve_slot_context(
                self.con, self.shell(2), "dev", 25)

    def test_slot_boot_section_inlines_context_and_complete_skill(self) -> None:
        ctx = run.resolve_slot_context(
            self.con, self.shell(2), "dev", 25, "U1")
        rendered = compose.render_slot_directive(ctx)
        self.assertIn("`dev`", rendered)
        self.assertIn("document `25`", rendered)
        self.assertIn("`U1` (unit id `10`) — slot launcher", rendered)
        self.assertIn("branch `feat/u1`", rendered)
        self.assertIn("PR #77", rendered)
        self.assertIn("DEV BODY", rendered)
        self.assertIn("./sc directives emit ready-for-review", rendered)

    def test_slot_default_prompt_names_loaded_skill_and_scope(self) -> None:
        ctx = run.resolve_slot_context(
            self.con, self.shell(3), "rev", 25)
        prompt = run.slot_default_prompt(ctx)
        self.assertIn("rev_sprint", prompt)
        self.assertIn("sprint 25", prompt)
        self.assertIn("U1, U2", prompt)


class SlotArgumentTest(unittest.TestCase):
    def test_slot_and_sprint_are_required_together_before_db_open(self) -> None:
        opened = mock.Mock()
        with mock.patch.object(
                run.sys, "argv",
                ["run.py", "--headless", "DEV1", "--slot", "dev"]), \
                mock.patch.object(run, "open_db", opened), \
                self.assertRaises(SystemExit) as raised:
            run.main()
        self.assertIn(
            "--slot and --sprint are required together",
            str(raised.exception),
        )
        opened.assert_not_called()

    def test_slot_flags_refuse_interactive_boot_before_db_open(self) -> None:
        opened = mock.Mock()
        with mock.patch.object(
                run.sys, "argv",
                ["run.py", "DEV1", "--slot", "dev", "--sprint", "25"]), \
                mock.patch.object(run, "open_db", opened), \
                self.assertRaises(SystemExit) as raised:
            run.main()
        self.assertIn("require ./sc run", str(raised.exception))
        opened.assert_not_called()


class SlotLaunchIntegrationTest(unittest.TestCase):
    class _StopAfterSession(RuntimeError):
        pass

    def test_scripted_slot_boot_stamps_validated_sprint_before_render(self) -> None:
        con = slot_connection()
        self.addCleanup(con.close)
        chosen = dict(con.execute(
            "SELECT * FROM shells WHERE shell_id=2").fetchone())
        recorded = {}

        def stop_after_session(_con, shell_id, lifecycle=None):
            self.assertEqual(shell_id, 2)
            recorded.update(lifecycle or {})
            raise self._StopAfterSession

        defaults = {
            "dev": {
                "default_harness": "claude",
                "models": {"claude": "sonnet"},
            }
        }
        with mock.patch.dict(
                run.os.environ, {"RENDER_ONLY": "1"}, clear=True), \
                mock.patch.object(
                    run.sys, "argv",
                    ["run.py", "--headless", "DEV1", "--harness", "claude",
                     "--slot", "dev", "--sprint", "25", "--unit", "U1"]), \
                mock.patch.object(run, "open_db", return_value=con), \
                mock.patch.object(
                    run, "authenticate", return_value={"user_id": 1}), \
                mock.patch.object(
                    run, "flavor_defaults", return_value=defaults), \
                mock.patch.object(run, "list_shells", return_value=[chosen]), \
                mock.patch.object(run, "pick_shell", return_value=chosen), \
                mock.patch.object(
                    run.shell_liveness, "compute",
                    return_value={"supported": False, "indeterminate": 0}), \
                mock.patch.object(run, "ensure_harness_path"), \
                mock.patch.object(
                    run, "load_adapter",
                    return_value={
                        "harness": "claude",
                        "headless": {
                            "launch": ["claude"],
                            "model_flag": "--model",
                            "effort": {
                                "config_flag": "--config",
                                "config_key": "effort",
                            },
                        },
                    }), \
                mock.patch.object(
                    run, "open_session", side_effect=stop_after_session), \
                self.assertRaises(self._StopAfterSession):
            run.main()

        self.assertEqual(recorded["sprint_ref"], "25")
        self.assertEqual(recorded["harness"], "claude")


if __name__ == "__main__":
    unittest.main(verbosity=2)
