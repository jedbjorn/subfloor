"""Regression coverage for the model-facing Windows VM skill contract."""

from __future__ import annotations

import re
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
SKILLS = ENGINE / "assets" / "skills"
MIGRATION = ENGINE / "migrations" / "0180_reseed_windows_vm_skills.sql"
README = ROOT / "docs" / "README.md"
BROKER_DOC = ENGINE / "docs" / "windows-vm-broker.md"

sys.path.insert(0, str(ENGINE / "scripts"))
import seed_skills


def skill(name: str) -> dict:
    return seed_skills.parse_skill(SKILLS / name / "SKILL.md")


def reset_invocations(content: str) -> list[str]:
    return re.findall(r"\./sc vm reset(?: --[a-z-]+)*", content)


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
        self.assertIn("ask the operator to run `./sc vm-broker-up`", content)
        self.assertIn(
            "run `./sc vm mcp down --json` if GUI transport was used", content
        )
        self.assertIn("are re-joined with single spaces", content)
        self.assertIn("local shell token\n  boundaries are not preserved", content)
        self.assertIn("multiline PowerShell, use `--command-file`", content)
        self.assertIn("paths with spaces, and Unicode reach the broker unchanged", content)

        self.assertEqual(
            reset_invocations(content),
            ["./sc vm reset --off --json", "./sc vm reset --off --json"],
        )

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
        self.assertIn("Python 3.13+", content)
        self.assertIn("`pip install uv`", content)
        self.assertIn("`uvx windows-mcp serve --help` exits zero", content)
        self.assertIn(
            "windows-mcp install --transport streamable-http --host "
            "127.0.0.1 --port 8000",
            content,
        )
        self.assertIn(
            "bound to localhost ONLY (never expose it on the VM network)", content
        )
        self.assertIn("`./sc vm-bake`", content)
        self.assertIn("English-language Windows", content)
        self.assertIn("unless the server itself runs elevated", content)

        self.assertEqual(
            reset_invocations(content), ["./sc vm reset --off --json"]
        )

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

    def test_shipped_docs_use_adapter_injection_and_typed_mcp_lifecycle(self):
        readme = README.read_text()
        broker = BROKER_DOC.read_text()
        combined = f"{readme}\n{broker}".lower()

        self.assertNotIn("claude mcp add", combined)
        self.assertIn("managed `windows-mcp` definition", readme)
        self.assertIn("./sc vm mcp up", readme)
        self.assertIn("adapter-injected windows-mcp", broker)
        self.assertIn("**`./sc vm mcp up`**", broker)
        for line in broker.splitlines():
            if any(
                f"`{name}`" in line
                for name in ("windows_devkit", "windows_vm_gui")
            ):
                self.assertNotIn("curl --unix-socket", line)
        self.assertIn("through typed `./sc vm` commands", broker)


class WindowsSkillReseedTest(unittest.TestCase):
    def test_terminal_reseed_converges_dirty_rows_and_preserves_local_skills(self):
        expected = {
            name: skill(name) for name in ("windows_devkit", "windows_vm_gui")
        }
        expected_ids = {"windows_devkit": 41, "windows_vm_gui": 42}
        con = sqlite3.connect(":memory:")
        try:
            con.executescript(
                "PRAGMA foreign_keys=ON;"
                "CREATE TABLE skills ("
                "skill_id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT, "
                "category TEXT, command TEXT, common INTEGER, content TEXT, "
                "is_deleted INTEGER DEFAULT 0);"
                "CREATE TABLE shell_skills ("
                "shell_id INTEGER, skill_id INTEGER REFERENCES skills(skill_id) "
                "ON DELETE CASCADE, PRIMARY KEY(shell_id,skill_id));"
                "CREATE TABLE flavor_skills ("
                "flavor TEXT, skill_id INTEGER REFERENCES skills(skill_id) "
                "ON DELETE CASCADE, PRIMARY KEY(flavor,skill_id));"
                "INSERT INTO skills "
                "(skill_id,name,description,category,command,common,content,is_deleted) "
                "VALUES "
                "(41,'windows_devkit','stale','wrong','old',1,'legacy reset first',1),"
                "(42,'windows_vm_gui','stale','wrong','old',1,'claude mcp add',1),"
                "(99,'fork_only_skill','local','fork',NULL,0,'bespoke body',0);"
                "INSERT INTO shell_skills VALUES (7,41);"
                "INSERT INTO flavor_skills VALUES ('dev',42);"
            )
            migration = MIGRATION.read_text()
            con.executescript(migration)
            con.executescript(migration)

            rows = con.execute(
                "SELECT skill_id,name,description,category,command,common,content,"
                "is_deleted FROM skills ORDER BY name"
            ).fetchall()
            shell_grants = con.execute(
                "SELECT shell_id,skill_id FROM shell_skills ORDER BY shell_id,skill_id"
            ).fetchall()
            flavor_grants = con.execute(
                "SELECT flavor,skill_id FROM flavor_skills ORDER BY flavor,skill_id"
            ).fetchall()
        finally:
            con.close()

        self.assertEqual(
            rows,
            [
                (
                    99,
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
                        expected_ids[spec["name"]],
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
        self.assertEqual(shell_grants, [(7, 41)])
        self.assertEqual(flavor_grants, [("dev", 42)])


if __name__ == "__main__":
    unittest.main()
