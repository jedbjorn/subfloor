"""The scenario protocol executes real bounded commands, without a browser."""
import argparse
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder/scripts"))
import visual_qa as qa
import visual_qa_evidence as evidence

PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", 1440, 900) + b"\x08\x06\x00\x00\x00" + b"1234"


def state(name="Confirmation", **capture):
    return {"name": name, "captures": [{"name": "Dark / desktop", "ok": True,
            "image": "confirm.png", "viewport_width": 1440, "viewport_height": 900, **capture}]}


class ScenarioTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.gallery = self.repo / "gallery"
        self.gallery.mkdir()
        (self.gallery / "confirm.png").write_bytes(PNG)

    def write_command(self, exit_code=0):
        script = self.repo / "capture.py"
        raw = {"version": 1, "data_mode": "stubbed", "states": [state()]}
        script.write_text(
            "import json, os\nfrom pathlib import Path\n"
            "out = Path(os.environ['SC_VISUAL_QA_OUTPUT'])\n"
            "assert os.environ['SC_VISUAL_QA_URL'] == 'http://app'\n"
            "assert json.loads(os.environ['SC_VISUAL_QA_VIEWPORTS'])[0]['name'] == 'mobile'\n"
            f"(out / 'states.json').write_text({json.dumps(json.dumps(raw))})\n"
            f"raise SystemExit({exit_code})\n"
        )
        import shlex
        return shlex.join([sys.executable, str(script)])

    def test_real_command_uses_environment_and_builds_common_gallery(self):
        config = qa.validate_config({"serve": "x", "capture_command": self.write_command()})
        result = qa.capture_scenarios(config, "http://app", self.gallery, self.repo, os.environ)
        self.assertEqual(result["outcome"], "passed")
        self.assertEqual(result["routes"][0]["route"], "Confirmation")
        self.assertIn("Confirmation", (self.gallery / "index.html").read_text())
        self.assertEqual(result["data_mode"], "stubbed")

    def test_command_failure_retains_completed_state_and_is_fatal(self):
        config = qa.validate_config({"serve": "x", "capture_command": self.write_command(1)})
        result = qa.capture_scenarios(config, "http://app", self.gallery, self.repo, os.environ)
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(len(result["routes"]), 1)
        self.assertIn("exit 1", result["error"])

    def test_timeout_stops_process_and_reports_failure(self):
        config = qa.validate_config({"serve": "x", "capture_command": "sleep 30", "capture_timeout_s": 1})
        result = qa.capture_scenarios(config, "http://app", self.gallery, self.repo, os.environ)
        self.assertEqual(result["outcome"], "failed")
        self.assertIn("timed out", result["error"])

    def test_manifest_refusal_cases(self):
        cases = [
            {"version": 2, "data_mode": "stubbed", "states": [state()]},
            {"version": 1, "data_mode": "unknown", "states": [state()]},
            {"version": 1, "data_mode": "real", "states": []},
            {"version": 1, "data_mode": "real", "states": [state(ok="true")]},
            {"version": 1, "data_mode": "real", "states": [state(image="../secret.png")]},
            {"version": 1, "data_mode": "real", "states": [state(image="/etc/passwd")]},
            {"version": 1, "data_mode": "real", "states": [state(image="missing.png")]},
            {"version": 1, "data_mode": "real", "states": [state(), state()]},
        ]
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                evidence.scenario_summary(raw, self.gallery)

    def test_assertion_failure_and_missing_screenshot_cannot_pass(self):
        result = evidence.scenario_summary({"version": 1, "data_mode": "stubbed",
                  "states": [state(ok=False, image=None, error="Expected disabled Confirm")]}, self.gallery)
        self.assertEqual(result["outcome"], "failed")
        self.assertFalse(result["routes"][0]["captures"][0]["image_written"])

    def test_symlink_and_non_png_and_oversize_are_refused(self):
        (self.gallery / "linked.png").symlink_to(self.gallery / "confirm.png")
        with self.assertRaisesRegex(ValueError, "symlink"):
            evidence.png_file(self.gallery, "linked.png")
        (self.gallery / "fake.png").write_text("<html>bad</html>")
        with self.assertRaisesRegex(ValueError, "not a PNG"):
            evidence.png_file(self.gallery, "fake.png")
        with mock.patch.object(evidence, "MAX_IMAGE_BYTES", 1), self.assertRaisesRegex(ValueError, "8 MiB"):
            evidence.png_file(self.gallery, "confirm.png")

    def test_capture_count_and_total_bytes_are_bounded(self):
        raw = {"version": 1, "data_mode": "static", "states": [state()]}
        with mock.patch.object(evidence, "MAX_IMAGES", 0), self.assertRaisesRegex(ValueError, "100"):
            evidence.scenario_summary(raw, self.gallery)
        with mock.patch.object(evidence, "MAX_TOTAL_BYTES", 1), self.assertRaisesRegex(ValueError, "64 MiB"):
            evidence.scenario_summary(raw, self.gallery)

    def test_report_export_preserves_unowned_gallery_and_report(self):
        (self.gallery / "summary.json").write_text("unowned")
        report = self.repo / "report"
        report.mkdir()
        (report / "keep.txt").write_text("unowned")
        args = argparse.Namespace(report="report", no_publish=True)
        summary = {"outcome": "failed", "routes": [], "routes_total": 0, "routes_failed": 0}
        with mock.patch.object(qa.subprocess, "run", return_value=mock.Mock(stdout="b" * 40)), \
             self.assertRaisesRegex(qa.VisualQaError, "isn't visual-QA output"):
            qa.finish_ci(summary, self.gallery, args, self.repo, {})
        self.assertEqual((report / "keep.txt").read_text(), "unowned")
        self.assertEqual((self.gallery / "summary.json").read_text(), "unowned")

    def test_config_change_captures_even_when_path_filter_would_skip(self):
        config = self.repo / ".sc-state/visual-qa.json"
        config.parent.mkdir()
        config.write_text(json.dumps({"serve": "x", "routes": ["/"], "paths": ["src/**"]}))
        with mock.patch.object(qa, "prepare_gallery"), mock.patch.object(qa, "finish_ci"), \
             mock.patch.object(qa, "ci_app", side_effect=qa.VisualQaError("app boot attempted")) as boot:
            code = qa.cmd_ci(argparse.Namespace(), repo=self.repo, environ={},
                             changed_paths=lambda *a, **kw: [".sc-state/visual-qa.json"],
                             installer=lambda: None, app_context=boot)
        self.assertEqual(code, 1)
        boot.assert_called_once()

    def test_no_publish_exports_commit_metadata_without_github_write(self):
        event = self.repo / "event.json"
        event.write_text(json.dumps({"number": 7, "pull_request": {"head": {"sha": "a" * 40}}}))
        summary = {
            "outcome": "neutral", "routes": [], "routes_total": 0, "routes_failed": 0,
            "reason": "No configured app paths changed."}
        with mock.patch.object(qa.subprocess, "run", return_value=mock.Mock(stdout="b" * 40)), \
             mock.patch.object(qa, "post_sticky_comment") as post:
            qa.finish_ci(summary, self.gallery, argparse.Namespace(report="report", no_publish=True), self.repo,
                         {"GITHUB_EVENT_PATH": str(event), "GITHUB_RUN_ID": "42"})
        post.assert_not_called()
        exported = json.loads((self.repo / "report/summary.json").read_text())
        self.assertEqual(exported["metadata"]["tested_sha"], "b" * 40)
        self.assertEqual(exported["metadata"]["source_head_sha"], "a" * 40)


if __name__ == "__main__":
    unittest.main()
