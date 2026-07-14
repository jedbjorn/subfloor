#!/usr/bin/env python3
"""Tests for shell_liveness orphan detection: the pure classifier
(classify_orphan), the guard-shaping helper (orphan_split), and a compute()
smoke pass against the live /proc.

Stdlib `unittest`, matching the sibling suites.

Run:
    python3 tests/test_shell_liveness.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
import shell_liveness  # noqa: E402


class ClassifyOrphanTest(unittest.TestCase):
    """The pure verdict: (tty_nr, ppid, tty_fd, tty_exists) → orphan state."""

    def test_attached_normal_session(self):
        # Interactive session, pty alive → not an orphan.
        self.assertIsNone(shell_liveness.classify_orphan(
            34816, 4242, "/dev/pts/3", True))

    def test_tty_gone_deleted_suffix(self):
        # Terminal window closed; readlink flags the dead pty.
        self.assertEqual("tty-gone", shell_liveness.classify_orphan(
            34816, 4242, "/dev/pts/3 (deleted)", True))

    def test_tty_gone_device_missing(self):
        # Same closure, no (deleted) marker — the pts node just isn't there.
        self.assertEqual("tty-gone", shell_liveness.classify_orphan(
            34816, 4242, "/dev/pts/3", False))

    def test_detached_reparented_to_init(self):
        # Headless survivor: no controlling TTY, parent gone → init.
        self.assertEqual("detached", shell_liveness.classify_orphan(
            0, 1, None, None))

    def test_headless_with_live_parent_is_not_orphaned(self):
        # A NORMAL headless boot: no TTY but its spawner is alive.
        self.assertIsNone(shell_liveness.classify_orphan(0, 4242, None, None))

    def test_missing_stat_is_conservative(self):
        # No /proc data → never call an orphan.
        self.assertIsNone(shell_liveness.classify_orphan(None, None, None, None))

    def test_tty_present_but_stdio_redirected_is_conservative(self):
        # tty_nr says attached, but no stdio fd resolves to a tty → no verdict.
        self.assertIsNone(shell_liveness.classify_orphan(34816, 4242, None, None))


class OrphanSplitTest(unittest.TestCase):
    """orphan_split shapes the sc run guard: (all pids, orphaned pids)."""

    SNAP = {
        "processes": [
            {"pid": 100, "shortname": "dev1", "orphaned": "tty-gone"},
            {"pid": 101, "shortname": "dev1", "orphaned": None},
            {"pid": 200, "shortname": "dev2", "orphaned": "detached"},
            {"pid": 300, "shortname": None, "orphaned": None},  # admin root
        ],
    }

    def test_mixed_shell_is_not_all_orphaned(self):
        pids, orphans = shell_liveness.orphan_split("dev1", self.SNAP)
        self.assertEqual([100, 101], pids)
        self.assertEqual([100], orphans)
        self.assertNotEqual(len(pids), len(orphans))

    def test_fully_orphaned_shell(self):
        pids, orphans = shell_liveness.orphan_split("dev2", self.SNAP)
        self.assertEqual(pids, orphans)
        self.assertEqual([200], orphans)

    def test_case_insensitive_shortname(self):
        pids, _ = shell_liveness.orphan_split("DEV2", self.SNAP)
        self.assertEqual([200], pids)

    def test_unknown_shell_is_empty(self):
        self.assertEqual(([], []), shell_liveness.orphan_split("ghost", self.SNAP))


class ComputeSmokeTest(unittest.TestCase):
    """compute() against the real /proc: shape only, no liveness assumptions."""

    def test_snapshot_shape(self):
        snap = shell_liveness.compute()
        if not snap.get("supported"):
            self.skipTest("non-Linux: /proc unavailable")
        self.assertIn("orphaned_pids", snap)
        self.assertIsInstance(snap["orphaned_pids"], list)
        for p in snap["processes"]:
            self.assertIn("orphaned", p)
            self.assertIn(p["orphaned"], (None, "tty-gone", "detached"))
            if p["is_self"]:
                # The scanning session is by definition not an orphan.
                self.assertIsNone(p["orphaned"])


if __name__ == "__main__":
    unittest.main()
