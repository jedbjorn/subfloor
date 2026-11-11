"""GUI contracts for operator sign-in (spec 271).

The sign-in page, the vendored QR encoder, session loss in the main app
(every request helper, the raw fetch sites and the event stream), the drawer's
Account section, and the engine controls with and without a session. String
contracts over the served files plus node runs of the real functions against a
fake DOM and a stub review server and host authority.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_gui_drawer_models import (  # noqa: E402
    API, APP, DRAWER_BLOCK, ENGINE, ENGINE_HARNESS, FAKE_DOM, LITERAL, STYLE, between,
    needs_node, run_js,
)

UI = ROOT / ".super-coder" / "ui"
SIGNIN_HTML = (UI / "signin.html").read_text()
SIGNIN_JS = (UI / "signin.js").read_text()
SIGNIN_CSS = (UI / "signin.css").read_text()
QRCODE_JS = (UI / "vendor" / "qrcode.js").read_text()

SESSION = between("// ── Session loss (spec #271)", "function requestKey()")
ACCOUNT = DRAWER_BLOCK[DRAWER_BLOCK.index("// Account (spec #271)"):
                       DRAWER_BLOCK.index("for (const [id, render] of [")]
# The static paths the review server serves without a session.
OPEN_STATIC = {"/signin.js", "/signin.css", "/theme.js", "/style.css", "/vendor/qrcode.js"}
SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
OTPAUTH = ("otpauth://totp/Subfloor:operator?secret=" + SECRET
           + "&issuer=Subfloor&algorithm=SHA1&digits=6&period=30")
HOME = "http://localhost:8837/?a=1#interface/DEV1"   # the harness page's validated next
FAILED = ("Sign-in failed. Check the username, password and code, "
          "and that this device's clock is right.")


def function_source(name: str) -> str:
    """One top-level function of app.js, by name."""
    match = re.search(rf"^(?:async )?function {re.escape(name)}\(", APP, re.M)
    assert match, name
    return APP[match.start():APP.index("\n}\n", match.start()) + 3]


# ── Served files ──────────────────────────────────────────────────────────────

def test_sign_in_page_loads_only_open_static_files_and_no_inline_code():
    assert re.findall(r'(?:src|href)="([^"]+)"', SIGNIN_HTML) == [
        "/theme.js", "/style.css", "/signin.css", "/vendor/qrcode.js", "/signin.js"]
    assert set(re.findall(r'(?:src|href)="([^"]+)"', SIGNIN_HTML)) == OPEN_STATIC
    # The CSP allows no inline script, handler or style.
    assert re.findall(r"<script(?![^>]*\bsrc=)", SIGNIN_HTML) == []
    assert not re.search(r"\son\w+\s*=", SIGNIN_HTML)
    assert "style=" not in SIGNIN_HTML and "<style" not in SIGNIN_HTML
    # The theme is applied before first paint, as in the app.
    assert SIGNIN_HTML.index('src="/theme.js"') < SIGNIN_HTML.index('href="/style.css"')
    assert '<meta name="viewport" content="width=device-width, initial-scale=1" />' in SIGNIN_HTML
    assert 'id="signin-body"' in SIGNIN_HTML


def test_sign_in_script_stores_nothing_and_sends_no_bearer():
    for forbidden in ("localStorage", "sessionStorage", "indexedDB", "document.cookie",
                      "console.", "innerHTML", "pushState", "replaceState", "Authorization",
                      "eval(", "sendBeacon"):
        assert forbidden not in SIGNIN_JS.replace("no localStorage", ""), forbidden
    assert SIGNIN_JS.count('credentials: "include"') == 1
    assert SIGNIN_JS.count("env.fetch(") == 1, "one request function for the whole page"
    for value in ("username", "current-password", "new-password", "one-time-code"):
        assert f'autocomplete: "{value}"' in SIGNIN_JS
    assert 'inputMode: "numeric"' in SIGNIN_JS
    assert 'role: "alert"' in SIGNIN_JS


def test_sign_in_styles_use_theme_tokens_only():
    assert not LITERAL.search(SIGNIN_CSS)
    declared = set(re.findall(r"(--[\w-]+):", STYLE))
    assert set(re.findall(r"var\((--[\w-]+)\)", SIGNIN_CSS)) <= declared
    assert "min(26rem, 100%)" in SIGNIN_CSS, "the card fits a phone-width screen"


def test_qr_encoder_is_vendored_with_its_source_version_and_licence():
    head = QRCODE_JS[:600]
    assert "qrcode-generator 1.4.4" in head and "MIT License" in head
    assert "https://github.com/kazuhikoarase/qrcode-generator" in head
    assert "Kazuhiko Arase" in head
    assert "http://" not in SIGNIN_JS and "https://" not in SIGNIN_JS, "nothing is fetched from a CDN"


# ── Session loss in the main app ──────────────────────────────────────────────

def test_every_request_in_the_app_goes_through_the_session_aware_fetch():
    # Derived from the source: a new raw fetch anywhere in app.js fails here.
    raw = [APP.count("\n", 0, m.start()) + 1
           for m in re.finditer(r"(?<![\w.])fetch\(", APP)]
    assert len(raw) == 1, f"raw fetch calls at lines {raw}"
    guard = function_source("sessionFetch")
    assert "const response = await fetch(url, init);" in guard
    assert "isSignInRequired(401, data)" in guard and "goToSignIn();" in guard
    for other in ("XMLHttpRequest", "sendBeacon", "new WebSocket("):
        assert other not in APP
    # Every helper and every former raw site calls it.
    for name in ("api", "chatApi", "reviewObservationApi", "engineOpsRequest"):
        assert "sessionFetch(" in function_source(name), name
    for site in ('sessionFetch("/api/scripts/" + s.key', "sessionFetch(`/api/services/${s.key}/${action}`",
                 'sessionFetch("/api/vm/validate/" + check', 'sessionFetch("/api/scripts/map"'):
        assert site in APP, site
    assert APP.count("sessionFetch(") == 10, "definition, probe and eight call sites"
    assert "sessionFetch(" in function_source("probeSession")
    # The one event stream asks about the session when it errors.
    assert APP.count("new EventSource(") == 1
    stream = function_source("chatOpenStream")
    assert 'source.onerror = () => { onState("reconnecting"); watch.errored(); };' in stream
    assert 'source.addEventListener("sign_in_required", () => watch.signedOut());' in stream
    # Stopping the stream stops its watch.
    assert function_source("chatStopStream").index("stopSessionWatch();") \
        < function_source("chatStopStream").index("chatSource.close()")


SESSION_ENV = r"""
const navigations = [];
globalThis.location = {origin: "http://127.0.0.1:8837", pathname: "/", search: "?q=1",
  hash: "#interface/DEV1/cv_1", hostname: "127.0.0.1", assign: (url) => { navigations.push(url); }};
const answer = (status, data) => {
  const response = {ok: status >= 200 && status < 300, status, statusText: "x",
    json: async () => data};
  response.clone = () => response;
  return response;
};
const settle = async () => { for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r)); };
// "pending" when the promise has neither resolved nor rejected.
const outcome = async (promise) => {
  let state = "pending";
  promise.then(() => { state = "resolved"; }, () => { state = "rejected"; });
  await settle();
  return state;
};
"""
NEXT = "/signin.html?next=%2F%3Fq%3D1%23interface%2FDEV1%2Fcv_1"


@needs_node
@pytest.mark.parametrize("helper", ["api", "chatApi", "reviewObservationApi",
                                    "engineOpsRequest", "sessionFetch"])
@pytest.mark.parametrize("body", [
    {"error": "sign_in_required"},                                   # the review server's shape
    {"error": {"code": "sign_in_required", "message": "sign in"}},   # the host authority's
])
def test_sign_in_required_sends_the_operator_to_sign_in_from_every_helper(helper, body):
    script = SESSION_ENV + API + function_source("chatApi") \
        + function_source("reviewObservationApi") + function_source("engineOpsRequest") + r"""
const HELPER = "__HELPER__";
globalThis.fetch = async () => answer(401, __BODY__);
const calls = {
  api: () => api("/shells"),
  chatApi: () => chatApi("/conversations"),
  reviewObservationApi: () => reviewObservationApi("/x"),
  engineOpsRequest: () => engineOpsRequest("http://127.0.0.1:8857", "/status"),
  sessionFetch: () => sessionFetch("/api/scripts/map", {method: "POST"}),
};
(async () => {
  const first = await outcome(calls[HELPER]());
  const second = await outcome(calls[HELPER]());   // a second failure does not navigate again
  console.log(JSON.stringify({first, second, navigations}));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = run_js(script.replace("__HELPER__", helper).replace("__BODY__", json.dumps(body)))
    # The caller is left waiting (no "offline", no error toast) and the page leaves once.
    assert result == {"first": "pending", "second": "pending", "navigations": [NEXT]}


@needs_node
def test_other_failures_are_still_reported_to_the_caller():
    script = SESSION_ENV + API + r"""
const queue = [
  answer(401, {error: "not_this"}), answer(401, {}), answer(403, {error: "sign_in_required"}),
  answer(500, {error: {code: "boom", message: "bad"}}), answer(200, {ok: 1}),
];
globalThis.fetch = async () => queue.shift();
(async () => {
  const seen = [];
  for (let i = 0; i < 5; i++)
    seen.push(await api("/x").then((data) => "ok:" + JSON.stringify(data), (e) => "error:" + e.message));
  console.log(JSON.stringify({seen, navigations}));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    assert run_js(script) == {
        "seen": ["error:not_this", "error:x", "error:sign_in_required", "error:boom: bad", 'ok:{"ok":1}'],
        "navigations": [],
    }


# A browser-like stream and clock for the session probe: timers fire only as
# the clock advances, and the fake EventSource behaves as a browser's does. A
# dropped connection raises an error and is retried by the stream itself after
# three seconds; a refused one (any HTTP error) raises one error and closes
# the stream for good. Nothing in these tests emits an error by hand.
STREAM_ENV = SESSION_ENV + r"""
let clock = 1000000;
Date.now = () => clock;
const timers = [];
globalThis.setTimeout = (fn, ms = 0) => { const timer = {at: clock + ms, fn}; timers.push(timer); return timer; };
globalThis.clearTimeout = (timer) => { const i = timers.indexOf(timer); if (i >= 0) timers.splice(i, 1); };
// Run every timer that falls due while the clock moves forward by `ms`.
const advance = async (ms) => {
  const end = clock + ms;
  for (;;) {
    await settle();
    const due = timers.filter((t) => t.at <= end).sort((x, y) => x.at - y.at)[0];
    if (!due) break;
    timers.splice(timers.indexOf(due), 1);
    clock = due.at;
    due.fn();
  }
  clock = end;
  await settle();
};
const server = {stream: "ok", session: true, reachable: true, authRoute: true, authBody: null};
const probes = [], opened = [], connections = [];
globalThis.fetch = (url) => {
  probes.push(clock);
  if (!server.reachable) return Promise.reject(new TypeError("Failed to fetch"));
  return Promise.resolve(!server.authRoute ? answer(404, {error: "not found"})
    : answer(200, server.authBody || {enrolled: true, signed_in: server.session}));
};
globalThis.EventSource = class {
  constructor(url) { this.url = url; this.readyState = 0; this.listeners = {}; opened.push(this); this.connect(); }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  close() { this.readyState = 2; }
  connect() {
    setTimeout(() => {
      if (this.readyState === 2) return;
      const outcome = !server.reachable ? "drop" : !server.session ? "refuse" : server.stream;
      connections.push(clock);
      if (outcome === "ok") { this.readyState = 1; if (this.onopen) this.onopen(); return; }
      if (outcome === "refuse") { this.readyState = 2; if (this.onerror) this.onerror(); return; }
      this.readyState = 0; if (this.onerror) this.onerror(); setTimeout(() => this.connect(), 3000);
    }, 10);
  }
  // The connection breaks under an open stream: error now, retry in three seconds.
  drop() { this.readyState = 0; if (this.onerror) this.onerror(); setTimeout(() => this.connect(), 3000); }
  emit(type, event) { this.listeners[type]({data: JSON.stringify(event)}); }
  // What the gate does to an open stream whose session ends: name it, then close.
  revoke() {
    const named = this.listeners["sign_in_required"];
    if (named) named({data: "{}"});
    this.readyState = 2;
    if (this.onerror) this.onerror();
  }
};
const handlers = {};
const listen = (type, fn) => { (handlers[type] = handlers[type] || []).push(fn); };
globalThis.document = {hidden: false, addEventListener: listen};
globalThis.window = {addEventListener: listen};
const fire = (type) => { for (const fn of handlers[type] || []) fn(); };
const wakeIfDefined = () => { if (typeof wakeSessionWatch === "function") wakeSessionWatch(); };
// The watch a stream owns: null, or how much it is holding.
const watching = () => typeof sessionWatch === "undefined" ? "n/a" : sessionWatch === null ? null
  : {timer: Boolean(sessionWatch.timer), probing: sessionWatch.probing};
let chatSource = null, chatRenderGeneration = 1;
const states = [], events = [];
const openStream = () => chatOpenStream("cv_1", 1, 7, (event) => events.push(event.sequence),
  (state) => states.push(state));
const report = (extra = {}) => console.log(JSON.stringify({
  probes: probes.map((at) => at - 1000000), navigations, urls: opened.map((s) => s.url),
  last: states[states.length - 1], pendingTimers: timers.length, ...extra}));
"""
STREAM_SOURCE = SESSION + function_source("chatStopStream") + function_source("chatOpenStream")
# The page's wake-up listeners for the stream watch, run against a fake
# document and window that record them (STREAM_ENV's `fire`).
WAKE_MARK = "// A tab coming back, or the network returning"
WAKE = APP[APP.index(WAKE_MARK):APP.index("// Close any open popover menu")] if WAKE_MARK in APP else ""


@needs_node
def test_a_session_revoked_inside_the_probe_window_still_reaches_sign_in():
    # The reviewed sequence: a transient error probes fine; the session is then
    # revoked; the stream's own retry is refused inside the five-second window
    # and the browser closes the stream. That last error must not be lost.
    script = STREAM_ENV + STREAM_SOURCE + r"""
(async () => {
  openStream();
  await advance(100);                       // connected
  opened[0].drop();                         // t=100: transient error, first probe: signed in
  await advance(1000);
  const afterDrop = {probes: probes.length, navigations: [...navigations]};
  server.session = false;                   // t=1100: revoked
  await advance(2500);                      // t=3100: the retry is refused, the stream closes
  const afterRefusal = {closed: opened[0].readyState === 2, probes: probes.length,
    navigations: [...navigations], deferred: timers.length};
  await advance(3000);                      // past the window: the deferred probe runs
  report({afterDrop, afterRefusal});
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = run_js(script)
    assert result["afterDrop"] == {"probes": 1, "navigations": []}
    assert result["afterRefusal"] == {"closed": True, "probes": 1, "navigations": [], "deferred": 1}
    assert result["probes"] == [100, 5100], "one probe, then one deferred to the end of the window"
    assert result["navigations"] == [NEXT]
    assert result["pendingTimers"] == 0


@needs_node
def test_a_refused_stream_is_reopened_once_the_session_is_confirmed():
    script = STREAM_ENV + STREAM_SOURCE + r"""
(async () => {
  openStream();
  await advance(100);
  opened[0].emit("message.accepted", {sequence: 8});
  opened[0].emit("usage", {sequence: 9});
  // The server refuses the stream (an error status) while the session holds.
  server.stream = "refuse";
  opened[0].drop();
  await advance(3100);                       // probe at t=100 (ok); the retry at t=3100 is refused
  const closed = {readyState: opened[0].readyState, streams: opened.length};
  server.stream = "ok";
  await advance(2500);                       // deferred probe at t=5100 confirms; the stream reopens
  const reopened = {streams: opened.length, state: states[states.length - 1]};
  // A stream that was replaced or stopped meanwhile is not reopened.
  server.stream = "refuse";
  opened[1].drop();
  await advance(3100);
  chatStopStream();
  await advance(20000);
  report({closed, reopened, events});
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = run_js(script)
    assert result["closed"] == {"readyState": 2, "streams": 1}
    assert result["reopened"] == {"streams": 2, "state": "connected"}
    assert result["urls"] == ["/api/conversations/cv_1/events?after=7",
                              "/api/conversations/cv_1/events?after=9"], "resumes after the last event seen"
    assert result["events"] == [8, 9]
    assert result["navigations"] == [] and result["pendingTimers"] == 0


@needs_node
def test_an_outage_holds_one_watch_and_stopping_the_stream_ends_it():
    script = STREAM_ENV + STREAM_SOURCE + r"""
(async () => {
  const out = {};
  openStream();
  await advance(100);
  // Ten minutes with the server gone: the stream errors every three seconds.
  server.reachable = false;
  opened[0].drop();
  let peakTimers = 0;
  for (let i = 0; i < 600; i++) { await advance(1000); peakTimers = Math.max(peakTimers, timers.length); }
  out.outage = {probes: probes.length, watch: watching(), peakTimers, navigations: [...navigations],
    gaps: probes.slice(1).map((at, i) => at - probes[i]),
    waiters: typeof sessionProbeWaiters === "undefined" ? 0 : sessionProbeWaiters.size};
  // The operator leaves Chats: the stream is stopped and the view re-rendered.
  chatStopStream();
  chatRenderGeneration += 1;
  const before = probes.length;
  await advance(600000);
  out.afterStop = {extraProbes: probes.length - before, watch: watching(), pendingTimers: timers.length,
    waiters: typeof sessionProbeWaiters === "undefined" ? 0 : sessionProbeWaiters.size};
  // A new stream gets its own watch, and replacing it leaves exactly one.
  server.reachable = true;
  openStream(); openStream();
  await advance(100);
  opened[opened.length - 1].drop();
  await advance(50);
  out.replaced = {streams: opened.length, closedOld: opened[1].readyState === 2, watch: watching() !== null};
  console.log(JSON.stringify(out));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    out = run_js(script)
    outage = out["outage"]
    assert outage["waiters"] == 0 and outage["navigations"] == []
    assert outage["peakTimers"] <= 2, "the stream's own retry, and at most one probe timer"
    # Backing off: 5 s doubling to a minute, so ten minutes cost about a dozen probes.
    assert outage["probes"] <= 14, outage["probes"]
    assert outage["gaps"][:5] == [10000, 20000, 40000, 60000, 60000]
    assert max(outage["gaps"]) <= 63000
    assert out["afterStop"] == {"extraProbes": 0, "watch": None, "pendingTimers": 0, "waiters": 0}
    assert out["replaced"] == {"streams": 3, "closedOld": True, "watch": True}


@needs_node
def test_the_named_stream_event_goes_to_sign_in_without_a_probe():
    script = STREAM_ENV + STREAM_SOURCE + r"""
(async () => {
  openStream();
  await advance(100);
  // The status route is misrouted, so a probe could not tell anyone anything.
  server.authRoute = false;
  server.session = false;
  opened[0].revoke();
  await settle();
  const atOnce = {navigations: [...navigations], probes: probes.length, watch: watching()};
  await advance(120000);
  report({atOnce, streams: opened.length});
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = run_js(script)
    assert result["atOnce"] == {"navigations": [NEXT], "probes": 0, "watch": None}
    assert result["probes"] == [] and result["streams"] == 1 and result["pendingTimers"] == 0


@needs_node
@pytest.mark.parametrize("unknown", ["404", "500-ish body", "malformed", "unreachable"])
def test_an_unknown_probe_answer_never_reopens_a_refused_stream(unknown):
    # The reviewed sequence without the named event: the session is revoked,
    # the reconnect is refused, and the status route cannot say why.
    script = STREAM_ENV + STREAM_SOURCE + r"""
const UNKNOWN = "__UNKNOWN__";
(async () => {
  openStream();
  await advance(100);
  server.session = false;                                   // revoked: reconnects are refused
  if (UNKNOWN === "404") server.authRoute = false;
  if (UNKNOWN === "500-ish body") server.authBody = {error: "internal"};
  if (UNKNOWN === "malformed") server.authBody = {signed_in: "yes"};
  if (UNKNOWN === "unreachable") server.reachable = false;   // the stream just keeps failing
  opened[0].drop();                                         // otherwise the retry at +3 s is refused: closed
  await advance(60000);
  const minute = {probes: probes.length, streams: opened.length, navigations: [...navigations],
    state: states[states.length - 1], watch: watching()};
  await advance(540000);
  const tenMinutes = {probes: probes.length, streams: opened.length};
  // The route starts answering: the revocation is finally confirmed.
  server.authRoute = true; server.authBody = null; server.reachable = true;
  await advance(61000);
  report({minute, tenMinutes});
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = run_js(script.replace("__UNKNOWN__", unknown))
    minute = result["minute"]
    assert minute["streams"] == 1, "a refused stream is not reopened on an unknown answer"
    assert minute["navigations"] == [] and minute["state"] == "reconnecting"
    assert minute["probes"] <= 5, "backing off, not a probe every window"
    assert minute["watch"] == {"timer": True, "probing": False}
    assert result["tenMinutes"]["streams"] == 1 and result["tenMinutes"]["probes"] <= 15
    assert result["navigations"] == [NEXT], "once the route answers, the sign-out is acted on"
    assert result["pendingTimers"] == 0


@needs_node
def test_a_refused_stream_recovers_within_one_slow_interval_once_the_server_is_healthy():
    # The reviewed sequence: the stream answers with errors while auth-status
    # says signed in, long enough to pass the fast attempts; then the server
    # recovers. No reload, no chat switch.
    script = STREAM_ENV + STREAM_SOURCE + WAKE + r"""
(async () => {
  openStream();
  await advance(100);
  server.stream = "refuse";
  opened[0].drop();
  await advance(240000);
  const refused = {streams: opened.length, watch: watching(), closed: opened[opened.length - 1].readyState === 2,
    state: states[states.length - 1], navigations: [...navigations]};
  server.stream = "ok";                                   // healthy again
  const healthyAt = clock;
  let recoveredAfter = null;
  for (let i = 0; i < 3600 && recoveredAfter === null; i++) {
    await advance(1000);
    if (states[states.length - 1] === "connected") recoveredAfter = clock - healthyAt;
  }
  await advance(600000);                                  // and it stays quiet afterwards
  report({refused, recoveredAfter, streamsAfter: opened.length, watch: watching(), events});
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = run_js(script)
    refused = result["refused"]
    assert refused["closed"] is True and refused["state"] == "reconnecting"
    assert refused["watch"] == {"timer": True, "probing": False}, "an attempt is always scheduled"
    assert refused["navigations"] == []
    assert result["recoveredAfter"] is not None and result["recoveredAfter"] <= 61000
    assert result["last"] == "connected" and result["navigations"] == []
    assert result["streamsAfter"] == refused["streams"] + 1, "one reopen was enough"
    assert result["watch"] == {"timer": False, "probing": False} and result["pendingTimers"] == 0


@needs_node
def test_a_long_refusal_costs_one_attempt_per_interval_and_the_label_stays_true():
    script = STREAM_ENV + STREAM_SOURCE + WAKE + r"""
(async () => {
  openStream();
  await advance(100);
  server.stream = "refuse";
  opened[0].drop();
  let peakTimers = 0, unbacked = 0;
  for (let i = 0; i < 3600; i++) {
    await advance(1000);
    peakTimers = Math.max(peakTimers, timers.length);
    // "Reconnecting" must mean it: a closed stream has an attempt pending.
    const source = opened[opened.length - 1], watch = watching();
    if (states[states.length - 1] === "reconnecting" && source.readyState === 2
        && !(watch && (watch.timer || watch.probing))) unbacked += 1;
  }
  const gaps = connections.slice(1).map((at, i) => at - connections[i]);
  report({attempts: connections.length, gaps: gaps.slice(0, 7), steady: [...new Set(gaps.slice(7))],
    peakTimers, unbacked, probeCount: probes.length});
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = run_js(script)
    # Fast attempts first, each further apart, then one a minute for the rest of the hour.
    assert result["gaps"] == [3100, 2000, 10000, 20000, 40000, 60000, 60000]
    assert result["steady"] == [60000]
    assert 60 <= result["attempts"] <= 66 and result["probeCount"] <= 66
    assert result["peakTimers"] <= 2, "one probe timer, plus a connection in progress"
    assert result["unbacked"] == 0
    assert result["navigations"] == []


@needs_node
def test_visibility_online_and_sending_each_bring_one_attempt_forward():
    script = STREAM_ENV + STREAM_SOURCE + WAKE + r"""
(async () => {
  const out = {};
  openStream();
  await advance(100);
  // An open stream: a wake-up does nothing.
  fire("visibilitychange"); fire("online"); wakeIfDefined();
  await advance(1000);
  out.whileOpen = probes.length;
  server.stream = "refuse";
  opened[0].drop();
  await advance(400000);                                   // well into the slow lane
  const attempt = async (trigger) => {
    await advance(10000);                                  // clear of the minimum gap
    const before = connections.length, at = clock;
    trigger();
    await advance(1000);
    return {attempts: connections.length - before, timers: timers.length};
  };
  out.visible = await attempt(() => fire("visibilitychange"));
  out.online = await attempt(() => fire("online"));
  out.sent = await attempt(() => wakeIfDefined());          // what a sent message does
  out.together = await attempt(() => { fire("visibilitychange"); fire("online"); wakeIfDefined(); fire("online"); });
  // A tab going to the background is not a wake-up.
  document.hidden = true;
  out.hidden = await attempt(() => fire("visibilitychange"));
  document.hidden = false;
  // Wake-ups in quick succession are held to the minimum gap, not a storm.
  const before = connections.length;
  for (let i = 0; i < 40; i++) { fire("online"); await advance(250); }
  out.burst = connections.length - before;
  // After the stream is stopped, nothing is left to wake.
  chatStopStream();
  fire("online"); fire("visibilitychange"); wakeIfDefined();
  await advance(600000);
  report({out, registered: Object.keys(handlers).sort()});
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = run_js(script)
    out = result["out"]
    assert result["registered"] == ["online", "visibilitychange"]
    assert out["whileOpen"] == 0
    for name in ("visible", "online", "sent", "together"):
        assert out[name] == {"attempts": 1, "timers": 1}, name
    assert out["hidden"]["attempts"] == 0
    assert out["burst"] <= 2, "ten seconds of wake-ups, five seconds apart at the fastest"
    assert result["pendingTimers"] == 0 and result["navigations"] == []


def test_the_wake_ups_are_wired_to_the_watch():
    assert 'document.addEventListener("visibilitychange", () => { if (!document.hidden) wakeSessionWatch(); });' in APP
    assert 'window.addEventListener("online", () => wakeSessionWatch());' in APP
    submit = APP[APP.index("  async function submit() {"):APP.index("  send.onclick = submit;")]
    sent = submit[submit.index('"POST", { text }, chatPendingSend.key'):submit.index("} catch (error) {")]
    assert "wakeSessionWatch();" in sent, "a sent message retries a closed stream"
    watch = function_source("startSessionWatch")
    assert "STREAM_REOPEN_LIMIT" not in APP and "stopSessionWatch(); return; }" not in watch
    assert watch.count("stopSessionWatch();") == 3, "replacing a watch, signing out, and the stream's event"
    # The label follows the stream state the watch keeps honest.
    assert 'streamStatus === "reconnecting" ? "Reconnecting" : "Connecting"' in APP


@needs_node
def test_a_drop_with_the_session_intact_recovers_by_itself():
    script = STREAM_ENV + STREAM_SOURCE + r"""
(async () => {
  openStream();
  await advance(100);
  server.reachable = false;
  opened[0].drop();
  await advance(60000);
  server.reachable = true;
  await advance(10000);
  const recovered = {state: states[states.length - 1], streams: opened.length};
  // Revoked later: leave once; nothing probes or reconnects afterwards.
  server.session = false;
  opened[0].drop();
  await advance(70000);
  const before = probes.length;
  await advance(600000);
  report({recovered, extraProbes: probes.length - before});
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = run_js(script)
    assert result["recovered"] == {"state": "connected", "streams": 1}
    assert result["navigations"] == [NEXT]
    assert result["extraProbes"] == 0 and result["pendingTimers"] == 0


# ── next ──────────────────────────────────────────────────────────────────────

NEXT_CASES = {
    # honoured
    "/": "/", "/?a=1#interface/DEV1": "/?a=1#interface/DEV1", "/index.html#docs": "/index.html#docs",
    "/a/../b": "/b", "/./x": "/x",
    # another host, a scheme, a backslash form, a control character, this page
    "//evil.example/": "/", "https://evil.example/": "/", "http://127.0.0.1:8837/": "/",
    "/\\evil.example": "/", "\\\\evil.example": "/", "/a\\b": "/",
    "javascript:alert(1)": "/", "data:text/html,x": "/", "": "/", "evil": "/",
    "/\tjavascript:x": "/", "/a\nb": "/", "/signin.html?next=/": "/", "/signin.html": "/",
    "/x/../signin.html": "/",
    # normalisation that would turn a local-looking path into another origin
    "/a/..//evil.example": "/", "/.//evil.example": "/", "/%2e//evil.example": "/",
    "/%2E//evil.example": "/", "/a/%2e%2e//evil.example": "/", "/a/%2E%2e//evil.example": "/",
    "/..//evil.example": "/", "/a/b/../..//evil.example/path?x#y": "/", "/././/evil.example": "/",
    "/a/..///evil.example": "/", "/a/../%2e//evil.example": "/",
    # encoded separators stay encoded, so they stay a path on this origin
    "/%2F%2Fevil.example": "/%2F%2Fevil.example", "/%5Cevil.example": "/%5Cevil.example",
    "/a/..%2F%2Fevil.example": "/a/..%2F%2Fevil.example",
    # double-encoded forms are not decoded a second time
    "/%252e//evil.example": "/%252e//evil.example", "/%252F%252Fevil.example": "/%252F%252Fevil.example",
    "/a/%252e%252e//evil.example": "/a/%252e%252e//evil.example",
}
NEXT_QUERIES = {
    # as they arrive in ?next=, once- and twice-encoded
    "?next=%2Fa%2F..%2F%2Fevil.example": "/", "?next=%2F.%2F%2Fevil.example": "/",
    "?next=%2F%252e%2F%2Fevil.example": "/", "?next=%2F%25252e%2F%2Fevil.example": "/%252e//evil.example", "?next=/%2e//evil.example": "/",
    "?next=%252Fa%252F..%252F%252Fevil.example": "/", "?next=%2F%2Fevil.example": "/",
    "?next=%255C%255Cevil.example": "/", "?next=https%3A%2F%2Fevil.example": "/",
    "?next=%2F%3Fa%3D1": "/?a=1", "": "/",
}


@needs_node
@pytest.mark.parametrize("source", ["signin.js", "app.js"])
def test_next_is_validated_after_normalisation_on_both_pages(source):
    code = SIGNIN_JS if source == "signin.js" else between("const NEXT_UNSAFE", "function signInUrl()")
    script = code + r"""
const cases = __CASES__;
const origin = "http://127.0.0.1:8837";
const out = {}, escapes = [];
for (const raw of Object.keys(cases)) {
  const result = safeNext(raw, origin);
  out[raw] = result;
  // Whatever comes back, navigating to it stays on this origin, by any reading.
  if (new URL(origin + result).origin !== origin) escapes.push(raw);
  if (new URL(result, origin).origin !== origin) escapes.push(raw);
  if (!/^\/(?![\/\\])/.test(result)) escapes.push(raw);
}
console.log(JSON.stringify({out, escapes,
  notString: [safeNext(null, origin), safeNext(undefined, origin), safeNext(7, origin)]}));
"""
    result = run_js(script.replace("__CASES__", json.dumps(NEXT_CASES)))
    assert result["out"] == NEXT_CASES
    assert result["escapes"] == []
    assert result["notString"] == ["/", "/", "/"]


def test_both_pages_carry_the_same_next_rule():
    in_app = between("const NEXT_UNSAFE", "function signInUrl()")
    in_page = SIGNIN_JS[SIGNIN_JS.index("const NEXT_UNSAFE"):SIGNIN_JS.index("// A redirect from the review")]
    assert in_app.strip() == in_page.strip()
    assert "safeNext(location.pathname + location.search + location.hash, location.origin)" in APP
    assert "env.navigate(location.origin + next)" in SIGNIN_JS
    assert SIGNIN_JS.count("env.navigate(") == 1, "one way out of the sign-in page"


@needs_node
def test_sign_in_page_reads_next_from_its_own_url():
    script = SIGNIN_JS + r"""
const queries = __QUERIES__;
const origin = "http://127.0.0.1:8837";
const loc = (search, hash) => ({origin, search, hash});
const out = {};
for (const search of Object.keys(queries)) out[search] = nextTarget(loc(search, ""));
console.log(JSON.stringify({out,
  fragmentKept: nextTarget(loc("?next=%2F", "#interface/DEV1")),
  ownFragmentWins: nextTarget(loc("?next=%2F%23docs", "#interface/DEV1")),
  hostileWithFragment: nextTarget(loc("?next=%2Fa%2F..%2F%2Fevil.example", "#x")),
  base: [signInAuthorityBase("http://127.0.0.1:8857", {hostname: "localhost"}),
         signInAuthorityBase("http://127.0.0.1:8857/x?y", {hostname: "127.0.0.1"}),
         signInAuthorityBase(null, {hostname: "localhost"}),
         signInAuthorityBase("not a url", {hostname: "localhost"})],
}));
"""
    result = run_js(script.replace("__QUERIES__", json.dumps(NEXT_QUERIES)))
    assert result["out"] == NEXT_QUERIES
    assert result["fragmentKept"] == "/#interface/DEV1"
    assert result["ownFragmentWins"] == "/#docs"
    assert result["hostileWithFragment"] == "/#x"
    assert result["base"] == ["http://localhost:8857", "http://127.0.0.1:8857", None, None]


@needs_node
def test_a_hostile_next_ends_on_this_origin_after_sign_in():
    script = SIGNIN_HARNESS.replace('search: "?next=%2F%3Fa%3D1", hash: "#interface/DEV1"',
                                    'search: "?next=%2Fa%2F..%2F%2Fevil.example", hash: ""') + r"""
server.auth.signed_in = true; server.review.signed_in = true;
(async () => {
  await page.start(); await settle();
  finish({navigations, next: page.next});
})().catch((error) => { realLog(String(error.stack)); process.exitCode = 1; });
"""
    assert run_js(script) == {"navigations": ["http://localhost:8837/"], "next": "/"}


@needs_node
@pytest.mark.parametrize("pathname,expected", [
    ("//evil.example/x", "/signin.html?next=%2F"),
    ("/\\evil.example", "/signin.html?next=%2F"),
    ("/index.html", "/signin.html?next=%2Findex.html%3Fq%3D1%23interface%2FDEV1%2Fcv_1"),
])
def test_the_app_only_hands_the_sign_in_page_a_local_next(pathname, expected):
    script = SESSION_ENV + SESSION + r"""
location.pathname = __PATH__;
goToSignIn();
console.log(JSON.stringify({navigations}));
"""
    assert run_js(script.replace("__PATH__", json.dumps(pathname))) == {"navigations": [expected]}


# ── The sign-in page ──────────────────────────────────────────────────────────

SIGNIN_HARNESS = FAKE_DOM + QRCODE_JS + SIGNIN_JS + r"""
const AUTHORITY = "http://localhost:8857";       // the page's hostname, not the advertised one
const SECRET = "__SECRET__", OTPAUTH = "__OTPAUTH__";
let focused = null;
FakeElement.prototype.focus = function () { focused = this; };
const rects = [];
FakeElement.prototype.getContext = function () {
  return {fillStyle: "", fillRect(x, y, w, h) { rects.push([this.fillStyle, x, y, w, h]); }};
};
// Any use of browser storage or the console is a failure of the page.
const leaks = [];
const trap = (name) => new Proxy({}, {get: (_, key) => (...args) => { leaks.push([name, String(key), args]); }});
globalThis.localStorage = trap("localStorage");
globalThis.sessionStorage = trap("sessionStorage");
const realLog = console.log.bind(console);
globalThis.console = trap("console");
const server = {
  reviewUp: true, authorityUp: true,
  review: {enrolled: true, signed_in: false, authority: {url: "http://127.0.0.1:8857", up: true}},
  auth: {pinned: true, gui_origin: "http://localhost:8837", enrolled: true, signed_in: false,
    user: null, session: null, retry_after: 0, setup_cli: "./sc auth user add <username>"},
  routes: {},          // "/auth/identify": (body) => [status, data, retryAfterHeader?]
};
const requests = [];
const fail = (code, message = "refused", details = {}) => ({error: {code, message, details}});
const fetchStub = async (url, init = {}) => {
  const body = init.body === undefined ? null : JSON.parse(init.body);
  requests.push({url, method: init.method || "GET", credentials: init.credentials,
    type: init.headers ? init.headers["Content-Type"] : null, body});
  let status = 200, data = {}, retry = null;
  if (url === "/api/auth/status") {
    if (!server.reviewUp) throw new TypeError("Failed to fetch");
    data = server.review;
  } else {
    if (!url.startsWith(AUTHORITY + "/") || !server.authorityUp) throw new TypeError("Failed to fetch");
    const path = url.slice(AUTHORITY.length);
    if (path === "/auth/status") data = server.auth;
    else [status, data, retry = null] = server.routes[path](body);
  }
  return {ok: status >= 200 && status < 300, status, json: async () => data,
    headers: {get: (name) => name === "Retry-After" ? retry : null}};
};
let clock = 5000000, sleeps = 0;
const intervals = new Map(), navigations = [];
let intervalId = 0;
const root = new FakeElement("div");
const page = createSignIn({
  root, document: {createElement: (tag) => new FakeElement(tag)},
  location: {origin: "http://localhost:8837", hostname: "localhost",
    search: "?next=%2F%3Fa%3D1", hash: "#interface/DEV1"},
  fetch: fetchStub, qrcode,
  now: () => clock,
  setInterval: (fn) => { intervals.set(++intervalId, fn); return intervalId; },
  clearInterval: (id) => { intervals.delete(id); },
  sleep: async () => { sleeps += 1; },
  navigate: (to) => { navigations.push(to); },
});
const txt = (node) => typeof node === "string" ? node
  : node.nodeType === 3 ? node.textContent
  : node.children.length ? node.children.map(txt).join("") : node.textContent;
const settle = async () => { for (let i = 0; i < 12; i++) await new Promise((r) => setImmediate(r)); };
const heading = () => txt(root.children[0]);
const inputs = () => all(root, (n) => n.tagName === "INPUT");
const input = (id) => inputs().find((n) => n.id === id);
const formNode = () => all(root, (n) => n.tagName === "FORM")[0] || null;
const notice = () => { const n = all(root, (n) => n.className.startsWith("signin-notice"))[0]; return n ? n.textContent : null; };
const submitButton = () => all(root, (n) => n.tagName === "BUTTON" && n.type === "submit")[0];
const buttons = () => all(root, (n) => n.tagName === "BUTTON").map((n) => n.textContent);
const fill = (values) => { for (const [id, value] of Object.entries(values)) input(id).value = value; };
const submit = async (values = {}) => { fill(values); await formNode().onsubmit({preventDefault() {}}); await settle(); };
const tickClock = (seconds) => { clock += seconds * 1000; for (const fn of intervals.values()) fn(); };
const posts = () => requests.filter((r) => r.method === "POST");
const view = () => ({heading: heading(), fields: inputs().map((n) => n.id), notice: notice(),
  text: root.children.map(txt).join(" | "), focus: focused ? focused.id || focused.tagName : null});
const finish = (value) => realLog(JSON.stringify(value));
""".replace("__SECRET__", SECRET).replace("__OTPAUTH__", OTPAUTH)


@needs_node
@pytest.mark.parametrize("setup,expected", [
    # the authority does not answer
    ("server.authorityUp = false;",
     {"heading": "Sign-in is not available", "form": False, "has": ["./sc launch"], "navigations": []}),
    # the review server does not say where the authority is
    ("server.review.authority = {url: null, up: false};",
     {"heading": "Sign-in is not available", "form": False, "has": ["./sc launch"], "navigations": []}),
    # the review server itself does not answer
    ("server.reviewUp = false;",
     {"heading": "Sign-in is not available", "form": False, "has": ["The review server did not answer."],
      "navigations": []}),
    # no GUI origin pinned yet, and no user: the instruction only
    ("Object.assign(server.auth, {pinned: false, enrolled: false, reason: 'no origin'});",
     {"heading": "No GUI user exists yet", "form": False, "has": ["./sc auth user add <username>"],
      "navigations": []}),
    ("server.auth.enrolled = false; server.auth.setup_cli = './sc auth user add NAME';",
     {"heading": "No GUI user exists yet", "form": False, "has": ["./sc auth user add NAME"],
      "navigations": []}),
    # signed in everywhere: straight to next
    ("server.auth.signed_in = true; server.review.signed_in = true;",
     {"heading": "Sign in", "form": False, "has": [], "navigations": [HOME]}),
    # the review server accepts the session and the authority is down: the GUI still works
    ("server.review.signed_in = true; server.authorityUp = false;",
     {"heading": "Sign in", "form": False, "has": [], "navigations": [HOME]}),
    # the review server accepts a session the authority does not: sign in again, no bounce
    ("server.review.signed_in = true;",
     {"heading": "Sign in", "form": True, "has": [], "navigations": []}),
    # the authority accepts a session the review server never comes to see: say so, no loop
    ("server.auth.signed_in = true;",
     {"heading": "Signed in, but the GUI does not see it", "form": False,
      "has": ["./sc launch", "Try again", "Sign out"], "navigations": []}),
    # the plain case
    ("", {"heading": "Sign in", "form": True, "has": [], "navigations": []}),
])
def test_sign_in_page_states(setup, expected):
    script = SIGNIN_HARNESS + setup + r"""
(async () => {
  await page.start(); await settle();
  finish({heading: heading(), form: Boolean(formNode()), text: view().text + " | " + buttons().join(" | "),
    fields: view().fields, navigations, posts: posts().length, leaks});
})().catch((error) => { realLog(String(error.stack)); process.exitCode = 1; });
"""
    result = run_js(script)
    assert result["heading"] == expected["heading"]
    assert result["form"] is expected["form"]
    for fragment in expected["has"]:
        assert fragment in result["text"], fragment
    assert result["navigations"] == expected["navigations"]
    assert result["fields"] == (["username"] if expected["form"] else [])
    assert result["posts"] == 0 and result["leaks"] == []


@needs_node
def test_sign_in_page_counts_down_a_rate_limit_and_names_the_unlock_verb():
    script = SIGNIN_HARNESS + r"""
server.auth.retry_after = 30;
(async () => {
  await page.start(); await settle();
  const out = {};
  out.atLoad = {notice: notice(), disabled: submitButton().disabled};
  await submit({username: "operator"});                  // refused locally while waiting
  out.sentWhileWaiting = posts().length;
  tickClock(10);
  out.later = notice();
  tickClock(21);
  out.over = {notice: notice(), disabled: submitButton().disabled};
  // A 429 during sign-in starts a new wait, from the body or the header.
  server.routes["/auth/identify"] = () => [200, {first_sign_in: false}];
  await submit({username: "operator"});
  server.routes["/auth/sign-in"] = () => [429, fail("rate_limited", "wait", {retry_after: 8, unlock_cli: "./sc auth unlock"})];
  await submit({password: "correct horse", code: "123456"});
  out.limited = {notice: notice(), disabled: submitButton().disabled, heading: heading(),
    password: input("password").value};
  tickClock(8);
  server.routes["/auth/sign-in"] = () => [429, {}, "4"];
  await submit({password: "correct horse", code: "123456"});
  out.fromHeader = notice();
  out.liveTimers = intervals.size;      // one view, one countdown
  finish({out, leaks});
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = run_js(script)
    out = result["out"]
    wait = "Too many failed attempts. Try again in {} s, or run ./sc auth unlock on the host."
    assert out["atLoad"] == {"notice": wait.format(30), "disabled": True}
    assert out["sentWhileWaiting"] == 0
    assert out["later"] == wait.format(20)
    assert out["over"] == {"notice": "", "disabled": False}
    assert out["limited"] == {"notice": wait.format(8), "disabled": True, "heading": "Sign in",
                              "password": ""}
    assert out["fromHeader"] == wait.format(4)
    assert out["liveTimers"] == 1
    assert result["leaks"] == []


@needs_node
def test_normal_sign_in_flow():
    script = SIGNIN_HARNESS + r"""
server.routes["/auth/identify"] = () => [200, {first_sign_in: false}];
(async () => {
  await page.start(); await settle();
  const out = {start: view()};
  await submit({username: ""});
  out.emptyUsername = {notice: notice(), posts: posts().length};
  await submit({username: "  operator "});
  out.password = view();
  out.attributes = inputs().map((n) => [n.id, n.type, n.autocomplete, Boolean(n.readOnly)]);
  // Nothing is sent until there is a password and a six-digit code.
  await submit({password: "", code: "123456"});
  await submit({password: "correct horse", code: "12345"});
  out.incomplete = {notice: notice(), posts: posts().length};
  // One generic failure, whatever was wrong.
  const refusals = [fail("invalid_credentials", "Sign-in failed."), fail("bad_request", "nope")];
  server.routes["/auth/sign-in"] = () => [refusals.length === 2 ? 401 : 400, refusals.shift()];
  await submit({password: "wrong", code: "123 456", device: " desk "});
  out.refused = {notice: notice(), password: input("password").value, code: input("code").value,
    focus: view().focus};
  await submit({password: "wrong", code: "123456"});
  out.refusedAgain = notice();
  // Accepted: wait for the review server to see the session, then go to next.
  let reviewReads = 0;
  server.routes["/auth/sign-in"] = () => { server.review = new Proxy(server.review, {
    get: (target, key) => key === "signed_in" ? (reviewReads += 1) > 2 : target[key]});
    return [200, {signed_in: true, user: {user_id: 1, username: "operator"}, session: {id: "s1"}}]; };
  await submit({password: "correct horse", code: "123456"});
  out.done = {navigations, sleeps, heading: heading()};
  out.posts = posts();
  finish({out, leaks});
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = run_js(script)
    out = result["out"]
    assert out["start"]["heading"] == "Sign in" and out["start"]["focus"] == "username"
    assert out["emptyUsername"] == {"notice": "Enter your username.", "posts": 0}
    assert out["password"]["fields"] == ["username", "password", "code", "device"]
    assert out["password"]["focus"] == "password"
    assert out["attributes"] == [
        ["username", "text", "username", True], ["password", "password", "current-password", False],
        ["code", "text", "one-time-code", False], ["device", "text", "off", False]]
    assert out["incomplete"] == {
        "notice": "Enter the six-digit code from your authenticator app.", "posts": 1}
    assert out["refused"] == {"notice": FAILED, "password": "", "code": "", "focus": "password"}
    assert out["refusedAgain"] == FAILED
    assert out["done"] == {"navigations": [HOME], "sleeps": 2, "heading": "Signed in"}
    json_post = {"method": "POST", "credentials": "include", "type": "application/json"}
    assert out["posts"] == [
        {"url": "http://localhost:8857/auth/identify", **json_post, "body": {"username": "operator"}},
        {"url": "http://localhost:8857/auth/sign-in", **json_post,
         "body": {"username": "operator", "password": "wrong", "code": "123456", "device": "desk"}},
        {"url": "http://localhost:8857/auth/sign-in", **json_post,
         "body": {"username": "operator", "password": "wrong", "code": "123456", "device": "desk"}},
        {"url": "http://localhost:8857/auth/sign-in", **json_post,
         "body": {"username": "operator", "password": "correct horse", "code": "123456",
                  "device": "desk"}},
    ]
    assert result["leaks"] == []


@needs_node
def test_first_sign_in_sequence_sends_nothing_to_confirm_until_it_is_complete():
    script = SIGNIN_HARNESS + r"""
server.routes["/auth/identify"] = () => [200, {first_sign_in: true}];
const begin = () => [200, {enrolment: "enr_1", username: "operator", secret: SECRET, otpauth_uri: OTPAUTH,
  issuer: "Subfloor", algorithm: "SHA1", digits: 6, period: 30, expires_in: 900, password_min: 10}];
const confirms = () => posts().filter((r) => r.url.endsWith("/auth/first/confirm")).length;
(async () => {
  await page.start(); await settle();
  await submit({username: "operator"});
  const out = {claim: view()};
  server.routes["/auth/first/begin"] = () => [401, fail("invalid_claim")];
  await submit({claim: "AAAA-BBBB"});
  out.badClaim = {notice: notice(), heading: heading(), value: input("claim").value};
  server.routes["/auth/first/begin"] = begin;
  await submit({claim: " ABCD-EFGH "});
  out.password = view();
  out.passwordAttributes = inputs().map((n) => [n.id, n.type, n.autocomplete, n.minLength || null]);
  await submit({"new-password": "short", "new-password-again": "short"});
  out.tooShort = notice();
  await submit({"new-password": "a long password", "new-password-again": "a long passw0rd"});
  out.mismatch = {notice: notice(), again: input("new-password-again").value, heading: heading()};
  await submit({"new-password": "a long password", "new-password-again": "a long password"});
  // The link step: QR code, manual key, one code.
  const qr = qrcode(0, "M"); qr.addData(OTPAUTH); qr.make();
  let dark = 0;
  for (let r = 0; r < qr.getModuleCount(); r++)
    for (let c = 0; c < qr.getModuleCount(); c++) if (qr.isDark(r, c)) dark += 1;
  const canvas = all(root, (n) => n.tagName === "CANVAS")[0];
  out.link = {heading: heading(), fields: view().fields, focus: view().focus,
    key: txt(all(root, (n) => n.className === "signin-key")[0]),
    uri: txt(all(root, (n) => n.className === "signin-uri")[0]),
    canvas: [canvas.width, (qr.getModuleCount() + 8) * 4, canvas.role],
    background: rects[0], darkModules: rects.length - 1 === dark,
    colours: [...new Set(rects.map((rect) => rect[0]))],
    confirmsSoFar: confirms()};
  await submit({code: ""});
  await submit({code: "12ab56"});
  out.noCode = {notice: notice(), confirms: confirms()};
  server.routes["/auth/first/confirm"] = () => [401, fail("invalid_code")];
  await submit({code: "111111", device: "phone"});
  out.wrongCode = {notice: notice(), heading: heading(), code: input("code").value};
  server.routes["/auth/first/confirm"] = () => [400, fail("password_unacceptable")];
  await submit({code: "222222"});
  out.badPassword = {heading: heading(), notice: notice()};
  await submit({"new-password": "another long one", "new-password-again": "another long one"});
  server.routes["/auth/first/confirm"] = () => [409, fail("enrolment_expired")];
  await submit({code: "333333"});
  out.expired = {heading: heading(), notice: notice(), text: view().text.includes(SECRET)};
  await submit({claim: "ABCD-EFGH"});
  await submit({"new-password": "another long one", "new-password-again": "another long one"});
  server.routes["/auth/first/confirm"] = () => { server.review = {...server.review, signed_in: true};
    return [200, {signed_in: true, user: {user_id: 1, username: "operator"}, session: {id: "s1"}}]; };
  await submit({code: "444 444"});
  out.done = {navigations, text: view().text};
  out.confirmBodies = posts().filter((r) => r.url.endsWith("/auth/first/confirm")).map((r) => r.body);
  out.begins = posts().filter((r) => r.url.endsWith("/auth/first/begin")).map((r) => r.body);
  // The secret went nowhere but the page: not into a request, a URL, storage or the console.
  out.secretInRequests = requests.some((r) => JSON.stringify(r).includes(SECRET));
  out.secretInNavigation = navigations.some((to) => to.includes(SECRET));
  out.secretLeftOnPage = view().text.includes(SECRET);
  finish({out, leaks});
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = run_js(script)
    out = result["out"]
    assert out["claim"]["heading"] == "First sign-in"
    assert out["claim"]["fields"] == ["claim"] and out["claim"]["focus"] == "claim"
    assert out["badClaim"] == {"notice": "That claim code was not accepted.",
                               "heading": "First sign-in", "value": ""}
    assert out["password"]["heading"] == "Choose a password"
    assert out["passwordAttributes"] == [
        ["username", "text", "username", None], ["new-password", "password", "new-password", 10],
        ["new-password-again", "password", "new-password", 10]]
    assert out["tooShort"] == "Use at least 10 characters."
    assert out["mismatch"] == {"notice": "The two passwords do not match.", "again": "",
                               "heading": "Choose a password"}
    link = out["link"]
    assert link["heading"] == "Link an authenticator app"
    assert link["fields"] == ["code", "device"] and link["focus"] == "code"
    assert link["key"] == "JBSW Y3DP EHPK 3PXP JBSW Y3DP EHPK 3PXP"
    assert link["uri"] == OTPAUTH
    assert link["canvas"][0] == link["canvas"][1] and link["canvas"][2] == "img"
    assert link["background"][:3] == ["#ffffff", 0, 0] and link["darkModules"] is True
    assert link["colours"] == ["#ffffff", "#000000"]
    assert link["confirmsSoFar"] == 0, "choosing the password sends nothing"
    assert out["noCode"] == {
        "notice": "Enter the six-digit code your authenticator app shows.", "confirms": 0}
    assert out["wrongCode"] == {
        "notice": "That code did not match. Wait for the next code and try again.",
        "heading": "Link an authenticator app", "code": ""}
    assert out["badPassword"] == {"heading": "Choose a password",
                                  "notice": "That password was not accepted. Choose another."}
    assert out["expired"] == {"heading": "First sign-in",
                              "notice": "First sign-in timed out. Enter the claim code again.",
                              "text": False}
    assert out["done"]["navigations"] == [HOME]
    assert out["begins"] == [{"username": "operator", "claim_code": "AAAA-BBBB"},
                             {"username": "operator", "claim_code": "ABCD-EFGH"},
                             {"username": "operator", "claim_code": "ABCD-EFGH"}]
    assert out["confirmBodies"] == [
        {"enrolment": "enr_1", "password": "a long password", "code": "111111", "device": "phone"},
        {"enrolment": "enr_1", "password": "a long password", "code": "222222", "device": "phone"},
        {"enrolment": "enr_1", "password": "another long one", "code": "333333"},
        {"enrolment": "enr_1", "password": "another long one", "code": "444444"},
    ]
    assert out["secretInRequests"] is False and out["secretInNavigation"] is False
    assert out["secretLeftOnPage"] is False
    assert result["leaks"] == []


# ── QR correctness ────────────────────────────────────────────────────────────
# An independent decoder (finder, timing, alignment and version areas masked
# out; all eight data masks tried; Reed-Solomon syndromes checked; byte-mode
# payload read back). It shares no code and no table with the encoder.

QR_ALIGNMENT = {5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46],
                10: [6, 28, 50]}
# level M: error-correction codewords per block, then (blocks, data codewords per block)
QR_BLOCKS_M = {5: (24, [(2, 43)]), 6: (16, [(4, 27)]), 7: (18, [(4, 31)]),
               8: (22, [(2, 38), (2, 39)]), 9: (22, [(3, 36), (2, 37)]),
               10: (26, [(4, 43), (1, 44)])}
QR_MASKS = [
    lambda i, j: (i + j) % 2 == 0, lambda i, j: i % 2 == 0, lambda i, j: j % 3 == 0,
    lambda i, j: (i + j) % 3 == 0, lambda i, j: (i // 2 + j // 3) % 2 == 0,
    lambda i, j: (i * j) % 2 + (i * j) % 3 == 0,
    lambda i, j: ((i * j) % 2 + (i * j) % 3) % 2 == 0,
    lambda i, j: ((i + j) % 2 + (i * j) % 3) % 2 == 0,
]


def _gf_tables():
    exp, log = [0] * 512, [0] * 256
    value = 1
    for power in range(255):
        exp[power], log[value] = value, power
        value <<= 1
        if value & 0x100:
            value ^= 0x11D
    for power in range(255, 512):
        exp[power] = exp[power - 255]
    return exp, log


def _syndromes_are_zero(block: list[int], ec: int) -> bool:
    exp, log = _gf_tables()
    for root in range(ec):
        total = 0
        for byte in block:
            total = (exp[log[total] + root] if total else 0) ^ byte
        if total:
            return False
    return True


def decode_qr_level_m(rows: list[str]) -> str:
    size = len(rows)
    version = (size - 17) // 4
    assert size == 17 + 4 * version and version in QR_BLOCKS_M, f"unexpected size {size}"
    module = [[ch == "1" for ch in row] for row in rows]
    function = [[False] * size for _ in range(size)]

    def mark(r0, r1, c0, c1):
        for r in range(r0, r1):
            for c in range(c0, c1):
                function[r][c] = True

    mark(0, 9, 0, 9), mark(0, 9, size - 8, size), mark(size - 8, size, 0, 9)   # finders + format
    mark(6, 7, 0, size), mark(0, size, 6, 7)                                   # timing
    centres = QR_ALIGNMENT[version]
    for r in centres:
        for c in centres:
            if (r, c) in ((6, 6), (6, centres[-1]), (centres[-1], 6)):
                continue
            mark(r - 2, r + 3, c - 2, c + 3)
    if version >= 7:
        mark(0, 6, size - 11, size - 8), mark(size - 11, size - 8, 0, 6)

    path = []
    column, upward = size - 1, True
    while column > 0:
        if column == 6:
            column -= 1
        for r in (range(size - 1, -1, -1) if upward else range(size)):
            for c in (column, column - 1):
                if not function[r][c]:
                    path.append((r, c))
        column -= 2
        upward = not upward

    ec, groups = QR_BLOCKS_M[version]
    lengths = [data for count, data in groups for _ in range(count)]
    total = sum(lengths) + ec * len(lengths)
    assert len(path) // 8 == total, "the function-pattern map leaves the right number of modules"
    decoded = []
    for mask in QR_MASKS:
        bits = [module[r][c] ^ mask(r, c) for r, c in path]
        words = [int("".join("1" if b else "0" for b in bits[i:i + 8]), 2)
                 for i in range(0, total * 8, 8)]
        blocks = [[] for _ in lengths]
        cursor = 0
        for index in range(max(lengths)):
            for block, length in zip(blocks, lengths):
                if index < length:
                    block.append(words[cursor])
                    cursor += 1
        for _ in range(ec):
            for block in blocks:
                block.append(words[cursor])
                cursor += 1
        if not all(_syndromes_are_zero(block, ec) for block in blocks):
            continue
        stream = "".join(f"{byte:08b}" for block, length in zip(blocks, lengths)
                         for byte in block[:length])
        assert stream[:4] == "0100", "byte mode"
        width = 8 if version < 10 else 16
        count = int(stream[4:4 + width], 2)
        start = 4 + width
        decoded.append(bytes(int(stream[start + 8 * i:start + 8 * i + 8], 2)
                             for i in range(count)).decode("latin-1"))
    assert len(decoded) == 1, f"exactly one mask yields valid codewords, got {len(decoded)}"
    return decoded[0]


QR_URIS = [
    OTPAUTH,
    "otpauth://totp/Subfloor:jed?secret=GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ&issuer=Subfloor",
    "otpauth://totp/Subfloor:a.rather.longer.operator.name%40example?secret="
    "MFRGGZDFMZTWQ2LKNNWG23TPOBYXE43UOV3HO6DZ&issuer=Subfloor&algorithm=SHA1&digits=6&period=30",
]


def _encoder_matrix(text: str) -> list[str]:
    script = QRCODE_JS + r"""
const qr = qrcode(0, "M"); qr.addData(__TEXT__); qr.make();
const n = qr.getModuleCount(), rows = [];
for (let r = 0; r < n; r++) { let row = ""; for (let c = 0; c < n; c++) row += qr.isDark(r, c) ? "1" : "0"; rows.push(row); }
console.log(JSON.stringify({rows}));
"""
    return run_js(script.replace("__TEXT__", json.dumps(text)))["rows"]


@needs_node
@pytest.mark.parametrize("uri", QR_URIS)
def test_vendored_encoder_output_decodes_back_to_the_otpauth_uri(uri):
    rows = _encoder_matrix(uri)
    assert len({len(row) for row in rows}) == 1 and len(rows) == len(rows[0])
    assert decode_qr_level_m(rows) == uri


def test_the_decoder_rejects_a_damaged_symbol():
    if shutil.which("node") is None:
        pytest.skip("node is required")
    rows = _encoder_matrix(OTPAUTH)
    size = len(rows)
    flipped = list(rows)
    r, c = size - 2, size - 2          # a data module in the bottom-right corner
    flipped[r] = flipped[r][:c] + ("0" if flipped[r][c] == "1" else "1") + flipped[r][c + 1:]
    with pytest.raises(AssertionError):
        decode_qr_level_m(flipped)


@pytest.mark.skipif(shutil.which("node") is None or shutil.which("zbarimg") is None,
                    reason="node and zbarimg are required")
def test_a_stock_scanner_reads_the_code_when_one_is_installed(tmp_path):
    rows = _encoder_matrix(OTPAUTH)
    scale, quiet, n = 6, 4, len(rows)
    side = (n + 2 * quiet) * scale
    image = []
    for y in range(side):
        r = y // scale - quiet
        image.append(" ".join(
            "1" if 0 <= r < n and 0 <= x // scale - quiet < n and rows[r][x // scale - quiet] == "1"
            else "0" for x in range(side)))
    path = tmp_path / "code.pbm"
    path.write_text(f"P1\n{side} {side}\n" + "\n".join(image) + "\n")
    read = subprocess.run(["zbarimg", "-q", "--raw", str(path)], text=True, capture_output=True,
                          check=False)
    assert read.stdout.strip() == OTPAUTH


# ── Drawer: Account ───────────────────────────────────────────────────────────

ACCOUNT_HARNESS = SESSION_ENV + FAKE_DOM + API + ENGINE + ACCOUNT + r"""
const AUTHORITY = "http://127.0.0.1:8857";
const server = {
  authorityUp: true, reviewRoute: true,
  auth: {pinned: true, enrolled: true, signed_in: true, user: {user_id: 1, username: "operator"},
    session: {id: "s1", device: "desk", created_at: "2026-01-01T00:00:00Z",
      last_seen_at: "2026-01-05T00:00:00Z", current: true}},
  sessions: [
    {id: "s1", device: "desk", created_at: "2026-01-01T00:00:00Z", last_seen_at: "2026-01-05T00:00:00Z", current: true},
    {id: "s2", device: "phone", created_at: "2026-01-02T00:00:00Z", last_seen_at: "2026-01-04T00:00:00Z", current: false},
    {id: "s3", device: "", created_at: "2026-01-03T00:00:00Z", last_seen_at: "2026-01-03T00:00:00Z", current: false},
  ],
  sessionsStatus: 200, revoke: null, signOut: () => answer(200, {signed_in: false}),
};
const requests = [];
let confirmAnswer = true;
const confirmed = [];
accountConfirmRevoke = async (session) => { confirmed.push(session.id); return confirmAnswer; };
globalThis.fetch = async (url, init = {}) => {
  if (url === "/api/auth/status")
    return server.reviewRoute
      ? answer(200, {enrolled: true, signed_in: true, authority: {url: AUTHORITY, up: true}})
      : answer(404, {error: "not found"});
  const path = url.slice(AUTHORITY.length);
  requests.push({path, method: init.method || "GET", credentials: init.credentials,
    type: init.headers ? init.headers["Content-Type"] : null, body: init.body ?? null});
  if (!server.authorityUp) throw new TypeError("Failed to fetch");
  if (path === "/auth/status") return answer(200, server.auth);
  if (path === "/auth/sessions") return server.sessionsStatus === 200
    ? answer(200, {sessions: server.sessions})
    : answer(401, {error: {code: "sign_in_required", message: "sign in"}});
  if (path === "/auth/sign-out") return server.signOut();
  if (path === "/auth/sessions/revoke") return server.revoke(JSON.parse(init.body));
  throw new Error("unexpected " + url);
};
const txt = (node) => typeof node === "string" ? node : node.nodeType === 3 ? node.textContent
  : node.children.length ? node.children.map(txt).join("") : node.textContent;
const lines = (mount) => mount.children.map(txt);
const button = (mount, label, nth = 0) => all(mount, (n) => n.tagName === "BUTTON")
  .filter((n) => n.textContent === label)[nth];
const open = async () => { const mount = el("div"); const close = renderDrawerAccount(mount); await settle(); return [mount, close]; };
Date.prototype.toLocaleString = function () { return "<when>"; };
const dated = (text) => text;
"""


@needs_node
def test_account_section_lists_devices_and_signs_out():
    script = ACCOUNT_HARNESS + r"""
(async () => {
  const [mount] = await open();
  const out = {lines: lines(mount).map(dated),
    devices: all(mount, (n) => n.className === "account-device").map((row) => dated(txt(row))),
    labels: all(mount, (n) => n.tagName === "BUTTON").map((n) => n.attributes["aria-label"] || n.textContent),
    loads: requests.map((r) => [r.method, r.path, r.credentials])};
  await button(mount, "Sign out").onclick();
  await settle();
  out.signOut = requests[requests.length - 1];
  out.navigations = [...navigations];
  // A failed sign-out is reported in place.
  server.signOut = () => answer(500, {error: {code: "internal_error", message: "disk"}});
  const [second] = await open();
  await button(second, "Sign out").onclick(); await settle();
  out.failed = lines(second)[lines(second).length - 1];
  console.log(JSON.stringify(out));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    out = run_js(script)
    assert out["lines"][0] == "Signed in asoperatorThis devicedesk · since <when>"
    assert out["lines"][1:3] == ["Sign out", "Other devices"]
    assert out["devices"] == ["phoneLast seen <when>Revoke", "Unnamed deviceLast seen <when>Revoke"]
    assert out["labels"] == ["Sign out", "Revoke phone", "Revoke Unnamed device"]
    assert out["loads"] == [["GET", "/auth/status", "include"], ["GET", "/auth/sessions", "include"]]
    assert out["signOut"] == {"path": "/auth/sign-out", "method": "POST", "credentials": "include",
                              "type": "application/json", "body": "{}"}
    assert out["navigations"] == ["/signin.html"]
    assert out["failed"] == "Sign-out failed: disk"


@needs_node
def test_account_revoke_needs_confirmation_and_revoking_this_device_signs_out():
    script = ACCOUNT_HARNESS + r"""
(async () => {
  const out = {};
  const [mount] = await open();
  confirmAnswer = false;
  await button(mount, "Revoke").onclick(); await settle();
  out.declined = {confirmed: [...confirmed], posts: requests.filter((r) => r.method === "POST").length};
  confirmAnswer = true;
  server.revoke = ({id}) => { server.sessions = server.sessions.filter((s) => s.id !== id);
    return answer(200, {revoked: true, current: false}); };
  await button(mount, "Revoke").onclick(); await settle();
  out.revoked = {post: requests.filter((r) => r.method === "POST")[0],
    devices: all(mount, (n) => n.className === "account-device").length, navigations: [...navigations]};
  // The authority says the revoked session was this one: leave for sign-in.
  server.revoke = () => answer(200, {revoked: true, current: true});
  await button(mount, "Revoke").onclick(); await settle();
  out.self = [...navigations];
  // Refused: reported in place, list re-read.
  navigations.length = 0;
  server.sessions.push({id: "s9", device: "tablet", last_seen_at: "2026-01-04T00:00:00Z", current: false});
  const [second] = await open();
  server.revoke = () => answer(404, {error: {code: "session_not_found", message: "not one of yours"}});
  await button(second, "Revoke").onclick(); await settle();
  out.refused = lines(second)[lines(second).length - 1];
  console.log(JSON.stringify(out));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    out = run_js(script)
    assert out["declined"] == {"confirmed": ["s2"], "posts": 0}
    assert out["revoked"]["post"] == {
        "path": "/auth/sessions/revoke", "method": "POST", "credentials": "include",
        "type": "application/json", "body": '{"id":"s2"}'}
    assert out["revoked"]["devices"] == 1 and out["revoked"]["navigations"] == []
    assert out["self"] == ["/signin.html"]
    assert out["refused"] == "Revoke failed: not one of yours"


@needs_node
@pytest.mark.parametrize("setup,expected", [
    ("server.authorityUp = false;", {"down": True, "navigations": [], "buttons": 0}),
    ("server.reviewRoute = false;", {"down": True, "navigations": [], "buttons": 0}),
    # the authority no longer accepts this browser: to the sign-in screen, with next
    ("server.auth = {...server.auth, signed_in: false, user: null, session: null};",
     {"down": False, "navigations": [NEXT], "buttons": 0}),
    ("server.sessionsStatus = 401;", {"down": False, "navigations": [NEXT], "buttons": 0}),
])
def test_account_section_without_the_authority_or_the_session(setup, expected):
    script = ACCOUNT_HARNESS + setup + r"""
(async () => {
  const [mount] = await open();
  console.log(JSON.stringify({text: lines(mount).join(" | "), navigations,
    buttons: all(mount, (n) => n.tagName === "BUTTON").length}));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = run_js(script)
    assert result["navigations"] == expected["navigations"]
    assert result["buttons"] == expected["buttons"]
    if expected["down"]:
        assert "The host authority is not running" in result["text"]
        assert "./sc launch" in result["text"] and "./sc auth sessions" in result["text"]
    else:
        assert result["text"] == "Loading…"


@needs_node
def test_closing_the_drawer_stops_the_account_section_painting():
    script = ACCOUNT_HARNESS + r"""
(async () => {
  const mount = el("div");
  const close = renderDrawerAccount(mount);
  close();
  await settle();
  console.log(JSON.stringify({text: lines(mount), isFunction: typeof close === "function"}));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    assert run_js(script) == {"text": ["Loading…"], "isFunction": True}


@needs_node
def test_revoke_confirmation_answers_on_every_way_out():
    script = SESSION_ENV + FAKE_DOM + ACCOUNT + r"""
let modal = null, closes = 0;
function openActionModal(options) { modal = options; return () => { closes += 1; }; }
const txt = (node) => node.children.length ? node.children.map((c) => c.textContent).join("") : node.textContent;
(async () => {
  const session = {id: "s2", device: "phone", last_seen_at: "2026-01-04T00:00:00Z"};
  const yes = accountConfirmRevoke(session);
  const shown = {title: modal.title, text: modal.bodyNode.children.map(txt).join(" "),
    action: modal.actionNode.className};
  modal.actionNode.onclick();
  const no = accountConfirmRevoke(session);
  modal.dismissNode.onclick();
  console.log(JSON.stringify({shown, answers: [await yes, await no], closes}));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = run_js(script)
    assert result["answers"] == [True, False] and result["closes"] == 2
    assert result["shown"]["title"] == "Revoke device"
    assert result["shown"]["action"] == "act primary danger"
    assert "Revoke phone" in result["shown"]["text"]
    assert "returned to the sign-in screen on its next request" in result["shown"]["text"]


def test_account_is_the_fifth_drawer_section_and_uses_the_close_hook():
    assert '["drawer-account", renderDrawerAccount],' in DRAWER_BLOCK
    account = function_source("renderDrawerAccount")
    assert "return () => { closed = true; };" in account
    assert 'engineOpsRequest(state.base, "/auth/sign-out", "POST", {})' in account
    assert '"/auth/sessions/revoke", "POST", { id: session.id }' in account
    assert "fetch(" not in ACCOUNT.replace("sessionFetch(", ""), "the authority is reached one way"


# ── Engine controls and the session ───────────────────────────────────────────

@needs_node
def test_engine_section_without_a_session_goes_to_sign_in():
    script = ENGINE_HARNESS + r"""
server.auth = {...server.auth, signed_in: false, user: null, session: null};
(async () => {
  const mount = await open();
  finish({navigations, paths: serviceRequests.map((r) => r.path), banner: snap(mount).banner});
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    assert run_js(script) == {
        "navigations": ["/signin.html?next=%2F%23shells"],
        "paths": ["/auth/status"],            # /status, which needs the session, is never asked
        "banner": "Loading…",
    }


@needs_node
def test_engine_operation_refused_for_a_lost_session_goes_to_sign_in():
    script = ENGINE_HARNESS + r"""
server.post = () => json(401, SIGN_IN_REQUIRED);
(async () => {
  const mount = await open();
  press(mount, "Restart engine");
  await settle();
  finish({navigations, ticks, notices,
    posts: serviceRequests.filter((r) => r.method === "POST").map((r) => r.path)});
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    assert run_js(script) == {
        "navigations": ["/signin.html?next=%2F%23shells"], "ticks": 0, "notices": [],
        "posts": ["/ops/restart"],
    }


@needs_node
def test_engine_section_names_the_pin_verb_when_no_origin_is_pinned():
    script = ENGINE_HARNESS + r"""
(async () => {
  const out = {};
  server.auth = {ok: true, pinned: false, enrolled: false, signed_in: false,
    reason: "No GUI origin is pinned.", setup_cli: "./sc auth user add <username>"};
  out.unpinned = snap(await open());
  // Pinned and signed in, but the pin is withdrawn before the operation starts.
  server.auth = {ok: true, pinned: true, enrolled: true, signed_in: true};
  const mount = await open();
  server.post = () => json(409, {error: {code: "gui_origin_unpinned", message: "No GUI origin is pinned.",
    details: {pin_cli: "./sc auth origin", setup_cli: "./sc auth user add <username>"}}});
  await press(mount, "Update harnesses"); await settle();
  out.refused = snap(mount).banner;
  // The authority refuses this page's origin outright.
  server.auth = null;
  globalThis.fetch = ((inner) => async (url, init) => url.endsWith("/auth/status")
    ? json(403, {error: {code: "same_origin_required", message: "wrong origin", details: {}}})
    : inner(url, init))(globalThis.fetch);
  out.wrongOrigin = snap(await open());
  finish({out, navigations});
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = run_js(script)
    out = result["out"]
    assert out["unpinned"]["banner"] == (
        "No GUI origin is pinned. On the host, run ./sc auth origin "
        "(or ./sc auth user add <username> if no GUI user exists yet).")
    assert [c["disabled"] for c in out["unpinned"]["controls"]] == [True, True, True]
    assert [c["cli"] for c in out["unpinned"]["controls"]] == [
        "./sc update", "./sc restart", "./sc update-harnesses"]
    assert out["refused"] == ("update-harnesses was not started: gui_origin_unpinned: "
                              "No GUI origin is pinned. On the host, run ./sc auth origin.")
    assert out["wrongOrigin"]["banner"] == (
        "The host authority refused this page: same_origin_required: wrong origin")
    assert [c["disabled"] for c in out["wrongOrigin"]["controls"]] == [True, True, True]
    assert result["navigations"] == []


def test_engine_operations_ask_for_no_authenticator_code():
    # FnB ruling: a signed-in session plus the typed confirmation is the rule.
    confirm = function_source("engineConfirm")
    assert "one-time-code" not in ENGINE and "totp" not in ENGINE.lower()
    assert "authenticator" not in ENGINE.lower()
    assert "const typed = Boolean(facts.stops_engine);" in confirm


# ── Closing the drawer withdraws an unsent confirmation ───────────────────────

@needs_node
@pytest.mark.parametrize("op,label", [("update", "Update engine"), ("restart", "Restart engine"),
                                      ("update-harnesses", "Update harnesses")])
@pytest.mark.parametrize("phase", ["confirmation", "revalidation"])
def test_closing_the_drawer_before_an_operation_is_sent_posts_nothing(op, label, phase):
    script = ENGINE_HARNESS + r"""
const PHASE = "__PHASE__";
server.post = () => json(202, {started: true, operation: running("__OP__", "2026-06-06T00:00:00Z")});
(async () => {
  const mount = el("div");
  const cleanup = renderDrawerEngine(mount);
  await settle();
  const before = snap(mount);
  let armed = false;
  // The operator confirms, then closes the drawer: either while the dialog's
  // answer is being delivered, or while the status is re-read before the POST.
  engineConfirm = async (name) => { confirms.push(name); if (PHASE === "confirmation") cleanup(); else armed = true; return true; };
  const inner = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    if (armed && url === "/api/engine-ops") { armed = false; cleanup(); }
    return inner(url, init);
  };
  press(mount, "__LABEL__");
  await settle();
  finish({confirms, posts: serviceRequests.filter((r) => r.method === "POST").map((r) => r.path),
    unchanged: JSON.stringify(snap(mount)) === JSON.stringify(before), ticks, navigations});
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = run_js(script.replace("__PHASE__", phase).replace("__OP__", op).replace("__LABEL__", label))
    assert result == {"confirms": [op], "posts": [], "unchanged": True, "ticks": 0, "navigations": []}


@needs_node
def test_closing_the_drawer_before_a_revoke_is_sent_posts_nothing():
    script = ACCOUNT_HARNESS + r"""
(async () => {
  const [mount, close] = await open();
  accountConfirmRevoke = async (session) => { confirmed.push(session.id); close(); return true; };
  server.revoke = () => answer(200, {revoked: true, current: false});
  await button(mount, "Revoke").onclick(); await settle();
  console.log(JSON.stringify({confirmed, posts: requests.filter((r) => r.method === "POST").length,
    devices: all(mount, (n) => n.className === "account-device").length}));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    assert run_js(script) == {"confirmed": ["s2"], "posts": 0, "devices": 2}


def test_no_wait_separates_a_confirmation_from_its_post_without_a_closed_check():
    # Sign-out has no wait before its POST; revoke and the engine operations
    # re-check `closed` after each one.
    request = ENGINE[ENGINE.index("  const request = async (op, label, view) => {"):
                     ENGINE.index("  paint({ discovery: null, status: null }")]
    before_post = request[:request.index("    try { answer = await engineOpsRequest(url")]
    waits = [line for line in before_post.splitlines() if "await " in line]
    assert waits == [
        "    if (!await engineConfirm(op, label, view.ops[op], view) || closed) return;",
        "    const state = await engineOpsLoad();",
    ]
    assert "    const state = await engineOpsLoad();\n    if (closed) return;" in before_post
    account = function_source("renderDrawerAccount")
    assert "if (!await accountConfirmRevoke(session) || closed) return;" in account
    sign_out = account[account.index("signOut.onclick = async () => {"):account.index("const others =")]
    assert sign_out.index("await engineOpsRequest(state.base, \"/auth/sign-out\"") \
        == sign_out.index("await "), "nothing is awaited before the sign-out request"
