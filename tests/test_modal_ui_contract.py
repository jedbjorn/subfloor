"""Shared modal frame behavior and caller contracts."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / ".super-coder" / "ui" / "app.js").read_text()
MODAL_BLOCK = APP[
    APP.index("let modalSequence = 0"):
    APP.index("// Unified edit modal")
]
ESCAPE_BLOCK = APP[
    APP.index("// Esc dismisses the topmost modal."):
    APP.index('$("#snapshot")')
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


def test_action_frame_slots_accessibility_and_every_close_path_restore_focus():
    harness = r"""
class FakeElement {
  constructor(tag) {
    this.tagName = tag; this.nodeType = 1; this.children = []; this.className = "";
    this.style = {}; this.attributes = {}; this.parentElement = null;
    this.isConnected = false; this.focusCount = 0;
  }
  append(...nodes) {
    for (const node of nodes) {
      this.children.push(node);
      if (node?.nodeType === 1) {
        node.parentElement = this;
        if (this.isConnected) connect(node, true);
      }
    }
  }
  remove() {
    if (this.parentElement)
      this.parentElement.children = this.parentElement.children.filter((x) => x !== this);
    connect(this, false); this.parentElement = null;
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  focus() { this.focusCount += 1; document.activeElement = this; }
}
function connect(node, value) {
  if (node?.nodeType !== 1) return;
  node.isConnected = value;
  for (const child of node.children) connect(child, value);
}
function all(node, predicate, out = []) {
  if (node?.nodeType === 1 && predicate(node)) out.push(node);
  for (const child of node?.children || []) all(child, predicate, out);
  return out;
}
const body = new FakeElement("body"); body.isConnected = true;
const listeners = {};
globalThis.document = {
  body, activeElement: null,
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (text) => ({nodeType: 3, textContent: String(text ?? "")}),
  addEventListener: (type, fn) => { listeners[type] = fn; },
  querySelectorAll: (selector) => selector === ".modal-overlay"
    ? all(body, (node) => node.className === "modal-overlay") : [],
};
const el = (tag, props = {}, ...kids) => {
  const node = Object.assign(new FakeElement(tag), props);
  for (const kid of kids) node.append(kid?.nodeType ? kid : document.createTextNode(kid ?? ""));
  return node;
};
"""
    script = harness + MODAL_BLOCK + ESCAPE_BLOCK + r"""
const trigger1 = el("button", {id: "trigger-1"}); body.append(trigger1); trigger1.focus();
const dismiss = el("button", {textContent: "Cancel"});
const action = el("button", {textContent: "Save"});
const close1 = openActionModal({title: "Edit", bodyNode: el("div"), dismissNode: dismiss,
  actionNode: action, width: 400, height: 300});
const overlay1 = document.querySelectorAll(".modal-overlay").at(-1);
const dialog1 = overlay1.children[0];
const footer1 = dialog1.children[2];
const structure = {
  role: dialog1.attributes.role,
  modal: dialog1.attributes["aria-modal"],
  labelledby: dialog1.attributes["aria-labelledby"],
  titleId: dialog1.children[0].children[0].id,
  startClass: footer1.children[0].className,
  startNode: footer1.children[0].children[0].textContent,
  endClass: footer1.children[1].className,
  endNode: footer1.children[1].children[0].textContent,
};
close1();

const trigger2 = el("button", {id: "trigger-2"}); body.append(trigger2); trigger2.focus();
openModal({title: "Viewer", bodyNode: el("div"), footerEnd: el("button")});
const overlay2 = document.querySelectorAll(".modal-overlay").at(-1);
overlay2.onmousedown({target: overlay2});

const trigger3 = el("button", {id: "trigger-3"}); body.append(trigger3); trigger3.focus();
openModal({title: "Escape", bodyNode: el("div")});
listeners.keydown({key: "Escape"});

console.log(JSON.stringify({
  structure,
  directRestored: trigger1.focusCount === 2,
  overlayRestored: trigger2.focusCount === 2,
  escapeRestored: trigger3.focusCount === 2,
  overlaysLeft: document.querySelectorAll(".modal-overlay").length,
}));
"""
    result = run_js(script)
    assert result == {
        "structure": {
            "role": "dialog",
            "modal": "true",
            "labelledby": "modal-title-1",
            "titleId": "modal-title-1",
            "startClass": "modal-foot-start",
            "startNode": "Cancel",
            "endClass": "modal-foot-end",
            "endNode": "Save",
        },
        "directRestored": True,
        "overlayRestored": True,
        "escapeRestored": True,
        "overlaysLeft": 0,
    }


def test_all_modal_callers_use_semantic_action_or_named_viewer_slots():
    assert "footNodes" not in APP
    assert APP.count("const close = openActionModal({") == 6
    assert APP.count("footerStart:") == 3  # helper + two viewer callers
    assert APP.count("footerEnd:") == 3
    assert "if (overlay?.closeModal) overlay.closeModal();" in APP
