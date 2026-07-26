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
SHELL_STATE = APP[APP.index("let selectedShell ="):
                  APP.index("// Rough token estimator")]
SHELL_RENDER = APP[APP.index("async function renderShells(root)"):
                   APP.index("// Default Models — the flavor_defaults")]
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


def test_skills_is_nested_under_shells_instead_of_global_navigation():
    nav = INDEX[INDEX.index("<nav>"):INDEX.index("</nav>")]
    main = INDEX[INDEX.index("<main>"):INDEX.index("</main>")]
    assert nav.count('data-tab="shells"') == 1
    assert 'data-tab="skills"' not in nav
    assert 'id="view-skills"' not in main
    assert 'id="view-shells"' in main


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
