"""Sprints v2 header, priority selection, routing, and polling lifecycle."""
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
SPRINT_BLOCK = APP[
    APP.index("const SPRINTS_REFRESH_MS"):
    APP.index("// ── Tabs + boot")
]
ROUTER_AT = APP.index("function routeFromHash()")
ROUTER = APP[
    ROUTER_AT:
    APP.index('document.querySelectorAll("nav button").forEach', ROUTER_AT)
]

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is required")


def run_js(script: str) -> dict:
    proc = subprocess.run(
        ["node", "-e", script], text=True, capture_output=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_global_header_leads_with_chats_sprints_shells_and_keeps_remaining_order():
    nav = INDEX[INDEX.index("<nav>"):INDEX.index("</nav>")]
    names = [
        "interface", "sprints", "shells", "roadmap", "docs", "flags",
        "worktrees", "map", "analytics", "scripts",
    ]
    positions = [nav.index(f'data-tab="{name}"') for name in names]
    assert positions == sorted(positions)
    assert nav.count('data-tab="sprints"') == 1
    assert INDEX.count('id="view-sprints"') == 1


def test_priority_selection_is_armed_then_latest_paused_then_prepared_then_terminal():
    script = SPRINT_BLOCK + r"""
const base = [
  {sprint_id: 1, lifecycle: "completed", created_at: "2026-08-01 12:00:00"},
  {sprint_id: 2, lifecycle: "prepared", created_at: "2026-08-01 10:00:00"},
  {sprint_id: 3, lifecycle: "paused", paused_at: "2026-08-01 09:00:00"},
  {sprint_id: 4, lifecycle: "paused", paused_at: "2026-08-01 11:00:00"},
  {sprint_id: 5, lifecycle: "armed", created_at: "2026-07-31 00:00:00"},
];
console.log(JSON.stringify({
  armed: sprintPriority(base).sprint_id,
  paused: sprintPriority(base.filter((x) => x.lifecycle !== "armed")).sprint_id,
  prepared: sprintPriority(base.filter((x) => !["armed", "paused"].includes(x.lifecycle))).sprint_id,
  terminal: sprintPriority(base.filter((x) => x.lifecycle === "completed")).sprint_id,
}));
"""
    assert run_js(script) == {"armed": 5, "paused": 4, "prepared": 2, "terminal": 1}


def test_hash_router_preserves_empty_shells_and_exact_sprint_and_roadmap_feature_routes():
    state = r"""
let shellTab = "harness", roadmapView = "board", roadmapFeatureId = null;
let sprintRouteId = null, anView = "tokens";
let chatRouteShell = "", chatRouteConversation = "", chatRouteMode = "chat";
let chatModeController = null;
const SHELL_TAB_HASH = {harness: "shells"};
const VIEWS = {shells: 1, interface: 1, sprints: 1, roadmap: 1, analytics: 1};
const shown = [];
function show(tab) { shown.push(tab); }
globalThis.location = {hash: ""};
"""
    script = state + ROUTER + r"""
const seen = [];
for (const hash of ["", "#sprints", "#sprints/27", "#sprints/nope", "#roadmap-feature-31"]) {
  location.hash = hash;
  routeFromHash();
  seen.push({hash, tab: shown.at(-1), sprint: Number.isNaN(sprintRouteId) ? "invalid" : sprintRouteId,
             feature: roadmapFeatureId});
}
console.log(JSON.stringify({seen}));
"""
    assert run_js(script)["seen"] == [
        {"hash": "", "tab": "shells", "sprint": None, "feature": None},
        {"hash": "#sprints", "tab": "sprints", "sprint": None, "feature": None},
        {"hash": "#sprints/27", "tab": "sprints", "sprint": 27, "feature": None},
        {"hash": "#sprints/nope", "tab": "sprints", "sprint": "invalid", "feature": None},
        {"hash": "#roadmap-feature-31", "tab": "roadmap", "sprint": "invalid", "feature": 31},
    ]


def test_polling_stops_off_tab_or_hidden_and_visible_sprints_schedule_once():
    script = r"""
let timers = [], cleared = [];
globalThis.setTimeout = (fn, ms) => { timers.push(ms); return timers.length; };
globalThis.clearTimeout = (id) => cleared.push(id);
globalThis.document = {hidden: false};
function renderSprints() {}
""" + SPRINT_BLOCK + r"""
activeTab = "sprints";
sprintScheduleRefresh({}, 1);
const visible = {timers: [...timers], active: sprintPollTimer};
activeTab = "shells";
sprintScheduleRefresh({}, 2);
const offTab = {timers: [...timers], active: sprintPollTimer};
activeTab = "sprints";
document.hidden = true;
sprintScheduleRefresh({}, 3);
console.log(JSON.stringify({visible, offTab, hidden: {timers, active: sprintPollTimer}, cleared}));
"""
    result = run_js(script)
    assert result["visible"] == {"timers": [5000], "active": 1}
    assert result["offTab"] == {"timers": [5000], "active": None}
    assert result["hidden"] == {"timers": [5000], "active": None}
    assert result["cleared"] == [1]


def test_board_renders_five_exact_columns_cancelled_distinction_and_focus_wiring():
    harness = r"""
class FakeElement {
  constructor(tag) {
    this.tagName = tag; this.nodeType = 1; this.children = []; this._text = "";
    this.className = ""; this.dataset = {}; this.attributes = {}; this.isConnected = false;
    this.scrollWidth = 1200; this.scrollHeight = 500;
    this.classList = {
      toggle: (name, on) => {
        const names = new Set(this.className.split(" ").filter(Boolean));
        if (on) names.add(name); else names.delete(name);
        this.className = [...names].join(" ");
      },
    };
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; this._text = ""; }
  set textContent(value) { this._text = String(value ?? ""); this.children = []; }
  get textContent() { return this._text + this.children.map(
    (x) => typeof x === "string" ? x : (x.textContent || "")).join(""); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  querySelectorAll() { return []; }
  getBoundingClientRect() { return {left: 0, right: 100, top: 0, height: 50}; }
}
globalThis.document = {
  hidden: false,
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (text) => ({nodeType: 3, textContent: String(text ?? "")}),
  createElementNS: (_ns, tag) => new FakeElement(tag),
};
globalThis.window = {addEventListener() {}, removeEventListener() {}};
globalThis.requestAnimationFrame = () => {};
globalThis.SVGNS = "svg";
const el = (tag, props = {}, ...kids) => {
  const node = Object.assign(new FakeElement(tag), props);
  for (const kid of kids) node.append(kid?.nodeType ? kid : document.createTextNode(kid ?? ""));
  return node;
};
function openModal() { return () => {}; }
"""
    script = harness + SPRINT_BLOCK + r"""
const person = (shortname) => ({shell_id: 1, shortname, current_conversation_id: null});
const unit = (id, column, disposition, deps = []) => ({
  work_unit_id: id, title: `Unit ${id}`, expected_output: `Output ${id}`,
  output_kind: "code", completion_result: disposition === "cancelled" ? "No longer needed" : null,
  planned_wave: id, disposition, column, created_at: "2026-08-01 10:00:00",
  updated_at: "2026-08-01 10:00:00", completed_at: null,
  developer: person("DEV1"), reviewer: person("REV1"), task_ids: [], tasks: [],
  prerequisite_ids: deps, dependent_ids: [], pull_requests: [], messages: [],
});
const snapshot = {
  sprint: {sprint_id: 9, feature: {feature_id: 31, title: "Board"},
    planner: {shortname: "PLN1"}, lifecycle: "armed", created_at: "2026-08-01 10:00:00",
    armed_at: "2026-08-01 10:01:00", paused_at: null, completed_at: null,
    aborted_at: null, terminal_outcome: null},
  specs: [],
  work_units: [unit(1,"done","cancelled"), unit(2,"review","fixing",[1]),
    unit(3,"dev","active"), unit(4,"waiting","ready"), unit(5,"blocked","blocked")],
  dependencies: [{work_unit_id: 2, depends_on_work_unit_id: 1}],
};
const root = sprintBoardNode(snapshot);
function all(node, pred, out = []) {
  if (node?.nodeType === 1 && pred(node)) out.push(node);
  for (const child of node?.children || []) all(child, pred, out);
  return out;
}
const columns = all(root, (node) => node.tagName === "section");
const cards = all(root, (node) => node.tagName === "button" && node.dataset.unitId);
console.log(JSON.stringify({
  headings: columns.map((column) => column.children[0].textContent),
  cards: cards.map((card) => card.textContent),
  focusParity: cards.every((card) => typeof card.onfocus === "function" && typeof card.onmouseenter === "function"),
}));
"""
    result = run_js(script)
    assert result["headings"] == ["Done1", "Review1", "Dev1", "Waiting1", "Blocked1"]
    assert any("Cancelled — not completed" in text for text in result["cards"])
    assert any("Depends: U1" in text and "fixing" in text for text in result["cards"])
    assert result["focusParity"] is True


def test_participant_conversation_link_uses_existing_interface_route():
    script = r"""
class FakeElement { constructor(tag) { this.tagName = tag; this.nodeType = 1; } }
globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (text) => ({nodeType: 3, textContent: String(text)}),
};
const el = (tag, props = {}) => Object.assign(new FakeElement(tag), props);
""" + SPRINT_BLOCK + r"""
const linked = sprintParticipantLink({shortname: "DEV 1", current_conversation_id: "cv_abc"});
const plain = sprintParticipantLink({shortname: "REV1", current_conversation_id: null});
console.log(JSON.stringify({href: linked.href, label: linked.textContent, plain: plain.tagName}));
"""
    assert run_js(script) == {
        "href": "#interface/DEV%201/cv_abc/chat",
        "label": "DEV 1",
        "plain": "span",
    }


def test_lifecycle_actions_are_exact_and_terminal_sprints_have_none():
    harness = r"""
class FakeElement {
  constructor(tag) { this.tagName = tag; this.nodeType = 1; this.children = []; this.className = ""; }
  append(...nodes) { this.children.push(...nodes); }
  set textContent(value) { this._text = String(value ?? ""); this.children = []; }
  get textContent() { return (this._text || "") + this.children.map((node) => node.textContent || "").join(""); }
}
globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (text) => ({nodeType: 3, textContent: String(text ?? "")}),
};
const el = (tag, props = {}, ...kids) => {
  const node = Object.assign(new FakeElement(tag), props);
  node.append(...kids.map((kid) => kid?.nodeType ? kid : document.createTextNode(kid ?? "")));
  return node;
};
"""
    script = harness + SPRINT_BLOCK + r"""
const labels = (lifecycle) => sprintActionButtons({lifecycle}).children.map((node) => node.textContent);
console.log(JSON.stringify({
  prepared: labels("prepared"), armed: labels("armed"), paused: labels("paused"),
  completed: labels("completed"), aborted: labels("aborted"),
}));
"""
    assert run_js(script) == {
        "prepared": ["Abort Sprint"],
        "armed": ["Pause Sprint", "Abort Sprint"],
        "paused": ["Resume Sprint", "Abort Sprint"],
        "completed": [],
        "aborted": [],
    }


def test_all_lifecycle_fixtures_render_exact_badge_actions_times_and_terminal_outcome():
    harness = r"""
class FakeElement {
  constructor(tag) {
    this.tagName = tag; this.nodeType = 1; this.children = []; this.className = "";
    this.dataset = {}; this.attributes = {}; this.isConnected = false;
    this.scrollWidth = 1200; this.scrollHeight = 500; this.open = false;
    this.hidden = false; this.disabled = false;
    this.classList = {toggle() {}};
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; this._text = ""; }
  set textContent(value) { this._text = String(value ?? ""); this.children = []; }
  get textContent() { return (this._text || "") + this.children.map(
    (node) => node.textContent || "").join(""); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  querySelectorAll() { return []; }
  getBoundingClientRect() { return {left: 0, right: 100, top: 0, height: 50}; }
}
globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (text) => ({nodeType: 3, textContent: String(text ?? "")}),
  createElementNS: (_ns, tag) => new FakeElement(tag),
};
globalThis.window = {addEventListener() {}, removeEventListener() {}};
globalThis.requestAnimationFrame = () => {};
globalThis.SVGNS = "svg";
const el = (tag, props = {}, ...kids) => {
  const node = Object.assign(new FakeElement(tag), props);
  node.append(...kids.map((kid) => kid?.nodeType ? kid : document.createTextNode(kid ?? "")));
  return node;
};
function openModal() { return () => {}; }
"""
    script = harness + SPRINT_BLOCK + r"""
const expected = {
  prepared: {actions: ["Abort Sprint"], times: ["Created:", "Elapsed:"], outcome: false},
  armed: {actions: ["Pause Sprint", "Abort Sprint"], times: ["Created:", "Armed:", "Elapsed:"], outcome: false},
  paused: {actions: ["Resume Sprint", "Abort Sprint"], times: ["Created:", "Armed:", "Paused:", "Elapsed:"], outcome: false},
  completed: {actions: [], times: ["Created:", "Armed:", "Completed:", "Elapsed:"], outcome: true},
  aborted: {actions: [], times: ["Created:", "Armed:", "Aborted:", "Elapsed:"], outcome: true},
};
const actual = {};
for (const lifecycle of Object.keys(expected)) {
  const terminal = ["completed", "aborted"].includes(lifecycle);
  const sprint = {
    sprint_id: 9, feature: {feature_id: 31, title: "Board"}, planner: {shortname: "PLN1"},
    lifecycle, created_at: "2026-08-01 10:00:00",
    armed_at: lifecycle === "prepared" ? null : "2026-08-01 10:01:00",
    paused_at: lifecycle === "paused" ? "2026-08-01 10:02:00" : null,
    completed_at: lifecycle === "completed" ? "2026-08-01 10:03:00" : null,
    aborted_at: lifecycle === "aborted" ? "2026-08-01 10:03:00" : null,
    terminal_outcome: terminal ? `${lifecycle} outcome` : null,
  };
  const root = sprintBoardNode({sprint, specs: [], work_units: [], dependencies: []});
  const header = root.children[0];
  actual[lifecycle] = {
    badge: header.children[0].children[1].textContent,
    actions: header.children[3].children.map((node) => node.textContent),
    times: header.children[2].children.map((node) => node.textContent.split(" ")[0]),
    outcome: header.children.some((node) => node.className === "sprint-terminal-outcome"),
  };
}
console.log(JSON.stringify({actual, expected}));
"""
    result = run_js(script)
    for lifecycle, expected in result["expected"].items():
        assert result["actual"][lifecycle] == {"badge": lifecycle, **expected}


def test_global_and_scoped_audit_feeds_do_not_read_until_expanded():
    harness = r"""
class FakeElement {
  constructor(tag) {
    this.tagName = tag; this.nodeType = 1; this.children = []; this.open = false;
    this.hidden = false; this.disabled = false; this.className = "";
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  set textContent(value) { this._text = String(value ?? ""); this.children = []; }
  get textContent() { return this._text || ""; }
}
globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (text) => ({nodeType: 3, textContent: String(text ?? "")}),
};
const el = (tag, props = {}, ...kids) => {
  const node = Object.assign(new FakeElement(tag), props);
  node.append(...kids.map((kid) => kid?.nodeType ? kid : document.createTextNode(kid ?? "")));
  return node;
};
const calls = [];
async function api(path) { calls.push(path); return {items: [], next_cursor: null}; }
function toast() {}
"""
    script = harness + SPRINT_BLOCK + r"""
(async () => {
  sprintSelectedId = 9;
  const globalFeeds = sprintFeedsNode(9);
  const scoped = sprintScopedFeed(9, 27, "events", "Unit events");
  const before = [...calls];
  globalFeeds.children[0].open = true;
  globalFeeds.children[0].ontoggle();
  scoped.open = true;
  scoped.ontoggle();
  await new Promise((resolve) => setTimeout(resolve, 0));
  console.log(JSON.stringify({before, after: calls}));
})();
"""
    assert run_js(script) == {
        "before": [],
        "after": [
            "/sprints/9/events?limit=50",
            "/sprints/9/events?limit=50&work_unit_id=27",
        ],
    }


def test_feed_row_accordion_survives_refresh_repaint_by_stable_identity():
    harness = r"""
class FakeElement {
  constructor(tag) {
    this.tagName = tag; this.nodeType = 1; this.children = []; this.open = false;
    this.hidden = false; this.disabled = false; this.className = "";
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  set textContent(value) { this._text = String(value ?? ""); this.children = []; }
  get textContent() { return this._text || ""; }
}
globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (text) => ({nodeType: 3, textContent: String(text ?? "")}),
};
const el = (tag, props = {}, ...kids) => {
  const node = Object.assign(new FakeElement(tag), props);
  node.append(...kids.map((kid) => kid?.nodeType ? kid : document.createTextNode(kid ?? "")));
  return node;
};
let call = 0;
async function api() {
  call += 1;
  return call === 1
    ? {items: [{event_id: 1, actor: {kind: "system"}, type: "work_unit.ready",
        created_at: "2026-08-01 10:00:00", details: {work_unit_id: 1}}], next_cursor: null}
    : {items: [{event_id: 2, actor: {kind: "system"}, type: "review.approved",
        created_at: "2026-08-01 10:01:00", details: {work_unit_id: 1}}], next_cursor: null};
}
function toast() {}
"""
    script = harness + SPRINT_BLOCK + r"""
(async () => {
  sprintSelectedId = 9;
  const feeds = sprintFeedsNode(9);
  feeds.children[0].open = true;
  feeds.children[0].ontoggle();
  await new Promise((resolve) => setTimeout(resolve, 0));
  sprintFeedRefs.events.list.children[0].open = true;
  sprintFeedRefs.events.list.children[0].ontoggle();
  await sprintLoadFeed("events", {refresh: true});
  console.log(JSON.stringify({
    rowOpenStates: sprintFeedRefs.events.list.children.map((row) => row.open),
    remembered: [...sprintFeedState.events.openRows],
  }));
})();
"""
    assert run_js(script) == {
        "rowOpenStates": [False, True],
        "remembered": ["event:1"],
    }


def test_failed_refresh_keeps_last_good_snapshot_with_retry_and_continued_polling():
    harness = r"""
let timers = [];
globalThis.setTimeout = (_fn, ms) => { timers.push(ms); return timers.length; };
globalThis.clearTimeout = () => {};
class FakeElement {
  constructor(tag) { this.tagName = tag; this.nodeType = 1; this.children = []; this.className = ""; }
  append(...nodes) { this.children.push(...nodes); }
  set textContent(value) { this._text = String(value ?? ""); this.children = []; }
  get textContent() { return (this._text || "") + this.children.map((node) => node.textContent || "").join(""); }
}
globalThis.document = {
  hidden: false,
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (text) => ({nodeType: 3, textContent: String(text ?? "")}),
};
const el = (tag, props = {}, ...kids) => {
  const node = Object.assign(new FakeElement(tag), props);
  node.append(...kids.map((kid) => kid?.nodeType ? kid : document.createTextNode(kid ?? "")));
  return node;
};
function renderSprints() {}
"""
    script = harness + SPRINT_BLOCK + r"""
activeTab = "sprints";
sprintSelectedId = 9;
sprintLastGoodId = 9;
const board = new FakeElement("main");
board.firstChild = {id: "last-good"};
board.prepend = (node) => board.children.unshift(node);
board.querySelector = () => null;
const kept = sprintKeepLastGood(board, sprintRenderGeneration, new Error("offline"));
console.log(JSON.stringify({
  kept,
  oldStillMounted: board.firstChild.id,
  notice: board.children[0].textContent,
  retryLabel: board.children[0].children[1].textContent,
  timers,
}));
"""
    result = run_js(script)
    assert result["kept"] is True
    assert result["oldStillMounted"] == "last-good"
    assert "last good Sprint snapshot" in result["notice"]
    assert result["retryLabel"] == "Retry now"
    assert result["timers"] == [5000]


def test_late_feed_response_cannot_cross_contaminate_a_newly_selected_sprint():
    harness = r"""
class FakeElement {
  constructor(tag) { this.tagName = tag; this.nodeType = 1; this.children = []; this.open = false; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  set textContent(value) { this._text = String(value ?? ""); this.children = []; }
  get textContent() { return this._text || ""; }
}
globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (text) => ({nodeType: 3, textContent: String(text ?? "")}),
};
const el = (tag, props = {}, ...kids) => {
  const node = Object.assign(new FakeElement(tag), props);
  node.append(...kids.map((kid) => kid?.nodeType ? kid : document.createTextNode(kid ?? "")));
  return node;
};
let resolveOld;
async function api() { return new Promise((resolve) => { resolveOld = resolve; }); }
function toast() {}
"""
    script = harness + SPRINT_BLOCK + r"""
(async () => {
  sprintSelectedId = 9;
  const old = sprintFeedsNode(9);
  old.children[0].open = true;
  old.children[0].ontoggle();
  sprintSelectedId = 10;
  sprintFeedsNode(10);
  resolveOld({items: [{event_id: 99}], next_cursor: "old-tail"});
  await new Promise((resolve) => setTimeout(resolve, 0));
  console.log(JSON.stringify({selected: sprintFeedSprintId, items: sprintFeedState.events.items}));
})();
"""
    assert run_js(script) == {"selected": 10, "items": []}


def test_wide_and_narrow_visual_contract_keeps_five_fixed_columns_scrollable():
    assert "#view-sprints { max-width: none; }" in STYLE
    assert ".sprint-board-scroll { overflow-x: auto;" in STYLE
    assert "grid-template-columns: repeat(5, minmax(230px, 1fr))" in STYLE
    narrow = STYLE[STYLE.index("@media (max-width: 760px)"):]
    assert ".sprint-board-canvas { min-width: 1180px; }" in narrow
    assert 'svg.setAttribute("aria-hidden", "true")' in SPRINT_BLOCK
    assert 'card.onfocus = () => highlight(id, true)' in SPRINT_BLOCK
    assert 'root.querySelector?.(`[data-unit-id="${focusedUnitId}"]`)?.focus()' in SPRINT_BLOCK
