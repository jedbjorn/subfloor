"""Explicit setup preserves fork ownership and the retired default lane."""
import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder/scripts"))
import visual_qa as qa


class SetupTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)

    def configure(self):
        path = self.repo / ".sc-state/visual-qa.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"serve": "x", "routes": ["/"]}))

    def test_unconfigured_never_creates_workflows(self):
        with self.assertRaisesRegex(qa.VisualQaError, "must be configured"):
            qa.cmd_setup_ci(argparse.Namespace(), repo=self.repo)
        self.assertFalse((self.repo / ".github").exists())

    def test_explicit_setup_separates_capture_and_trusted_publish(self):
        self.configure()
        qa.cmd_setup_ci(argparse.Namespace(), repo=self.repo)
        workflows = self.repo / ".github/workflows"
        capture = (workflows / "subfloor-visual-qa-capture.yml").read_text()
        publish = (workflows / "subfloor-visual-qa-publish.yml").read_text()
        self.assertNotIn("contents: write", capture)
        self.assertNotIn("pull-requests: write", capture)
        self.assertIn("--no-publish --report", capture)
        self.assertIn("workflow_run:", publish)
        self.assertIn("ref: ${{ github.event.repository.default_branch }}", publish)
        self.assertIn("contents: write", publish)
        self.assertIn("run-id: ${{ github.event.workflow_run.id }}", publish)
        self.assertNotIn("managed-by: subfloor", capture + publish)
        with self.assertRaisesRegex(qa.VisualQaError, "already exist"):
            qa.cmd_setup_ci(argparse.Namespace(), repo=self.repo)
        self.assertEqual(capture, (workflows / "subfloor-visual-qa-capture.yml").read_text())

    def test_existing_capture_lane_is_not_duplicated_or_overwritten(self):
        self.configure()
        existing = self.repo / ".github/workflows/custom.yaml"
        existing.parent.mkdir(parents=True)
        existing.write_text("run: ./sc visual-qa ci\n")
        with self.assertRaisesRegex(qa.VisualQaError, "competing capture"):
            qa.cmd_setup_ci(argparse.Namespace(), repo=self.repo)
        self.assertEqual(list(existing.parent.iterdir()), [existing])
        self.assertEqual(existing.read_text(), "run: ./sc visual-qa ci\n")


if __name__ == "__main__":
    unittest.main()
