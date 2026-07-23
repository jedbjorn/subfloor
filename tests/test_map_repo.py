#!/usr/bin/env python3
"""Regression coverage for the repo map's unconditional skip surface."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
import map_repo  # noqa: E402


SCHEMA = """
CREATE TABLE dr_repo (
    repo_id INTEGER PRIMARY KEY,
    name TEXT,
    root TEXT,
    remote TEXT,
    vcs TEXT,
    default_branch TEXT,
    file_count INTEGER,
    mapped_at TEXT
);
CREATE TABLE dr_filepath (
    path TEXT PRIMARY KEY,
    ext TEXT,
    lang TEXT,
    role TEXT,
    bytes INTEGER,
    lines INTEGER,
    desc TEXT
);
CREATE TABLE dr_dependency (
    manager TEXT,
    name TEXT,
    version TEXT,
    kind TEXT,
    source_file TEXT
);
CREATE TABLE dr_env (name TEXT, source_file TEXT);
CREATE TABLE dr_section (
    name TEXT PRIMARY KEY,
    path_prefix TEXT,
    description TEXT,
    sort_order INTEGER
);
"""


class WorktreeSkipTest(unittest.TestCase):
    def test_linked_worktree_is_absent_from_every_core_projection(self):
        with tempfile.TemporaryDirectory() as td:
            temp_root = Path(td)
            root = temp_root / "repo"
            root.mkdir()
            db_path = temp_root / "map.db"
            con = sqlite3.connect(db_path)
            con.executescript(SCHEMA)
            con.close()

            (root / "app.py").write_text("print('selected checkout')\n")
            (root / "package.json").write_text(
                '{"dependencies":{"express":"1.0.0"}}')
            (root / ".env.example").write_text("ROOT_ONLY=1\n")

            linked = root / ".sc-worktrees" / "dev1"
            linked.mkdir(parents=True)
            (linked / "app.py").write_text("print('linked worktree')\n")
            (linked / "package.json").write_text(
                '{"dependencies":{"express":"1.0.0","uuid":"1.0.0"}}')
            (linked / ".env.example").write_text("LINKED_ONLY=1\n")

            def connect() -> sqlite3.Connection:
                return sqlite3.connect(db_path)

            with mock.patch.object(map_repo, "REPO_ROOT", root), \
                    mock.patch.object(map_repo, "ENGINE", root / ".super-coder"), \
                    mock.patch.object(map_repo, "CONFIG_PATH",
                                      root / ".sc-state" / "map.config.json"), \
                    mock.patch.object(map_repo, "CONFIG_PATH_LEGACY",
                                      root / ".super-coder" / "map.config.json"), \
                    mock.patch.object(map_repo, "is_source_repo",
                                      return_value=False), \
                    mock.patch.object(map_repo, "git", return_value=""), \
                    mock.patch.object(map_repo.map_db, "connect", side_effect=connect):
                self.assertEqual(0, map_repo.main())

            con = sqlite3.connect(db_path)
            paths = [row[0] for row in con.execute(
                "SELECT path FROM dr_filepath ORDER BY path")]
            dependencies = con.execute(
                "SELECT manager, name FROM dr_dependency ORDER BY name").fetchall()
            env_names = [row[0] for row in con.execute(
                "SELECT name FROM dr_env ORDER BY name")]
            file_count = con.execute(
                "SELECT file_count FROM dr_repo WHERE repo_id=1").fetchone()[0]
            con.close()

        self.assertEqual([".env.example", "app.py", "package.json"], paths)
        self.assertEqual([("npm", "express")], dependencies)
        self.assertEqual(["ROOT_ONLY"], env_names)
        self.assertEqual(len(paths), file_count)
        self.assertFalse(any(path.startswith(".sc-worktrees/") for path in paths))


if __name__ == "__main__":
    unittest.main()
