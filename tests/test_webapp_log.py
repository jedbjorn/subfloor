#!/usr/bin/env python3
"""Tests for the rolling webapp event log (server.log_event / read_log).

The live publish incident was unexplainable because nothing recorded what the
API did. This log is that record: ONE file, last server.LOG_MAX_EVENTS
end-to-end events, JSON-per-line so each event is a single physical line and the
roll is a line-count trim. Stdlib unittest, tmp log path, no dependencies —
matching test_publish_*.py's style.

Run:
    python3 tests/test_webapp_log.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "api"))
import server  # noqa: E402


class RollingLogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="webapp-log-"))
        self._orig_dir, self._orig_path = server.LOG_DIR, server.LOG_PATH
        server.LOG_DIR = self.tmp / "logs"
        server.LOG_PATH = server.LOG_DIR / "webapp.log"

    def tearDown(self) -> None:
        server.LOG_DIR, server.LOG_PATH = self._orig_dir, self._orig_path
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_caps_at_max_events_keeping_newest(self) -> None:
        n = server.LOG_MAX_EVENTS + 5
        for i in range(n):
            server.log_event("publish", ok=True, detail=[f"event {i}"])
        lines = server.LOG_PATH.read_text().splitlines()
        self.assertEqual(len(lines), server.LOG_MAX_EVENTS)
        events = server.read_log()
        self.assertEqual(len(events), server.LOG_MAX_EVENTS)
        # Oldest dropped, newest kept, in order.
        self.assertEqual(events[0]["detail"], [f"event {n - server.LOG_MAX_EVENTS}"])
        self.assertEqual(events[-1]["detail"], [f"event {n - 1}"])

    def test_one_physical_line_per_event_despite_multiline_trace(self) -> None:
        server.log_event("publish", ok=False, pushed=False, pr_url=None,
                         detail="line one\nline two\nline three")
        lines = server.LOG_PATH.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        ev = json.loads(lines[0])
        self.assertEqual(ev["detail"], ["line one", "line two", "line three"])
        self.assertEqual(ev["op"], "publish")
        self.assertFalse(ev["ok"])
        self.assertIn("ts", ev)

    def test_custom_fields_are_recorded(self) -> None:
        server.log_event("publish", ok=True, pushed=True,
                         pr_url="https://x/pr/1", detail=["done"])
        ev = server.read_log()[0]
        self.assertEqual(ev["pushed"], True)
        self.assertEqual(ev["pr_url"], "https://x/pr/1")

    def test_read_log_tolerates_a_corrupt_line(self) -> None:
        server.LOG_DIR.mkdir(parents=True, exist_ok=True)
        good = json.dumps({"op": "snapshot", "ok": True, "detail": ["ok"]})
        server.LOG_PATH.write_text(good + "\n" + "{not valid json\n")
        events = server.read_log()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["op"], "snapshot")
        self.assertFalse(events[1]["ok"])  # corrupt line surfaced, not dropped

    def test_read_log_empty_when_no_file(self) -> None:
        self.assertEqual(server.read_log(), [])

    def test_logging_never_raises_on_bad_path(self) -> None:
        # Best-effort contract: a logging I/O failure must not break the caller.
        server.LOG_DIR = self.tmp / "logs"
        server.LOG_PATH = self.tmp  # a directory — write_text would raise
        try:
            server.log_event("publish", ok=True, detail=["x"])
        except Exception as e:  # noqa: BLE001
            self.fail(f"log_event raised: {e}")


if __name__ == "__main__":
    unittest.main()
