#!/usr/bin/env python3
"""Served-asset freshness — spec #43 U3 (issue 3: stale JS after an engine
update).

Read off a real socket through the real `server.dispatch_http`, because the
defect is entirely a matter of what headers a browser receives: served with
no cache directives and no validator, a browser is PERMITTED to reuse a
heuristically-cached `app.js`, so an engine update stayed invisible until the
operator hard-refreshed. Asserting the header dict a helper returns would
prove the string exists; only a conditional request over the wire proves the
server answers one.

Run:
    python3 -m pytest tests/test_ui_freshness.py
"""
from __future__ import annotations

import asyncio
import http.client
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "api"))

import server  # noqa: E402
import transport as transport_mod  # noqa: E402


class ServedAssetTestCase(unittest.TestCase):
    """A throwaway UI dir served by the real transport, so the tests can
    rewrite an asset the way an engine update does without touching the
    shipped one.

    Shared with tests/test_vendor_assets.py (spec #48): both suites need the
    same "a real socket, the real dispatcher, a disposable tree" apparatus,
    and a second copy of it is a fixture that drifts from this one.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ui = Path(self.tmp.name)
        (self.ui / "app.js").write_text("console.log('v1');\n")
        (self.ui / "style.css").write_text("body { color: red; }\n")
        (self.ui / "index.html").write_text("<!doctype html><title>v1</title>")
        patch = mock.patch.object(server, "UI_DIR", self.ui)
        patch.start()
        self.addCleanup(patch.stop)

        self.loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(self.loop)
            self.transport = transport_mod.Transport(
                "127.0.0.1", 0, server.dispatch_http, self._no_ws,
                log=lambda *_: None)
            self.loop.run_until_complete(self.transport.start())
            self.port = self.transport.port
            ready.set()
            self.loop.run_forever()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        self.assertTrue(ready.wait(10), "transport did not start")

    async def _no_ws(self, reader, writer, head_raw):  # pragma: no cover
        writer.close()

    def tearDown(self):
        asyncio.run_coroutine_threadsafe(
            self.transport.stop(), self.loop).result(timeout=10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=10)
        self.loop.close()

    def request(self, method, path, headers=None):
        con = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            con.request(method, path, headers=headers or {})
            resp = con.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            con.close()

    def get(self, path, headers=None):
        return self.request("GET", path, headers)


class ServedAssetFreshnessTest(ServedAssetTestCase):
    """Spec #43 U3 — cache directives and the revalidation round trip."""

    # -- the validator exists at all -----------------------------------------

    def test_every_served_asset_carries_a_validator_and_must_revalidate(self):
        for path in ("/", "/index.html", "/app.js", "/style.css"):
            with self.subTest(path=path):
                status, headers, body = self.get(path)
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("Cache-Control"), "no-cache")
                self.assertTrue(
                    (headers.get("ETag") or "").startswith('"'),
                    f"{path} served without a quoted entity-tag: "
                    f"{headers.get('ETag')!r}")
                self.assertTrue(body)

    def test_the_app_shell_keeps_its_policy_alongside_the_freshness_headers(self):
        """The CSP and the cache headers are built into one dict now — a
        regression there would drop the security header, not just a
        performance one."""
        status, headers, _ = self.get("/")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Security-Policy"), server._CSP)
        self.assertEqual(headers.get("Cache-Control"), "no-cache")
        self.assertIn("ETag", headers)

    # -- the revalidation round trip -----------------------------------------

    def test_an_unchanged_asset_answers_304_and_sends_no_body(self):
        _, first, body = self.get("/app.js")
        status, headers, second_body = self.get(
            "/app.js", {"If-None-Match": first["ETag"]})
        self.assertEqual(status, 304)
        self.assertEqual(second_body, b"")
        self.assertEqual(headers.get("ETag"), first["ETag"])
        self.assertEqual(headers.get("Cache-Control"), "no-cache")
        # A 304 describes a representation it is not sending; a
        # Content-Length here would claim this response has that many bytes.
        self.assertNotIn("Content-Length", headers)
        self.assertTrue(body, "sanity: the 200 did have a body")

    def test_an_updated_asset_reaches_a_client_holding_the_old_copy(self):
        """The acceptance criterion, end to end: update app.js on disk and the
        next navigation serves the new bytes — no hard refresh."""
        _, first, _ = self.get("/app.js")
        (self.ui / "app.js").write_text("console.log('v2');\n")
        status, headers, body = self.get(
            "/app.js", {"If-None-Match": first["ETag"]})
        self.assertEqual(status, 200, "a client holding the stale tag was "
                                      "told its copy was still current")
        self.assertIn(b"v2", body)
        self.assertNotEqual(headers.get("ETag"), first["ETag"])

    def test_identical_bytes_keep_their_tag_across_a_rebuild(self):
        """The tag is derived from content, not mtime — an engine update that
        rewrites a file byte-identically must not force a re-download."""
        _, first, _ = self.get("/style.css")
        (self.ui / "style.css").write_text("body { color: red; }\n")
        status, _, _ = self.get("/style.css",
                                {"If-None-Match": first["ETag"]})
        self.assertEqual(status, 304)

    def test_each_asset_is_validated_against_its_own_tag(self):
        """One shared tag would 304 style.css for a client holding app.js's."""
        _, app, _ = self.get("/app.js")
        status, _, _ = self.get("/style.css", {"If-None-Match": app["ETag"]})
        self.assertEqual(status, 200)

    # -- what browsers actually send -----------------------------------------

    def test_a_tag_list_and_the_weak_prefix_both_match(self):
        """If-None-Match is a LIST and may arrive weakened (RFC 9110 13.1.2
        mandates weak comparison here). Whole-header equality would miss both
        and re-serve the asset on every navigation — the header would be
        present and useless."""
        _, first, _ = self.get("/app.js")
        for value in (f'"stale-one", {first["ETag"]}',
                      f'W/{first["ETag"]}',
                      "*"):
            with self.subTest(if_none_match=value):
                status, _, _ = self.get("/app.js", {"If-None-Match": value})
                self.assertEqual(status, 304)

    def test_a_foreign_tag_does_not_suppress_the_body(self):
        status, _, body = self.get("/app.js", {"If-None-Match": '"nope"'})
        self.assertEqual(status, 200)
        self.assertIn(b"v1", body)


if __name__ == "__main__":
    unittest.main()
