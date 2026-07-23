#!/usr/bin/env python3
"""Tests for shell_liveness orphan detection: the pure classifier
(classify_orphan), the guard-shaping helper (orphan_split), and a compute()
smoke pass against the live /proc.

Stdlib `unittest`, matching the sibling suites.

Run:
    python3 tests/test_shell_liveness.py
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

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


class SessionStateTest(unittest.TestCase):
    """session_state shapes the picker annotation: 'busy' / 'orphan' / None."""

    SNAP = {
        "supported": True,
        "processes": [
            {"pid": 100, "shortname": "dev1", "orphaned": "tty-gone"},
            {"pid": 101, "shortname": "dev1", "orphaned": None},
            {"pid": 200, "shortname": "dev2", "orphaned": "detached"},
            {"pid": 300, "shortname": None, "orphaned": None},  # admin root
        ],
    }

    def test_live_session_wins_over_orphan_sibling(self):
        # One working session among orphans → someone is there → busy.
        self.assertEqual("busy", shell_liveness.session_state("dev1", self.SNAP))

    def test_all_orphaned_is_orphan(self):
        self.assertEqual("orphan", shell_liveness.session_state("dev2", self.SNAP))

    def test_dormant_shell_is_none(self):
        self.assertIsNone(shell_liveness.session_state("ghost", self.SNAP))

    def test_case_insensitive_shortname(self):
        self.assertEqual("orphan", shell_liveness.session_state("DEV2", self.SNAP))

    def test_unsupported_snapshot_is_none(self):
        # Non-Linux: no /proc → no verdicts, the picker degrades to unmarked.
        self.assertIsNone(shell_liveness.session_state(
            "dev2", {"supported": False, "processes": self.SNAP["processes"]}))


class AdminPresenceTest(unittest.TestCase):
    """Cleanup requires positive evidence that the current Admin owns root."""

    def test_missing_self_identity_is_indeterminate_and_unsafe(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(shell_liveness, "PROC", Path(td)), \
                mock.patch.object(shell_liveness, "harness_binaries",
                                  return_value={"codex"}), \
                mock.patch.object(shell_liveness, "_shell_labels", return_value={}):
            snap = shell_liveness.compute()

        self.assertIsNone(snap["self_pid"])
        self.assertEqual("indeterminate", snap["admin_presence"])
        self.assertFalse(snap["safe_to_clean_all"])

        output = io.StringIO()
        with redirect_stdout(output):
            shell_liveness._print_text(snap)
        self.assertIn("admin_presence=indeterminate", output.getvalue())
        self.assertIn("cleanup remains unsafe", output.getvalue())

    def test_matched_root_self_is_positive_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            proc = Path(td)
            process = proc / "123"
            process.mkdir()
            (process / "comm").write_text("codex\n")
            (process / "cwd").symlink_to(shell_liveness.REPO_ROOT)

            with mock.patch.object(shell_liveness, "PROC", proc), \
                    mock.patch.object(shell_liveness, "harness_binaries",
                                      return_value={"codex"}), \
                    mock.patch.object(shell_liveness, "_shell_labels",
                                      return_value={}), \
                    mock.patch.object(shell_liveness, "_self_harness_pid",
                                      return_value=123), \
                    mock.patch.object(shell_liveness, "_tty_nr", return_value=0), \
                    mock.patch.object(shell_liveness, "_ppid", return_value=2), \
                    mock.patch.object(shell_liveness, "_tty_fd", return_value=None):
                snap = shell_liveness.compute()

        self.assertEqual("present", snap["admin_presence"])
        self.assertEqual([123], snap["admin_root_pids"])
        self.assertTrue(snap["safe_to_clean_all"])


class ComputeSmokeTest(unittest.TestCase):
    """compute() against the real /proc: shape only, no liveness assumptions."""

    def test_snapshot_shape(self):
        snap = shell_liveness.compute()
        if not snap.get("supported"):
            self.skipTest("non-Linux: /proc unavailable")
        self.assertIn(snap["admin_presence"], ("present", "indeterminate"))
        if snap["admin_presence"] == "indeterminate":
            self.assertFalse(snap["safe_to_clean_all"])
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
