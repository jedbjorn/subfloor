"""Shells navigation contracts for the build-free review UI."""

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
SHELL_STATE = APP[APP.index("let selectedShell ="):
                  APP.index("// Rough token estimator")]
SHELL_RENDER = APP[APP.index("async function renderShells(root)"):
                   APP.index("// Default Models — the flavor_defaults")]
DEFAULT_MODELS = APP[APP.index("function thinkingLevelState"):
                     APP.index("// Harness — the shell's surfaces")]
ROUTER_AT = APP.index("function routeFromHash()")
ROUTER = APP[ROUTER_AT:
             APP.index('document.querySelectorAll("nav button").forEach',
                       ROUTER_AT)]

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required"
)


def run_js(script: str) -> dict:
    proc = subprocess.run(
        ["node", "-e", script], text=True, capture_output=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_thinking_selector_state_matrix_is_route_aware():
    helper = APP[
        APP.index("function thinkingLevelState"):
        APP.index("function dmModelPicker")
    ]
    script = helper + r"""
const catalog = {stale: false, harnesses: {codex: {models: [
  {id: "gpt-high", availability: "available", supported_efforts: ["low", "high"]},
  {id: "gpt-explicit", availability: "available", supported_efforts: ["low", "medium"]},
]}}};
console.log(JSON.stringify({
  controlled: thinkingLevelState("codex", catalog, "gpt-high", "low"),
  defaulted: thinkingLevelState("codex", catalog, "gpt-high", null),
  explicit: thinkingLevelState("codex", catalog, "gpt-explicit", null),
  harnessDefault: thinkingLevelState("codex", catalog, null, null),
  vibe: thinkingLevelState("vibe", catalog, "devstral", null),
  stale: thinkingLevelState("codex", {...catalog, stale: true}, "gpt-high", "high"),
}));
"""
    result = run_js(script)
    assert result["controlled"]["selected"] == "low"
    assert result["defaulted"]["selected"] == "high"
    assert result["explicit"]["selected"] == ""
    assert result["explicit"]["disabled"] is False
    assert result["harnessDefault"]["label"] == "Harness default"
    assert result["harnessDefault"]["disabled"] is True
    assert result["vibe"]["label"] == "Thinking control unavailable"
    assert result["stale"]["disabled"] is True
    assert "Refresh & verify" in result["stale"]["guidance"]


def test_default_models_saves_model_and_effort_atomically():
    assert 'model: null, effort: null' in DEFAULT_MODELS
    assert 'model: value, effort: state.selected' in DEFAULT_MODELS
    assert 'model: row.model, effort' in DEFAULT_MODELS
    assert 'row.effort_state = "selection-required"' in DEFAULT_MODELS
    assert 'ariaLabel: `Thinking level for ${flavor} ${h}`' in DEFAULT_MODELS


def test_skills_is_nested_under_shells_instead_of_global_navigation():
    nav = INDEX[INDEX.index("<nav>"):INDEX.index("</nav>")]
    main = INDEX[INDEX.index("<main>"):INDEX.index("</main>")]
    assert nav.count('data-tab="shells"') == 1
    assert 'data-tab="skills"' not in nav
    assert 'id="view-skills"' not in main
    assert 'id="view-shells"' in main


def test_new_shell_creator_offers_bespoke_without_a_flavor_template():
    creator = APP[APP.index("function openNewShellModal"):
                  APP.index("async function renderShells(root)")]
    assert "Bespoke — custom skill pack" in creator
    assert "flavor: fl.value || null" in creator
    assert '"shell type"' in creator
    assert "openActionModal" in creator
    assert "dismissNode: cancel, actionNode: create" in creator
    assert "footNodes" not in creator


def test_fork_global_shell_header_is_inert_with_only_fifteen_percent_transparency():
    assert 'sub.classList.add("subbar-inert")' in SHELL_RENDER
    inert = STYLE[
        STYLE.index(".subbar-inert {"):
        STYLE.index("}", STYLE.index(".subbar-inert {")) + 1
    ]
    assert "opacity: .85" in inert
    assert "pointer-events: none" in inert
    assert "opacity: .4" not in inert


def test_skill_assignments_are_flavor_scoped_with_bespoke_exceptions():
    assignments = APP[APP.index("async function renderSkillAssignments"):
                      APP.index("// ── Roadmap")]
    assert "const bespokeShells = shells.filter((sh) => !sh.flavor)" in assignments
    assert "for (const fl of flavors)" in assignments
    assert "/flavors/${encodeURIComponent(fl.flavor)}/skills/${s.skill_id}" in assignments
    assert "for (const sh of bespokeShells)" in assignments
    assert "for (const sh of shells)" not in assignments


def test_each_shell_section_has_a_distinct_reload_safe_hash():
    script = SHELL_STATE + r"""
globalThis.location = { hash: "" };
let roadmapView = null;
let anView = "tokens";
let ifSelected = null;
const VIEWS = {
  interface: 1, shells: 1, roadmap: 1, analytics: 1, docs: 1,
};
const shown = [];
function show(tab) { shown.push(tab); }
""" + ROUTER + r"""
const seen = [];
for (const hash of [
  "", "#shells", "#shells-skills", "#shells-skill-assignments",
  "#shells-default-models", "#shells-unknown", "#nonsense",
]) {
  location.hash = hash;
  routeFromHash();
  seen.push({ hash, tab: shown.at(-1), shellTab });
}
console.log(JSON.stringify({ seen }));
"""
    result = run_js(script)
    assert result["seen"] == [
        {"hash": "", "tab": "shells", "shellTab": "harness"},
        {"hash": "#shells", "tab": "shells", "shellTab": "harness"},
        {"hash": "#shells-skills", "tab": "shells", "shellTab": "skills"},
        {
            "hash": "#shells-skill-assignments",
            "tab": "shells",
            "shellTab": "assignments",
        },
        {
            "hash": "#shells-default-models",
            "tab": "shells",
            "shellTab": "models",
        },
        {"hash": "#shells-unknown", "tab": "shells", "shellTab": "harness"},
        {"hash": "#nonsense", "tab": "shells", "shellTab": "harness"},
    ]


def test_shell_subtabs_navigate_by_hash_and_dispatch_the_right_panel():
    script = r"""
class FakeElement {
  constructor(tag) {
    this.tagName = tag;
    this.nodeType = 1;
    this.children = [];
    this.className = "";
    this._text = "";
    this.classList = {
      add: (name) => {
        const names = new Set(this.className.split(" ").filter(Boolean));
        names.add(name);
        this.className = [...names].join(" ");
      },
    };
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
globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (text) => ({ nodeType: 3, textContent: String(text ?? "") }),
};
globalThis.location = { hash: "" };
const el = (tag, props = {}, ...kids) => {
  const node = Object.assign(new FakeElement(tag), props);
  for (const kid of kids)
    node.append(kid?.nodeType ? kid : document.createTextNode(kid ?? ""));
  return node;
};
""" + SHELL_STATE + r"""
const shell = {
  shell_id: 3, shortname: "DEV3", display_name: "Code-01",
  flavor: "dev", role: "Dev shell", mandate: "Build", current_state: "",
  skills: [],
};
async function api(path) {
  if (path === "/shells") return { shells: [shell] };
  if (path === "/shell-templates") return { templates: [] };
  if (path === "/shells/3") return shell;
  throw new Error("unexpected API call: " + path);
}
const microlabel = (text) => el("span", {}, text);
function glassDropdown() { return el("button", {}, "Code-01"); }
function openNewShellModal() {}
function setStatus() {}
function toast() {}
const rendered = [];
function renderHarness() { rendered.push("harness"); }
function renderSkillViewer() { rendered.push("skills"); }
function renderSkillAssignments() { rendered.push("assignments"); }
function renderDefaultModels() { rendered.push("models"); }
""" + SHELL_RENDER + r"""
function all(root, predicate, found = []) {
  if (predicate(root)) found.push(root);
  for (const child of root.children || [])
    if (child?.nodeType === 1) all(child, predicate, found);
  return found;
}

(async () => {
  const views = [];
  for (const key of ["harness", "skills", "assignments", "models"]) {
    shellTab = key;
    const root = new FakeElement("div");
    await renderShells(root);
    const tabs = all(root, (node) => node.className === "vtabs")[0];
    const buttons = tabs.children;
    const hashes = [];
    for (const button of buttons) {
      button.onclick();
      hashes.push(location.hash);
    }
    views.push({
      key,
      labels: buttons.map((button) => button.textContent),
      active: buttons.filter((button) => button.className === "active-tab")
        .map((button) => button.textContent),
      hashes,
      paneClass: all(root, (node) => node.className.includes("shell-pane"))[0]
        .className,
      rendered: rendered.at(-1),
    });
  }
  console.log(JSON.stringify({ views }));
})().catch((error) => {
  console.error(error.stack || error);
  process.exit(1);
});
"""
    result = run_js(script)
    labels = ["Harness", "Skills", "Skill Assignments", "Default Models"]
    hashes = [
        "shells",
        "shells-skills",
        "shells-skill-assignments",
        "shells-default-models",
    ]
    for view, label in zip(result["views"], labels, strict=True):
        assert view["labels"] == labels
        assert view["active"] == [label]
        assert view["hashes"] == hashes
        assert view["rendered"] == view["key"]
    assert result["views"][2]["paneClass"] == "shell-pane skill-assignments"
    assert result["views"][0]["paneClass"] == "shell-pane"


def test_overlapping_shell_renders_discard_the_stale_response():
    script = r"""
class FakeElement {
  constructor(tag) {
    this.tagName = tag;
    this.nodeType = 1;
    this.children = [];
    this.className = "";
    this._text = "";
    this.classList = {
      add: (name) => {
        const names = new Set(this.className.split(" ").filter(Boolean));
        names.add(name);
        this.className = [...names].join(" ");
      },
    };
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
globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (text) => ({ nodeType: 3, textContent: String(text ?? "") }),
};
const el = (tag, props = {}, ...kids) => {
  const node = Object.assign(new FakeElement(tag), props);
  for (const kid of kids)
    node.append(kid?.nodeType ? kid : document.createTextNode(kid ?? ""));
  return node;
};
""" + SHELL_STATE + r"""
const shells = [
  {
    shell_id: 3, shortname: "DEV3", display_name: "Code-01",
    flavor: "dev", role: "first role", mandate: "first mandate", skills: [],
  },
  {
    shell_id: 4, shortname: "DEV4", display_name: "Code-02",
    flavor: "dev", role: "second role", mandate: "second mandate", skills: [],
  },
];
const detailRequests = [];
async function api(path) {
  if (path === "/shells") return { shells };
  if (path === "/shell-templates") return { templates: [] };
  if (path.startsWith("/shells/")) return new Promise((resolve) => {
    detailRequests.push({ path, resolve });
  });
  throw new Error("unexpected API call: " + path);
}
const microlabel = (text) => el("span", {}, text);
function glassDropdown({ items, value }) {
  return el("button", {}, items.find((item) => item.value === value).label);
}
function openNewShellModal() {}
function setStatus() {}
function toast() {}
function renderHarness() {}
function renderSkillViewer() {}
function renderSkillAssignments() {}
function renderDefaultModels() {}
""" + SHELL_RENDER + r"""
const tick = () => new Promise((resolve) => setImmediate(resolve));

(async () => {
  const root = new FakeElement("div");
  selectedShell = 3;
  const stale = renderShells(root);
  while (detailRequests.length < 1) await tick();

  selectedShell = 4;
  const current = renderShells(root);
  while (detailRequests.length < 2) await tick();

  detailRequests.find((request) => request.path === "/shells/4").resolve(shells[1]);
  await current;
  detailRequests.find((request) => request.path === "/shells/3").resolve(shells[0]);
  await stale;

  console.log(JSON.stringify({
    text: root.textContent,
    subbars: root.children.filter((child) => child.className === "subbar").length,
  }));
})().catch((error) => {
  console.error(error.stack || error);
  process.exit(1);
});
"""
    result = run_js(script)
    assert result["subbars"] == 1
    assert "Code-02" in result["text"]
    assert "second role" in result["text"]
    assert "Code-01" not in result["text"]
    assert "first role" not in result["text"]


def test_model_refresh_verifies_and_renders_fork_local_harness_evidence():
    script = r"""
class FakeElement {
  constructor(tag) {
    this.tagName = tag;
    this.nodeType = 1;
    this.children = [];
    this.className = "";
    this._text = "";
    this.disabled = false;
  }
  append(...nodes) { this.children.push(...nodes); }
  set textContent(value) { this._text = String(value ?? ""); this.children = []; }
  get textContent() {
    return this._text + this.children.map(
      (child) => typeof child === "string" ? child : child.textContent
    ).join("");
  }
}
globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (text) => ({ nodeType: 3, textContent: String(text ?? "") }),
};
const el = (tag, props = {}, ...kids) => {
  const node = Object.assign(new FakeElement(tag), props);
  for (const kid of kids)
    node.append(kid?.nodeType ? kid : document.createTextNode(kid ?? ""));
  return node;
};
const catalog = {
  harnesses: {}, sources: ["models.dev", "codex-cache"],
  fetched_at: "2026-08-09T18:00:00+00:00", stale: false,
  verification: {
    checked_at: "2026-08-09T18:01:00+00:00", runtime: "sandbox",
    harnesses: {
      codex: {
        version: "0.147.0", compatibility: "newer-unverified", error: null,
      },
      vibe: { version: null, compatibility: null, error: "HARNESS_UNAVAILABLE" },
    },
    defaults: [{
      flavor: "planner", harness: "vibe", model: "vibe-model",
      runnable: false, state: "harness-error", reason: "HARNESS_UNAVAILABLE",
    }],
    summary: {
      harnesses_ready: 1, harnesses_checked: 2,
      exact_routes_runnable: 1, exact_routes: 2, harness_defaults: 0,
    },
  },
};
const requests = [];
async function api(path) {
  requests.push(path);
  if (path === "/flavor-defaults")
    return { flavors: {}, harnesses: ["codex", "vibe"] };
  if (path === "/models" || path === "/models?refresh=1") return catalog;
  throw new Error("unexpected API call: " + path);
}
const statuses = [];
function setStatus(value) { statuses.push(value); }
function toast() {}
const microlabel = (text) => el("span", {}, text);
function all(root, predicate, found = []) {
  if (predicate(root)) found.push(root);
  for (const child of root.children || [])
    if (child?.nodeType === 1) all(child, predicate, found);
  return found;
}
""" + DEFAULT_MODELS + r"""
(async () => {
  const root = new FakeElement("div");
  await renderDefaultModels(root, {});
  const button = all(root, (node) => node.tagName === "button")[0];
  await button.onclick();
  console.log(JSON.stringify({ text: root.textContent, requests, statuses }));
})().catch((error) => {
  console.error(error.stack || error);
  process.exit(1);
});
"""
    result = run_js(script)
    assert result["requests"] == [
        "/flavor-defaults", "/models", "/models?refresh=1",
        "/flavor-defaults",
    ]
    assert result["statuses"] == [
        "refreshing model catalog and harnesses…",
        "refresh complete — review verification warnings",
    ]
    assert "Refresh & verify" in result["text"]
    assert "Fork verification" in result["text"]
    assert "codex0.147.0newer-unverified" in result["text"]
    assert "vibenot installedHARNESS_UNAVAILABLE" in result["text"]
    assert "exact defaults: 1/2 runnable" in result["text"]
    assert "planner · vibe · vibe-model — HARNESS_UNAVAILABLE" in result["text"]
