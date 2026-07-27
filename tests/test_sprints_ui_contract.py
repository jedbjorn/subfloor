"""Sprints route shell and refresh lifecycle, driven through the real app.js."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
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
    this.dataset = {}; this.attributes = {};
    this.scrollWidth = 1000; this.scrollHeight = 500;
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
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === "class") this.className = String(value);
  }
  getAttribute(name) { return this.attributes[name] ?? null; }
  querySelectorAll(selector) {
    if (!selector.startsWith(".")) return [];
    const name = selector.slice(1);
    return all(this, (node) =>
      String(node.className || "").split(" ").filter(Boolean).includes(name));
  }
  getBoundingClientRect() {
    const index = Number(String(this.dataset.seq || "").replace(/\D/g, "")) || 0;
    const left = index * 170;
    return { left, right: left + 140, top: index * 25,
             height: 70, width: 140, bottom: index * 25 + 70 };
  }
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
  createElementNS: (_ns, tag) => new FakeElement(tag),
  createTextNode: (text) => ({ nodeType: 3, textContent: String(text ?? "") }),
  querySelector: (selector) =>
    selector === 'nav button[data-tab="sprints"]' ? navButton : null,
  addEventListener: (name, fn) => listeners.set(name, fn),
  removeEventListener: (name, fn) => {
    if (listeners.get(name) === fn) listeners.delete(name);
  },
};
const windowListeners = new Map();
globalThis.window = {
  addEventListener: (name, fn) => {
    if (!windowListeners.has(name)) windowListeners.set(name, new Set());
    windowListeners.get(name).add(fn);
  },
  removeEventListener: (name, fn) => {
    const handlers = windowListeners.get(name);
    if (!handlers) return;
    handlers.delete(fn);
    if (!handlers.size) windowListeners.delete(name);
  },
};
const windowListenerCount = (name) => windowListeners.get(name)?.size || 0;
globalThis.requestAnimationFrame = (fn) => { fn(); return 1; };
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
            "units": [{
                "seq": "U3",
                "unit_title": "Sprints route and nav signal",
                "state": "merged",
                "state_recognized": True,
                "dev_shell_id": 11,
                "dev_shortname": "DEV5",
                "reviewer_shell_id": 7,
                "reviewer_shortname": "REV2",
                "depends_on": "U1",
                "overlap": None,
                "branch": "feat/sprints-page",
                "pr_number": 639,
            }] if units is None else units,
        }],
    }


def unit(seq, state, *, title=None, depends_on=None, overlap=None,
         dev_id=11, dev="DEV5", reviewer_id=7, reviewer="REV2",
         branch=None, pr=None, recognized=True):
    return {
        "seq": seq,
        "unit_title": title or f"Unit {seq}",
        "state": state,
        "state_recognized": recognized,
        "dev_shell_id": dev_id,
        "dev_shortname": dev,
        "reviewer_shell_id": reviewer_id,
        "reviewer_shortname": reviewer,
        "depends_on": depends_on,
        "overlap": overlap,
        "branch": branch,
        "pr_number": pr,
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


def test_flow_columns_cards_and_active_role_emphasis():
    long_title = "Dependency rendering " + ("with a long title " * 8)
    long_overlap = "Same-file overlap " + ("must stay on one line " * 8)
    units = [
        unit(
            "U1", "pending",
            dev_id=None, dev=None, reviewer_id=29, reviewer=None,
        ),
        unit("U2", "working", title=long_title, overlap=long_overlap,
             branch="feat/sprints-flow-boards", pr=640),
        unit("U3", "in_review"),
        unit("U4", "blocked"),
        unit("U5", "merged"),
        unit("U6", "cancelled"),
        {
            **unit("U7", "mystery", recognized=False),
            "task_body": "must not leak task body",
            "messages": ["must not leak messages"],
            "checks": "must not leak checks",
        },
    ]
    result = run_js(
        """
apiQueue = [DATA];
await sprintsRefresh({ render: false });
const root = makeRoot();
await renderSprints(root);
const cards = byClass(root, "sprint-unit");
const card = (seq) => cards.find((node) => node.dataset.seq === seq);
const roles = (seq) => byClass(card(seq), "sprint-role").map(
  (role) => ({ text: role.textContent, className: role.className }));
out({
  headings: byClass(root, "sprint-col-head").map((node) => node.textContent),
  cards: cards.length,
  u2: {
    text: card("U2").textContent,
    title: byClass(card("U2"), "sprint-unit-title")[0].title,
    overlap: byClass(card("U2"), "sprint-unit-overlap")[0].title,
    detailsHidden: byClass(card("U2"), "sprint-unit-details")[0].hidden,
  },
  u1Roles: roles("U1"),
  u2Roles: roles("U2"),
  u3Roles: roles("U3"),
  u4Roles: roles("U4"),
  done: [card("U5").textContent, card("U6").textContent],
  unknown: card("U7").textContent,
});
""",
        prelude="const DATA = " + json.dumps(payload(units=units)) + ";\n",
    )
    assert result["headings"] == [
        "Done2", "Review1", "Dev1", "Waiting1", "Blocked1", "Unrecognized1",
    ]
    assert result["cards"] == 7
    assert long_title in result["u2"]["text"]
    assert result["u2"]["title"] == long_title
    assert result["u2"]["overlap"] == long_overlap
    assert result["u2"]["detailsHidden"] is True
    assert "Branch: feat/sprints-flow-boards" in result["u2"]["text"]
    assert "PR #640" in result["u2"]["text"]
    assert result["u1Roles"] == [
        {"text": "Dev: Unassigned", "className": "sprint-role warn"},
        {"text": "Reviewer: Shell #29", "className": "sprint-role warn"},
    ]
    assert all("active" not in role["className"] for role in result["u1Roles"])
    assert "active" in result["u2Roles"][0]["className"]
    assert "active" not in result["u2Roles"][1]["className"]
    assert "active" not in result["u3Roles"][0]["className"]
    assert "active" in result["u3Roles"][1]["className"]
    assert all("active" not in role["className"] for role in result["u4Roles"])
    assert "Merged" in result["done"][0]
    assert "Cancelled" in result["done"][1]
    assert "mystery" in result["unknown"]
    assert "must not leak" not in result["unknown"]


def test_roles_resolve_independently_to_their_own_shortnames():
    result = run_js(
        """
const flow = sprintsBuildFlow(DATA.sprints[0]);
const roles = byClass(flow, "sprint-role").map(
  (role) => ({ text: role.textContent, className: role.className }));
out(roles);
""",
        prelude="const DATA = " + json.dumps(payload(units=[
            unit("U1", "pending", dev_id=None, dev=None)
        ])) + ";\n",
    )
    assert result == [
        {"text": "Dev: Unassigned", "className": "sprint-role warn"},
        {"text": "Reviewer: REV2", "className": "sprint-role"},
    ]


def test_unrecognized_column_is_omitted_when_empty():
    result = run_js(
        """
apiQueue = [DATA];
await sprintsRefresh({ render: false });
const root = makeRoot();
await renderSprints(root);
out({ headings: byClass(root, "sprint-col-head").map((node) => node.textContent) });
""",
        prelude="const DATA = " + json.dumps(
            payload(units=[unit("U1", "working")])
        ) + ";\n",
    )
    assert result["headings"] == [
        "Done", "Review", "Dev1", "Waiting", "Blocked",
    ]


def test_projection_recognition_flag_overrides_known_ui_column_key():
    result = run_js(
        """
const flow = sprintsBuildFlow(DATA.sprints[0]);
const card = byClass(flow, "sprint-unit")[0];
out({
  headings: byClass(flow, "sprint-col-head").map((node) => node.textContent),
  cardClass: card.className,
  cardText: card.textContent,
});
""",
        prelude="const DATA = " + json.dumps(
            payload(units=[unit("U1", "done", recognized=False)])
        ) + ";\n",
    )
    assert result["headings"] == [
        "Done", "Review", "Dev", "Waiting", "Blocked", "Unrecognized1",
    ]
    assert "unrecognized" in result["cardClass"]
    assert "done" in result["cardText"]


def test_unrecognized_state_label_ignores_object_prototype():
    result = run_js(
        """
const flow = sprintsBuildFlow(DATA.sprints[0]);
const card = byClass(flow, "sprint-unit")[0];
out({ text: card.textContent });
""",
        prelude="const DATA = " + json.dumps(
            payload(units=[unit("U1", "constructor", recognized=False)])
        ) + ";\n",
    )
    assert "constructor" in result["text"]
    assert "function Object" not in result["text"]


def test_unrecognized_column_does_not_mutate_shared_columns():
    result = run_js(
        """
const unknown = sprintsBuildFlow(UNKNOWN.sprints[0]);
const known = sprintsBuildFlow(KNOWN.sprints[0]);
out({
  unknown: byClass(unknown, "sprint-col-head").map((node) => node.textContent),
  known: byClass(known, "sprint-col-head").map((node) => node.textContent),
  shared: SPRINT_FLOW_COLUMNS.map((column) => column.label),
});
""",
        prelude=(
            "const UNKNOWN = " + json.dumps(payload(
                units=[unit("U1", "mystery", recognized=False)]
            )) + ";\n"
            "const KNOWN = " + json.dumps(payload(
                units=[unit("U1", "working")]
            )) + ";\n"
        ),
    )
    assert result["unknown"][-1] == "Unrecognized1"
    assert result["known"] == ["Done", "Review", "Dev1", "Waiting", "Blocked"]
    assert result["shared"] == ["Done", "Review", "Dev", "Waiting", "Blocked"]


def test_unavailable_dependency_has_visible_warning_marker():
    result = run_js(
        """
const flow = sprintsBuildFlow(DATA.sprints[0]);
const marker = byClass(flow, "sprint-dep-warning")[0];
out({ text: marker.textContent, className: marker.className });
""",
        prelude="const DATA = " + json.dumps(payload(
            units=[unit("U1", "working", depends_on="U0")]
        )) + ";\n",
    )
    assert result["text"] == "⚠"
    assert "warn" in result["className"]


def test_unavailable_dependency_warning_is_accessibly_named():
    result = run_js(
        """
const flow = sprintsBuildFlow(DATA.sprints[0]);
const marker = byClass(flow, "sprint-dep-warning")[0];
out({ role: marker.role, name: marker.ariaLabel });
""",
        prelude="const DATA = " + json.dumps(payload(
            units=[unit("U1", "working", depends_on="U0")]
        )) + ";\n",
    )
    assert result == {
        "role": "img", "name": "dependency unavailable: U0",
    }


def test_dependency_wires_are_sprint_scoped_and_bad_tokens_degrade_locally():
    data = payload(units=[
        unit("U1", "pending"),
        unit("U2", "working", depends_on="U1, U9, U2, S59-U1,,"),
    ])
    data["sprints"].append({
        "document_id": 78,
        "title": "SPRINT: Other graph",
        "started_at": "2026-07-26T21:00:00Z",
        "planner": None,
        "feature": None,
        "units": [
            unit("U1", "pending"),
            unit("U2", "in_review", depends_on="U1"),
            unit("U9", "blocked"),
        ],
    })
    data["active_count"] = 2
    result = run_js(
        """
apiQueue = [DATA];
await sprintsRefresh({ render: false });
const root = makeRoot();
await renderSprints(root);
const boards = byClass(root, "sprint-board");
const firstCards = byClass(boards[0], "sprint-unit");
const source = firstCards.find((card) => card.dataset.seq === "U1");
source.onmouseenter();
const hover = {
  flow: byClass(boards[0], "sprint-flow")[0].className,
  cards: firstCards.filter((card) => card.classList.contains("lit"))
    .map((card) => card.dataset.seq),
  wires: byClass(boards[0], "sprint-wire")
    .filter((wire) => wire.classList.contains("lit")).length,
};
source.onmouseleave();
out({
  wires: boards.map((board) => byClass(board, "sprint-wire").map(
    (wire) => [wire.dataset.from, wire.dataset.to])),
  markers: boards.map((board) => byTag(board, "marker")[0].getAttribute("id")),
  unavailable: boards.map((board) => byClass(board, "sprint-dep-warning")
    .map((node) => node.ariaLabel)),
  hover,
});
""",
        prelude="const DATA = " + json.dumps(data) + ";\n",
    )
    assert result["wires"] == [[["U1", "U2"]], [["U1", "U2"]]]
    assert result["markers"] == ["sprint-arrow-77", "sprint-arrow-78"]
    assert result["unavailable"][0] == [
        "dependency unavailable: U9, U2, S59-U1, (empty), (empty)"
    ]
    assert result["unavailable"][1] == []
    assert "sprint-spotlight" in result["hover"]["flow"]
    assert result["hover"]["cards"] == ["U2", "U1"]
    assert result["hover"]["wires"] == 1


def test_satisfied_dependencies_draw_no_wire_but_stay_named():
    result = run_js(
        """
const flow = sprintsBuildFlow(DATA.sprints[0]);
const card = byClass(flow, "sprint-unit").find(
  (node) => node.dataset.seq === "U3");
out({
  wires: byClass(flow, "sprint-wire").map(
    (wire) => [wire.dataset.from, wire.dataset.to]),
  depends: byClass(card, "sprint-unit-deps")[0].textContent,
  warnings: byClass(flow, "sprint-dep-warning").length,
});
""",
        prelude="const DATA = " + json.dumps(payload(units=[
            unit("U1", "merged"),
            unit("U2", "cancelled"),
            unit("U4", "pending"),
            unit("U3", "working", depends_on="U1, U2, U4"),
        ])) + ";\n",
    )
    assert result["wires"] == [["U4", "U3"]]
    assert result["depends"] == "Depends: U1, U2, U4"
    assert result["warnings"] == 0


def test_rows_follow_wires_to_reduce_crossings():
    result = run_js(
        """
const flow = sprintsBuildFlow(DATA.sprints[0]);
const cols = byClass(flow, "sprint-col");
const order = (key) => byClass(
  cols.find((col) => col.classList.contains(key)), "sprint-unit")
  .map((node) => node.dataset.seq);
out({ dev: order("working"), waiting: order("pending") });
""",
        prelude="const DATA = " + json.dumps(payload(units=[
            unit("U1", "pending"),
            unit("U2", "pending"),
            unit("U5", "working", depends_on="U2"),
            unit("U6", "working", depends_on="U1"),
        ])) + ";\n",
    )
    assert result["dev"] == ["U6", "U5"]
    assert result["waiting"] == ["U1", "U2"]


def test_wires_anchor_on_near_edges_by_direction():
    # The harness derives each card's rect from its seq digits, so U1→U2 is a
    # forward wire, U5→U2 a backward one, and U3→X3 (equal digits, identical
    # rects) exercises the same-column gutter branch.
    result = run_js(
        """
const flow = sprintsBuildFlow(DATA.sprints[0]);
out(byClass(flow, "sprint-wire").map((wire) => (
  { from: wire.dataset.from, to: wire.dataset.to,
    d: wire.getAttribute("d") })));
""",
        prelude="const DATA = " + json.dumps(payload(units=[
            unit("U1", "pending"),
            unit("U5", "in_review"),
            unit("U2", "working", depends_on="U1, U5"),
            unit("U3", "pending"),
            unit("X3", "pending", depends_on="U3"),
        ])) + ";\n",
    )
    assert result == [
        {"from": "U1", "to": "U2",
         "d": "M 310 60 C 350 60, 300 85, 340 85"},
        {"from": "U5", "to": "U2",
         "d": "M 850 160 C 702 160, 628 85, 480 85"},
        {"from": "U3", "to": "X3",
         "d": "M 650 110 C 674 110, 674 110, 650 110"},
    ]


def test_click_expands_details_pins_wires_and_blank_space_clears_selection():
    result = run_js(
        """
const flow = sprintsBuildFlow(DATA.sprints[0]);
const cards = byClass(flow, "sprint-unit");
const card = (seq) => cards.find((node) => node.dataset.seq === seq);
const details = byClass(card("U2"), "sprint-unit-details")[0];
let stopped = 0;
card("U2").onclick({ stopPropagation: () => { stopped += 1; } });
card("U2").onmouseleave();
const selected = {
  stopped,
  flow: flow.className,
  cards: cards.filter((node) => node.classList.contains("selected"))
    .map((node) => node.dataset.seq),
  litCards: cards.filter((node) => node.classList.contains("lit"))
    .map((node) => node.dataset.seq),
  litWires: byClass(flow, "sprint-wire")
    .filter((wire) => wire.classList.contains("lit")).length,
  detailsHidden: details.hidden,
  detailsText: details.textContent,
  pressed: card("U2").ariaPressed,
  expanded: card("U2").ariaExpanded,
};
flow.onclick();
out({
  selected,
  cleared: {
    flow: flow.className,
    selectedCards: cards.filter(
      (node) => node.classList.contains("selected")).length,
    litCards: cards.filter((node) => node.classList.contains("lit")).length,
    litWires: byClass(flow, "sprint-wire")
      .filter((wire) => wire.classList.contains("lit")).length,
    detailsHidden: details.hidden,
    pressed: card("U2").ariaPressed,
    expanded: card("U2").ariaExpanded,
  },
});
""",
        prelude="const DATA = " + json.dumps(payload(units=[
            unit("U1", "pending"),
            unit(
                "U2", "working", depends_on="U1",
                overlap="shares scripts/sprint.py",
                branch="feat/sprint-details", pr=650,
            ),
            unit("U3", "blocked"),
        ])) + ";\n",
    )
    assert result["selected"] == {
        "stopped": 1,
        "flow": "sprint-flow sprint-spotlight",
        "cards": ["U2"],
        "litCards": ["U2", "U1"],
        "litWires": 1,
        "detailsHidden": False,
        "detailsText": (
            "shares scripts/sprint.py"
            "Branch: feat/sprint-details"
            "PR #650"
        ),
        "pressed": "true",
        "expanded": "true",
    }
    assert result["cleared"] == {
        "flow": "sprint-flow",
        "selectedCards": 0,
        "litCards": 0,
        "litWires": 0,
        "detailsHidden": True,
        "pressed": "false",
        "expanded": "false",
    }


def test_keyboard_toggles_selection_without_hijacking_other_keys():
    result = run_js(
        """
const flow = sprintsBuildFlow(DATA.sprints[0]);
const card = byClass(flow, "sprint-unit")[0];
const details = byClass(card, "sprint-unit-details")[0];
let prevented = 0;
const key = (value) => ({
  key: value,
  preventDefault: () => { prevented += 1; },
});
card.onkeydown(key("ArrowDown"));
const ignored = {
  selected: card.classList.contains("selected"),
  detailsHidden: details.hidden,
  prevented,
};
card.onkeydown(key("Enter"));
const selected = {
  selected: card.classList.contains("selected"),
  detailsHidden: details.hidden,
  prevented,
};
card.onkeydown(key(" "));
out({
  role: card.role,
  tabIndex: card.tabIndex,
  ignored,
  selected,
  cleared: {
    selected: card.classList.contains("selected"),
    detailsHidden: details.hidden,
    prevented,
  },
});
""",
        prelude="const DATA = " + json.dumps(payload(units=[
            unit("U1", "working", overlap="detail"),
        ])) + ";\n",
    )
    assert result == {
        "role": "button",
        "tabIndex": 0,
        "ignored": {
            "selected": False,
            "detailsHidden": True,
            "prevented": 0,
        },
        "selected": {
            "selected": True,
            "detailsHidden": False,
            "prevented": 1,
        },
        "cleared": {
            "selected": False,
            "detailsHidden": True,
            "prevented": 2,
        },
    }


def test_identical_refresh_reuses_dom_and_preserves_selection_and_scroll():
    result = run_js(
        """
apiQueue = [DATA, JSON.parse(JSON.stringify(DATA))];
await sprintsRefresh({ render: false });
const root = makeRoot();
await renderSprints(root);
const flow = byClass(root, "sprint-flow")[0];
const card = byClass(root, "sprint-unit")
  .find((node) => node.dataset.seq === "U1");
flow.scrollLeft = 240;
card.onclick({ stopPropagation: () => {} });
card.onmouseleave();
await sprintsRefresh();
out({
  flowReused: byClass(root, "sprint-flow")[0] === flow,
  scrollLeft: flow.scrollLeft,
  selectedCards: byClass(root, "sprint-unit")
    .filter((node) => node.classList.contains("selected")).length,
  litCards: byClass(root, "sprint-unit")
    .filter((node) => node.classList.contains("lit")).length,
  resizeListeners: windowListenerCount("resize"),
});
""",
        prelude="const DATA = " + json.dumps(payload(units=[
            unit("U1", "pending"),
            unit("U2", "working", depends_on="U1"),
        ])) + ";\n",
    )
    assert result == {
        "flowReused": True,
        "scrollLeft": 240,
        "selectedCards": 1,
        "litCards": 2,
        "resizeListeners": 1,
    }


def test_transport_only_refresh_reuses_rendered_dom():
    initial = payload(units=[
        {
            **unit("U1", "working", overlap="visible detail"),
            "unit_id": 41,
            "sprint_doc_id": 77,
            "assigned_at": "2026-07-27T06:00:00Z",
            "state_changed_at": "2026-07-27T06:00:00Z",
            "updated_at": "2026-07-27T06:00:00Z",
            "updated_by_shell_id": 11,
        },
    ])
    updated = json.loads(json.dumps(initial))
    changed_unit = updated["sprints"][0]["units"][0]
    changed_unit["updated_at"] = "2026-07-27T06:01:00Z"
    changed_unit["updated_by_shell_id"] = 7
    result = run_js(
        """
apiQueue = [INITIAL, UPDATED];
await sprintsRefresh({ render: false });
const root = makeRoot();
await renderSprints(root);
const flow = byClass(root, "sprint-flow")[0];
const card = byClass(root, "sprint-unit")[0];
flow.scrollLeft = 240;
card.onclick({ stopPropagation: () => {} });
await sprintsRefresh();
out({
  flowReused: byClass(root, "sprint-flow")[0] === flow,
  scrollLeft: flow.scrollLeft,
  selectedCards: byClass(root, "sprint-unit")
    .filter((node) => node.classList.contains("selected")).length,
});
""",
        prelude=(
            "const INITIAL = " + json.dumps(initial) + ";\n"
            "const UPDATED = " + json.dumps(updated) + ";\n"
        ),
    )
    assert result == {
        "flowReused": True,
        "scrollLeft": 240,
        "selectedCards": 1,
    }


class TestRenderedProjectionInvalidation(unittest.TestCase):
    def test_each_rendered_field_invalidates_the_dom(self):
        cases = [
            ("document_id", 78),
            ("title", "SPRINT: Renamed"),
            ("started_at", "2026-07-26T21:00:00Z"),
            ("planner.shortname", "PLN9"),
            ("feature.feature_id", 44),
            ("feature.title", "Renamed feature"),
            ("units.0.seq", "U4"),
            ("units.0.unit_title", "Renamed unit"),
            ("units.0.state", "cancelled"),
            ("units.0.state_recognized", False),
            ("units.0.dev_shell_id", 99),
            ("units.0.dev_shortname", "DEV9"),
            ("units.0.reviewer_shell_id", 98),
            ("units.0.reviewer_shortname", "REV9"),
            ("units.0.depends_on", "U9"),
            ("units.0.overlap", "changed overlap"),
            ("units.0.branch", "feat/changed"),
            ("units.0.pr_number", 700),
        ]
        for field, value in cases:
            with self.subTest(field=field):
                initial = payload()
                updated = json.loads(json.dumps(initial))
                target = updated["sprints"][0]
                parts = field.split(".")
                for part in parts[:-1]:
                    target = (
                        target[int(part)] if part.isdigit() else target[part]
                    )
                target[parts[-1]] = value
                result = run_js(
                    """
apiQueue = [INITIAL, UPDATED];
await sprintsRefresh({ render: false });
const root = makeRoot();
await renderSprints(root);
const flow = byClass(root, "sprint-flow")[0];
await sprintsRefresh();
out({ flowReused: byClass(root, "sprint-flow")[0] === flow });
""",
                    prelude=(
                        "const INITIAL = " + json.dumps(initial) + ";\n"
                        "const UPDATED = " + json.dumps(updated) + ";\n"
                    ),
                )
                self.assertFalse(result["flowReused"])


def test_freezing_boards_updates_two_to_one_and_one_to_zero():
    initial = payload()
    initial["sprints"].append({
        "document_id": 78,
        "title": "SPRINT: Frozen after this payload",
        "started_at": "2026-07-26T21:00:00Z",
        "planner": None,
        "feature": None,
        "units": [unit("U8", "working")],
    })
    initial["active_count"] = 2
    updated = json.loads(json.dumps(initial))
    updated["active_count"] = 1
    updated["sprints"] = updated["sprints"][:1]
    empty = payload(count=0)
    result = run_js(
        """
apiQueue = [INITIAL, UPDATED, EMPTY];
await sprintsRefresh({ render: false });
const root = makeRoot();
await renderSprints(root);
const survivor = byClass(root, "sprint-unit")[0];
survivor.onclick({ stopPropagation: () => {} });
await sprintsRefresh();
const boards = byClass(root, "sprint-board");
const afterOne = {
  nav: navButton.textContent,
  warn: navButton.classList.contains("warn"),
  boardCount: boards.length,
  survivorPresent: boards.some(
    (board) => board.textContent.includes("SPRINT: Active sprint flow board")),
  frozenPresent: boards.some(
    (board) => board.textContent.includes("SPRINT: Frozen after this payload")),
  selected: byClass(root, "sprint-unit")
    .filter((card) => card.classList.contains("selected"))
    .map((card) => card.dataset.seq),
};
await sprintsRefresh();
out({
  afterOne,
  afterZero: {
    nav: navButton.textContent,
    warn: navButton.classList.contains("warn"),
    title: navButton.title,
    boardCount: byClass(root, "sprint-board").length,
    text: root.textContent,
  },
});
""",
        prelude=(
            "const INITIAL = " + json.dumps(initial) + ";\n"
            "const UPDATED = " + json.dumps(updated) + ";\n"
            "const EMPTY = " + json.dumps(empty) + ";\n"
        ),
    )
    assert result == {
        "afterOne": {
            "nav": "Sprints 1",
            "warn": True,
            "boardCount": 1,
            "survivorPresent": True,
            "frozenPresent": False,
            "selected": ["U3"],
        },
        "afterZero": {
            "nav": "Sprints",
            "warn": False,
            "title": "",
            "boardCount": 0,
            "text": "No active sprints.",
        },
    }


def test_changed_refresh_replaces_dom_preserves_selection_and_cleans_listener():
    initial = payload(units=[
        unit("U1", "working", overlap="initial overlap"),
    ])
    updated = payload(units=[
        unit("U1", "working", overlap="updated overlap"),
        unit("U2", "in_review", depends_on="U1"),
    ])
    result = run_js(
        """
apiQueue = [INITIAL, UPDATED];
await sprintsRefresh({ render: false });
const root = makeRoot();
await renderSprints(root);
const firstFlow = byClass(root, "sprint-flow")[0];
const firstCard = byClass(firstFlow, "sprint-unit")[0];
firstCard.onclick({ stopPropagation: () => {} });
const before = windowListenerCount("resize");
await sprintsRefresh();
const nextFlow = byClass(root, "sprint-flow")[0];
const selected = byClass(nextFlow, "sprint-unit")
  .filter((node) => node.classList.contains("selected"));
out({
  flowReused: nextFlow === firstFlow,
  before,
  after: windowListenerCount("resize"),
  selected: selected.map((node) => node.dataset.seq),
  detailsHidden: byClass(selected[0], "sprint-unit-details")[0].hidden,
  detailsText: byClass(selected[0], "sprint-unit-details")[0].textContent,
});
""",
        prelude=(
            "const INITIAL = " + json.dumps(initial) + ";\n"
            "const UPDATED = " + json.dumps(updated) + ";\n"
        ),
    )
    assert result == {
        "flowReused": False,
        "before": 1,
        "after": 1,
        "selected": ["U1"],
        "detailsHidden": False,
        "detailsText": "updated overlap",
    }


def test_flow_styles_clamp_text_and_contain_narrow_viewport_scrolling():
    sprint_css = STYLE[STYLE.index("/* Active sprint flow boards"):
                       STYLE.index("/* Roadmap Flow view")]
    assert "#view-sprints, .sprint-board { min-width: 0; }" in sprint_css
    assert "#view-sprints { max-width: 1350px; }" in sprint_css
    assert ".sprint-board { overflow: hidden; }" in sprint_css
    assert "display: flex; gap: 35px;" in sprint_css
    assert ".sprint-col { flex: 1 1 0;" in sprint_css
    assert ".sprint-unit { width: 100%;" in sprint_css
    assert "overflow-x: auto" in sprint_css
    assert "text-overflow: ellipsis" in sprint_css
    assert "white-space: nowrap" in sprint_css
    assert "cursor: pointer" in sprint_css
    assert ".sprint-unit:focus-visible" in sprint_css


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
  resizeListenerPresent: windowListeners.has("resize"),
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
        "resizeListenerPresent": False,
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
