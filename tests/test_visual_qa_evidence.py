"""Trusted publication treats browser-job output as bounded data, not code."""
import base64
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder/scripts"))
import visual_qa as qa
import visual_qa_evidence as evidence

from tests.test_visual_qa_scenarios import PNG, state


class PublisherTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.gallery = Path(temporary.name)
        (self.gallery / "confirm.png").write_bytes(PNG)
        (self.gallery / "secret.txt").write_text("not evidence")
        self.report = evidence.scenario_summary({"version": 1, "data_mode": "stubbed", "states": [state()]}, self.gallery)
        self.report.update(version=1, metadata={"source_head_sha": "a" * 40, "tested_sha": "b" * 40,
                                               "run_id": 42, "run_attempt": 1, "pr_number": 7})
        self.event = {"workflow_run": {"id": 42, "run_attempt": 1, "head_sha": "a" * 40,
                                      "head_branch": "feat/ui", "event": "pull_request",
                                      "conclusion": "success", "head_repository": {"full_name": "acme/app"}}}
        self.env = {"GITHUB_REPOSITORY": "acme/app", "GITHUB_TOKEN": "token"}
        self.calls = []
        self.old_meta = None
        self.pr = {"state": "open", "head": {"sha": "a" * 40, "ref": "feat/ui", "repo": {"full_name": "acme/app"}},
                   "base": {"repo": {"full_name": "acme/app"}}}
        self.existing = []

    def requester(self, method, url, token, payload=None):
        self.calls.append((method, url, payload))
        if method == "GET" and "/pulls/" in url:
            return self.pr
        if method == "GET" and "/contents/" in url:
            if self.old_meta:
                return {"content": base64.b64encode(json.dumps({"metadata": self.old_meta}).encode()).decode()}
            raise HTTPError(url, 404, "not found", {}, None)
        if method == "GET" and "/git/ref/" in url:
            raise HTTPError(url, 404, "not found", {}, None)
        if method == "GET" and "/comments" in url:
            return self.existing
        return {"sha": "c" * 40}

    def publish(self):
        (self.gallery / "summary.json").write_text(json.dumps(self.report))
        return evidence.publish_report(self.gallery, self.event, self.env, self.requester)

    def test_inline_report_is_pinned_and_only_png_manifest_are_uploaded(self):
        self.assertTrue(self.publish())
        tree = next(p for m, u, p in self.calls if u.endswith("/git/trees"))
        self.assertEqual({item["path"] for item in tree["tree"]}, {"confirm.png", "summary.json"})
        body = next(p["body"] for m, u, p in self.calls if u.endswith("/comments") and m == "POST")
        self.assertIn('/blob/' + "c" * 40 + '/confirm.png?raw=true', body)
        self.assertIn('<img src=', body)
        self.assertIn("| State | Dark / desktop |", body)
        self.assertIn("stubbed", body)
        self.assertIn("b" * 40, body)
        commit = next(p for m, u, p in self.calls if u.endswith("/git/commits"))
        self.assertEqual(commit["parents"], [])

    def test_rerun_updates_only_our_bot_comment(self):
        self.existing = [{"id": 1, "body": "<!-- subfloor-visual-qa -->", "user": {"login": "attacker"}},
                         {"id": 2, "body": "<!-- subfloor-visual-qa -->", "user": {"login": "github-actions[bot]"}}]
        self.publish()
        self.assertTrue(any(m == "PATCH" and u.endswith("/comments/2") for m, u, p in self.calls))
        self.assertFalse(any(m == "PATCH" and u.endswith("/comments/1") for m, u, p in self.calls))

    def test_stale_closed_external_and_mismatched_captures_never_publish(self):
        for change in ("stale", "closed", "external", "mismatch"):
            with self.subTest(change=change):
                self.calls.clear()
                self.pr["head"]["sha"] = "a" * 40
                self.pr["state"] = "open"
                self.event["workflow_run"]["head_repository"]["full_name"] = "acme/app"
                self.report["metadata"]["run_id"] = 42
                if change == "stale":
                    self.pr["head"]["sha"] = "d" * 40
                elif change == "closed":
                    self.pr["state"] = "closed"
                elif change == "external":
                    self.event["workflow_run"]["head_repository"]["full_name"] = "outsider/app"
                else:
                    self.report["metadata"]["run_id"] = 43
                if change == "mismatch":
                    with self.assertRaisesRegex(ValueError, "originating"):
                        self.publish()
                else:
                    self.publish()
                self.assertFalse(any(m != "GET" for m, u, p in self.calls))

    def test_same_head_older_run_cannot_replace_newer_evidence(self):
        self.old_meta = {"run_id": 43, "run_attempt": 1}
        self.publish()
        self.assertFalse(any(m != "GET" for m, u, p in self.calls))

    def test_failed_workflow_cannot_claim_passed_evidence(self):
        self.event["workflow_run"]["conclusion"] = "failure"
        self.publish()
        body = next(p["body"] for m, u, p in self.calls if u.endswith("/comments") and m == "POST")
        self.assertIn("did not complete successfully", body)
        self.assertIn("### ✗ Visual QA", body)

    def test_neutral_replaces_gallery_with_skip_without_publishing_images(self):
        self.report.update(outcome="neutral", routes=[], reason="No configured app paths changed.")
        self.publish()
        self.assertFalse(any("/git/" in u for m, u, p in self.calls))
        body = next(p["body"] for m, u, p in self.calls if u.endswith("/comments") and m == "POST")
        self.assertIn("skipped", body)
        self.assertNotIn("<img", body)

    def test_head_change_during_upload_does_not_post_report(self):
        original = self.requester
        def requester(method, url, token, payload=None):
            result = original(method, url, token, payload)
            if method == "POST" and url.endswith("/git/refs"):
                self.pr["head"]["sha"] = "d" * 40
            return result
        (self.gallery / "summary.json").write_text(json.dumps(self.report))
        evidence.publish_report(self.gallery, self.event, self.env, requester)
        self.assertFalse(any("/comments" in u for m, u, p in self.calls))

    def test_readonly_token_refuses_publish_and_preserves_files(self):
        def requester(method, url, token, payload=None):
            if method != "GET":
                raise HTTPError(url, 403, "read-only", {}, None)
            return self.requester(method, url, token, payload)
        (self.gallery / "summary.json").write_text(json.dumps(self.report))
        with self.assertRaises(HTTPError):
            evidence.publish_report(self.gallery, self.event, self.env, requester)
        self.assertTrue((self.gallery / "confirm.png").exists())

    def test_malformed_report_cannot_invent_pass_or_escape_gallery(self):
        for raw in ({**self.report, "outcome": "passed", "routes": []},
                    {**self.report, "version": 2},
                    {**self.report, "routes": [state(image="../private.png")]},
                    {**self.report, "routes": [state(image="confirm.png", ok=True, image_written=False)]}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                evidence.validated_report(raw, self.gallery)

    def test_table_escapes_labels_and_accepts_different_variant_sets(self):
        raw = copy.deepcopy(self.report)
        raw["routes"][0]["route"] = 'Confirmation | <img src=x>\n`'
        raw["routes"].append({"route": "Mobile", "captures": [{**raw["routes"][0]["captures"][0], "name": "phone"}]})
        body = qa.build_comment(raw, environ={})
        self.assertIn("&#124;", body)
        self.assertIn("&lt;img", body)
        self.assertIn("| Dark / desktop | phone |", body)
        self.assertIn("| — |", body)


if __name__ == "__main__":
    unittest.main()
