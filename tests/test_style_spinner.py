#!/usr/bin/env python3
"""Regression tests for launcher styling and its TTY-only spinner."""
from __future__ import annotations

import io
import sqlite3
import sys
import threading
import time
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import style  # noqa: E402
import run  # noqa: E402


class _Stdout(io.StringIO):
    def __init__(self, tty: bool) -> None:
        super().__init__()
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


class _DelayedStdout(_Stdout):
    def __init__(self) -> None:
        super().__init__(tty=True)
        self.frame_started = threading.Event()
        self._delayed = False

    def write(self, text: str) -> int:
        if text.startswith("\r|") and not self._delayed:
            self._delayed = True
            self.frame_started.set()
            time.sleep(0.3)
        return super().write(text)


def _wait_for(stream: _Stdout, text: str) -> None:
    deadline = time.monotonic() + 0.5
    while text not in stream.getvalue() and time.monotonic() < deadline:
        time.sleep(0.01)
    if text not in stream.getvalue():
        raise AssertionError(f"spinner never wrote {text!r}: {stream.getvalue()!r}")


class _ExecReached(Exception):
    pass


class _LabelRecorder:
    def __init__(self, labels: list[str], initial: str) -> None:
        self._labels = labels
        self._label = initial
        labels.append(initial)

    @property
    def label(self) -> str:
        return self._label

    @label.setter
    def label(self, value: str) -> None:
        self._label = value
        self._labels.append(value)


class ShellStatusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.shell = {"shortname": "DEV1", "flavor": "dev", "current_state": ""}
        self.snap = {"supported": True, "processes": [], "indeterminate": 0}

    def test_status_colors_and_labels(self) -> None:
        cases = (
            ("busy", "\x1b[38;5;214mBusy\x1b[0m        "),
            ("orphan", "\x1b[31mOrphaned\x1b[0m    "),
            (None, "\x1b[32mAvailable\x1b[0m   "),
        )
        with mock.patch.object(style, "ON", True):
            for state, expected in cases:
                with self.subTest(state=state), mock.patch.object(
                        run.shell_liveness, "session_state", return_value=state):
                    self.assertEqual(expected, run._shell_status(self.shell, self.snap))

    def test_admin_and_indeterminate_states_are_explicit(self) -> None:
        admin = {"shortname": "ADMIN", "flavor": "admin"}
        partial = {**self.snap, "indeterminate": 1}
        unsupported = {"supported": False, "processes": []}

        self.assertEqual("Exempt      ", run._shell_status(admin, self.snap))
        self.assertEqual("Unknown     ", run._shell_status(self.shell, partial))
        self.assertEqual("Unknown     ", run._shell_status(self.shell, unsupported))

    def test_sprint_reservation_replaces_only_available(self) -> None:
        sprint_shell = {
            **self.shell,
            "current_state": "working notes\nSPRINT doc=21 unit=4 status=waiting",
            "sprint_reserved": True,
        }
        with mock.patch.object(style, "ON", True), mock.patch.object(
                run.shell_liveness, "session_state", return_value=None):
            self.assertEqual("\x1b[38;5;214mSprint\x1b[0m      ",
                             run._shell_status(sprint_shell, self.snap))

        for state, expected in (("busy", "Busy"), ("orphan", "Orphaned")):
            with self.subTest(state=state), mock.patch.object(
                    run.shell_liveness, "session_state", return_value=state):
                self.assertEqual(expected,
                                 run._shell_status(sprint_shell, self.snap).strip())

        partial = {**self.snap, "indeterminate": 1}
        with mock.patch.object(run.shell_liveness, "session_state", return_value=None):
            self.assertEqual("Unknown",
                             run._shell_status(sprint_shell, partial).strip())

    def test_only_active_unfrozen_sprint_docs_reserve_shells(self) -> None:
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.row_factory = sqlite3.Row
        con.executescript("""
            CREATE TABLE shells (
                shell_id INTEGER PRIMARY KEY,
                display_name TEXT,
                shortname TEXT,
                mandate TEXT,
                is_shared INTEGER NOT NULL DEFAULT 0,
                flavor TEXT,
                current_state TEXT,
                user_id INTEGER,
                is_deleted INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE documents (
                document_id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL,
                frozen INTEGER NOT NULL DEFAULT 0,
                body TEXT
            );
            INSERT INTO documents VALUES
                (21, 'doc', 0, '# SPRINT: active\nstatus: ACTIVE'),
                (22, 'doc', 0, '# SPRINT: closed\nstatus: CLOSED'),
                (23, 'doc', 1, '# SPRINT: frozen\nstatus: ACTIVE'),
                (24, 'doc', 0, '# SPRINT: malformed\nstatus:');
            INSERT INTO shells VALUES
                (1, 'Active', 'DEV1', '', 0, 'dev', 'SPRINT doc=21 unit=1', 1, 0),
                (2, 'Closed', 'DEV2', '', 0, 'dev', 'SPRINT doc=22 unit=2', 1, 0),
                (3, 'Frozen', 'REV1', '', 0, 'reviewer', 'SPRINT doc=23 reviewing=2', 1, 0),
                (4, 'Missing', 'DEV3', '', 0, 'dev', 'SPRINT doc=999 unit=3', 1, 0),
                (5, 'Bad marker', 'DEV4', '', 0, 'dev', 'SPRINT doc=oops unit=4', 1, 0),
                (6, 'Bad tracker', 'DEV5', '', 0, 'dev', 'SPRINT doc=24 unit=5', 1, 0);
        """)

        shells = {shell["shortname"]: shell
                  for shell in run.list_shells(con, user_id=1)}

        self.assertEqual(
            {"DEV1": True, "DEV2": False, "REV1": False,
             "DEV3": False, "DEV4": False, "DEV5": False},
            {name: shell["sprint_reserved"] for name, shell in shells.items()},
        )
        with mock.patch.object(
                run.shell_liveness, "session_state", return_value=None):
            self.assertEqual(
                "Sprint", run._shell_status(shells["DEV1"], self.snap).strip())
            self.assertEqual(
                "Available", run._shell_status(shells["DEV2"], self.snap).strip())

    def test_sprint_annotation_never_blocks_boot(self) -> None:
        sprint_shell = {**self.shell, "sprint_reserved": True}
        with mock.patch.object(
                run.shell_liveness, "session_state", return_value=None):
            self.assertTrue(run.confirm_live(sprint_shell, self.snap))

    def test_picker_has_a_dedicated_status_column(self) -> None:
        shell = {**self.shell, "display_name": "Dev One"}
        stdout = _Stdout(tty=False)
        stdin = _Stdout(tty=True)

        with mock.patch.object(run.sys, "stdout", stdout), \
             mock.patch.object(run.sys, "stdin", stdin), \
             mock.patch("builtins.input", return_value="1"):
            chosen = run.pick_shell([shell], None, False, snap=self.snap)

        self.assertIs(shell, chosen)
        self.assertIn("Shortname     Status      Default", stdout.getvalue())
        self.assertIn("DEV1          Available", stdout.getvalue())


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

    def test_slow_frame_finishes_before_line_is_cleared(self) -> None:
        stdout = _DelayedStdout()

        with mock.patch.object(style.sys, "stdout", stdout):
            with style.spinner("slow terminal") as spinner:
                self.assertTrue(stdout.frame_started.wait(0.5))

            spinner._thread.join(timeout=1)
            stopped = stdout.getvalue()

        self.assertFalse(spinner._thread.is_alive())
        self.assertIn("\r| slow terminal…", stopped)
        self.assertTrue(stopped.endswith("\r\x1b[2K"))

    def test_thread_start_failure_falls_back_to_silent_noop(self) -> None:
        stdout = _Stdout(tty=True)
        entered = False

        with mock.patch.object(style.sys, "stdout", stdout), \
             mock.patch.object(style.threading.Thread, "start",
                               side_effect=RuntimeError("cannot start thread")):
            with style.spinner("booting") as spinner:
                entered = True
                spinner.label = "rendering boot doc + skills"

        self.assertTrue(entered)
        self.assertEqual("", stdout.getvalue())
        self.assertIsNone(spinner._thread)


class BootPhaseLabelTest(unittest.TestCase):
    def _run_main(self, *, admin: bool, no_prune: bool) -> tuple[list[str], int, int]:
        flavor = "admin" if admin else "dev"
        chosen = {"shell_id": 1, "shortname": "DEV1", "flavor": flavor}
        full = {"shell_id": 1, "display_name": "Dev One", "api_key": None}
        con = mock.Mock()
        con.execute.return_value.fetchone.return_value = full
        labels: list[str] = []

        @contextmanager
        def recording_spinner(label: str, *, enabled: bool = True):
            self.assertTrue(enabled)
            yield _LabelRecorder(labels, label)

        env = {"SC_NO_AUTOPRUNE": "1"} if no_prune else {}
        stdout = _Stdout(tty=False)
        stdin = _Stdout(tty=False)
        sync = mock.Mock(return_value="in sync with origin/main")
        prune = mock.Mock(return_value={})
        analytics = mock.Mock()
        analytics.sweep.return_value = {"inserted": 0, "updated": 0}
        fdefaults = {flavor: {"default_harness": "claude", "models": {"claude": None}}}
        adapter = {"launch": ["claude"], "emit": [], "env": {}}

        with ExitStack() as stack:
            stack.enter_context(mock.patch.dict(run.os.environ, env, clear=True))
            stack.enter_context(mock.patch.dict(sys.modules, {"analytics": analytics}))
            stack.enter_context(mock.patch.object(
                run.sys, "argv", ["run.py", "--first", "--harness", "claude"]))
            stack.enter_context(mock.patch.object(run.sys, "stdin", stdin))
            stack.enter_context(mock.patch.object(run.sys, "stdout", stdout))
            stack.enter_context(mock.patch.object(run, "open_db", return_value=con))
            stack.enter_context(mock.patch.object(
                run.seed_skills, "sync_engine_skills", return_value=[]))
            stack.enter_context(mock.patch.object(
                run, "authenticate", return_value={"user_id": 1}))
            stack.enter_context(mock.patch.object(
                run, "flavor_defaults", return_value=fdefaults))
            stack.enter_context(mock.patch.object(run, "list_shells", return_value=[chosen]))
            stack.enter_context(mock.patch.object(run, "pick_shell", return_value=chosen))
            stack.enter_context(mock.patch.object(run, "ensure_harness_path"))
            stack.enter_context(mock.patch.object(
                run.style, "spinner", side_effect=recording_spinner))
            stack.enter_context(mock.patch.object(
                run, "open_session", return_value=("0001", 1)))
            stack.enter_context(mock.patch.object(
                run.session_supervisor, "supersede_for_enter", return_value=None))
            stack.enter_context(mock.patch.object(run.ports_mod, "resolve", return_value={}))
            stack.enter_context(mock.patch.object(run, "ensure_worktree"))
            stack.enter_context(mock.patch.object(run, "sync_worktree", sync))
            stack.enter_context(mock.patch.object(
                run, "link_worktree_map", return_value=None))
            stack.enter_context(mock.patch.object(run.git_prune, "prune", prune))
            stack.enter_context(mock.patch.object(
                run.git_prune, "status_line", return_value=None))
            stack.enter_context(mock.patch.object(run, "compose_boot", return_value="boot"))
            stack.enter_context(mock.patch.object(
                run.flat, "render_skill_md", return_value={"written": [], "skipped": []}))
            stack.enter_context(mock.patch.object(run, "atomic_write"))
            stack.enter_context(mock.patch.object(run, "load_adapter", return_value=adapter))
            stack.enter_context(mock.patch.object(run, "emit_adapter", return_value=[]))
            stack.enter_context(mock.patch.object(run, "resolve_opencode_plugins"))
            stack.enter_context(mock.patch.object(run, "apply_merge_json", return_value=[]))
            stack.enter_context(mock.patch.object(run, "apply_sandbox", return_value=[]))
            stack.enter_context(mock.patch.object(run, "set_terminal_tab_title"))
            stack.enter_context(mock.patch.object(run.os, "chdir"))
            stack.enter_context(mock.patch.object(
                run.session_supervisor, "supervise", side_effect=_ExecReached))
            with self.assertRaises(_ExecReached):
                run.main()

        return labels, sync.call_count, prune.call_count

    def test_admin_boot_with_prune_disabled_skips_both_phase_labels(self) -> None:
        labels, sync_calls, prune_calls = self._run_main(admin=True, no_prune=True)

        self.assertEqual([
            "sweeping analytics",
            "opening session",
            "rendering boot doc + skills",
        ], labels)
        self.assertEqual(0, sync_calls)
        self.assertEqual(0, prune_calls)

    def test_worktree_boot_with_prune_enabled_reports_every_phase(self) -> None:
        labels, sync_calls, prune_calls = self._run_main(admin=False, no_prune=False)

        self.assertEqual([
            "sweeping analytics",
            "opening session",
            "syncing worktree",
            "pruning merged branches",
            "rendering boot doc + skills",
        ], labels)
        self.assertEqual(1, sync_calls)
        self.assertEqual(1, prune_calls)


if __name__ == "__main__":
    unittest.main()
