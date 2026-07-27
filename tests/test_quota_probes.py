#!/usr/bin/env python3
"""Tests for the quota probe package — spec doc #57, superseding #49.

THE FIXTURES ARE NOW SANITIZED REAL CAPTURES, and that is the single most
load-bearing fact about this suite. The originals were TRANSCRIBED from spec
49's observed-field tables, which were wrong about moonshot in three places —
so the normalizer was pinned against the planner's reading of the payload and
every test agreed with it. Six field-level defects lived behind that agreement,
in all three providers, every one of them invisible because the fixture said
what the probe said.

Two consequences run through the tests below. Where a live capture's own value
is too weak to pin an arithmetic (moonshot's five-hour window reads 100/100, so
`used` derives to zero — and zero is also what a broken derivation returns), a
second case is DERIVED from the capture with the structure held exactly and
only the numbers moved. And where the wire does not currently send a shape the
code must handle (a five-hour SCOPED openai window), it is pinned anyway: a
test that only covers what the wire sends today is how this feature got here.

Every test drives the probes against the recorded fixtures in
tests/fixtures/quota_probes/ (see that dir's README for provenance). NO TEST
PERFORMS A LIVE CALL, and that is enforced rather than promised: `urlopen` is
replaced for the duration of every test with a stub that fails the test if it
is ever reached.

The five Probe Contract invariants each get a test that a realistic bug turns
red:
  • read-only on credentials — the files' bytes AND mtimes are compared
    across a full probe_all, so an accidental rewrite-with-same-content is
    caught too;
  • tokens never stored, logged, or returned — a sentinel token must appear in
    the Authorization header (proving the probe actually authenticates) and
    NOWHERE in the returned rows or the log;
  • expiry reported, not repaired — an expired token yields `unauth` AND
    makes no request at all;
  • absent credential file yields `na` and never a 0% anywhere in the result;
  • one provider's failure is contained — a raiser and a hang each degrade to
    their own `error` row while the other providers' rows stand.

Plus normalization: window kind is derived from the OBSERVED DURATION, pinned
by swapping two windows' durations and asserting their kinds swap — a probe
that read position or label instead would not move.

Run:
    python3 tests/test_quota_probes.py
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
FIXTURES = ROOT / "tests" / "fixtures" / "quota_probes"

sys.path.insert(0, str(ENGINE / "scripts"))

import harness_versions  # noqa: E402
import quota_probes as qp  # noqa: E402
from quota_probes import anthropic as p_anthropic  # noqa: E402
from quota_probes import dispatch  # noqa: E402
from quota_probes import moonshot as p_moonshot  # noqa: E402
from quota_probes import openai as p_openai  # noqa: E402

TOKEN = "sentinel-access-token-must-never-leak"
HOUR_MS = 3600 * 1000

# The real version probe, captured before any stub replaces the module
# attribute — the seam test below runs it for real against a scrubbed PATH.
REAL_VERSION_PROBE = harness_versions.probe
# What a codex-equipped host answers. Stubbed for every test (see ProbeCase),
# because `harness_versions.probe` shells out to the installed CLI and the
# openai probe is the one caller that depends on it.
STUB_CODEX_VERSION = "codex-cli 0.145.0"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class Endpoint:
    """Stand-in for quota_probes.get_json — records what it was called with."""

    def __init__(self, code=200, payload=None, delay=0.0):
        self.code, self.payload, self.delay = code, payload, delay
        self.calls: list[tuple] = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers), timeout))
        if self.delay:
            time.sleep(self.delay)
        return self.code, self.payload


class ProbeCase(unittest.TestCase):
    """Writes credential files into a temp HOME and points each probe at them.

    Every probe module resolves its credential paths at import time, so the
    constants are patched rather than $HOME — the same thing the real code
    would read, without touching the operator's own files.

    HTTP IS NOT THE ONLY EXTERNAL DEPENDENCY, and stubbing only that one is
    what put 20 failures in CI (SC-170). The openai probe derives its client
    version from the INSTALLED codex CLI, so on a runner without one it
    short-circuits to a named error before the get_json seam is ever reached
    and every openai leg reds — the suite's result depending on the host's
    toolchain, which is precisely what the fixture work exists to end. Both
    seams are stubbed here; `test_the_version_seam_is_the_installed_cli` runs
    the real one against a scrubbed PATH so the coupling itself stays pinned.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.notes: list[str] = []
        self.log = self.notes.append

        self.claude_creds = self.tmp / ".claude/.credentials.json"
        self.claude_profile = self.tmp / ".claude.json"
        self.codex_auth = self.tmp / ".codex/auth.json"
        self.kimi_creds = self.tmp / ".kimi-code/credentials/kimi-code.json"
        expires = int((time.time() + 3600) * 1000)
        self.write(self.claude_creds, {"claudeAiOauth": {
            "accessToken": TOKEN, "refreshToken": "sentinel-refresh",
            "expiresAt": expires, "subscriptionType": "max"}})
        self.write(self.claude_profile, {"oauthAccount": {
            "accountUuid": "abcd1234-0000-4000-8000-00000000beef",
            "emailAddress": "placeholder@example.com"}})
        self.write(self.codex_auth, {"auth_mode": "chatgpt", "tokens": {
            "access_token": TOKEN, "refresh_token": "sentinel-refresh",
            "account_id": "acct_placeholder_0001"}})
        self.write(self.kimi_creds, {
            "access_token": TOKEN, "refresh_token": "sentinel-refresh",
            "expires_at": expires})

        self.patch(p_anthropic, CREDENTIALS=self.claude_creds,
                   PROFILE=self.claude_profile)
        self.patch(p_openai, CREDENTIALS=self.codex_auth)
        self.patch(p_moonshot, CREDENTIALS=self.kimi_creds)
        self.patch(harness_versions, probe=lambda _: STUB_CODEX_VERSION)

        # The no-live-call guard. Reached only if a probe bypasses get_json.
        guard = mock.patch("urllib.request.urlopen",
                           side_effect=AssertionError("live HTTP call attempted"))
        guard.start()
        self.addCleanup(guard.stop)

    def write(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))

    def patch(self, module, **attrs) -> None:
        for name, value in attrs.items():
            p = mock.patch.object(module, name, value)
            p.start()
            self.addCleanup(p.stop)

    def endpoint(self, module, code=200, payload=None, delay=0.0) -> Endpoint:
        ep = Endpoint(code, payload, delay)
        self.patch(module, get_json=ep)
        return ep

    def one(self, rows: list) -> dict:
        self.assertEqual(len(rows), 1, f"expected one account, got {rows}")
        return rows[0]

    def by_kind(self, acct: dict) -> dict:
        return {(w["window_kind"], w["scope"]): w for w in acct["windows"]}


class NormalizerTest(unittest.TestCase):
    def test_kind_comes_from_the_duration(self):
        self.assertEqual(qp.kind_for_seconds(18000), "five_hour")
        self.assertEqual(qp.kind_for_seconds(604800), "weekly")
        self.assertEqual(qp.kind_for_seconds(900), "short")
        self.assertEqual(qp.kind_for_seconds("604800"), "weekly")

    def test_unmapped_duration_is_not_guessed(self):
        self.assertIsNone(qp.kind_for_seconds(7200))
        self.assertIsNone(qp.kind_for_seconds(None))
        self.assertIsNone(qp.kind_for_seconds("weekly"))

    def test_percent_is_derived_only_from_a_positive_limit(self):
        self.assertEqual(qp.percent_from(1, 4), 25.0)
        self.assertIsNone(qp.percent_from(1, 0), "limit=0 must not divide")
        self.assertIsNone(qp.percent_from(1, None))
        self.assertIsNone(qp.percent_from(None, 100))

    def test_epoch_and_iso_normalize_to_utc_seconds(self):
        self.assertEqual(qp.iso_from_epoch(1785016800), "2026-07-25T22:00:00Z")
        self.assertEqual(qp.iso_from_epoch(1785016800000), "2026-07-25T22:00:00Z")
        self.assertEqual(qp.norm_iso("2026-07-25T21:00:00+00:00"),
                         "2026-07-25T21:00:00Z")
        self.assertIsNone(qp.norm_iso("not a time"))


class AnthropicProbeTest(ProbeCase):
    def probe(self, code=200, payload=None) -> tuple:
        ep = self.endpoint(p_anthropic, code,
                           fixture("anthropic_usage.json") if payload is None
                           else payload)
        return self.one(p_anthropic.probe(self.log, 5)), ep

    def test_normalizes_the_three_observed_windows(self):
        acct, ep = self.probe()
        self.assertEqual(acct["status"], "ok")
        self.assertEqual(acct["account_ref"],
                         "abcd1234-0000-4000-8000-00000000beef")
        windows = self.by_kind(acct)
        self.assertEqual(set(windows), {("session", None), ("weekly", None),
                                        ("weekly_scoped", "Fable")})
        self.assertEqual(windows[("session", None)]["used_percent"], 47.0)
        self.assertEqual(windows[("session", None)]["resets_at"],
                         "2026-07-25T23:10:00Z")
        self.assertEqual(windows[("weekly", None)]["used_percent"], 36.0)
        self.assertEqual(windows[("weekly_scoped", "Fable")]["used_percent"],
                         47.0)
        self.assertEqual(ep.calls[0][0], p_anthropic.URL)
        self.assertEqual(ep.calls[0][1]["anthropic-beta"],
                         p_anthropic.BETA_HEADER)

    def test_the_operator_address_on_disk_never_reaches_the_result(self):
        """THE POSITIVE CONTROL IS THE POINT OF THIS TEST.

        ~/.claude.json really does carry `emailAddress`, and setUp really does
        write one — asserted present here so the absence assertion below cannot
        pass merely because nothing was there to find. Decision #69 once made
        that address the card's label; decision #75 stopped collecting it.

        The uuid from the SAME object must still be read, or this test would
        also pass against a probe that had stopped reading the file at all."""
        on_disk = json.loads(self.claude_profile.read_text())
        self.assertEqual(on_disk["oauthAccount"]["emailAddress"],
                         "placeholder@example.com")
        acct, _ = self.probe()
        self.assertEqual(acct["account_ref"],
                         "abcd1234-0000-4000-8000-00000000beef")
        self.assertNotIn("placeholder@example.com", json.dumps(acct))
        self.assertNotIn("@", json.dumps(acct))
        self.assertNotIn("account_label", acct)
        self.assertEqual([n for n in self.notes if "@" in n], [])

    def test_no_plan_is_collected_from_either_source(self):
        """`plan` is dropped (migration 0097), and BOTH candidate sources are
        pinned because the probe read the wrong one for its whole life.

        `subscriptionType` is a real key of the CREDENTIAL FILE — setUp writes
        `max` — and was never a key of the payload, which is why reading it
        from the payload returned None silently and no test noticed. Neither
        may reach the result now."""
        self.assertEqual(json.loads(self.claude_creds.read_text())
                         ["claudeAiOauth"]["subscriptionType"], "max")
        acct, _ = self.probe()
        self.assertNotIn("plan", acct)
        self.assertNotIn("max", json.dumps(acct))

    def test_percent_only_provider_leaves_counts_null(self):
        acct, _ = self.probe()
        for w in acct["windows"]:
            self.assertIsNone(w["used"], "a count was invented from a percent")
            self.assertIsNone(w["limit_value"])

    def test_expired_token_is_reported_never_used(self):
        self.write(self.claude_creds, {"claudeAiOauth": {
            "accessToken": TOKEN, "expiresAt": int((time.time() - 60) * 1000)}})
        acct, ep = self.probe()
        self.assertEqual(acct["status"], "unauth")
        self.assertEqual(ep.calls, [], "an expired token was sent to the provider")
        self.assertEqual(acct["windows"], [])
        # The account is still identified, so the card keeps its last values.
        self.assertEqual(acct["account_ref"],
                         "abcd1234-0000-4000-8000-00000000beef")

    def test_absent_credential_file_is_na_not_zero(self):
        self.claude_creds.unlink()
        acct, ep = self.probe()
        self.assertEqual(acct["status"], "na")
        self.assertIsNone(acct["account_ref"], "an na account must key nothing")
        self.assertEqual(acct["windows"], [])
        self.assertEqual(ep.calls, [])
        self.assertNotIn("0", json.dumps([w.get("used_percent")
                                          for w in acct["windows"]]))

    def test_provider_rejection_is_unauth_other_failures_are_error(self):
        acct, _ = self.probe(code=401)
        self.assertEqual(acct["status"], "unauth")
        acct, _ = self.probe(code=503)
        self.assertEqual(acct["status"], "error")
        self.assertIn("503", acct["detail"])
        self.assertEqual(acct["windows"], [])

    def test_shape_drift_writes_no_partial_rows(self):
        acct, _ = self.probe(payload={"subscriptionType": "max"})
        self.assertEqual(acct["status"], "error")
        self.assertEqual(acct["windows"], [])
        self.assertTrue(any("shape drift" in n for n in self.notes),
                        f"drift must be loud; log was {self.notes}")

    def test_an_empty_limits_list_is_data_not_drift(self):
        """The other half of the rule above: `limits: []` is a well-formed
        envelope reporting no window — a real answer, kept `ok`."""
        acct, _ = self.probe(payload={"subscriptionType": "max", "limits": []})
        self.assertEqual(acct["status"], "ok")
        self.assertEqual(acct["windows"], [])
        self.assertEqual([n for n in self.notes if "drift" in n], [],
                         "an idle account must not be reported as drift")

    def test_an_unreadable_limits_entry_is_drift_not_a_silent_skip(self):
        """L-614-1, this provider's instance. Every window on a Claude card
        comes out of this list, so an entry the reader cannot open is a window
        vanishing — and the reader's answer to it was `continue`, under status
        `ok`, with the account's OTHER windows still rendering as though the
        card were whole. That is the same failure as the wrong-typed container
        one level up, which this suite already calls drift."""
        payload = fixture("anthropic_usage.json")
        payload["limits"].append("five_hour")
        acct, _ = self.probe(payload=payload)
        self.assertEqual(acct["status"], "error")
        self.assertEqual(acct["windows"], [],
                         "no partial rows — the readable limits must not ship "
                         "as if they were the whole reading")
        self.assertTrue(any("shape drift" in n for n in self.notes),
                        f"drift must be loud; log was {self.notes}")

    def test_unknown_limit_kind_is_kept_not_dropped(self):
        acct, _ = self.probe(payload={"limits": [
            {"kind": "monthly_all", "percent": 5, "resets_at": None}]})
        w = self.one(acct["windows"])
        self.assertEqual(w["window_kind"], "unknown")
        self.assertEqual(w["scope"], "monthly_all")
        self.assertEqual(w["used_percent"], 5.0)


class OpenAIProbeTest(ProbeCase):
    def probe(self, code=200, payload=None) -> tuple:
        ep = self.endpoint(p_openai, code,
                           fixture("openai_usage.json") if payload is None
                           else payload)
        return self.one(p_openai.probe(self.log, 5)), ep

    def test_normalizes_the_observed_windows(self):
        """Both windows the live capture carries — and the scoped one is the
        whole of new defect A: it lives inside `additional_rate_limits[0]
        .rate_limit`, one level below where the shipped probe looked."""
        acct, ep = self.probe()
        self.assertEqual(acct["status"], "ok")
        windows = self.by_kind(acct)
        self.assertEqual(set(windows),
                         {("weekly", None), ("weekly", "GPT-5.3-Codex-Spark")})
        self.assertEqual(windows[("weekly", None)]["used_percent"], 2.0)
        self.assertEqual(windows[("weekly", None)]["resets_at"],
                         "2026-08-01T22:18:28Z")
        scoped = windows[("weekly", "GPT-5.3-Codex-Spark")]
        self.assertEqual(scoped["used_percent"], 0.0)
        self.assertEqual(scoped["resets_at"], "2026-08-01T22:58:31Z")
        self.assertEqual(ep.calls[0][1]["chatgpt-account-id"],
                         "acct_placeholder_0001")

    def test_the_scoped_window_carries_real_numbers_not_an_empty_row(self):
        """THE REGRESSION PIN FOR DEFECT A, and the reason it needs its own
        test: the shipped probe read `used_percent` / `limit_window_seconds` /
        `reset_at` at ENTRY level, where the live payload carries none of them.
        It produced a row of NULLs, logged NO drift, and returned status `ok`.

        `assertIsNotNone` rather than a value match is deliberate — this pins
        the failure MODE (an empty row shipping as a healthy reading), so it
        stays meaningful when the capture's numbers are refreshed."""
        acct, _ = self.probe()
        scoped = self.by_kind(acct)[("weekly", "GPT-5.3-Codex-Spark")]
        self.assertIsNotNone(scoped["used_percent"],
                             "the scoped row shipped with no percent while "
                             "status stayed ok — defect A, exactly")
        self.assertIsNotNone(scoped["resets_at"])
        self.assertEqual(acct["status"], "ok")
        self.assertEqual([n for n in self.notes if "drift" in n], [])

    def test_a_scoped_five_hour_window_is_not_labelled_weekly(self):
        """The wire does not currently send this, and that is the point.

        The scoped row's kind used to be right BY ACCIDENT: the duration was
        unreadable at entry level, so `kind_for_seconds` returned None and a
        `fallback_kind="weekly"` caught it — while the live window happened to
        be 604800. Any other duration would have been labelled weekly just as
        confidently. A test that only covers what the wire sends today is how
        this feature got here."""
        payload = fixture("openai_usage.json")
        entry = payload["additional_rate_limits"][0]
        entry["rate_limit"]["primary_window"]["limit_window_seconds"] = 18000
        acct, _ = self.probe(payload=payload)
        scoped = self.by_kind(acct)[("five_hour", "GPT-5.3-Codex-Spark")]
        self.assertEqual(scoped["window_kind"], "five_hour")
        self.assertNotEqual(scoped["window_kind"], "weekly")

    def test_an_entry_carrying_no_rate_limit_is_drift(self):
        """One level below the envelope is where defect A lived, so that is
        where the check has to reach. An entry with no `rate_limit{}` is a
        payload that has moved, not an idle account — and the shipped probe's
        answer to it was a silent empty row under status `ok`."""
        payload = fixture("openai_usage.json")
        del payload["additional_rate_limits"][0]["rate_limit"]
        acct, _ = self.probe(payload=payload)
        self.assertEqual(acct["status"], "error")
        self.assertEqual(acct["windows"], [],
                         "no partial rows — the intact top-level window must "
                         "not ship as if it were the whole reading")
        self.assertTrue(any("shape drift" in n for n in self.notes),
                        f"drift must be loud; log was {self.notes}")

    def test_an_entry_that_is_not_an_object_is_drift(self):
        """L-614-1 — the same finding as the test above, one TYPE up, and it
        shipped inside the fix for the container-level twin.

        `additional_rate_limits: 5` is drift because a list is where the reader
        iterates; `additional_rate_limits: [5]` was a silent `continue` under
        status `ok`, which is the identical scoped-window-vanishes failure with
        the identical "nothing looks wrong" card. The mirror leg is the test
        below: `[]` and an absent key stay `ok`, so this cannot be satisfied by
        erroring on every empty collection."""
        payload = fixture("openai_usage.json")
        payload["additional_rate_limits"] = ["gpt-5.3-codex-spark"]
        acct, _ = self.probe(payload=payload)
        self.assertEqual(acct["status"], "error")
        self.assertEqual(acct["windows"], [],
                         "no partial rows — the intact top-level window must "
                         "not ship as if it were the whole reading")
        self.assertTrue(any("shape drift" in n for n in self.notes),
                        f"drift must be loud; log was {self.notes}")

    def test_kind_follows_the_duration_not_the_window_name(self):
        """Give the two windows different durations: their kinds follow the
        DURATION, not the container they arrived in.

        A probe that mapped top-level→weekly / scoped→weekly by position or by
        a fallback passes the test above and fails only here. The live capture
        has both at 604800, which is exactly why it cannot pin this itself."""
        payload = fixture("openai_usage.json")
        payload["rate_limit"]["primary_window"]["limit_window_seconds"] = 18000
        acct, _ = self.probe(payload=payload)
        kinds = {w["scope"]: w["window_kind"] for w in acct["windows"]}
        self.assertEqual(kinds[None], "five_hour")
        self.assertEqual(kinds["GPT-5.3-Codex-Spark"], "weekly")

    def test_unrecognized_duration_keeps_the_window_under_its_own_row(self):
        payload = {"account_id": "a1", "rate_limit": {"primary_window": {
            "used_percent": 9, "limit_window_seconds": 7200, "reset_at": 0}}}
        acct, _ = self.probe(payload=payload)
        w = self.one(acct["windows"])
        self.assertEqual(w["window_kind"], "unknown")
        self.assertEqual(w["scope"], "7200s", "the duration must survive")
        self.assertEqual(w["used_percent"], 9.0)

    def test_lost_envelope_is_drift_not_a_measured_zero(self):
        """A 200 that no longer carries `rate_limit` at all. Parsing it as
        zero windows would render a healthy card with nothing on it —
        indistinguishable from an account that genuinely has no window."""
        acct, _ = self.probe(payload={"account_id": "acct_placeholder_0001",
                                      "email": "placeholder@example.com",
                                      "plan_type": "prolite"})
        self.assertEqual(acct["status"], "error")
        self.assertEqual(acct["windows"], [])
        self.assertEqual(acct["account_ref"], "acct_placeholder_0001",
                         "a drifted card still keys to its account")
        self.assertTrue(any("shape drift" in n for n in self.notes),
                        f"drift must be loud; log was {self.notes}")

    def test_an_empty_envelope_is_data_not_drift(self):
        """`rate_limit: {}` — the structure is intact and reports no window.
        Calling THAT drift would cry wolf at an idle account and train the
        operator to ignore the error state, which is the worse failure."""
        acct, _ = self.probe(payload={"account_id": "acct_placeholder_0001",
                                      "rate_limit": {}})
        self.assertEqual(acct["status"], "ok")
        self.assertEqual(acct["windows"], [])
        self.assertEqual([n for n in self.notes if "drift" in n], [])

    def test_an_additional_rate_limits_that_is_not_a_list_is_drift(self):
        """The same rule moonshot's `limits` obeys: no legitimate-empty
        reading exists for the wrong TYPE under a key the normalizer
        iterates. Skipping it silently reports `ok` while every scoped
        window vanishes — the failure shape SC-167 was blocked for.

        Both an iterable wrong type and a non-iterable one: the drift check
        is the only thing standing between them and the loop in `_windows`,
        and only the non-iterable case can tell a working check from a probe
        that survives on the loop crashing instead."""
        for value in ({"premium": {"used_percent": 3.0}}, 5):
            with self.subTest(additional_rate_limits=value):
                self.notes.clear()
                payload = fixture("openai_usage.json")
                payload["rate_limit"]["additional_rate_limits"] = value
                acct, _ = self.probe(payload=payload)
                self.assertEqual(acct["status"], "error")
                self.assertEqual(acct["windows"], [],
                                 "no partial rows — the two intact windows "
                                 "must not ship as if they were the whole "
                                 "reading")
                self.assertTrue(any("shape drift" in n for n in self.notes),
                                f"drift must be loud; log was {self.notes}")

    def test_an_absent_or_empty_additional_rate_limits_is_data_not_drift(self):
        """The other direction, which the guard must not over-fire on: a plan
        with no scoped window omits the key or sends `[]`, and both readings
        are real. The main fixture's two windows still stand."""
        for value in (None, []):
            with self.subTest(additional_rate_limits=value):
                self.notes.clear()
                payload = fixture("openai_usage.json")
                if value is None:
                    del payload["additional_rate_limits"]
                else:
                    payload["additional_rate_limits"] = value
                acct, _ = self.probe(payload=payload)
                self.assertEqual(acct["status"], "ok")
                self.assertEqual(self.by_kind(acct).keys(),
                                 {("weekly", None)},
                                 "the top-level window still stands alone")
                self.assertEqual([n for n in self.notes if "drift" in n], [])

    def test_a_200_that_is_not_a_json_object_says_so(self):
        acct, _ = self.probe(payload=["not", "an", "object"])
        self.assertEqual(acct["status"], "error")
        self.assertIn("not a JSON object", acct["detail"],
                      "'HTTP 200' as a failure detail tells the operator nothing")
        self.assertTrue(any("shape drift" in n for n in self.notes))

    def test_absent_credential_file_is_na_not_zero(self):
        self.codex_auth.unlink()
        acct, ep = self.probe()
        self.assertEqual(acct["status"], "na")
        self.assertIsNone(acct["account_ref"])
        self.assertEqual(ep.calls, [])

    def test_a_failed_probe_claims_no_account(self):
        """INVERTED FROM SPRINT 52, and flag #196 is why.

        This used to assert the opposite — that the file's account_id keeps an
        unauth card keyed. But the file's id is a GUESS at who the endpoint
        will name, and a WRONG one: the file says `acct_placeholder_0001`
        while a 200 says `user-PLACEHOLDER…`. Writing the guess on every
        failure is what minted a permanent second registry row under one
        account. A failed probe learned nothing about identity, so it claims
        none — which is what stops the duplicate at its source rather than
        reconciling it afterwards.

        The last successful probe's rows are untouched by this and still
        render with their own age; that is `_latest_readings`' job, pinned in
        the API suite."""
        for code in (401, 403, 503):
            with self.subTest(code=code):
                acct, _ = self.probe(code=code)
                self.assertIsNone(acct["account_ref"])
                self.assertEqual(acct["windows"], [])

    def test_a_403_is_a_loud_client_rejection_never_unauth(self):
        """Flag #196's half that does not dissolve, with its MIRROR LEGS.

        A 403 from this endpoint is an anti-bot rejection of the CLIENT, not a
        verdict on the operator's session — it arrived with an HTML body while
        the token was perfectly valid. Mapping it to `unauth` blamed the
        operator for being signed out when they were not, and a silently empty
        card would hide the breakage entirely."""
        acct, _ = self.probe(code=403)
        self.assertEqual(acct["status"], "error")
        self.assertNotEqual(acct["status"], "unauth",
                            "a 403 here is a client rejection, and telling "
                            "the operator they are signed out is a claim the "
                            "probe has not established")
        self.assertTrue(acct["detail"],
                        "an empty detail is the silent-empty-card failure — "
                        "the operator must see WHY the probe broke")
        self.assertIn("403", acct["detail"])

    def test_the_client_version_comes_from_the_installed_cli(self):
        """The fork installs harnesses at --latest on every update, so a baked
        constant goes stale by design. Pinned by moving the installed version
        and watching every header follow it."""
        with mock.patch.object(p_openai.harness_versions, "probe",
                               lambda _: "codex-cli 9.9.9"):
            _, ep = self.probe()
        headers = ep.calls[0][1]
        self.assertEqual(headers["version"], "9.9.9")
        self.assertEqual(headers["originator"], "codex_cli_rs")
        self.assertEqual(headers["User-Agent"], "codex_cli_rs/9.9.9")

    def test_no_codex_cli_is_a_named_error_not_a_bare_request(self):
        """Sending no client identity provokes the very 403 this fix exists to
        avoid, and a 403 we caused ourselves is indistinguishable from the
        endpoint tightening. Say which one it is instead."""
        with mock.patch.object(p_openai.harness_versions, "probe",
                               lambda _: None):
            acct, ep = self.probe()
        self.assertEqual(acct["status"], "error")
        self.assertEqual(ep.calls, [], "a request went out with no client identity")
        self.assertIn("client", acct["detail"])

    def test_the_version_seam_is_a_real_subprocess_against_the_hosts_path(self):
        """SC-170's pin: THE SEAM ITSELF, run for real, both directions.

        Every other test here stubs `harness_versions.probe` — correct
        isolation, and also where a coupling hides. The seam is a subprocess
        against the host's PATH, and this suite stubbed only the HTTP one until
        CI found the second: 20 red legs on every runner without a codex CLI,
        because the probe short-circuits before `get_json` is reached. So the
        real seam gets a leg, with PATH pointed at a directory this test owns:

        Leg 1 — a `codex` on PATH: the version reaches every header, through
        shutil.which and a real `--version` call, with nothing stubbed.
        Leg 2 — the same PATH with no codex in it: the loud named error, and no
        request. Without leg 1 this passes for a probe that can never find a
        CLI at all, which is the failure it is here to detect."""
        bindir = self.tmp / "bin"
        bindir.mkdir()
        fake = bindir / "codex"
        fake.write_text("#!/bin/sh\necho 'codex-cli 1.2.3'\n")
        fake.chmod(0o755)

        with mock.patch.object(harness_versions, "probe", REAL_VERSION_PROBE):
            with mock.patch.dict(os.environ, {"PATH": str(bindir)}):
                acct, ep = self.probe()
            self.assertEqual(acct["status"], "ok")
            self.assertEqual(ep.calls[0][1]["version"], "1.2.3")
            self.assertEqual(ep.calls[0][1]["User-Agent"], "codex_cli_rs/1.2.3")

            fake.unlink()
            with mock.patch.dict(os.environ, {"PATH": str(bindir)}):
                acct, ep = self.probe()
        self.assertEqual(acct["status"], "error")
        self.assertIn("codex", acct["detail"])
        self.assertEqual(ep.calls, [],
                         "a request went out with no client identity")


class MoonshotProbeTest(ProbeCase):
    def probe(self, code=200, payload=None) -> tuple:
        ep = self.endpoint(p_moonshot, code,
                           fixture("moonshot_usages.json") if payload is None
                           else payload)
        return self.one(p_moonshot.probe(self.log, 5)), ep

    def test_derives_percent_from_absolute_counts(self):
        """The account-wide `usage` block, which pins its own arithmetic: the
        capture reads 97/100 and the counts arrive as STRINGS."""
        acct, _ = self.probe()
        self.assertEqual(acct["status"], "ok")
        self.assertEqual(acct["account_ref"], "placeholderuser000001")
        weekly = self.by_kind(acct)[("weekly", None)]
        self.assertEqual((weekly["used"], weekly["limit_value"]), (97, 100))
        self.assertEqual(weekly["used_percent"], 97.0)
        self.assertEqual(weekly["resets_at"], "2026-07-29T11:38:20Z")

    def test_limits_entry_unwraps_detail_and_derives_used(self):
        """Defect 3: entries nest their counts under `detail` and carry NO
        `used` key at all, so `used` is derived as limit - remaining.

        THIS CASE IS DERIVED FROM THE CAPTURE, NOT RECORDED. The live window
        reads limit=100 remaining=100, so the real derivation lands on ZERO —
        and zero is also what every BROKEN derivation returns (missing key →
        None → 0, wrong nesting → nothing found → 0). A test built only on the
        capture would pass against a derivation that never works.

        So the structure is the capture's exactly and only the numbers move.
        The zero case is pinned too, in the test below, because it is what the
        wire actually sends."""
        payload = fixture("moonshot_usages.json")
        detail = payload["limits"][0]["detail"]
        self.assertEqual(set(payload["limits"][0]), {"window", "detail"},
                         "the capture's entry shape moved — re-derive this case")
        self.assertNotIn("used", detail, "defect 3 was that no `used` exists")
        detail["limit"], detail["remaining"] = "400", "150"
        acct, _ = self.probe(payload=payload)
        five_hour = self.by_kind(acct)[("five_hour", None)]
        self.assertEqual((five_hour["used"], five_hour["limit_value"]),
                         (250, 400), "used must be limit - remaining")
        self.assertEqual(five_hour["used_percent"], 62.5)
        self.assertEqual(five_hour["resets_at"], "2026-07-26T00:38:20Z",
                         "resetTime is read from `detail`, not entry level")

    def test_an_untouched_five_hour_window_reads_zero_not_nothing(self):
        """The capture's own numbers, kept as their own case: an unused window
        is a MEASURED zero with its limit intact. The pair is what separates it
        from a failed read — a broken derivation returns used=0 too, but it
        cannot also produce limit_value=100 and a reset time."""
        acct, _ = self.probe()
        five_hour = self.by_kind(acct)[("five_hour", None)]
        self.assertEqual((five_hour["used"], five_hour["limit_value"]), (0, 100))
        self.assertEqual(five_hour["used_percent"], 0.0)
        self.assertEqual(five_hour["resets_at"], "2026-07-26T00:38:20Z")

    def test_the_time_unit_prefix_is_stripped_not_enumerated(self):
        """Defect 1, both spellings. The wire sends `TIME_UNIT_MINUTE`; the
        shipped map keyed on a bare `MINUTE`, so 300 minutes — a five-hour
        window, a kind the vocabulary already had — filed as unrecognized.

        Both must work: the prefix is STRIPPED rather than the two spellings
        enumerated, so a `TIME_UNIT_HOUR` the wire has not sent yet resolves
        too."""
        for unit in ("TIME_UNIT_MINUTE", "MINUTE"):
            with self.subTest(timeUnit=unit):
                payload = fixture("moonshot_usages.json")
                payload["limits"][0]["window"]["timeUnit"] = unit
                acct, _ = self.probe(payload=payload)
                self.assertIn(("five_hour", None), self.by_kind(acct),
                              f"300 {unit} is five hours")

    def test_300_minutes_is_five_hours(self):
        """The kind comes from duration × timeUnit, so changing the unit
        alone must change the kind."""
        payload = fixture("moonshot_usages.json")
        payload["limits"][0]["window"]["timeUnit"] = "SECOND"
        acct, _ = self.probe(payload=payload)
        kinds = {w["window_kind"] for w in acct["windows"]}
        self.assertIn("short", kinds, "300 SECONDS is not five hours")
        self.assertNotIn("five_hour", kinds)

    def test_zero_limit_reads_na_never_divides(self):
        payload = fixture("moonshot_usages.json")
        payload["usage"]["limit"] = 0
        payload["limits"] = []
        acct, _ = self.probe(payload=payload)
        w = self.one(acct["windows"])
        self.assertIsNone(w["used_percent"])
        self.assertEqual((w["used"], w["limit_value"]), (97, 0),
                         "the raw counts still stand")

    def test_unknown_time_unit_keeps_the_window(self):
        """A unit the vocabulary cannot convert still yields a ROW — the limit
        is real and dropping it would hide a real constraint.

        The raw window goes to the LOG, which is a diagnostic the operator
        never reads, and NOT to `scope`, which renders on the card and is
        persisted. That split is the whole lesson of defect 2: the place to
        put an untrusted value is the log."""
        payload = fixture("moonshot_usages.json")
        payload["limits"][0]["window"] = {"duration": 2, "timeUnit": "FORTNIGHT"}
        acct, _ = self.probe(payload=payload)
        odd = [w for w in acct["windows"] if w["window_kind"] == "unknown"]
        self.assertEqual(len(odd), 1, "an unconvertible window was dropped")
        self.assertEqual(odd[0]["scope"], "unrecognized window")
        self.assertNotIn("FORTNIGHT", str(odd[0]["scope"]))
        self.assertTrue(any("FORTNIGHT" in n for n in self.notes),
                        f"the raw window must be diagnosable; log was {self.notes}")

    def test_no_container_repr_ever_reaches_operator_visible_text(self):
        """DEFECT 2, PINNED AS A CLASS RATHER THAN AS THE TYPO IT LOOKED LIKE.

        The shipped probe interpolated the whole window DICT into `scope`, so
        `{'duration': 300, 'timeUnit': 'TIME_UNIT_MINUTE'}` rendered on the
        card AND was persisted to the DB. The rule is general — a container is
        never interpolated into text the operator reads — so this asserts the
        SHAPE of the output rather than one expected string, and sweeps every
        field a card renders.

        The unrecognized-window path is the one that reads an uncontrolled
        value straight from the wire, so each case below is a container
        arriving where a scalar was assumed. OpenAI carried the identical
        latent bug on its own duration field, which is why `duration_label` is
        shared rather than copied — a class needs one implementation."""
        for bad in ({"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                    [300, "MINUTE"],
                    {"nested": {"duration": 7}}):
            with self.subTest(window=bad):
                payload = fixture("moonshot_usages.json")
                payload["limits"][0]["window"] = bad
                acct, _ = self.probe(payload=payload)
                for w in acct["windows"]:
                    for field in ("scope", "window_kind"):
                        text = w[field]
                        if text is None:
                            continue
                        self.assertNotRegex(
                            str(text), r"[{}\[\]]|'\w+':",
                            f"a container repr reached {field} — the exact "
                            "defect that put a Python dict on the card and "
                            "into the database")

    def test_lost_usage_block_is_drift_not_a_measured_zero(self):
        """`usage` is the account-wide block every payload carries — an
        account with nothing spent reports `used: 0`, it does not drop the
        key. Its absence is the shape having changed, not an empty account."""
        payload = fixture("moonshot_usages.json")
        del payload["usage"]
        acct, _ = self.probe(payload=payload)
        self.assertEqual(acct["status"], "error")
        self.assertEqual(acct["windows"], [])
        self.assertEqual(acct["account_ref"], "placeholderuser000001",
                         "a drifted card still keys to its account")
        self.assertTrue(any("shape drift" in n for n in self.notes),
                        f"drift must be loud; log was {self.notes}")

    def test_an_absent_limits_list_is_data_not_drift(self):
        """The asymmetry, pinned: `limits` is a collection, so absent is a
        plan with no metered sub-window and the weekly figure still stands."""
        payload = fixture("moonshot_usages.json")
        del payload["limits"]
        acct, _ = self.probe(payload=payload)
        self.assertEqual(acct["status"], "ok")
        w = self.one(acct["windows"])
        self.assertEqual((w["window_kind"], w["used"]), ("weekly", 97))
        self.assertEqual([n for n in self.notes if "drift" in n], [])

    def test_an_empty_usage_block_is_data_not_drift(self):
        """The block's EMPTY direction, mirroring the absent-`limits` test:
        `usage: {}` is present and reports nothing, so the row is all-null
        rather than an error. Crying drift here would false-alarm exactly the
        idle account the panel exists to reassure."""
        payload = fixture("moonshot_usages.json")
        payload["usage"] = {}
        payload["limits"] = []
        acct, _ = self.probe(payload=payload)
        self.assertEqual(acct["status"], "ok")
        w = self.one(acct["windows"])
        self.assertEqual((w["window_kind"], w["used"], w["limit_value"],
                          w["used_percent"]), ("weekly", None, None, None),
                         "an empty block reports nothing, it does not read 0")
        self.assertEqual([n for n in self.notes if "drift" in n], [])

    def test_a_limits_key_that_is_not_a_list_is_drift(self):
        """No legitimate-empty reading exists for the wrong TYPE under a key
        the normalizer iterates."""
        payload = fixture("moonshot_usages.json")
        payload["limits"] = {"weekly": {"used": 1}}
        acct, _ = self.probe(payload=payload)
        self.assertEqual(acct["status"], "error")
        self.assertEqual(acct["windows"], [])
        self.assertTrue(any("shape drift" in n for n in self.notes))

    def test_a_limits_entry_that_is_not_an_object_is_drift(self):
        """L-614-1 here, and this one is not new to the PR — it is the silent
        skip the wrong-type check above was always sitting on top of. A metered
        sub-window the reader cannot open disappears from the card while the
        account-wide weekly figure keeps rendering, so nothing on the card says
        anything went wrong. The absent/empty legs above are the mirror: this
        must not fire on an idle account."""
        payload = fixture("moonshot_usages.json")
        payload["limits"] = list(payload["limits"]) + ["five_hour"]
        acct, _ = self.probe(payload=payload)
        self.assertEqual(acct["status"], "error")
        self.assertEqual(acct["windows"], [],
                         "no partial rows — the weekly figure must not ship "
                         "as if it were the whole reading")
        self.assertTrue(any("shape drift" in n for n in self.notes),
                        f"drift must be loud; log was {self.notes}")

    def test_a_200_that_is_not_a_json_object_says_so(self):
        acct, _ = self.probe(payload="<html>maintenance</html>")
        self.assertEqual(acct["status"], "error")
        self.assertIn("not a JSON object", acct["detail"])
        self.assertTrue(any("shape drift" in n for n in self.notes))

    def test_expired_token_is_reported_never_used(self):
        self.write(self.kimi_creds, {"access_token": TOKEN,
                                     "expires_at": int(time.time()) - 60})
        acct, ep = self.probe()
        self.assertEqual(acct["status"], "unauth")
        self.assertEqual(ep.calls, [])


class CredentialSafetyTest(ProbeCase):
    """The two invariants that make this package safe to run on a live host."""

    def endpoints(self) -> list:
        return [self.endpoint(mod, 200, fixture(name)) for mod, name in (
            (p_anthropic, "anthropic_usage.json"),
            (p_openai, "openai_usage.json"),
            (p_moonshot, "moonshot_usages.json"))]

    def test_token_reaches_the_header_and_nothing_else(self):
        eps = self.endpoints()
        rows = dispatch.probe_all(self.log, timeout=5)
        sent = [call[1].get("Authorization") for ep in eps for call in ep.calls]
        self.assertEqual(len(sent), 3, "every provider must be probed")
        for header in sent:
            self.assertIn(TOKEN, header or "",
                          "the probe must actually authenticate")
        self.assertNotIn(TOKEN, json.dumps(rows),
                         "a token reached a returned row")
        self.assertNotIn("sentinel-refresh", json.dumps(rows))
        self.assertNotIn(TOKEN, "\n".join(self.notes), "a token reached the log")
        self.assertNotIn("sentinel-refresh", "\n".join(self.notes))

    def test_credential_files_are_never_written(self):
        paths = [self.claude_creds, self.claude_profile, self.codex_auth,
                 self.kimi_creds]
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
        self.endpoints()
        dispatch.probe_all(self.log, timeout=5)
        for p in paths:
            self.assertEqual((p.read_bytes(), p.stat().st_mtime_ns), before[p],
                             f"{p.name} was written by a probe")

    def test_no_probe_module_opens_a_credential_file_for_writing(self):
        """A second, static check: nothing in the package can write at all."""
        package = ENGINE / "scripts" / "quota_probes"
        for path in sorted(package.glob("*.py")):
            source = path.read_text()
            for forbidden in ("write_text(", "write_bytes(", "os.replace(",
                              "unlink(", ".refresh("):
                self.assertNotIn(forbidden, source,
                                 f"{path.name} contains a write path")


class DispatchTest(ProbeCase):
    def test_fresh_process_has_no_probe_status(self):
        fresh = importlib.reload(dispatch)
        self.addCleanup(importlib.reload, fresh)

        self.assertEqual({}, fresh.latest_statuses())

    def test_status_accessor_is_timestamped_narrow_and_read_only(self):
        fresh = importlib.reload(dispatch)
        self.addCleanup(importlib.reload, fresh)
        rows = {
            "anthropic": {
                "provider": "anthropic",
                "status": "ok",
                "captured_at": "2020-01-01T00:59:00Z",
                "detail": "must not cross the seam",
                "windows": [{"used_percent": 10}],
            },
            "openai": {
                "provider": "openai",
                "status": "error",
                "captured_at": "2020-01-01T00:58:30Z",
                "detail": "must not cross the seam",
                "windows": [],
            },
        }
        with mock.patch.object(
            fresh,
            "_run",
            side_effect=lambda name, log, timeout: [rows[name]],
        ):
            returned = fresh.probe_all(
                self.log,
                timeout=5,
                providers=["anthropic", "openai"],
            )

        self.assertEqual(
            [rows["anthropic"], rows["openai"]],
            returned,
        )
        statuses = fresh.latest_statuses()
        self.assertEqual(
            {
                "anthropic": ("ok", "2020-01-01T00:59:00Z"),
                "openai": ("error", "2020-01-01T00:58:30Z"),
            },
            statuses,
        )
        statuses.pop("anthropic")
        self.assertEqual(
            {
                "anthropic": ("ok", "2020-01-01T00:59:00Z"),
                "openai": ("error", "2020-01-01T00:58:30Z"),
            },
            fresh.latest_statuses(),
        )

    def test_one_provider_raising_is_contained(self):
        self.endpoint(p_anthropic, 200, fixture("anthropic_usage.json"))
        self.endpoint(p_moonshot, 200, fixture("moonshot_usages.json"))
        boom = mock.patch.object(p_openai, "probe",
                                 side_effect=RuntimeError("provider exploded"))
        boom.start()
        self.addCleanup(boom.stop)
        rows = {r["provider"]: r for r in dispatch.probe_all(self.log, timeout=5)}
        self.assertEqual(set(rows), {"anthropic", "openai", "moonshot"})
        self.assertEqual(rows["openai"]["status"], "error")
        self.assertIn("RuntimeError", rows["openai"]["detail"])
        self.assertEqual(rows["anthropic"]["status"], "ok")
        self.assertEqual(rows["moonshot"]["status"], "ok")
        # Loud, and loud about the right thing. This asserted the exception's
        # MESSAGE ("provider exploded") until flag #195: an exception message
        # can carry the request headers, and these notes are echoed into the
        # API response — so the message was the leak, not the diagnosis. The
        # property the assertion was for (containment is logged, never
        # swallowed) is unchanged; it is pinned on the provider and the type,
        # which cannot carry a token. tests/test_quota_gate.py holds the
        # sweep that proves the message is gone from every surface.
        self.assertTrue(any("openai" in n and "RuntimeError" in n
                            for n in self.notes),
                        "the contained failure was swallowed silently")
        self.assertFalse(any("provider exploded" in n for n in self.notes),
                         "the exception message reached a log line")

    def test_a_hung_provider_costs_the_timeout_not_the_request(self):
        self.endpoint(p_anthropic, 200, fixture("anthropic_usage.json"))
        self.endpoint(p_moonshot, 200, fixture("moonshot_usages.json"))
        self.endpoint(p_openai, 200, fixture("openai_usage.json"), delay=3)
        started = time.monotonic()
        rows = {r["provider"]: r for r in dispatch.probe_all(self.log, timeout=0.2)}
        elapsed = time.monotonic() - started
        # Bound chosen to sit between the contained cost (timeout + grace, so
        # ~1.2s) and the hang (3s): dropping the deadline reddens here rather
        # than hanging the suite, which is what makes it a usable mutation.
        self.assertLess(elapsed, 2, f"the hang was not contained ({elapsed:.1f}s)")
        self.assertEqual(rows["openai"]["status"], "error")
        self.assertIn("timed out", rows["openai"]["detail"])
        self.assertEqual(rows["anthropic"]["status"], "ok")
        self.assertEqual(rows["moonshot"]["status"], "ok")

    def test_a_missing_probe_module_is_one_error_row(self):
        """The other containment leg: a provider whose module cannot be
        imported at all (a rename, a syntax error on a fresh floor) must cost
        its own row and not the sweep."""
        self.endpoint(p_anthropic, 200, fixture("anthropic_usage.json"))
        rows = dispatch.probe_all(self.log, timeout=5,
                                  providers=["anthropic", "nosuchprovider"])
        self.assertEqual([r["provider"] for r in rows],
                         ["anthropic", "nosuchprovider"])
        self.assertEqual(rows[0]["status"], "ok")
        self.assertEqual(rows[1]["status"], "error")
        self.assertIn("unavailable", rows[1]["detail"])

    def test_every_provider_is_probed_in_a_stable_order(self):
        self.endpoint(p_anthropic, 200, fixture("anthropic_usage.json"))
        self.endpoint(p_openai, 200, fixture("openai_usage.json"))
        self.endpoint(p_moonshot, 200, fixture("moonshot_usages.json"))
        rows = dispatch.probe_all(self.log, timeout=5)
        self.assertEqual([r["provider"] for r in rows], qp.PROVIDERS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
