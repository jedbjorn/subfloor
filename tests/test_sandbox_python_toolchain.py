#!/usr/bin/env python3
"""Contracts for Subfloor's image-owned Python project mechanisms."""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
DOCKERFILE = ROOT / ".super-coder" / "Dockerfile"
sys.path.insert(0, str(ENGINE / "scripts"))

import seed_skills  # noqa: E402


class SandboxPythonToolchainTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = DOCKERFILE.read_text()
        cls.folded = cls.text.replace("\\\n", " ")

    def test_uv_and_pytest_are_exact_pinned_baseline_tools(self) -> None:
        self.assertIn(
            'RUN pip install --no-cache-dir "uv==0.11.26" "pytest==9.1.1"',
            self.folded,
        )
        self.assertIn("&& uv --version", self.folded)
        self.assertIn("&& python -m pytest --version", self.folded)

    def test_harness_refresh_does_not_rebuild_python_tools(self) -> None:
        tool_at = self.folded.index(
            'RUN pip install --no-cache-dir "uv==0.11.26" "pytest==9.1.1"'
        )
        epoch_at = self.folded.index("ARG SC_HARNESS_EPOCH=0")
        self.assertLess(tool_at, epoch_at)


class PythonToolingSkillReseedTest(unittest.TestCase):
    def test_terminal_reseed_converges_to_authoritative_dev_kit_skill(self) -> None:
        expected = seed_skills.parse_skill(
            ENGINE / "assets" / "seed" / "skills" / "dev_kit" / "SKILL.md"
        )
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(
            "CREATE TABLE skills ("
            "skill_id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT, "
            "category TEXT, command TEXT, common INTEGER, content TEXT, "
            "is_deleted INTEGER DEFAULT 0);"
            "INSERT INTO skills VALUES "
            "(1,'dev_kit','stale','stale','stale',1,'stale',1);"
        )
        migration = (
            ENGINE / "migrations" / "0228_reseed_python_test_tooling.sql"
        ).read_text()

        con.executescript(migration)
        con.executescript(migration)

        actual = con.execute(
            "SELECT name,description,category,command,common,content,is_deleted "
            "FROM skills ORDER BY skill_id"
        ).fetchall()
        self.assertEqual(
            actual,
            [(
                expected["name"],
                expected["description"],
                expected["category"],
                expected["command"],
                expected["common"],
                expected["content"],
                0,
            )],
        )
        self.assertIn("pinned `uv` + `pytest`", actual[0][5])


if __name__ == "__main__":
    unittest.main(verbosity=2)
