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
from html.parser import HTMLParser
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

NAV_GROUPS = [["interface", "sprints", "shells"], ["roadmap", "docs", "flags"],
              ["worktrees", "map"], ["analytics", "scripts"]]
NAV_TABS = [tab for group in NAV_GROUPS for tab in group]
DRAWER_MOUNTS = ["drawer-appearance", "drawer-tokens", "drawer-models", "drawer-engine",
                 "drawer-account"]
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

def test_navigation_shows_all_ten_views_in_four_groups_with_three_separators():
    nav = INDEX[INDEX.index("<nav>") + len("<nav>"):INDEX.index("</nav>")]
    # Every child of <nav> is a tab button or a separator: nothing is nested
    # behind a trigger, so every view is one click away.
    children = re.findall(r"<(\w+)([^>]*)>", nav)
    kinds = []
    for tag, attrs in children:
        tab = re.search(r'data-tab="([^"]+)"', attrs)
        if tag == "button" and tab:
            kinds.append(tab.group(1))
        else:
            assert (tag, attrs.strip()) == ("span", 'class="navsep"'), (tag, attrs)
            kinds.append("|")
    expected = []
    for group in NAV_GROUPS:
        expected += group + ["|"]
    assert kinds == expected[:-1]
    assert len(NAV_TABS) == 10 and kinds.count("|") == 3
    assert [label for label in re.findall(r">([^<>]+)</button>", nav)] == [
        "Chats", "Sprints", "Shells", "Roadmap", "Documents", "Flags",
        "Worktrees", "Repo Map", "Analytics", "Scripts"]


def test_the_more_group_is_gone_from_markup_script_and_styles():
    for served in (INDEX, APP, STYLE):
        assert "nav-more" not in served
    assert "setNavMore" not in APP
    assert ">More<" not in INDEX and "aria-haspopup=\"true\"" not in INDEX


def test_every_nav_entry_is_a_view_and_every_view_has_a_nav_entry_and_section():
    views = re.findall(r'^  (\w+): \["#view-(\w+)", render\w+\],$',
                       APP[APP.index("const VIEWS = {"):APP.index("async function load(tab)")],
                       re.MULTILINE)
    assert sorted(key for key, _ in views) == sorted(NAV_TABS)
    for key, section in views:
        assert key == section
        assert INDEX.count(f'id="view-{key}"') == 1
        assert INDEX.count(f'data-tab="{key}"') == 1


def test_flat_nav_marks_the_active_tab_and_routes_every_button_by_hash():
    show = APP[APP.index("function show(tab)"):APP.index("// Hash routing:")]
    assert 'for (const b of document.querySelectorAll("nav button")) {' in show
    assert 'const on = b.dataset.tab === tab;\n    b.classList.toggle("active", on);' in show
    # A scrolled nav keeps the active tab in view.
    assert 'if (on) b.scrollIntoView?.({ block: "nearest", inline: "nearest" });' in show
    # Every nav button is a route: one handler, no trigger to special-case.
    assert ('document.querySelectorAll("nav button").forEach((b) => '
            '(b.onclick = () => { location.hash = b.dataset.tab; }));') in APP


def rules_outside_media(selector: str) -> list[str]:
    """Declarations of every top-level (not @media-nested) rule for selector."""
    return re.findall(r"^" + re.escape(selector) + r" \{\s*([^}]*?)\s*\}", STYLE, re.MULTILINE)


def test_header_gives_way_in_the_nav_at_every_width_not_at_one_breakpoint():
    # The rules that keep the header to one row hold at every width: the nav
    # scrolls within itself, and the tools zone's children neither wrap nor
    # shrink, so its automatic minimum is its whole content on one line.
    nav = " ".join(rules_outside_media("nav"))
    for declaration in ("flex: 0 1 auto;", "min-width: 0;", "overflow-x: auto;",
                        "overflow-y: hidden;", "scrollbar-width: none;"):
        assert declaration in nav
    assert rules_outside_media("nav > *") == ["flex: none; white-space: nowrap;"]
    assert rules_outside_media(".tools > *") == ["flex: none; white-space: nowrap;"]
    tools = " ".join(rules_outside_media(".tools"))
    assert "flex: 1 1 0;" in tools and "min-width" not in tools and "overflow" not in tools
    status = " ".join(rules_outside_media(".status"))
    assert "max-width: 22rem;" in status and "text-overflow: ellipsis;" in status
    # No width breakpoint carries the header between the phone layout and full width.
    assert re.findall(r"@media \(max-width: (\d+)px\) \{\n(?:[^@]*?\n)?  (?:header|nav)\b", STYLE) == ["760"]
    # The scrolling nav clips what sits outside it: underline and ring stay inside.
    assert "bottom: 0; height: 2px;" in " ".join(rules_outside_media("nav button.active::after"))
    assert rules_outside_media("nav button:focus-visible") == ["box-shadow: inset var(--focus-ring);"]
    narrow = STYLE[STYLE.index("@media (max-width: 760px) {"):]
    narrow = narrow[:narrow.index("\n}\n")]
    assert "nav { flex: 1 1 0; min-width: 35%; }" in narrow


def test_brand_truncation_is_scoped_to_the_header_and_never_reaches_document_headings():
    # A bare h1 rule with nowrap/ellipsis would cut every rendered document
    # and chat heading to one line.
    for match in re.finditer(r"(?:^|[\n,{}])\s*([^{}\n]*\bh1\b[^{}\n]*)\{([^}]*)\}", STYLE):
        selectors, body = match.group(1), match.group(2)
        if not any(word in body for word in ("nowrap", "ellipsis", "overflow")):
            continue
        for selector in selectors.split(","):
            if re.search(r"\bh1\b", selector):
                assert selector.strip().startswith("header h1"), selector
    assert rules_outside_media("header h1") == [
        "min-width: 6rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;"]
    narrow = STYLE[STYLE.index("@media (max-width: 760px) {"):]
    narrow = narrow[:narrow.index("\n}\n")]
    assert "  header h1 { flex: 0 1 auto; min-width: 0; }" in narrow
    assert not re.search(r"^  h1\b", narrow, re.MULTILINE)


NAV_WHEEL = APP[APP.index("function navWheel(e) {"):APP.index("// Header chrome (spec #270)")]


@needs_node
def test_nav_wheel_scrolls_sideways_only_when_that_is_the_only_thing_to_do():
    script = r"""
const listeners = [];
const document = {querySelector: (selector) => ({
  addEventListener: (type, fn, options) => listeners.push([selector, type, fn.name, options])})};
""" + NAV_WHEEL + r"""
const run = (nav, event) => {
  let prevented = 0;
  const state = {scrollWidth: 1000, clientWidth: 400, scrollLeft: 0, ...nav};
  navWheel({deltaX: 0, deltaY: 0, ctrlKey: false, ...event, currentTarget: state,
    preventDefault() { prevented += 1; }});
  return [state.scrollLeft, prevented];
};
console.log(JSON.stringify({
  listeners,
  down: run({}, {deltaY: 120}),
  up: run({scrollLeft: 300}, {deltaY: -120}),
  zoom: run({}, {deltaY: 120, ctrlKey: true}),
  sideways: run({}, {deltaY: 10, deltaX: 40}),
  fits: run({scrollWidth: 400}, {deltaY: 120}),
  atStart: run({scrollLeft: 0}, {deltaY: -120}),
  atEnd: run({scrollLeft: 600}, {deltaY: 120}),
  nearEnd: run({scrollLeft: 599.5}, {deltaY: 120}),
}));
"""
    assert run_js(script) == {
        "listeners": [["nav", "wheel", "navWheel", {"passive": False}]],
        "down": [120, 1], "up": [180, 1],
        # Left to the browser: zoom, a sideways gesture, a nav that fits, and
        # a nav already at the end the wheel is heading for (the page scrolls).
        "zoom": [0, 0], "sideways": [0, 0], "fits": [0, 0],
        "atStart": [0, 0], "atEnd": [600, 0], "nearEnd": [599.5, 0],
    }


def test_header_keeps_status_and_snapshot_and_gains_one_drawer_button():
    tools = INDEX[INDEX.index('<div class="tools">'):INDEX.index("</header>")]
    assert tools.index('id="status"') < tools.index('id="snapshot"') < tools.index('id="drawer-open"')
    assert tools.count("<button") == 2
    assert 'id="drawer-open" aria-haspopup="dialog" aria-label="Open settings drawer"' in tools
    assert '$("#drawer-open").onclick = openSettingsDrawer;' in APP


# ── Drawer ────────────────────────────────────────────────────────────────────

def test_settings_drawer_declares_the_five_mounts_in_order():
    block = APP[APP.index("const DRAWER_SECTIONS = ["):APP.index("let closeSettingsDrawer")]
    assert re.findall(r'id: "([^"]+)"', block) == DRAWER_MOUNTS
    assert re.findall(r'label: "([^"]+)"', block) == ["Appearance", "Tokens", "Models", "Engine", "Account"]
    assert 'id: "drawer-appearance", label: "Appearance", render: renderDrawerAppearance' in block
    assert block.count("render: null") == 4


@needs_node
def test_drawer_builds_labelled_mounts_and_closes_on_escape_backdrop_and_button():
    drawer = APP[APP.index("const DRAWER_FOCUSABLE"):APP.index("// The operator's settings drawer")]
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
  contains(node) { return all(this, (n) => n === node).length > 0; }
  querySelectorAll(selector) {
    const tags = selector.split(",").map((part) => part.trim());
    return all(this, (n) => n !== this && tags.includes(n.tagName));
  }
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
// One Tab press as the browser delivers it to the overlay: returns where focus
// ends up and whether the drawer took the move over from the browser.
function tab(overlay, shiftKey = false) {
  let prevented = false;
  overlay.onkeydown({ key: "Tab", shiftKey, preventDefault: () => { prevented = true; } });
  return [document.activeElement.textContent || document.activeElement.className, prevented];
}
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
        mount.append(el("div", {}, "filled"),
          el("button", { textContent: "first-control" }),
          el("button", { textContent: "disabled-control", disabled: true }),
          el("button", { textContent: "last-control" }));
        closes.ctx = ctx.close; } },
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
  initialFocus: document.activeElement.className,
};
// Focus trap: the close button is the first stop, last-control the last, and
// the disabled control is never a stop.
result.shiftTabFromFirst = tab(overlay, true);
result.tabFromLast = tab(overlay);
result.tabInTheMiddle = tab(overlay);
opener.focus();
result.tabFromOutside = tab(overlay);
opener.focus();
result.shiftTabFromOutside = tab(overlay, true);
let otherKeyPrevented = false;
overlay.onkeydown({ key: "a", preventDefault: () => { otherKeyPrevented = true; } });
result.otherKeysPass = !otherKeyPrevented;
opener.focus();
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
        "filled": 4, "empty": 0, "failed": "error: boom",
        "closeIsShared": True,
        "initialFocus": "drawer-close",
        "shiftTabFromFirst": ["last-control", True],
        "tabFromLast": ["✕", True],
        "tabInTheMiddle": ["✕", False],
        "tabFromOutside": ["✕", True],
        "shiftTabFromOutside": ["last-control", True],
        "otherKeysPass": True,
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

class _InlineScriptAudit(HTMLParser):
    """Collects what the CSP would refuse: scripts without a src, script
    bodies, and on* handler attributes on any element."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.script_sources: list[str] = []
        self.inline_scripts: list[str] = []
        self.inline_handlers: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        names = dict(attrs)
        self.inline_handlers += [f"{tag} {name}" for name in names if name.startswith("on")]
        if tag == "script":
            self._in_script = True
            if names.get("src"):
                self.script_sources.append(names["src"])
            else:
                self.inline_scripts.append("<script> without src")

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if self._in_script and data.strip():
            self.inline_scripts.append(data.strip())

def test_theme_script_loads_from_head_before_the_stylesheet_with_no_inline_script():
    head = INDEX[INDEX.index("<head>"):INDEX.index("</head>")]
    assert head.index('<script src="/theme.js"></script>') < head.index('href="/style.css"')
    # Parsed, not pattern-matched: the parser lower-cases tag and attribute
    # names, so no spelling of a script tag or handler slips past.
    page = _InlineScriptAudit()
    page.feed(INDEX)
    page.close()
    assert page.script_sources == ["/theme.js", "/vendor/marked.umd.js",
                                   "/vendor/purify.min.js", "/app.js"]
    assert page.inline_scripts == []
    # no inline event handlers either: the CSP would drop them silently
    assert page.inline_handlers == []


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


def _resolve(name: str, raw: dict[str, str], surface: str) -> str:
    """A token's painted colour over an opaque surface, following var() and
    compositing `color-mix(in srgb, <colour> N%, transparent)` as alpha N."""
    value = raw[name].strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        return value
    alias = re.fullmatch(r"var\((--[\w-]+)\)", value)
    if alias:
        return _resolve(alias.group(1), raw, surface)
    mix = re.fullmatch(
        r"color-mix\(in srgb, var\((--[\w-]+)\) (\d+)%, transparent\)", value)
    assert mix, f"{name}: cannot composite {value!r}"
    top = _resolve(mix.group(1), raw, surface)
    alpha = int(mix.group(2)) / 100
    channels = [
        round(int(top[i:i + 2], 16) * alpha + int(surface[i:i + 2], 16) * (1 - alpha))
        for i in (1, 3, 5)
    ]
    return "#" + "".join(f"{c:02x}" for c in channels)


def _raw_tokens(theme: str) -> dict[str, str]:
    raw = dict(re.findall(r"(--[\w-]+):\s*([^;]+);", token_block(DARK_OPEN)))
    if theme == "light":
        raw.update(re.findall(r"(--[\w-]+):\s*([^;]+);", token_block(LIGHT_OPEN)))
    return raw


# WCAG 1.4.11: graphics that carry meaning without text hold 3:1 against the
# surface they are drawn on. Each entry is (token, surfaces it is drawn on).
ALL_SURFACES = ["--bg", "--panel", "--panel2", "--menu-bg"]
GRAPHICS = [
    ("--scroll-thumb", ALL_SURFACES),          # any scrollable region
    ("--scroll-thumb-hover", ALL_SURFACES),
    ("--wire", ["--bg", "--panel"]),           # roadmap flow dependency wires
    ("--wire-soft", ["--bg", "--panel"]),      # sprint board dependency wires
    ("--control-border", ALL_SURFACES),        # input, select, switch outlines
    ("--accent", ALL_SURFACES),                # focus ring, chart line, lit wires
    ("--dim", ["--panel", "--panel2"]),        # chart crosshair, switch knob
    ("--tint-6", ["--panel2"]),                # resting switch knob on its track
    ("--ok", ["--bg", "--panel"]),             # status dots and meter fills
    ("--warn", ["--bg", "--panel", "--panel2"]),
    ("--task-open", ["--bg", "--panel"]),
    ("--danger-strong", ["--bg", "--panel", "--panel2"]),
]


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_graphical_tokens_meet_wcag_non_text_contrast_when_composited(theme):
    raw = _raw_tokens(theme)
    failures = []
    for token, surfaces in GRAPHICS:
        for surface in surfaces:
            base = _resolve(surface, raw, "#000000")
            ratio = _contrast(_resolve(token, raw, base), base)
            if ratio < 3.0:
                failures.append(f"{token} on {surface}: {ratio:.2f}")
    assert failures == []


def test_graphical_rules_draw_from_the_tokens_the_contrast_test_covers():
    assert "scrollbar-color: var(--scroll-thumb) transparent;" in STYLE
    assert "::-webkit-scrollbar-thumb { background: var(--scroll-thumb);" in STYLE
    assert "::-webkit-scrollbar-thumb:hover { background: var(--scroll-thumb-hover); }" in STYLE
    assert ":focus-visible { outline: none; box-shadow: var(--focus-ring); }" in STYLE
    assert "--focus-ring: 0 0 0 2px var(--accent);" in token_block(DARK_OPEN)
    assert "--focus-ring" not in token_block(LIGHT_OPEN)
    assert ("textarea:focus, input:focus, select:focus "
            "{ outline: none; border-color: var(--accent); }") in STYLE
    assert ".chat-composer-field:focus-within { border-color: var(--accent); }" in STYLE
    fields = STYLE[STYLE.index("textarea, input[type=text], select {"):]
    assert "border: 1px solid var(--control-border);" in fields[:fields.index("}")]
    for at in [m.start() for m in re.finditer(r"^\.switch \.slider \{", STYLE, re.MULTILINE)]:
        assert "border: 1px solid var(--control-border);" in STYLE[at:STYLE.index("}", at)]
    assert ".an-line { fill: none; stroke: var(--accent);" in STYLE
    assert ".an-cross { stroke: var(--dim);" in STYLE
    # No focus indicator or scrollbar still rides the faint neutral ramp.
    for line in STYLE.splitlines():
        if "focus" in line or "scrollbar" in line:
            assert "var(--tint-" not in line, line


def test_svg_wires_and_charts_draw_from_tokens():
    for selector in (".sprint-wire {", ".flow-wire {"):
        rule = STYLE[STYLE.index(selector):STYLE.index("}", STYLE.index(selector))]
        assert "stroke: var(--wire" in rule
    chart = "\n".join(line for line in STYLE.splitlines() if line.startswith(".an-"))
    assert chart and LITERAL.search(chart) is None
    for attribute in ("fill:", "stroke:"):
        for value in re.findall(rf"{attribute}\s*([^;}}]+)", chart):
            assert value.strip() == "none" or "var(--" in value
