#!/usr/bin/env python3
"""Terminal styling for the launcher — one place for every ANSI decision.

Color is cosmetic, never load-bearing: every helper degrades to the plain
string when stdout is not a TTY, or when NO_COLOR / SC_NO_COLOR is set, so
headless boots, RENDER_ONLY verifies, and piped logs stay byte-clean. Callers
therefore never guard a call site — they style unconditionally and this module
decides. Measured widths (panel) strip ANSI first, so a styled line never
skews a border.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import os
import re
import sys
import threading

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _enabled() -> bool:
    if os.environ.get("NO_COLOR") is not None or os.environ.get("SC_NO_COLOR"):
        return False
    return sys.stdout.isatty()


ON = _enabled()


def _c(code: str):
    def paint(s: object) -> str:
        return f"\x1b[{code}m{s}\x1b[0m" if ON else str(s)
    return paint


bold = _c("1")
dim = _c("2")
accent = _c("38;5;135")   # subfloor purple
cyan = _c("36")
green = _c("32")
yellow = _c("33")
amber = _c("38;5;214")
red = _c("31")


class _Spinner:
    """Small stdout spinner whose lifecycle is owned by a context manager."""

    _FRAMES = "|/-\\"

    def __init__(self, label: str, *, enabled: bool = True) -> None:
        self.label = label
        self._active = enabled and sys.stdout.isatty()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stopped = False

    def start(self) -> None:
        if not self._active:
            return
        self._thread = threading.Thread(target=self._spin, daemon=True)
        try:
            self._thread.start()
        except RuntimeError:
            self._thread = None
            self._active = False

    def stop(self) -> None:
        if self._thread is None or self._stopped:
            return
        self._stopped = True
        self._stop.set()
        self._thread.join()
        sys.stdout.write("\r\x1b[2K")
        sys.stdout.flush()

    def _spin(self) -> None:
        frame = 0
        while not self._stop.is_set():
            sys.stdout.write(f"\r{self._FRAMES[frame]} {self.label}…")
            sys.stdout.flush()
            frame = (frame + 1) % len(self._FRAMES)
            self._stop.wait(0.1)


@contextmanager
def spinner(label: str, *, enabled: bool = True) -> Iterator[_Spinner]:
    """Show liveness on a TTY and always clear the line before returning."""
    state = _Spinner(label, enabled=enabled)
    state.start()
    try:
        yield state
    finally:
        state.stop()


def visible_len(s: str) -> int:
    return len(_ANSI_RE.sub("", s))


def banner(fork: str) -> str:
    """The wordmark standing on its floor line. Fits the brand, two lines."""
    name = f"{bold(accent('subfloor'))} {dim('·')} {fork}"
    floor = dim("─" * max(28, visible_len(name) + 4))
    return f"\n  {name}\n  {floor}"


def panel(lines: list[str], pad: int = 2) -> str:
    """A rounded box around pre-styled lines (ANSI-aware width)."""
    inner = max(visible_len(l) for l in lines) + pad * 2
    top = dim("╭" + "─" * inner + "╮")
    bot = dim("╰" + "─" * inner + "╯")
    body = []
    for l in lines:
        fill = " " * (inner - pad - visible_len(l) - pad)
        body.append(f"{dim('│')}{' ' * pad}{l}{fill}{' ' * pad}{dim('│')}")
    return "\n".join([top, *body, bot])
