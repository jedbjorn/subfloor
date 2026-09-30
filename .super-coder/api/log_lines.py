#!/usr/bin/env python3
"""UTC line stamps for the server's stdout/stderr, which land in server.log.

The dispatcher redirects both streams to one file, and the server writes with
bare `print` plus `logging` warnings from the scripts, so nothing in that file
was attributable to a moment in time. The wrapper buffers partial writes per
thread and emits complete, stamped lines under a shared stdout/stderr lock.
"""
from __future__ import annotations

import sys
import threading
from datetime import datetime, timezone

STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# stdout and stderr are separate streams redirected to the same server.log.
_EMIT_LOCK = threading.Lock()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TimestampedWriter:
    """Emit complete lines with a UTC timestamp, keeping fragments per thread."""

    def __init__(self, stream, clock=_utc_now):
        self._stream = stream
        self._clock = clock
        self._pending = threading.local()

    def write(self, text: str) -> int:
        if not text:
            return 0
        stamp = self._clock().strftime(STAMP_FORMAT) + " "
        pending = getattr(self._pending, "text", "")
        lines = []
        segments = text.split("\n")
        for index, segment in enumerate(segments):
            if not pending and (segment or index < len(segments) - 1):
                pending = stamp
            pending += segment
            if index < len(segments) - 1:
                lines.append(pending + "\n")
                pending = ""
        self._pending.text = pending
        if lines:
            with _EMIT_LOCK:
                for line in lines:
                    self._stream.write(line)
        return len(text)

    def flush(self) -> None:
        # Flushing must not expose an incomplete record from any thread.
        with _EMIT_LOCK:
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
