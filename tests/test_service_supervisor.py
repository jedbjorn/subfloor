#!/usr/bin/env python3
"""Launch wiring for the API + provider-neutral session dispatcher."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
import service_supervisor  # noqa: E402


class CommandTest(unittest.TestCase):
    def test_serve_starts_api_and_dispatcher_on_the_same_loopback_port(self):
        with mock.patch.dict(os.environ, {"SC_PYTHON": "/test/python"}):
            api, dispatcher = service_supervisor.commands(9123)
        self.assertEqual(
            ["/test/python", str(ENGINE / "api" / "server.py"),
             "--port", "9123"],
            api,
        )
        self.assertEqual(
            ["/test/python", str(ENGINE / "scripts" / "session_dispatcher.py"),
             "--api-base", "http://127.0.0.1:9123"],
            dispatcher,
        )
        self.assertNotIn("0.0.0.0", dispatcher)


if __name__ == "__main__":
    unittest.main()
