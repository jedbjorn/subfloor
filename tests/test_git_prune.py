#!/usr/bin/env python3
"""Tests for the boot-time branch prune (git_prune.prune).

Stdlib `unittest`, real git in a tmpdir — matching test_worktree_sync.py's
no-dependency style.

Boundary note: git_hygiene._git/_out bind `cwd=REPO_ROOT` as a default argument
(at import), so the *detection* layer can't be retargeted at a tmp repo by
patching a global — and it doesn't need to be: that layer ships with git_hygiene
and the review server already exercises it. What's new here is prune's *action*
layer — "delete exactly the `stale` set, refuse anything git refuses, stay
silent on a no-op" — so the tests feed a hand-built snapshot (the documented
`snapshot=` seam) and assert the deletions against a real git repo.

Run:
    python3 tests/test_git_prune.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import git_hygiene  # noqa: E402
import git_prune  # noqa: E402

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


def snap(*branches: tuple[str, bool], gh: bool = True) -> dict:
    """A minimal git_hygiene-shaped snapshot: (name, stale) pairs."""
    return {"gh_available": gh,
            "branches": [{"name": n, "stale": s} for n, s in branches]}


class PruneTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="prune-test-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "commit", "--allow-empty", "-m", "init")
        for b in ("feature-merged", "feature-open"):
            git(self.repo, "branch", b, "main")

    def tearDown(self) -> None:
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_deletes_exactly_the_stale_set(self) -> None:
        result = git_prune.prune(
            repo=self.repo,
            snapshot=snap(("feature-merged", True), ("feature-open", False)))
        self.assertEqual(result["deleted"], ["feature-merged"])
        self.assertEqual(result["failed"], [])
        remaining = local_branches(self.repo)
        self.assertNotIn("feature-merged", remaining)
        self.assertIn("feature-open", remaining)   # not stale — kept
        self.assertIn("main", remaining)

    def test_dry_run_deletes_nothing(self) -> None:
        result = git_prune.prune(
            repo=self.repo, dry_run=True,
            snapshot=snap(("feature-merged", True)))
        self.assertEqual(result["deleted"], ["feature-merged"])
        self.assertTrue(result["dry_run"])
        self.assertIn("feature-merged", local_branches(self.repo))  # untouched

    def test_checked_out_branch_is_refused_by_git(self) -> None:
        # Even if a snapshot wrongly marked a checked-out branch stale (a race),
        # `git branch -D` refuses it — the independent second guard. It lands in
        # `failed`, never silently lost, and survives on disk.
        wt = self.tmp / "wt"
        git(self.repo, "worktree", "add", "-b", "feature-live", str(wt), "main")
        result = git_prune.prune(
            repo=self.repo, snapshot=snap(("feature-live", True)))
        self.assertEqual(result["deleted"], [])
        self.assertEqual(result["failed"], ["feature-live"])
        self.assertIn("feature-live", local_branches(self.repo))

    def test_compute_failure_soft_fails(self) -> None:
        # A git_hygiene blow-up must yield an error result, never an exception
        # that could block a boot.
        orig = git_hygiene.compute
        git_hygiene.compute = lambda **kw: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            result = git_prune.prune(repo=self.repo)
        finally:
            git_hygiene.compute = orig
        self.assertTrue(result["error"])
        self.assertEqual(result["deleted"], [])
        self.assertIsNone(git_prune.status_line(result))

    def test_status_line_silent_on_noop(self) -> None:
        self.assertIsNone(git_prune.status_line(
            {"deleted": [], "failed": [], "candidates": 0}))
        self.assertIsNone(git_prune.status_line({"error": True}))
        line = git_prune.status_line({"deleted": ["a", "b"], "failed": []})
        self.assertIn("pruned 2 merged branches", line)
        one = git_prune.status_line({"deleted": ["a"], "failed": []})
        self.assertIn("pruned 1 merged branch ", one)  # singular, no plural 'es'


class MergedPrClassificationTests(unittest.TestCase):
    """`_gh_merged_prs` must let the NEWEST PR per branch decide state, so a
    reused branch name whose latest PR is OPEN is never misread as merged (which
    would mark it stale and `git branch -D` live work)."""

    def _run(self, prs: list[dict]) -> dict:
        fake = mock.Mock(returncode=0, stdout=json.dumps(prs))
        with mock.patch.object(git_hygiene.shutil, "which", return_value="/usr/bin/gh"), \
             mock.patch.object(git_hygiene.subprocess, "run", return_value=fake):
            by_branch, available = git_hygiene._gh_merged_prs()
        self.assertTrue(available)
        return by_branch

    def test_open_pr_on_reused_branch_wins_over_older_merged(self):
        # feat/x: #10 merged long ago, #20 open now on the same name.
        by_branch = self._run([
            {"number": 20, "headRefName": "feat/x", "state": "OPEN", "mergedAt": None},
            {"number": 10, "headRefName": "feat/x", "state": "MERGED",
             "mergedAt": "2026-01-01T00:00:00Z"},
        ])
        self.assertEqual(by_branch["feat/x"]["state"], "OPEN")   # not prunable
        self.assertEqual(by_branch["feat/x"]["number"], 20)

    def test_reopen_then_merge_is_still_prunable(self):
        # newest PR is the merged one -> branch is genuinely done.
        by_branch = self._run([
            {"number": 30, "headRefName": "feat/y", "state": "MERGED",
             "mergedAt": "2026-02-02T00:00:00Z"},
            {"number": 12, "headRefName": "feat/y", "state": "CLOSED", "mergedAt": None},
        ])
        self.assertEqual(by_branch["feat/y"]["state"], "MERGED")

    def test_order_independence(self):
        # same inputs, merged-first ordering, must still pick the open #20.
        by_branch = self._run([
            {"number": 10, "headRefName": "feat/x", "state": "MERGED",
             "mergedAt": "2026-01-01T00:00:00Z"},
            {"number": 20, "headRefName": "feat/x", "state": "OPEN", "mergedAt": None},
        ])
        self.assertEqual(by_branch["feat/x"]["state"], "OPEN")


if __name__ == "__main__":
    unittest.main()
