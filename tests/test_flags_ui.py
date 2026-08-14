"""Flags create/edit modal and expanded-card action contracts."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / ".super-coder" / "ui" / "app.js").read_text()
STYLE = (ROOT / ".super-coder" / "ui" / "style.css").read_text()
FLAG_FORM = APP[
    APP.index("function openFlagModal"):
    APP.index("async function renderFlags")
]
FLAG_ROW = APP[
    APP.index("function flagRow"):
    APP.index("// ── Scripts")
]

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required"
)


def run_js(script: str) -> dict:
    proc = subprocess.run(
        ["node", "-e", script], text=True, capture_output=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


ELEMENT_HARNESS = r"""
class FakeElement {
  constructor(tag) {
    this.tagName = tag; this.nodeType = 1; this.children = []; this.className = "";
    this.value = ""; this.selected = false; this.disabled = false; this._text = "";
  }
  append(...nodes) { this.children.push(...nodes); }
  focus() { this.focused = true; }
  set textContent(value) { this._text = String(value ?? ""); this.children = []; }
  get textContent() {
    return this._text + this.children.map((node) =>
      typeof node === "string" ? node : (node.textContent || "")).join("");
  }
}
globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (text) => ({nodeType: 3, textContent: String(text ?? "")}),
};
const el = (tag, props = {}, ...kids) => {
  const node = Object.assign(new FakeElement(tag), props);
  for (const kid of kids) node.append(kid?.nodeType ? kid : document.createTextNode(kid ?? ""));
  return node;
};
"""


def test_create_and_edit_share_one_roomy_form_and_send_exact_mutations():
    script = ELEMENT_HARNESS + r"""
let captured = null, closed = 0;
const calls = [], statuses = [], loads = [], errors = [];
function openActionModal(options) { captured = options; return () => { closed += 1; }; }
async function api(...args) { calls.push(args); return {}; }
function setStatus(value) { statuses.push(value); }
function load(value) { loads.push(value); }
function toast(value) { errors.push(value); }
""" + FLAG_FORM + r"""
(async () => {
  const features = [{feature_id: 7, title: "Seven"}, {feature_id: 8, title: "Eight"}];
  const flag = {flag_id: 41, display_name: "SC-041", description: "Original body",
    feature_id: 7, priority: "Low", resolved: 1, resolution_notes: "done"};
  openFlagModal(features, flag);
  const edit = captured;
  const editForm = edit.bodyNode;
  const editName = editForm.children[1], editDesc = editForm.children[3];
  const editFeature = editForm.children[5], editPriority = editForm.children[7];
  const prefill = {
    title: edit.title, action: edit.actionNode.textContent,
    name: editName.value, description: editDesc.value, rows: editDesc.rows,
    feature: editFeature.children.filter((option) => option.selected).map((option) => String(option.value)),
    priority: editPriority.children.filter((option) => option.selected).map((option) => option.value),
    width: edit.width, height: edit.height,
  };
  editName.value = "SC-041A"; editDesc.value = "Revised body";
  editFeature.value = "8"; editPriority.value = "High";
  await edit.actionNode.onclick();

  openFlagModal(features);
  const create = captured;
  const createForm = create.bodyNode;
  createForm.children[1].value = "SC-NEW";
  createForm.children[3].value = "New body";
  createForm.children[5].value = "";
  createForm.children[7].value = "Medium";
  const createAction = create.actionNode.textContent;
  await create.actionNode.onclick();

  console.log(JSON.stringify({
    prefill,
    editCall: calls[0], createCall: calls[1],
    createTitle: create.title, createAction,
    createRows: createForm.children[3].rows, createHeight: create.height,
    dismissals: [edit.dismissNode.textContent, create.dismissNode.textContent],
    closed, statuses, loads, errors,
  }));
})();
"""
    result = run_js(script)
    assert result["prefill"] == {
        "title": "Edit flag #41",
        "action": "Save",
        "name": "SC-041",
        "description": "Original body",
        "rows": 8,
        "feature": ["7"],
        "priority": ["Low"],
        "width": 600,
        "height": 520,
    }
    assert result["editCall"] == [
        "/flags/41",
        "PATCH",
        {
            "display_name": "SC-041A",
            "description": "Revised body",
            "feature_id": "8",
            "priority": "High",
        },
    ]
    assert result["createCall"] == [
        "/flags",
        "POST",
        {
            "display_name": "SC-NEW",
            "description": "New body",
            "feature_id": None,
            "priority": "Medium",
        },
    ]
    assert result["createTitle"] == "New flag"
    assert result["createAction"] == "Create"
    assert result["createRows"] == 8
    assert result["createHeight"] == 520
    assert result["dismissals"] == ["Cancel", "Cancel"]
    assert result["closed"] == 2
    assert result["statuses"] == ["flag saved", "flag created"]
    assert result["loads"] == ["flags", "flags"]
    assert result["errors"] == []


def test_expanded_cards_keep_resolve_left_and_edit_anchored_right():
    script = ELEMENT_HARNESS + r"""
let opened = null, prevented = false, stopped = false;
function openFlagModal(features, flag) { opened = {features, flag}; }
function prompt() { return null; }
async function api() {}
function setStatus() {}
function load() {}
function toast() {}
""" + FLAG_ROW + r"""
const features = [{feature_id: 7, title: "Seven"}];
const base = {flag_id: 4, display_name: "SC-4", priority: "Medium",
  description: "Body", feature_id: 7, feature_title: "Seven"};
const openRow = flagRow({...base, resolved: 0}, features);
const resolvedRow = flagRow({...base, resolved: 1, resolved_date: "2026-08-01",
  resolution_notes: "done"}, features);
const openActions = openRow.children[1].children.at(-1);
const resolvedActions = resolvedRow.children[1].children.at(-1);
openActions.children[1].onclick({
  preventDefault() { prevented = true; }, stopPropagation() { stopped = true; },
});
console.log(JSON.stringify({
  openClass: openActions.className,
  openLabels: openActions.children.map((node) => node.textContent),
  resolvedLabels: resolvedActions.children.map((node) => node.textContent),
  editClass: openActions.children[1].className,
  openedFlag: opened.flag.flag_id,
  featureCount: opened.features.length,
  prevented, stopped,
}));
"""
    result = run_js(script)
    assert result == {
        "openClass": "flag-actions",
        "openLabels": ["resolve", "edit"],
        "resolvedLabels": ["edit"],
        "editClass": "act flag-edit",
        "openedFlag": 4,
        "featureCount": 1,
        "prevented": True,
        "stopped": True,
    }
    assert ".flag-actions .flag-edit { margin-left: auto; }" in STYLE


def test_managed_advisory_is_labeled_nonblocking_and_has_no_human_actions():
    script = ELEMENT_HARNESS + r"""
function openFlagModal() { throw new Error("managed advisory must not be editable"); }
function prompt() { throw new Error("managed advisory must not be resolvable"); }
async function api() { throw new Error("managed advisory must not mutate through UI"); }
function setStatus() {}
function load() {}
function toast() {}
""" + FLAG_ROW + r"""
const row = flagRow({
  flag_id: 77, display_name: "Native sandbox readiness advisory", priority: "Low",
  description: "exact evidence", resolved: 0, management_state: "system",
  severity: "advisory", blocking_scope: "none", blocks_runtime: 0,
}, []);
const head = row.children[0], body = row.children[1];
console.log(JSON.stringify({
  rowClass: row.className,
  headText: head.textContent,
  bodyText: body.textContent,
  actionCount: body.children.at(-1).children.length,
}));
"""
    result = run_js(script)
    assert result["rowClass"] == "flag advisory"
    assert "Advisory" in result["headText"]
    assert "System-managed" in result["headText"]
    assert "Does not block runtime or shell entry" in result["bodyText"]
    assert result["actionCount"] == 0
