#!/usr/bin/env python3
"""UTC line stamps for the server's stdout/stderr, which land in server.log.

The dispatcher redirects both streams to one file, and the server writes with
bare `print` plus `logging` warnings from the scripts, so nothing in that file
was attributable to a moment in time. The wrapper buffers partial writes per
thread and emits complete, stamped lines under a shared stdout/stderr lock.
"""
from __future__ import annotations

import atexit
import sys
import threading
import weakref
from datetime import datetime, timezone

STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# stdout and stderr are separate streams redirected to the same server.log.
_EMIT_LOCK = threading.Lock()
_WRITERS = weakref.WeakSet()


def _drain_at_exit() -> None:
    for writer in list(_WRITERS):
        if not getattr(writer._stream, "closed", False):
            writer.drain_all()


atexit.register(_drain_at_exit)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TimestampedWriter:
    """Emit complete lines with a UTC timestamp, keeping fragments per thread."""

    def __init__(self, stream, clock=_utc_now):
        self._stream = stream
        self._clock = clock
        # Thread objects avoid identifier reuse and retain dead workers' text
        # until it is drained. Empty buffers do not retain their threads.
        self._pending = {}
        with _EMIT_LOCK:
            _WRITERS.add(self)

    def write(self, text: str) -> int:
        if not text:
            return 0
        stamp = self._clock().strftime(STAMP_FORMAT) + " "
        with _EMIT_LOCK:
            self._write_locked(text, stamp, threading.current_thread())
        return len(text)

    def _write_locked(self, text, stamp, thread) -> None:
        pending = self._pending.get(thread, "")
        segments = text.split("\n")
        for index, segment in enumerate(segments):
            if not pending and (segment or index < len(segments) - 1):
                pending = stamp
            pending += segment
            if index < len(segments) - 1:
                self._stream.write(pending + "\n")
                pending = ""
        if pending:
            self._pending[thread] = pending
        else:
            self._pending.pop(thread, None)

    def _drain_thread_locked(self, thread) -> None:
        pending = self._pending.get(thread)
        if pending:
            self._stream.write(pending + "\n")
            del self._pending[thread]

    def write_record(self, text: str) -> int:
        """Emit a complete record after terminating this thread's fragment."""
        stamp = self._clock().strftime(STAMP_FORMAT) + " "
        thread = threading.current_thread()
        with _EMIT_LOCK:
            self._drain_thread_locked(thread)
            self._write_locked(text if text.endswith("\n") else text + "\n", stamp, thread)
        return len(text)

    def flush(self) -> None:
        with _EMIT_LOCK:
            self._drain_thread_locked(threading.current_thread())
            self._stream.flush()

    def drain_all(self) -> None:
        """Terminate all pending fragments, including those of dead workers."""
        with _EMIT_LOCK:
            for thread in list(self._pending):
                self._drain_thread_locked(thread)
            self._stream.flush()

    def close(self) -> None:
        with _EMIT_LOCK:
            for thread in list(self._pending):
                self._drain_thread_locked(thread)
            self._stream.close()
            _WRITERS.discard(self)

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
