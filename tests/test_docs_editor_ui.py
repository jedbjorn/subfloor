"""The Docs editor never saves a body it did not load."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / ".super-coder" / "ui" / "app.js").read_text()
DOC = APP[APP.index("function retirementBanner"):APP.index("// ── Docs ──")]

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required"
)

HARNESS = r"""
class FakeElement {
  constructor(tag) { this.tagName = tag; this.nodeType = 1; this.children = []; this.dataset = {}; }
  append(...nodes) { this.children.push(...nodes); }
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
const toasts = [], statuses = [], calls = [], gets = [];
const toast = (m) => toasts.push(m);
const setStatus = (m) => statuses.push(m);
// GETs park until the test settles them; PATCHes resolve at once.
const api = (path, method = "GET", body) => {
  calls.push({ path, method, body });
  if (method !== "GET") return Promise.resolve({});
  return new Promise((resolve, reject) => gets.push({ resolve, reject }));
};
const tick = () => new Promise((r) => setImmediate(r));
const find = (n, pred) => {
  if (!n?.children) return null;
  for (const c of n.children) { if (pred(c)) return c; const hit = find(c, pred); if (hit) return hit; }
  return null;
};
const parts = (d, opts) => {
  const wrap = docBlock(d, opts);
  return {
    wrap,
    edit: find(wrap, (c) => c.textContent === "edit"),
    save: find(wrap, (c) => c.textContent === "save doc"),
    ta: find(wrap, (c) => c.tagName === "textarea"),
  };
};
const DOCROW = { document_id: 7, kind: "spec", seq: 1, title: "t" };
"""


def run_js(body: str) -> dict:
    script = HARNESS + DOC + body
    proc = subprocess.run(["node", "-e", script], text=True, capture_output=True, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_editor_is_disabled_until_the_body_load_resolves():
    result = run_js(r"""
(async () => {
  const { edit, save, ta } = parts(DOCROW);
  edit.onclick(); await tick();
  const during = { ta: ta.disabled, save: save.disabled };
  ta.value = "typed too early";
  await save.onclick();                       // a forced click still submits nothing
  const patchesDuring = calls.filter((c) => c.method === "PATCH").length;
  gets[0].resolve({ body: "real body" }); await tick();
  const after = { ta: ta.disabled, save: save.disabled, value: ta.value };
  ta.value = "edited"; await save.onclick();
  console.log(JSON.stringify({ during, patchesDuring, after,
    patches: calls.filter((c) => c.method === "PATCH").map((c) => c.body), statuses }));
})();
""")
    assert result == {
        "during": {"ta": True, "save": True},
        "patchesDuring": 0,
        "after": {"ta": False, "save": False, "value": "real body"},
        "patches": [{"body": "edited"}],
        "statuses": ["doc saved"],
    }


def test_retoggle_during_load_never_issues_a_second_get():
    result = run_js(r"""
(async () => {
  const { edit, ta } = parts(DOCROW);
  edit.onclick(); edit.onclick(); edit.onclick(); await tick();   // open, close, open
  gets[0].resolve({ body: "real body" }); await tick();
  ta.value = "edited";
  edit.onclick(); edit.onclick(); await tick();                   // close, reopen after load
  console.log(JSON.stringify({ gets: gets.length, value: ta.value }));
})();
""")
    assert result == {"gets": 1, "value": "edited"}


def test_failed_load_closes_the_editor_and_a_later_edit_retries():
    result = run_js(r"""
(async () => {
  const { wrap, edit, save, ta } = parts(DOCROW);
  const box = find(wrap, (c) => c.children?.includes(ta));
  edit.onclick(); await tick();
  gets[0].reject(new Error("boom")); await tick();
  const failed = { hidden: box.hidden, ta: ta.disabled, save: save.disabled, toasts: [...toasts] };
  await save.onclick();
  edit.onclick(); await tick();
  gets[1].resolve({ body: "real body" }); await tick();
  console.log(JSON.stringify({ failed, patches: calls.filter((c) => c.method === "PATCH").length,
    retried: { hidden: box.hidden, ta: ta.disabled, value: ta.value } }));
})();
""")
    assert result == {
        "failed": {"hidden": True, "ta": True, "save": True, "toasts": ["error: boom"]},
        "patches": 0,
        "retried": {"hidden": False, "ta": False, "value": "real body"},
    }


def test_retired_frozen_and_read_only_rows_carry_no_editor():
    result = run_js(r"""
(async () => {
  const has = (d, opts) => { const p = parts(d, opts); return !!(p.edit || p.save || p.ta); };
  console.log(JSON.stringify({
    retired: has({ ...DOCROW, retired: 1, retired_date: "2026-09-01" }),
    frozen: has({ ...DOCROW, frozen: 1 }),
    readOnly: has(DOCROW, { readOnly: true }),
    gets: gets.length,
  }));
})();
""")
    assert result == {"retired": False, "frozen": False, "readOnly": False, "gets": 0}
