#!/usr/bin/env python3
"""Contracts for Subfloor's image-owned Python project mechanisms."""
from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
DOCKERFILE = ROOT / ".super-coder" / "Dockerfile"


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
    def test_terminal_reseed_preserves_fork_customized_dev_kit_skill(self) -> None:
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(
            "CREATE TABLE shells ("
            "shell_id INTEGER PRIMARY KEY, flavor TEXT, "
            "system_prompt TEXT NOT NULL);"
            "CREATE TABLE skills ("
            "skill_id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT, "
            "category TEXT, command TEXT, common INTEGER, content TEXT, "
            "is_deleted INTEGER DEFAULT 0);"
            "CREATE TABLE shell_skills (shell_id INTEGER, skill_id INTEGER);"
            "CREATE TABLE flavor_skills (flavor TEXT, skill_id INTEGER, "
            "UNIQUE(flavor,skill_id));"
            "INSERT INTO skills VALUES "
            "(1,'dev_kit','stale','stale','stale',1,'stale',1);"
        )
        migration = (
            ENGINE / "migrations" / "0241_global_skill_simplification.sql"
        ).read_text()

        con.executescript(migration)
        con.executescript(migration)

        actual = con.execute(
            "SELECT name,description,category,command,common,content,is_deleted "
            "FROM skills WHERE name='dev_kit'"
        ).fetchone()
        self.assertEqual(
            actual,
            (
                "dev_kit",
                "stale",
                "stale",
                "stale",
                1,
                "stale",
                1,
            ),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
