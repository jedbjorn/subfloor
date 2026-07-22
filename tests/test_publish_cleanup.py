#!/usr/bin/env python3
"""Tests for publish's cleanup contract (server._land_on_base).

Stdlib `unittest`, real git in a tmpdir — matching test_git_prune.py's
no-dependency style. The full git_publish() round-trip shells out to snapshot/
render + a real remote + `gh`, so it isn't unit-testable here; what IS isolable
(and is the behavior change) is the ephemeral-branch cleanup: always land back
on main, drop the local branch ONLY when its commit reached origin, keep it
otherwise so an unpushed commit isn't lost. server._git binds cwd to the
module global REPO_ROOT at call time, so pointing that at a tmp repo retargets
the whole helper.

Run:
    python3 tests/test_publish_cleanup.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "api"))
import server  # noqa: E402  (server.py adds scripts/ to the path on import)

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def git(cwd: Path, *args: str) -> str:
    res = subprocess.run(["git", "-C", str(cwd), *args],
                         capture_output=True, text=True, env=GIT_ENV)
    if res.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {res.stderr}")
    return res.stdout.strip()


def local_branches(cwd: Path) -> set[str]:
    return set(git(cwd, "for-each-ref", "--format=%(refname:short)",
                   "refs/heads/").split())


def current_branch(cwd: Path) -> str:
    return git(cwd, "rev-parse", "--abbrev-ref", "HEAD")


class LandOnBaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="publish-test-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", server.BASE_BRANCH)
        git(self.repo, "commit", "--allow-empty", "-m", "init")
        # Sit on the ephemeral publish branch with a commit, as publish would
        # right before cleanup runs.
        git(self.repo, "checkout", "-b", server.PUBLISH_BRANCH)
        git(self.repo, "commit", "--allow-empty", "-m", "gui: publish content")
        self._orig_root = server.REPO_ROOT
        server.REPO_ROOT = self.repo

    def tearDown(self) -> None:
        server.REPO_ROOT = self._orig_root
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_pushed_drops_local_branch(self) -> None:
        # commit reached origin → the local branch is disposable.
        out: list[str] = []
        server._land_on_base(out, {"ok": True, "pr_url": "x", "pushed": True})
        self.assertEqual(current_branch(self.repo), server.BASE_BRANCH)
        self.assertNotIn(server.PUBLISH_BRANCH, local_branches(self.repo))
        self.assertTrue(any("cleaned up" in line for line in out))

    def test_unpushed_keeps_local_branch(self) -> None:
        # no token / push failed → keep the branch so the commit isn't lost.
        out: list[str] = []
        server._land_on_base(out, {"ok": True, "pr_url": None, "pushed": False})
        self.assertEqual(current_branch(self.repo), server.BASE_BRANCH)
        self.assertIn(server.PUBLISH_BRANCH, local_branches(self.repo))
        self.assertTrue(any("kept local" in line for line in out))

    def test_already_on_base_is_a_noop_return(self) -> None:
        # Nothing to do but report — e.g. branch creation failed earlier so we
        # never left main and no publish branch exists.
        git(self.repo, "checkout", server.BASE_BRANCH)
        git(self.repo, "branch", "-D", server.PUBLISH_BRANCH)
        out: list[str] = []
        server._land_on_base(out, {"ok": False, "pr_url": None, "pushed": False})
        self.assertEqual(current_branch(self.repo), server.BASE_BRANCH)
        self.assertEqual(out, [f"↩ back on {server.BASE_BRANCH}"])


if __name__ == "__main__":
    unittest.main()
