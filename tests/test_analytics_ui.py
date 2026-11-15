"""Token analytics UI request scoping."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / ".super-coder" / "ui" / "app.js").read_text()

EL = APP[APP.index("const el ="):APP.index("const esc =")]
STATE = APP[APP.index("let anFilters ="):APP.index("const AN_CLASSES")]
CLASSES = APP[APP.index("const AN_CLASSES"):APP.index("const fmtTok")]
QUERY = APP[APP.index("function anQuery"):APP.index("const AN_RANGES")]
RANGES = APP[APP.index("const AN_RANGES"):APP.index("async function anLoadPage")]
SECTION = APP[APP.index("function anSelect"):APP.index("// ── Provider Quota")]

HAS_NODE = shutil.which("node") is not None
pytestmark = pytest.mark.skipif(not HAS_NODE, reason="node is required")

DOM = r"""
class FakeElement {
  constructor(tag) {
    this.tagName = tag; this.nodeType = 1; this.children = [];
    this._text = ""; this.className = ""; this.style = {}; this.value = "";
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; this._text = ""; }
  set textContent(value) { this._text = String(value ?? ""); this.children = []; }
  get textContent() {
    return this._text + this.children.map(
      (c) => typeof c === "string" ? c : (c.textContent || "")).join("");
  }
}
globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (text) => ({ nodeType: 3, textContent: String(text ?? "") }),
};
let calls = [];
async function api(path, method = "GET") {
  calls.push({ path, method });
  if (path === "/analytics/filters")
    return { harnesses: ["codex", "kimi"], models: ["gpt", "kimi-code/k3"] };
  if (path.startsWith("/analytics/usage"))
    return { favorite_models: [{ flavor: "dev", model: "kimi-code/k3", sessions: 2 }] };
  return {};
}
async function anLoadPage(days) { anDaysLoaded = days; }
function anChart() { return new FakeElement("div"); }
function anBuckets() { return [{ value: 0, date: new Date() }]; }
function fmtTok(value) { return String(value ?? 0); }
function localDay(value) { return String(value); }
function localTime(value) { return String(value); }
function cardTotal() { return 0; }
function microlabel(text) { return new FakeElement("span"); }
function statRow() { return new FakeElement("div"); }
function toast() {}
function all(root, pred, found = []) {
  if (pred(root)) found.push(root);
  for (const c of root.children || []) if (c && c.nodeType === 1) all(c, pred, found);
  return found;
}
function out(value) { console.log(JSON.stringify(value)); }
"""


def test_favorite_usage_requests_keep_filters_and_follow_the_time_chips():
    script = EL + STATE + CLASSES + QUERY + RANGES + DOM + SECTION + r"""
(async () => {
  anFilters.harness = "codex";
  anFilters.model = "kimi-code/k3";
  const root = new FakeElement("div");
  await anTokenSection(root);
  const month = all(root, (n) => n.tagName === "button" && n.textContent === "1M")[0];
  month.onclick();
  await new Promise((resolve) => setTimeout(resolve, 0));
  const usage = calls.filter((c) => c.path.startsWith("/analytics/usage"))
    .map((c) => {
      const url = new URL(c.path, "http://local");
      return Object.fromEntries(url.searchParams.entries());
    });
  out({ usage, favoriteVisible: root.textContent.includes("kimi-code/k3") });
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""
    proc = subprocess.run(["node", "-e", script], text=True, capture_output=True, check=False)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])

    assert len(result["usage"]) == 2
    assert result["favoriteVisible"] is True
    for query in result["usage"]:
        assert query["harness"] == "codex"
        assert query["model"] == "kimi-code/k3"
    assert result["usage"][0]["from"] != result["usage"][1]["from"]
