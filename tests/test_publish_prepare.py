#!/usr/bin/env python3
"""Tests for publish's branch-prep contract (server._prepare_branch + helpers).

Stdlib `unittest`, real git in a tmpdir — matching test_publish_cleanup.py's
no-dependency style. _prepare_branch is the hardening that fixed the
self-perpetuating stranded-tree bug: it must land on a clean BASE and (re)create
the ephemeral publish branch even when a prior run left the tree stranded on a
dirty publish branch, while refusing to clobber unrelated user work. server._git
binds cwd to the module global REPO_ROOT at call time, so pointing that at a tmp
repo retargets the whole helper.

Run:
    python3 tests/test_publish_prepare.py
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
import server  # noqa: E402

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


def current_branch(cwd: Path) -> str:
    return git(cwd, "rev-parse", "--abbrev-ref", "HEAD")


def local_branches(cwd: Path) -> set[str]:
    return set(git(cwd, "for-each-ref", "--format=%(refname:short)",
                   "refs/heads/").split())


def write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


class PrepareBranchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="prepare-test-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", server.BASE_BRANCH)
        # A tracked, regenerable file (content.sql) on base.
        write(self.repo / ".sc-state" / "content.sql", "base\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "init")
        self._orig_root = server.REPO_ROOT
        server.REPO_ROOT = self.repo

    def tearDown(self) -> None:
        server.REPO_ROOT = self._orig_root
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def _strand(self) -> None:
        """Reproduce the stuck state: on the publish branch, ahead of base, with
        the regenerable content.sql dirty in the working tree."""
        git(self.repo, "checkout", "-b", server.PUBLISH_BRANCH)
        write(self.repo / ".sc-state" / "content.sql", "published\n")
        git(self.repo, "commit", "-am", "gui: publish content")
        write(self.repo / ".sc-state" / "content.sql", "dirty-uncommitted\n")

    def test_recovers_from_stranded_dirty_publish_branch(self) -> None:
        # This is the exact bug: tree stranded on the publish branch with dirty
        # regenerated content. Old code could neither delete the current branch
        # nor check out base. _prepare_branch must recover and hand back a fresh
        # publish branch off base.
        self._strand()
        out: list[str] = []
        state = {"ok": True, "pr_url": None, "pushed": False}
        ok = server._prepare_branch(out, state)
        self.assertTrue(ok, msg="\n".join(out))
        self.assertTrue(state["ok"])
        # On a freshly recreated publish branch, no stale commits, clean tree.
        self.assertEqual(current_branch(self.repo), server.PUBLISH_BRANCH)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        # Recreated from base, so the stale "published" commit is gone.
        self.assertEqual(
            git(self.repo, "rev-list", "--count",
                f"{server.BASE_BRANCH}..{server.PUBLISH_BRANCH}"), "0")
        self.assertEqual((self.repo / ".sc-state" / "content.sql").read_text(),
                         "base\n")

    def test_clean_base_creates_publish_branch(self) -> None:
        out: list[str] = []
        state = {"ok": True, "pr_url": None, "pushed": False}
        self.assertTrue(server._prepare_branch(out, state), msg="\n".join(out))
        self.assertEqual(current_branch(self.repo), server.PUBLISH_BRANCH)

    def test_refuses_when_unrelated_file_dirty(self) -> None:
        # A non-regenerable tracked file with uncommitted edits is user work —
        # publish must refuse rather than reset or clobber it.
        write(self.repo / "notes.txt", "v1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "add notes")
        write(self.repo / "notes.txt", "uncommitted edit\n")
        out: list[str] = []
        state = {"ok": True, "pr_url": None, "pushed": False}
        ok = server._prepare_branch(out, state)
        self.assertFalse(ok)
        self.assertFalse(state["ok"])
        self.assertTrue(any("non-content changes" in line for line in out))
        # The dirty user file is untouched, still on base.
        self.assertEqual(current_branch(self.repo), server.BASE_BRANCH)
        self.assertEqual((self.repo / "notes.txt").read_text(), "uncommitted edit\n")


class HelperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="prepare-helper-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", server.BASE_BRANCH)
        write(self.repo / ".sc-state" / "content.sql", "base\n")
        write(self.repo / "notes.txt", "base\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "init")
        self._orig_root = server.REPO_ROOT
        server.REPO_ROOT = self.repo

    def tearDown(self) -> None:
        server.REPO_ROOT = self._orig_root
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_restore_regenerable_discards_only_regenerable(self) -> None:
        write(self.repo / ".sc-state" / "content.sql", "dirty\n")
        write(self.repo / "notes.txt", "dirty\n")
        out: list[str] = []
        server._restore_regenerable(out)
        # content.sql reset, notes.txt (non-regenerable) left dirty.
        self.assertEqual((self.repo / ".sc-state" / "content.sql").read_text(),
                         "base\n")
        self.assertEqual((self.repo / "notes.txt").read_text(), "dirty\n")

    def test_unexpected_dirty_excludes_regenerable(self) -> None:
        write(self.repo / ".sc-state" / "content.sql", "dirty\n")
        self.assertEqual(server._unexpected_dirty(), [])
        write(self.repo / "notes.txt", "dirty\n")
        self.assertEqual(server._unexpected_dirty(), ["notes.txt"])


if __name__ == "__main__":
    unittest.main()
