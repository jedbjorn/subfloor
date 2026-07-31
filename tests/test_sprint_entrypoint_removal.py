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
            ("GET", "/api/sprints"),
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
        for name in (
            "conductor_routes",
            "conductor_runtime",
            "sprint_routes",
            "pr_poller",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(server, name))


class RemovedBrowserSurfaceTest(unittest.TestCase):
    def test_navigation_keeps_the_retained_views_only(self):
        html = INDEX_HTML.read_text()
        buttons = [
            name
            for name in (
                "shells",
                "interface",
                "roadmap",
                "docs",
                "flags",
                "worktrees",
                "map",
                "analytics",
                "scripts",
            )
            if f'data-tab="{name}"' in html
        ]
        self.assertEqual(
            buttons,
            [
                "shells",
                "interface",
                "roadmap",
                "docs",
                "flags",
                "worktrees",
                "map",
                "analytics",
                "scripts",
            ],
        )
        self.assertNotIn('data-tab="sprints"', html)
        self.assertNotIn('id="view-sprints"', html)

    def test_app_has_no_sprint_board_poll_or_analytics_grouping(self):
        source = APP_JS.read_text()
        for removed in (
            '"/sprints',
            "renderSprints",
            "sprintsState",
            "SPRINTS_REFRESH_MS",
            "sprint_ref",
            "sprint_titles",
            "an-sprint",
        ):
            with self.subTest(removed=removed):
                self.assertNotIn(removed, source)
        self.assertIn('interface: ["#view-interface", renderInterface]', source)
        self.assertIn('analytics: ["#view-analytics", renderAnalytics]', source)


if __name__ == "__main__":
    unittest.main()
