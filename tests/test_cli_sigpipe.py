#!/usr/bin/env python3
"""SIGPIPE hygiene for the `sc` CLI surface (flag #384).

`./sc mem get roadmap | head -1` used to print one line and then a 12-line
BrokenPipeError traceback: Python ignores SIGPIPE, so an early-closed reader
surfaces as an exception out of whatever `print()` was running. Agent shells
pipe `sc` output constantly, so that traceback lands in transcripts fleet-wide.

Three walls here:

  RunCliTest          the mechanism — what `cli_entry.run_cli` returns, what it
                      swallows, and what it must NOT swallow.
  EntrypointsTest     the fan-out — every `__main__` block under scripts/ routes
                      through the one wrapper, so a new script cannot quietly
                      reintroduce the bug, and the retired
                      `signal(SIGPIPE, SIG_DFL)` copies stay retired.
  LivePipeTest        the behavior — real subprocesses against a closed reader.

Two conditions decide whether a closed reader is even *observable*, so the live
tests fix both rather than inheriting them:

  buffering   the sandbox exports PYTHONUNBUFFERED=1, so a print writes to fd 1
              immediately and raises there; without it, stdout is block-buffered
              and the break lands in the flush instead (rc 120, "Exception
              ignored in: <_io.TextIOWrapper ...>"). Every live case runs under
              BOTH — they are different code paths through the wrapper.
  volume      a head-like reader that closes after one line breaks nothing if
              the child already fit its whole output in the 64KB pipe buffer.
              So the head-like child floods (200KB) and the small-output cases
              use a PRE-CLOSED pipe (read end gone before the child starts).
              Neither depends on winning a race with the reader.

`test_instrument_sees_the_bug_unwrapped` is the positive control for all of it:
the same children WITHOUT the wrapper must show the traceback, in every
condition asserted clean below. Note what is deliberately NOT used as a probe —
argparse swallows its own write errors (`--help` down a dead pipe is quiet even
unpatched), so the real-command leg drives a `print()`-bearing command instead.

Run:
    python3 tests/test_cli_sigpipe.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"

sys.path.insert(0, str(SCRIPTS))
import cli_entry  # noqa: E402

MAIN_BLOCK = re.compile(r"^if __name__ == .__main__.:\n(?P<body>(?:.*\n?)*)", re.M)

_BUFFERED = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
_BUFFERED["PYTHONDONTWRITEBYTECODE"] = "1"
_UNBUFFERED = {**_BUFFERED, "PYTHONUNBUFFERED": "1"}
ENVS = (("buffered", _BUFFERED), ("unbuffered", _UNBUFFERED))

CHILD = """\
import sys
sys.path.insert(0, {scripts!r})
{importline}

def main():
    for i in range({lines}):
        print("line %d %s" % (i, "x" * 40))
    return 0

{call}
"""

# Output small enough to stay buffered, a real failure on stderr, a nonzero
# status — the break then happens in the wrapper's flush, AFTER the command has
# already decided its own outcome.
FAILS_AFTER_OUTPUT = """\
import sys
sys.path.insert(0, {scripts!r})
from cli_entry import run_cli

def main():
    print("some output")
    sys.stderr.write("real failure\\n")
    raise SystemExit(3)

sys.exit(run_cli(main))
"""

FLOOD = 5000   # ~200KB: past any pipe buffer, so a late write is guaranteed
FEW = 10       # a few hundred bytes: fits the pipe buffer whole


def _write_child(source: str) -> str:
    fh = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
    fh.write(source)
    fh.close()
    return fh.name


def _preclosed(argv: list[str], env: dict) -> subprocess.CompletedProcess:
    """Run argv with fd 1 on a pipe whose read end is already closed."""
    r, w = os.pipe()
    os.close(r)
    try:
        return subprocess.run(argv, stdout=w, stderr=subprocess.PIPE,
                              text=True, env=env, timeout=60)
    finally:
        os.close(w)


def _headlike(argv: list[str], env: dict) -> tuple[str, str, int]:
    """Read one line, close the pipe, let the writer discover it."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env)
    first = proc.stdout.readline()
    proc.stdout.close()
    err = proc.stderr.read()
    proc.stderr.close()
    return first, err, proc.wait(timeout=60)


class RunCliTest(unittest.TestCase):
    """The wrapper itself: pass-through, quieting, and what stays loud."""

    def test_returns_the_mains_status(self):
        self.assertEqual(cli_entry.run_cli(lambda: 7), 7)
        self.assertEqual(cli_entry.run_cli(lambda a, b=0: a + b, 2, b=3), 5)

    def test_broken_pipe_mid_run_exits_zero(self):
        def main():
            raise BrokenPipeError(32, "Broken pipe")

        self.assertEqual(cli_entry.run_cli(main), 0,
                         "a reader that closed early got what it asked for — "
                         "that is success, not failure")

    def test_system_exit_keeps_its_status(self):
        with self.assertRaises(SystemExit) as caught:
            cli_entry.run_cli(lambda: (_ for _ in ()).throw(SystemExit(2)))
        self.assertEqual(caught.exception.code, 2,
                         "argparse's usage error must not become a success")

    def test_other_exceptions_still_raise(self):
        with self.assertRaises(ValueError):
            cli_entry.run_cli(lambda: (_ for _ in ()).throw(ValueError("boom")))

    def test_silence_stdout_tolerates_a_stream_with_no_fd(self):
        """Under a captured stdout (a test buffer) there is no fd 1 to
        re-point; the wrapper must degrade, not raise a second error."""
        import io

        held, sys.stdout = sys.stdout, io.StringIO()
        try:
            cli_entry._silence_stdout()  # must not raise
            self.assertEqual(cli_entry.run_cli(lambda: 0), 0)
        finally:
            sys.stdout = held


class EntrypointsTest(unittest.TestCase):
    """One mechanism, not N copies — enforced over the whole scripts/ tree."""

    def setUp(self):
        self.blocks = {}
        for path in sorted(SCRIPTS.glob("*.py")):
            m = MAIN_BLOCK.search(path.read_text())
            if m:
                self.blocks[path.name] = m.group("body")

    def test_finder_sees_a_known_entrypoint(self):
        """Positive control: an empty scan would pass every test below."""
        self.assertIn("init_fork.py", self.blocks)
        self.assertGreaterEqual(len(self.blocks), 35,
                                "scripts/ holds ~39 CLI entrypoints (the "
                                "retired CLI modules removed); "
                                "a short scan means the finder broke, not "
                                "that the tree shrank")

    def test_every_entrypoint_routes_through_run_cli(self):
        for name, body in self.blocks.items():
            with self.subTest(script=name):
                self.assertIn("from cli_entry import run_cli", body,
                              f"{name}: __main__ must import the wrapper")
                self.assertIn("run_cli(", body,
                              f"{name}: __main__ must call its main through "
                              "run_cli — a bare main() reintroduces #384")

    def test_no_script_hand_rolls_sigpipe_handling(self):
        for path in sorted(SCRIPTS.glob("*.py")):
            if path.name == "cli_entry.py":
                continue
            with self.subTest(script=path.name):
                self.assertNotIn("SIGPIPE", path.read_text(),
                                 f"{path.name}: SIGPIPE handling belongs to "
                                 "cli_entry alone — the SIG_DFL copies (#299) "
                                 "killed the process with signal 13 and cost "
                                 "the command its own exit status")


class LivePipeTest(unittest.TestCase):
    """Real processes, closed readers, both buffering modes."""

    def setUp(self):
        self.made: list[str] = []

    def tearDown(self):
        for path in self.made:
            os.unlink(path)

    def _child(self, *, wrapped: bool, lines: int) -> str:
        src = CHILD.format(
            scripts=str(SCRIPTS),
            importline="from cli_entry import run_cli" if wrapped else "",
            lines=lines,
            call="sys.exit(run_cli(main))" if wrapped else "sys.exit(main())",
        )
        path = _write_child(src)
        self.made.append(path)
        return path

    def test_instrument_sees_the_bug_unwrapped(self):
        """Known-positive for every clean assertion below: without the wrapper,
        each of these conditions produces the noise on stderr."""
        flood = self._child(wrapped=False, lines=FLOOD)
        few = self._child(wrapped=False, lines=FEW)
        for label, env in ENVS:
            with self.subTest(env=label, reader="pre-closed", out="flood"):
                done = _preclosed([sys.executable, flood], env)
                self.assertIn("BrokenPipeError", done.stderr)
                self.assertNotEqual(done.returncode, 0)
            with self.subTest(env=label, reader="pre-closed", out="few"):
                done = _preclosed([sys.executable, few], env)
                self.assertIn("BrokenPipeError", done.stderr)
                self.assertNotEqual(done.returncode, 0)
            with self.subTest(env=label, reader="head-like", out="flood"):
                _, err, rc = _headlike([sys.executable, flood], env)
                self.assertIn("BrokenPipeError", err)
                self.assertNotEqual(rc, 0)

    def test_head_like_reader_gets_its_lines_and_no_noise(self):
        wrapped = self._child(wrapped=True, lines=FLOOD)
        for label, env in ENVS:
            with self.subTest(env=label):
                first, err, rc = _headlike([sys.executable, wrapped], env)
                self.assertEqual(first, "line 0 " + "x" * 40 + "\n",
                                 "the reader must still get what it asked for")
                self.assertEqual(err, "", "an early-closed reader is not an error")
                self.assertEqual(rc, 0)

    def test_pre_closed_stdout_is_silent(self):
        for lines in (FEW, FLOOD):
            wrapped = self._child(wrapped=True, lines=lines)
            for label, env in ENVS:
                with self.subTest(env=label, lines=lines):
                    done = _preclosed([sys.executable, wrapped], env)
                    self.assertEqual(done.stderr, "")
                    self.assertEqual(done.returncode, 0)

    def test_real_command_is_silent_on_a_closed_reader(self):
        """A real engine entrypoint that prints and needs no DB — `sc` itself
        runs this one on every invocation."""
        argv = [sys.executable, str(SCRIPTS / "artifact_policy.py"), "path", "map-db"]
        for label, env in ENVS:
            with self.subTest(env=label):
                done = _preclosed(argv, env)
                self.assertEqual(done.stderr, "",
                                 "a real command printed to stderr because "
                                 "nobody was reading stdout")
                self.assertEqual(done.returncode, 0)

    def test_real_error_still_reaches_stderr(self):
        """The other wall: quieting the pipe must not quiet failures."""
        argv = [sys.executable, str(SCRIPTS / "init_fork.py"), "--bogus"]
        for label, env in ENVS:
            with self.subTest(env=label):
                done = _preclosed(argv, env)
                self.assertIn("error:", done.stderr)
                self.assertNotIn("BrokenPipeError", done.stderr)
                self.assertEqual(done.returncode, 2,
                                 "a usage error is still a usage error when "
                                 "nobody is reading")

    def test_failure_after_output_keeps_status_and_stays_quiet(self):
        """Output buffered, command fails, THEN the flush hits the dead pipe:
        the status is the command's own, and the pipe adds nothing to stderr.

        Buffered only — that is the condition under which the command reaches
        its own failure before the pipe is discovered. Unbuffered, the first
        print raises and the run is simply interrupted (exit 0, covered above).
        """
        path = _write_child(FAILS_AFTER_OUTPUT.format(scripts=str(SCRIPTS)))
        self.made.append(path)
        done = _preclosed([sys.executable, path], _BUFFERED)
        self.assertEqual(done.stderr, "real failure\n")
        self.assertEqual(done.returncode, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
