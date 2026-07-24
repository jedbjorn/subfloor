#!/usr/bin/env python3
"""The launcher's operator surface — the URLs and the command chart (#50).

The review GUI link went missing from `make dos-e` when `enter` started
routing through `./sc interface enter`: the boot summary moved INSIDE the
tmux pane, where the harness TUI overdraws it. Nothing was deleted; the
output moved somewhere the operator cannot read. These pin the three
surfaces that replaced it, and the chart that lists them:

  - `./sc url` prints this fork's DERIVED ports — a fork whose offset is not
    0 must not be told 8800 (ports.py owns the derivation, nobody restates it)
  - `enter` / `enter-<shortname>` restate the links BEFORE handing the
    terminal to the harness
  - every dispatchable verb is charted in `./sc help` — enumerated from the
    dispatcher itself, so a verb added without a help line fails here rather
    than becoming invisible to its operator
  - the engine sets its own tmux status line, explicitly, instead of
    inheriting whatever ~/.tmux.conf the environment happens to provide

Run:
    python3 tests/test_operator_surface.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"

sys.path.insert(0, str(ENGINE / "scripts"))
import interface_runtime
import ports

HAS_TMUX = shutil.which("tmux") is not None

# Dispatchable but deliberately absent from the operator chart. Anything else
# missing is drift, not a decision.
UNCHARTED_BY_DESIGN = {
    # The in-pane exec target the runtime spawns — a server-only primitive,
    # never an operator verb (same line aliases.mk draws for the Make surface).
    "interface-exec",
    # `./sc help` listing itself is noise.
    "help",
}


def sc(*args: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["./sc", *args], cwd=ROOT, text=True,
                          capture_output=True, check=False,
                          env={**os.environ, "NO_COLOR": "1", **(env or {})})


def fork_ports() -> dict:
    """This fork's ports as the DISPATCHER resolves them. Not re-derived
    here: `./sc` resolves the engine at the MAIN worktree root, so importing
    ports.py out of a linked worktree's own copy answers about a different
    instance.json than the operator's commands use."""
    out = sc("ports")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def dispatch_verbs() -> list[str]:
    """Every top-level verb `sc` dispatches. Read from the dispatcher, not
    restated here — a list maintained alongside the case statement is a list
    that drifts away from it."""
    lines = (ROOT / "sc").read_text().splitlines()
    start = next(i for i, l in enumerate(lines) if 'case "$cmd" in' in l)
    labels: list[str] = []
    for line in lines[start:]:
        m = re.match(r"^ {2}([a-z][a-z0-9|*_-]*)\)", line)
        if m:
            labels.extend(m.group(1).split("|"))
    # `boot-*` / `enter-*` are shortname globs charted under their base verb.
    return sorted({v for v in labels if "*" not in v})


class ScUrlTest(unittest.TestCase):
    """`./sc url` — the recall path once the boot summary has scrolled away."""

    def test_url_prints_the_ports_this_fork_actually_derived(self):
        cfg = fork_ports()
        out = sc("url")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(f"http://127.0.0.1:{cfg['port']}", out.stdout)
        self.assertIn(f"http://127.0.0.1:{cfg['dev_port']}", out.stdout)

    def test_no_port_is_written_into_the_printer(self):
        """Every fork lands on its own offset, so a literal port in the
        printer is right for exactly one fork and silently wrong for the
        rest — the failure `./sc url` exists to prevent."""
        body = re.search(r"^sc_urls\(\) \{.*?^\}",
                         (ROOT / "sc").read_text(),
                         re.DOTALL | re.MULTILINE)
        self.assertIsNotNone(body, "sc_urls() not found in ./sc")
        self.assertNotRegex(body.group(0), r"127\.0\.0\.1:\d",
                            "sc_urls() must derive both ports, never state one")
        self.assertIn("$(port)", body.group(0))
        self.assertIn("$(devport)", body.group(0))


class EnterPreAttachPrintTest(unittest.TestCase):
    """`enter` hands the terminal to the harness — so it states the links
    on the way in, while stdout is still the operator's."""

    def setUp(self):
        self.bin = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.bin)
        self._stub("docker", "#!/bin/sh\nexit 0\n")
        # A python that runs everything except ports.py — the one seam whose
        # failure must not decide whether the operator gets a shell.
        self._stub("no-ports-python",
                   "#!/bin/sh\ncase \"$*\" in *ports.py*) exit 1 ;; esac\n"
                   "exec python3 \"$@\"\n")

    def _stub(self, name: str, body: str) -> Path:
        path = self.bin / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return path

    def _env(self, **extra: str) -> dict:
        return {"PATH": f"{self.bin}:{os.environ['PATH']}", **extra}

    def test_enter_states_the_urls_before_the_docker_exec(self):
        cfg = fork_ports()
        for argv in (("enter",), ("enter-DEV1",)):
            with self.subTest(verb=argv[0]):
                out = sc(*argv, env=self._env())
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertIn(f"http://127.0.0.1:{cfg['port']}", out.stdout)
                self.assertIn(f"http://127.0.0.1:{cfg['dev_port']}", out.stdout)

    def test_an_underivable_url_never_costs_the_operator_their_shell(self):
        """`sc` runs under `set -e`, so an unguarded print is a new way for
        `enter` — the main entry path — to abort before the harness."""
        env = self._env(SC_PYTHON=str(self.bin / "no-ports-python"))
        out = sc("enter", env=env)
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_url_itself_refuses_loudly_rather_than_printing_nothing(self):
        """The recall command is the opposite case: silence plus exit 0 reads
        as "this fork has no GUI"."""
        env = self._env(SC_PYTHON=str(self.bin / "no-ports-python"))
        out = sc("url", env=env)
        self.assertNotEqual(out.returncode, 0)
        self.assertNotIn("http://", out.stdout)
        self.assertIn("could not derive", out.stderr)


class HelpChartTest(unittest.TestCase):
    """A live verb absent from the chart is a verb its operator cannot find."""

    def test_every_dispatch_verb_is_charted(self):
        help_text = sc("help").stdout
        uncharted = [
            v for v in dispatch_verbs()
            if v not in UNCHARTED_BY_DESIGN and not v.startswith("-")
            and not re.search(
                rf"(?<![\w-])(?:\./)?sc {re.escape(v)}(?![\w-])", help_text)
        ]
        self.assertEqual(uncharted, [],
                         f"dispatchable but absent from ./sc help: {uncharted}")

    def test_the_retired_watch_daemon_verbs_are_charted_as_retired(self):
        """Retired but still dispatchable: they print the cutover notice, so
        the chart has to say RETIRED rather than leave them looking live."""
        help_text = sc("help").stdout
        for verb in ("watch-daemon-up", "watch-daemon-install"):
            with self.subTest(verb=verb):
                line = next(l for l in help_text.splitlines()
                            if re.search(rf"sc {verb}(?![\w-])", l))
                self.assertIn("RETIRED", line)

    def test_retired_start_verbs_refuse_instead_of_starting_anything(self):
        up = sc("watch-daemon-up")
        self.assertEqual(up.returncode, 0, up.stderr)
        self.assertIn("RETIRED", up.stdout)
        install = sc("watch-daemon-install")
        self.assertEqual(install.returncode, 1)
        self.assertIn("RETIRED", install.stderr)


@unittest.skipUnless(HAS_TMUX, "needs tmux")
class TmuxStatusLineTest(unittest.TestCase):
    """The engine-owned status line (#50). The engine sets no other tmux
    option and inherits the environment's ~/.tmux.conf, so every option this
    line depends on is stated rather than assumed."""

    PORT = 8842   # a fork that is NOT on the 8800 base offset

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.rt = interface_runtime.InterfaceRuntime(
            str(self.tmp / "shell_db.db"), run_dir=str(self.tmp / "run"))
        # Pinned: an unpinned resolve() re-probes free ports on every call,
        # so the runtime and the assertion could legitimately disagree.
        patch = mock.patch.object(ports, "resolve",
                                  return_value={"port": self.PORT})
        patch.start()
        self.addCleanup(patch.stop)
        self.url = f"http://127.0.0.1:{self.PORT}"
        self.addCleanup(self._kill_server)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _kill_server(self):
        subprocess.run(["tmux", "-S", self.rt.sock, "kill-server"],
                       capture_output=True, check=False)

    def _open(self, window="s1", cols=80, rows=24):
        asyncio.run(self.rt._new_session(window, cols, rows, "sleep 30"))

    def _label(self, window, shortname):
        asyncio.run(self.rt._set_window_label(window, shortname))

    def _render(self, window, fmt):
        out = subprocess.run(
            ["tmux", "-S", self.rt.sock, "display-message", "-p", "-t",
             f"{interface_runtime.TMUX_SESSION}:{window}", fmt],
            capture_output=True, text=True, check=True)
        return out.stdout.strip()

    def test_line_carries_the_shell_and_the_derived_review_url(self):
        self._open()
        self._label("s1", "DEV5")
        self.assertIn("DEV5", self._render("s1", "#{E:status-left}"))
        self.assertIn(self.url, self._render("s1", "#{E:status-right}"))

    def test_each_window_names_its_own_shell_over_one_shared_url(self):
        """One tmux session holds every shell's window, so the shortname is
        per-window state — a session-wide label would name whichever shell
        happened to start first, for all of them."""
        self._open(window="s1")
        self._label("s1", "DEV5")
        subprocess.run(
            ["tmux", "-S", self.rt.sock, "new-window", "-d", "-t",
             f"{interface_runtime.TMUX_SESSION}:", "-n", "s2", "sleep 30"],
            capture_output=True, check=True)
        self._label("s2", "PLN1")
        self.assertIn("DEV5", self._render("s1", "#{E:status-left}"))
        self.assertIn("PLN1", self._render("s2", "#{E:status-left}"))
        for window in ("s1", "s2"):
            self.assertIn(self.url, self._render(window, "#{E:status-right}"))

    def test_the_line_is_stated_over_a_config_that_would_suppress_it(self):
        """`status off` plus tmux's 10-column status-left default is a real
        ~/.tmux.conf, and inheriting it would silently ship a blank line."""
        self._open()
        subprocess.run(
            ["tmux", "-S", self.rt.sock, "set-option", "-t",
             interface_runtime.TMUX_SESSION, "status", "off"],
            capture_output=True, check=True)
        subprocess.run(
            ["tmux", "-S", self.rt.sock, "set-option", "-t",
             interface_runtime.TMUX_SESSION, "status-left-length", "10"],
            capture_output=True, check=True)
        asyncio.run(self.rt._set_status_line())
        self._label("s1", "DEV5")
        self.assertEqual(self._render("s1", "#{status}"), "on")
        self.assertGreaterEqual(
            int(self._render("s1", "#{status-left-length}")),
            len(self._render("s1", "#{E:status-left}")))
        self.assertGreaterEqual(
            int(self._render("s1", "#{status-right-length}")),
            len(self._render("s1", "#{E:status-right}")))

    def test_the_line_costs_the_pane_no_rows(self):
        """The pane is sized to the attaching terminal. A status line that
        took a row would leave every harness TUI one row short of its
        client."""
        self._open(cols=100, rows=30)
        self.assertEqual(self._render("s1", "#{pane_width}x#{pane_height}"),
                         "100x30")

    def test_a_refused_status_line_never_costs_a_shell_its_session(self):
        """Cosmetic, so it is fail-open — but loudly: the failure is logged,
        and the guard covers reading instance.json, not just the tmux call."""
        with mock.patch.object(ports, "resolve",
                               side_effect=OSError("instance.json unreadable")):
            self._open()
        self.assertEqual(self._render("s1", "#{pane_width}"), "80")
        self.assertTrue(self.rt._tmux_session_started)


if __name__ == "__main__":
    unittest.main()
