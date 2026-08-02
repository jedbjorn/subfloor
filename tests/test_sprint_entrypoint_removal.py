#!/usr/bin/env python3
"""Task #167: Sprint v1 is unreachable while retained systems stay mounted."""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
APP_JS = ENGINE / "ui" / "app.js"
INDEX_HTML = ENGINE / "ui" / "index.html"

sys.path.insert(0, str(ENGINE / "api"))
import server  # noqa: E402


class RemovedHttpSurfaceTest(unittest.TestCase):
    def test_every_removed_route_has_the_standard_unknown_response(self):
        routes = (
            ("POST", "/api/sprints"),
            ("GET", "/api/sprint-units?sprint_doc_id=1"),
            ("POST", "/api/sprint-units"),
            ("PATCH", "/api/sprint-units"),
            ("GET", "/api/spec-qaqc-reviews"),
            ("POST", "/api/spec-qaqc-reviews"),
            ("GET", "/api/directives"),
            ("POST", "/api/directives"),
            ("GET", "/api/sentinel-events"),
            ("GET", "/_sc/watches"),
            ("POST", "/_sc/watches"),
            ("POST", "/_sc/watches/reconcile"),
        )
        for method, path in routes:
            with self.subTest(method=method, path=path), mock.patch.object(
                server,
                "db",
                side_effect=lambda: sqlite3.connect(":memory:"),
            ):
                status, _headers, body = server.dispatch_http(
                    method,
                    path,
                    "Host: 127.0.0.1:8800\r\n",
                    b"{}",
                )
            self.assertEqual(status, 404)
            self.assertEqual(json.loads(body), {"error": "not found"})

    def test_retained_route_modules_remain_registered(self):
        self.assertTrue(hasattr(server, "conversation_routes"))
        self.assertTrue(hasattr(server, "review_routes"))
        self.assertTrue(hasattr(server, "sprint_board"))
        for name in (
            "conductor_routes",
            "conductor_runtime",
            "sprint_routes",
            "pr_poller",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(server, name))


class RemovedBrowserSurfaceTest(unittest.TestCase):
    def test_navigation_adds_only_the_new_v2_sprints_view(self):
        html = INDEX_HTML.read_text()
        nav = html[html.index("<nav>"):html.index("</nav>")]
        positions = [
            nav.index(f'data-tab="{name}"')
            for name in ("interface", "sprints", "shells")
        ]
        self.assertEqual(sorted(positions), positions)
        self.assertEqual(1, nav.count('data-tab="sprints"'))
        self.assertEqual(1, html.count('id="view-sprints"'))

    def test_app_uses_new_v2_board_names_without_restoring_v1_state(self):
        source = APP_JS.read_text()
        for removed in (
            "sprintsState",
            "sprint_ref",
            "sprint_titles",
            "an-sprint",
        ):
            with self.subTest(removed=removed):
                self.assertNotIn(removed, source)
        self.assertIn('api("/sprints?limit=100")', source)
        self.assertIn("async function renderSprints", source)
        self.assertIn("const SPRINTS_REFRESH_MS = 5000", source)
        self.assertIn('interface: ["#view-interface", renderInterface]', source)
        self.assertIn('sprints: ["#view-sprints", renderSprints]', source)
        self.assertIn('analytics: ["#view-analytics", renderAnalytics]', source)


if __name__ == "__main__":
    unittest.main()
