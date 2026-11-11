"""Drawer contents and model refresh contracts for spec 270.

The Tokens and Models sections of the settings drawer, the Configure
"Refresh models" control, and the catalogue read that "＋ Chat" issues without
waiting on it. String contracts over app.js and style.css, plus node runs of
the real functions against a minimal fake DOM and a recording fetch.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / ".super-coder" / "ui"
APP = (UI / "app.js").read_text()
STYLE = (UI / "style.css").read_text()

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is required")

LITERAL = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\(")


def between(start: str, end: str) -> str:
    begin = APP.index(start)
    return APP[begin:APP.index(end, begin)]


API = between("async function api(", "function requestKey()")
TOKEN_HELPERS = between("const fmtTok =", "const localDay =")
CATALOGUE = between("let modelCatalogue = null;", "async function renderDefaultModels")
DRAWER_BLOCK = between("// ── Settings drawer: Tokens, Models, Engine", "// ── Analytics ──")
TOKENS = DRAWER_BLOCK[DRAWER_BLOCK.index("const DRAWER_TOKEN_DAYS"):
                      DRAWER_BLOCK.index("// Models: what the server says")]
MODELS = DRAWER_BLOCK[DRAWER_BLOCK.index("// Models: what the server says"):
                      DRAWER_BLOCK.index("// Engine: the controls arrive")]
WATCH = DRAWER_BLOCK[DRAWER_BLOCK.index("const CATALOGUE_WATCH_MS"):]
NEW_CHAT = between("  newChat.onclick = async () => {", "  configure.onclick = async () => {")
RENDER_NEW = between("async function chatRenderNew(", "function chatTranscriptPageItems")

FAKE_DOM = r"""
class FakeElement {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.nodeType = 1; this.children = [];
    this.className = ""; this.attributes = {}; this.parentElement = null;
    this._text = ""; this.disabled = false;
  }
  // Like the DOM, assigning textContent replaces the children.
  get textContent() { return this._text; }
  set textContent(value) { this.children = []; this._text = String(value); }
  append(...nodes) {
    for (const node of nodes) {
      this.children.push(node);
      if (node?.nodeType === 1) node.parentElement = this;
    }
  }
  // Like a real <select>, replacing the options drops the current selection.
  replaceChildren(...nodes) { this.children = []; this._value = undefined; this.append(...nodes); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  get options() { return all(this, (node) => node.tagName === "OPTION"); }
  get value() {
    if (this.tagName !== "SELECT") return this._value ?? "";
    const options = this.options;
    if (options.some((option) => option.value === this._value)) return this._value;
    return (options.find((option) => option.selected) || options[0])?.value ?? "";
  }
  set value(next) { this._value = next; }
}
function all(node, predicate, out = []) {
  for (const child of node?.children || []) {
    if (child?.nodeType === 1 && predicate(child)) out.push(child);
    all(child, predicate, out);
  }
  return out;
}
function el(tag, props = {}, ...kids) {
  const node = new FakeElement(tag);
  Object.assign(node, props);
  for (const kid of kids)
    node.append(typeof kid === "string" ? {nodeType: 3, textContent: kid} : kid);
  return node;
}
const text = (node) => node.nodeType === 3 ? node.textContent
  : node.children.length ? node.children.map(text).join("") : node.textContent;
const tick = () => new Promise((resolve) => setImmediate(resolve));
"""


def run_js(script: str) -> dict:
    proc = subprocess.run(["node", "-e", script], text=True, capture_output=True, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ── String contracts ──────────────────────────────────────────────────────────

def test_drawer_sections_are_filled_by_assignment_in_this_lane():
    for mount, render in [("drawer-tokens", "renderDrawerTokens"),
                          ("drawer-models", "renderDrawerModels"),
                          ("drawer-engine", "renderDrawerEngine")]:
        assert f'["{mount}", {render}]' in DRAWER_BLOCK
        assert f"function {render}(mount" in DRAWER_BLOCK
    assert "DRAWER_SECTIONS.find((section) => section.id === id).render = render" in DRAWER_BLOCK


def test_token_section_reads_stored_sessions_and_never_sweeps():
    assert 'api("/analytics/sessions?days=" + DRAWER_TOKEN_DAYS)' in TOKENS
    assert "const DRAWER_TOKEN_DAYS = 7;" in TOKENS
    assert "cardTotal(card)" in TOKENS and "fmtTok(total.week)" in TOKENS
    # The only sweep stays in the full Analytics view.
    assert "sweep" not in DRAWER_BLOCK.replace("never a sweep", "").replace("the sweep that", "")
    assert APP.count('api("/analytics/sweep", "POST")') == 1
    assert 'href: "#analytics"' in TOKENS
    assert "link.onclick = () => close();" in TOKENS


def test_models_section_shows_server_freshness_and_refreshes_on_demand():
    assert "catalog.refresh_completed_at || catalog.fetched_at" in MODELS
    assert "catalog.stale_reason" in MODELS
    for reason in ("ttl", "generation_mismatch", "local_source_changed"):
        assert f"  {reason}: " in MODELS
    assert 'textContent: "Refresh models"' in MODELS
    assert "await loadModelCatalogue(true)" in MODELS
    assert 'refresh ? "/models?refresh=1" : "/models"' in CATALOGUE


def test_engine_section_holds_a_neutral_placeholder():
    engine = DRAWER_BLOCK[DRAWER_BLOCK.index("function renderDrawerEngine"):
                          DRAWER_BLOCK.index("for (const [id, render]")]
    assert "Engine controls are not available in this build." in engine
    assert "api(" not in engine and "fetch(" not in engine


def test_configure_refresh_and_new_chat_use_only_the_server_stale_flag():
    assert 'textContent: "Refresh models"' in RENDER_NEW
    assert "const refreshed = await loadModelCatalogue(true);" in RENDER_NEW
    assert 'el("div", { className: "chat-refresh-row" }, refreshModels, refreshNote)' in RENDER_NEW
    assert "watchCatalogueOnNewChat();" in NEW_CHAT
    assert "await watchCatalogueOnNewChat" not in APP
    assert NEW_CHAT.index("watchCatalogueOnNewChat();") < NEW_CHAT.index("chatCreateConversation(shell)")
    # No client-side staleness rule: no clock, TTL or timestamp comparison.
    assert "if (!catalog.stale || catalogueWatching) return;" in WATCH
    for clock in ("Date.", "fetched_at", "refresh_completed_at", "ttl"):
        assert clock not in WATCH


def test_lane_css_block_closes_the_stylesheet_and_uses_tokens_only():
    block = STYLE[STYLE.index("/* ── Drawer contents and model refresh (spec 270)"):]
    assert "\n/* " not in block[3:], "the lane block is the last block in style.css"
    assert not LITERAL.search(block)
    for selector in (".drawer-tokens", ".drawer-tok-num", ".drawer-link",
                     ".drawer-model-state.stale", ".chat-refresh-models", ".chat-refresh-note"):
        assert selector + " " in block or selector + ":" in block
    declared = set(re.findall(r"(--[\w-]+):", STYLE))
    assert set(re.findall(r"var\((--[\w-]+)\)", block)) <= declared


# ── Behaviour under node ──────────────────────────────────────────────────────

@needs_node
def test_token_section_renders_today_and_week_per_harness_from_one_get():
    script = FAKE_DOM + API + TOKEN_HELPERS + TOKENS + r"""
const calls = [];
const now = new Date();
const earlier = new Date(now.getTime() - 3 * 864e5);
const sessions = [
  {harness: "claude", started_at: now.toISOString(), input_tokens: 1000, output_tokens: 500,
   cache_read_tokens: 0, cache_write_tokens: 0, reasoning_tokens: 9999},
  {harness: "claude", started_at: earlier.toISOString(), input_tokens: 2000000},
  {harness: "codex", started_at: earlier.toISOString(), output_tokens: 250},
  {harness: null, started_at: null, input_tokens: 5},
];
globalThis.fetch = async (url, init) => {
  calls.push([init.method, url]);
  return {ok: true, json: async () => ({sessions, next_before: null})};
};
(async () => {
  const mount = el("div");
  let closed = 0;
  renderDrawerTokens(mount, {close: () => { closed += 1; }});
  const loading = text(mount.children[0]);
  await tick(); await tick();
  const table = mount.children[0];
  const cells = table.children.map(text);
  const link = mount.children[mount.children.length - 1];
  link.onclick();
  const fixed = drawerTokenSummary(
    [{harness: "kimi", started_at: "2026-03-04T12:00:00", input_tokens: 7},
     {harness: "kimi", started_at: "2026-03-02T12:00:00", input_tokens: 3}],
    new Date("2026-03-04T18:00:00"));
  console.log(JSON.stringify({calls, loading, tableClass: table.className, cells,
    href: link.href, linkText: text(link), closed, fixed}));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    assert run_js(script) == {
        "calls": [["GET", "/api/analytics/sessions?days=7"]],
        "loading": "Loading…",
        "tableClass": "drawer-tokens",
        "cells": ["", "Today", "7 days",
                  "Total", "1.5k", "2.0M",
                  "claude", "1.5k", "2.0M",
                  "codex", "0", "250",
                  "unknown", "0", "5"],
        "href": "#analytics", "linkText": "Open full analytics →", "closed": 1,
        "fixed": {"total": {"today": 7, "week": 10},
                  "harnesses": [{"harness": "kimi", "today": 7, "week": 10}]},
    }


@needs_node
def test_token_section_reports_an_empty_window_and_a_failed_read():
    script = FAKE_DOM + API + TOKEN_HELPERS + TOKENS + r"""
let fail = false;
globalThis.fetch = async () => fail
  ? {ok: false, statusText: "boom", json: async () => ({error: "db locked"})}
  : {ok: true, json: async () => ({sessions: [], next_before: null})};
(async () => {
  const empty = el("div");
  renderDrawerTokens(empty, {close() {}});
  await tick(); await tick();
  fail = true;
  const failed = el("div");
  renderDrawerTokens(failed, {close() {}});
  await tick(); await tick();
  console.log(JSON.stringify({empty: empty.children.map(text), failed: failed.children.map(text)}));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    assert run_js(script) == {
        "empty": ["Today7 daysTotal00", "No recorded sessions in the last 7 days.",
                  "Open full analytics →"],
        "failed": ["token summary error: db locked", "Open full analytics →"],
    }


@needs_node
def test_models_section_paints_stale_reason_and_refresh_requests_refresh_1():
    script = FAKE_DOM + API + CATALOGUE + MODELS + r"""
const calls = [], responses = [];
function toast() {}
globalThis.fetch = async (url) => {
  calls.push(url);
  return {ok: true, json: async () => responses.shift()};
};
const recent = new Date(Date.now() - 5 * 60000).toISOString();
responses.push({catalogue_generation: "a", stale: true, stale_reason: "ttl",
  fetched_at: "2020-01-01T00:00:00+00:00", harnesses: {}});
responses.push({catalogue_generation: "b", stale: false, fetched_at: "2020-01-01T00:00:00+00:00",
  refresh_completed_at: recent, harnesses: {}});
responses.push({catalogue_generation: "b", stale: true, stale_reason: "odd_reason",
  refresh_completed_at: recent, harnesses: {}});
(async () => {
  const mount = el("div");
  renderDrawerModels(mount);
  await tick(); await tick();
  const [age, state, actions] = mount.children;
  const button = actions.children[0];
  const first = {age: age.textContent.split(" · ")[0], state: state.textContent, cls: state.className};
  const pending = button.onclick();
  const during = {disabled: button.disabled, label: button.textContent};
  await pending;
  const second = {age: age.textContent.split(" · ")[0], state: state.textContent,
    cls: state.className, disabled: button.disabled, label: button.textContent};
  await button.onclick();
  const third = {state: state.textContent, cls: state.className};
  console.log(JSON.stringify({calls, first, during, second, third,
    never: catalogueFreshness({stale: false}).age}));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = run_js(script)
    assert result["calls"] == ["/api/models", "/api/models?refresh=1", "/api/models?refresh=1"]
    assert re.fullmatch(r"Fetched \d+ d ago", result["first"]["age"])
    assert result["first"]["state"] == "Stale — the cache is older than its time-to-live"
    assert result["first"]["cls"] == "drawer-model-state stale"
    assert result["during"] == {"disabled": True, "label": "Refreshing…"}
    assert result["second"] == {"age": "Fetched 5 min ago", "state": "Current",
                                "cls": "drawer-model-state", "disabled": False,
                                "label": "Refresh models"}
    assert result["third"] == {"state": "Stale — odd_reason (the refresh did not complete)",
                               "cls": "drawer-model-state stale"}
    assert result["never"] == "Never fetched"


@needs_node
def test_new_chat_issues_the_catalogue_get_and_creates_without_waiting_for_it():
    script = FAKE_DOM + API + CATALOGUE + WATCH + r"""
const calls = [], notices = [];
function toast(message) { notices.push(message); }
// The catalogue read never answers: creation must finish regardless.
globalThis.fetch = (url) => { calls.push(url); return new Promise(() => {}); };
const location = {};
const shell = {shortname: "DEV1"};
const selectedConversation = null;
const newChat = {disabled: false, textContent: "＋ Chat"};
const configure = {disabled: false};
const chatCloseForSwitch = async () => true;
const chatWithShellRelease = (run) => run();
const chatCreateConversation = async () => { calls.push("create"); return {conversation_id: "cv_1"}; };
const chatHash = (shortname, id) => `interface/${shortname}/${id}`;
const chatBusyToast = (error) => { throw error; };
""" + NEW_CHAT + r"""
(async () => {
  await newChat.onclick();
  console.log(JSON.stringify({calls, hash: location.hash, notices}));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    assert run_js(script) == {
        "calls": ["/api/models", "create"],
        "hash": "interface/DEV1/cv_1",
        "notices": [],
    }


@needs_node
@pytest.mark.parametrize("scenario,expected", [
    # fresh answer: no notice, no polling
    ("fresh", {"notices": 0, "reads": 1, "timers": 0, "watching": False}),
    # stale, then fresh on the third poll: one notice, reload, stop
    ("recovers", {"notices": 1, "reads": 4, "timers": 3, "watching": False}),
    # stale forever (and one failed read): stops at the cap without a second notice
    ("gives_up", {"notices": 1, "reads": 21, "timers": 20, "watching": False}),
    # two clicks while stale: one watch, one notice
    ("double", {"notices": 1, "reads": 4, "timers": 2, "watching": False}),
])
def test_stale_catalogue_gets_one_notice_and_one_bounded_watch(scenario, expected):
    script = WATCH + r"""
const SCENARIO = "__SCENARIO__";
const notices = [], timers = [];
let reads = 0, scheduled = 0;
function toast(message) { notices.push(message); }
globalThis.setTimeout = (fn, ms) => { scheduled += 1; timers.push([fn, ms]); };
async function loadModelCatalogue(refresh) {
  if (refresh) throw new Error("the watch must never force a refresh");
  reads += 1;
  if (SCENARIO === "fresh") return {stale: false};
  if (SCENARIO === "recovers") return {stale: reads < 4};
  if (SCENARIO === "double") return {stale: reads < 4};
  if (reads === 5) throw new Error("transient");
  return {stale: true};
}
const settle = async () => { for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r)); };
(async () => {
  watchCatalogueOnNewChat();
  if (SCENARIO === "double") watchCatalogueOnNewChat();
  await settle();
  const delays = new Set();
  while (timers.length) {
    const [fn, ms] = timers.shift();
    delays.add(ms);
    fn();
    await settle();
  }
  console.log(JSON.stringify({notices: notices.length, reads, timers: scheduled,
    watching: catalogueWatching, delays: [...delays], limit: CATALOGUE_WATCH_LIMIT,
    notice: notices[0] || null}));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = run_js(script.replace("__SCENARIO__", scenario))
    assert {key: result[key] for key in expected} == expected
    assert result["limit"] == 20
    if expected["timers"]:
        assert result["delays"] == [3000]
        assert result["notice"] == "Model catalogue is stale — refreshing in the background."


@needs_node
def test_configure_refresh_repaints_pickers_and_keeps_valid_selections():
    script = FAKE_DOM + r"""
const CHAT_HARNESS_DEFAULT_VALUE = "__sc_harness_default__";
const CHAT_HARNESSES_FROM_SERVER = () => ["claude", "codex"];
const chatHarnessUnavailableReason = () => "";
const refreshes = [], responses = [], efforts = [];
let dropdownRefreshes = 0;
async function loadModelCatalogue(refresh = false) {
  refreshes.push(refresh);
  const next = responses.shift();
  if (next instanceof Error) throw next;
  return next;
}
function chatModelOptions(select, catalog, harness, defaultModel) {
  select.replaceChildren(el("option", {value: "", textContent: "default " + defaultModel}));
  for (const model of catalog.harnesses?.[harness]?.models || [])
    select.append(el("option", {value: model.id, textContent: model.id}));
}
function chatModelDropdown(select) {
  return {root: el("div", {}, select), refresh: () => { dropdownRefreshes += 1; }};
}
function renderNativeOptionControl(select, harness, catalog, model, preferred) {
  efforts.push({harness, model, preferred: preferred ?? null,
    models: (catalog.harnesses?.[harness]?.models || []).map((entry) => entry.id)});
  select.replaceChildren(...["", "low", "medium", "high"].map((value) => el("option", {
    value, textContent: value || "default", selected: value === (preferred || "")})));
  return {disabled: false, requiresConfirmation: false, native: true,
    selected: preferred || null, guidance: "ok"};
}
const catalogue = (...ids) => ({stale: false, harnesses: {
  codex: {models: ids.map((id) => ({id}))}, claude: {models: [{id: "opus"}]}}});
""" + RENDER_NEW + r"""
(async () => {
  const host = el("div");
  const defaults = {flavors: {dev: [
    {harness: "claude", is_default: true, model: "opus", effort: "medium"},
    {harness: "codex", model: "gpt-a", effort: "low"},
  ]}, harness_status: {}};
  await chatRenderNew(host, {flavor: "dev", display_name: "Dev"}, defaults, catalogue("gpt-a", "gpt-b"));
  const form = host.children[0];
  const [harnessSelect, effortSelect] = all(form, (n) => n.tagName === "SELECT"
    && n.parentElement === form);
  const modelSelect = all(form, (n) => n.tagName === "SELECT" && n.parentElement !== form)[0];
  const title = all(form, (n) => n.tagName === "INPUT")[0];
  const button = all(form, (n) => n.className === "chat-refresh-models")[0];
  const note = all(form, (n) => n.className === "chat-refresh-note")[0];
  const state = () => ({harness: harnessSelect.value, model: modelSelect.value,
    effort: effortSelect.value, title: title.value, note: note.textContent,
    button: [button.disabled, button.textContent], last: efforts[efforts.length - 1]});

  // The operator picks a non-default harness, model, thinking level and title.
  harnessSelect.value = "codex"; harnessSelect.onchange();
  modelSelect.value = "gpt-b"; modelSelect.onchange();
  effortSelect.value = "high"; effortSelect.onchange();
  title.value = "My chat";
  const before = state();

  responses.push(catalogue("gpt-b", "gpt-c"));
  const pending = button.onclick();
  const during = [button.disabled, button.textContent];
  await pending;
  const kept = state();

  responses.push(catalogue("gpt-c"));
  await button.onclick();
  const dropped = state();

  responses.push({...catalogue("gpt-c"), stale: true});
  await button.onclick();
  const stale = state().note;

  responses.push(new Error("offline"));
  await button.onclick();
  const failed = state();

  console.log(JSON.stringify({refreshes, before, during, kept, dropped, stale, failed,
    dropdownRefreshes, role: note.role}));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = run_js(script)
    assert result["refreshes"] == [True, True, True, True]
    assert result["during"] == [True, "Refreshing…"]
    assert result["role"] == "status"
    assert result["before"]["model"] == "gpt-b" and result["before"]["effort"] == "high"
    # Still offered: harness, model, thinking level and title all survive, and
    # the pickers are repainted from the new catalogue.
    assert result["kept"] == {
        "harness": "codex", "model": "gpt-b", "effort": "high", "title": "My chat",
        "note": "Models refreshed.", "button": [False, "Refresh models"],
        "last": {"harness": "codex", "model": "gpt-b", "preferred": "high",
                 "models": ["gpt-b", "gpt-c"]},
    }
    # No longer offered: fall back to the shell default route, and say so.
    assert result["dropped"] == {
        "harness": "codex", "model": "", "effort": "low", "title": "My chat",
        "note": "Models refreshed — the selected model is no longer offered.",
        "button": [False, "Refresh models"],
        "last": {"harness": "codex", "model": "gpt-a", "preferred": "low", "models": ["gpt-c"]},
    }
    assert result["stale"] == "The refresh did not complete; showing the last catalogue."
    assert result["failed"]["note"] == "Refresh failed: offline"
    assert result["failed"]["button"] == [False, "Refresh models"]
    assert result["failed"]["title"] == "My chat"
    # first paint, harness change, then one repaint per successful refresh
    assert result["dropdownRefreshes"] == 5


# ── Acceptance criterion 5: a forced-stale cache file, end to end ─────────────

@needs_node
def test_forced_stale_cache_file_reports_stale_and_new_chat_does_not_wait(tmp_path):
    """The server half and the client half of criterion 5, joined on one payload.

    A cache file older than its TTL is written to temporary state; the function
    GET /api/models serves answers `stale: true` and asks for a background
    refresh (stubbed: no network, no live instance). That exact answer is then
    handed, late, to the real "＋ Chat" handler.
    """
    import sys
    from unittest import mock

    sys.path.insert(0, str(ROOT / ".super-coder" / "api"))
    sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))
    import model_catalog as mc

    cache = tmp_path / "model_catalog.json"
    cache.write_text(json.dumps({
        "v": mc.PAYLOAD_VERSION, "fetched_at": "2020-01-01T00:00:00+00:00",
        "stale": False, "sources": [], "harnesses": {
            "codex": {"models": [{"id": "gpt-old", "availability": "available"}]}},
    }))
    with mock.patch.object(mc, "CACHE", cache), \
            mock.patch.object(mc, "_start_background_refresh") as refresh:
        served = mc.catalog(env={}, opencode_provider=lambda: [])
    assert served["stale"] is True
    assert served["stale_reason"] == "ttl"
    assert refresh.call_count == 1, "a stale read starts the server's own refresh"
    assert json.loads(cache.read_text())["stale"] is False, "the read does not rewrite the cache"

    script = FAKE_DOM + API + CATALOGUE + WATCH + r"""
const SERVED = __SERVED__;
const calls = [], notices = [], timers = [];
let answerModels;
function toast(message) { notices.push(message); }
globalThis.setTimeout = (fn, ms) => { timers.push(ms); };
globalThis.fetch = (url) => {
  calls.push(url);
  return new Promise((resolve) => { answerModels = () => resolve({ok: true, json: async () => SERVED}); });
};
const location = {};
const shell = {shortname: "DEV1"};
const selectedConversation = null;
const newChat = {disabled: false, textContent: "＋ Chat"};
const configure = {disabled: false};
const chatCloseForSwitch = async () => true;
const chatWithShellRelease = (run) => run();
const chatCreateConversation = async () => { calls.push("create"); return {conversation_id: "cv_1"}; };
const chatHash = (shortname, id) => `interface/${shortname}/${id}`;
const chatBusyToast = (error) => { throw error; };
""" + NEW_CHAT + r"""
(async () => {
  await newChat.onclick();
  const created = {calls: [...calls], hash: location.hash, notices: [...notices]};
  answerModels();                       // the catalogue answers only now
  for (let i = 0; i < 10; i++) await new Promise((resolve) => setImmediate(resolve));
  console.log(JSON.stringify({created, notices, timers, adopted: modelCatalogue.stale}));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = run_js(script.replace("__SERVED__", json.dumps(served)))
    assert result["created"] == {"calls": ["/api/models", "create"],
                                 "hash": "interface/DEV1/cv_1", "notices": []}
    assert result["notices"] == ["Model catalogue is stale — refreshing in the background."]
    assert result["timers"] == [3000], "one bounded watch is armed"
    assert result["adopted"] is True
