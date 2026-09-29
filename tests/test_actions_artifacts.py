"""Destructive cleanup guards and standalone workflow execution, without GitHub."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder/scripts"))
import actions_artifact_gc as gc
import actions_artifacts as cli

NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)
OLD = "2026-09-01T00:00:00Z"


def artifact(aid=1, created=OLD, run=10):
    return {"id": aid, "name": "test-report", "created_at": created,
            "size_in_bytes": 100, "workflow_run": {"id": run}}


class CleanupTest(unittest.TestCase):
    def execute(self, artifacts, statuses, apply=False):
        calls = []

        def api(endpoint, method="GET"):
            calls.append((endpoint, method))
            if "/artifacts?" in endpoint:
                return {"artifacts": artifacts}
            if method == "DELETE":
                return None
            run = int(endpoint.rsplit("/", 1)[-1])
            status = statuses[run]
            return status.pop(0) if isinstance(status, list) else status

        with mock.patch.object(gc, "api", side_effect=api), contextlib.redirect_stdout(io.StringIO()):
            summary = gc.cleanup("owner/repo", 7, apply, NOW)
        return summary, calls

    def test_dry_run_reads_but_never_deletes(self):
        summary, calls = self.execute([artifact()], {10: {"status": "completed", "updated_at": OLD}})
        self.assertEqual(summary["would_delete_count"], 1)
        self.assertEqual(summary["bytes"], 100)
        self.assertTrue(all(method == "GET" for _, method in calls))

    def test_apply_deletes_only_old_completed_runs(self):
        artifacts = [artifact(), artifact(2, "2026-09-29T00:00:00Z"),
                     artifact(3, run=11), artifact(4, run=12), artifact(5, run=None)]
        summary, calls = self.execute(artifacts, {
            10: {"status": "completed", "updated_at": OLD},
            11: {"status": "in_progress", "updated_at": OLD},
            12: {"status": "completed", "updated_at": "2026-09-29T00:00:00Z"},
        }, True)
        self.assertEqual([path for path, method in calls if method == "DELETE"],
                         ["repos/owner/repo/actions/artifacts/1"])
        self.assertEqual(summary["deleted_count"], 1)
        self.assertEqual(summary["skipped_count"], 3)

    def test_rerun_between_plan_and_delete_is_preserved(self):
        for changed in [{"status": "queued", "updated_at": OLD},
                        {"status": "completed", "updated_at": "2026-09-29T00:00:00Z"}]:
            with self.subTest(changed=changed):
                summary, calls = self.execute([artifact()], {
                    10: [{"status": "completed", "updated_at": OLD}, changed],
                }, True)
                self.assertEqual(summary["deleted_count"], 0)
                self.assertEqual(summary["bytes"], 0)
                self.assertFalse(any(method == "DELETE" for _, method in calls))

    def test_full_inventory_is_read_before_first_mutation(self):
        calls = []

        def api(endpoint, method="GET"):
            calls.append((endpoint, method))
            if endpoint.endswith("&page=1"):
                return {"artifacts": [artifact(aid) for aid in range(1, 101)]}
            if endpoint.endswith("&page=2"):
                return {"artifacts": [artifact(101)]}
            if method == "DELETE":
                return None
            return {"status": "completed", "updated_at": OLD}

        with mock.patch.object(gc, "api", side_effect=api), contextlib.redirect_stdout(io.StringIO()):
            summary = gc.cleanup("owner/repo", 7, True, NOW)
        self.assertEqual(summary["deleted_count"], 101)
        self.assertIn("page=2", calls[1][0])
        self.assertEqual(sum(method == "DELETE" for _, method in calls), 101)

    def test_read_failure_prevents_all_deletes(self):
        calls = []

        def api(endpoint, method="GET"):
            calls.append(method)
            if "/artifacts?" in endpoint:
                return {"artifacts": [artifact(), artifact(2, run=11)]}
            if endpoint.endswith("/10"):
                return {"status": "completed", "updated_at": OLD}
            raise gc.CleanupError("403")

        with mock.patch.object(gc, "api", side_effect=api), self.assertRaises(gc.CleanupError):
            gc.cleanup("owner/repo", 7, True, NOW)
        self.assertNotIn("DELETE", calls)

    def test_boundary_age_is_preserved(self):
        summary, calls = self.execute([artifact(created="2026-09-23T00:00:00Z")], {}, True)
        self.assertEqual(summary["deleted_count"], 0)
        self.assertEqual(len(calls), 1)

    def test_invalid_repository_and_days_are_rejected(self):
        with mock.patch.object(gc, "api") as api, self.assertRaises(gc.CleanupError):
            gc.cleanup("owner/repo/other", 7, True, NOW)
        api.assert_not_called()
        for days in ["0", "-1", "abc"]:
            with self.subTest(days=days), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                gc.main(["--repo", "owner/repo", "--older-than-days", days])

    def test_auth_failure_is_not_reported_as_empty_inventory(self):
        result = subprocess.CompletedProcess([], 1, "", "secret credential")
        with mock.patch.object(gc.subprocess, "run", return_value=result), self.assertRaises(gc.CleanupError) as raised:
            gc.api("repos/owner/repo/actions/artifacts")
        self.assertNotIn("secret", str(raised.exception))


class WorkflowTest(unittest.TestCase):
    def test_checked_in_workflow_uses_current_runtime(self):
        self.assertEqual((ROOT / cli.WORKFLOW).read_text(), cli.workflow_text())

    def test_setup_is_explicit_and_refuses_overwrites_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = cli.setup_ci(root, 14)
            text = path.read_text()
            self.assertIn("ARTIFACT_RETENTION_DAYS: '14'", text)
            with self.assertRaises(gc.CleanupError):
                cli.setup_ci(root, 7)
            self.assertEqual(path.read_text(), text)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github").symlink_to(root)
            with self.assertRaises(gc.CleanupError):
                cli.setup_ci(root, 7)

    def test_workflow_uses_only_actions_write_and_default_branch(self):
        text = cli.workflow_text()
        self.assertNotIn("pull_request:", text)
        self.assertNotIn("actions/checkout", text)
        self.assertNotIn("contents: write", text)
        self.assertIn("actions: write", text)
        self.assertIn("github.event.repository.default_branch", text)
        self.assertIn("default: false", text)

    def test_embedded_ci_program_runs_without_engine_or_checkout(self):
        text = cli.workflow_text()
        script = text.split("        run: |\n", 1)[1]
        script = "\n".join(line[10:] for line in script.splitlines()) + "\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Exercise the actual Bash heredoc and CLI, with a standalone gh.
            gh = root / "gh"
            gh.write_text('#!/bin/sh\nprintf \'%s\\n\' \'{"artifacts":[]}\'\n')
            gh.chmod(0o755)
            env = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}", GH_REPO="owner/repo",
                       ARTIFACT_RETENTION_DAYS="7", APPLY="true")
            result = subprocess.run(["bash", "-c", script], cwd=root, env=env,
                                    capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["deleted_count"], 0)


if __name__ == "__main__":
    unittest.main()
