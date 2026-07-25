#!/usr/bin/env python3
"""Tests for the Account Analytics API routes (spec doc #49, sprint 52 unit 3).

Stdlib `unittest`, matching the sibling suites. Two layers:

  * `QuotaUpsertTest` / `QuotaReadTest` / `QuotaTtlTest` call the assembler
    directly against a throwaway DB built the way the engine ships it
    (schema.sql + every migration in filename order), with `probe_all` stubbed.
  * `QuotaRouteTest` stands up the real `server.Handler` on an ephemeral port
    (the test_mem harness pattern) and drives the two URLs over HTTP, so the
    route paths are pinned BEHAVIOURALLY — a source-substring check would pass
    whether or not the route answers.

Probe results are built with U2's own `quota_probes.account()` / `window()`
builders rather than hand-authored dicts. A hand-authored fixture proves a
mechanism against data the producer cannot emit; going through the real
builders means a change to that seam breaks these tests, which is the point.

No test performs a live call: `probe_all` is stubbed in every case.

Run:
    python3 tests/test_quota_accounts_api.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"

sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))
import quota_probes  # noqa: E402  (the row builders U2 owns)
import server  # noqa: E402


def build_db(path=":memory:") -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA.read_text())
    for mig in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(mig.read_text())
    con.execute("PRAGMA foreign_keys=ON")
    con.commit()
    return con


def iso(minutes_ago: float = 0.0, days_ago: float = 0.0) -> str:
    ts = (datetime.now(timezone.utc)
          - timedelta(minutes=minutes_ago, days=days_ago))
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def acct(provider="anthropic", ref="uuid-1", label="jed@gmail.com", plan="max",
         captured_at=None, status="ok", is_current=1, windows=(), detail=None):
    return quota_probes.account(
        provider=provider, probe_version="1", captured_at=captured_at or iso(),
        account_ref=ref, account_label=label, plan=plan, is_current=is_current,
        status=status, detail=detail, windows=list(windows))


def win(kind="session", scope=None, percent=22.0, used=None, limit_value=None,
        captured_at=None, status="ok"):
    return quota_probes.window(
        window_kind=kind, probe_version="1", captured_at=captured_at or iso(),
        scope=scope, used_percent=percent, used=used, limit_value=limit_value,
        resets_at=iso(), status=status)


class QuotaBase(unittest.TestCase):
    def setUp(self):
        self.con = build_db()
        self.addCleanup(self.con.close)
        # Module state: the TTL's out-of-band clock. Reset per test so one
        # test's probe cannot suppress the next test's.
        saved = dict(server._QUOTA_PROBE)
        self.addCleanup(lambda: server._QUOTA_PROBE.update(saved))
        server._QUOTA_PROBE.update({"at": None, "providers": []})
        self.calls = []

    def stub(self, *results):
        """Stub probe_all with one result list per successive call; the last
        result repeats. Records every call so probe COUNT is assertable."""
        seq = list(results) or [[]]

        def fake(log, *a, **kw):
            self.calls.append(True)
            return seq[min(len(self.calls) - 1, len(seq) - 1)]
        return mock.patch.object(server.quota_dispatch, "probe_all", fake)

    def q(self, sql, *params):
        return self.con.execute(sql, params).fetchall()


class QuotaUpsertTest(QuotaBase):
    """The upsert — the one thing this unit alone can get right."""

    def test_reprobe_updates_the_null_scope_row_instead_of_twinning_it(self):
        # session/weekly/five_hour are ALL scope-NULL, so this is the panel's
        # COMMON path, not an edge case. Under a plain UNIQUE(...scope) SQLite
        # compares NULLs as distinct and every probe would add a duplicate.
        first = [acct(windows=[win("session", None, 22.0)])]
        second = [acct(windows=[win("session", None, 44.0)])]
        with self.stub(first, second):
            server.get_analytics_accounts(self.con)
            server.get_analytics_accounts(self.con, force=True)
        rows = self.q("SELECT used_percent FROM harness_quota_window "
                      "WHERE window_kind='session' AND scope IS NULL")
        self.assertEqual(1, len(rows))
        self.assertEqual(44.0, rows[0]["used_percent"])

    def test_scoped_windows_stay_distinct(self):
        # COALESCE folds NULL to '' — it must not fold two REAL scopes together.
        with self.stub([acct(windows=[win("weekly_scoped", "opus", 10.0),
                                      win("weekly_scoped", "sonnet", 20.0),
                                      win("weekly", None, 30.0)])]):
            server.get_analytics_accounts(self.con)
        self.assertEqual(3, self.q("SELECT COUNT(*) c FROM harness_quota_window")[0]["c"])

    def test_returning_account_keeps_its_original_first_seen(self):
        old = iso(days_ago=30)
        with self.stub([acct(captured_at=old)], [acct(captured_at=iso())]):
            server.get_analytics_accounts(self.con)
            server.get_analytics_accounts(self.con, force=True)
        row = self.q("SELECT first_seen, last_seen FROM harness_quota_account")[0]
        self.assertEqual(old, row["first_seen"])       # never re-minted
        self.assertGreater(row["last_seen"], old)      # but activity advanced

    def test_switching_account_moves_is_current_off_the_old_row(self):
        with self.stub([acct(ref="uuid-1", label="a@x.com")],
                       [acct(ref="uuid-2", label="b@x.com")]):
            server.get_analytics_accounts(self.con)
            server.get_analytics_accounts(self.con, force=True)
        rows = {r["account_ref"]: r["is_current"] for r in
                self.q("SELECT account_ref, is_current FROM harness_quota_account")}
        self.assertEqual({"uuid-1": 0, "uuid-2": 1}, rows)

    def test_account_without_a_ref_writes_no_registry_row(self):
        # No credential file → `na`. The absence of a limit is not a limit of
        # zero, and a row keyed on a null ref could never be matched again.
        na = quota_probes.account(provider="moonshot", probe_version="1",
                                  captured_at=iso(), status="na", is_current=0)
        with self.stub([na]):
            out = server.get_analytics_accounts(self.con)
        self.assertEqual(0, self.q("SELECT COUNT(*) c FROM harness_quota_account")[0]["c"])
        # ...but the provider is still REPORTED, or "no accounts" would be
        # indistinguishable from "every probe failed".
        self.assertEqual([{"provider": "moonshot", "status": "na", "detail": None}],
                         out["providers"])

    def test_unauth_preserves_the_last_known_values(self):
        # "Expiry is reported, not repaired": the card shows what it last knew,
        # with its age — never a measured zero.
        good = [acct(windows=[win("session", None, 61.0, captured_at=iso(minutes_ago=90))])]
        expired = [acct(status="unauth", windows=[])]
        with self.stub(good, expired):
            server.get_analytics_accounts(self.con)
            server.get_analytics_accounts(self.con, force=True)
        row = self.q("SELECT used_percent, captured_at FROM harness_quota_window")[0]
        self.assertEqual(61.0, row["used_percent"])
        self.assertEqual(iso(minutes_ago=90)[:16], row["captured_at"][:16])

    def test_probe_payload_extras_never_reach_the_response(self):
        # A token must not be able to ride out of the probe seam through this
        # layer: the response is built from named keys, never a dict splat.
        leaky = acct()
        leaky["access_token"] = "sk-ant-oat01-SHOULD-NEVER-APPEAR"
        with self.stub([leaky]):
            out = server.get_analytics_accounts(self.con)
        self.assertNotIn("SHOULD-NEVER-APPEAR", json.dumps(out))


class QuotaReadTest(QuotaBase):
    """The 7-day activity window — a filter, never a delete."""

    def test_stale_account_stops_rendering_but_its_row_survives(self):
        with self.stub([acct(ref="old", label="old@x.com", captured_at=iso(days_ago=30))]):
            server.get_analytics_accounts(self.con)
        # Demote it: only the account the credential file resolves to NOW is
        # exempt from the window.
        self.con.execute("UPDATE harness_quota_account SET is_current=0")
        self.con.commit()
        with self.stub([]):
            out = server.get_analytics_accounts(self.con, force=True)
        self.assertEqual([], out["accounts"])
        self.assertEqual(1, self.q("SELECT COUNT(*) c FROM harness_quota_account")[0]["c"])

    def test_current_account_renders_even_when_its_last_seen_has_aged(self):
        # A failed probe leaves last_seen un-advanced; the section must still
        # show the current account rather than an empty page.
        with self.stub([acct(captured_at=iso(days_ago=30))]):
            out = server.get_analytics_accounts(self.con)
        self.assertEqual(["uuid-1"], [a["account_ref"] for a in out["accounts"]])

    def test_windows_attach_to_their_own_account(self):
        with self.stub([acct(ref="a", windows=[win("session", None, 10.0)]),
                        acct(provider="openai", ref="b", windows=[
                            win("weekly", None, 20.0), win("five_hour", None, 30.0)])]):
            out = server.get_analytics_accounts(self.con)
        by_ref = {a["account_ref"]: a for a in out["accounts"]}
        self.assertEqual([10.0], [w["used_percent"] for w in by_ref["a"]["windows"]])
        self.assertEqual({20.0, 30.0},
                         {w["used_percent"] for w in by_ref["b"]["windows"]})


class QuotaTtlTest(QuotaBase):
    """The 60s TTL — toggling the two sections must not hammer three
    third-party endpoints, and the refresh button must always bypass it."""

    def test_second_arrival_inside_the_ttl_does_not_probe(self):
        with self.stub([acct(windows=[win()])]):
            server.get_analytics_accounts(self.con)
            out = server.get_analytics_accounts(self.con)
        self.assertEqual(1, len(self.calls))
        self.assertFalse(out["probed"])

    def test_ttl_holds_when_the_probe_wrote_no_rows(self):
        # The degraded case the DB-only clock cannot cover: nothing configured
        # (or every provider erroring) writes no captured_at at all, so a
        # captured_at-only TTL would re-probe on every single arrival.
        with self.stub([]):
            server.get_analytics_accounts(self.con)
            server.get_analytics_accounts(self.con)
        self.assertEqual(1, len(self.calls))

    def test_an_aged_capture_probes_again(self):
        with self.stub([acct(windows=[win(captured_at=iso(minutes_ago=5))])]):
            server.get_analytics_accounts(self.con)
            server._QUOTA_PROBE["at"] = None      # only the DB clock is left
            out = server.get_analytics_accounts(self.con)
        self.assertEqual(2, len(self.calls))
        self.assertTrue(out["probed"])

    def test_force_bypasses_a_fresh_ttl(self):
        with self.stub([acct(windows=[win()])]):
            server.get_analytics_accounts(self.con)
            out = server.get_analytics_accounts(self.con, force=True)
        self.assertEqual(2, len(self.calls))
        self.assertTrue(out["probed"])

    def test_the_route_owns_no_timeout_of_its_own(self):
        # Concurrency, the 5s per-probe timeout and one-provider-failure
        # containment are U2's probe_all, by a ratified ambiguity call. A
        # second timeout layer here is a finding, not defence in depth.
        seen = {}

        def fake(log, *a, **kw):
            seen["args"], seen["kwargs"] = a, kw
            return []
        with mock.patch.object(server.quota_dispatch, "probe_all", fake):
            server.get_analytics_accounts(self.con)
        self.assertEqual((), seen["args"])
        self.assertEqual({}, seen["kwargs"])


class QuotaRouteTest(unittest.TestCase):
    """The two URLs, over real HTTP. `accounts`, never `usage`."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.db = cls.tmp / "shell_db.db"
        build_db(str(cls.db)).close()
        cls._saved_db = server.DB_PATH
        server.DB_PATH = cls.db
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        server.DB_PATH = cls._saved_db

    def setUp(self):
        server._QUOTA_PROBE.update({"at": None, "providers": []})
        # This class shares one on-disk DB across its tests, and a capture left
        # by an earlier test legitimately holds the TTL closed — clear both
        # clocks so each test starts cold.
        con = sqlite3.connect(self.db)
        con.execute("DELETE FROM harness_quota_window")
        con.execute("DELETE FROM harness_quota_account")
        con.commit()
        con.close()
        self.calls = []

    def call(self, path, method="GET"):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=b"{}" if method == "POST" else None)
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())

    def stubbed(self, results):
        def fake(log, *a, **kw):
            self.calls.append(True)
            return results
        return mock.patch.object(server.quota_dispatch, "probe_all", fake)

    def test_get_accounts_serves_the_registry(self):
        with self.stubbed([acct(label="jed@gmail.com", windows=[win()])]):
            status, body = self.call("/api/analytics/accounts")
        self.assertEqual(200, status)
        self.assertEqual(["jed@gmail.com"],
                         [a["account_label"] for a in body["accounts"]])
        self.assertEqual(7, body["activity_days"])
        self.assertEqual(60, body["ttl_seconds"])

    def test_post_probe_route_forces_a_probe(self):
        with self.stubbed([acct(windows=[win()])]):
            self.call("/api/analytics/accounts")        # arms the TTL
            status, body = self.call("/api/analytics/accounts/probe", method="POST")
        self.assertEqual(200, status)
        self.assertEqual(2, len(self.calls))
        self.assertTrue(body["probed"])

    def test_token_analytics_usage_route_still_means_token_spend(self):
        # The reason the route word is `accounts`: /api/analytics/usage already
        # has a meaning on this tab and must keep it.
        status, body = self.call("/api/analytics/usage")
        self.assertEqual(200, status)
        self.assertIn("favorite_models", body)
        self.assertNotIn("accounts", body)


if __name__ == "__main__":
    unittest.main()
