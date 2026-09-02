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
sys.path.insert(0, str(ENGINE / "render"))
import compose  # noqa: E402
import engine_paths  # noqa: E402
import map_repo  # noqa: E402
import install  # noqa: E402


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

            (root / ".agents" / "skills" / "generated").mkdir(parents=True)
            (root / ".agents" / "skills" / "generated" / "package.json").write_text(
                '{"dependencies":{"generated-only":"1.0.0"}}'
            )
            (root / ".agents" / "README.md").write_text("host owned\n")
            (root / ".opencode" / "skills" / "generated").mkdir(parents=True)
            (root / ".opencode" / "skills" / "generated" / ".env.example").write_text(
                "GENERATED_ONLY=1\n"
            )
            (root / ".opencode" / "project.json").write_text("{}\n")
            (root / ".codex").mkdir()
            (root / ".codex" / "hooks.json").write_text("{}\n")
            (root / ".codex" / "config.toml").write_text("host_owned = true\n")
            (root / ".claude" / "skills" / "generated").mkdir(parents=True)
            (root / ".claude" / "skills" / "generated" / "SKILL.md").write_text(
                "generated\n"
            )
            (root / ".claude" / "project.json").write_text("{}\n")

            linked = root / ".sc-worktrees" / "dev1"
            linked.mkdir(parents=True)
            (linked / "app.py").write_text("print('linked worktree')\n")
            (linked / "package.json").write_text(
                '{"dependencies":{"express":"1.0.0","uuid":"1.0.0"}}')
            (linked / ".env.example").write_text("LINKED_ONLY=1\n")

            def connect() -> sqlite3.Connection:
                return sqlite3.connect(db_path)

            with mock.patch.object(map_repo, "REPO_ROOT", root), \
                    mock.patch.object(map_repo, "MAP_ROOT", root), \
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

        self.assertEqual(
            [
                ".agents/README.md",
                ".claude/project.json",
                ".codex/config.toml",
                ".env.example",
                ".opencode/project.json",
                "app.py",
                "package.json",
            ],
            paths,
        )
        self.assertEqual([("npm", "express")], dependencies)
        self.assertEqual(["ROOT_ONLY"], env_names)
        self.assertEqual(len(paths), file_count)
        self.assertFalse(any(path.startswith(".sc-worktrees/") for path in paths))

    def test_engine_owned_paths_are_exact_and_host_siblings_remain(self):
        self.assertTrue(
            engine_paths.is_generated_install_path(".agents/skills/git/SKILL.md")
        )
        self.assertTrue(
            engine_paths.is_generated_install_path(".opencode/skills/spec/SKILL.md")
        )
        self.assertTrue(engine_paths.is_generated_install_path(".codex/hooks.json"))
        self.assertFalse(engine_paths.is_generated_install_path(".agents/README.md"))
        self.assertFalse(engine_paths.is_generated_install_path(".opencode/project.json"))
        self.assertFalse(engine_paths.is_generated_install_path(".codex/config.toml"))

        skip_dirs = set(map_repo.SKIP_DIRS)
        skip_dirs.discard(".super-coder")
        for path in engine_paths.GENERATED_INSTALL_PATHS:
            self.assertTrue(map_repo.path_is_skipped(path.parts, skip_dirs, set()))
        host_paths = (
            ".agents/README.md",
            ".opencode/project.json",
            ".codex/config.toml",
        )
        for path in host_paths:
            self.assertFalse(map_repo.path_is_skipped(Path(path).parts, skip_dirs, set()))


class RepositoryRootRenderTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        self.con.execute(
            "INSERT INTO dr_repo (repo_id, root, default_branch, mapped_at) "
            "VALUES (1, '/tmp/repo', 'main', '2026-08-20T00:00:00')"
        )

    def tearDown(self):
        self.con.close()

    def test_root_files_render_separately_from_nested_unsectioned_files(self):
        self.con.executemany(
            "INSERT INTO dr_filepath (path) VALUES (?)",
            [("README.md",), ("Makefile",), ("src/app.py",), ("loose/item.py",)],
        )
        self.con.execute(
            "INSERT INTO dr_section (name, path_prefix, description, sort_order) "
            "VALUES ('Source', 'src/', 'Application source', 1)"
        )

        rendered = compose.render_connections(self.con)

        self.assertIn(
            "**Repository Root** · `./` · 2 files — Top-level project entrypoints and metadata",
            rendered,
        )
        self.assertIn("**Source** · `src/` · 1 files — Application source", rendered)
        self.assertIn("_other / unsectioned_ · 1 files", rendered)
        self.assertNotIn("_other / unsectioned_ · 3 files", rendered)

    def test_no_empty_synthetic_section_is_persisted(self):
        self.con.execute("INSERT INTO dr_filepath (path) VALUES ('nested/file.py')")

        rendered = compose.render_connections(self.con)

        self.assertNotIn("Repository Root", rendered)
        section_count = self.con.execute("SELECT COUNT(*) FROM dr_section").fetchone()[0]
        self.assertEqual(0, section_count)
        self.assertIn("_other / unsectioned_ · 1 files", rendered)


class ExternalWorkProjectTest(unittest.TestCase):
    def test_declared_work_repo_is_the_map_target(self):
        with tempfile.TemporaryDirectory() as td:
            temp_root = Path(td)
            home = temp_root / "home"
            project = temp_root / "project"
            home.mkdir()
            project.mkdir()
            (project / "app.py").write_text("print('project')\n")
            (home / "home_only.py").write_text("print('home')\n")
            with mock.patch.object(install, "work_repo", return_value=str(project)):
                self.assertEqual(project, map_repo._resolve_map_root())
            db_path = temp_root / "map.db"
            con = sqlite3.connect(db_path)
            con.executescript(SCHEMA)
            con.close()

            def connect() -> sqlite3.Connection:
                return sqlite3.connect(db_path)

            with mock.patch.object(map_repo, "REPO_ROOT", home), \
                    mock.patch.object(map_repo, "MAP_ROOT", project), \
                    mock.patch.object(map_repo, "CONFIG_PATH", home / "config.json"), \
                    mock.patch.object(map_repo, "CONFIG_PATH_LEGACY", home / "legacy.json"), \
                    mock.patch.object(map_repo, "is_source_repo", return_value=False), \
                    mock.patch.object(map_repo, "git", return_value=""), \
                    mock.patch.object(map_repo.artifact_policy, "prepare_local_state"), \
                    mock.patch.object(map_repo.map_db, "connect", side_effect=connect):
                self.assertEqual(0, map_repo.main())

            con = sqlite3.connect(db_path)
            paths = [row[0] for row in con.execute(
                "SELECT path FROM dr_filepath ORDER BY path")]
            mapped_root = con.execute(
                "SELECT root FROM dr_repo WHERE repo_id=1").fetchone()[0]
            con.close()

        self.assertEqual(["app.py"], paths)
        self.assertEqual(str(project), mapped_root)

    def test_work_repo_expands_home_directory(self):
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td) / ".super-coder"
            config_dir.mkdir()
            (config_dir / "instance.json").write_text(
                '{"work_repo": "~/Repos/super-coder"}')
            with mock.patch.object(install, "ENGINE", config_dir):
                self.assertEqual(
                    str(Path("~/Repos/super-coder").expanduser()),
                    install.work_repo(),
                )


if __name__ == "__main__":
    unittest.main()
