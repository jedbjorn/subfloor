#!/usr/bin/env python3
"""Hermetic tests for Visual QA's fork distribution surfaces."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
TEMPLATES = ENGINE / "templates" / "fork"
sys.path.insert(0, str(ENGINE / "scripts"))

import engine_manifest  # noqa: E402
import init_fork  # noqa: E402
import install  # noqa: E402
import update  # noqa: E402


class VisualQaTemplateTest(unittest.TestCase):
    def test_workflow_is_the_fixed_managed_shim(self):
        text = (TEMPLATES / "subfloor-visual-qa.yml").read_text()

        self.assertTrue(text.startswith("# managed-by: subfloor — visual-qa shim v1\n"))
        self.assertIn("pull_request:\n", text)
        self.assertIn("workflow_dispatch:\n", text)
        self.assertIn("contents: read\n", text)
        self.assertIn("pull-requests: write\n", text)
        self.assertIn("group: subfloor-visual-qa-${{ github.ref }}", text)
        self.assertIn("test -s .sc-state/engine.ref", text)
        self.assertIn('checkout "$engine_ref" -- .super-coder', text)
        self.assertIn("hashFiles('.super-coder/scripts/visual_qa.py')", text)
        self.assertNotIn("playwright==", text)
        self.assertIn("if: always()\n        uses: actions/upload-artifact@v4", text)

    def test_example_config_is_valid_and_inactive(self):
        config = json.loads((TEMPLATES / "visual-qa.example.json").read_text())
        self.assertEqual(
            config,
            {
                "cwd": ".",
                "setup": ["npm ci", "npm run build"],
                "serve": "npm run preview -- --port {port} --host 127.0.0.1",
                "port": 4173,
                "ready_path": "/",
                "ready_timeout_s": 120,
                "settle_ms": 500,
                "routes": ["/", "/dashboard"],
                "viewports": "default",
                "paths": ["src/**", "static/**", "package.json"],
                "services": [],
                "artifact_retention_days": 14,
            },
        )

    def test_manifest_names_and_covers_both_fork_templates(self):
        expected = (
            ".super-coder/templates/fork/subfloor-visual-qa.yml",
            ".super-coder/templates/fork/visual-qa.example.json",
        )
        self.assertEqual(engine_manifest.FORK_TEMPLATE_PATHS, expected)
        for relative in expected:
            self.assertTrue((ROOT / relative).is_file(), relative)
            self.assertTrue(
                any(relative == entry or relative.startswith(entry.rstrip("/") + "/")
                    for entry in engine_manifest.ENGINE_PATHS),
                f"{relative} is not covered by ENGINE_PATHS",
            )


class VisualQaSeedTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)

    def test_seed_writes_exact_inactive_surfaces_and_preserves_them(self):
        expected = [
            Path(".github/workflows/subfloor-visual-qa.yml"),
            Path(".sc-state/visual-qa.example.json"),
        ]
        written = install.seed_visual_qa_files(
            self.repo, TEMPLATES, source_repo=False
        )
        self.assertEqual(written, expected)
        self.assertEqual(
            (self.repo / expected[0]).read_text(),
            (TEMPLATES / "subfloor-visual-qa.yml").read_text(),
        )
        self.assertEqual(
            (self.repo / expected[1]).read_text(),
            (TEMPLATES / "visual-qa.example.json").read_text(),
        )
        self.assertFalse((self.repo / ".sc-state/visual-qa.json").exists())

        (self.repo / expected[0]).write_text("fork-owned workflow\n")
        self.assertEqual(
            install.seed_visual_qa_files(self.repo, TEMPLATES, source_repo=False),
            [],
        )
        self.assertEqual((self.repo / expected[0]).read_text(), "fork-owned workflow\n")
        self.assertEqual(
            (self.repo / expected[1]).read_text(),
            (TEMPLATES / "visual-qa.example.json").read_text(),
        )

    def test_source_repo_guard_writes_nothing(self):
        with mock.patch.object(install, "is_source_repo", return_value=True):
            self.assertEqual(install.seed_visual_qa_files(self.repo, TEMPLATES), [])
        self.assertFalse((self.repo / ".github").exists())
        self.assertFalse((self.repo / ".sc-state").exists())

    def test_init_fork_calls_the_shared_seed_path_for_a_fresh_database(self):
        database = self.repo / "shell.db"
        with sqlite3.connect(database) as con:
            con.executescript(
                "CREATE TABLE users(user_id INTEGER PRIMARY KEY, username TEXT, is_active INTEGER);"
                "CREATE TABLE shells(shell_id INTEGER PRIMARY KEY, shortname TEXT, is_deleted INTEGER DEFAULT 0);"
                "CREATE TABLE shell_skills(shell_id INTEGER, skill_id INTEGER);"
            )

        next_shell_id = iter(range(1, 7))

        def create_shell(con, *, flavor, **_kwargs):
            shell_id = next(next_shell_id)
            con.execute(
                "INSERT INTO shells(shell_id, shortname, is_deleted) VALUES (?, ?, 0)",
                (shell_id, f"{flavor[:3].upper()}{shell_id}"),
            )
            return shell_id

        seeded = [Path(".github/workflows/subfloor-visual-qa.yml")]
        with (
            mock.patch.object(init_fork, "DB_PATH", database),
            mock.patch.object(init_fork.install_mod, "seed_visual_qa_files", return_value=seeded) as seed,
            mock.patch.object(init_fork, "create_shell", side_effect=create_shell),
            mock.patch.object(
                init_fork,
                "flavors",
                return_value=[{"flavor": name} for name in
                              ("admin", "planner", "dev", "reviewer", "cartographer")],
            ),
        ):
            self.assertEqual(init_fork.main(["--username", "Jed"]), 0)

        seed.assert_called_once_with()
        with sqlite3.connect(database) as con:
            self.assertEqual(con.execute("SELECT username FROM users").fetchall(), [("Jed",)])
            self.assertEqual(con.execute("SELECT COUNT(*) FROM shells").fetchone()[0], 6)


class VisualQaUpdateTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        self.workflow = self.repo / ".github/workflows/subfloor-visual-qa.yml"
        self.example = self.repo / ".sc-state/visual-qa.example.json"

    def reconcile(self):
        return update.ensure_workflows(self.repo, TEMPLATES, source_repo=False)

    def test_absent_files_are_seeded_and_rerun_converges(self):
        action, changed = self.reconcile()
        self.assertEqual(action, "seeded")
        self.assertEqual(
            changed,
            [Path(".github/workflows/subfloor-visual-qa.yml"),
             Path(".sc-state/visual-qa.example.json")],
        )
        self.assertEqual(
            self.workflow.read_text(),
            (TEMPLATES / "subfloor-visual-qa.yml").read_text(),
        )
        self.assertEqual(
            self.example.read_text(),
            (TEMPLATES / "visual-qa.example.json").read_text(),
        )
        self.assertFalse((self.repo / ".sc-state/visual-qa.json").exists())

        self.assertEqual(self.reconcile(), ("current", []))

    def test_older_managed_workflow_is_refreshed_but_example_is_preserved(self):
        self.workflow.parent.mkdir(parents=True)
        self.workflow.write_text("# managed-by: subfloor — visual-qa shim v0\nold\n")
        self.example.parent.mkdir(parents=True)
        self.example.write_text("fork note\n")

        action, changed = self.reconcile()
        self.assertEqual(action, "updated")
        self.assertEqual(changed, [Path(".github/workflows/subfloor-visual-qa.yml")])
        self.assertEqual(
            self.workflow.read_text(),
            (TEMPLATES / "subfloor-visual-qa.yml").read_text(),
        )
        self.assertEqual(self.example.read_text(), "fork note\n")

    def test_unmanaged_workflow_is_preserved_while_missing_example_is_seeded(self):
        self.workflow.parent.mkdir(parents=True)
        self.workflow.write_text("name: Fork-owned Visual QA\n")

        action, changed = self.reconcile()
        self.assertEqual(action, "unmanaged")
        self.assertEqual(changed, [Path(".sc-state/visual-qa.example.json")])
        self.assertEqual(self.workflow.read_text(), "name: Fork-owned Visual QA\n")
        self.assertEqual(
            self.example.read_text(),
            (TEMPLATES / "visual-qa.example.json").read_text(),
        )

    def test_same_or_newer_managed_version_is_not_rewritten(self):
        self.workflow.parent.mkdir(parents=True)
        self.workflow.write_text("# managed-by: subfloor — visual-qa shim v2\nfuture\n")
        self.example.parent.mkdir(parents=True)
        self.example.write_text("existing\n")

        self.assertEqual(self.reconcile(), ("current", []))
        self.assertEqual(
            self.workflow.read_text(),
            "# managed-by: subfloor — visual-qa shim v2\nfuture\n",
        )
        self.assertEqual(self.example.read_text(), "existing\n")

    def test_source_repo_rule_is_a_total_no_op(self):
        with mock.patch.object(update, "is_source_repo", return_value=True):
            self.assertEqual(
                update.ensure_workflows(self.repo, TEMPLATES),
                ("source", []),
            )
        self.assertFalse(self.workflow.exists())
        self.assertFalse(self.example.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
