"""Engine-automated fork repin: commit, PR, merge and fast-forward (issue #702)."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))
import fork_repin

BLOCK = f"{fork_repin.GITIGNORE_BEGIN}\n/.super-coder/\n{fork_repin.GITIGNORE_END}\n"
NEW_BLOCK = f"{fork_repin.GITIGNORE_BEGIN}\n/.super-coder/\n/.sc-worktrees/\n{fork_repin.GITIGNORE_END}\n"
PIN = "a" * 40


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=True, env=env).stdout.strip()


class RepinFixture(unittest.TestCase):
    """A fork whose `sc` and engine pin were never committed (anj-cloud)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.remote = base / "fork.git"
        seed = base / "seed"
        git(base, "init", "-q", "--bare", "-b", "main", str(self.remote))
        git(base, "init", "-q", "-b", "main", str(seed))
        (seed / "app.txt").write_text("app\n")
        (seed / ".gitignore").write_text("node_modules/\n" + BLOCK)
        git(seed, "add", "-A")
        git(seed, "commit", "-qm", "initial public release")
        git(seed, "push", "-q", str(self.remote), "main")
        self.repo = base / "fork"
        git(base, "clone", "-q", str(self.remote), str(self.repo))
        for key, value in (("user.name", "T"), ("user.email", "t@t")):
            git(self.repo, "config", key, value)
        sc = self.repo / "sc"
        sc.write_text("#!/bin/sh\necho launcher\n")
        sc.chmod(0o755)
        (self.repo / ".sc-state").mkdir()
        (self.repo / ".sc-state" / "engine.ref").write_text(PIN + "\n")
        (self.repo / ".gitignore").write_text("node_modules/\n" + NEW_BLOCK)

    def merge_repin_into_main(self) -> None:
        self.fetch()
        git(self.repo, "push", "-q", "origin",
            f"refs/remotes/origin/{fork_repin.REPIN_BRANCH}:refs/heads/main", "--force")

    def fetch(self) -> None:
        git(self.repo, "fetch", "-q", "origin",
            "+refs/heads/*:refs/remotes/origin/*")


class DesiredChangesTest(RepinFixture):
    def test_untracked_launcher_pin_and_managed_block_are_drift(self) -> None:
        changes = fork_repin.desired_changes(self.repo, "origin/main")
        self.assertEqual(set(changes), {"sc", ".sc-state/engine.ref", ".gitignore"})
        self.assertEqual(changes["sc"][0], "100755")
        self.assertEqual(changes[".sc-state/engine.ref"][0], "100644")

    def test_only_the_managed_block_is_carried_into_gitignore(self) -> None:
        (self.repo / ".gitignore").write_text("node_modules/\nmy-local-edit\n" + NEW_BLOCK)
        spliced = fork_repin.desired_changes(self.repo, "origin/main")[".gitignore"][1]
        self.assertEqual(spliced.decode(), "node_modules/\n" + NEW_BLOCK)

    def test_missing_managed_file_is_never_a_deletion(self) -> None:
        (self.repo / "sc").unlink()
        self.assertNotIn("sc", fork_repin.desired_changes(self.repo, "origin/main"))


class BuildCommitTest(RepinFixture):
    def test_commit_sits_on_upstream_and_leaves_checkout_alone(self) -> None:
        status_before = git(self.repo, "status", "--porcelain")
        changes = fork_repin.desired_changes(self.repo, "origin/main")
        commit = fork_repin.build_commit(self.repo, "origin/main", changes, "chore(engine): repin")
        self.assertEqual(git(self.repo, "rev-parse", f"{commit}^"),
                         git(self.repo, "rev-parse", "origin/main"))
        files = git(self.repo, "diff-tree", "--no-commit-id", "--name-only", "-r", commit).split()
        self.assertEqual(sorted(files), [".gitignore", ".sc-state/engine.ref", "sc"])
        self.assertEqual(git(self.repo, "ls-tree", commit, "sc").split()[0], "100755")
        self.assertEqual(git(self.repo, "status", "--porcelain"), status_before)
        self.assertEqual(git(self.repo, "branch", "--show-current"), "main")


class SyncTest(RepinFixture):
    def gh(self, merge_ok: bool = True, auto_ok: bool = True):
        calls: list[tuple[str, ...]] = []

        def fake(*args: str) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            out, rc = "", 0
            if args[:2] == ("pr", "create"):
                out = "https://github.com/o/r/pull/41"
            elif args[:2] == ("repo", "view"):
                out = "true\ttrue\ttrue"
            elif args[:2] == ("pr", "merge"):
                if "--auto" in args:
                    rc = 0 if auto_ok else 1
                elif merge_ok:
                    self.merge_repin_into_main()
                else:
                    rc = 1
            return subprocess.CompletedProcess(args, rc, out, "policy" if rc else "")

        return calls, mock.patch.object(fork_repin, "_gh", side_effect=fake)

    def sync(self) -> list[str]:
        with mock.patch.object(fork_repin, "_github_repo", return_value="o/r"):
            return [line for line in fork_repin.sync(self.repo) if line]

    def test_drift_is_committed_merged_and_pulled(self) -> None:
        calls, patch = self.gh()
        with patch:
            lines = self.sync()
        self.assertEqual(lines[0], "merged PR #41 (.gitignore, .sc-state/engine.ref, sc)")
        self.assertIn("fast-forwarded", lines[1])
        self.assertIn(("pr", "merge", "41", "--repo", "o/r", "--squash"), calls)
        self.assertEqual(git(self.repo, "ls-files", "sc"), "sc")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertEqual((self.repo / "sc").read_text(), "#!/bin/sh\necho launcher\n")
        self.assertTrue(os.access(self.repo / "sc", os.X_OK))
        # A second launch finds nothing to do.
        calls, patch = self.gh()
        with patch:
            self.assertEqual(self.sync(), [])
        self.assertEqual(calls, [])

    def test_required_checks_enable_auto_merge_then_next_launch_pulls(self) -> None:
        calls, patch = self.gh(merge_ok=False)
        with patch:
            lines = self.sync()
        self.assertEqual(lines, ["PR #41 (.gitignore, .sc-state/engine.ref, sc) set to auto-merge; the next launch pulls it"])
        self.assertIn(("pr", "merge", "41", "--repo", "o/r", "--squash", "--auto"), calls)
        self.assertEqual(git(self.repo, "ls-files", "sc"), "")
        self.merge_repin_into_main()  # GitHub lands it once checks pass
        calls, patch = self.gh()
        with patch:
            lines = self.sync()
        self.assertEqual(calls, [])
        self.assertIn("fast-forwarded", lines[0])
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_open_repin_pr_is_reused(self) -> None:
        calls, patch = self.gh()

        def listed(*args: str) -> subprocess.CompletedProcess[str]:
            if args[:2] == ("pr", "list"):
                calls.append(args)
                return subprocess.CompletedProcess(args, 0, "7", "")
            return patch.kwargs["side_effect"](*args)

        with mock.patch.object(fork_repin, "_gh", side_effect=listed):
            lines = self.sync()
        self.assertEqual(lines[0], "merged PR #7 (.gitignore, .sc-state/engine.ref, sc)")
        self.assertFalse(any(c[:2] == ("pr", "create") for c in calls))

    def test_failed_merge_and_auto_merge_raise_for_a_warning(self) -> None:
        _calls, patch = self.gh(merge_ok=False, auto_ok=False)
        with patch, self.assertRaises(fork_repin.RepinError):
            self.sync()

    def test_off_default_branch_reports_instead_of_pulling(self) -> None:
        git(self.repo, "checkout", "-q", "-b", "feature")
        _calls, patch = self.gh()
        with patch:
            lines = self.sync()
        self.assertIn("is on 'feature'", lines[1])
        self.assertEqual(git(self.repo, "branch", "--show-current"), "feature")

    def test_source_repo_is_skipped(self) -> None:
        git(self.repo, "remote", "set-url", "origin", "https://github.com/o/subfloor.git")
        with mock.patch.object(fork_repin, "_gh") as gh:
            self.assertEqual(fork_repin.sync(self.repo), [])
        gh.assert_not_called()


class MainTest(unittest.TestCase):
    def test_failure_is_a_warning_with_exit_zero(self) -> None:
        with mock.patch.object(fork_repin, "sync", side_effect=fork_repin.RepinError("no gh")), \
             mock.patch("sys.stderr") as err:
            self.assertEqual(fork_repin.main(["sync"]), 0)
        self.assertIn("no gh", "".join(c.args[0] for c in err.write.call_args_list))


if __name__ == "__main__":
    unittest.main()
