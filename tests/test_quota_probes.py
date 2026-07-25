#!/usr/bin/env python3
"""Tests for the quota probe package — spec doc #49, sprint 52 unit 2.

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

import json
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

import quota_probes as qp  # noqa: E402
from quota_probes import anthropic as p_anthropic  # noqa: E402
from quota_probes import dispatch  # noqa: E402
from quota_probes import moonshot as p_moonshot  # noqa: E402
from quota_probes import openai as p_openai  # noqa: E402

TOKEN = "sentinel-access-token-must-never-leak"
HOUR_MS = 3600 * 1000


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
        self.assertEqual(acct["plan"], "max")
        self.assertEqual(acct["account_ref"],
                         "abcd1234-0000-4000-8000-00000000beef")
        self.assertEqual(acct["account_label"], "placeholder@example.com",
                         "decision #69: the label is the full email")
        windows = self.by_kind(acct)
        self.assertEqual(set(windows), {("session", None), ("weekly", None),
                                        ("weekly_scoped", "Claude Opus 5")})
        self.assertEqual(windows[("session", None)]["used_percent"], 22.0)
        self.assertEqual(windows[("session", None)]["resets_at"],
                         "2026-07-25T21:00:00Z")
        self.assertEqual(ep.calls[0][0], p_anthropic.URL)
        self.assertEqual(ep.calls[0][1]["anthropic-beta"],
                         p_anthropic.BETA_HEADER)

    def test_label_falls_back_to_the_uuid_when_no_address_is_on_file(self):
        """The ref and the label come from the same object but are not the
        same field: an older profile without emailAddress must still key AND
        label the card."""
        self.write(self.claude_profile, {"oauthAccount": {
            "accountUuid": "abcd1234-0000-4000-8000-00000000beef"}})
        acct, _ = self.probe()
        self.assertEqual(acct["account_label"], "abcd1234")
        self.assertEqual(acct["account_ref"],
                         "abcd1234-0000-4000-8000-00000000beef")

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
        acct, ep = self.probe()
        self.assertEqual(acct["status"], "ok")
        self.assertEqual(acct["account_label"], "placeholder@example.com",
                         "the label is the full address, not a local part")
        self.assertEqual(acct["account_ref"], "acct_placeholder_0001")
        self.assertEqual(acct["plan"], "prolite")
        windows = self.by_kind(acct)
        self.assertEqual(set(windows), {("five_hour", None), ("weekly", None),
                                        ("weekly", "premium")})
        self.assertEqual(windows[("five_hour", None)]["used_percent"], 12.5)
        self.assertEqual(windows[("weekly", None)]["resets_at"],
                         "2026-07-30T12:00:00Z")
        self.assertEqual(ep.calls[0][1]["chatgpt-account-id"],
                         "acct_placeholder_0001")

    def test_kind_follows_the_duration_not_the_window_name(self):
        """Swap the two windows' durations: their kinds must swap with them.

        A probe that mapped primary→five_hour / secondary→weekly by name or
        position passes the test above and fails only here.
        """
        payload = fixture("openai_usage.json")
        primary = payload["rate_limit"]["primary_window"]
        secondary = payload["rate_limit"]["secondary_window"]
        primary["limit_window_seconds"], secondary["limit_window_seconds"] = \
            secondary["limit_window_seconds"], primary["limit_window_seconds"]
        acct, _ = self.probe(payload=payload)
        kinds = {w["used_percent"]: w["window_kind"] for w in acct["windows"]}
        self.assertEqual(kinds[12.5], "weekly")
        self.assertEqual(kinds[47.0], "five_hour")

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
                    del payload["rate_limit"]["additional_rate_limits"]
                else:
                    payload["rate_limit"]["additional_rate_limits"] = value
                acct, _ = self.probe(payload=payload)
                self.assertEqual(acct["status"], "ok")
                self.assertEqual({w["window_kind"] for w in acct["windows"]},
                                 {"five_hour", "weekly"})
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

    def test_rejection_keeps_the_account_identified(self):
        acct, _ = self.probe(code=401)
        self.assertEqual(acct["status"], "unauth")
        self.assertEqual(acct["account_ref"], "acct_placeholder_0001",
                         "the ref is on disk, so an unauth card still keys")


class MoonshotProbeTest(ProbeCase):
    def probe(self, code=200, payload=None) -> tuple:
        ep = self.endpoint(p_moonshot, code,
                           fixture("moonshot_usages.json") if payload is None
                           else payload)
        return self.one(p_moonshot.probe(self.log, 5)), ep

    def test_derives_percent_from_absolute_counts(self):
        acct, _ = self.probe()
        self.assertEqual(acct["status"], "ok")
        self.assertEqual(acct["account_ref"], "placeholder-user-0001")
        self.assertEqual(acct["plan"], "LEVEL_ADVANCED")
        windows = self.by_kind(acct)
        weekly = windows[("weekly", None)]
        self.assertEqual((weekly["used"], weekly["limit_value"]), (128000, 500000))
        self.assertEqual(weekly["used_percent"], 25.6)
        five_hour = windows[("five_hour", None)]
        self.assertEqual(five_hour["used_percent"], 21.0)
        self.assertEqual(five_hour["resets_at"], "2026-07-25T20:00:00Z")

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
        self.assertEqual((w["used"], w["limit_value"]), (128000, 0),
                         "the raw counts still stand")

    def test_unknown_time_unit_keeps_the_window(self):
        payload = fixture("moonshot_usages.json")
        payload["limits"][0]["window"] = {"duration": 2, "timeUnit": "FORTNIGHT"}
        acct, _ = self.probe(payload=payload)
        odd = [w for w in acct["windows"] if w["window_kind"] == "unknown"]
        self.assertEqual(len(odd), 1)
        self.assertIn("FORTNIGHT", str(odd[0]["scope"]))

    def test_lost_usage_block_is_drift_not_a_measured_zero(self):
        """`usage` is the account-wide block every payload carries — an
        account with nothing spent reports `used: 0`, it does not drop the
        key. Its absence is the shape having changed, not an empty account."""
        payload = fixture("moonshot_usages.json")
        del payload["usage"]
        acct, _ = self.probe(payload=payload)
        self.assertEqual(acct["status"], "error")
        self.assertEqual(acct["windows"], [])
        self.assertEqual(acct["account_ref"], "placeholder-user-0001",
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
        self.assertEqual((w["window_kind"], w["used"]), ("weekly", 128000))
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
