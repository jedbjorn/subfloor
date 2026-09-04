#!/usr/bin/env python3
"""UTC line stamps for the server's stdout/stderr, which land in server.log.

The dispatcher redirects both streams to one file, and the server writes with
bare `print` plus `logging` warnings from the scripts, so nothing in that file
was attributable to a moment in time. The wrapper stamps each *line* once, at
the point the line starts, so a partial write keeps a single prefix.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TimestampedWriter:
    """Wrap a text stream, prefixing every line with a UTC timestamp."""

    def __init__(self, stream, clock=_utc_now):
        self._stream = stream
        self._clock = clock
        self._at_line_start = True

    def write(self, text: str) -> int:
        if not text:
            return 0
        stamp = self._clock().strftime(STAMP_FORMAT) + " "
        out = []
        segments = text.split("\n")
        for index, segment in enumerate(segments):
            if segment and self._at_line_start:
                out.append(stamp)
                self._at_line_start = False
            out.append(segment)
            if index < len(segments) - 1:
                out.append("\n")
                self._at_line_start = True
        self._stream.write("".join(out))
        return len(text)

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name):
        # fileno / isatty / encoding and friends belong to the real stream.
        return getattr(self._stream, name)


def install(clock=_utc_now) -> None:
    """Wrap sys.stdout and sys.stderr once; installing twice is a no-op."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if isinstance(stream, TimestampedWriter):
            continue
        setattr(sys, name, TimestampedWriter(stream, clock=clock))
