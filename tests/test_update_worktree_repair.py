#!/usr/bin/env python3
"""Every update repairs every shell worktree after a whole-fork move."""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))
import update  # noqa: E402


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


class UpdateWorktreeRepairTest(unittest.TestCase):
    def test_repairs_all_shell_worktrees_on_every_call(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            old_repo = base / "old" / "fork"
            old_repo.mkdir(parents=True)
            git(old_repo, "init", "-q", "-b", "main")
            git(old_repo, "config", "user.email", "test@example.com")
            git(old_repo, "config", "user.name", "Test")
            (old_repo / "tracked.txt").write_text("base\n")
            git(old_repo, "add", "tracked.txt")
            git(old_repo, "commit", "-qm", "base")
            old_worktrees = old_repo / ".sc-worktrees"
            old_worktrees.mkdir()
            for name in ("dev1", "pln1", "rev1"):
                git(
                    old_repo,
                    "worktree",
                    "add",
                    "-q",
                    "-b",
                    f"shell/{name}",
                    str(old_worktrees / name),
                )

            moved_repo = base / "Repos" / "fork"
            moved_repo.parent.mkdir()
            old_repo.rename(moved_repo)
            expected = tuple(
                moved_repo / ".sc-worktrees" / name
                for name in ("dev1", "pln1", "rev1")
            )
            with mock.patch.object(update, "REPO_ROOT", moved_repo), \
                    contextlib.redirect_stdout(io.StringIO()):
                first = update.repair_git_worktrees()
                second = update.repair_git_worktrees()

            self.assertEqual(expected, first)
            self.assertEqual(expected, second)
            for path in expected:
                self.assertEqual(
                    str(path), git(path, "rev-parse", "--show-toplevel")
                )
            listing = git(moved_repo, "worktree", "list", "--porcelain")
            for path in expected:
                self.assertIn(f"worktree {path}", listing)
            self.assertNotIn(str(base / "old" / "fork"), listing)


if __name__ == "__main__":
    unittest.main(verbosity=2)
