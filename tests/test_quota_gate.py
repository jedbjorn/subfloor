#!/usr/bin/env python3
"""Verification gate — spec #57 (Provider Quota), superseding spec #49's.

A second copy of a passing check is a second place to drift, so this file holds
only what nothing else proves. Most of spec #57's gate items belong to the
layer that owns them and are pinned there:

    account_label gone from the schema, no email anywhere
                                             test_quota_schema / _snapshot / _api
    snapshot exclusion still passes           test_quota_snapshot_exclusion.py
    openai 200 + version from the CLI         tests/test_quota_probes.py
    403 -> error, never unauth (+ mirror)     tests/test_quota_probes.py
    TIME_UNIT_MINUTE at 300 -> five_hour      tests/test_quota_probes.py
    no container repr reaches scope/card      test_quota_probes / _accounts_ui
    limits[].detail unwrapped, used derived   tests/test_quota_probes.py
    plan gone from schema and probes          test_quota_schema / this file
    fixtures are sanitized real captures      tests/fixtures/.../README.md
    degraded probe keeps figures + age        test_quota_accounts_api / _ui
    README drift claim corrected              tests/fixtures/.../README.md

What lives HERE is what no single layer can reach:

  * the CROSS-LAYER items, which span the probe package, the API and the UI at
    once — token containment, `na` parity across all three providers, a hung
    provider costing only its own reading, and one probe per toggle;
  * deep-linking, which needs the router AND the boot IIFE executed;
  * "every removed mechanism is REMOVED, not left inert", which is a claim
    about the source of every layer at once and cannot be asked of any one.

SPEC #49'S CHECKBOX 7 IS GONE RATHER THAN UNPROVEN — a hidden account switched
back with its first_seen intact. The 7-day filter, the is_current exemption and
first_seen were all dropped by 0097, so there is nothing to hide, switch back
to, or preserve. `NoInertAccountMachineryTest` is what took its place.

Where a checkbox crosses units, the composition is driven through a real
artifact rather than an assumption about one: the toggle test replays the
request sequence the UI actually emits against the real HTTP routes, and the
containment test renders the API's actual response body. Asserting "the UI
GETs" in one file and "a GET probes once" in another leaves the seam between
them — which is the only thing these checkboxes are about — untested.

NO TEST PERFORMS A LIVE CALL. Where a probe's real transport must run (the
not-header-safe case, whose exception fires inside urlopen and so cannot be
reached with urlopen stubbed), the socket itself is barricaded instead.

Run:
    python3 -m pytest tests/test_quota_gate.py
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"
FIXTURES = ROOT / "tests" / "fixtures" / "quota_probes"

sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))

import harness_versions  # noqa: E402
import quota_probes as qp  # noqa: E402
import server  # noqa: E402
from quota_probes import anthropic as p_anthropic  # noqa: E402
from quota_probes import dispatch  # noqa: E402
from quota_probes import moonshot as p_moonshot  # noqa: E402
from quota_probes import openai as p_openai  # noqa: E402

# Distinct from U2's sentinel: a canary that appears in this file only proves
# nothing about the value the other suite plants.
CANARY = "gate-canary-access-token-must-never-leak"
CANARY_REFRESH = "gate-canary-refresh-token"
HOUR_MS = 3600 * 1000

HAS_NODE = shutil.which("node") is not None

APP = (ENGINE / "ui" / "app.js").read_text()


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def build_db(path=":memory:") -> sqlite3.Connection:
    """The engine as it ships: schema.sql plus every migration in order."""
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


class GateCase(unittest.TestCase):
    """Real credential files in a temp HOME, a real migrated DB, and the real
    stack from probe_all through the API assembler."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.con = build_db()
        self.addCleanup(self.con.close)

        saved = dict(server._QUOTA_PROBE)
        self.addCleanup(lambda: server._QUOTA_PROBE.update(saved))
        server._QUOTA_PROBE.update({"at": None, "providers": []})

        self.claude_creds = self.tmp / ".claude/.credentials.json"
        self.claude_profile = self.tmp / ".claude.json"
        self.codex_auth = self.tmp / ".codex/auth.json"
        self.kimi_creds = self.tmp / ".kimi-code/credentials/kimi-code.json"
        self.write_credentials()

        # Paths resolve at import time, so the constants are patched rather
        # than $HOME — the same read the real code performs, without ever
        # touching the operator's own files.
        self.patch(p_anthropic, CREDENTIALS=self.claude_creds,
                   PROFILE=self.claude_profile)
        self.patch(p_openai, CREDENTIALS=self.codex_auth)
        self.patch(p_moonshot, CREDENTIALS=self.kimi_creds)
        # The openai probe's SECOND external dependency: it derives its client
        # version from the installed codex CLI, so an unstubbed seam makes this
        # gate answer differently on a runner that has one and a runner that
        # does not (SC-170). The seam itself is pinned in the probe suite.
        self.patch(harness_versions, probe=lambda _: "codex-cli 0.145.0")

    def write_credentials(self, token: str = CANARY) -> None:
        expires = int((time.time() + 3600) * 1000)
        self.write(self.claude_creds, {"claudeAiOauth": {
            "accessToken": token, "refreshToken": CANARY_REFRESH,
            "expiresAt": expires, "subscriptionType": "max"}})
        self.write(self.claude_profile, {"oauthAccount": {
            "accountUuid": "abcd1234-0000-4000-8000-00000000beef",
            "emailAddress": "placeholder@example.com"}})
        self.write(self.codex_auth, {"auth_mode": "chatgpt", "tokens": {
            "access_token": token, "refresh_token": CANARY_REFRESH,
            "account_id": "acct_placeholder_0001"}})
        self.write(self.kimi_creds, {
            "access_token": token, "refresh_token": CANARY_REFRESH,
            "expires_at": expires})

    def write(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))

    def patch(self, module, **attrs) -> None:
        for name, value in attrs.items():
            p = mock.patch.object(module, name, value)
            p.start()
            self.addCleanup(p.stop)

    def endpoints(self, code=200, delay=0.0, payloads=None) -> dict:
        """Patch every provider's get_json. Default: the recorded fixtures."""
        recorded = {"anthropic": "anthropic_usage.json",
                    "openai": "openai_usage.json",
                    "moonshot": "moonshot_usages.json"}
        eps = {}
        for module, name in ((p_anthropic, "anthropic"), (p_openai, "openai"),
                             (p_moonshot, "moonshot")):
            payload = (payloads or {}).get(name, fixture(recorded[name]))
            ep = Endpoint(code, payload, delay)
            self.patch(module, get_json=ep)
            eps[name] = ep
        return eps

    def no_live_calls(self) -> None:
        """Barricade the socket, not urlopen. The not-header-safe case raises
        inside urlopen, so stubbing urlopen would delete the very path under
        test; forbidding the connection still proves no call left the host."""
        guard = mock.patch("http.client.HTTPConnection.connect",
                           side_effect=AssertionError("live connection attempted"))
        guard.start()
        self.addCleanup(guard.stop)

    def stack(self, force: bool = False) -> dict:
        """The real thing: probe_all -> upsert -> response. Nothing stubbed
        between the probe package and the API's own assembler."""
        return server.get_analytics_quota(self.con, force=force)

    def db_text(self) -> str:
        """Every cell of both quota tables, as text."""
        out = []
        for table in ("harness_quota_account", "harness_quota_window"):
            for row in self.con.execute(f"SELECT * FROM {table}"):
                out.append("|".join("" if v is None else str(v) for v in row))
        return "\n".join(out)

    def assert_no_token(self, response: dict) -> None:
        """The gate's last line, on all three surfaces it names."""
        surfaces = {
            "API response": json.dumps(response, default=str),
            "log line": "\n".join(response.get("notes") or []),
            "DB row": self.db_text(),
        }
        for name, blob in surfaces.items():
            self.assertNotIn(CANARY, blob, f"the access token reached a {name}")
            self.assertNotIn(CANARY_REFRESH, blob,
                             f"the refresh token reached a {name}")


# ── Checkbox 11 — no token in any log line, API response, or DB row ──────────

class TokenSweepTest(GateCase):
    """The gate's last line, and the one no unit owns: it spans all three
    probes AND the API layer, so each unit's own suite could only ever see its
    own half.

    U2 proves the invariant on the path where nothing goes wrong — and its
    harness replaces `urlopen` with an assertion for every test, so no test in
    that package has ever executed the real transport. Every case below is a
    path where something DID go wrong, because that is where a token gets
    carried somewhere it was never meant to go: into an exception message, and
    from there into whatever logs the exception.
    """

    def probed(self, response: dict) -> dict:
        return {p["provider"]: p for p in response["providers"]}

    def test_the_happy_path_leaks_nothing_and_actually_authenticated(self):
        """The positive control the other cases are read against: with all
        three providers answering, the token MUST reach the header (or 'no
        token anywhere' is satisfied by a probe that never ran) and must reach
        nothing else. Extends U2's rows-and-log check to the response body and
        to both DB tables, neither of which any unit asserts."""
        eps = self.endpoints()
        out = self.stack()
        sent = [h.get("Authorization", "") for ep in eps.values()
                for _, h, _ in ep.calls]
        self.assertEqual(3, len(sent), "every provider must be probed")
        for header in sent:
            self.assertIn(CANARY, header, "the probe did not authenticate")
        self.assertTrue(self.db_text(), "nothing was written — a vacuous sweep")
        self.assert_no_token(out)

    def test_a_rejected_credential_leaks_nothing(self):
        self.endpoints(code=401)
        out = self.stack()
        self.assertEqual({"unauth", "unauth", "unauth"},
                         {p["status"] for p in out["providers"]})
        self.assert_no_token(out)

    def test_a_drifted_envelope_leaks_nothing(self):
        self.endpoints(payloads={n: {"nothing": "recognizable"}
                                 for n in ("anthropic", "openai", "moonshot")})
        out = self.stack()
        self.assertIn("error", {p["status"] for p in out["providers"]})
        self.assert_no_token(out)

    def test_an_unreachable_endpoint_leaks_nothing(self):
        """Transport failure: `detail` carries the reason string, so this is
        the response's own path for third-party text."""
        for module in (p_anthropic, p_openai, p_moonshot):
            self.patch(module, get_json=lambda url, headers, timeout: (
                0, "connection refused"))
        out = self.stack()
        self.assertEqual({"error"}, {p["status"] for p in out["providers"]})
        self.assert_no_token(out)

    def test_a_probe_raising_with_the_token_in_its_message_leaks_nothing(self):
        """The classic shape: a transport error whose message carries the
        request headers. The dispatcher contains the failure — that is U2's
        tested property — but containment says nothing about what the
        containment LOGS, and the note it writes is echoed into the API
        response by `probe_quota_accounts`."""
        self.patch(p_openai, get_json=mock.Mock(side_effect=RuntimeError(
            "POST failed: headers={'Authorization': 'Bearer %s'}" % CANARY)))
        self.endpoints_for_the_rest()
        out = self.stack()
        self.assertEqual("error", self.probed(out)["openai"]["status"],
                         "the failure path did not run")
        self.assert_no_token(out)

    def test_a_credential_that_is_not_header_safe_leaks_nothing(self):
        """Reachable with no exotic provider behaviour at all: a token
        carrying a character illegal in an HTTP header — a trailing newline is
        the everyday case, from a hand-copied or truncated credential file.

        `http.client.putheader` raises ValueError whose message is the header
        VALUE. `get_json` catches (URLError, TimeoutError, OSError) only, so it
        escapes; the dispatcher's `except Exception` then logs it. This runs
        the REAL transport — the socket is barricaded rather than urlopen,
        because the exception fires inside urlopen and stubbing it would delete
        the path under test."""
        self.no_live_calls()
        self.write_credentials(token=CANARY + "\n")
        out = self.stack()
        self.assertEqual({"error"}, {p["status"] for p in out["providers"]},
                         "the failure path did not run")
        self.assert_no_token(out)
        # The diagnosis, which is the whole reason the catch lives in get_json
        # rather than the leak being scrubbed at the log: the operator has a
        # corrupt credential file and nothing else can tell them so. Asserted
        # because "no token appears" is equally true of a bare
        # `probe raised ValueError`, which trades the leak for a mystery — and
        # without this the get_json half of the fix is unpinned, which is
        # exactly what the mutation driver caught.
        for provider in out["providers"]:
            self.assertIn("invalid request header", provider["detail"] or "",
                          f"{provider['provider']} reported no usable reason")

    def endpoints_for_the_rest(self) -> None:
        """Fixtures for the two providers a test is not breaking."""
        for module, name in ((p_anthropic, "anthropic_usage.json"),
                             (p_moonshot, "moonshot_usages.json")):
            self.patch(module, get_json=Endpoint(200, fixture(name)))


# ── Checkbox 3 — a missing credential file yields `na`, never 0% ─────────────

class MissingCredentialParityTest(GateCase):
    """U2 carries this leg for anthropic and openai; moonshot has none. Asked
    once across all three providers rather than as a third copy of the same
    test, so adding a fourth provider without the leg fails here."""

    def test_no_credential_file_anywhere_is_na_and_never_a_zero(self):
        for path in (self.claude_creds, self.claude_profile, self.codex_auth,
                     self.kimi_creds):
            path.unlink()
        self.no_live_calls()
        out = self.stack()
        self.assertEqual({"anthropic": "na", "openai": "na", "moonshot": "na"},
                         {p["provider"]: p["status"] for p in out["providers"]})
        # `na` is not `0%`: no registry row, and no zero anywhere in the payload
        # that a meter could render as measured. THE CARDS STILL RENDER — every
        # provider gets an entry regardless — so the check is that they carry no
        # numbers, not that they are absent.
        self.assertEqual("", self.db_text())
        for entry in out["providers"]:
            self.assertEqual([], entry["windows"])
            self.assertIsNone(entry["captured_at"])


# ── Spec 57 gate — every removed mechanism is REMOVED, not left inert ────────

class NoInertAccountMachineryTest(unittest.TestCase):
    """Spec #57's gate item: "Every removed piece of account machinery is
    removed, not left inert: no reader remains for anything dropped, and no
    comment describes a mechanism that no longer exists."

    THIS REPLACES SPEC #49'S CHECKBOX 7 (a hidden account switched back with its
    first_seen intact). That checkbox is not unproven here — its MECHANISM is
    gone: the 7-day filter, the is_current exemption and first_seen were all
    dropped by 0097, so there is no hiding, no switching back, and no timestamp
    to preserve. Testing it would be testing a mechanism that does not exist,
    which is the same defect this item exists to catch.

    Source-level on purpose, and it is the one gate item where that is the
    right instrument: "no reader remains" is a claim about the CODE, and a
    behavioural test cannot distinguish dead code from absent code — which is
    exactly the distinction the item is about. This feature's documented
    recurring defect is dead machinery carrying a live comment.
    """

    ENGINE_SOURCES = ("api/server.py", "ui/app.js", "ui/style.css",
                      "scripts/quota_probes/__init__.py",
                      "scripts/quota_probes/anthropic.py",
                      "scripts/quota_probes/openai.py",
                      "scripts/quota_probes/moonshot.py")
    # Every column 0097 drops, plus the two UI mechanisms that read them.
    DROPPED = ("account_label", "is_current", "first_seen", "last_seen")

    def quota_region(self, text: str, path: str) -> str:
        """Only the quota code — these names are common words elsewhere in the
        engine (watched_prs has its own last_seen, and every tab has a plan)."""
        if path == "api/server.py":
            return text[text.index("def _quota_upsert"):text.index("# \u2500\u2500 Mutations")]
        if path == "ui/app.js":
            return text[text.index("// \u2500\u2500 Provider Quota"):
                        text.index("// \u2500\u2500 Interface tab")]
        if path == "ui/style.css":
            return text[text.index("/* \u2500\u2500 Provider Quota"):]
        return text

    @staticmethod
    def code_only(text: str, rel: str) -> str:
        """Executable code with prose stripped out.

        The distinction is deliberate and is the whole reason this helper
        exists. A comment SAYING a column was dropped and why is the opposite
        of the defect — it is how the next reader learns the mechanism is gone.
        What the gate forbids is a comment describing a mechanism AS IF IT
        STILL RAN, and a live reader for a column that is not there. Only the
        second is decidable by grep, so only the second is asserted."""
        if rel.endswith(".py"):
            text = re.sub(r'""".*?"""', "", text, flags=re.S)
            return "\n".join(line.split("#")[0] for line in text.splitlines())
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        return "\n".join(line for line in text.splitlines()
                         if not line.lstrip().startswith("//"))

    def test_no_dropped_column_has_a_reader_left_anywhere(self):
        for rel in self.ENGINE_SOURCES:
            region = self.code_only(
                self.quota_region((ENGINE / rel).read_text(), rel), rel)
            for name in self.DROPPED:
                self.assertNotIn(
                    name, region,
                    f"{rel} still READS {name} in live code, which 0097 "
                    "dropped — the column does not exist to be read")

    def test_the_stripper_can_actually_see_a_reader(self):
        """POSITIVE CONTROL for the test above, and it needs one badly: the
        assertion is a grep over text that has had prose removed, so an
        over-eager stripper would return an empty string and pass everything.

        A real reader is planted in each language's code AND in its comments;
        the code one must be seen and the comment one must not."""
        cases = [("x.py", 'q = "SELECT last_seen FROM t"  # last_seen in a comment\n'
                          '"""last_seen in a docstring"""\n'),
                 ("x.js", "const a = row.last_seen;\n// last_seen in a comment\n"),
                 ("x.css", ".last_seen { color: red }\n/* last_seen in a comment */\n")]
        for rel, src in cases:
            with self.subTest(rel=rel):
                stripped = self.code_only(src, rel)
                self.assertIn("last_seen", stripped, "the stripper ate the code")
                self.assertEqual(1, stripped.count("last_seen"),
                                 f"prose survived stripping in {rel}: {stripped!r}")

    def test_the_probe_package_no_longer_collects_a_plan(self):
        """`plan` gets its own leg because it is the one that reads as
        harmless. It is not identity the way an email is, so "leave it, it
        costs nothing" is the argument that would have kept it — and it is
        precisely the argument that let two of three probes read it from the
        wrong place undetected."""
        acct = qp.account(provider="anthropic", probe_version="1",
                          captured_at=iso())
        self.assertNotIn("plan", acct)
        # ...and no probe module passes one, which the builder alone cannot say.
        for rel in ("anthropic", "openai", "moonshot"):
            src = (ENGINE / f"scripts/quota_probes/{rel}.py").read_text()
            self.assertNotIn("plan=", src, f"{rel}.py still collects a plan")

    def test_the_ui_carries_no_sign_in_language_at_all(self):
        """The claims both shipped defects made, gone from the source rather
        than merely unreachable. A string a card cannot currently render is one
        refactor away from rendering again."""
        region = self.quota_region((ENGINE / "ui/app.js").read_text(), "ui/app.js")
        for claim in ("signed out", "not signed in", "no account identified"):
            self.assertNotIn(
                claim.lower(),
                # The section's own commentary NAMES these strings to explain
                # why they are gone, so only rendered literals are examined.
                "".join(line for line in region.splitlines(keepends=True)
                        if not line.lstrip().startswith("//")).lower(),
                f"the section can still render {claim!r}")


# ── Node-driven halves: checkboxes 8 and 4 ───────────────────────────────────

EL = APP[APP.index("const el ="):APP.index("const esc =")]
HELPERS = APP[APP.index("const fmt = (n)"):APP.index("// On/off switch")]
QUOTA = APP[APP.index("// ── Provider Quota"):APP.index("// ── Interface tab")]
_ROUTER_AT = APP.index("function routeFromHash()")
# U4's slice stops at the nav-button line; this one deliberately runs PAST it,
# through `window.addEventListener("hashchange", routeFromHash)` — the wiring
# is exactly what checkbox 8 is about and what that slice leaves out.
ROUTER = APP[_ROUTER_AT:APP.index("// Close any open popover menu", _ROUTER_AT)]
# The boot IIFE, verbatim: `routeFromHash()` on load is the whole of "survives
# a reload", and nothing anywhere executes it.
BOOT = APP[APP.index('(async () => {\n  try {\n    const h = await api("/health");'):]

DOM = """
class FakeElement {
  constructor(tag) {
    this.tagName = tag; this.nodeType = 1; this.children = [];
    this._text = ""; this.className = ""; this.disabled = false;
    this.title = ""; this.style = {}; this.onclick = null; this.dataset = {};
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; this._text = ""; }
  set textContent(value) { this._text = String(value ?? ""); this.children = []; }
  get textContent() {
    return this._text + this.children.map(
      (c) => typeof c === "string" ? c : (c.textContent || "")).join("");
  }
}
globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (text) => ({ nodeType: 3, textContent: String(text ?? "") }),
  querySelectorAll: () => [],
  addEventListener: () => {},
};
globalThis.location = { hash: "" };
let listeners = {};
globalThis.window = {
  addEventListener: (name, fn) => { (listeners[name] ||= []).push(fn); },
  open: () => {},
};
let calls = [];
let toasts = [];
let tokenSectionRuns = 0;
let apiFail = null;
async function api(path, method = "GET", body) {
  calls.push({ path, method: method || "GET" });
  if (path === "/health") return { repo: "super-coder", port: 8800 };
  if (apiFail) throw new Error(apiFail);
  return PAYLOAD;
}
function toast(msg) { toasts.push(String(msg)); }
async function anTokenSection(root) {
  tokenSectionRuns += 1;
  await api("/analytics/sweep", "POST");
  root.replaceChildren(new FakeElement("div"));
}
let anView = "tokens";
let roadmapView = null, ifSelected = null, shown = [];
const VIEWS = { analytics: 1, roadmap: 1, interface: 1, shells: 1, docs: 1 };
function show(tab) { shown.push(tab); }
// Boot-region collaborators, stubbed so the IIFE runs as written.
let forkName = "";
const STUB = new FakeElement("div");
function $(sel) { return STUB; }
function setStatus(s) {}
function all(root, pred, found = []) {
  if (pred(root)) found.push(root);
  for (const c of root.children || []) if (c && c.nodeType === 1) all(c, pred, found);
  return found;
}
const cls = (n) => String(n.className || "").split(" ").filter(Boolean);
const byClass = (root, name) => all(root, (n) => cls(n).includes(name));
const buttons = (root) => all(root, (n) => n.tagName === "button");
const texts = (nodes) => nodes.map((n) => n.textContent);
function out(obj) { console.log(JSON.stringify(obj)); }
function root() { return new FakeElement("div"); }
"""


def run_js(body: str, data: dict | None = None, boot: bool = False,
           hash: str = "") -> dict:
    """Drive the real app.js regions under a minimal DOM.

    `boot` appends the app's own load-time IIFE — what a reload actually runs.
    `hash` is in place BEFORE that IIFE evaluates, exactly as a real page load
    has it; setting it afterwards would test a navigation, not a reload."""
    dom = DOM.replace('globalThis.location = { hash: "" };',
                      'globalThis.location = { hash: "%s" };' % hash)
    script = ("const PAYLOAD = " + json.dumps(data or {"providers": []})
              + ";\n" + EL + HELPERS + dom + QUOTA + ROUTER
              + (BOOT if boot else "")
              # The boot IIFE awaits /health before it routes, so the body has
              # to let it settle — reading the view synchronously would see the
              # state from before the load finished.
              + "\n(async () => {\n"
              + ("await new Promise((r) => setTimeout(r, 0));\n" if boot else "")
              + body + "\n})().catch((e) => {"
              " console.error(e.stack || e); process.exit(1); });\n")
    proc = subprocess.run(["node", "-e", script], text=True, capture_output=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@unittest.skipUnless(HAS_NODE, "node is required")
class DeepLinkSurvivesReloadTest(unittest.TestCase):
    """Checkbox 8. U4 proves that routeFromHash MAPS #analytics-quota to the
    accounts sub-view, and that the toggle sets the hash. Neither proves the
    hash is ever READ — its ROUTER slice stops before
    `window.addEventListener("hashchange", routeFromHash)`, and the boot-time
    `routeFromHash()` call sits in an IIFE at the end of the file that no test
    executes at all.

    A deep link that nothing invokes on load is a URL that works only if you
    were already on the page. That is precisely the half "survives a reload"
    means, so both wirings are executed here rather than read."""

    def test_loading_the_page_on_the_quota_hash_lands_on_the_section(self):
        r = run_js("""
          out({ view: anView, tab: shown[shown.length - 1], routed: shown.length });
        """, boot=True, hash="#analytics-quota")
        self.assertEqual("quota", r["view"])
        self.assertEqual("analytics", r["tab"])
        self.assertEqual(1, r["routed"], "the load routed more than once")

    def test_the_boot_route_runs_even_when_the_health_call_fails(self):
        """`routeFromHash()` sits outside the IIFE's try/catch on purpose: an
        offline engine must still leave the operator on the URL they loaded,
        not silently on Shells."""
        r = run_js("""
          apiFail = "offline";
          out({ view: anView, tab: shown[shown.length - 1] });
        """, boot=True, hash="#analytics-quota")
        self.assertEqual("quota", r["view"])
        self.assertEqual("analytics", r["tab"])

    def test_a_reload_on_the_token_hash_does_not_land_on_quota(self):
        """The other direction, so the test above cannot pass by the sub-view
        happening to default to accounts."""
        r = run_js("""
          out({ view: anView, tab: shown[shown.length - 1] });
        """, boot=True, hash="#analytics")
        self.assertEqual("tokens", r["view"])
        self.assertEqual("analytics", r["tab"])

    def test_a_reload_with_no_hash_still_lands_on_the_default_view(self):
        r = run_js("""
          out({ tab: shown[shown.length - 1] });
        """, boot=True)
        self.assertEqual("shells", r["tab"])

    def test_the_hash_listener_is_registered_so_back_and_forward_route(self):
        r = run_js("""
          const registered = (listeners.hashchange || []).length;
          location.hash = "#analytics-quota";
          for (const fn of listeners.hashchange || []) fn();
          out({ registered, view: anView, tab: shown[shown.length - 1] });
        """)
        self.assertEqual(1, r["registered"], "nothing listens for a hash change")
        self.assertEqual("quota", r["view"])
        self.assertEqual("analytics", r["tab"])


# ── Checkbox 9 — toggling sections twice inside a minute performs one probe ──

class ProbeCountAcrossToggleTest(unittest.TestCase):
    """The seam. U3 proves the TTL by calling the assembler twice directly; U4
    proves arrival GETs once and never POSTs. Neither drives a toggle, so
    nothing yet connects "the UI toggles" to "one probe happens".

    The composition is not assumed here: the UI's OWN emitted request sequence
    is captured from node and replayed against the real HTTP routes. If the
    section ever starts forcing a probe on arrival, or the toggle stops
    round-tripping through the API at all, this changes with it."""

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
        con = sqlite3.connect(self.db)
        con.execute("DELETE FROM harness_quota_window")
        con.execute("DELETE FROM harness_quota_account")
        con.commit()
        con.close()
        self.calls = []

    def probed(self):
        def fake(log, *a, **kw):
            self.calls.append(True)
            return [qp.account(provider="anthropic", probe_version="1",
                               captured_at=iso(), account_ref="uuid-1",
                               windows=[qp.window(window_kind="weekly",
                                                  probe_version="1",
                                                  captured_at=iso(),
                                                  used_percent=22.0)])]
        return mock.patch.object(server.quota_dispatch, "probe_all", fake)

    def replay(self, emitted: list[dict]) -> None:
        """Issue the requests the UI emitted, at the URLs it would reach."""
        for call in emitted:
            req = urllib.request.Request(
                self.base + "/api" + call["path"], method=call["method"],
                data=b"{}" if call["method"] == "POST" else None)
            with urllib.request.urlopen(req, timeout=10) as r:
                self.assertEqual(200, r.status)
                r.read()

    @unittest.skipUnless(HAS_NODE, "node is required")
    def test_quota_then_tokens_then_quota_inside_a_minute_probes_once(self):
        emitted = run_js("""
          anView = "quota";  await renderAnalytics(root());
          anView = "tokens"; await renderAnalytics(root());
          anView = "quota";  await renderAnalytics(root());
          out({ emitted: calls });
        """)["emitted"]
        # The UI's real behaviour, stated so a change to it is visible here:
        # two arrivals at quota, one at tokens, no POST anywhere.
        self.assertEqual(
            ["/analytics/quota GET", "/analytics/sweep POST",
             "/analytics/quota GET"],
            [c["path"] + " " + c["method"] for c in emitted])
        with self.probed():
            self.replay([c for c in emitted if "quota" in c["path"]])
        self.assertEqual(1, len(self.calls),
                         "the toggle hammered the providers twice inside the TTL")

    def test_the_refresh_button_still_bypasses_the_ttl_over_http(self):
        """The other side of the same clock: the TTL must absorb a toggle and
        must never absorb the refresh button."""
        with self.probed():
            self.replay([{"path": "/analytics/quota", "method": "GET"},
                         {"path": "/analytics/quota", "method": "GET"},
                         {"path": "/analytics/quota/probe", "method": "POST"}])
        self.assertEqual(2, len(self.calls))


# ── Checkbox 4 — one provider timing out leaves the other two cards rendering ─

class TimeoutLeavesOtherCardsTest(GateCase):
    """U2 proves containment in ROWS; the spec words this checkbox in CARDS,
    and no test carries a hung provider all the way to one. The API response
    rendered here is the real one — produced by a real probe_all against a
    provider that genuinely hangs — rather than a hand-authored payload
    asserting what the API is assumed to emit."""

    @unittest.skipUnless(HAS_NODE, "node is required")
    def test_a_hung_provider_costs_its_own_card_and_no_others(self):
        self.endpoints()
        # Only openai hangs; the deadline that contains it is the DISPATCHER's,
        # which is the property under test. The real probe_all runs — only its
        # deadline is shortened, so the suite does not sit out the shipped 5s.
        # (That the route passes no timeout of its own is U3's test, not this.)
        self.patch(p_openai, get_json=Endpoint(200, fixture("openai_usage.json"),
                                               delay=3))
        # Bound before patching: server.quota_dispatch IS this module, so
        # reading probe_all through it inside the replacement would recurse.
        real_probe_all = dispatch.probe_all
        short = mock.patch.object(
            server.quota_dispatch, "probe_all",
            lambda log, *a, **kw: real_probe_all(log, timeout=0.2))
        started = time.monotonic()
        with short:
            out = self.stack()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2, f"the hang was not contained ({elapsed:.1f}s)")

        by_provider = {p["provider"]: p for p in out["providers"]}
        self.assertEqual("error", by_provider["openai"]["status"])
        self.assertIn("timed out", by_provider["openai"]["detail"])

        rendered = run_js("""
          const r0 = root(); anDrawQuota(r0, PAYLOAD);
          out({ cards: byClass(r0, "an-acct").length,
                withWindows: byClass(r0, "an-acct").filter(
                  (c) => byClass(c, "an-win").length).length,
                body: r0.textContent });
        """, data=json.loads(json.dumps(out, default=str)))
        # Every provider still renders a card, and the two healthy ones still
        # carry their NUMBERS — the card count alone cannot show that, since
        # cards render unconditionally now.
        self.assertEqual(3, rendered["cards"])
        self.assertEqual(2, rendered["withWindows"],
                         "the hang cost another provider its reading")
        self.assertIn("Claude", rendered["body"])
        self.assertIn("Kimi", rendered["body"])
        # ...and the hung one says so rather than going quiet or reading 0%.
        self.assertIn("timed out", rendered["body"])
        self.assertNotIn(CANARY, rendered["body"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
