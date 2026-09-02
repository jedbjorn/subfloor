#!/usr/bin/env python3
"""Live probes for the pre-commit home-repo guard (.super-coder/hooks/pre-commit).

When instance.json declares a `work_repo`, the home repo exists only as the
shells' memory substrate — a commit there is (almost) always a shell that
resolved bare `git` to the wrong repo. The hook must refuse it with a message
naming the work repo, and must stand down for: the SC_HOME_MAINTENANCE=1
override (engine publish flow, FnB maintenance), the admin flavor, commits in
any OTHER repo, and installs that declare no work_repo.

These tests drive the REAL hook against a scratch home repo carrying a fake
engine (instance.json + a permissive branch-guard stub), so only the home-repo
decision is under test — the branch guard has its own suite.

The scratch repos live beside this checkout, NOT /tmp (mirrors
test_branch_guard.py — keeps host TMPDIR quirks out of git behavior).

Run:
    python3 tests/test_home_repo_guard.py
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
HOOK = ROOT / "scripts_sc" / "hooks" / "pre-commit"

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
WORK_REPO = "/w/subfloor"


class HomeRepoGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = Path(tempfile.mkdtemp(prefix="sc-hrg-test-", dir=ROOT.parent))
        # scratch HOME repo with a fake engine inside it
        cls.home = cls.base / "home"
        cls.engine = cls.home / ".super-coder"
        (cls.engine / "scripts").mkdir(parents=True)
        (cls.engine / "instance.json").write_text(
            json.dumps({"repo": "home", "work_repo": WORK_REPO}))
        stub = cls.engine / "hooks"
        stub.mkdir(parents=True)
        stub = stub / "pre-commit"
        stub.write_text("#!/usr/bin/env bash\nexit 0\n")  # permissive: isolate the home guard
        stub.chmod(0o755)
        cls._run(cls.home, "git", "init", "-q", "-b", "main")
        (cls.home / "f.txt").write_text("x\n")
        cls._run(cls.home, "git", "add", "f.txt")
        cls._run(cls.home, "git", "commit", "-q", "-m", "init")
        # a worktree of the home repo (the shells' actual seat)
        cls.worktree = cls.base / "wt"
        cls._run(cls.home, "git", "worktree", "add", "-q",
                 str(cls.worktree), "-b", "shell/t")
        # an unrelated OTHER repo (stands in for the work repo)
        cls.other = cls.base / "other"
        cls.other.mkdir()
        cls._run(cls.other, "git", "init", "-q", "-b", "main")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, ignore_errors=True)

    @staticmethod
    def _run(cwd, *a):
        subprocess.run(a, cwd=cwd, check=True, capture_output=True,
                       env={**os.environ, **GIT_ENV})

    def hook(self, cwd: Path, **extra_env) -> subprocess.CompletedProcess:
        """Run the real pre-commit hook as git would: cwd = repo toplevel."""
        env = {k: v for k, v in os.environ.items()
               if k not in ("SC_HOME_MAINTENANCE", "SC_SHELL_FLAVOR",
                            "SC_ENGINE_DIR")}
        env.update({"SC_ENGINE_DIR": str(self.engine), **extra_env})
        return subprocess.run(["bash", str(HOOK)], cwd=cwd, env=env,
                              capture_output=True, text=True)

    def test_home_repo_commit_blocked_with_redirect(self):
        r = self.hook(self.home)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("HOME substrate repo", r.stderr)
        self.assertIn(WORK_REPO, r.stderr)  # the redirect names the work repo
        self.assertIn("SC_HOME_MAINTENANCE=1", r.stderr)  # and the override

    def test_home_worktree_commit_blocked_too(self):
        # worktrees share the home git-common-dir — the shells' real seat
        r = self.hook(self.worktree)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn(WORK_REPO, r.stderr)

    def test_maintenance_override_allows(self):
        r = self.hook(self.home, SC_HOME_MAINTENANCE="1")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_admin_flavor_allows(self):
        r = self.hook(self.home, SC_SHELL_FLAVOR="admin")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_other_repo_commit_allowed(self):
        r = self.hook(self.other)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_no_work_repo_declared_never_guards(self):
        (self.engine / "instance.json").write_text(json.dumps({"repo": "home"}))
        try:
            r = self.hook(self.home)
            self.assertEqual(r.returncode, 0, r.stderr)
        finally:
            (self.engine / "instance.json").write_text(
                json.dumps({"repo": "home", "work_repo": WORK_REPO}))


if __name__ == "__main__":
    unittest.main()
