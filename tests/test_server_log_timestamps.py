#!/usr/bin/env python3
"""server.log line stamps: one UTC prefix per line, never two.

Covers the shapes server.py actually produces — `print` in whole lines, chunked
partial writes, and `logging` warnings from the scripts through basicConfig.

Run:
    python3 -m unittest tests.test_server_log_timestamps
"""
from __future__ import annotations

import io
import logging
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "api"))

import log_lines  # noqa: E402

STAMP = "2026-09-04T12:00:00Z "


def fixed_clock() -> datetime:
    return datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)


class CountingStream(io.StringIO):
    """A stream that records flushes, which StringIO otherwise swallows."""

    flushes = 0

    def flush(self) -> None:
        self.flushes += 1


class TimestampedWriterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.buf = io.StringIO()
        self.writer = log_lines.TimestampedWriter(self.buf, clock=fixed_clock)

    def test_each_line_gets_one_prefix(self) -> None:
        self.writer.write("first\nsecond\n")
        self.assertEqual(self.buf.getvalue(), f"{STAMP}first\n{STAMP}second\n")

    def test_partial_writes_share_one_prefix(self) -> None:
        self.writer.write("Subfloor review ")
        self.writer.write("layer starting")
        self.writer.write("\n")
        self.writer.write("next\n")
        self.assertEqual(
            self.buf.getvalue(),
            f"{STAMP}Subfloor review layer starting\n{STAMP}next\n",
        )

    def test_reports_written_length_and_passes_through_flush(self) -> None:
        counting = CountingStream()
        writer = log_lines.TimestampedWriter(counting, clock=fixed_clock)
        self.assertEqual(writer.write("hi\n"), 3)
        writer.flush()
        self.assertEqual(counting.flushes, 1)

    def test_delegates_stream_attributes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server.log"
            with path.open("w", encoding="utf-8") as handle:
                writer = log_lines.TimestampedWriter(handle, clock=fixed_clock)
                self.assertEqual(writer.fileno(), handle.fileno())
                self.assertFalse(writer.isatty())
                self.assertEqual(writer.encoding, "utf-8")

    def test_install_is_idempotent(self) -> None:
        real_out, real_err = sys.stdout, sys.stderr
        self.addCleanup(setattr, sys, "stdout", real_out)
        self.addCleanup(setattr, sys, "stderr", real_err)
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()

        log_lines.install(clock=fixed_clock)
        wrapped_out, wrapped_err = sys.stdout, sys.stderr
        log_lines.install(clock=fixed_clock)

        self.assertIs(sys.stdout, wrapped_out)
        self.assertIs(sys.stderr, wrapped_err)
        print("hello")
        self.assertEqual(sys.stdout._stream.getvalue(), f"{STAMP}hello\n")


class LoggingThroughWriterTest(unittest.TestCase):
    def test_warning_is_prefixed_exactly_once(self) -> None:
        stream = io.StringIO()
        writer = log_lines.TimestampedWriter(stream, clock=fixed_clock)
        handler = logging.StreamHandler(writer)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        log = logging.getLogger("super_coder.db.test")
        log.addHandler(handler)
        log.setLevel(logging.WARNING)
        self.addCleanup(log.removeHandler, handler)

        log.warning("slow write held the lock")

        self.assertEqual(
            stream.getvalue(),
            f"{STAMP}WARNING super_coder.db.test: slow write held the lock\n",
        )


if __name__ == "__main__":
    unittest.main()
