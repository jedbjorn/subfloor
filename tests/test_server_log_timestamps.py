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
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

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

    def test_flush_keeps_partial_line_buffered_until_newline(self) -> None:
        self.writer.write("pending")
        self.writer.flush()
        self.assertEqual(self.buf.getvalue(), "")
        self.writer.write(" record\n")
        self.assertEqual(self.buf.getvalue(), f"{STAMP}pending record\n")

    def test_partial_line_keeps_its_start_timestamp(self) -> None:
        ticks = iter((fixed_clock(), datetime(2026, 9, 4, 13, tzinfo=timezone.utc)))
        writer = log_lines.TimestampedWriter(self.buf, clock=lambda: next(ticks))
        writer.write("first")
        writer.write("\nsecond\n")
        self.assertEqual(
            self.buf.getvalue(),
            f"{STAMP}first\n2026-09-04T13:00:00Z second\n",
        )

    def test_empty_lines_are_complete_timestamped_records(self) -> None:
        self.writer.write("\nfirst\n\n")
        self.assertEqual(self.buf.getvalue(), f"{STAMP}\n{STAMP}first\n{STAMP}\n")

    def test_stdout_and_stderr_share_complete_line_emission_lock(self) -> None:
        contended = threading.Event()
        ready = threading.Barrier(2)
        case = self

        class ObservedLock:
            def __init__(self):
                self.lock = threading.Lock()

            def __enter__(self):
                if not self.lock.acquire(blocking=False):
                    contended.set()
                    self.lock.acquire()

            def __exit__(self, *args):
                self.lock.release()

        class SchedulingStream(io.StringIO):
            def write(self, value):
                # A destination write need not itself be atomic. Hold its
                # first half until the other wrapper attempts emission.
                case.assertTrue(value.endswith("\n"))
                middle = len(value) // 2
                super().write(value[:middle])
                case.assertTrue(contended.wait(timeout=10))
                super().write(value[middle:])
                return len(value)

        output = SchedulingStream()
        with (
            mock.patch.object(sys, "stdout", output),
            mock.patch.object(sys, "stderr", output),
            mock.patch.object(log_lines, "_EMIT_LOCK", ObservedLock()),
        ):
            log_lines.install(clock=fixed_clock)

            def emit(stream, message):
                ready.wait(timeout=10)
                print(message, file=stream)

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(emit, stream, message) for stream, message in (
                    (sys.stdout, "stdout record"), (sys.stderr, "stderr record"),
                )]
                for future in futures:
                    future.result(timeout=10)
        self.assertCountEqual(output.getvalue().splitlines(), [
            f"{STAMP}stdout record", f"{STAMP}stderr record",
        ])

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
