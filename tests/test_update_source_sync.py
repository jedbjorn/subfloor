#!/usr/bin/env python3
"""Source-repo update fast-forwards its checkout before reconciling from it.

In the source repo `./sc update` skips fetch/materialize and lays the floor FROM
THE WORKING TREE, so a stale checkout reconciles stale code and reports success.
These pin the sync that closes that, and — just as load-bearing — the two cases
where it must REFUSE rather than touch the tree.
"""
from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
import update  # noqa: E402


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True, capture_output=True, text=True).stdout.strip()


class SourceSyncCase(unittest.TestCase):
    """A real clone with a real upstream — the behaviour under test is git's."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.origin = tmp / "origin.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.origin)],
                       check=True, capture_output=True)
        self.work = tmp / "work"
        subprocess.run(["git", "clone", str(self.origin), str(self.work)],
                       check=True, capture_output=True)
        git(self.work, "config", "user.email", "t@example.com")
        git(self.work, "config", "user.name", "t")
        (self.work / "a.txt").write_text("one\n")
        git(self.work, "add", "a.txt")
        git(self.work, "commit", "-m", "one")
        git(self.work, "push", "-u", "origin", "main")
        self.addCleanup(self.tmp.cleanup)

    def sync(self) -> str:
        """Run the sync against the fixture checkout, returning its output."""
        buf = io.StringIO()
        with mock.patch.object(update, "REPO_ROOT", self.work), \
                contextlib.redirect_stdout(buf):
            update.sync_source_checkout()
        return buf.getvalue()

    def fall_behind(self, n: int = 1) -> str:
        """Push a commit, then rewind the checkout so it trails by `n`."""
        for i in range(n):
            (self.work / "a.txt").write_text(f"upstream {i}\n")
            git(self.work, "commit", "-am", f"upstream {i}")
        git(self.work, "push", "origin", "main")
        tip = git(self.work, "rev-parse", "HEAD")
        git(self.work, "reset", "--hard", f"HEAD~{n}")
        return tip

    def head(self) -> str:
        return git(self.work, "rev-parse", "HEAD")

    # ── the fix ──────────────────────────────────────────────────────────────

    def test_behind_and_clean_fast_forwards(self):
        tip = self.fall_behind()
        self.assertNotEqual(self.head(), tip)
        out = self.sync()
        self.assertEqual(self.head(), tip, "checkout was not fast-forwarded")
        self.assertIn("fast-forwarded", out)

    def test_fast_forward_is_linear_never_a_merge(self):
        """Asserting the new HEAD alone cannot tell a fast-forward from a merge
        that lands the same commit, so assert the shape too.

        Measured, not assumed: a plain `git pull` does NOT redden this — git
        fast-forwards on its own when it can, and that mutation is caught by the
        diverged case instead. The mutation this leg owns is one that forces a
        merge where a fast-forward was possible (`--no-ff`), which no other test
        here detects."""
        tip = self.fall_behind()
        self.sync()
        self.assertEqual(self.head(), tip)
        parents = git(self.work, "rev-list", "--parents", "-n", "1", "HEAD").split()
        self.assertEqual(len(parents), 2, f"HEAD is not a linear commit: {parents}")

    def test_already_current_is_a_quiet_noop(self):
        before = self.head()
        out = self.sync()
        self.assertEqual(self.head(), before)
        self.assertIn("already current", out)

    # ── the refusals ─────────────────────────────────────────────────────────

    def test_behind_with_uncommitted_work_aborts_untouched(self):
        """The main checkout is the running server's tree; never fast-forward
        one holding work in flight, and never reconcile from a stale one."""
        tip = self.fall_behind()
        (self.work / "scratch.txt").write_text("operator work in flight\n")
        before = self.head()
        with self.assertRaises(SystemExit) as raised:
            self.sync()
        self.assertEqual(self.head(), before, "aborted run still moved HEAD")
        self.assertNotEqual(self.head(), tip)
        self.assertIn("uncommitted changes", str(raised.exception))
        self.assertTrue((self.work / "scratch.txt").exists(),
                        "the operator's file was discarded")

    def test_diverged_aborts_without_merging_or_resetting(self):
        tip = self.fall_behind()
        (self.work / "local.txt").write_text("local\n")
        git(self.work, "add", "local.txt")
        git(self.work, "commit", "-m", "local only")
        before = self.head()
        with self.assertRaises(SystemExit) as raised:
            self.sync()
        self.assertEqual(self.head(), before, "diverged branch was moved")
        self.assertIn("diverged", str(raised.exception))
        # the upstream commit must NOT have been merged in
        merged = git(self.work, "branch", "--contains", tip, "--format=%(refname)")
        self.assertEqual(merged, "", "upstream commit was merged despite divergence")

    # ── the skips ────────────────────────────────────────────────────────────

    def test_no_upstream_is_skipped_not_fatal(self):
        git(self.work, "checkout", "-b", "untracked-branch")
        out = self.sync()
        self.assertIn("tracks no upstream", out)

    def test_detached_head_is_skipped_not_fatal(self):
        git(self.work, "checkout", "--detach")
        out = self.sync()
        self.assertIn("detached HEAD", out)


class Stop(Exception):
    """Sentinel — halts main() at the seam under test."""


class SourceSyncWiringCase(unittest.TestCase):
    """The function above is only worth anything if main() actually calls it.

    Every leg halts main() at a seam rather than running the reconcile, and
    halts it at BOTH seams — the sync itself and the first step after it. That
    second patch is not belt-and-braces: with only the first, deleting the call
    site lets main() run on into `migrate_or_rebuild` and `snapshot` against the
    live repo. Observed, not theorised — the first version of this test did
    exactly that when the call site was mutated away.
    """

    def test_source_repo_update_syncs_before_reconciling(self):
        with mock.patch.object(update, "is_source_repo", return_value=True), \
                mock.patch.object(update, "preflight_live_state"), \
                mock.patch.object(update, "ensure_workflows", side_effect=Stop), \
                mock.patch.object(update, "sync_source_checkout",
                                  side_effect=Stop) as sync, \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(Stop):
                update.main([])
        sync.assert_called_once()

    def test_no_fetch_opts_out_of_the_sync(self):
        """--no-fetch means touch no network; it stays the escape hatch for
        reconciling a tree deliberately. Positive control: the test above shows
        this same harness DOES reach the sync without the flag."""
        with mock.patch.object(update, "is_source_repo", return_value=True), \
                mock.patch.object(update, "preflight_live_state"), \
                mock.patch.object(update, "ensure_workflows", side_effect=Stop), \
                mock.patch.object(update, "sync_source_checkout") as sync, \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(Stop):
                update.main(["--no-fetch"])
        sync.assert_not_called()


if __name__ == "__main__":
    unittest.main()
