"""Provider Quota UI (spec #57, superseding #49): toggle, router, cards, thresholds.

Driven through node against the REAL app.js regions with a minimal DOM — the
same idiom as the interface UI contract suite — rather than grepping the source
for strings. A string assertion passes against a function nobody calls; these
render the section and read what came out.

The unit's value rests on three properties, and all three are about not lying,
so all three are asked in both directions:

  * COLOUR IS COMPUTED FROM used_percent ALONE. Asserting "96% is red" leaves a
    reading of a provider's own severity field entirely free, so every colour
    test also pins a card whose provider status disagrees with its percent —
    an `error` provider at 22% must still render normal, and an `ok` provider at
    96% must still render red.
  * A MISSING NUMBER IS NEVER DRAWN AS ZERO. Asserting "n/a is shown" leaves a
    0%-wide bar free to be drawn under the label, which is the failure mode the
    spec names: a meter reads as measured. So every n/a test asserts the text
    AND the absence of a fill element.
  * THE CARD MAKES NO CLAIM ABOUT THE OPERATOR'S SESSION. This is what spec #57
    replaced the per-account panel WITH, so it is asserted as a property of the
    rendered output rather than trusted to the absence of the old code: no
    label, no account id, no plan, no sign-in language, and — the mirror leg
    that matters most — a degraded probe still shows its last-known figures
    WITH their age, rather than blanking the card or passing them off as fresh.

WHAT THIS SUITE STOPPED PINNING, AND WHY IT IS NOT A GAP. The 7-day activity
window, the is_current exemption, muted rendering, the disabled refresh button,
the per-card refresh button and the full-email label all had tests here and all
are gone. They were not weakened — the mechanisms were REMOVED (decision #75,
migration 0097), and a test for a mechanism that no longer exists is the
"comment describing a mechanism that no longer exists" defect wearing a
different hat. What replaces them is the property they were each approximating:
every provider renders a card, no card says anything about who is signed in,
and the section has exactly one refresh control because one probe run is all
the route can do.
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
SHELL_STATE = APP[APP.index("let selectedShell ="):
                  APP.index("// Rough token estimator")]
QUOTA = APP[APP.index("// ── Provider Quota"):APP.index("// ── Interface tab")]
_ROUTER_AT = APP.index("function routeFromHash()")
ROUTER = APP[_ROUTER_AT:
             APP.index('document.querySelectorAll("nav button").forEach', _ROUTER_AT)]

HAS_NODE = shutil.which("node") is not None
pytestmark = pytest.mark.skipif(not HAS_NODE, reason="node is required")


def iso(**delta) -> str:
    return (datetime.now(timezone.utc) + timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def window(kind="weekly", pct=10.0, **over) -> dict:
    """One harness_quota_window row as GET /api/analytics/quota sends it."""
    row = {"window_pk": 1, "window_kind": kind, "scope": None, "used_percent": pct,
           "used": None, "limit_value": None, "resets_at": iso(hours=3),
           "captured_at": iso(minutes=-2), "status": "ok", "probe_version": "1"}
    row.update(over)
    return row


_UNSET = object()


def provider(name="anthropic", status="ok", detail=None, captured_at=_UNSET,
             windows=None) -> dict:
    """One entry of the response's `providers` array — the whole response shape
    now. There is no accounts array and no identity of any kind in it.

    `captured_at` takes a SENTINEL default rather than None, because a null
    captured_at is the never-probed card — a real and distinct state the tests
    below have to be able to ask for."""
    return {"provider": name, "status": status, "detail": detail,
            "captured_at": iso(minutes=-2) if captured_at is _UNSET else captured_at,
            "windows": [window()] if windows is None else windows}


def payload(providers=None, **over) -> dict:
    d = {"providers": [provider()] if providers is None else providers,
         "ttl_seconds": 60, "probed": True, "notes": []}
    d.update(over)
    return d


def all_three(**over) -> list:
    """The real shape of a live response: every provider always present."""
    return [provider(name, **over) for name in ("anthropic", "openai", "moonshot")]


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
// The token section is stubbed so "arriving at quota never sweeps" is
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
              + EL + HELPERS + SHELL_STATE + HARNESS + QUOTA + ROUTER
              + "\n(async () => {\n" + body + "\n})().catch((e) => {"
              " console.error(e.stack || e); process.exit(1); });\n")
    proc = subprocess.run(["node", "-e", script], text=True, capture_output=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ── router + toggle ──────────────────────────────────────────────────────────

def test_quota_hash_routes_to_the_analytics_tab_as_a_sub_view():
    """#analytics-quota is a URL, and `analytics` stays the active nav tab —
    the #roadmap / #roadmap-flow convention. Both halves matter: routing to a
    tab of its own would lose the toggle, and not routing at all would fall
    through to the default view."""
    r = run_js("""
      const seen = [];
      for (const hash of ["#analytics-quota", "#analytics", "#analytics-quota"]) {
        location.hash = hash; routeFromHash();
        seen.push({ view: anView, tab: shown[shown.length - 1] });
      }
      location.hash = "#roadmap-flow"; routeFromHash();
      out({ seen, roadmapTab: shown[shown.length - 1], roadmapView, viewAfter: anView });
    """)
    assert r["seen"] == [{"view": "quota", "tab": "analytics"},
                         {"view": "tokens", "tab": "analytics"},
                         {"view": "quota", "tab": "analytics"}]
    # The new branch must not swallow the roadmap sub-view it was modelled on.
    assert r["roadmapTab"] == "roadmap" and r["roadmapView"] == "flow"
    assert r["viewAfter"] == "quota"


def test_the_old_accounts_hash_has_no_alias_and_falls_through():
    """R3: NO COMPATIBILITY ALIAS. #analytics-accounts named a per-account panel
    this spec deletes, and aliasing it would itself be a route naming a
    mechanism that no longer exists — the precise defect class being removed.

    It must not silently resolve to the quota view; it falls through to Token
    Analytics like any other unrecognized analytics hash."""
    r = run_js("""
      location.hash = "#analytics-accounts"; routeFromHash();
      out({ view: anView, tab: shown[shown.length - 1] });
    """)
    assert r["view"] == "tokens"
    assert r["tab"] == "analytics"


def test_unknown_hash_still_falls_through_to_the_default_view():
    r = run_js("""
      location.hash = "#nonsense"; routeFromHash();
      out({ tab: shown[shown.length - 1] });
    """)
    assert r["tab"] == "shells"


def test_toggle_navigates_by_hash_so_the_sub_view_is_deep_linkable():
    """Clicking must set the hash, not re-render in place: a toggle that
    re-renders directly leaves the URL behind and the section stops surviving a
    reload — which is one of the spec's verification checkboxes, and it stands
    against the NEW hash."""
    r = run_js("""
      const a = root(); anView = "tokens"; await renderAnalytics(a);
      const chips = buttons(a).filter((b) => cls(b).includes("chip"));
      const labels = texts(chips);
      const active = texts(chips.filter((b) => cls(b).includes("on")));
      chips[1].onclick();
      const afterQuota = location.hash;
      const b = root(); anView = "quota"; await renderAnalytics(b);
      const chips2 = buttons(b).filter((x) => cls(x).includes("chip"));
      const active2 = texts(chips2.filter((x) => cls(x).includes("on")));
      chips2[0].onclick();
      out({ labels, active, active2, afterQuota, afterTokens: location.hash });
    """)
    assert r["labels"] == ["Token Analytics", "Provider Quota"]
    assert r["active"] == ["Token Analytics"] and r["active2"] == ["Provider Quota"]
    assert r["afterQuota"] == "analytics-quota"
    assert r["afterTokens"] == "analytics"


def test_each_section_fires_only_its_own_work_on_arrival():
    """The token sweep and the quota probe both fire on entry. Arriving at one
    must not run the other: reading token spend would otherwise cost three
    third-party calls, and the probe is specified to fire ONLY here."""
    r = run_js("""
      anView = "quota"; await renderAnalytics(root());
      const onQuota = { calls: calls.map((c) => c.path + " " + c.method), token: tokenSectionRuns };
      calls = []; tokenSectionRuns = 0;
      anView = "tokens"; await renderAnalytics(root());
      out({ onQuota, onTokens: { calls: calls.map((c) => c.path + " " + c.method),
                                 token: tokenSectionRuns } });
    """)
    assert r["onQuota"] == {"calls": ["/analytics/quota GET"], "token": 0}
    assert r["onTokens"] == {"calls": ["/analytics/sweep POST"], "token": 1}


def test_arrival_probes_once_and_never_forces_the_ttl():
    """GET is the arrival probe — the route decides for itself whether the TTL
    has aged out. A client that also POSTs would defeat the TTL outright and
    make "toggling twice inside a minute performs one probe" false."""
    r = run_js("""
      await anQuotaSection(root());
      out({ calls: calls.map((c) => c.path + " " + c.method) });
    """)
    assert r["calls"] == ["/analytics/quota GET"]


def test_section_reports_a_failed_read_instead_of_rendering_an_empty_panel():
    r = run_js("""
      apiFail = "HTTP 500";
      const r0 = root(); await anQuotaSection(r0);
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
      anDrawQuota(r0, PAYLOAD);
      const pctNode = byClass(r0, "an-win-pct")[0];
      const fill = byClass(r0, "an-meter-fill")[0];
      out({ pctClasses: cls(pctNode), fillClasses: cls(fill), width: fill.style.width,
            text: pctNode.textContent });
    """, payload([provider(windows=[window(pct=pct)])]))
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
    data = payload([
        provider("openai", status="error", detail="HTTP 429",
                 windows=[window(pct=22.0)]),
        provider("anthropic", status="ok", windows=[window(pct=96.0)])])
    r = run_js("""
      const r0 = root(); anDrawQuota(r0, PAYLOAD);
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
      const r0 = root(); anDrawQuota(r0, PAYLOAD);
      const pctNode = byClass(r0, "an-win-pct")[0];
      out({ text: texts(byClass(r0, "an-win-pct")), fills: byClass(r0, "an-meter-fill").length,
            meters: byClass(r0, "an-meter").length,
            tone: cls(pctNode).filter((c) => c !== "an-win-pct") });
    """, payload([provider(windows=[window(pct=None)])]))
    assert r["text"] == ["n/a"]
    assert r["fills"] == 0
    assert r["tone"] == []
    assert r["meters"] == 1  # the empty track still renders — the row is not dropped


def test_moonshot_all_null_row_renders_as_na_rather_than_zero_or_nothing():
    """Moonshot `usage: {}` emits one all-null weekly row where openai emits
    none — asymmetric but truthful (U2's reviewer). It must neither be filtered
    out nor drawn as 0%."""
    r = run_js("""
      const r0 = root(); anDrawQuota(r0, PAYLOAD);
      out({ rows: byClass(r0, "an-win").length, pct: texts(byClass(r0, "an-win-pct")),
            fills: byClass(r0, "an-meter-fill").length, name: texts(byClass(r0, "an-win-name")) });
    """, payload([provider("moonshot", windows=[
        window(kind="weekly", pct=None, used=None, limit_value=None,
               resets_at=None)])]))
    assert r["rows"] == 1 and r["pct"] == ["n/a"] and r["fills"] == 0
    assert r["name"] == ["Weekly"]


def test_zero_limit_renders_na_not_a_division():
    """limit_value = 0 arrives with used_percent NULL (the probe never
    back-computes). The counts still show, so the operator sees WHY it is n/a."""
    r = run_js("""
      const r0 = root(); anDrawQuota(r0, PAYLOAD);
      out({ pct: texts(byClass(r0, "an-win-pct")), fills: byClass(r0, "an-meter-fill").length,
            body: r0.textContent });
    """, payload([provider(windows=[window(pct=None, used=0, limit_value=0)])]))
    assert r["pct"] == ["n/a"] and r["fills"] == 0
    assert "0 / 0" in r["body"]


def test_a_real_zero_percent_still_draws_a_measured_zero():
    """The mirror of the n/a rule, so the fix cannot be "never draw a bar": a
    provider that genuinely measured 0% renders 0%, not n/a.

    This is the UI half of moonshot's untouched five-hour window, which really
    does read 0 used against a limit of 100 — a measured zero, not a failure."""
    r = run_js("""
      const r0 = root(); anDrawQuota(r0, PAYLOAD);
      out({ pct: texts(byClass(r0, "an-win-pct")), width: byClass(r0, "an-meter-fill")[0].style.width });
    """, payload([provider(windows=[window(pct=0.0, used=0, limit_value=500)])]))
    assert r["pct"] == ["0%"] and r["width"] == "0%"


# ── one card per provider, never hidden ──────────────────────────────────────

def test_every_provider_renders_a_card_including_the_unconfigured_ones():
    """Nothing is filtered out, and that is the rule the old panel broke.

    Hiding a card is how a panel stops lying and starts saying nothing: the
    operator cannot tell "not configured" from "not readable" from a card that
    is not there. Asserted with two of three providers unusable, so a section
    that renders only what it has data for fails here."""
    r = run_js("""
      const r0 = root(); anDrawQuota(r0, PAYLOAD);
      out({ cards: byClass(r0, "an-acct").length,
            heads: byClass(r0, "an-acct-head").map((n) => n.textContent) });
    """, payload([
        provider("anthropic", windows=[window(pct=40.0)]),
        provider("openai", status="na", captured_at=None, windows=[]),
        provider("moonshot", status="unauth", captured_at=None, windows=[])]))
    assert r["cards"] == 3
    assert [h.split("anthropic")[0].split("openai")[0].split("moonshot")[0]
            for h in r["heads"]] == ["Claude", "Codex", "Kimi"]


def test_never_probed_and_idle_say_different_things():
    """Both render zero windows, and collapsing them loses the operator's next
    move. A provider that has never returned anything has nothing to show; one
    that returned an intact envelope carrying zero windows is genuinely idle,
    and that IS its reading.

    THE SIGNAL IS THE STATUS, and these three payloads are ones the API emits.
    The card used to branch on captured_at, which the API derives from window
    rows — so a card with no windows never had one and the idle sentence could
    not be reached by any real response. This test pinned that branch with a
    hand-authored payload the producer cannot emit, which is the defect class
    this whole unit exists to end. `ok` with zero windows is the producible
    idle reading (pinned at the API layer in the sibling suite); no status is
    the never-probed one, and a failed probe that has never landed a reading
    has nothing to show either."""
    r = run_js("""
      const r0 = root(); anDrawQuota(r0, PAYLOAD);
      out({ bodies: byClass(r0, "an-acct").map((c) => c.textContent) });
    """, payload([provider("anthropic", status=None, captured_at=None, windows=[]),
                  provider("openai", status="ok", captured_at=None, windows=[]),
                  provider("moonshot", status="na", captured_at=None, windows=[])]))
    never, idle, unconfigured = r["bodies"]
    assert "no reading yet" in never
    assert "no windows reported" in idle
    assert "no reading yet" in unconfigured
    assert "no windows reported" not in never + unconfigured
    assert never != idle


def test_no_card_makes_any_claim_about_the_operator():
    """THE PROPERTY THIS WHOLE SPEC EXISTS FOR, asserted on rendered output.

    Both defects that shipped were false CLAIMS, not wrong numbers: a 403
    rendered "signed out — last known" while the operator was actively using
    Codex (#196), and a lapsed 15-minute Kimi token rendered "no account
    identified" (#197). The response is fed identity-shaped fields it must
    ignore — a positive control proving the sweep would notice them."""
    leaky = provider("anthropic", status="unauth", detail="expired",
                     windows=[window(pct=61.0)])
    leaky["account_label"] = "operator@example.com"
    leaky["plan"] = "max"
    leaky["account_ref"] = "uuid-secret"
    r = run_js("""
      const r0 = root(); anDrawQuota(r0, PAYLOAD);
      out({ body: r0.textContent });
    """, payload([leaky]))
    body = r["body"]
    for claim in ("signed out", "not signed in", "no account identified",
                  "operator@example.com", "@", "uuid-secret", "max"):
        assert claim not in body, f"the card claimed {claim!r}"
    # ...and the reading still rendered, or the sweep proves nothing.
    assert "61%" in body


def test_a_degraded_probe_keeps_its_figures_and_stamps_them_with_their_age():
    """THE EMPTY-STATE RULE WITH BOTH MIRROR LEGS. A lapsed Kimi token is the
    common case, not an error, and the operator's most useful information in
    that moment is where they stood 20 minutes ago.

    Leg 1 — it must not BLANK the card: the figures are still there.
    Leg 2 — it must not present them AS FRESH: the age is rendered from the
    reading's own captured_at, so a three-hour-old reading says so."""
    r = run_js("""
      const r0 = root(); anDrawQuota(r0, PAYLOAD);
      const card = byClass(r0, "an-acct")[0];
      out({ pct: texts(byClass(card, "an-win-pct")), body: card.textContent });
    """, payload([provider("anthropic", status="unauth", detail="expired",
                           captured_at=iso(hours=-3),
                           windows=[window(pct=61.0)])]))
    assert r["pct"] == ["61%"]                      # leg 1: not blanked
    assert "as of 3h ago" in r["body"]              # leg 2: not passed off as fresh
    assert "token not usable" in r["body"]          # the probe's state, not the operator's


def test_the_age_is_never_omitted_when_there_is_a_reading():
    """The age is the only thing keeping a stale card honest, so its presence is
    pinned for a FRESH card too — a section that rendered the age only when it
    judged a reading old would be deciding what counts as stale, which is the
    operator's call."""
    r = run_js("""
      const r0 = root(); anDrawQuota(r0, PAYLOAD);
      out({ fresh: byClass(r0, "an-acct")[0].textContent,
            none: byClass(r0, "an-acct")[1].textContent });
    """, payload([provider("anthropic", captured_at=iso(minutes=-2)),
                  provider("openai", captured_at=None, windows=[])]))
    assert "as of" in r["fresh"]
    # ...but a card with no reading has no age to state, and must not invent one.
    assert "as of" not in r["none"]


def test_one_refresh_control_for_the_section_and_none_on_the_cards():
    """ONE PROBE RUN IS ALL THERE IS. Each card used to carry its own
    "refresh ⟳" that POSTed the same route and re-probed all three providers —
    a label under-describing what the button does, three times over.

    Per-card refresh made sense under the ACCOUNT model, where cards differed
    in whether they could be refreshed at all. Provider cards do not differ
    that way, so the control belongs to the section, and it is never disabled
    by a judgement about whether a probe can succeed: the old panel made that
    judgement from the registry's idea of who was signed in, and was wrong in
    exactly the case the operator most wants it — a lapsed Kimi token that a
    re-probe fixes the moment they boot the harness. Both statuses below are
    cases the old panel would have disabled."""
    r = run_js("""
      const r0 = root(); anDrawQuota(r0, PAYLOAD);
      const bar = byClass(r0, "an-acct-bar")[0];
      out({ all: buttons(r0).map((b) => b.textContent),
            onCards: byClass(r0, "an-acct").map((c) => buttons(c).length),
            disabled: buttons(bar)[0].disabled,
            wired: buttons(bar)[0].onclick !== null });
    """, payload([provider("anthropic", status="unauth", captured_at=iso(hours=-3)),
                  provider("moonshot", status="na", captured_at=None, windows=[])]))
    assert r["all"] == ["refresh all ⟳"], "the section has exactly one control"
    assert r["onCards"] == [0, 0]
    assert r["disabled"] is False and r["wired"] is True


def test_each_card_links_out_to_its_own_providers_usage_page():
    """The link is part of the design, not a convenience: the ruling that
    retired account identity rests on the provider's own page being one click
    away for anything this panel deliberately stops showing."""
    r = run_js("""
      const r0 = root(); anDrawQuota(r0, PAYLOAD);
      out({ hrefs: all(r0, (n) => n.tagName === "a").map((n) => n.href) });
    """, payload(all_three()))
    assert r["hrefs"] == ["https://claude.ai/settings/usage",
                          "https://chatgpt.com/codex/settings/usage",
                          "https://www.kimi.com/code"]


def test_refresh_forces_a_probe_and_redraws_from_its_response():
    r = run_js("""
      const r0 = root(); anDrawQuota(r0, PAYLOAD);
      const btn = buttons(r0).filter((b) => b.textContent.startsWith("refresh all"))[0];
      await btn.onclick();
      out({ calls: calls.map((c) => c.path + " " + c.method),
            cards: byClass(r0, "an-acct").length, toasts });
    """)
    assert r["calls"] == ["/analytics/quota/probe POST"]
    assert r["cards"] == 1 and r["toasts"] == []


def test_a_failed_refresh_reports_and_re_enables_rather_than_wedging():
    r = run_js("""
      const r0 = root(); anDrawQuota(r0, PAYLOAD);
      const btn = buttons(r0).filter((b) => b.textContent.startsWith("refresh all"))[0];
      apiFail = "boom";
      await btn.onclick();
      out({ toasts, disabled: btn.disabled, label: btn.textContent });
    """)
    assert r["toasts"] == ["probe error: boom"]
    assert r["disabled"] is False
    assert r["label"] == "refresh all ⟳"


def test_an_empty_providers_array_says_so_rather_than_rendering_nothing():
    """Only reachable if the API returns no entries at all — which it cannot,
    since it builds from the PROVIDERS constant. Kept because "cannot happen"
    is a claim about another layer, and a blank panel would be the worst way to
    discover that claim had stopped being true."""
    r = run_js("""
      const r0 = root(); anDrawQuota(r0, PAYLOAD); out({ body: r0.textContent });
    """, payload([]))
    assert "No probe has run yet" in r["body"]


# ── window rows ──────────────────────────────────────────────────────────────

def test_unrecognized_window_renders_under_its_raw_kind_and_duration():
    """The probe stores a window it could not map rather than dropping it,
    precisely so the panel can show it. Dropping it here would waste that."""
    r = run_js("""
      const r0 = root(); anDrawQuota(r0, PAYLOAD);
      out({ names: texts(byClass(r0, "an-win-name")), rows: byClass(r0, "an-win").length });
    """, payload([provider(windows=[
        window(kind="weekly", pct=40.0),
        window(kind="unknown", pct=12.0, scope="1209600s"),
        window(kind="weekly_scoped", pct=5.0, scope="claude-opus-5"),
        window(kind="session", pct=88.0),
        window(kind="five_hour", pct=3.0)])]))
    # Known kinds in their own order, unrecognized last — never dropped.
    assert r["names"] == ["Session", "5-hour", "Weekly", "Weekly · scoped · claude-opus-5",
                          "unknown · 1209600s"]
    assert r["rows"] == 5


def test_no_container_repr_ever_reaches_a_rendered_card():
    """The UI end of the container-repr class. The probe is what formats a raw
    duration, but `scope` is passed through to the card verbatim, so the rule
    is pinned on BOTH sides of that seam — a defect that reached the database
    reached the card by the same route."""
    r = run_js("""
      const r0 = root(); anDrawQuota(r0, PAYLOAD);
      out({ names: texts(byClass(r0, "an-win-name")), body: r0.textContent });
    """, payload([provider(windows=[
        window(kind="unknown", pct=12.0, scope="unrecognized window")])]))
    assert r["names"] == ["unknown · unrecognized window"]
    for bad in ("{", "}", "[", "]", "'duration'", "timeUnit"):
        assert bad not in r["body"], f"a container repr reached the card: {bad}"


def test_reset_renders_as_a_countdown_and_never_as_negative_time():
    r = run_js("""
      const r0 = root(); anDrawQuota(r0, PAYLOAD);
      out({ body: r0.textContent });
    """, payload([provider(windows=[
        window(kind="session", pct=1.0, resets_at=iso(hours=2, minutes=30)),
        window(kind="weekly", pct=2.0, resets_at=iso(hours=-5)),
        window(kind="short", pct=3.0, resets_at=None)])]))
    assert "resets in 2h 29m" in r["body"] or "resets in 2h 30m" in r["body"]
    assert "resets due" in r["body"]
    assert "-" not in r["body"].split("resets")[2].split("status")[0]


def test_providers_render_in_the_dispatchers_order():
    r = run_js("""
      const r0 = root(); anDrawQuota(r0, PAYLOAD);
      out({ pills: texts(byClass(r0, "pill")) });
    """, payload(all_three()))
    assert r["pills"] == ["anthropic", "openai", "moonshot"]


# ── styles the thresholds depend on ──────────────────────────────────────────

def test_threshold_classes_have_distinct_styling():
    """The colour rule is only real if the classes differ visibly. Asserted here
    because the JS tests can only see class names."""
    for sel in (".an-win-pct.amber", ".an-win-pct.red",
                ".an-meter-fill.amber", ".an-meter-fill.red"):
        assert sel in CSS, f"{sel} is not styled"


def test_the_muted_card_style_is_gone_not_merely_unused():
    """Removed, not left inert. Dimming marked a card as "not the current
    account", a distinction a provider-level panel does not have — and a rule
    left in the stylesheet is the CSS form of a comment describing a mechanism
    that no longer exists."""
    assert "an-muted" not in CSS
