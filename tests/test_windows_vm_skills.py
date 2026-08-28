"""Windows VM mechanisms survive retirement of global fork-specific doctrine."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
SKILLS = ENGINE / "assets" / "skills"
MIGRATION = ENGINE / "migrations" / "0239_global_skill_simplification.sql"
README = ROOT / "docs" / "README.md"
BROKER_DOC = ENGINE / "docs" / "windows-vm-broker.md"
DISPATCH = ENGINE / "scripts" / "dispatch.sh"
RELAY = ENGINE / "scripts" / "vm_mcp_relay.py"
RETIRED = ("configure_winbox", "windows_devkit", "windows_vm_gui")


class WindowsCapabilityBoundaryTest(unittest.TestCase):
    def test_fork_specific_global_skills_are_permanent_tombstones(self) -> None:
        tombstones = set(
            json.loads((ENGINE / "assets" / "skill_tombstones.json").read_text())
        )
        for name in RETIRED:
            with self.subTest(skill=name):
                self.assertFalse((SKILLS / name / "SKILL.md").exists())
                self.assertIn(name, tombstones)

    def test_public_docs_keep_typed_vm_mechanisms_and_local_skill_boundary(self) -> None:
        readme = README.read_text()
        self.assertIn("typed `./sc vm`", readme)
        self.assertIn("managed `windows-mcp` definition", readme)
        self.assertIn("./sc vm mcp up", readme)
        self.assertIn("`fork_skill_design`", readme)
        for name in RETIRED:
            self.assertNotIn(name, readme)

    def test_broker_design_keeps_adapter_injection_and_typed_lifecycle(self) -> None:
        broker = BROKER_DOC.read_text()
        self.assertNotIn("claude mcp add", broker.lower())
        self.assertIn("adapter-injected windows-mcp", broker)
        self.assertIn("**`./sc vm mcp up`**", broker)
        self.assertIn("through typed `./sc vm` commands", broker)

    def test_delivered_relay_uses_managed_adapter_injection(self) -> None:
        combined = f"{DISPATCH.read_text()}\n{RELAY.read_text()}".lower()
        self.assertNotIn("claude mcp add", combined)
        self.assertIn("managed adapter injection", combined)
        self.assertIn("./sc vm mcp up", combined)

    def test_top_level_help_catalogues_public_guest_commands(self) -> None:
        completed = subprocess.run(
            [str(ROOT / "sc"), "help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "SC_DISPATCH": str(DISPATCH)},
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        for command in ("vm push", "vm exec", "vm capture"):
            self.assertIn(f"./sc {command}", completed.stdout)


class WindowsSkillRetirementMigrationTest(unittest.TestCase):
    def test_trailing_migration_removes_rows_and_grants_but_preserves_local(self) -> None:
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
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
            "(40,'configure_winbox','stale','wrong','old',1,'raw ssh',0),"
            "(41,'windows_devkit','stale','wrong','old',1,'legacy reset',0),"
            "(42,'windows_vm_gui','stale','wrong','old',1,'claude mcp add',0),"
            "(99,'fork_windows_lab','local','fork',NULL,0,'bespoke body',0);"
            "INSERT INTO shell_skills VALUES (7,41);"
            "INSERT INTO flavor_skills VALUES ('dev',42),('reviewer',99);"
        )

        migration = MIGRATION.read_text()
        con.executescript(migration)
        con.executescript(migration)

        self.assertEqual(
            con.execute(
                "SELECT name,content,is_deleted FROM skills "
                "WHERE name IN ('configure_winbox','windows_devkit',"
                "'windows_vm_gui','fork_windows_lab') ORDER BY name"
            ).fetchall(),
            [("fork_windows_lab", "bespoke body", 0)],
        )
        self.assertEqual(con.execute("SELECT * FROM shell_skills").fetchall(), [])
        self.assertEqual(
            con.execute(
                "SELECT flavor,skill_id FROM flavor_skills "
                "WHERE skill_id IN (40,41,42,99)"
            ).fetchall(),
            [("reviewer", 99)],
        )
        self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
