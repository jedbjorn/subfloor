"""One entrypoint wrapper for every `sc` script that prints (flag #384).

Python installs SIG_IGN for SIGPIPE, so a reader that closes early — `| head`,
`| grep -m1`, a pager the operator quits — turns into a `BrokenPipeError` in
whatever `print()` happened to be running. The command's real output has already
been delivered; what the operator sees on top of it is a traceback. Agent shells
pipe `sc` output on nearly every turn, so those tracebacks land in transcripts
fleet-wide.

Every `__main__` block under `scripts/` routes its main callable through
`run_cli()` — one mechanism, one place to fix, and `tests/test_cli_sigpipe.py`
fails if a new entrypoint forgets. It replaces the hand-copied
`signal.signal(SIGPIPE, SIG_DFL)` that had accreted in three scripts (#299):
that variant kills the process with signal 13 mid-write, so the shell sees exit
141 and the command gets no say in its own status.

Two things have to be quiet, not one:

    the write   — the `print()` that raises, caught here;
    the flush   — the interpreter's own flush of what is still buffered, which
                  runs after `main()` returns and prints
                  "Exception ignored in: <_io.TextIOWrapper name='<stdout>'>".
                  Re-pointing fd 1 at /dev/null is what silences that one; it
                  cannot be caught from here.

Exit semantics:

    broken pipe interrupts a run   -> 0. The reader asked for N lines and got
                                     them; `head` closing the pipe is success.
    the command already has a status (a return value, argparse's `SystemExit`,
    any other exception)           -> that status stands, unchanged. A closed
                                     reader silences noise; it never converts a
                                     failure into a success.

stderr is never touched — a real error still reaches the operator, whatever
happened to stdout.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Callable
from typing import Any


def _silence_stdout() -> None:
    """Point fd 1 at /dev/null so the interpreter's exit-flush stays quiet."""
    try:
        fd = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        return  # out of descriptors; nothing better to try
    try:
        os.dup2(fd, sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        pass  # stdout is not a real fd (a test buffer): nothing to re-point
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _flush_stdout() -> None:
    """Flush stdout now, so a dead reader is discovered here and not at exit."""
    try:
        sys.stdout.flush()
    except BrokenPipeError:
        _silence_stdout()
    except (OSError, ValueError):
        pass  # a closed or detached stream: not this wrapper's business


def run_cli(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call a CLI main, quietly absorbing a reader that closed stdout early."""
    try:
        rc = fn(*args, **kwargs)
    except BrokenPipeError:
        rc = 0  # the reader closed mid-run; it got what it asked for
    except BaseException:
        _flush_stdout()  # keep the pipe quiet; the raised status still stands
        raise
    _flush_stdout()
    return rc
