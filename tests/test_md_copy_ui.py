"""Rendered markdown code blocks carry a copy-to-clipboard button."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / ".super-coder" / "ui" / "app.js").read_text()
STYLE = (ROOT / ".super-coder" / "ui" / "style.css").read_text()
MD = APP[APP.index("function mdBlock"):APP.index("async function api(")]

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required"
)

HARNESS = r"""
class FakeElement {
  constructor(tag, text = "") { this.tagName = tag; this.nodeType = 1; this.children = []; this._text = text; }
  append(...nodes) { this.children.push(...nodes); }
  get textContent() {
    return this._text + this.children.map((n) => typeof n === "string" ? n : n.textContent).join("");
  }
  set innerHTML(html) { this.pres = [...html.matchAll(/<pre>(.*?)<\/pre>/gs)].map((m) => new FakeElement("pre", m[1])); }
  querySelectorAll(sel) { return sel === "pre" ? this.pres || [] : []; }
}
const document = {
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (text) => text,
};
const el = (t, props = {}, ...kids) => {
  const n = Object.assign(document.createElement(t), props);
  for (const k of kids) n.append(k?.nodeType ? k : document.createTextNode(k ?? ""));
  return n;
};
const marked = { parse: (s) => s };
const DOMPurify = { sanitize: (s) => s };
const toasts = [];
const toast = (m) => toasts.push(m);
"""


def run_js(body: str) -> dict:
    script = HARNESS + MD + body
    proc = subprocess.run(["node", "-e", script], text=True, capture_output=True, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_each_code_block_copies_its_own_text():
    result = run_js(r"""
(async () => {
  const copied = [];
  Object.defineProperty(globalThis, "navigator", { value: { clipboard: { writeText: async (t) => { copied.push(t); } } }, configurable: true });
  const div = mdBlock("<p>x</p><pre>ls -la</pre><pre>git status</pre>");
  const buttons = div.pres.map((p) => p.children.find((c) => c.className === "md-copy"));
  for (const b of buttons) await b.onclick();
  console.log(JSON.stringify({ n: buttons.filter(Boolean).length, copied, toasts }));
})();
""")
    assert result == {"n": 2, "copied": ["ls -la", "git status"], "toasts": ["copied", "copied"]}


def test_missing_clipboard_api_reports_failure():
    result = run_js(r"""
(async () => {
  Object.defineProperty(globalThis, "navigator", { value: {}, configurable: true });
  const div = mdBlock("<pre>echo hi</pre>");
  await div.pres[0].children[0].onclick();
  console.log(JSON.stringify({ toasts }));
})();
""")
    assert result == {"toasts": ["copy failed"]}


def test_copy_button_is_positioned_inside_code_block():
    assert ".md pre { position: relative;" in STYLE
    assert ".md-copy {" in STYLE and "position: absolute" in STYLE
