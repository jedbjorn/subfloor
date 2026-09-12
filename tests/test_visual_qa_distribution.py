#!/usr/bin/env python3
"""Hermetic tests for Visual QA's fork distribution surfaces.

Subfloor no longer ships a default Visual QA CI lane. Install and init seed
nothing; update retires the managed shim that earlier engines seeded and
leaves a fork-owned workflow alone.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))

import engine_manifest  # noqa: E402
import install  # noqa: E402
import update  # noqa: E402

WORKFLOW = Path(".github/workflows/subfloor-visual-qa.yml")
MANAGED = "# managed-by: subfloor — visual-qa shim v4\nname: Subfloor Visual QA\n"


class VisualQaNoDefaultCiTest(unittest.TestCase):
    def test_engine_ships_no_fork_workflow_templates(self):
        fork_templates = ENGINE / "templates" / "fork"
        self.assertFalse(fork_templates.exists(), sorted(fork_templates.glob("*")))
        self.assertFalse(hasattr(engine_manifest, "FORK_TEMPLATE_PATHS"))
        self.assertFalse(hasattr(install, "seed_visual_qa_files"))
        self.assertFalse(hasattr(install, "VISUAL_QA_TEMPLATE_TARGETS"))

    def test_install_source_never_mentions_the_shim(self):
        for name in ("install.py", "init_fork.py"):
            text = (ENGINE / "scripts" / name).read_text()
            self.assertNotIn("subfloor-visual-qa", text, name)
            self.assertNotIn("visual-qa.example", text, name)


class VisualQaUpdateRetirementTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        self.workflow = self.repo / WORKFLOW

    def reconcile(self):
        return update.ensure_workflows(self.repo, source_repo=False)

    def test_managed_shim_is_retired_and_rerun_converges(self):
        self.workflow.parent.mkdir(parents=True)
        self.workflow.write_text(MANAGED)

        self.assertEqual(self.reconcile(), ("retired", [WORKFLOW]))
        self.assertFalse(self.workflow.exists())
        self.assertEqual(self.reconcile(), ("absent", []))

    def test_older_managed_versions_are_retired_too(self):
        self.workflow.parent.mkdir(parents=True)
        self.workflow.write_text("# managed-by: subfloor — visual-qa shim v1\nold\n")

        self.assertEqual(self.reconcile(), ("retired", [WORKFLOW]))
        self.assertFalse(self.workflow.exists())

    def test_fork_owned_workflow_is_preserved(self):
        self.workflow.parent.mkdir(parents=True)
        self.workflow.write_text("name: Fork-owned Visual QA\n")

        self.assertEqual(self.reconcile(), ("unmanaged", []))
        self.assertEqual(self.workflow.read_text(), "name: Fork-owned Visual QA\n")

    def test_absent_workflow_is_never_seeded(self):
        self.assertEqual(self.reconcile(), ("absent", []))
        self.assertFalse((self.repo / ".github").exists())
        self.assertFalse((self.repo / ".sc-state").exists())

    def test_source_repo_rule_is_a_total_no_op(self):
        self.workflow.parent.mkdir(parents=True)
        self.workflow.write_text(MANAGED)
        with mock.patch.object(update, "is_source_repo", return_value=True):
            self.assertEqual(update.ensure_workflows(self.repo), ("source", []))
        self.assertEqual(self.workflow.read_text(), MANAGED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
