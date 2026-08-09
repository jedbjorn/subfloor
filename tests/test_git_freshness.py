"""Real-Git coverage for target-aware boot freshness projection."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import git_freshness

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Freshness Test",
    "GIT_AUTHOR_EMAIL": "freshness@example.invalid",
    "GIT_COMMITTER_NAME": "Freshness Test",
    "GIT_COMMITTER_EMAIL": "freshness@example.invalid",
}


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=GIT_ENV,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


class FreshnessFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        seed = root / "seed"
        seed.mkdir()
        git(seed, "init", "-q", "-b", "main")
        (seed / "tracked.txt").write_text("one\n")
        git(seed, "add", "tracked.txt")
        git(seed, "commit", "-qm", "one")
        self.origin = root / "origin.git"
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(seed), str(self.origin)],
            env=GIT_ENV,
            check=True,
        )
        self.repo = root / "repo"
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(self.repo)],
            env=GIT_ENV,
            check=True,
        )
        self.shell = root / "shell"
        git(self.repo, "worktree", "add", "-q", "-b", "shell/dev1", str(self.shell))

    def advance_origin(self) -> str:
        other = Path(self.tmp.name) / "other"
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(other)],
            env=GIT_ENV,
            check=True,
        )
        (other / "tracked.txt").write_text("two\n")
        git(other, "commit", "-qam", "two")
        git(other, "push", "-q", "origin", "main")
        return git(other, "rev-parse", "HEAD")

    def project_shell(self) -> git_freshness.FreshnessProjection:
        return git_freshness.project(
            self.shell,
            policy=git_freshness.TARGET_ISOLATED_SHELL,
            expected_branch="shell/dev1",
            allow_auto_advance=True,
        )


class IsolatedShellFreshnessTest(FreshnessFixture):
    def test_clean_expected_base_auto_advances(self) -> None:
        tip = self.advance_origin()

        result = self.project_shell()

        self.assertEqual(result.target, git_freshness.TARGET_ISOLATED_SHELL)
        self.assertEqual(result.action, "auto_advanced")
        self.assertEqual(result.remote, "verified")
        self.assertEqual((result.ahead, result.behind), (0, 0))
        self.assertEqual(git(self.shell, "rev-parse", "HEAD"), tip)

    def test_dirty_staged_and_untracked_state_is_preserved(self) -> None:
        self.advance_origin()
        (self.shell / "tracked.txt").write_text("dirty\n")
        git(self.shell, "add", "tracked.txt")
        (self.shell / "untracked.txt").write_text("keep\n")
        before = git(self.shell, "rev-parse", "HEAD")

        result = self.project_shell()

        self.assertEqual(result.action, "preserved")
        self.assertTrue(result.dirty)
        self.assertEqual(result.behind, 1)
        self.assertEqual(git(self.shell, "rev-parse", "HEAD"), before)
        self.assertEqual((self.shell / "untracked.txt").read_text(), "keep\n")

    def test_local_only_commit_is_preserved(self) -> None:
        self.advance_origin()
        (self.shell / "local.txt").write_text("local\n")
        git(self.shell, "add", "local.txt")
        git(self.shell, "commit", "-qm", "local")
        before = git(self.shell, "rev-parse", "HEAD")

        result = self.project_shell()

        self.assertEqual(result.action, "preserved")
        self.assertEqual((result.ahead, result.behind), (1, 1))
        self.assertIn("local-only commits", result.detail)
        self.assertEqual(git(self.shell, "rev-parse", "HEAD"), before)

    def test_active_branch_and_detached_head_are_preserved(self) -> None:
        for state in ("active", "detached"):
            with self.subTest(state=state):
                git(self.shell, "checkout", "-q", "shell/dev1")
                if state == "active":
                    if not git(self.shell, "branch", "--list", "feat/test"):
                        git(self.shell, "checkout", "-qb", "feat/test")
                    else:
                        git(self.shell, "checkout", "-q", "feat/test")
                    expected = git_freshness.TARGET_ACTIVE_BRANCH
                else:
                    git(self.shell, "checkout", "-q", "--detach")
                    expected = git_freshness.TARGET_DETACHED
                before = git(self.shell, "rev-parse", "HEAD")

                result = self.project_shell()

                self.assertEqual(result.target, expected)
                self.assertEqual(result.action, "preserved")
                self.assertEqual(git(self.shell, "rev-parse", "HEAD"), before)


class PreservedTargetFreshnessTest(FreshnessFixture):
    def test_shared_reviewer_and_live_targets_never_move(self) -> None:
        self.advance_origin()
        cases = (
            (git_freshness.TARGET_SHARED_WORK, self.repo),
            (git_freshness.TARGET_REVIEWER_HEAD, self.shell),
            (git_freshness.TARGET_LIVE_ENGINE, self.repo),
        )
        for policy, repo in cases:
            with self.subTest(policy=policy):
                before = git(repo, "rev-parse", "HEAD")
                result = git_freshness.project(
                    repo,
                    policy=policy,
                    allow_auto_advance=True,
                )
                self.assertEqual(result.target, policy)
                self.assertEqual(result.action, "preserved")
                self.assertEqual(result.remote, "verified")
                self.assertEqual(result.behind, 1)
                self.assertEqual(git(repo, "rev-parse", "HEAD"), before)

    def test_missing_remote_and_offline_fetch_are_unverified_not_current(self) -> None:
        git(self.repo, "remote", "remove", "origin")
        missing = git_freshness.project(
            self.repo,
            policy=git_freshness.TARGET_SHARED_WORK,
        )
        self.assertEqual(missing.remote, "unverified")
        self.assertIsNone(missing.ahead)
        self.assertIsNone(missing.behind)
        self.assertIn("missing remote", missing.detail)

        git(self.repo, "remote", "add", "origin", str(self.origin))
        self.origin.rename(Path(str(self.origin) + ".offline"))
        offline = git_freshness.project(
            self.repo,
            policy=git_freshness.TARGET_SHARED_WORK,
        )
        self.assertEqual(offline.remote, "unverified")
        self.assertIsNone(offline.ahead)
        self.assertIsNone(offline.behind)
        self.assertIn("fetch origin/main failed", offline.detail)

    def test_refresh_lock_contention_is_unverified(self) -> None:
        with mock.patch.object(
            git_freshness,
            "_refresh_lock",
            side_effect=TimeoutError("shared-repo freshness lock is busy"),
        ):
            result = git_freshness.project(
                self.repo,
                policy=git_freshness.TARGET_SHARED_WORK,
            )

        self.assertEqual(result.remote, "unverified")
        self.assertIn("lock is busy", result.detail)
        self.assertNotIn("current", result.detail)

    def test_render_names_exact_target_identity_and_unknown_relation(self) -> None:
        git(self.repo, "remote", "remove", "origin")
        projection = git_freshness.project(
            self.repo,
            policy=git_freshness.TARGET_SHARED_WORK,
        )

        rendered = git_freshness.render(projection)

        self.assertIn(f"shared_work_repo: `{self.repo.resolve()}`", rendered)
        self.assertIn("branch `main`", rendered)
        self.assertIn("ahead/behind unknown", rendered)
        self.assertIn("remote unverified", rendered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
