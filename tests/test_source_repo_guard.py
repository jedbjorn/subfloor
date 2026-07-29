#!/usr/bin/env python3
"""The source-repo guard must survive the super-coder → subfloor rename.

is_source_repo() is the ONLY thing standing between the source repo and the
fork-flavored B7 engine untrack (`git rm -r --cached .super-coder`) plus the
fork gitignore block. The day origin was renamed to subfloor, the
basename == "super-coder" check silently flipped to False and the untrack
fired on the dogfood repo. Three modules carry the check (install, update,
map_repo); all must key off install.SOURCE_REPO_NAMES and accept every canonical
source name, including the public CLI repository basename.

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
        self.assertIn("subfloor-cli", install.SOURCE_REPO_NAMES)
        self.assertIn("sc-cachy", install.SOURCE_REPO_NAMES)

    def test_install_accepts_source_names(self):
        # Fallback pinned False so ONLY the basename decision is under test —
        # in this checkout the engine is tracked, which would mask a miss.
        orig, orig_tracked = install.origin_basename, install._engine_tracked
        install._engine_tracked = lambda: False
        try:
            for base, want in [("super-coder", True), ("subfloor", True),
                               ("subfloor-cli", True),
                               ("sc-cachy", True),
                               ("my-fork", False), (None, False)]:
                install.origin_basename = lambda b=base: b
                self.assertEqual(install.is_source_repo(), want, base)
        finally:
            install.origin_basename = orig
            install._engine_tracked = orig_tracked

    def test_tracked_engine_reads_as_source_without_origin(self):
        # A remote-less home substrate must still read as source (else the B7
        # untrack fires on it) — the tracked-engine fallback is what saves it.
        orig, orig_tracked = install.origin_basename, install._engine_tracked
        try:
            install.origin_basename = lambda: None
            install._engine_tracked = lambda: True
            self.assertTrue(install.is_source_repo())
            install._engine_tracked = lambda: False
            self.assertFalse(install.is_source_repo())
        finally:
            install.origin_basename = orig
            install._engine_tracked = orig_tracked

    def test_update_accepts_source_names(self):
        # update.is_source_repo delegates to install's — one detection.
        orig, orig_tracked = install.origin_basename, install._engine_tracked
        install._engine_tracked = lambda: False
        try:
            for base, want in [("subfloor", True), ("super-coder", True),
                               ("subfloor-cli", True), ("sc-cachy", True),
                               ("my-fork", False)]:
                install.origin_basename = lambda b=base: b
                self.assertEqual(update.is_source_repo(), want, base)
        finally:
            install.origin_basename = orig
            install._engine_tracked = orig_tracked

    def test_map_repo_accepts_source_names(self):
        # Home-mapped mode delegates to install's detection — one detection.
        # Pin MAP_ROOT to the home repo: in work-repo mode is_source_repo asks
        # the MAPPED tree (tracked engine) and ignores origin entirely.
        orig, orig_tracked = install.origin_basename, install._engine_tracked
        orig_root = map_repo.MAP_ROOT
        install._engine_tracked = lambda: False
        map_repo.MAP_ROOT = map_repo.REPO_ROOT
        try:
            for base, want in [("subfloor", True), ("super-coder", True),
                               ("subfloor-cli", True), ("sc-cachy", True),
                               ("other", False)]:
                install.origin_basename = lambda b=base: b
                self.assertEqual(map_repo.is_source_repo(), want, base)
        finally:
            install.origin_basename = orig
            install._engine_tracked = orig_tracked
            map_repo.MAP_ROOT = orig_root

    def test_map_repo_workrepo_mode_asks_the_mapped_tree(self):
        # Work-repo mode: the mapped tree's tracked engine decides; origin
        # spoofing must have no effect. A non-repo dir -> not source.
        orig, orig_root = install.origin_basename, map_repo.MAP_ROOT
        install.origin_basename = lambda: "sc-cachy"   # would say source
        map_repo.MAP_ROOT = Path("/nonexistent-mapped-repo")
        try:
            self.assertFalse(map_repo.is_source_repo())
        finally:
            install.origin_basename = orig
            map_repo.MAP_ROOT = orig_root

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
