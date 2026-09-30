"""Feature #88 R1: deterministic buffered request timing and diagnostics."""
from __future__ import annotations

import asyncio
import io
import json
import re
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "api"))
sys.path.insert(0, str(ENGINE / "scripts"))

import conversation_routes
import db_driver
import log_lines
import request_timing
import server
import transport


class Writer:
    def __init__(self, *, disconnect=False):
        self.data = bytearray()
        self.disconnect = disconnect

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        if self.disconnect and b"data:" in self.data:
            raise ConnectionError("client disconnected")

    def close(self):
        pass


def parsed_response(writer):
    head, body = bytes(writer.data).split(b"\r\n\r\n", 1)
    lines = head.decode().split("\r\n")
    headers = dict(line.split(": ", 1) for line in lines[1:])
    return int(lines[0].split()[1]), headers, body


def timing_values(headers):
    value = headers["Server-Timing"]
    match = re.fullmatch(
        r"queue;dur=(\d+\.\d+), app;dur=(\d+\.\d+), db;dur=(\d+\.\d+)",
        value,
    )
    assert match is not None, value
    return tuple(float(value) for value in match.groups())


class RequestTimingCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.recorder = request_timing.RequestTimingRecorder(started_at="test-start")

    async def request(self, handler, path="/api/items/42", *, method="GET",
                      headers="Host: localhost", body=b"", clock=None,
                      stream_handler=None, ws_handler=None, log=None,
                      reader=None, writer=None):
        instance = transport.Transport(
            "127.0.0.1", 0, handler, ws_handler,
            stream_handler=stream_handler, recorder=self.recorder,
            clock=clock or (lambda: 0.0), log=log or (lambda _: None),
        )
        reader = reader or asyncio.StreamReader()
        reader.feed_data(
            f"{method} {path} HTTP/1.1\r\n{headers}\r\n"
            f"Content-Length: {len(body)}\r\n\r\n".encode() + body
        )
        reader.feed_eof()
        writer = writer or Writer()
        await instance._on_connection(reader, writer)
        return parsed_response(writer)


class RequestTimingTransportTest(RequestTimingCase):
    async def test_real_api_json_static_and_error_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            ui = Path(tmp)
            (ui / "app.js").write_text("console.log('timed');")
            with mock.patch.object(server, "UI_DIR", ui):
                for path, status in (("/app.js", 200), ("/missing", 404)):
                    with self.subTest(path=path):
                        actual, headers, _ = await self.request(server.dispatch_http, path)
                        self.assertEqual(actual, status)
                        self.assertEqual(timing_values(headers), (0, 0, 0))
        actual, headers, body = await self.request(server.dispatch_http, method="TRACE")
        self.assertEqual(actual, 405)
        self.assertTrue(json.loads(body))
        self.assertEqual(timing_values(headers), (0, 0, 0))

    async def test_queue_begins_after_full_body_and_db_openings_accumulate(self):
        calls = []
        # The read completion consumes the first time; executor entry the
        # second. Each connect consumes two more, then handler completion.
        ticks = iter((10.0, 10.125, 10.140, 10.156, 10.170, 10.190, 10.375))

        def handler(method, path, headers, body):
            calls.append(body)
            db_driver.connect("unused")
            db_driver.connect("unused")
            return 200, [("Content-Type", "application/json")], b"{}"

        class SplitBodyReader(asyncio.StreamReader):
            def __init__(self):
                super().__init__()
                self.body_read = False
                self.chunks = iter((
                    b"POST /api/items/42 HTTP/1.1\r\nContent-Length: 4\r\n\r\nab",
                    b"cd",
                ))

            async def read(self, size=-1):
                chunk = next(self.chunks)
                self.body_read = chunk == b"cd"
                return chunk

        reader = SplitBodyReader()

        def clock():
            self.assertTrue(reader.body_read, "timing began before body read completed")
            return next(ticks)

        with mock.patch.object(db_driver, "_connect", return_value=object()):
            status, headers, _ = await self.request(
                handler, clock=clock, reader=reader,
            )
        self.assertEqual(status, 200)
        self.assertEqual(calls, [b"abcd"])
        self.assertEqual(timing_values(headers), (125, 250, 36))
        self.assertIsNone(db_driver._CONNECTION_TIMING.get())
        self.assertEqual(self.recorder.snapshot()["templates"]["POST /api/items/{id}"]["count"], 1)

    async def test_handler_exception_is_timed_and_redacts_exception(self):
        logs = []

        def fail(*args):
            raise ValueError("secret-shaped-exception")

        ticks = iter((0, 0.125, 0.5))
        status, headers, body = await self.request(
            fail, path="/api/items/42?token=secret-shaped-query",
            clock=lambda: next(ticks), log=logs.append,
        )
        self.assertEqual(status, 500)
        self.assertEqual(timing_values(headers), (125, 375, 0))
        self.assertEqual(json.loads(body)["error"]["code"], "INTERNAL_ERROR")
        self.assertNotIn("secret-shaped", "\n".join(logs))
        self.assertIn("status=500", logs[-1])

        status, headers, _ = await self.request(fail, path="http://[invalid?token=secret")
        self.assertEqual(status, 500)
        self.assertEqual(timing_values(headers), (0, 0, 0))

    async def test_real_sse_and_websocket_bypass_buffered_timing(self):
        handler = mock.Mock(side_effect=AssertionError("buffered handler called"))
        event = {"sequence": 1, "event_type": "test", "payload": {}}
        with (
            mock.patch.object(conversation_routes, "_stream_authorize", return_value={}),
            mock.patch.object(conversation_routes, "_event_batch", return_value=[event]),
        ):
            status, headers, _ = await self.request(
                handler, "/api/conversations/cv_" + "a" * 32 + "/events",
                stream_handler=conversation_routes.stream_events,
                writer=Writer(disconnect=True),
            )
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/event-stream")
        self.assertNotIn("Server-Timing", headers)

        async def ws(reader, writer, raw):
            writer.write(b"HTTP/1.1 101 Switching Protocols\r\n\r\n")
            writer.close()

        status, headers, _ = await self.request(
            handler, headers="Host: localhost\r\nConnection: Upgrade\r\nUpgrade: websocket",
            ws_handler=ws,
        )
        self.assertEqual(status, 101)
        self.assertNotIn("Server-Timing", headers)
        self.assertEqual(self.recorder.snapshot()["templates"], {})
        handler.assert_not_called()

    async def test_slow_log_threshold_and_no_request_content(self):
        output = io.StringIO()
        stamp = lambda: datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)
        writer = log_lines.TimestampedWriter(output, clock=stamp)
        ticks = iter((0, 0.125, 0.499, 1, 1.125, 1.5))
        path = "/api/conversations/cv_" + "a" * 32 + "?token=sk-secret-shaped-value&refresh=private"
        for _ in range(2):
            await self.request(
                lambda *args: (201, [], b"ok"), path, method="POST",
                headers="Host: localhost\r\nAuthorization: Bearer header-secret",
                body=b"body-secret", clock=lambda: next(ticks),
                log=lambda line: print(line, file=writer),
            )
        lines = output.getvalue().splitlines()
        self.assertEqual(lines, [
            "2026-09-30T12:00:00Z request timing method=POST "
            "template=POST /api/conversations/{id}?refresh status=201 "
            "queue_ms=125.000 app_ms=375.000 db_ms=0.000"
        ])
        for secret in ("sk-secret-shaped-value", "header-secret", "body-secret", "private", "a" * 32, path):
            self.assertNotIn(secret, output.getvalue())

    def test_concurrent_slow_requests_each_get_a_complete_timestamped_line(self):
        contended = threading.Event()
        barrier = threading.Barrier(2)
        local = threading.local()

        def clock():
            local.calls = getattr(local, "calls", 0) + 1
            return 0 if local.calls == 1 else 0.5

        def handler(*args):
            barrier.wait(timeout=10)
            return 200, [], b"{}"

        class SchedulingStream(io.StringIO):
            def write(self, value):
                written = super().write(value)
                if "request timing" in value:
                    # Pause between print's text and newline until the other
                    # executor thread attempts to acquire the emission lock.
                    if not contended.wait(timeout=10):
                        raise AssertionError("second log did not contend")
                return written

        class ObservedLock:
            def __init__(self, lock):
                self.lock = lock

            def __enter__(self):
                if not self.lock.acquire(blocking=False):
                    contended.set()
                    self.lock.acquire()

            def __exit__(self, *args):
                self.lock.release()

        output = SchedulingStream()
        writer = log_lines.TimestampedWriter(
            output, clock=lambda: datetime(2026, 9, 30, 12, tzinfo=timezone.utc),
        )
        instance = transport.Transport(
            "127.0.0.1", 0, handler, None,
            recorder=self.recorder, clock=clock,
        )
        instance._log_lock = ObservedLock(instance._log_lock)
        with mock.patch.object(sys, "stdout", writer):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(
                    instance._timed_handler, "GET", path, "", b"", 0,
                ) for path in ("/api/first", "/api/second")]
                responses = [future.result(timeout=10) for future in futures]
        self.assertEqual([response[0] for response in responses], [200, 200])
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertCountEqual(lines, [
            "2026-09-30T12:00:00Z request timing method=GET "
            f"template=GET /api/{route} status=200 queue_ms=0.000 "
            "app_ms=500.000 db_ms=0.000"
            for route in ("first", "second")
        ])

    def test_conversation_log_cannot_insert_query_into_slow_record(self):
        self._check_conversation_log_interleaving(conversation_first=False)

    def test_conversation_log_cannot_take_slow_records_timestamp(self):
        self._check_conversation_log_interleaving(conversation_first=True)

    def _check_conversation_log_interleaving(self, *, conversation_first):
        partial_written = threading.Event()
        competing_print_done = threading.Event()
        local = threading.local()
        case = self
        secret = "secret-shaped-value"
        conversation_id = "cv_" + "a" * 32
        path = "/api/second?token=" + secret

        class SchedulingWriter(log_lines.TimestampedWriter):
            def write(self, value):
                written = super().write(value)
                # Pause print between its text and newline. The other thread
                # must finish a whole print while this fragment is pending.
                if "conversation close:" in value:
                    if conversation_first:
                        partial_written.set()
                        case.assertTrue(competing_print_done.wait(timeout=10))
                elif "template=GET /api/first " in value:
                    if conversation_first:
                        local.first_timing = True
                    else:
                        partial_written.set()
                        case.assertTrue(competing_print_done.wait(timeout=10))
                elif value == "\n" and getattr(local, "first_timing", False):
                    competing_print_done.set()
                return written

        def clock():
            local.calls = getattr(local, "calls", 0) + 1
            return 0 if local.calls == 1 else 0.5

        def handler(method, request_path, headers, body):
            if request_path == path:
                if not conversation_first:
                    self.assertTrue(partial_written.wait(timeout=10))
                conversation_routes._terminate_closed_processes(conversation_id, [])
                if not conversation_first:
                    competing_print_done.set()
            elif conversation_first:
                self.assertTrue(partial_written.wait(timeout=10))
            return 200, [], b"{}"

        output = io.StringIO()
        writer = SchedulingWriter(
            output, clock=lambda: datetime(2026, 9, 30, 12, tzinfo=timezone.utc),
        )
        instance = transport.Transport(
            "127.0.0.1", 0, handler, None,
            recorder=self.recorder, clock=clock,
        )
        with (
            mock.patch.object(sys, "stdout", writer),
            mock.patch.object(
                conversation_routes.run_mod.shell_liveness, "terminate",
                side_effect=None if conversation_first else OSError(path),
                return_value=[123],
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            futures = [executor.submit(
                instance._timed_handler, "GET", request_path, "", b"", 0,
            ) for request_path in ("/api/first", path)]
            self.assertEqual([future.result(timeout=10)[0] for future in futures], [200, 200])

        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 3)
        timing_lines = [line for line in lines if "request timing" in line]
        self.assertCountEqual(timing_lines, [
            "2026-09-30T12:00:00Z request timing method=GET "
            f"template=GET /api/{route} status=200 queue_ms=0.000 "
            "app_ms=500.000 db_ms=0.000"
            for route in ("first", "second")
        ])
        for line in timing_lines:
            for content in ("conversation close:", secret, conversation_id, path):
                self.assertNotIn(content, line)
        conversation_lines = [line for line in lines if "conversation close:" in line]
        self.assertEqual(len(conversation_lines), 1)
        self.assertTrue(conversation_lines[0].startswith("2026-09-30T12:00:00Z conversation close:"))
        if conversation_first:
            self.assertIn("123 survived SIGKILL", conversation_lines[0])
        else:
            self.assertIn(secret, conversation_lines[0])


class RequestTimingRecorderTest(unittest.TestCase):
    def test_templates_collapse_ids_and_preserve_only_refresh_flag(self):
        for opaque in ("42", "-9", "abcdef0123456789", "cv_" + "a" * 32,
                       "rt_" + "b" * 32, "12345678-abcd-1234-abcd-123456789abc",
                       "msg_8AbCdEfGhIjKlMnOp", "%34%32"):
            with self.subTest(opaque=opaque):
                self.assertEqual(
                    request_timing.route_template("GET", f"/api/items/{opaque}/messages?secret=123"),
                    "GET /api/items/{id}/messages",
                )
        for query in ("refresh", "refresh=", "secret=private&refresh=1", "%72efresh=private"):
            self.assertEqual(
                request_timing.route_template("POST", f"/api/items/1?{query}"),
                "POST /api/items/{id}?refresh",
            )
        self.assertEqual(request_timing.route_template("GET", "/app.js?cache=1"), "GET /app.js")

    def test_last_500_samples_percentiles_and_restart(self):
        recorder = request_timing.RequestTimingRecorder(started_at="fixed-start")
        for value in range(1, 601):
            recorder.record("GET /api/test", value, 2 * value)
        snapshot = recorder.snapshot()
        self.assertEqual(snapshot["started_at"], "fixed-start")
        self.assertEqual(snapshot["templates"], {
            "GET /api/test": {
                "count": 500,
                "queue": {"p50": 350, "p95": 575, "max": 600},
                "app": {"p50": 700, "p95": 1150, "max": 1200},
            }
        })
        self.assertEqual(request_timing.RequestTimingRecorder().snapshot()["templates"], {})

    def test_db_timing_context_isolated_between_concurrent_requests(self):
        barrier = threading.Barrier(2)

        def run_request(duration):
            ticks = iter((0, duration))
            with db_driver.connection_timing(lambda: next(ticks)) as timing:
                barrier.wait(timeout=10)
                db_driver.connect("unused")
                barrier.wait(timeout=10)
            self.assertIsNone(db_driver._CONNECTION_TIMING.get())
            return timing.milliseconds

        with mock.patch.object(db_driver, "_connect", return_value=object()):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(run_request, duration) for duration in (0.01, 0.02)]
                self.assertEqual([future.result(timeout=10) for future in futures], [10, 20])

    def test_failed_db_open_is_accumulated_and_context_resets(self):
        ticks = iter((0, 0.02))
        with mock.patch.object(db_driver, "_connect", side_effect=sqlite3.OperationalError("failed")):
            with db_driver.connection_timing(lambda: next(ticks)) as timing:
                with self.assertRaises(sqlite3.OperationalError):
                    db_driver.connect("unused")
        self.assertEqual(timing.milliseconds, 20)
        self.assertIsNone(db_driver._CONNECTION_TIMING.get())


class RequestTimingApiTest(RequestTimingCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = Path(tmp.name) / "engine.db"
        con = sqlite3.connect(self.db_path)
        con.executescript(
            "CREATE TABLE users(user_id INTEGER, username TEXT, is_active INTEGER);"
            "CREATE TABLE shells(shell_id INTEGER, api_key TEXT, is_deleted INTEGER);"
            "INSERT INTO users VALUES(1, 'operator', 1);"
            "INSERT INTO shells VALUES(1, 'shell-token', 0);"
        )
        con.close()
        for patch in (
            mock.patch.object(conversation_routes, "DB_PATH", self.db_path),
            mock.patch.object(request_timing, "RECORDER", self.recorder),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    async def test_endpoint_database_open_failure_keeps_no_store(self):
        with mock.patch.object(
            conversation_routes, "_db",
            side_effect=sqlite3.OperationalError("secret-shaped-db-path unavailable"),
        ) as connect:
            status, headers, body = await self.request(
                server.dispatch_http, "/api/diagnostics/request-timing",
            )
        connect.assert_called_once_with()
        self.assertEqual(status, 500)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(timing_values(headers), (0, 0, 0))
        self.assertEqual(json.loads(body), {"error": {
            "code": "INTERNAL_ERROR",
            "message": "request timing diagnostics failed", "details": {},
        }})

    async def test_endpoint_database_query_failure_keeps_no_store_and_closes(self):
        for auth in ("", "\r\nAuthorization: Bearer shell-token"):
            with self.subTest(auth=auth):
                con = mock.Mock()
                con.execute.side_effect = sqlite3.OperationalError("secret-shaped-query failed")
                with mock.patch.object(conversation_routes, "_db", return_value=con):
                    status, headers, body = await self.request(
                        server.dispatch_http, "/api/diagnostics/request-timing",
                        headers="Host: localhost" + auth,
                    )
                con.execute.assert_called_once()
                con.close.assert_called_once_with()
                self.assertEqual(status, 500)
                self.assertEqual(headers["Content-Type"], "application/json")
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(timing_values(headers), (0, 0, 0))
                self.assertEqual(json.loads(body), {"error": {
                    "code": "INTERNAL_ERROR",
                    "message": "request timing diagnostics failed", "details": {},
                }})

    async def test_endpoint_operator_only_and_loopback_host(self):
        endpoint = "/api/diagnostics/request-timing"
        status, headers, body = await self.request(server.dispatch_http, endpoint)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(timing_values(headers), (0, 0, 0))
        self.assertEqual(json.loads(body)["started_at"], "test-start")
        for header, expected, code in (
            ("Host: localhost\r\nAuthorization: Bearer shell-token", 403, "OPERATOR_REQUIRED"),
            ("Host: localhost\r\nAuthorization: Bearer unknown", 401, "UNAUTHORIZED"),
            ("Host: localhost\r\nAuthorization: Basic shell-token", 401, "UNAUTHORIZED"),
            ("Host: evil.example", 403, "HOST_NOT_ALLOWED"),
            ("", 403, "HOST_NOT_ALLOWED"),
        ):
            with self.subTest(header=header):
                status, headers, body = await self.request(server.dispatch_http, endpoint, headers=header)
                self.assertEqual(status, expected)
                self.assertEqual(json.loads(body)["error"]["code"], code)
                self.assertEqual(headers["Cache-Control"], "no-store")
        for host in ("127.0.0.1:8800", "[::1]:8800"):
            status, _, _ = await self.request(server.dispatch_http, endpoint, headers=f"Host: {host}")
            self.assertEqual(status, 200)
        with mock.patch.object(conversation_routes, "_db") as connect:
            status, _, _ = await self.request(server.dispatch_http, endpoint, headers="Host: remote.example")
            self.assertEqual(status, 403)
            connect.assert_not_called()

    async def test_endpoint_bounded_id_collapsing_and_no_db_writes(self):
        for index in range(70):
            path = f"/synthetic/route-{chr(97 + index // 26)}{chr(97 + index % 26)}"
            await self.request(lambda *args: (200, [], b"{}"), path)
        self.assertEqual(len(self.recorder.snapshot()["templates"]), 64)
        self.assertEqual(self.recorder.snapshot()["templates"]["other"]["count"], 7)
        status, _, body = await self.request(server.dispatch_http, "/api/diagnostics/request-timing")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["templates"]), 64)
        self.assertEqual(json.loads(body)["templates"]["other"]["count"], 7)
        # Existing templates remain in their own bucket after overflow.
        await self.request(lambda *args: (200, [], b"{}"), "/synthetic/route-aa")
        self.assertEqual(self.recorder.snapshot()["templates"]["GET /synthetic/route-aa"]["count"], 2)

        self.recorder = request_timing.RequestTimingRecorder(started_at="injected-start")
        for opaque in ("cv_" + "a" * 32, "cv_" + "b" * 32):
            await self.request(lambda *args: (200, [], b"{}"), f"/api/conversations/{opaque}?secret=value")
        queries = []
        changes = []

        def read_only_db():
            class ReadOnlyConnection(sqlite3.Connection):
                def close(self):
                    changes.append(self.total_changes)
                    super().close()

            con = sqlite3.connect(self.db_path, factory=ReadOnlyConnection)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA query_only=ON")
            con.set_trace_callback(queries.append)
            return con

        with (
            mock.patch.object(conversation_routes, "_db", side_effect=read_only_db),
            mock.patch.object(request_timing, "RECORDER", self.recorder),
        ):
            status, headers, body = await self.request(server.dispatch_http, "/api/diagnostics/request-timing")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(json.loads(body), {
            "started_at": "injected-start",
            "templates": {
                "GET /api/conversations/{id}": {
                    "count": 2, "queue": {"p50": 0, "p95": 0, "max": 0},
                    "app": {"p50": 0, "p95": 0, "max": 0},
                }
            },
        })
        self.assertEqual(changes, [0])
        self.assertTrue(queries)
        self.assertTrue(all(query.startswith("SELECT ") for query in queries), queries)
