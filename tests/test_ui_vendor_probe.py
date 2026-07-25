#!/usr/bin/env python3
"""Honest failure reporting when the terminal globals are missing — spec #48
(sprint 45, unit 11).

The guard this replaces knew one fact ("Terminal is not defined") and asserted
a different one ("still loading — refresh the page"). Both worlds produce the
same undefined global: the genuine defer race, where a refresh is beside the
point because waiting works, and a vendor script the running server never
served, where no number of refreshes will ever define it. The operator in the
2026-07-25 outage was handed the refresh remedy for the second world and spent
the incident acting on it.

So the properties here are about what the operator is TOLD, not about whether
the terminal mounts — the code did the right thing and said the wrong thing,
which is the failure mode a "did it survive" assertion cannot see.

Driven through node against the real `ifOpenStream`/`ifDiagnoseVendor` source
sliced out of ui/app.js, with fetch, the DOM enumeration and the terminal
globals as the injected seams. The bound is exercised in real time (~2s) with
a watchdog, so removing it fails the run instead of hanging it.

Run:
    python3 -m pytest tests/test_ui_vendor_probe.py
"""
from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"

APP = (ENGINE / "ui" / "app.js").read_text()
INDEX = (ENGINE / "ui" / "index.html").read_text()
EL = APP[APP.index("const el ="):APP.index("const esc =")]
DETACH = APP[APP.index("function ifDetach()"):APP.index("window.addEventListener(\"pagehide\"")]
VENDOR = APP[APP.index("// The terminal globals are undefined in two"):
             APP.index("function ifSendInput")]

HAS_NODE = shutil.which("node") is not None

HARNESS = r"""
globalThis.ifAttach = null;

class FakeElement {
  constructor(tag) {
    this.tagName = tag; this.nodeType = 1; this.children = []; this.style = {};
    this._text = "";
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  set textContent(value) { this._text = String(value ?? ""); }
  get textContent() { return this._text; }
}

// The four vendor scripts index.html declares, in its order.
let scriptSrcs = ["/vendor/xterm/xterm.js", "/vendor/xterm/addon-fit.js",
                  "/vendor/marked.umd.js", "/vendor/purify.min.js"];
let selectorsAsked = [];
globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (text) => ({ nodeType: 3, textContent: String(text ?? "") }),
  querySelectorAll: (selector) => {
    selectorsAsked.push(selector);
    return scriptSrcs.map((src) => ({
      getAttribute: (name) => (name === "src" ? src : null) }));
  },
};

// Every probe the code makes, with the options it made it with.
let probes = [];
let respond = async () => ({ status: 200 });
globalThis.fetch = (src, options) => {
  probes.push({ src, ...options });
  return respond(src, options);
};

const sockets = [];
globalThis.WebSocket = class {
  constructor(url, protocol) { this.url = url; this.protocol = protocol;
    this.readyState = 0; sockets.push(this); }
  send() {} close() { this.readyState = 3; }
};
globalThis.WebSocket.OPEN = 1;
globalThis.ResizeObserver = class { observe() {} disconnect() {} };
globalThis.location = { protocol: "http:", host: "sc.test" };

const TERMINAL = class {
  constructor() { this.cols = 80; this.rows = 24; }
  loadAddon() {} open() {} write() {} reset() {} dispose() {}
  resize() {} onData() {} onResize() {}
};
const FIT = { FitAddon: class { proposeDimensions() { return { cols: 80, rows: 24 }; } } };
function defineGlobals() { globalThis.Terminal = TERMINAL; globalThis.FitAddon = FIT; }
function undefineGlobals() { delete globalThis.Terminal; delete globalThis.FitAddon; }

function invariant(ok, message) { if (!ok) throw new Error(message); }
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

// A fresh attachment, current by construction.
function attach() {
  const a = {
    sessionId: 7, role: "viewer", ws: null, term: null, heartbeat: 0,
    resizeObs: null, vendorRetry: null, seq: 1, inflight: 0, awaiting: false,
    halted: false, outBuf: "", composerPendingSeq: null,
    st: { note: "", writer: "active" },
    termEl: new FakeElement("div"),
    notes: [],
  };
  a.paint = () => a.notes.push(a.st.note);
  ifAttach = a;
  return a;
}

// The terminal notes are the assertion surface, so wait on the note rather
// than on a timer — and give up loudly, because "it never settled" is a
// result this suite has to be able to report rather than hang on.
async function settle(a, pattern, budgetMs = 6000) {
  const deadline = Date.now() + budgetMs;
  while (Date.now() < deadline) {
    if (pattern.test(a.st.note)) return a.st.note;
    await sleep(20);
  }
  throw new Error("no note matching " + pattern + " within " + budgetMs +
    "ms — last note was " + JSON.stringify(a.st.note));
}

function rounds() { return probes.length / scriptSrcs.length; }

function reset() {
  probes = []; selectorsAsked = []; sockets.length = 0;
  scriptSrcs = ["/vendor/xterm/xterm.js", "/vendor/xterm/addon-fit.js",
                "/vendor/marked.umd.js", "/vendor/purify.min.js"];
  respond = async () => ({ status: 200 });
  undefineGlobals();
  ifAttach = null;
}
"""


def run_js(body: str, timeout: int = 60) -> str:
    script = (EL + VENDOR + HARNESS +
              "(async () => {\nreset();\n" + body + "\n})().then(() => "
              "process.exit(0), (error) => {\n"
              "  console.error(error.stack || error);\n"
              "  process.exit(1);\n});\n")
    result = subprocess.run(["node", "-e", script], text=True,
                            capture_output=True, check=False, timeout=timeout)
    assert result.returncode == 0, result.stderr
    return result.stdout


@unittest.skipUnless(HAS_NODE, "node is required for the browser contract")
class VendorProbeTest(unittest.TestCase):

    def test_a_missing_vendor_script_is_named_with_its_status(self):
        """The outage, reported honestly: the note carries the src and the
        status the server actually gave, and sends the operator at the floor
        rather than at the browser."""
        run_js(r"""
        respond = async (src) => ({ status: src.endsWith("addon-fit.js") ? 404 : 200 });
        const a = attach();
        ifOpenStream(a, "ticket-1");
        const note = await settle(a, /returned/);
        invariant(note.includes("/vendor/xterm/addon-fit.js returned 404"),
          "the note did not name the failed script and its status: " + note);
        invariant(/restart/.test(note), "the note never says what would help: " + note);
        invariant(!/refresh/i.test(note),
          "prescribed a refresh for a fault no refresh can fix: " + note);
        invariant(sockets.length === 0, "opened a stream with no terminal");
        invariant(a.vendorRetry === null, "kept retrying a 404");
        invariant(rounds() === 1, "waited out the bound before reporting a 404");
        """)

    def test_the_probe_asks_the_server_rather_than_the_cache(self):
        """A cached answer would describe the page load, not the floor — and
        HEAD is what keeps the diagnosis from re-downloading 1.2MB of xterm."""
        run_js(r"""
        respond = async () => ({ status: 404 });
        const a = attach();
        ifOpenStream(a, "ticket-1");
        await settle(a, /returned/);
        invariant(probes.length === 4, "probed " + probes.length + " of 4 scripts");
        for (const probe of probes) {
          invariant(probe.method === "HEAD", "probe was not a HEAD: " + probe.method);
          invariant(probe.cache === "no-store", "probe read the cache: " + probe.cache);
        }
        invariant(selectorsAsked.every((s) => s === 'script[src^="/vendor/"]'),
          "enumerated something other than the page's vendor scripts: " + selectorsAsked);
        """)

    def test_every_failed_script_is_named_not_just_the_first(self):
        run_js(r"""
        respond = async (src) => ({ status: src.includes("xterm") ? 503 : 200 });
        const a = attach();
        ifOpenStream(a, "ticket-1");
        const note = await settle(a, /returned/);
        invariant(note.includes("/vendor/xterm/xterm.js returned 503") &&
                  note.includes("/vendor/xterm/addon-fit.js returned 503"),
          "only part of the failure was reported: " + note);
        """)

    def test_an_old_floor_that_cannot_answer_the_probe_is_reported_honestly(self):
        """The degradation this design has to survive (PLN1's ruling on
        result #1873): the probe runs in a browser talking to whatever server
        is RUNNING, and a server predating this unit has no HEAD route for
        /vendor/ — it answers 405 for scripts it serves perfectly over GET.

        The report is still true there, which is the whole point: a floor that
        405s the probe IS older than the page that made it, and the remedy it
        names — restart the floor — is exactly the one that fixes it. Nothing
        here may soften a 405 into "still loading".
        """
        run_js(r"""
        respond = async () => ({ status: 405 });   // a floor without do_HEAD
        const a = attach();
        ifOpenStream(a, "ticket-1");
        const note = await settle(a, /returned/);
        invariant(note.includes("/vendor/xterm/xterm.js returned 405"),
          "an old floor's 405 was not reported: " + note);
        invariant(/older than this page/.test(note) && /restart/.test(note),
          "the note did not point at the floor: " + note);
        invariant(!/refresh/i.test(note), "prescribed a refresh: " + note);
        invariant(rounds() === 1, "retried against a floor that cannot answer");
        """)

    def test_a_script_that_serves_but_defines_nothing_terminates_within_the_bound(self):
        """A corrupt or empty vendor file is 200 forever, so the retry has to
        end by itself. An unbounded retry is the same lie in another costume:
        a permanent spinner that never says anything false and never says
        anything at all."""
        run_js(r"""
        const a = attach();
        const started = Date.now();
        ifOpenStream(a, "ticket-1");
        const note = await settle(a, /did not define/);
        const elapsed = Date.now() - started;
        invariant(note.includes("served but did not define Terminal and FitAddon"),
          "the note did not say what was served or what stayed undefined: " + note);
        invariant(!/refresh/i.test(note), "prescribed a refresh: " + note);
        invariant(rounds() === 8, "bound was " + rounds() + " attempts, not 8");
        invariant(elapsed < 5000, "the bound took " + elapsed + "ms");
        // Terminal means terminal: nothing keeps running after the verdict.
        const settled = probes.length;
        await sleep(600);
        invariant(probes.length === settled, "kept probing after reporting");
        invariant(a.vendorRetry === null, "left a retry armed");
        """, timeout=90)

    def test_the_load_race_resolves_itself_without_asking_the_operator(self):
        """The world the old message was right about — and it still should not
        delegate to a human, because waiting is the entire remedy."""
        run_js(r"""
        const a = attach();
        respond = async () => {
          if (rounds() >= 2) defineGlobals();     // the deferred scripts execute
          return { status: 200 };
        };
        ifOpenStream(a, "ticket-1");
        const deadline = Date.now() + 4000;
        while (!sockets.length && Date.now() < deadline) await sleep(20);
        invariant(sockets.length === 1, "the attach never recovered on its own");
        invariant(!/refresh/i.test(a.notes.join(" | ")),
          "asked the operator to refresh during a race that fixed itself: " + a.notes);
        invariant(!/returned|did not define/.test(a.st.note),
          "reported a fault after recovering: " + a.st.note);
        """)

    def test_a_probe_landing_after_the_operator_moved_on_does_nothing(self):
        """The attach can end while the HEADs are in flight, and what the dead
        attachment must not do is CONTINUE — no repaint of a pane nobody is
        looking at, and above all no next attempt queued against it.

        Split from the throwing case below on purpose: the guard before the
        paint and the guard after the await are independently sufficient for a
        note, so a single test kept BOTH mutations green. The retry is what
        only the post-await guard prevents.
        """
        run_js(r"""
        const held = [];
        respond = () => new Promise((resolve) => held.push(resolve));
        const stale = attach();
        ifOpenStream(stale, "ticket-1");
        await sleep(50);
        const staleNote = stale.st.note;
        const staleNotes = stale.notes.length;
        // The operator selects another shell: a newer attachment is current.
        const current = attach();
        current.st.note = "attached";
        held.forEach((resolve) => resolve({ status: 200 }));
        await sleep(80);
        invariant(stale.vendorRetry === null,
          "a probe queued another attempt against an attachment already gone");
        invariant(stale.st.note === staleNote,
          "a probe repainted an attachment that was already gone: " + stale.st.note);
        invariant(stale.notes.length === staleNotes, "repainted the stale pane");
        invariant(current.st.note === "attached",
          "the stale probe overwrote the live pane: " + current.st.note);
        """)

    def test_a_probe_that_throws_after_the_operator_moved_on_repaints_nothing(self):
        """The failure path reaches the note without passing the post-await
        check, so this is the one the paint's own guard has to catch."""
        run_js(r"""
        let reject;
        respond = () => new Promise((_, no) => { reject = no; });
        const stale = attach();
        ifOpenStream(stale, "ticket-1");
        await sleep(50);
        const staleNote = stale.st.note;
        const current = attach();
        current.st.note = "attached";
        reject(new Error("Failed to fetch"));
        await sleep(150);
        invariant(stale.st.note === staleNote,
          "a failed probe repainted an attachment already gone: " + stale.st.note);
        invariant(current.st.note === "attached",
          "the stale probe overwrote the live pane: " + current.st.note);
        """)

    def test_a_queued_attempt_dies_with_the_attachment(self):
        """The attach can also end BETWEEN attempts, with the timer already
        armed — spec #48's "cancelled on detach". The armed callback is the
        last thing standing between a dropped pane and a stream opened against
        a session the operator left."""
        run_js(r"""
        const a = attach();
        ifOpenStream(a, "ticket-1");
        await sleep(80);
        invariant(a.vendorRetry !== null, "no attempt was queued to cancel");
        const probed = probes.length;
        ifAttach = null;                    // the operator navigates away
        await sleep(500);                   // past two retry intervals
        invariant(probes.length === probed,
          "a detached attachment kept probing: " + probes.length + " > " + probed);
        invariant(sockets.length === 0, "opened a stream for a dead attachment");
        """)

    def test_a_probe_that_throws_names_the_error_and_claims_nothing_else(self):
        run_js(r"""
        respond = async () => { throw new Error("Failed to fetch"); };
        const a = attach();
        ifOpenStream(a, "ticket-1");
        const note = await settle(a, /could not check/);
        invariant(note.includes("Failed to fetch"), "swallowed the error: " + note);
        invariant(!/refresh/i.test(note), "prescribed a refresh: " + note);
        invariant(!/returned|did not define|restart/.test(note),
          "claimed a diagnosis it could not make: " + note);
        """)

    def test_a_page_with_no_vendor_scripts_says_exactly_that(self):
        run_js(r"""
        scriptSrcs = [];
        const a = attach();
        ifOpenStream(a, "ticket-1");
        const note = await settle(a, /declares no/);
        invariant(probes.length === 0, "probed a script list it does not have");
        invariant(!/refresh/i.test(note), "prescribed a refresh: " + note);
        """)


class VendorEnumerationTest(unittest.TestCase):
    """The probe reads the page instead of a second list — so the check that
    matters is that the page and the selector still describe each other. (The
    real DOM match is proven in the browser by
    tests/test_interface_ui_rendered.py.)"""

    def test_the_selector_matches_the_script_tags_the_shell_declares(self):
        self.assertIn('script[src^="/vendor/"]', APP)
        srcs = re.findall(r'<script src="([^"]+)"', INDEX)
        vendored = [s for s in srcs if s.startswith("/vendor/")]
        self.assertGreaterEqual(len(vendored), 2, srcs)
        self.assertIn("/vendor/xterm/xterm.js", vendored)
        self.assertIn("/vendor/xterm/addon-fit.js", vendored)

    def test_detach_clears_a_queued_attempt(self):
        """Belt to the armed callback's braces: teardown drops the timer the
        way it drops the heartbeat, so nothing is left pending on a pane the
        operator closed. Asserted against the source because ifDetach's other
        work (lease DELETE, terminal dispose) is out of this harness's
        reach."""
        self.assertIn("clearTimeout(a.vendorRetry)", DETACH)

    def test_the_old_remedy_is_gone_from_the_shell(self):
        """It said `refresh the page` for a route-table miss no refresh can
        clear. Nothing in the attach path may say it again."""
        self.assertNotIn("still loading — refresh the page", APP)
        # No note this path can paint may carry it either — the word survives
        # in the comments, which is where the reasoning belongs.
        self.assertEqual(re.findall(r'"[^"]*refresh[^"]*"', VENDOR, re.I), [])


if __name__ == "__main__":
    unittest.main()
