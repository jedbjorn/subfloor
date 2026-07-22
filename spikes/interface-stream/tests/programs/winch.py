"""Log terminal sizes on SIGWINCH to a file; draw a corner marker per size.
usage: winch.py <log-file>"""
import os
import signal
import sys
import time

logf = sys.argv[1]


def report(*_args) -> None:
    s = os.get_terminal_size(1)
    with open(logf, "a") as fh:
        fh.write(f"{s.columns}x{s.lines}\n")
    # clear + redraw so the post-resize grid is deterministic (tmux and
    # xterm reflow pre-resize content differently — that's emulator-internal,
    # not stream state)
    marker = f"\x1b[2J\x1b[H\x1b[{s.lines};{s.columns - 2}H###\x1b[1;1H{s.columns}x{s.lines}".encode()
    os.write(1, marker)


signal.signal(signal.SIGWINCH, report)
report()
open(logf + ".ready", "w").close()
while True:
    time.sleep(1)
