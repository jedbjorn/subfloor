#!/usr/bin/env python3
"""The launcher's host-side operator surface — URLs and the command chart.

  - `./sc url` prints this fork's DERIVED ports — a fork whose offset is not
    0 must not be told 8800 (ports.py owns the derivation, nobody restates it)
  - `enter` / `enter-<shortname>` restate the links BEFORE handing the
    terminal to the harness
  - every dispatchable verb is charted in `./sc help` — enumerated from the
    dispatcher itself, so a verb added without a help line fails here rather
    than becoming invisible to its operator

Run:
    python3 tests/test_operator_surface.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Dispatchable but deliberately absent from the operator chart. Anything else
# missing is drift, not a decision.
UNCHARTED_BY_DESIGN = {
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
    lines = (ROOT / ".super-coder" / "scripts" / "dispatch.sh").read_text().splitlines()
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
                         (ROOT / ".super-coder" / "scripts" / "dispatch.sh").read_text(),
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
        self._stub(
            "docker",
            "#!/bin/sh\n"
            "[ -n \"$SC_DOCKER_LOG\" ] && printf '%s\\n' \"$*\" > \"$SC_DOCKER_LOG\"\n"
            "exit 0\n",
        )
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

    def test_enter_dispatches_direct_boot(self):
        log = self.bin / "docker.log"
        cases = ((("enter",), "./sc boot"), (("enter-DEV1",), "./sc boot DEV1"))
        for argv, target in cases:
            with self.subTest(verb=argv[0]):
                out = sc(*argv, env=self._env(SC_DOCKER_LOG=str(log)))
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertIn(target, log.read_text())
                self.assertNotIn("interface", log.read_text().lower())

    def test_an_underivable_url_never_costs_the_operator_their_shell(self):
        """`sc` runs under `set -e`, so an unguarded print is a new way for
        `enter` — the main entry path — to abort before the harness.

        Both entry verbs, because both carry their own guard: `enter-*` is a
        second dispatch line, not a fallthrough into `enter`, and it is the
        one `make dos-e s=<shell>` uses."""
        env = self._env(SC_PYTHON=str(self.bin / "no-ports-python"))
        for argv in (("enter",), ("enter-DEV1",)):
            with self.subTest(verb=argv[0]):
                out = sc(*argv, env=env)
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

    def test_update_help_matches_ff_only_runtime_contract(self):
        help_text = sc("help").stdout
        update_block = help_text.split("./sc update", 1)[1].split(
            "./sc update-harnesses", 1
        )[0]
        self.assertIn("git pull --ff-only", update_block)
        self.assertIn("engine update continues", update_block)

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

    def test_removed_v1_sprint_verbs_are_unknown_and_absent_from_help(self):
        help_text = sc("help").stdout
        for verb in (
            "directives",
            "events",
            "watch",
            "watch-daemon-up",
            "watch-daemon-down",
            "watch-daemon-install",
            "watch-daemon-uninstall",
        ):
            with self.subTest(verb=verb):
                out = sc(verb)
                self.assertEqual(out.returncode, 2)
                self.assertEqual(out.stdout, "")
                self.assertEqual(
                    out.stderr,
                    f"sc: unknown command '{verb}' (try ./sc help)\n",
                )
                self.assertNotRegex(
                    help_text,
                    rf"(?<![\w-])(?:\./)?sc {re.escape(verb)}(?![\w-])",
                )


if __name__ == "__main__":
    unittest.main()
