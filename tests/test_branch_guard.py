#!/usr/bin/env python3
"""Live probes for branch-guard.sh (#317).

The guard blocks default-branch edits, but a gitignored target (the shared/
handoff dir pattern) can never land on a branch — blocking it forced shells to
side-step the hook via Bash `cp` to complete a documented workflow. These tests
drive the real script against a scratch repo.

The scratch repo deliberately lives under $HOME, NOT /tmp — the guard's scratch
exemption allows /tmp/* outright, which would short-circuit every case here.

Run:
    python3 tests/test_branch_guard.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / ".super-coder" / "scripts" / "branch-guard.sh"

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


class BranchGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = Path(tempfile.mkdtemp(prefix="sc-bg-test-", dir=Path.home()))
        run = lambda *a: subprocess.run(a, cwd=cls.repo, check=True,  # noqa: E731
                                        capture_output=True,
                                        env={**os.environ, **GIT_ENV})
        run("git", "init", "-q", "-b", "main")
        (cls.repo / ".gitignore").write_text("shared/\n")
        (cls.repo / "shared" / "specs").mkdir(parents=True)
        (cls.repo / "src").mkdir()
        (cls.repo / "src" / "app.py").write_text("x = 1\n")
        run("git", "add", ".gitignore", "src/app.py")
        run("git", "commit", "-q", "-m", "init")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.repo, ignore_errors=True)

    def guard(self, target: str) -> subprocess.CompletedProcess:
        """Run the guard the way the claude PreToolUse hook does: JSON on stdin,
        cwd inside the repo, no admin/shared-dir/TMPDIR escape hatches."""
        env = {k: v for k, v in os.environ.items()
               if k not in ("SC_SHELL_FLAVOR", "SC_SHARED_DIRS", "TMPDIR",
                            "SC_PROTECTED_BRANCHES", "SC_SHELL_WORKTREE")}
        payload = json.dumps({"tool_input": {"file_path": target}})
        return subprocess.run(["bash", str(GUARD)], input=payload, text=True,
                              cwd=self.repo, env=env, capture_output=True)

    def test_gitignored_target_allowed_on_protected_branch(self):
        # the #317 case: shared/ is gitignored — a write there can't land on main
        r = self.guard(str(self.repo / "shared" / "specs" / "handoff.md"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_tracked_area_target_blocked_on_protected_branch(self):
        r = self.guard(str(self.repo / "src" / "new_module.py"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("protected branch 'main'", r.stderr)

    def test_tracked_area_target_allowed_on_feature_branch(self):
        subprocess.run(["git", "checkout", "-q", "-b", "feat/x"],
                       cwd=self.repo, check=True, capture_output=True)
        try:
            r = self.guard(str(self.repo / "src" / "new_module.py"))
            self.assertEqual(r.returncode, 0, r.stderr)
        finally:
            subprocess.run(["git", "checkout", "-q", "main"],
                           cwd=self.repo, check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
