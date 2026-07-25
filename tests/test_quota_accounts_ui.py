"""Account Analytics UI (spec #49 U4): toggle, router, cards, thresholds.

Driven through node against the REAL app.js regions with a minimal DOM — the
same idiom as the interface UI contract suite — rather than grepping the source
for strings. A string assertion passes against a function nobody calls; these
render the section and read what came out.

The unit's value rests on two properties, and both are about not lying about a
number, so both are asked in both directions:

  * COLOUR IS COMPUTED FROM used_percent ALONE. Asserting "96% is red" leaves a
    reading of a provider's own severity field entirely free, so every colour
    test also pins a card whose provider status disagrees with its percent —
    an `error` provider at 22% must still render normal, and an `ok` provider at
    96% must still render red.
  * A MISSING NUMBER IS NEVER DRAWN AS ZERO. Asserting "n/a is shown" leaves a
    0%-wide bar free to be drawn under the label, which is the failure mode the
    spec names: a meter reads as measured. So every n/a test asserts the text
    AND the absence of a fill element.

The per-provider status list gets the same treatment: `na` and an empty accounts
array are asserted to render differently, because collapsing them is precisely
what the status list exists to prevent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / ".super-coder" / "ui" / "app.js").read_text()
CSS = (ROOT / ".super-coder" / "ui" / "style.css").read_text()

EL = APP[APP.index("const el ="):APP.index("const esc =")]
# fmt / microlabel / statRow — sliced, not restated, so a card's meta row is
# rendered by the app's own helper.
HELPERS = APP[APP.index("const fmt = (n)"):APP.index("// On/off switch")]
ACCOUNTS = APP[APP.index("// ── Account Analytics"):APP.index("// ── Interface tab")]
_ROUTER_AT = APP.index("function routeFromHash()")
ROUTER = APP[_ROUTER_AT:
             APP.index('document.querySelectorAll("nav button").forEach', _ROUTER_AT)]

HAS_NODE = shutil.which("node") is not None
pytestmark = pytest.mark.skipif(not HAS_NODE, reason="node is required")


def iso(**delta) -> str:
    return (datetime.now(timezone.utc) + timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def window(kind="weekly", pct=10.0, **over) -> dict:
    """One harness_quota_window row as GET /api/analytics/accounts sends it."""
    row = {"window_pk": 1, "window_kind": kind, "scope": None, "used_percent": pct,
           "used": None, "limit_value": None, "resets_at": iso(hours=3),
           "captured_at": iso(minutes=-2), "status": "ok", "probe_version": "1"}
    row.update(over)
    return row


def account(provider="anthropic", **over) -> dict:
    row = {"account_pk": 1, "provider": provider, "account_ref": "ref-1",
           "account_label": "jed@person.com", "plan": "max",
           "first_seen": iso(days=-30), "last_seen": iso(minutes=-2),
           "is_current": 1, "windows": [window()]}
    row.update(over)
    return row


def payload(accounts=None, providers=None, **over) -> dict:
    d = {"accounts": [account()] if accounts is None else accounts,
         "activity_days": 7, "ttl_seconds": 60, "probed": True,
         "providers": ([{"provider": "anthropic", "status": "ok", "detail": None}]
                       if providers is None else providers),
         "notes": []}
    d.update(over)
    return d


HARNESS = r"""
class FakeElement {
  constructor(tag) {
    this.tagName = tag; this.nodeType = 1; this.children = [];
    this._text = ""; this.className = ""; this.disabled = false;
    this.title = ""; this.style = {}; this.onclick = null;
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
};
globalThis.location = { hash: "" };

let calls = [];
let toasts = [];
let tokenSectionRuns = 0;
let apiFail = null;
async function api(path, method = "GET", body) {
  calls.push({ path, method: method || "GET" });
  if (apiFail) throw new Error(apiFail);
  return PAYLOAD;
}
function toast(msg) { toasts.push(String(msg)); }
// The token section is stubbed so "arriving at accounts never sweeps" is
// observable: a real one would fire /analytics/sweep into the same recorder.
async function anTokenSection(root) {
  tokenSectionRuns += 1;
  await api("/analytics/sweep", "POST");
  root.replaceChildren(new FakeElement("div"));
}
let anView = "tokens";

// routeFromHash's collaborators. `show` records rather than renders, so the
// nav-tab claim is checked as a value and not as a side effect.
let roadmapView = null, ifSelected = null, shown = [];
const VIEWS = { analytics: 1, roadmap: 1, interface: 1, shells: 1, docs: 1 };
function show(tab) { shown.push(tab); }

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


def run_js(body: str, data: dict | None = None) -> dict:
    script = ("const PAYLOAD = " + json.dumps(data or payload()) + ";\n"
              + EL + HELPERS + HARNESS + ACCOUNTS + ROUTER
              + "\n(async () => {\n" + body + "\n})().catch((e) => {"
              " console.error(e.stack || e); process.exit(1); });\n")
    proc = subprocess.run(["node", "-e", script], text=True, capture_output=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ── router + toggle ──────────────────────────────────────────────────────────

def test_accounts_hash_routes_to_the_analytics_tab_as_a_sub_view():
    """#analytics-accounts is a URL, and `analytics` stays the active nav tab —
    the #roadmap / #roadmap-flow convention. Both halves matter: routing to a
    tab of its own would lose the toggle, and not routing at all would fall
    through to the default view."""
    r = run_js("""
      const seen = [];
      for (const hash of ["#analytics-accounts", "#analytics", "#analytics-accounts"]) {
        location.hash = hash; routeFromHash();
        seen.push({ view: anView, tab: shown[shown.length - 1] });
      }
      location.hash = "#roadmap-flow"; routeFromHash();
      out({ seen, roadmapTab: shown[shown.length - 1], roadmapView, viewAfter: anView });
    """)
    assert r["seen"] == [{"view": "accounts", "tab": "analytics"},
                         {"view": "tokens", "tab": "analytics"},
                         {"view": "accounts", "tab": "analytics"}]
    # The new branch must not swallow the roadmap sub-view it was modelled on.
    assert r["roadmapTab"] == "roadmap" and r["roadmapView"] == "flow"
    assert r["viewAfter"] == "accounts"


def test_unknown_hash_still_falls_through_to_the_default_view():
    r = run_js("""
      location.hash = "#nonsense"; routeFromHash();
      out({ tab: shown[shown.length - 1] });
    """)
    assert r["tab"] == "shells"


def test_toggle_navigates_by_hash_so_the_sub_view_is_deep_linkable():
    """Clicking must set the hash, not re-render in place: a toggle that
    re-renders directly leaves the URL behind and the section stops surviving a
    reload — which is one of the spec's verification checkboxes."""
    r = run_js("""
      const a = root(); anView = "tokens"; await renderAnalytics(a);
      const chips = buttons(a).filter((b) => cls(b).includes("chip"));
      const labels = texts(chips);
      const active = texts(chips.filter((b) => cls(b).includes("on")));
      chips[1].onclick();
      const afterAccounts = location.hash;
      const b = root(); anView = "accounts"; await renderAnalytics(b);
      const chips2 = buttons(b).filter((x) => cls(x).includes("chip"));
      const active2 = texts(chips2.filter((x) => cls(x).includes("on")));
      chips2[0].onclick();
      out({ labels, active, active2, afterAccounts, afterTokens: location.hash });
    """)
    assert r["labels"] == ["Token Analytics", "Account Analytics"]
    assert r["active"] == ["Token Analytics"] and r["active2"] == ["Account Analytics"]
    assert r["afterAccounts"] == "analytics-accounts"
    assert r["afterTokens"] == "analytics"


def test_each_section_fires_only_its_own_work_on_arrival():
    """The token sweep and the account probe both fire on entry. Arriving at one
    must not run the other: reading token spend would otherwise cost three
    third-party calls, and the probe is specified to fire ONLY here."""
    r = run_js("""
      anView = "accounts"; await renderAnalytics(root());
      const onAccounts = { calls: calls.map((c) => c.path + " " + c.method), token: tokenSectionRuns };
      calls = []; tokenSectionRuns = 0;
      anView = "tokens"; await renderAnalytics(root());
      out({ onAccounts, onTokens: { calls: calls.map((c) => c.path + " " + c.method),
                                    token: tokenSectionRuns } });
    """)
    assert r["onAccounts"] == {"calls": ["/analytics/accounts GET"], "token": 0}
    assert r["onTokens"] == {"calls": ["/analytics/sweep POST"], "token": 1}


def test_arrival_probes_once_and_never_forces_the_ttl():
    """GET is the arrival probe — the route decides for itself whether the TTL
    has aged out. A client that also POSTs would defeat the TTL outright and
    make "toggling twice inside a minute performs one probe" false."""
    r = run_js("""
      await anAccountSection(root());
      out({ calls: calls.map((c) => c.path + " " + c.method) });
    """)
    assert r["calls"] == ["/analytics/accounts GET"]


def test_section_reports_a_failed_read_instead_of_rendering_an_empty_panel():
    r = run_js("""
      apiFail = "HTTP 500";
      const r0 = root(); await anAccountSection(r0);
      out({ text: r0.textContent });
    """)
    assert "error: HTTP 500" in r["text"]


# ── thresholds: colour from used_percent alone ───────────────────────────────

THRESHOLD_CASES = [(0.0, ""), (79.9, ""), (80.0, "amber"), (94.9, "amber"),
                   (95.0, "red"), (100.0, "red"), (140.0, "red")]


@pytest.mark.parametrize("pct,expected", THRESHOLD_CASES)
def test_meter_colour_is_threshold_driven(pct, expected):
    r = run_js("""
      const r0 = root();
      anDrawAccounts(r0, PAYLOAD);
      const pctNode = byClass(r0, "an-win-pct")[0];
      const fill = byClass(r0, "an-meter-fill")[0];
      out({ pctClasses: cls(pctNode), fillClasses: cls(fill), width: fill.style.width,
            text: pctNode.textContent });
    """, payload(accounts=[account(windows=[window(pct=pct)])]))
    tone = [c for c in r["pctClasses"] if c in ("amber", "red")]
    assert tone == ([expected] if expected else [])
    assert [c for c in r["fillClasses"] if c in ("amber", "red")] == ([expected] if expected else [])
    # Over 100 fills the track rather than overflowing it.
    assert r["width"] == f"{min(100.0, pct):g}%"


def test_provider_status_never_decides_colour():
    """The other direction, and the one a percent-only test leaves free: an
    `error` provider at 22% must render normal and an `ok` provider at 96% must
    render red. Colouring off the provider's own vocabulary is exactly what the
    spec forbids — anthropic says severity=normal at 22%, openai says
    limit_reached at 100%, moonshot says nothing."""
    data = payload(
        accounts=[account(provider="openai", account_pk=1, windows=[window(pct=22.0)]),
                  account(provider="anthropic", account_pk=2, account_ref="ref-2",
                          windows=[window(pct=96.0)])],
        providers=[{"provider": "openai", "status": "error", "detail": "HTTP 429"},
                   {"provider": "anthropic", "status": "ok", "detail": None}])
    r = run_js("""
      const r0 = root(); anDrawAccounts(r0, PAYLOAD);
      out({ tones: byClass(r0, "an-win-pct").map((n) => cls(n).filter((c) => c !== "an-win-pct")),
            texts: texts(byClass(r0, "an-win-pct")) });
    """, data)
    assert r["tones"] == [[], ["red"]]
    assert r["texts"] == ["22%", "96%"]


# ── never render a zero as if measured ───────────────────────────────────────

def test_null_percent_reads_na_and_draws_no_bar():
    """All three halves. The text alone would pass while a 0%-wide fill sat under
    it, and a meter with a bar reads as measured however it is labelled — and the
    label itself must carry NO threshold colour, because an absent number is not
    a comfortable one."""
    r = run_js("""
      const r0 = root(); anDrawAccounts(r0, PAYLOAD);
      const pctNode = byClass(r0, "an-win-pct")[0];
      out({ text: texts(byClass(r0, "an-win-pct")), fills: byClass(r0, "an-meter-fill").length,
            meters: byClass(r0, "an-meter").length,
            tone: cls(pctNode).filter((c) => c !== "an-win-pct") });
    """, payload(accounts=[account(windows=[window(pct=None)])]))
    assert r["text"] == ["n/a"]
    assert r["fills"] == 0
    assert r["tone"] == []
    assert r["meters"] == 1  # the empty track still renders — the row is not dropped


def test_moonshot_all_null_row_renders_as_na_rather_than_zero_or_nothing():
    """Moonshot `usage: {}` emits one all-null weekly row where openai emits
    none — asymmetric but truthful (U2's reviewer). It must neither be filtered
    out nor drawn as 0%."""
    r = run_js("""
      const r0 = root(); anDrawAccounts(r0, PAYLOAD);
      out({ rows: byClass(r0, "an-win").length, pct: texts(byClass(r0, "an-win-pct")),
            fills: byClass(r0, "an-meter-fill").length, name: texts(byClass(r0, "an-win-name")) });
    """, payload(
        accounts=[account(provider="moonshot", account_label="usr-9",
                          windows=[window(kind="weekly", pct=None, used=None,
                                          limit_value=None, resets_at=None)])],
        providers=[{"provider": "moonshot", "status": "ok", "detail": None}]))
    assert r["rows"] == 1 and r["pct"] == ["n/a"] and r["fills"] == 0
    assert r["name"] == ["Weekly"]


def test_zero_limit_renders_na_not_a_division():
    """limit_value = 0 arrives with used_percent NULL (the probe never
    back-computes). The counts still show, so the operator sees WHY it is n/a."""
    r = run_js("""
      const r0 = root(); anDrawAccounts(r0, PAYLOAD);
      out({ pct: texts(byClass(r0, "an-win-pct")), fills: byClass(r0, "an-meter-fill").length,
            body: r0.textContent });
    """, payload(accounts=[account(windows=[window(pct=None, used=0, limit_value=0)])]))
    assert r["pct"] == ["n/a"] and r["fills"] == 0
    assert "0 / 0" in r["body"]


def test_a_real_zero_percent_still_draws_a_measured_zero():
    """The mirror of the n/a rule, so the fix cannot be "never draw a bar": a
    provider that genuinely measured 0% renders 0%, not n/a."""
    r = run_js("""
      const r0 = root(); anDrawAccounts(r0, PAYLOAD);
      out({ pct: texts(byClass(r0, "an-win-pct")), width: byClass(r0, "an-meter-fill")[0].style.width });
    """, payload(accounts=[account(windows=[window(pct=0.0, used=0, limit_value=500)])]))
    assert r["pct"] == ["0%"] and r["width"] == "0%"


# ── per-provider status list ─────────────────────────────────────────────────

def test_na_provider_renders_no_card_at_all():
    """A missing credential file is not a limit of zero — it is no card. Asserted
    against a payload where another provider IS configured, so "renders nothing"
    cannot pass by the whole section being empty."""
    r = run_js("""
      const r0 = root(); anDrawAccounts(r0, PAYLOAD);
      out({ groups: texts(byClass(r0, "an-prov-head")), cards: byClass(r0, "an-acct").length,
            body: r0.textContent });
    """, payload(
        accounts=[account(provider="anthropic")],
        providers=[{"provider": "anthropic", "status": "ok", "detail": None},
                   {"provider": "openai", "status": "na", "detail": None},
                   {"provider": "moonshot", "status": "na", "detail": None}]))
    assert r["groups"] == ["Claudeanthropic"]
    assert r["cards"] == 1
    assert "Codex" not in r["body"] and "Kimi" not in r["body"]


def test_error_before_identification_still_renders_a_card_with_its_status():
    """The case the accounts array cannot express: the probe failed before it
    could name anybody, so there is no registry row. Silence here would read as
    "fine" — the spec requires a card carrying the HTTP status."""
    r = run_js("""
      const r0 = root(); anDrawAccounts(r0, PAYLOAD);
      out({ cards: byClass(r0, "an-acct").length, body: r0.textContent });
    """, payload(accounts=[], providers=[
        {"provider": "openai", "status": "error", "detail": "HTTP 503"}]))
    assert r["cards"] == 1
    assert "HTTP 503" in r["body"] and "Codex" in r["body"]


def test_nothing_configured_and_no_probe_yet_say_different_things():
    """Both arrive as an empty accounts array. Collapsing them is what the
    per-provider status list exists to prevent."""
    all_na = run_js("""
      const r0 = root(); anDrawAccounts(r0, PAYLOAD); out({ body: r0.textContent });
    """, payload(accounts=[], providers=[
        {"provider": p, "status": "na", "detail": None}
        for p in ("anthropic", "openai", "moonshot")]))
    never = run_js("""
      const r0 = root(); anDrawAccounts(r0, PAYLOAD); out({ body: r0.textContent });
    """, payload(accounts=[], providers=[]))
    assert "No harness credentials found" in all_na["body"]
    assert "No probe has run yet" in never["body"]
    assert all_na["body"] != never["body"]


def test_unauth_keeps_last_known_values_muted_with_their_age():
    """Expiry is reported, not repaired: the numbers stay, the emphasis goes,
    and the age says how old they are."""
    r = run_js("""
      const r0 = root(); anDrawAccounts(r0, PAYLOAD);
      const card = byClass(r0, "an-acct")[0];
      out({ muted: cls(card).includes("an-muted"), pct: texts(byClass(card, "an-win-pct")),
            body: card.textContent });
    """, payload(
        accounts=[account(windows=[window(pct=61.0, captured_at=iso(hours=-3))])],
        providers=[{"provider": "anthropic", "status": "unauth", "detail": "expired"}]))
    assert r["muted"] is True
    assert r["pct"] == ["61%"]
    assert "signed out — last known" in r["body"]
    assert "snapshot 3h ago" in r["body"]


# ── the non-current account ──────────────────────────────────────────────────

def test_non_current_account_is_muted_and_cannot_be_refreshed():
    """Its token is gone, so the probe route cannot re-read it. The button is
    disabled AND carries the reason — a disabled control with no explanation is
    its own small lie."""
    r = run_js("""
      const r0 = root(); anDrawAccounts(r0, PAYLOAD);
      const cards = byClass(r0, "an-acct");
      const btn = (c) => buttons(c).filter((b) => cls(b).includes("act"))[0];
      out({ order: byClass(r0, "an-acct-label").map((n) => n.textContent),
            muted: cards.map((c) => cls(c).includes("an-muted")),
            disabled: cards.map((c) => btn(c).disabled),
            titles: cards.map((c) => btn(c).title),
            notes: cards.map((c) => c.textContent.includes("not signed in")) });
    """, payload(
        # The current account's label sorts LAST alphabetically on purpose: with
        # labels that happen to sort into API order, "the UI preserves the API's
        # current-first order" is asserted by a fixture that cannot tell the
        # difference. Caught by mutation M20, which was inert until this changed.
        accounts=[account(account_pk=1, account_label="zed@person.com", is_current=1),
                  account(account_pk=2, account_ref="ref-old", account_label="abe@person.com",
                          is_current=0, last_seen=iso(days=-2))],
        providers=[{"provider": "anthropic", "status": "ok", "detail": None}]))
    assert r["order"] == ["zed@person.com", "abe@person.com"]  # API order preserved
    assert r["muted"] == [False, True]
    assert r["disabled"] == [False, True]
    assert r["titles"][0] == "" and "cannot be re-probed" in r["titles"][1]
    assert r["notes"] == [False, True]


def test_non_current_codex_is_disabled_too_because_no_rollout_refresh_exists():
    """Declared deviation, pinned so it cannot drift silently either way. The
    spec grants Codex an exception — a non-current OpenAI account refreshed from
    its rollout files — but no unit implements a rollout reader, so the only
    action available is the global re-probe, which provably cannot change this
    card. Enabling it would ship a button that lies. If a rollout path ever
    lands, this test is the thing that must be changed on purpose."""
    r = run_js("""
      const r0 = root(); anDrawAccounts(r0, PAYLOAD);
      const card = byClass(r0, "an-acct")[0];
      const btn = buttons(card).filter((b) => cls(b).includes("act"))[0];
      out({ disabled: btn.disabled, title: btn.title });
    """, payload(
        accounts=[account(provider="openai", is_current=0, account_label="old@codex.com",
                          last_seen=iso(days=-1))],
        providers=[{"provider": "openai", "status": "ok", "detail": None}]))
    assert r["disabled"] is True
    assert "cannot be re-probed" in r["title"]


def test_refresh_forces_a_probe_and_redraws_from_its_response():
    r = run_js("""
      const r0 = root(); anDrawAccounts(r0, PAYLOAD);
      const card = byClass(r0, "an-acct")[0];
      const btn = buttons(card).filter((b) => cls(b).includes("act"))[0];
      await btn.onclick();
      out({ calls: calls.map((c) => c.path + " " + c.method),
            cards: byClass(r0, "an-acct").length, toasts });
    """)
    assert r["calls"] == ["/analytics/accounts/probe POST"]
    assert r["cards"] == 1 and r["toasts"] == []


def test_a_failed_refresh_reports_and_re_enables_rather_than_wedging():
    r = run_js("""
      const r0 = root(); anDrawAccounts(r0, PAYLOAD);
      const btn = buttons(r0).filter((b) => b.textContent.startsWith("refresh all"))[0];
      apiFail = "boom";
      await btn.onclick();
      out({ toasts, disabled: btn.disabled, label: btn.textContent });
    """)
    assert r["toasts"] == ["probe error: boom"]
    assert r["disabled"] is False
    assert r["label"] == "refresh all ⟳"


# ── the seven-day window ─────────────────────────────────────────────────────

def test_every_account_stale_renders_the_current_one_with_a_note():
    """Never an empty page. The API exempts is_current from the filter so the row
    arrives; the UI's job is to say why the page is thin instead of letting it
    read as the whole truth about the week."""
    r = run_js("""
      const r0 = root(); anDrawAccounts(r0, PAYLOAD);
      out({ note: texts(byClass(r0, "an-note")), cards: byClass(r0, "an-acct").length });
    """, payload(accounts=[account(last_seen=iso(days=-30))]))
    assert r["cards"] == 1
    assert r["note"] and "showing the current account only" in r["note"][0]
    assert "7 days" in r["note"][0]


def test_a_fresh_account_carries_no_stale_note():
    """The other direction — a note on every page is a note nobody reads."""
    r = run_js("""
      const r0 = root(); anDrawAccounts(r0, PAYLOAD);
      out({ note: byClass(r0, "an-note").length });
    """, payload(accounts=[account(last_seen=iso(days=-30)), account(
        account_pk=2, account_ref="ref-2", last_seen=iso(hours=-1))]))
    assert r["note"] == 0


# ── window rows ──────────────────────────────────────────────────────────────

def test_unrecognized_window_renders_under_its_raw_kind_and_duration():
    """The probe stores a window it could not map rather than dropping it,
    precisely so the panel can show it. Dropping it here would waste that."""
    r = run_js("""
      const r0 = root(); anDrawAccounts(r0, PAYLOAD);
      out({ names: texts(byClass(r0, "an-win-name")), rows: byClass(r0, "an-win").length });
    """, payload(accounts=[account(windows=[
        window(kind="weekly", pct=40.0),
        window(kind="1209600 SECOND", pct=12.0, scope="1209600s"),
        window(kind="weekly_scoped", pct=5.0, scope="claude-opus-5"),
        window(kind="session", pct=88.0),
        window(kind="five_hour", pct=3.0)])]))
    # Known kinds in their own order, unrecognized last — never dropped.
    assert r["names"] == ["Session", "5-hour", "Weekly", "Weekly · scoped · claude-opus-5",
                          "1209600 SECOND · 1209600s"]
    assert r["rows"] == 5


def test_reset_renders_as_a_countdown_and_never_as_negative_time():
    r = run_js("""
      const r0 = root(); anDrawAccounts(r0, PAYLOAD);
      out({ body: r0.textContent });
    """, payload(accounts=[account(windows=[
        window(kind="session", pct=1.0, resets_at=iso(hours=2, minutes=30)),
        window(kind="weekly", pct=2.0, resets_at=iso(hours=-5)),
        window(kind="short", pct=3.0, resets_at=None)])]))
    assert "resets in 2h 29m" in r["body"] or "resets in 2h 30m" in r["body"]
    assert "resets due" in r["body"]
    assert "-" not in r["body"].split("resets")[2].split("status")[0]


def test_an_account_with_no_windows_says_so_rather_than_rendering_blank():
    r = run_js("""
      const r0 = root(); anDrawAccounts(r0, PAYLOAD);
      out({ body: r0.textContent, wins: byClass(r0, "an-win").length });
    """, payload(accounts=[account(windows=[])]))
    assert r["wins"] == 0
    assert "no windows reported" in r["body"]


def test_providers_render_in_the_dispatchers_order_with_their_accounts():
    r = run_js("""
      const r0 = root(); anDrawAccounts(r0, PAYLOAD);
      out({ groups: byClass(r0, "an-prov-head").map((n) => n.textContent),
            labels: byClass(r0, "an-acct-label").map((n) => n.textContent) });
    """, payload(
        accounts=[account(provider="anthropic", account_pk=1, account_label="a@x.com"),
                  account(provider="openai", account_pk=2, account_ref="r2", account_label="o@x.com"),
                  account(provider="moonshot", account_pk=3, account_ref="r3", account_label="usr-3")],
        providers=[{"provider": p, "status": "ok", "detail": None}
                   for p in ("anthropic", "openai", "moonshot")]))
    assert r["groups"] == ["Claudeanthropic", "Codexopenai", "Kimimoonshot"]
    assert r["labels"] == ["a@x.com", "o@x.com", "usr-3"]


def test_the_full_account_label_is_rendered_unredacted():
    """Decision #67: the label is stored and shown in full. Two accounts sharing
    a local part are indistinguishable truncated, which is the one case this
    panel exists to show."""
    r = run_js("""
      const r0 = root(); anDrawAccounts(r0, PAYLOAD);
      out({ labels: byClass(r0, "an-acct-label").map((n) => n.textContent) });
    """, payload(accounts=[
        account(account_pk=1, account_label="jed@gmail.com"),
        account(account_pk=2, account_ref="r2", account_label="jed@work.com", is_current=0)]))
    assert r["labels"] == ["jed@gmail.com", "jed@work.com"]


# ── styles the thresholds depend on ──────────────────────────────────────────

def test_threshold_classes_have_distinct_styling():
    """The colour rule is only real if the classes differ visibly. Asserted here
    because the JS tests can only see class names."""
    for sel in (".an-win-pct.amber", ".an-win-pct.red",
                ".an-meter-fill.amber", ".an-meter-fill.red",
                ".an-acct.an-muted", ".an-acct-foot .act:disabled"):
        assert sel in CSS, f"{sel} is not styled"
