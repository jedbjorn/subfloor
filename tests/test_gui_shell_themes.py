"""GUI shell contracts for spec 270: navigation grouping, the slide-over
drawer helper, and the light and dark themes.

String contracts over the three served files plus node runs of the real
theme.js and the real drawer helper against a minimal fake DOM. The CSS
checks are the durable form of two acceptance claims: no colour literal lives
outside the token blocks, and every text token meets WCAG AA (4.5:1) on every
surface it is drawn on, in both themes.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
UI = ENGINE / "ui"
APP = (UI / "app.js").read_text()
INDEX = (UI / "index.html").read_text()
STYLE = (UI / "style.css").read_text()
THEME = (UI / "theme.js").read_text()

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is required")

PRIMARY = ["interface", "sprints", "shells", "roadmap", "docs", "flags"]
MORE = ["worktrees", "map", "analytics", "scripts"]
DRAWER_MOUNTS = ["drawer-appearance", "drawer-tokens", "drawer-models", "drawer-engine"]
LITERAL = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\(")
DARK_OPEN = ":root {"
LIGHT_OPEN = ':root[data-theme="light"] {'


def run_js(script: str) -> dict:
    proc = subprocess.run(["node", "-e", script], text=True, capture_output=True, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def token_block(opener: str) -> str:
    start = STYLE.index("\n" + opener) + 1
    return STYLE[start:STYLE.index("\n}", start)]


def hex_tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})\b", block))


# ── Navigation ────────────────────────────────────────────────────────────────

def test_navigation_groups_six_primary_views_and_a_more_group_of_four():
    nav = INDEX[INDEX.index("<nav>"):INDEX.index("</nav>")]
    more_at = nav.index('class="nav-more"')
    menu = nav[nav.index('id="nav-more-menu"'):]
    tabs = re.findall(r'<button data-tab="([^"]+)"', nav)
    assert tabs == PRIMARY + MORE
    assert re.findall(r'<button data-tab="([^"]+)"', nav[:more_at]) == PRIMARY
    assert re.findall(r'<button data-tab="([^"]+)"', menu) == MORE
    # The trigger is a nav button without a data-tab: it opens the group and
    # is never itself a route.
    assert 'id="nav-more" class="nav-more-btn" aria-haspopup="true"' in nav
    assert 'aria-controls="nav-more-menu"' in nav
    assert 'id="nav-more-menu" class="gmenu nav-more-menu" hidden' in nav


def test_every_nav_entry_is_a_view_and_every_view_has_a_nav_entry_and_section():
    views = re.findall(r'^  (\w+): \["#view-(\w+)", render\w+\],$',
                       APP[APP.index("const VIEWS = {"):APP.index("async function load(tab)")],
                       re.MULTILINE)
    assert sorted(key for key, _ in views) == sorted(PRIMARY + MORE)
    for key, section in views:
        assert key == section
        assert INDEX.count(f'id="view-{key}"') == 1
        assert INDEX.count(f'data-tab="{key}"') == 1


def test_more_group_marks_its_trigger_and_dissolves_inline_when_the_nav_scrolls():
    show = APP[APP.index("function show(tab)"):APP.index("// Hash routing:")]
    assert 'if (on && b.closest(".nav-more")) inMore = true;' in show
    assert 'b.classList.toggle("active", inMore)' in show
    assert "setNavMore(false);" in show
    assert 'b.setAttribute("aria-expanded", String(open))' in show
    assert '$("#nav-more").onclick = () => setNavMore($("#nav-more-menu").hidden);' in APP
    narrow = STYLE[STYLE.index("@media (max-width: 760px) {"):]
    narrow = narrow[:narrow.index("\n}\n")]
    assert ".nav-more, .nav-more-menu, .nav-more-menu[hidden] { display: contents; }" in narrow
    assert ".nav-more-btn { display: none; }" in narrow


def test_header_keeps_status_and_snapshot_and_gains_one_drawer_button():
    tools = INDEX[INDEX.index('<div class="tools">'):INDEX.index("</header>")]
    assert tools.index('id="status"') < tools.index('id="snapshot"') < tools.index('id="drawer-open"')
    assert tools.count("<button") == 2
    assert 'id="drawer-open" aria-haspopup="dialog" aria-label="Open settings drawer"' in tools
    assert '$("#drawer-open").onclick = openSettingsDrawer;' in APP


# ── Drawer ────────────────────────────────────────────────────────────────────

def test_settings_drawer_declares_the_four_mounts_in_order():
    block = APP[APP.index("const DRAWER_SECTIONS = ["):APP.index("let closeSettingsDrawer")]
    assert re.findall(r'id: "([^"]+)"', block) == DRAWER_MOUNTS
    assert re.findall(r'label: "([^"]+)"', block) == ["Appearance", "Tokens", "Models", "Engine"]
    assert 'id: "drawer-appearance", label: "Appearance", render: renderDrawerAppearance' in block
    assert block.count("render: null") == 3


@needs_node
def test_drawer_builds_labelled_mounts_and_closes_on_escape_backdrop_and_button():
    drawer = APP[APP.index("function openDrawer({"):APP.index("// The operator's settings drawer")]
    escape = APP[APP.index("// Esc dismisses the topmost modal."):APP.index('$("#snapshot")')]
    script = r"""
class FakeElement {
  constructor(tag) {
    this.tagName = tag; this.nodeType = 1; this.children = []; this.className = "";
    this.style = {}; this.attributes = {}; this.parentElement = null;
    this.isConnected = false; this.focusCount = 0; this.textContent = "";
  }
  append(...nodes) {
    for (const node of nodes) {
      this.children.push(node);
      if (node?.nodeType === 1) { node.parentElement = this; if (this.isConnected) connect(node, true); }
    }
  }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
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
  addEventListener: (type, fn) => { listeners[type] = fn; },
  querySelectorAll: (selector) => selector === ".modal-overlay"
    ? all(body, (node) => node.className.split(" ").includes("modal-overlay")) : [],
};
function el(tag, props = {}, ...kids) {
  const node = document.createElement(tag);
  Object.assign(node, props);
  for (const kid of kids)
    node.append(typeof kid === "string" ? {nodeType: 3, textContent: kid} : kid);
  return node;
}
""" + drawer + escape + r"""
const opener = new FakeElement("button"); body.append(opener); opener.focus();
const text = (node) => node.children.map((c) => c.textContent).join("");
const open = (closes) => openDrawer({
  title: "Settings",
  sections: [
    { id: "one", label: "One", render: (mount, ctx) => {
        mount.append(el("div", {}, "filled")); closes.ctx = ctx.close; } },
    { id: "two", label: "Two", render: null },
    { id: "three", label: "Three", render: () => { throw new Error("boom"); } },
  ],
  onClose: () => { closes.count = (closes.count || 0) + 1; },
});
const first = {};
const close = open(first);
const overlay = body.children[1];
const panel = overlay.children[0];
const mounts = all(panel, (n) => n.className === "drawer-mount");
const result = {
  overlayClass: overlay.className,
  role: panel.attributes.role, modal: panel.attributes["aria-modal"],
  labelledby: panel.attributes["aria-labelledby"],
  titleId: all(panel, (n) => n.className === "drawer-title")[0].id,
  mountIds: mounts.map((n) => n.id),
  labels: all(panel, (n) => n.className === "drawer-label").map(text),
  filled: mounts[0].children.length, empty: mounts[1].children.length,
  failed: text(mounts[2].children[0]),
  closeIsShared: first.ctx === close,
};
// Escape
listeners.keydown({ key: "Escape" });
result.afterEscape = body.children.length;
result.focusRestored = document.activeElement === opener;
listeners.keydown({ key: "Escape" });
result.onCloseOnce = first.count;
// Backdrop: a mousedown on the overlay itself closes; one inside the panel does not.
const second = {}; open(second);
const overlay2 = body.children[1];
overlay2.onmousedown({ target: overlay2.children[0] });
result.insideKeepsOpen = body.children.length;
overlay2.onmousedown({ target: overlay2 });
result.afterBackdrop = body.children.length;
// The close button.
const third = {}; open(third);
all(body.children[1], (n) => n.className === "drawer-close")[0].onclick();
result.afterButton = body.children.length;
result.closes = [first.count, second.count, third.count];
console.log(JSON.stringify(result));
"""
    assert run_js(script) == {
        "overlayClass": "modal-overlay drawer-overlay",
        "role": "dialog", "modal": "true",
        "labelledby": "drawer-title", "titleId": "drawer-title",
        "mountIds": ["one", "two", "three"],
        "labels": ["One", "Two", "Three"],
        "filled": 1, "empty": 0, "failed": "error: boom",
        "closeIsShared": True,
        "afterEscape": 1, "focusRestored": True, "onCloseOnce": 1,
        "insideKeepsOpen": 2, "afterBackdrop": 1, "afterButton": 1,
        "closes": [1, 1, 1],
    }


def test_appearance_control_offers_system_light_dark_through_the_theme_contract():
    control = APP[APP.index("const THEME_CHOICES"):APP.index("// Read-only skill-content viewer")]
    assert '[["system", "System"], ["light", "Light"], ["dark", "Dark"]]' in control
    assert "const theme = window.scTheme;" in control
    assert "theme.set(value); paint();" in control
    assert 'group.setAttribute("role", "radiogroup")' in control
    assert 'b.setAttribute("aria-checked", String(on))' in control
    # app.js never touches storage for the theme: theme.js owns it.
    assert "sc-theme" not in APP


# ── Theme application ─────────────────────────────────────────────────────────

def test_theme_script_loads_from_head_before_the_stylesheet_with_no_inline_script():
    head = INDEX[INDEX.index("<head>"):INDEX.index("</head>")]
    assert head.index('<script src="/theme.js"></script>') < head.index('href="/style.css"')
    assert re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", INDEX) == []
    # no inline event handlers either: the CSP would drop them silently
    assert re.findall(r"<[^>]*\son\w+\s*=", INDEX) == []


def test_theme_script_is_a_served_static_route():
    sys.path.insert(0, str(ENGINE / "api"))
    import server
    assert server._STATIC["/theme.js"] == (
        "theme.js", "application/javascript; charset=utf-8")
    assert "script-src 'self'" in server._CSP


@needs_node
def test_theme_resolves_saved_choice_then_os_scheme_and_survives_blocked_storage():
    script = r"""
function boot({ stored = null, osLight = false, storageThrows = false }) {
  const store = { "sc-theme": stored };
  const attrs = {};
  const mediaListeners = [];
  const media = { matches: osLight, addEventListener: (_, fn) => mediaListeners.push(fn) };
  const window = {
    matchMedia: (query) => { window.query = query; return media; },
    addEventListener: () => {},
    localStorage: {
      getItem: (k) => { if (storageThrows) throw new Error("blocked"); return store[k] ?? null; },
      setItem: (k, v) => { if (storageThrows) throw new Error("blocked"); store[k] = v; },
      removeItem: (k) => { if (storageThrows) throw new Error("blocked"); delete store[k]; },
    },
  };
  const document = { documentElement: { setAttribute: (k, v) => { attrs[k] = v; } } };
  new Function("window", "document", THEME_SOURCE)(window, document);
  return { window, attrs, store, media, fire: () => mediaListeners.forEach((fn) => fn()) };
}
const out = {};
let t = boot({});
out.defaultDark = [t.attrs["data-theme"], t.attrs["data-theme-choice"], t.window.query];
t = boot({ osLight: true });
out.systemLight = t.attrs["data-theme"];
t = boot({ stored: "dark", osLight: true });
out.savedBeatsOs = [t.attrs["data-theme"], t.window.scTheme.choice()];
t = boot({ stored: "sepia", osLight: true });
out.junkIsSystem = [t.attrs["data-theme"], t.window.scTheme.choice()];
t = boot({});
out.setLight = [t.window.scTheme.set("light"), t.store["sc-theme"], t.attrs["data-theme"]];
t.media.matches = true; t.fire();
out.savedIgnoresOsChange = t.attrs["data-theme"];
out.setSystem = [t.window.scTheme.set("system"), "sc-theme" in t.store, t.window.scTheme.choice()];
t.media.matches = false; t.fire();
out.systemTracksOsChange = t.attrs["data-theme"];
t = boot({ storageThrows: true, osLight: true });
out.blockedBoot = t.attrs["data-theme"];
out.blockedSet = [t.window.scTheme.set("dark"), t.window.scTheme.choice(), t.attrs["data-theme"]];
console.log(JSON.stringify(out));
""".replace("THEME_SOURCE", json.dumps(THEME))
    assert run_js(script) == {
        "defaultDark": ["dark", "system", "(prefers-color-scheme: light)"],
        "systemLight": "light",
        "savedBeatsOs": ["dark", "dark"],
        "junkIsSystem": ["light", "system"],
        "setLight": ["light", "light", "light"],
        "savedIgnoresOsChange": "light",
        "setSystem": ["light", False, "system"],
        "systemTracksOsChange": "dark",
        "blockedBoot": "light",
        "blockedSet": ["dark", "dark", "dark"],
    }


# ── Colour tokens ─────────────────────────────────────────────────────────────

def test_no_colour_literal_lives_outside_the_token_blocks():
    dark, light = token_block(DARK_OPEN), token_block(LIGHT_OPEN)
    rules = STYLE.replace(dark, "").replace(light, "")
    offenders = [line.strip() for line in rules.splitlines() if LITERAL.search(line)]
    assert offenders == []
    assert re.search(r"\b(?:white|black)\b(?!-)", re.sub(r"/\*.*?\*/", "", rules, flags=re.S)) is None


def test_light_theme_redeclares_every_literal_colour_token():
    dark, light = token_block(DARK_OPEN), token_block(LIGHT_OPEN)
    literal_tokens = {
        name for name, value in re.findall(r"(--[\w-]+):\s*([^;]+);", dark)
        if LITERAL.search(value)
    }
    redeclared = set(re.findall(r"(--[\w-]+):", light))
    assert literal_tokens - redeclared == set()
    assert "color-scheme: dark;" in dark and "color-scheme: light;" in light


def test_every_used_custom_property_is_declared():
    used = set(re.findall(r"var\((--[\w-]+)", STYLE))
    declared = set(re.findall(r"(--[\w-]+)\s*:", STYLE))
    # set from app.js on the element that reads them
    inline = set(re.findall(r'setProperty\("(--[\w-]+)"', APP))
    inline |= set(re.findall(r'style="(--[\w-]+):', APP))
    assert sorted(used - declared - inline - {"--review-depth", "--token"}) == []


def _luminance(colour: str) -> float:
    channels = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_text_tokens_meet_wcag_aa_on_the_surfaces_they_are_drawn_on(theme):
    tokens = hex_tokens(token_block(DARK_OPEN))
    if theme == "light":
        tokens.update(hex_tokens(token_block(LIGHT_OPEN)))
    surfaces = ["--bg", "--panel", "--panel2"]
    text = ["--ink", "--dim", "--text-strong", "--text-hi", "--text-mid", "--text-low",
            "--text-faint", "--accent", "--ok", "--warn", "--danger", "--danger-strong",
            "--task-open", "--star", "--diff-add-ink", "--diff-del-ink", "--diff-hunk-ink"]
    # Popovers and dialogs sit on --menu-bg; these are the tokens drawn there.
    menu_text = ["--ink", "--dim", "--text-strong", "--text-hi", "--text-mid", "--text-low",
                 "--accent", "--ok", "--warn", "--danger"]
    pairs = [(fg, bg) for fg in text for bg in surfaces]
    pairs += [(fg, "--menu-bg") for fg in menu_text]
    pairs += [("--on-accent", "--accent"), ("--on-danger", "--danger-strong")]
    failures = [
        f"{fg} on {bg}: {_contrast(tokens[fg], tokens[bg]):.2f}"
        for fg, bg in pairs if _contrast(tokens[fg], tokens[bg]) < 4.5
    ]
    assert failures == []


def test_svg_wires_and_charts_draw_from_tokens():
    for selector in (".sprint-wire {", ".flow-wire {"):
        rule = STYLE[STYLE.index(selector):STYLE.index("}", STYLE.index(selector))]
        assert "stroke: var(--wire" in rule
    chart = "\n".join(line for line in STYLE.splitlines() if line.startswith(".an-"))
    assert chart and LITERAL.search(chart) is None
    for attribute in ("fill:", "stroke:"):
        for value in re.findall(rf"{attribute}\s*([^;}}]+)", chart):
            assert value.strip() == "none" or "var(--" in value
