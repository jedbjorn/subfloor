#!/usr/bin/env python3
"""Regression tests for the launcher's TTY-only spinner."""
from __future__ import annotations

import io
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import style  # noqa: E402


class _Stdout(io.StringIO):
    def __init__(self, tty: bool) -> None:
        super().__init__()
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


def _wait_for(stream: _Stdout, text: str) -> None:
    deadline = time.monotonic() + 0.5
    while text not in stream.getvalue() and time.monotonic() < deadline:
        time.sleep(0.01)
    if text not in stream.getvalue():
        raise AssertionError(f"spinner never wrote {text!r}: {stream.getvalue()!r}")


class SpinnerTest(unittest.TestCase):
    def test_non_tty_is_a_structural_noop(self) -> None:
        stdout = _Stdout(tty=False)

        with mock.patch.object(style.sys, "stdout", stdout):
            with style.spinner("sweeping analytics") as spinner:
                spinner.label = "syncing worktree"

        self.assertEqual("", stdout.getvalue())
        self.assertIsNone(spinner._thread)

    def test_disabled_spinner_is_a_noop_even_on_tty(self) -> None:
        stdout = _Stdout(tty=True)

        with mock.patch.object(style.sys, "stdout", stdout):
            with style.spinner("headless boot", enabled=False) as spinner:
                spinner.label = "rendering boot doc + skills"

        self.assertEqual("", stdout.getvalue())
        self.assertIsNone(spinner._thread)

    def test_tty_spins_updates_label_and_stays_stopped(self) -> None:
        stdout = _Stdout(tty=True)

        with mock.patch.object(style.sys, "stdout", stdout):
            with style.spinner("sweeping analytics") as spinner:
                _wait_for(stdout, "sweeping analytics")
                spinner.label = "syncing worktree"
                _wait_for(stdout, "syncing worktree")

            stopped = stdout.getvalue()
            time.sleep(0.15)

        self.assertTrue(spinner._thread.daemon)
        self.assertFalse(spinner._thread.is_alive())
        self.assertIn("\r| sweeping analytics…", stopped)
        self.assertIn("syncing worktree…", stopped)
        self.assertTrue(stopped.endswith("\r\x1b[2K"))
        self.assertEqual(stopped, stdout.getvalue())

    def test_keyboard_interrupt_clears_before_propagating(self) -> None:
        stdout = _Stdout(tty=True)

        with mock.patch.object(style.sys, "stdout", stdout):
            with self.assertRaises(KeyboardInterrupt):
                with style.spinner("syncing worktree") as spinner:
                    _wait_for(stdout, "syncing worktree")
                    raise KeyboardInterrupt
            stopped = stdout.getvalue()
            time.sleep(0.15)

        self.assertFalse(spinner._thread.is_alive())
        self.assertTrue(stopped.endswith("\r\x1b[2K"))
        self.assertEqual(stopped, stdout.getvalue())

    def test_explicit_stop_is_idempotent_at_context_exit(self) -> None:
        stdout = _Stdout(tty=True)

        with mock.patch.object(style.sys, "stdout", stdout):
            with style.spinner("syncing worktree") as spinner:
                _wait_for(stdout, "syncing worktree")
                spinner.stop()

        self.assertEqual(1, stdout.getvalue().count("\r\x1b[2K"))
        self.assertFalse(spinner._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
