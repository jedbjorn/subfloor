#!/usr/bin/env python3
"""The source-repo guard must survive the super-coder → subfloor rename.

is_source_repo() is the ONLY thing standing between the source repo and the
fork-flavored B7 engine untrack (`git rm -r --cached .super-coder`) plus the
fork gitignore block. The day origin was renamed to subfloor, the
basename == "super-coder" check silently flipped to False and the untrack
fired on the dogfood repo. Three modules carry the check (install, update,
map_repo); all must key off install.SOURCE_REPO_NAMES and accept BOTH names.

Run:
    python3 tests/test_source_repo_guard.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
import install  # noqa: E402
import map_repo  # noqa: E402
import update  # noqa: E402


class SourceRepoGuardTest(unittest.TestCase):
    def test_canonical_names(self):
        self.assertIn("super-coder", install.SOURCE_REPO_NAMES)
        self.assertIn("subfloor", install.SOURCE_REPO_NAMES)

    def test_install_accepts_both_names(self):
        orig = install.origin_basename
        try:
            for base, want in [("super-coder", True), ("subfloor", True),
                               ("my-fork", False), (None, False)]:
                install.origin_basename = lambda b=base: b
                self.assertEqual(install.is_source_repo(), want, base)
        finally:
            install.origin_basename = orig

    def test_update_accepts_both_names(self):
        orig = update.git
        try:
            for url, want in [("https://github.com/jedbjorn/subfloor.git", True),
                              ("https://github.com/jedbjorn/super-coder.git", True),
                              ("git@github.com:me/my-fork.git", False)]:
                update.git = lambda *a, u=url, **k: SimpleNamespace(stdout=u + "\n",
                                                                    returncode=0)
                self.assertEqual(update.is_source_repo(), want, url)
        finally:
            update.git = orig

    def test_map_repo_accepts_both_names(self):
        orig = map_repo.git
        try:
            for url, want in [("https://github.com/jedbjorn/subfloor", True),
                              ("https://github.com/jedbjorn/super-coder", True),
                              ("https://github.com/me/other", False)]:
                map_repo.git = lambda *a, u=url: u
                self.assertEqual(map_repo.is_source_repo(), want, url)
        finally:
            map_repo.git = orig

    def test_update_remote_matcher_accepts_renamed_url(self):
        orig = update.git
        try:
            update.git = lambda *a, **k: SimpleNamespace(
                stdout="origin\tgit@github.com:me/my-fork.git (fetch)\n"
                       "engine\thttps://github.com/jedbjorn/subfloor.git (fetch)\n",
                returncode=0)
            self.assertEqual(update.super_coder_remote(), "engine")
        finally:
            update.git = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
