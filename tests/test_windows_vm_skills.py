"""Regression coverage for the model-facing Windows VM skill contract."""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
SKILLS = ENGINE / "assets" / "skills"
MIGRATION = ENGINE / "migrations" / "0180_reseed_windows_vm_skills.sql"

sys.path.insert(0, str(ENGINE / "scripts"))
import seed_skills


def skill(name: str) -> dict:
    return seed_skills.parse_skill(SKILLS / name / "SKILL.md")


class WindowsSkillWorkflowTest(unittest.TestCase):
    def test_devkit_uses_supplied_state_start_if_off_and_end_only_reset(self):
        content = skill("windows_devkit")["content"]

        status = content.index("./sc vm status --json")
        start = content.index("./sc vm start --json")
        reset = content.index("./sc vm reset --off --json")
        self.assertLess(status, start)
        self.assertLess(start, reset)
        self.assertIn(
            "Assume the operator supplied a running VM with the testing "
            "application open.",
            content,
        )
        self.assertIn("There is no reset at the beginning or during a test.", content)
        self.assertIn("Do not automatically retry", content)
        self.assertIn("Report the test result and cleanup result separately.", content)

        reset_commands = [
            line.strip()
            for line in content.splitlines()
            if line.strip().startswith("./sc vm reset")
        ]
        self.assertEqual(reset_commands, ["./sc vm reset --off --json"])

    def test_gui_uses_adapter_tools_and_ordered_mcp_cleanup(self):
        content = skill("windows_vm_gui")["content"]

        status = content.index("./sc vm status --json")
        start = content.index("./sc vm start --json")
        mcp_up = content.index("./sc vm mcp up --json")
        mcp_down = content.index("./sc vm mcp down --json")
        reset = content.index("./sc vm reset --off --json")
        self.assertLess(status, start)
        self.assertLess(start, mcp_up)
        self.assertLess(mcp_up, mcp_down)
        self.assertLess(mcp_down, reset)
        self.assertIn("Claude, Codex, and OpenCode", content)
        self.assertIn("Kimi and Vibe are unsupported", content)
        self.assertIn("The harness tool list may be fixed at launch.", content)
        self.assertIn("Call `Snapshot` first.", content)
        self.assertIn("There is no opening or mid-test reset.", content)

    def test_skills_forbid_legacy_raw_and_persistent_registration_paths(self):
        combined = "\n".join(
            skill(name)["content"] for name in ("windows_devkit", "windows_vm_gui")
        ).lower()

        for forbidden in (
            "curl --unix-socket",
            "vm-broker-sock",
            "vm-mcp-relay",
            "claude mcp add",
            '"running":false',
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, combined)


class WindowsSkillReseedTest(unittest.TestCase):
    def test_terminal_reseed_converges_dirty_rows_and_preserves_local_skills(self):
        expected = {
            name: skill(name) for name in ("windows_devkit", "windows_vm_gui")
        }
        con = sqlite3.connect(":memory:")
        try:
            con.executescript(
                "CREATE TABLE skills ("
                "skill_id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT, "
                "category TEXT, command TEXT, common INTEGER, content TEXT, "
                "is_deleted INTEGER DEFAULT 0);"
                "INSERT INTO skills "
                "(name,description,category,command,common,content,is_deleted) VALUES "
                "('windows_devkit','stale','wrong','old',1,'legacy reset first',1),"
                "('windows_vm_gui','stale','wrong','old',1,'claude mcp add',1),"
                "('fork_only_skill','local','fork',NULL,0,'bespoke body',0);"
            )
            migration = MIGRATION.read_text()
            con.executescript(migration)
            con.executescript(migration)

            rows = con.execute(
                "SELECT name,description,category,command,common,content,is_deleted "
                "FROM skills ORDER BY name"
            ).fetchall()
        finally:
            con.close()

        self.assertEqual(
            rows,
            [
                (
                    "fork_only_skill",
                    "local",
                    "fork",
                    None,
                    0,
                    "bespoke body",
                    0,
                ),
                *[
                    (
                        spec["name"],
                        spec["description"],
                        spec["category"],
                        spec["command"],
                        spec["common"],
                        spec["content"],
                        0,
                    )
                    for spec in expected.values()
                ],
            ],
        )


if __name__ == "__main__":
    unittest.main()
