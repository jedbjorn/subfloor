#!/usr/bin/env python3
"""Tests for ensure_gitignore — fresh append + line-additive top-up.

The bug this guards: a fork installed before a new engine ignore rule (e.g. the
map DB cache) would never receive that rule, because the old check was
all-or-nothing on the marker. ensure_gitignore must top up missing patterns on
`./sc update` without duplicating existing ones.

Run:
    python3 tests/test_gitignore_sync.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".super-coder" / "scripts"))
import install  # noqa: E402

MAP_DB = "/.sc-state/map.db"
DB_BACKUPS = "/.sc-state/db_backups/"


class EnsureGitignoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.gi = self.root / ".gitignore"
        self.addCleanup(self.tmp.cleanup)

    def test_fresh_appends_full_block(self):
        changed = install.ensure_gitignore(self.root)
        self.assertTrue(changed)
        text = self.gi.read_text()
        self.assertIn(install._GITIGNORE_MARKER, text)
        for pat in install._required_ignores():
            self.assertIn(pat, text)
        self.assertIn(DB_BACKUPS, text)

    def test_idempotent(self):
        install.ensure_gitignore(self.root)
        self.assertFalse(install.ensure_gitignore(self.root))  # nothing to add

    def test_tops_up_missing_line_on_old_fork(self):
        # Simulate a fork installed before the map.db rule existed: the marker
        # block is present but missing the map DB patterns.
        old = "\n".join(
            ln for ln in install._GITIGNORE_BLOCK.splitlines() if "map.db" not in ln)
        self.gi.write_text(old + "\n")
        self.assertNotIn(MAP_DB, self.gi.read_text())

        changed = install.ensure_gitignore(self.root)
        self.assertTrue(changed)
        text = self.gi.read_text()
        self.assertIn(MAP_DB, text)
        self.assertIn(DB_BACKUPS, text)
        # existing lines are not duplicated
        self.assertEqual(text.count("/.super-coder/"), 1)
        # and a second run is a no-op
        self.assertFalse(install.ensure_gitignore(self.root))

    def test_no_marker_unrelated_content_appends_once(self):
        self.gi.write_text("node_modules/\n*.log\n")
        install.ensure_gitignore(self.root)
        text = self.gi.read_text()
        self.assertIn(install._GITIGNORE_MARKER, text)
        self.assertIn(MAP_DB, text)
        self.assertEqual(text.count(install._GITIGNORE_MARKER), 1)
        self.assertIn("node_modules/", text)  # pre-existing content preserved


if __name__ == "__main__":
    unittest.main()
