#!/usr/bin/env python3
"""Tests for the Provider Quota API routes (spec doc #57, superseding #49).

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
import urllib.error
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


def acct(provider="anthropic", ref="uuid-1", captured_at=None, status="ok",
         windows=(), detail=None):
    return quota_probes.account(
        provider=provider, probe_version="1", captured_at=captured_at or iso(),
        account_ref=ref, status=status, detail=detail, windows=list(windows))


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
            server.get_analytics_quota(self.con)
            server.get_analytics_quota(self.con, force=True)
        rows = self.q("SELECT used_percent FROM harness_quota_window "
                      "WHERE window_kind='session' AND scope IS NULL")
        self.assertEqual(1, len(rows))
        self.assertEqual(44.0, rows[0]["used_percent"])

    def test_scoped_windows_stay_distinct(self):
        # COALESCE folds NULL to '' — it must not fold two REAL scopes together.
        with self.stub([acct(windows=[win("weekly_scoped", "opus", 10.0),
                                      win("weekly_scoped", "sonnet", 20.0),
                                      win("weekly", None, 30.0)])]):
            server.get_analytics_quota(self.con)
        self.assertEqual(3, self.q("SELECT COUNT(*) c FROM harness_quota_window")[0]["c"])

    def test_reprobe_reuses_the_registry_row_instead_of_minting_one(self):
        """What account_ref is FOR, and all it is for after 0097. The row
        carries no timestamps to advance any more, so the only thing the
        upsert must still get right is not duplicating."""
        with self.stub([acct(captured_at=iso(days_ago=30))],
                       [acct(captured_at=iso())]):
            server.get_analytics_quota(self.con)
            server.get_analytics_quota(self.con, force=True)
        rows = self.q("SELECT account_pk, provider, account_ref "
                      "FROM harness_quota_account")
        self.assertEqual(1, len(rows), "a re-probe minted a second row")
        self.assertEqual("uuid-1", rows[0]["account_ref"])

    def test_a_second_account_on_one_provider_does_not_disambiguate(self):
        """Two refs under one provider is legal and invisible: the panel shows
        the NEWEST READING per provider and never asks which account produced
        it (decision #68's problem dissolving, not being solved)."""
        with self.stub([acct(ref="uuid-1", captured_at=iso(days_ago=2),
                             windows=[win("session", None, 10.0,
                                          captured_at=iso(days_ago=2))])],
                       [acct(ref="uuid-2", captured_at=iso(),
                             windows=[win("session", None, 80.0)])]):
            server.get_analytics_quota(self.con)
            out = server.get_analytics_quota(self.con, force=True)
        self.assertEqual(2, self.q("SELECT COUNT(*) c FROM "
                                   "harness_quota_account")[0]["c"])
        entry = {p["provider"]: p for p in out["providers"]}["anthropic"]
        self.assertEqual([80.0], [w["used_percent"] for w in entry["windows"]],
                         "the newest reading must win outright")
        self.assertNotIn("account_ref", json.dumps(entry))

    def test_account_without_a_ref_writes_no_registry_row(self):
        # No credential file → `na`. The absence of a limit is not a limit of
        # zero, and a row keyed on a null ref could never be matched again.
        na = quota_probes.account(provider="moonshot", probe_version="1",
                                  captured_at=iso(), status="na")
        with self.stub([na]):
            out = server.get_analytics_quota(self.con)
        self.assertEqual(0, self.q("SELECT COUNT(*) c FROM harness_quota_account")[0]["c"])
        # ...but the provider is still REPORTED, or "nothing configured" would
        # be indistinguishable from "the probe failed".
        entry = {p["provider"]: p for p in out["providers"]}["moonshot"]
        self.assertEqual("na", entry["status"])
        self.assertEqual([], entry["windows"])
        self.assertIsNone(entry["captured_at"])

    def test_unauth_preserves_the_last_known_values(self):
        # "Expiry is reported, not repaired": the card shows what it last knew,
        # with its age — never a measured zero.
        good = [acct(windows=[win("session", None, 61.0, captured_at=iso(minutes_ago=90))])]
        expired = [acct(status="unauth", windows=[])]
        with self.stub(good, expired):
            server.get_analytics_quota(self.con)
            server.get_analytics_quota(self.con, force=True)
        row = self.q("SELECT used_percent, captured_at FROM harness_quota_window")[0]
        self.assertEqual(61.0, row["used_percent"])
        self.assertEqual(iso(minutes_ago=90)[:16], row["captured_at"][:16])

    def test_probe_payload_extras_never_reach_the_response(self):
        # A token must not be able to ride out of the probe seam through this
        # layer: the response is built from named keys, never a dict splat.
        leaky = acct()
        leaky["access_token"] = "sk-ant-oat01-SHOULD-NEVER-APPEAR"
        with self.stub([leaky]):
            out = server.get_analytics_quota(self.con)
        self.assertNotIn("SHOULD-NEVER-APPEAR", json.dumps(out))


class QuotaReadTest(QuotaBase):
    """The provider-shaped response — one entry per provider, newest reading.

    THE 7-DAY ACTIVITY WINDOW THIS CLASS USED TO PIN IS GONE, along with the
    is_current exemption that made it survivable. Both existed to decide WHICH
    ACCOUNT's card to show; a provider-level panel never asks.
    """

    def providers(self, out) -> dict:
        return {p["provider"]: p for p in out["providers"]}

    def test_every_provider_gets_an_entry_even_with_nothing_in_the_registry(self):
        """Built from the PROVIDERS constant, not from what happens to be in
        the DB — and that is what makes it true BY CONSTRUCTION.

        Built from the status list or the registry, a never-probed provider
        would render only once something had populated it: a card whose
        existence depends on the very thing it exists to report the absence
        of. The operator must be able to tell "not configured" from "not
        readable", and an absent card says neither."""
        with self.stub([]):
            out = server.get_analytics_quota(self.con)
        self.assertEqual([p["provider"] for p in out["providers"]],
                         list(server.quota_dispatch.PROVIDERS))
        for entry in out["providers"]:
            self.assertEqual(entry["windows"], [])
            self.assertIsNone(entry["captured_at"], "no reading yet")

    def test_the_newest_reading_wins_by_window_captured_at(self):
        """Selection keys on the WINDOW's captured_at, not on any registry
        timestamp — which is why 0097 could drop last_seen at all."""
        with self.stub([acct(ref="a", windows=[
                            win("session", None, 10.0,
                                captured_at=iso(days_ago=3))]),
                        acct(ref="b", windows=[
                            win("session", None, 90.0, captured_at=iso())])]):
            out = server.get_analytics_quota(self.con)
        entry = self.providers(out)["anthropic"]
        self.assertEqual([90.0], [w["used_percent"] for w in entry["windows"]])

    def test_a_reading_with_no_windows_cannot_outrank_one_with_numbers(self):
        """FLAG #196'S STALE ROWS, PINNED — this is why no data migration was
        needed. The rows its failure path minted under a guessed account_ref
        carry no windows, so they hold no reading, so they can never win and
        can never be seen. Their harmlessness is a property of the selection,
        not an accident, so it gets a test."""
        with self.stub([acct(ref="stale-guessed-ref", windows=[]),
                        acct(ref="real", windows=[
                            win("session", None, 42.0)])]):
            out = server.get_analytics_quota(self.con)
        entry = self.providers(out)["anthropic"]
        self.assertEqual([42.0], [w["used_percent"] for w in entry["windows"]])
        self.assertIsNotNone(entry["captured_at"])

    def test_windows_stay_with_their_own_provider(self):
        with self.stub([acct(ref="a", windows=[win("session", None, 10.0)]),
                        acct(provider="openai", ref="b", windows=[
                            win("weekly", None, 20.0), win("five_hour", None, 30.0)])]):
            out = server.get_analytics_quota(self.con)
        by_provider = self.providers(out)
        self.assertEqual([10.0], [w["used_percent"]
                                  for w in by_provider["anthropic"]["windows"]])
        self.assertEqual({20.0, 30.0}, {w["used_percent"]
                                        for w in by_provider["openai"]["windows"]})

    def test_a_window_the_provider_stopped_reporting_leaves_the_reading(self):
        """N-4. The window upsert updates and never deletes, so a kind the
        provider stops sending keeps its row for good — and the reading used to
        be the whole accumulated set, filed under the NEWEST capture's age. An
        hour-old five-hour figure then rendered under "as of 1m ago", which is
        spec #57's second empty-state wall crossed: stale figures presented as
        fresh. A reading is one capture.

        Both legs, because the naive fix (delete on write) breaks the second:
        Leg 1 — the vanished kind is not in the reading.
        Leg 2 — its ROW is still in the DB, untouched. Nothing is destroyed on
        a probe's say-so; it is simply not part of this reading, and it comes
        back the moment the provider sends that window again."""
        old = iso(minutes_ago=60)
        with self.stub([acct(windows=[win("weekly", None, 12.0, captured_at=old),
                                      win("five_hour", None, 80.0, captured_at=old)])],
                       [acct(windows=[win("weekly", None, 15.0)])]):
            server.get_analytics_quota(self.con)
            out = server.get_analytics_quota(self.con, force=True)
        entry = self.providers(out)["anthropic"]
        self.assertEqual([("weekly", 15.0)],
                         [(w["window_kind"], w["used_percent"])
                          for w in entry["windows"]],
                         "an hour-old window rode along inside a fresh reading")
        self.assertNotEqual(old[:16], entry["captured_at"][:16])
        kept = self.q("SELECT window_kind FROM harness_quota_window "
                      "WHERE window_kind='five_hour'")
        self.assertEqual(1, len(kept), "the row was deleted rather than "
                                       "left out of this reading")

    def test_an_idle_provider_is_distinguishable_from_a_never_probed_one(self):
        """L-614-2 — the distinction the card's empty state renders, asserted
        on what this layer can actually EMIT.

        captured_at is derived from window rows, so a provider with no windows
        never has one and the card cannot read staleness off it. What separates
        the two is the STATUS: `ok` with zero windows is a probe that got an
        intact answer carrying nothing (an idle account), while a provider the
        process has no status for has never produced a reading at all. The card
        branched on captured_at until this test existed, which made its idle
        sentence unreachable through any real response."""
        idle = acct(status="ok", windows=[])
        with self.stub([idle]):
            out = server.get_analytics_quota(self.con)
        by_provider = self.providers(out)
        self.assertEqual(("ok", [], None),
                         (by_provider["anthropic"]["status"],
                          by_provider["anthropic"]["windows"],
                          by_provider["anthropic"]["captured_at"]))
        # ...and the provider the probe said nothing about carries no status,
        # which is the other sentence. Same windows, same captured_at — the
        # status is the only field that can tell them apart.
        self.assertIsNone(by_provider["openai"]["status"])
        self.assertEqual([], by_provider["openai"]["windows"])
        self.assertIsNone(by_provider["openai"]["captured_at"])

    def test_a_degraded_probe_keeps_last_known_figures_and_their_age(self):
        """The empty-state rule, WITH BOTH MIRROR LEGS. A lapsed Kimi token is
        the common case, not an error, and the operator's most useful
        information in that moment is where they stood 20 minutes ago.

        Leg 1 — it must not BLANK the card: the figures survive.
        Leg 2 — it must not present stale figures AS FRESH: captured_at still
        reads the old reading's time, so the age the card renders is true."""
        old = iso(minutes_ago=90)
        with self.stub([acct(windows=[win("session", None, 61.0,
                                          captured_at=old)])],
                       [acct(status="unauth", windows=[])]):
            server.get_analytics_quota(self.con)
            out = server.get_analytics_quota(self.con, force=True)
        entry = self.providers(out)["anthropic"]
        self.assertEqual([61.0], [w["used_percent"] for w in entry["windows"]])
        self.assertEqual("unauth", entry["status"])
        self.assertEqual(old[:16], entry["captured_at"][:16],
                         "a stale reading was stamped with a fresh time")

    def test_the_response_carries_no_account_identity_at_all(self):
        """The gate item, at the API layer, WITH A POSITIVE CONTROL: the probe
        result really does carry a ref and a leaked address, asserted before
        the response is checked, so this cannot pass on an empty probe."""
        leaky = acct(ref="uuid-secret-1", windows=[win()])
        leaky["account_label"] = "operator@example.com"
        self.assertIn("operator@example.com", json.dumps(leaky))
        with self.stub([leaky]):
            out = server.get_analytics_quota(self.con)
        blob = json.dumps(out)
        for banned in ("operator@example.com", "@", "uuid-secret-1",
                       "account_label", "account_ref", "plan", "is_current"):
            self.assertNotIn(banned, blob)
        # ...and the reading itself still arrived, or the sweep proves nothing.
        self.assertEqual([22.0], [w["used_percent"] for w in
                                  self.providers(out)["anthropic"]["windows"]])


class QuotaTtlTest(QuotaBase):
    """The 60s TTL — toggling the two sections must not hammer three
    third-party endpoints, and the refresh button must always bypass it."""

    def test_second_arrival_inside_the_ttl_does_not_probe(self):
        with self.stub([acct(windows=[win()])]):
            server.get_analytics_quota(self.con)
            out = server.get_analytics_quota(self.con)
        self.assertEqual(1, len(self.calls))
        self.assertFalse(out["probed"])

    def test_ttl_holds_when_the_probe_wrote_no_rows(self):
        # The degraded case the DB-only clock cannot cover: nothing configured
        # (or every provider erroring) writes no captured_at at all, so a
        # captured_at-only TTL would re-probe on every single arrival.
        with self.stub([]):
            server.get_analytics_quota(self.con)
            server.get_analytics_quota(self.con)
        self.assertEqual(1, len(self.calls))

    def test_an_aged_attempt_probes_again(self):
        # The clock is the ATTEMPT, so age the attempt — not the capture.
        with self.stub([acct(windows=[win()])]):
            server.get_analytics_quota(self.con)
            server._QUOTA_PROBE["at"] -= server.QUOTA_TTL_SECONDS + 1
            out = server.get_analytics_quota(self.con)
        self.assertEqual(2, len(self.calls))
        self.assertTrue(out["probed"])

    def test_a_restart_inside_the_ttl_probes_and_refills_the_status_list(self):
        # The attempt clock lives in the process and dies with it. A restart
        # within 60s of a probe that DID write rows must therefore probe again:
        # _QUOTA_PROBE["providers"] died with the same process, so a clock that
        # survived in the DB would serve an EMPTY per-provider status list and
        # hand the panel the "nothing configured vs. every probe failed"
        # ambiguity that list exists to remove.
        fresh = [acct(windows=[win(captured_at=iso())])]
        with self.stub(fresh):
            server.get_analytics_quota(self.con)
            self.assertTrue(self.q("SELECT 1 FROM harness_quota_window"),
                            "positive control: the first probe must have "
                            "written a window row for the DB clock to read")
            server._QUOTA_PROBE.update({"at": None, "providers": []})  # restart
            out = server.get_analytics_quota(self.con)
        self.assertEqual(2, len(self.calls))
        self.assertTrue(out["probed"])
        # The cards render either way — they are built from PROVIDERS. What a
        # restart must recover is the STATUS on them: without the re-probe
        # every entry would carry status None, which is the panel's way of
        # saying "never probed" and would be a lie about a live account.
        entry = {p["provider"]: p for p in out["providers"]}["anthropic"]
        self.assertEqual("ok", entry["status"])

    def test_force_bypasses_a_fresh_ttl(self):
        with self.stub([acct(windows=[win()])]):
            server.get_analytics_quota(self.con)
            out = server.get_analytics_quota(self.con, force=True)
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
            server.get_analytics_quota(self.con)
        self.assertEqual((), seen["args"])
        self.assertEqual({}, seen["kwargs"])


class QuotaRouteTest(unittest.TestCase):
    """The two URLs, over real HTTP. `quota`, never `accounts`, never `usage`."""

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

    def test_get_quota_serves_the_provider_entries(self):
        with self.stubbed([acct(windows=[win()])]):
            status, body = self.call("/api/analytics/quota")
        self.assertEqual(200, status)
        self.assertEqual([p["provider"] for p in body["providers"]],
                         list(server.quota_dispatch.PROVIDERS))
        entry = {p["provider"]: p for p in body["providers"]}["anthropic"]
        self.assertEqual([22.0], [w["used_percent"] for w in entry["windows"]])
        self.assertEqual(60, body["ttl_seconds"])
        self.assertNotIn("activity_days", body,
                         "the 7-day window is gone, not merely unread")
        self.assertNotIn("accounts", body)

    def test_the_old_accounts_route_is_gone_with_no_alias(self):
        """R3: no compatibility alias. An alias would itself be a route naming
        a mechanism that no longer exists — the exact defect class this unit
        removes. There is one operator and they are being told directly."""
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.call("/api/analytics/accounts")
        self.assertEqual(404, caught.exception.code)

    def test_post_probe_route_forces_a_probe(self):
        with self.stubbed([acct(windows=[win()])]):
            self.call("/api/analytics/quota")        # arms the TTL
            status, body = self.call("/api/analytics/quota/probe", method="POST")
        self.assertEqual(200, status)
        self.assertEqual(2, len(self.calls))
        self.assertTrue(body["probed"])

    def test_token_analytics_usage_route_still_means_token_spend(self):
        # The reason the route word is `quota` and not `usage`:
        # /api/analytics/usage already has a meaning on this tab — token spend
        # — and must keep it.
        status, body = self.call("/api/analytics/usage")
        self.assertEqual(200, status)
        self.assertIn("favorite_models", body)
        self.assertNotIn("providers", body)


if __name__ == "__main__":
    unittest.main()
