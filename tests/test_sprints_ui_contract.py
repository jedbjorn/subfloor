"""Sprints route shell and refresh lifecycle, driven through the real app.js."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / ".super-coder" / "ui" / "app.js").read_text()
INDEX = (ROOT / ".super-coder" / "ui" / "index.html").read_text()
STYLE = (ROOT / ".super-coder" / "ui" / "style.css").read_text()
EL = APP[APP.index("const el ="):APP.index("const esc =")]
SPRINTS = APP[APP.index("// ── Active sprints"):
              APP.index("// ── Tabs + boot")]
ROUTER_AT = APP.index("function routeFromHash()")
ROUTER = APP[ROUTER_AT:
             APP.index('document.querySelectorAll("nav button").forEach',
                       ROUTER_AT)]
SHOW = APP[APP.index("function show(tab)"):
           APP.index("// Hash routing:", APP.index("function show(tab)"))]

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required"
)


HARNESS = r"""
class FakeElement {
  constructor(tag) {
    this.tagName = tag; this.nodeType = 1; this.children = [];
    this._text = ""; this.className = ""; this.title = "";
    this.dateTime = ""; this.isConnected = true; this.hidden = false;
    this.dataset = {};
    this.classList = {
      add: (name) => this._setClass(name, true),
      remove: (name) => this._setClass(name, false),
      toggle: (name, force) => {
        const has = this.className.split(" ").filter(Boolean).includes(name);
        const next = force === undefined ? !has : Boolean(force);
        this._setClass(name, next);
        return next;
      },
      contains: (name) => this.className.split(" ").filter(Boolean).includes(name),
    };
  }
  _setClass(name, on) {
    const names = new Set(this.className.split(" ").filter(Boolean));
    if (on) names.add(name); else names.delete(name);
    this.className = [...names].join(" ");
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; this._text = ""; }
  set textContent(value) { this._text = String(value ?? ""); this.children = []; }
  get textContent() {
    return this._text + this.children.map(
      (child) => typeof child === "string" ? child : child.textContent
    ).join("");
  }
}
const navButton = new FakeElement("button");
navButton.textContent = "Sprints";
navButton.hidden = true;
navButton.dataset.tab = "sprints";
const listeners = new Map();
const intervals = [];
const cleared = [];
globalThis.document = {
  hidden: false,
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (text) => ({ nodeType: 3, textContent: String(text ?? "") }),
  querySelector: (selector) =>
    selector === 'nav button[data-tab="sprints"]' ? navButton : null,
  addEventListener: (name, fn) => listeners.set(name, fn),
  removeEventListener: (name, fn) => {
    if (listeners.get(name) === fn) listeners.delete(name);
  },
};
globalThis.setInterval = (fn, ms) => {
  const id = intervals.length + 1;
  intervals.push({ id, fn, ms });
  return id;
};
globalThis.clearInterval = (id) => cleared.push(id);
let apiQueue = [];
let apiCalls = [];
async function api(path) {
  apiCalls.push(path);
  const next = apiQueue.shift();
  if (next instanceof Error) throw next;
  return next;
}
function all(root, predicate, found = []) {
  if (predicate(root)) found.push(root);
  for (const child of root.children || [])
    if (child?.nodeType === 1) all(child, predicate, found);
  return found;
}
const byClass = (root, name) => all(
  root, (node) => node.className.split(" ").filter(Boolean).includes(name));
const byTag = (root, tag) => all(root, (node) => node.tagName === tag);
const makeRoot = () => new FakeElement("div");
const out = (value) => console.log(JSON.stringify(value));
"""


def payload(*, count=1, started_at="2026-07-26T20:00:00Z", units=None):
    return {
        "active_count": count,
        "sprints": [] if count == 0 else [{
            "document_id": 77,
            "title": "SPRINT: Active sprint flow board",
            "started_at": started_at,
            "planner": {"shell_id": 10, "shortname": "PLN2"},
            "feature": {"feature_id": 33, "title": "Active sprint flow board"},
            "units": [{"seq": "U3"}] if units is None else units,
        }],
    }


def run_js(body: str, *, prelude: str = "", suffix: str = "") -> dict:
    script = (
        EL + HARNESS + prelude + SPRINTS + suffix
        + "\n(async () => {\n" + body
        + "\n})().catch((error) => { console.error(error.stack || error);"
          " process.exit(1); });\n"
    )
    proc = subprocess.run(
        ["node", "-e", script], text=True, capture_output=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_nav_order_route_and_section_are_reload_safe():
    nav = INDEX[INDEX.index("<nav>"):INDEX.index("</nav>")]
    assert nav.index('data-tab="shells"') < nav.index('data-tab="sprints"')
    assert nav.index('data-tab="sprints"') < nav.index('data-tab="roadmap"')
    assert nav.index('data-tab="roadmap"') < nav.index('data-tab="docs"')
    assert 'data-tab="sprints" hidden' in nav
    assert INDEX.count('id="view-sprints"') == 1
    assert "nav button.warn { color: var(--warn); }" in STYLE

    script = r"""
globalThis.location = { hash: "#sprints" };
let shellTab = "harness", roadmapView = null, anView = "tokens", ifSelected = null;
const SHELL_TAB_HASH = {};
const VIEWS = { sprints: 1, shells: 1, roadmap: 1, analytics: 1, interface: 1 };
const shown = [];
function show(tab) { shown.push(tab); }
""" + ROUTER + r"""
routeFromHash();
console.log(JSON.stringify({ tab: shown.at(-1) }));
"""
    proc = subprocess.run(
        ["node", "-e", script], text=True, capture_output=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["tab"] == "sprints"


def test_authoritative_count_and_headers_render_without_controls():
    data = payload(count=3)
    data["sprints"][0]["title"] = "sprint: rendered verbatim"
    data["sprints"].append({
        "document_id": 78,
        "title": "SPRINT: second board",
        "started_at": None,
        "planner": None,
        "feature": None,
        "units": [],
    })
    result = run_js(
        """
apiQueue = [DATA];
await sprintsRefresh({ render: false });
const root = makeRoot();
await renderSprints(root);
const time = byTag(root, "time")[0];
out({
  nav: navButton.textContent,
  warn: navButton.classList.contains("warn"),
  hidden: navButton.hidden,
  text: root.textContent,
  titles: byClass(root, "sprint-board").map((board) => board.textContent),
  boards: byClass(root, "sprint-board").length,
  flows: byClass(root, "sprint-flow").length,
  controls: byTag(root, "button").length + byTag(root, "input").length
    + byTag(root, "select").length + byTag(root, "textarea").length,
  utc: time.title,
});
""",
        prelude="const DATA = " + json.dumps(data) + ";\n",
    )
    assert result["nav"] == "Sprints 3"
    assert result["warn"] is True
    assert result["hidden"] is False
    assert "sprint: rendered verbatim" in result["text"]
    assert "Doc #77" in result["text"]
    assert "Planner: PLN2" in result["text"]
    assert "Feature: #33 Active sprint flow board" in result["text"]
    assert "Planner: Unbound" in result["text"]
    assert "Feature: Unlinked" in result["text"]
    assert "Started:" in result["text"] and "Running:" in result["text"]
    assert result["boards"] == result["flows"] == 2
    assert result["titles"][0].startswith("sprint: rendered verbatim")
    assert result["titles"][1].startswith("SPRINT: second board")
    assert result["controls"] == 0
    assert result["utc"] == "2026-07-26T20:00:00Z"


def test_zero_state_and_initial_failure_are_distinct():
    zero = run_js(
        """
apiQueue = [DATA];
await sprintsRefresh({ render: false });
const root = makeRoot();
await renderSprints(root);
out({ text: root.textContent, nav: navButton.textContent,
      warn: navButton.classList.contains("warn"), hidden: navButton.hidden });
""",
        prelude="const DATA = " + json.dumps(payload(count=0)) + ";\n",
    )
    assert zero == {
        "text": "No active sprints.", "nav": "Sprints", "warn": False,
        "hidden": False,
    }

    failed = run_js(
        """
apiQueue = [new Error("offline")];
await sprintsRefresh({ render: false });
const root = makeRoot();
await renderSprints(root);
out({ text: root.textContent, nav: navButton.textContent,
      warn: navButton.classList.contains("warn"), hidden: navButton.hidden,
      title: navButton.title });
"""
    )
    assert failed == {
        "text": "error: offline", "nav": "Sprints", "warn": False,
        "hidden": False, "title": "Active sprint count unavailable",
    }

    empty_error = run_js(
        """
apiQueue = [new Error("")];
await sprintsRefresh({ render: false });
const root = makeRoot();
await renderSprints(root);
out({ text: root.textContent });
"""
    )
    assert empty_error["text"] == "error: request failed"

    malformed = run_js(
        """
apiQueue = [{}];
await sprintsRefresh({ render: false });
const root = makeRoot();
await renderSprints(root);
out({ text: root.textContent });
"""
    )
    assert malformed["text"] == "error: invalid active sprint projection"


def test_refresh_failure_retains_payload_and_marks_it_stale():
    result = run_js(
        """
apiQueue = [DATA, new Error("timeout")];
await sprintsRefresh({ render: false });
const root = makeRoot();
await renderSprints(root);
await sprintsRefresh();
out({ text: root.textContent, nav: navButton.textContent,
      boards: byClass(root, "sprint-board").length });
""",
        prelude="const DATA = " + json.dumps(payload()) + ";\n",
    )
    assert "Stale — refresh failed: timeout" in result["text"]
    assert "SPRINT: Active sprint flow board" in result["text"]
    assert result["boards"] == 1
    assert result["nav"] == "Sprints 1"


def test_null_timestamp_and_zero_units_keep_the_header():
    result = run_js(
        """
apiQueue = [DATA];
await sprintsRefresh({ render: false });
const root = makeRoot();
await renderSprints(root);
out({ text: root.textContent, times: byTag(root, "time").length,
      durations: byClass(root, "sprint-duration").length });
""",
        prelude="const DATA = " + json.dumps(
            payload(started_at=None, units=[])
        ) + ";\n",
    )
    assert "SPRINT: Active sprint flow board" in result["text"]
    assert "No units declared" in result["text"]
    assert "Started:" not in result["text"] and "Running:" not in result["text"]
    assert result["times"] == result["durations"] == 0


def test_duration_format_and_negative_delta_uses_zero_bucket():
    result = run_js(
        """
const now = Date.parse("2026-07-26T20:00:00Z");
const at = (minutes) =>
  new Date(now - minutes * 60000).toISOString();
out({
  under: sprintsDuration(at(0), now),
  minutes: sprintsDuration(at(18), now),
  hours: sprintsDuration(at(252), now),
  days: sprintsDuration(at(3120), now),
  skew: sprintsDuration(new Date(now + 60000).toISOString(), now),
});
"""
    )
    assert result == {
        "under": "<1m", "minutes": "18m", "hours": "4h 12m",
        "days": "2d 4h", "skew": "<1m",
    }


def test_document_poll_survives_route_exit_while_render_wiring_detaches():
    result = run_js(
        """
apiQueue = [DATA, DATA, DATA, UPDATED];
let now = Date.parse("2026-07-26T20:00:00Z");
Date.now = () => now;
sprintsStartPoll();
await sprintsRefresh({ render: false });
const root = makeRoot();
await renderSprints(root);
const before = apiCalls.length;
document.hidden = true;
await listeners.get("visibilitychange")();
const hidden = apiCalls.length;
document.hidden = false;
await listeners.get("visibilitychange")();
const visible = apiCalls.length;
await intervals.find((entry) => entry.ms === SPRINTS_REFRESH_MS).fn();
const afterNetworkTick = apiCalls.length;
const beforeLocalTick = apiCalls.length;
const durationBefore = byClass(root, "sprint-duration")[0].textContent;
now += 18 * 60000;
intervals.find((entry) => entry.ms === SPRINTS_DURATION_MS).fn();
const afterLocalTick = apiCalls.length;
const durationAfter = byClass(root, "sprint-duration")[0].textContent;
const renderedBeforeExit = root.textContent;
show("roadmap");
const routeExit = {
  active: sprintsState.active,
  root: sprintsState.root,
  refreshTimer: sprintsState.refreshTimer,
  durationTimer: sprintsState.durationTimer,
  listenerPresent: listeners.has("visibilitychange"),
  cleared: [...cleared],
};
await intervals.find((entry) => entry.ms === SPRINTS_REFRESH_MS).fn();
const afterExitTick = apiCalls.length;
const renderedAfterExit = root.textContent;
const navAfterExit = navButton.textContent;
sprintsStopPoll();
out({
  before, hidden, visible, afterNetworkTick, beforeLocalTick, afterLocalTick,
  durationBefore, durationAfter,
  intervals: intervals.map((entry) => entry.ms),
  routeExit, afterExitTick, renderedBeforeExit, renderedAfterExit, navAfterExit,
  finalCleared: cleared, finalListenerPresent: listeners.has("visibilitychange"),
});
""",
        prelude=(
            "const DATA = " + json.dumps(payload()) + ";\n"
            "const UPDATED = " + json.dumps(payload(count=2)) + ";\n"
        ),
        suffix="""
const sprintRoot = makeRoot();
const roadmapRoot = makeRoot();
const VIEWS = {
  sprints: ["#view-sprints", renderSprints],
  roadmap: ["#view-roadmap", async () => {}],
};
const roots = {"#view-sprints": sprintRoot, "#view-roadmap": roadmapRoot};
function $(selector) { return roots[selector]; }
document.querySelectorAll = (selector) =>
  selector === "nav button" ? [navButton] : [];
document.body = new FakeElement("body");
function ifDetach() {}
function ifStopRailPoll() {}
function setDocumentTitle() {}
function load() {}
""" + SHOW,
    )
    assert result["hidden"] == result["before"]
    assert result["visible"] == result["before"] + 1
    assert result["afterNetworkTick"] == result["visible"] + 1
    assert result["afterLocalTick"] == result["beforeLocalTick"]
    assert result["durationBefore"] == "Running: <1m"
    assert result["durationAfter"] == "Running: 18m"
    assert result["intervals"] == [15000, 60000]
    assert result["routeExit"] == {
        "active": False,
        "root": None,
        "refreshTimer": 1,
        "durationTimer": None,
        "listenerPresent": True,
        "cleared": [2],
    }
    assert result["afterExitTick"] == result["afterNetworkTick"] + 1
    assert result["renderedAfterExit"] == result["renderedBeforeExit"]
    assert result["navAfterExit"] == "Sprints 2"
    assert result["finalCleared"] == [2, 1]
    assert result["finalListenerPresent"] is False


def test_boot_fetches_projection_before_routing():
    boot = APP[APP.rindex("(async () => {"):]
    poll_at = boot.index("sprintsStartPoll()")
    fetch_at = boot.index("await sprintsRefresh({ render: false })")
    route_at = boot.index("routeFromHash()")
    assert poll_at < fetch_at < route_at
