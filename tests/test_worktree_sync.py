#!/usr/bin/env python3
"""Tests for the launcher's worktree drift check (run.sync_worktree).

Stdlib `unittest`, no pytest — matching the engine's no-dependency style.
Each test builds a throwaway origin + clone + shell worktree with real git in
a tmpdir, then drives the one decision that matters per case: auto-sync only
when provably nothing can be lost.

Run:
    python3 tests/test_worktree_sync.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from run import sync_worktree  # noqa: E402

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


def head(cwd: Path) -> str:
    return git(cwd, "rev-parse", "HEAD")


class WorktreeSyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        # Bare origin with one commit on main.
        self.origin = base / "origin.git"
        seed = base / "seed"
        seed.mkdir()
        git(seed, "init", "-q", "-b", "main")
        (seed / "f.txt").write_text("v1\n")
        git(seed, "add", "f.txt")
        git(seed, "commit", "-qm", "c1")
        subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(self.origin)],
                       check=True, capture_output=True, env=GIT_ENV)
        # The fork's main checkout + the shell's worktree (born at HEAD, like
        # ensure_worktree does).
        self.repo = base / "fork"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.repo)],
                       check=True, capture_output=True, env=GIT_ENV)
        self.wt = self.repo / ".sc-worktrees" / "dev1"
        git(self.repo, "worktree", "add", str(self.wt), "-b", "shell/dev1")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def advance_origin(self, msg: str = "c2") -> str:
        """Land a new commit on origin/main (another shell's PR merging)."""
        other = Path(self.tmp.name) / f"other-{msg}"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(other)],
                       check=True, capture_output=True, env=GIT_ENV)
        (other / "f.txt").write_text(f"{msg}\n")
        git(other, "commit", "-qam", msg)
        git(other, "push", "-q", "origin", "main")
        return head(other)

    def test_in_sync_reports_clean(self) -> None:
        note = sync_worktree(self.wt, "DEV1")
        self.assertIn("in sync", note)

    def test_behind_and_clean_auto_syncs(self) -> None:
        tip = self.advance_origin()
        note = sync_worktree(self.wt, "DEV1")
        self.assertIn("auto-synced", note)
        self.assertEqual(head(self.wt), tip)
        # Still on the base branch, not detached.
        self.assertEqual(git(self.wt, "symbolic-ref", "--short", "HEAD"),
                         "shell/dev1")

    def test_dirty_tree_blocks_and_is_surfaced(self) -> None:
        self.advance_origin()
        (self.wt / "f.txt").write_text("local edit\n")
        before = head(self.wt)
        note = sync_worktree(self.wt, "DEV1")
        self.assertIn("NOT auto-synced", note)
        self.assertIn("uncommitted changes", note)
        self.assertEqual(head(self.wt), before)
        self.assertEqual((self.wt / "f.txt").read_text(), "local edit\n")

    def test_local_commits_block_and_are_surfaced(self) -> None:
        self.advance_origin()
        (self.wt / "mine.txt").write_text("x\n")
        git(self.wt, "add", "mine.txt")
        git(self.wt, "commit", "-qm", "local work")
        before = head(self.wt)
        note = sync_worktree(self.wt, "DEV1")
        self.assertIn("NOT auto-synced", note)
        self.assertIn("unmerged local commit", note)
        self.assertEqual(head(self.wt), before)

    def test_feature_branch_left_alone(self) -> None:
        git(self.wt, "checkout", "-qb", "feat/x")
        self.advance_origin()
        before = head(self.wt)
        note = sync_worktree(self.wt, "DEV1")
        self.assertIn("mid-work on `feat/x`", note)
        self.assertEqual(head(self.wt), before)
        self.assertEqual(git(self.wt, "symbolic-ref", "--short", "HEAD"), "feat/x")

    def test_no_remote_soft_fails(self) -> None:
        git(self.repo, "remote", "remove", "origin")
        note = sync_worktree(self.wt, "DEV1")
        self.assertIn("drift check skipped", note)

    def test_in_sync_with_local_commits_notes_them(self) -> None:
        (self.wt / "mine.txt").write_text("x\n")
        git(self.wt, "add", "mine.txt")
        git(self.wt, "commit", "-qm", "local work")
        note = sync_worktree(self.wt, "DEV1")
        self.assertIn("in sync", note)
        self.assertIn("unmerged local commit", note)


if __name__ == "__main__":
    unittest.main()
