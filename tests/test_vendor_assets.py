#!/usr/bin/env python3
"""Vendored assets resolve against the tree as it is NOW — spec #48
(sprint 45, unit 11).

The defect these pin is a rate mismatch, not a wrong line: the route table was
built once at import while file BODIES were read per request, so pulling the
repo under a running server served a current `app.js` to a browser whose newly
vendored dependency the same server would not serve. Nothing in the units that
vendored it was wrong; the split was in the server, which is why the fix has
to be a resolution rule rather than another registration.

Everything here runs over a real socket through the real `dispatch_http`
against a disposable UI tree (the fixture is shared with
tests/test_ui_freshness.py), because half the properties — containment, the
404's shape, HEAD carrying no body — are about what crosses the wire and are
invisible to a test that calls the resolver directly.

Containment is asserted on the BODY, never on the status alone: a 404 that
still leaked the file's bytes would pass a status-only check.

Run:
    python3 -m pytest tests/test_vendor_assets.py
"""
from __future__ import annotations

import socket
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from test_ui_freshness import ServedAssetTestCase  # noqa: E402

# The bytes containment must never hand back. Named for what it is — a
# canary, not a credential: CodeQL reads a fixture called SECRET as stored
# sensitive data and files a high-severity alert against the test.
CANARY = "CANARY-OUTSIDE-THE-VENDOR-ROOT\n"


class VendoredAssetTest(ServedAssetTestCase):

    def raw(self, method, path):
        """Everything the server actually wrote, headers and all. The
        transport closes the connection per response, so EOF is the end."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
            s.sendall(f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                      "Connection: close\r\n\r\n".encode())
            chunks = []
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)

    def setUp(self):
        super().setUp()
        self.vendor = self.ui / "vendor"
        (self.vendor / "xterm").mkdir(parents=True)
        (self.vendor / "xterm" / "xterm.js").write_text("globalThis.Terminal = 1;\n")
        # Inside UI_DIR but above the vendor root — the reach the old frozen
        # table denied by having no route at all.
        (self.ui / "offlimits.js").write_text(CANARY)

    # -- the incident itself --------------------------------------------------

    def test_an_asset_vendored_after_the_server_started_is_reachable(self):
        """The whole unit, in one test: the server is already running when the
        file appears — exactly what a `git pull` under a live engine does."""
        self.assertEqual(self.get("/vendor/xterm/addon-fit.js")[0], 404)
        (self.vendor / "xterm" / "addon-fit.js").write_text(
            "globalThis.FitAddon = 1;\n")
        status, headers, body = self.get("/vendor/xterm/addon-fit.js")
        self.assertEqual(status, 200, "an asset vendored under a running "
                                      "server stayed unreachable")
        self.assertEqual(body, b"globalThis.FitAddon = 1;\n")
        self.assertEqual(headers.get("Content-Type"),
                         "application/javascript; charset=utf-8")

    def test_a_vendored_file_rewritten_under_the_server_serves_the_new_bytes(self):
        first = self.get("/vendor/xterm/xterm.js")[2]
        (self.vendor / "xterm" / "xterm.js").write_text("globalThis.Terminal = 2;\n")
        self.assertNotEqual(self.get("/vendor/xterm/xterm.js")[2], first)

    # -- containment ----------------------------------------------------------

    def test_traversal_is_contained_and_no_variant_leaks_a_byte(self):
        """Decoding has to happen BEFORE containment: `%2e%2e` is the spelling
        that survives a check applied to the raw path."""
        for path in ("/vendor/../offlimits.js",
                     "/vendor/%2e%2e/offlimits.js",
                     "/vendor/xterm/../../offlimits.js",
                     "/vendor/xterm/%2e%2e/%2e%2e/offlimits.js",
                     "/vendor/..%2fofflimits.js",
                     "/vendor/./../offlimits.js"):
            with self.subTest(path=path):
                status, _, body = self.get(path)
                self.assertEqual(status, 404)
                self.assertNotIn(b"CANARY", body,
                                 f"{path} answered 404 and served the file anyway")

    def test_a_percent_encoded_name_is_decoded_before_it_is_resolved(self):
        """The other half of decode-then-contain, and the half a traversal
        battery cannot see: with the decode removed every `..` spelling still
        404s (on a path that simply does not exist), so only a legitimate
        encoded name shows whether decoding happens at all."""
        (self.vendor / "space name.js").write_text("encoded\n")
        status, _, body = self.get("/vendor/space%20name.js")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"encoded\n")

    def test_a_symlink_out_of_the_tree_is_bounded_like_dot_dot(self):
        """Resolution precedes the bound check, which is the only ordering
        that catches this — a symlink's own path is squeaky clean."""
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "escape.js").write_text(CANARY)
        (self.vendor / "escape.js").symlink_to(outside / "escape.js")
        status, _, body = self.get("/vendor/escape.js")
        self.assertEqual(status, 404)
        self.assertNotIn(b"CANARY", body)

    def test_an_absolute_path_does_not_escape_the_join(self):
        """`root / "/etc/hosts"` is `/etc/hosts` in pathlib — the join itself
        is an escape hatch, so the bound check is what has to catch it."""
        status, _, body = self.get("/vendor//" + str(self.ui / "offlimits.js"))
        self.assertEqual(status, 404)
        self.assertNotIn(b"CANARY", body)

    # -- the allowlist --------------------------------------------------------

    def test_only_allowlisted_suffixes_are_served_from_the_vendor_root(self):
        """The planted files are real, readable, and inside the root: the
        suffix is the only thing standing between them and the operator."""
        for name, content in (("planted.py", "print('reachable')\n"),
                              ("planted.sql", "SELECT 1;\n"),
                              ("planted.env", "TOKEN=x\n"),
                              ("planted.map", '{"version":3}\n')):
            (self.vendor / name).write_text(content)
            with self.subTest(name=name):
                status, _, body = self.get("/vendor/" + name)
                self.assertEqual(status, 404)
                self.assertNotIn(content.encode(), body)
        # Sanity: the same directory serves an allowlisted file happily, so
        # the 404s above are the suffix gate and not a broken fixture.
        (self.vendor / "planted.js").write_text("ok\n")
        self.assertEqual(self.get("/vendor/planted.js")[0], 200)

    def test_source_maps_are_off_the_allowlist_deliberately(self):
        """`.map` was ruled out this round (spec #48 open question 3). Pinned
        so re-adding it is a decision someone makes, not a diff someone
        overlooks."""
        (self.vendor / "xterm" / "xterm.js.map").write_text('{"version":3}\n')
        self.assertEqual(self.get("/vendor/xterm/xterm.js.map")[0], 404)

    def test_a_binary_asset_round_trips_byte_identical(self):
        """Every byte value, including the ones no text codec survives — the
        sender was str-only before this unit, so an allowlist with fonts in it
        forces the bytes path."""
        blob = bytes(range(256)) * 4
        (self.vendor / "iosevka.woff2").write_bytes(blob)
        status, headers, body = self.get("/vendor/iosevka.woff2")
        self.assertEqual(status, 200)
        self.assertEqual(body, blob)
        self.assertEqual(headers.get("Content-Type"), "font/woff2")
        self.assertEqual(headers.get("Content-Length"), str(len(blob)))

    # -- what is NOT a file ---------------------------------------------------

    def test_directories_are_never_listed_or_served(self):
        for path in ("/vendor/", "/vendor/xterm/", "/vendor/xterm"):
            with self.subTest(path=path):
                status, _, body = self.get(path)
                self.assertEqual(status, 404)
                self.assertNotIn(b"xterm.js", body,
                                 f"{path} enumerated the directory")

    def test_a_directory_named_like_an_asset_is_still_not_a_file(self):
        (self.vendor / "trap.js").mkdir()
        self.assertEqual(self.get("/vendor/trap.js")[0], 404)

    def test_an_unresolvable_path_is_a_miss_not_a_server_error(self):
        """A NUL byte makes the filesystem itself refuse the name. That is a
        404, not the 500 an unguarded `resolve()` would raise into."""
        status, _, _ = self.get("/vendor/x%00y.js")
        self.assertEqual(status, 404)

    # -- the 404 says which gate said no --------------------------------------

    def test_each_rejection_names_the_gate_that_made_it(self):
        """The SHAPE of a 404 was the entire diagnosis in the outage this spec
        came from (a JSON route-miss vs a text `not built`). A per-request
        resolver collapses that distinction, so the reason is spelled out
        instead — one loopback operator's only reading material."""
        (self.vendor / "planted.py").write_text("x\n")
        cases = {
            "/vendor/planted.py": b"suffix not allowlisted",
            "/vendor/../offlimits.js": b"outside the vendor root",
            "/vendor/xterm/missing.js": b"no such file",
        }
        for path, reason in cases.items():
            with self.subTest(path=path):
                status, headers, body = self.get(path)
                self.assertEqual(status, 404)
                self.assertEqual(body, reason)
                self.assertTrue(headers.get("Content-Type", "")
                                .startswith("text/plain"))

    # -- freshness, unchanged from the shell files -----------------------------

    def test_an_unchanged_vendored_asset_answers_304(self):
        _, headers, _ = self.get("/vendor/xterm/xterm.js")
        self.assertEqual(headers.get("Cache-Control"), "no-cache")
        status, _, body = self.get("/vendor/xterm/xterm.js",
                                   {"If-None-Match": headers["ETag"]})
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")

    def test_changed_bytes_answer_200_with_a_new_tag(self):
        _, first, _ = self.get("/vendor/xterm/xterm.js")
        (self.vendor / "xterm" / "xterm.js").write_text("globalThis.Terminal = 3;\n")
        status, headers, body = self.get("/vendor/xterm/xterm.js",
                                         {"If-None-Match": first["ETag"]})
        self.assertEqual(status, 200)
        self.assertIn(b"= 3", body)
        self.assertNotEqual(headers.get("ETag"), first["ETag"])

    # -- HEAD, because the app shell's honest-failure probe uses it ------------

    def test_a_vendored_asset_answers_head_with_headers_and_no_body(self):
        """Without this the probe in ui/app.js reads 405 for every HEALTHY
        script and reports a floor that cannot serve the build — the exact
        dishonest message the other half of this unit removes.

        The empty body is read off a RAW socket, because http.client declines
        to read a body after a HEAD whatever the server sent: asserting on the
        parsed response proves the client's manners, not the server's. (Made
        the mutation that writes the body anyway go green, which is how this
        apparatus got replaced.)
        """
        get_status, get_headers, get_body = self.get("/vendor/xterm/xterm.js")
        status, headers, body = self.request("HEAD", "/vendor/xterm/xterm.js")
        self.assertEqual((get_status, status), (200, 200))
        raw = self.raw("HEAD", "/vendor/xterm/xterm.js")
        head, _, after_headers = raw.partition(b"\r\n\r\n")
        self.assertEqual(after_headers, b"",
                         "HEAD sent a body after its headers")
        self.assertIn(b"Content-Length: %d" % len(get_body), head)
        self.assertEqual(body, b"")
        self.assertEqual(headers.get("Content-Length"), str(len(get_body)))
        self.assertEqual(headers.get("Content-Type"),
                         get_headers.get("Content-Type"))
        self.assertEqual(headers.get("ETag"), get_headers.get("ETag"))

    def test_head_reports_a_missing_vendored_asset_as_404(self):
        """The probe's whole job: tell "served" from "not served"."""
        self.assertEqual(self.request("HEAD", "/vendor/xterm/addon-fit.js")[0], 404)
        (self.vendor / "xterm" / "addon-fit.js").write_text("x\n")
        self.assertEqual(self.request("HEAD", "/vendor/xterm/addon-fit.js")[0], 200)

    def test_head_stays_unavailable_off_the_vendor_route(self):
        """Narrow on purpose: HEAD on the API is not a contract this server
        offers, and this unit is not the place to start offering it."""
        for path in ("/app.js", "/", "/api/health"):
            with self.subTest(path=path):
                self.assertEqual(self.request("HEAD", path)[0], 405)

    # -- the deliberate limit --------------------------------------------------

    def test_new_top_level_shell_files_stay_frozen(self):
        """Spec #48 keeps the four shell files on the frozen table — a route
        change its author is looking at. Opening UI_DIR itself would widen
        filesystem tenancy for no observed benefit, so a new top-level file
        still needs a restart, and that is a judgement rather than an
        oversight."""
        (self.ui / "extra.js").write_text("console.log('new');\n")
        self.assertEqual(self.get("/extra.js")[0], 404)
        self.assertEqual(self.get("/app.js")[0], 200)


if __name__ == "__main__":
    unittest.main()
