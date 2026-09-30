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
import subprocess
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

    def test_flush_emits_partial_line_as_separate_record(self) -> None:
        self.writer.write("pending")
        self.writer.flush()
        self.assertEqual(self.buf.getvalue(), f"{STAMP}pending\n")
        self.writer.flush()
        self.writer.write(" record\n")
        self.assertEqual(self.buf.getvalue(), f"{STAMP}pending\n{STAMP} record\n")

    def test_write_record_terminates_only_current_threads_fragment(self) -> None:
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(self.writer.write, "worker pending").result(timeout=10)
        self.writer.write("current pending")
        self.assertEqual(self.writer.write_record("record"), 6)
        self.writer.write_record("already terminated\n")
        self.assertEqual(self.buf.getvalue(), (
            f"{STAMP}current pending\n{STAMP}record\n{STAMP}already terminated\n"
        ))
        self.writer.drain_all()
        self.assertTrue(self.buf.getvalue().endswith(f"{STAMP}worker pending\n"))

    def test_worker_flush_preserves_fragment_before_exit(self) -> None:
        def worker():
            self.writer.write("worker diagnostic without newline")
            self.writer.flush()

        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(worker).result(timeout=10)
        self.assertEqual(self.buf.getvalue(), f"{STAMP}worker diagnostic without newline\n")

    def test_drain_all_retains_dead_workers_fragments(self) -> None:
        for message in ("first worker", "second worker"):
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(self.writer.write, message).result(timeout=10)
        self.writer.write("main thread")
        self.writer.flush()
        self.assertEqual(self.buf.getvalue(), f"{STAMP}main thread\n")
        self.writer.drain_all()
        self.writer.drain_all()
        self.assertEqual(self.buf.getvalue(), (
            f"{STAMP}main thread\n{STAMP}first worker\n{STAMP}second worker\n"
        ))
        self.assertEqual(self.writer._pending, {})

    def test_close_drains_every_thread_before_closing_destination(self) -> None:
        class ClosingStream(io.StringIO):
            def close(self):
                self.at_close = self.getvalue()
                super().close()

        output = ClosingStream()
        writer = log_lines.TimestampedWriter(output, clock=fixed_clock)
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(writer.write, "worker fragment").result(timeout=10)
        writer.write("main fragment")
        writer.close()
        self.assertTrue(output.closed)
        self.assertEqual(output.at_close, f"{STAMP}worker fragment\n{STAMP}main fragment\n")
        self.assertEqual(writer._pending, {})

    def test_write_after_close_rejects_text(self) -> None:
        for text in ("late fragment", "late terminated\n", ""):
            with self.subTest(text=text):
                output = io.StringIO()
                writer = log_lines.TimestampedWriter(output, clock=fixed_clock)
                writer.close()
                with self.assertRaisesRegex(ValueError, "closed"):
                    writer.write(text)
                self.assertEqual(writer._pending, {})
                writer.close()

    def test_multiline_failure_does_not_replay_already_emitted_fragment(self) -> None:
        class FailSecond(io.StringIO):
            calls = 0

            def write(self, text):
                self.calls += 1
                if self.calls == 2:
                    raise OSError("injected failure on second line, before accepting bytes")
                return super().write(text)

        output = FailSecond()
        writer = log_lines.TimestampedWriter(output, clock=fixed_clock)
        writer.write("prior fragment")
        with self.assertRaises(OSError):
            writer.write(" completed\nsecond diagnostic\n")
        self.assertEqual(output.getvalue(), f"{STAMP}prior fragment completed\n")
        self.assertTrue(log_lines._EMIT_LOCK.acquire(blocking=False), "lock leaked after exception")
        log_lines._EMIT_LOCK.release()
        writer.write_record("next record")
        self.assertEqual(output.getvalue(), (
            f"{STAMP}prior fragment completed\n{STAMP}next record\n"
        ))
        self.assertEqual(writer._pending, {})

    def test_process_exit_drains_main_and_dead_worker_fragments(self) -> None:
        code = (
            f"import sys; sys.path.insert(0, {str(ROOT / '.super-coder' / 'api')!r})\n"
            "import log_lines\n"
            "from concurrent.futures import ThreadPoolExecutor\n"
            "from datetime import datetime, timezone\n"
            "log_lines.install(clock=lambda: datetime(2026, 9, 4, 12, tzinfo=timezone.utc))\n"
            "def worker():\n"
            "    sys.stdout.write('worker stdout fragment')\n"
            "    sys.stderr.write('worker stderr fragment')\n"
            "with ThreadPoolExecutor(max_workers=1) as executor:\n"
            "    executor.submit(worker).result()\n"
            "sys.stdout.write('last process diagnostic')\n"
        )
        result = subprocess.run(
            [sys.executable, "-u", "-c", code], capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertCountEqual(result.stdout.splitlines(), [
            f"{STAMP}worker stdout fragment", f"{STAMP}last process diagnostic",
        ])
        self.assertEqual(result.stderr, f"{STAMP}worker stderr fragment\n")

    def test_partial_stdout_and_complete_stderr_remain_distinct(self) -> None:
        out = self.writer
        err = log_lines.TimestampedWriter(self.buf, clock=fixed_clock)
        partial = threading.Event()
        completed = threading.Event()

        def first():
            out.write("stdout fragment ")
            partial.set()
            self.assertTrue(completed.wait(timeout=10))
            out.write("finished\n")

        def second():
            self.assertTrue(partial.wait(timeout=10))
            print("stderr complete", file=err, flush=True)
            completed.set()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(worker) for worker in (first, second)]
            for future in futures:
                future.result(timeout=10)
        self.assertEqual(self.buf.getvalue(), (
            f"{STAMP}stderr complete\n{STAMP}stdout fragment finished\n"
        ))

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
