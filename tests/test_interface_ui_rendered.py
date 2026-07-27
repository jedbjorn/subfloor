"""Rendered Interface layout, disclosures, and submit coverage for spec #30.

The normal engine suite stays dependency-light, so this module skips unless
Playwright is installed.  The ``interface-rendered`` CI job installs the same
pinned browser stack as visual QA and uploads the tall/short screenshots.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


sync_api = pytest.importorskip("playwright.sync_api")
sync_playwright = sync_api.sync_playwright

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / ".super-coder" / "ui"

SHELL = {
    "shell_id": 3,
    "shortname": "DEV3",
    "display_name": "Code-01",
    "availability": "occupied",
    "session_id": 7,
    "generation": 1,
    "harness": "codex",
    # The route the session was LAUNCHED with — the only model fact the engine
    # has (flag #130, decision #55), so every surface must label it as such
    # rather than render it as the model the session is running now.
    "model_route": "gpt-5.6-terra",
    "alerts": 2,
}
OTHER_SHELL = {
    **SHELL,
    "shell_id": 4,
    "shortname": "DEV4",
    "display_name": "Code-02",
    "session_id": 8,
    "model_route": "gpt-5.6-sol",
    "alerts": 0,
}
SESSION = {
    "session_id": 7,
    "generation": 1,
    "attachable": True,
    "identity_verified": True,
    "harness": "codex",
    "model_route": "gpt-5.6-terra",
    # The third leg of the launch triple (#43 U4's column, #43 U7's consumer).
    # Present here so +Chat's relaunch can be asserted against a real payload
    # rather than against a hand-set client field.
    "launch_effort": "high",
    "lifecycle": "idle",
    "composer": "clean",
    "browser_composer": "clean",
    "writer": {"held": False},
    "clients": 1,
    "wake_state": "armed",
    "archive_id": 172,
    "occupied_at": "2026-07-24 08:00:00",
    "legal_actions": ["send_input"],
    "state_reason": None,
}
ALERT = {
    "alert_id": 120,
    "session_id": 7,
    "generation": 1,
    "category": "delivery",
    "severity": "warning",
    "reason": "visual_qa_fixture",
    "meaning": "A rendered alert row reserves real layout space.",
    "next_action": "Inspect the session before retrying.",
    "opened_at": "2026-07-24 08:30:00",
    "dismissible": True,
    "acknowledged_at": None,
    "acknowledged_by": None,
    "resolved_at": None,
}
CRITICAL_ALERT = {
    **ALERT,
    "alert_id": 121,
    "severity": "critical",
    "reason": "delivery_unknown",
    "meaning": "Delivery may have crossed the broker crash boundary.",
}
CAPABILITY = {
    **ALERT,
    "alert_id": 122,
    "category": "capability",
    "severity": "info",
    "reason": "optional_hook_missing",
    "meaning": "Optional hook detail is unavailable.",
    "dismissible": False,
}
BINDING = {
    "binding_id": 44,
    "sprint_doc_id": 31,
    "planner_shell_id": 3,
    "session_id": 7,
    "generation": 1,
    "armed_at": "2026-07-24 08:00:00",
    "released_at": None,
    "release_reason": None,
    "sprint": {
        "document_id": 31,
        "title": "Interface corrective hardening",
        "frozen": False,
        "active": True,
    },
    "wake_state": "parked",
    "items": {"queued": 2, "quarantined": 1},
    "current_batch": {"batch_id": 91, "state": "queued"},
    "last_batch": {
        "batch_id": 90,
        "state": "delivery_unknown",
        "items": {"parked": 1},
    },
    "park": {
        "batch_id": 90,
        "input_park": True,
        "reason": "wake_batch_delivery_unknown",
    },
    "quarantined": [{
        "item_id": 93,
        "message_id": 1390,
        "error": "survived 3 wake turns",
        "completed_wakes": 3,
    }],
    "retry": {"applicable": True, "needs_outcome": True},
}

# Representative active-sprint projection fixtures. They intentionally exercise
# two independently titled boards while keeping the browser gate hermetic.
SPRINTS = {
    "active_count": 2,
    "sprints": [
        {
            "document_id": 59,
            "title": "SPRINT: Worker expectation reconciler",
            "started_at": "2026-07-25T23:36:37Z",
            "planner": {"shell_id": 9, "shortname": "PLN1"},
            "feature": {
                "feature_id": 27,
                "title": "Worker expectation reconciler",
            },
            "units": [
                {
                    "seq": "U1",
                    "unit_title": "Observe worker expectations",
                    "state": "merged",
                    "state_recognized": True,
                    "dev_shell_id": 5,
                    "dev_shortname": "DEV3",
                    "reviewer_shell_id": 6,
                    "reviewer_shortname": "REV1",
                    "depends_on": None,
                    "overlap": "activity readers and projection tests",
                    "branch": "feat/reconciler-observation",
                    "pr_number": 630,
                },
                {
                    "seq": "U9f",
                    "unit_title": "Boot-directive duty-4 stamp",
                    "state": "in_review",
                    "state_recognized": True,
                    "dev_shell_id": 5,
                    "dev_shortname": "DEV3",
                    "reviewer_shell_id": 7,
                    "reviewer_shortname": "REV2",
                    "depends_on": "U8b",
                    "overlap": "boot directive renderer and contract test",
                    "branch": "fix/boot-directive-duty4",
                    "pr_number": 649,
                },
            ],
        },
        {
            "document_id": 77,
            "title": "sprint: Active sprint flow board",
            "started_at": "2026-07-26T18:44:00Z",
            "planner": {"shell_id": 10, "shortname": "PLN2"},
            "feature": {
                "feature_id": 33,
                "title": "Active sprint flow board",
            },
            "units": [
                {
                    "seq": "U1",
                    "unit_title": "Active-sprint aggregate read projection",
                    "state": "merged",
                    "state_recognized": True,
                    "dev_shell_id": 5,
                    "dev_shortname": "DEV3",
                    "reviewer_shell_id": 7,
                    "reviewer_shortname": "REV2",
                    "depends_on": None,
                    "overlap": "API projection and endpoint coverage",
                    "branch": "feat/sprints-projection",
                    "pr_number": 637,
                },
                {
                    "seq": "U2",
                    "unit_title": "Structured sprint feature linkage",
                    "state": "merged",
                    "state_recognized": True,
                    "dev_shell_id": 8,
                    "dev_shortname": "DEV4",
                    "reviewer_shell_id": 7,
                    "reviewer_shortname": "REV2",
                    "depends_on": None,
                    "overlap": "sprint declaration guidance",
                    "branch": "feat/sprint-feature-linkage",
                    "pr_number": 647,
                },
                {
                    "seq": "U4",
                    "unit_title": "Per-sprint flow boards",
                    "state": "merged",
                    "state_recognized": True,
                    "dev_shell_id": 11,
                    "dev_shortname": "DEV5",
                    "reviewer_shell_id": 7,
                    "reviewer_shortname": "REV2",
                    "depends_on": "U3",
                    "overlap": "UI cards, lanes, and dependency wires",
                    "branch": "feat/sprints-flow-boards",
                    "pr_number": 642,
                },
                {
                    "seq": "U5",
                    "unit_title": "Adversarial read-only invariant and visual gate",
                    "state": "working",
                    "state_recognized": True,
                    "dev_shell_id": 5,
                    "dev_shortname": "DEV3",
                    "reviewer_shell_id": 7,
                    "reviewer_shortname": "REV2",
                    "depends_on": "U2, U4, U99",
                    "overlap": (
                        "rendered browser assertions and selected-card "
                        "viewport screenshots"
                    ),
                    "branch": "feat/s77-readonly-visual-gate",
                    "pr_number": None,
                },
                {
                    "seq": "U2f",
                    "unit_title": "Durable declaration guidance pins",
                    "state": "working",
                    "state_recognized": True,
                    "dev_shell_id": 8,
                    "dev_shortname": "DEV4",
                    "reviewer_shell_id": 7,
                    "reviewer_shortname": "REV2",
                    "depends_on": "U2",
                    "overlap": None,
                    "branch": None,
                    "pr_number": None,
                },
            ],
        },
    ],
}


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        pass


@pytest.fixture(scope="module")
def ui_url():
    handler = lambda *args, **kwargs: QuietHandler(  # noqa: E731
        *args, directory=str(UI), **kwargs
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            browser.close()


def _json(route, value: object, status: int = 200) -> None:
    route.fulfill(
        status=status,
        content_type="application/json",
        body=json.dumps(value),
    )


def _mock_api(route) -> None:
    request = route.request
    path = request.url.split("/api", 1)[-1]
    if path == "/health":
        return _json(route, {
            "repo": "rendered-fixture",
            "port": 0,
            "artifact_mode": "local",
            "git_publication": False,
        })
    if path == "/interface/browser-sessions":
        return _json(route, {"csrf": "rendered-fixture"})
    if path == "/interface/shells":
        return _json(route, {"shells": [SHELL, OTHER_SHELL]})
    if path == "/interface/sessions/7":
        return _json(route, SESSION)
    if path == "/interface/writer-leases":
        return _json(
            route,
            {"lease_id": 11, "lease_token": "lease", "next_input_seq": 1},
        )
    if path == "/interface/stream-tickets":
        return _json(route, {"ticket": "stream-ticket"})
    if path == "/interface/browser-composer":
        body = request.post_data_json or {}
        return _json(route, {"browser_composer": body.get("state", "clean")})
    if path.startswith("/interface/sprint-bindings"):
        return _json(route, {"bindings": [BINDING]})
    if path.startswith("/interface/sprint-alerts"):
        if "include_resolved=1" in path:
            history = [
                {
                    **ALERT,
                    "alert_id": 200 + index,
                    "reason": f"rendered_history_{index:02d}",
                    "resolved_at": "2026-07-24 08:40:00",
                }
                for index in range(14)
            ]
            return _json(route, {"alerts": history})
        return _json(route, {"alerts": [ALERT, CRITICAL_ALERT, CAPABILITY]})
    return _json(route, {})


# The socket stub is shared by the layout tests (which fake the terminal) and
# the grid-fit tests (which drive the REAL vendored xterm), so it is split out.
WS_STUB = r"""
window.__wsInstances = 0;
window.__wsResizeFrames = [];
window.__terminalResizes = [];
window.__inputFrames = [];
window.__lastWs = null;

class RenderedWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  constructor() {
    window.__wsInstances += 1;
    window.__lastWs = this;
    this.readyState = RenderedWebSocket.OPEN;
    queueMicrotask(() => this.onopen?.());
  }
  send(frame) {
    if (frame instanceof Uint8Array && frame[0] === 0x03) {
      const view = new DataView(frame.buffer, frame.byteOffset, frame.byteLength);
      window.__wsResizeFrames.push({
        rows: view.getUint16(1),
        cols: view.getUint16(3),
      });
    }
    if (frame instanceof Uint8Array && frame[0] === 0x01) {
      window.__inputFrames.push(Array.from(frame));
    }
  }
  close() { this.readyState = RenderedWebSocket.CLOSED; }
}
window.WebSocket = RenderedWebSocket;
"""

# These tests are about the PANE's flex layout, so the terminal is faked. The
# fake mirrors the real contract the app depends on — a loadAddon that hands the
# terminal to the addon, and a proposeDimensions derived from the container's
# real computed box — with a pretend cell, because measuring an actual cell is
# what the grid-fit tests below exist to check.
STUB_CELL_WIDTH = 9
STUB_CELL_HEIGHT = 18
TERMINAL_STUB = r"""
class RenderedTerminal {
  constructor() {
    this.rows = 24;
    this.cols = 80;
    this._resize = null;
    this._container = null;
  }
  open(container) {
    this._container = container;
    const terminal = document.createElement("div");
    terminal.className = "xterm";
    terminal.textContent = "Rendered xterm viewport";
    container.append(terminal);
  }
  loadAddon(addon) { addon.activate(this); }
  onData(callback) { this._data = callback; }
  onResize(callback) { this._resize = callback; }
  resize(cols, rows) {
    this.cols = cols;
    this.rows = rows;
    window.__terminalResizes.push({ rows, cols });
    this._resize?.({ rows, cols });
  }
  write() {}
  reset() {}
  dispose() {}
}
window.Terminal = RenderedTerminal;

window.FitAddon = { FitAddon: class {
  activate(terminal) { this._terminal = terminal; }
  dispose() {}
  proposeDimensions() {
    const parent = this._terminal?._container;
    if (!parent) return undefined;
    const style = window.getComputedStyle(parent);
    const height = parseInt(style.getPropertyValue("height"));
    const width = Math.max(0, parseInt(style.getPropertyValue("width"))) - 14;
    if (!height || width <= 0) return undefined;
    return {
      cols: Math.max(2, Math.floor(width / __CELL_W__)),
      rows: Math.max(1, Math.floor(height / __CELL_H__)),
    };
  }
} };
""".replace("__CELL_W__", str(STUB_CELL_WIDTH)).replace(
    "__CELL_H__", str(STUB_CELL_HEIGHT)
)

INIT_SCRIPT = WS_STUB + TERMINAL_STUB


def _open_interface(
    browser, ui_url: str, *, height: int, width: int = 1600,
    api_handler=_mock_api,
):
    context = browser.new_context(viewport={"width": width, "height": height})
    page = context.new_page()
    page.add_init_script(INIT_SCRIPT)
    # Both vendor scripts must be blanked, not just xterm: the real addon-fit
    # would otherwise overwrite the stub and then decline to measure the fake
    # terminal, freezing the grid at 80x24 for every layout test.
    for asset in ("xterm.js", "addon-fit.js"):
        page.route(f"**/vendor/xterm/{asset}", lambda route: route.fulfill(
            status=200, content_type="application/javascript", body=""
        ))
    page.route("**/api/**", api_handler)
    page.goto(f"{ui_url}/#interface/DEV3", wait_until="networkidle")
    page.locator(".if-term .xterm").wait_for()
    page.wait_for_function("window.__wsResizeFrames.length > 0")
    return context, page


def _layout(page) -> dict[str, object]:
    return page.evaluate(
        """() => {
          const pane = document.querySelector(".if-pane");
          const termElement = document.querySelector(".if-term");
          const term = termElement.getBoundingClientRect();
          const composer = document.querySelector(".if-composer").getBoundingClientRect();
          const children = Array.from(pane.children);
          const nonTermHeight = children
            .filter((child) => child !== termElement)
            .reduce((height, child) => height + child.getBoundingClientRect().height, 0);
          // Spacing is MEASURED between consecutive children rather than
          // derived from rowGap × (n-1): the terminal's bottom boundary
          // cancels the pane gap (spec #43 U4 removed the 14px of dead chrome
          // under the last row), so a uniform-gap model now under-counts the
          // height the terminal is entitled to fill.
          const rects = children.map((child) => child.getBoundingClientRect());
          let spacing = 0;
          for (let i = 1; i < rects.length; i += 1)
            spacing += rects[i].top - rects[i - 1].bottom;
          const docHeight = Math.max(
            document.documentElement.scrollHeight,
            document.body.scrollHeight
          );
          return {
            innerHeight: window.innerHeight,
            docHeight,
            pageScrolls: docHeight > window.innerHeight + 1,
            termHeight: term.height,
            availableTermHeight:
              pane.getBoundingClientRect().height - nonTermHeight - spacing,
            composerHeight: composer.height,
          };
        }"""
    )


def _artifact(tmp_path: Path, name: str) -> Path:
    configured = os.environ.get("INTERFACE_VISUAL_ARTIFACTS")
    directory = Path(configured) if configured else tmp_path
    if not directory.is_absolute():
        directory = ROOT / directory
    directory.mkdir(parents=True, exist_ok=True)
    return directory / name


def test_local_artifact_mode_keeps_actions_without_idle_status_noise(browser, ui_url):
    context = browser.new_context(viewport={"width": 1600, "height": 1000})
    page = context.new_page()
    page.route("**/api/**", _mock_api)
    try:
        page.goto(ui_url, wait_until="networkidle")
        snapshot = page.locator("#snapshot")
        publish = page.locator("#publish")
        assert snapshot.text_content() == "save locally ⤓"
        assert publish.text_content() == "publish off"
        assert publish.is_disabled()
        assert page.locator("#status").text_content() == ""
    finally:
        context.close()


def test_tall_fit_short_floor_and_visual_qa(browser, ui_url, tmp_path):
    context, page = _open_interface(browser, ui_url, height=1400)
    try:
        tall = _layout(page)
        assert tall["termHeight"] > 850
        assert tall["pageScrolls"] is False
        page.screenshot(path=str(_artifact(tmp_path, "interface-tall.png")),
                        full_page=True)

        page.set_viewport_size({"width": 1600, "height": 1000})
        page.wait_for_timeout(100)
        fitted = _layout(page)
        assert fitted["termHeight"] >= 500
        assert fitted["termHeight"] == pytest.approx(
            fitted["availableTermHeight"], abs=2
        )
        assert fitted["pageScrolls"] is False

        page.set_viewport_size({"width": 1600, "height": 700})
        page.wait_for_timeout(100)
        short = _layout(page)
        assert 500 <= short["termHeight"] <= 502
        assert short["pageScrolls"] is True
        page.screenshot(path=str(_artifact(tmp_path, "interface-short.png")),
                        full_page=True)
    finally:
        context.close()


def test_sprints_multi_board_read_only_visual_gate(
    browser, ui_url, tmp_path
):
    api_requests = []

    def sprints_api(route):
        request = route.request
        path = request.url.split("/api", 1)[-1]
        api_requests.append((request.method, path))
        if path == "/sprints?status=active":
            return _json(route, SPRINTS)
        return _mock_api(route)

    context = browser.new_context(viewport={"width": 1600, "height": 1000})
    page = context.new_page()
    page.route("**/api/**", sprints_api)
    try:
        page.goto(f"{ui_url}/#sprints", wait_until="networkidle")
        boards = page.locator("#view-sprints .sprint-board")
        assert boards.count() == 2
        assert boards.nth(0).locator("h2").inner_text() == (
            "SPRINT: Worker expectation reconciler"
        )
        assert boards.nth(1).locator("h2").inner_text() == (
            "sprint: Active sprint flow board"
        )
        assert page.locator(
            'nav button[data-tab="sprints"]'
        ).inner_text() == "SPRINTS 2"
        assert boards.nth(0).locator(".sprint-wire").count() == 0
        assert boards.nth(1).locator(".sprint-wire").count() == 3
        unavailable = boards.nth(1).get_by_role(
            "img", name="dependency unavailable: U99", exact=True
        )
        assert unavailable.count() == 1
        assert unavailable.is_visible()

        view = page.locator("#view-sprints")
        assert view.locator(
            "button, input, select, textarea, form, a, "
            '[contenteditable="true"], [draggable="true"]'
        ).count() == 0
        cards = view.locator('.sprint-unit[role="button"]')
        assert cards.count() == view.locator(".sprint-unit").count() == 7

        geometry = view.evaluate(
            """(node) => ({
              viewWidth: node.getBoundingClientRect().width,
              boards: Array.from(node.querySelectorAll(".sprint-board"))
                .map((board) => ({
                  gap: parseFloat(getComputedStyle(
                    board.querySelector(".sprint-cols")).columnGap),
                  columns: Array.from(board.querySelectorAll(".sprint-col"))
                    .map((column) => column.getBoundingClientRect().width),
                  cards: Array.from(board.querySelectorAll(".sprint-unit"))
                    .map((card) => ({
                      card: card.getBoundingClientRect().width,
                      column: card.parentElement.getBoundingClientRect().width,
                    })),
                })),
            })"""
        )
        assert geometry["viewWidth"] == 1350
        for board in geometry["boards"]:
            assert board["gap"] == 35
            assert max(board["columns"]) - min(board["columns"]) < 1
            assert all(
                abs(card["card"] - card["column"]) < 1
                for card in board["cards"]
            )

        for index in range(cards.count()):
            card = cards.nth(index)
            card.click()
            assert card.get_attribute("aria-pressed") == "true"
            card.press("Enter")
            assert card.get_attribute("aria-pressed") == "false"
            card.press(" ")
            assert card.get_attribute("aria-pressed") == "true"
            card.press(" ")
            assert card.get_attribute("aria-pressed") == "false"
        assert {method for method, _path in api_requests} == {"GET"}

        selected = boards.nth(1).locator('.sprint-unit[data-seq="U5"]')
        selected.click()
        assert selected.get_attribute("aria-pressed") == "true"
        assert selected.locator(".sprint-unit-details").is_visible()
        page.screenshot(
            path=str(_artifact(
                tmp_path, "sprints-multi-board-selected.png"
            )),
            full_page=True,
        )

        page.set_viewport_size({"width": 390, "height": 844})
        geometry = page.evaluate(
            """() => ({
              viewport: window.innerWidth,
              documentWidth: document.documentElement.scrollWidth,
              boards: Array.from(document.querySelectorAll(
                "#view-sprints .sprint-board"
              )).map((board) => {
                const flow = board.querySelector(".sprint-flow");
                return {
                  right: board.getBoundingClientRect().right,
                  clientWidth: flow.clientWidth,
                  scrollWidth: flow.scrollWidth,
                  overflowX: getComputedStyle(flow).overflowX,
                };
              }),
            })"""
        )
        assert geometry["documentWidth"] <= geometry["viewport"]
        assert all(
            board["right"] <= geometry["viewport"] + 1
            for board in geometry["boards"]
        )
        assert all(
            board["scrollWidth"] > board["clientWidth"]
            and board["overflowX"] == "auto"
            for board in geometry["boards"]
        )
        assert selected.get_attribute("aria-pressed") == "true"
        page.screenshot(
            path=str(_artifact(tmp_path, "sprints-narrow-selected.png")),
            full_page=True,
        )
        assert not {
            method for method, _path in api_requests
            if method in {"POST", "PUT", "PATCH", "DELETE"}
        }
    finally:
        context.close()


def test_attached_resize_refits_and_reports_without_reconnect(
    browser, ui_url
):
    context, page = _open_interface(browser, ui_url, height=1000)
    try:
        before = page.evaluate(
            """() => ({
              sockets: window.__wsInstances,
              terminals: window.__terminalResizes.slice(),
              frames: window.__wsResizeFrames.slice()
            })"""
        )
        page.set_viewport_size({"width": 1600, "height": 1400})
        page.wait_for_function(
            "(count) => window.__terminalResizes.length > count",
            arg=len(before["terminals"]),
        )
        after = page.evaluate(
            """() => ({
              sockets: window.__wsInstances,
              terminals: window.__terminalResizes.slice(),
              frames: window.__wsResizeFrames.slice()
            })"""
        )
        assert before["sockets"] == after["sockets"] == 1
        assert after["terminals"][-1]["rows"] > before["terminals"][-1]["rows"]
        assert len(after["frames"]) > len(before["frames"])
        assert after["frames"][-1] == after["terminals"][-1]
    finally:
        context.close()


def test_alert_history_and_multiline_composer_respect_terminal_floor(
    browser, ui_url
):
    context, page = _open_interface(browser, ui_url, height=1100)
    try:
        initial = _layout(page)
        assert initial["pageScrolls"] is False

        composer = page.locator(".if-composer-input")
        composer.fill("\n".join(f"message line {index}" for index in range(12)))
        page.wait_for_timeout(100)
        composed = _layout(page)
        assert composed["composerHeight"] > initial["composerHeight"]
        assert composed["termHeight"] >= 500
        assert (
            composed["termHeight"] < initial["termHeight"]
            or composed["pageScrolls"] is True
        )

        page.get_by_text("Alerts (2)", exact=True).click()
        page.get_by_role("button", name="Alert history").click()
        page.locator(".if-history").wait_for()
        expanded = _layout(page)
        assert expanded["termHeight"] >= 500
        assert expanded["pageScrolls"] is True
    finally:
        context.close()


def test_compact_details_alerts_and_actions_render_on_desktop_and_mobile(
    browser, ui_url, tmp_path
):
    desktop, page = _open_interface(browser, ui_url, height=1100)
    try:
        page.get_by_text("Alerts (2)", exact=True).wait_for()
        # Every occupied row labels its route as the launch route — the rail
        # never states a bare model it does not observe (flag #130, dec #55).
        rail_models = page.locator(".if-row-sub").all_inner_texts()
        assert "DEV3 · codex · GPT 5.6 TERRA (launched)" in rail_models
        assert "DEV4 · codex · GPT 5.6 SOL (launched)" in rail_models
        assert page.locator(".if-alerts").get_attribute("class").endswith(
            "critical"
        )
        assert page.locator(".if-details").get_attribute("open") is None
        assert page.locator(".if-alerts").get_attribute("open") is None

        page.get_by_text("Details", exact=True).click()
        details = page.locator(".if-details")
        assert "launched GPT 5.6 TERRA" in details.inner_text()
        assert "model " not in details.inner_text()
        assert "session #7 · arc #172" in details.inner_text()
        assert "wake armed" in details.inner_text()
        assert "sprint #31 Interface corrective hardening · ACTIVE" in (
            details.inner_text()
        )
        assert "last outcome #90 delivery_unknown · parked:1" in (
            details.inner_text()
        )
        assert "PARKED: wake_batch_delivery_unknown" in details.inner_text()
        assert page.get_by_role(
            "button", name="Retry — input landed"
        ).is_visible()
        assert page.get_by_role(
            "button", name="Retry — input lost"
        ).is_visible()

        page.get_by_text("Alerts (2)", exact=True).click()
        alerts = page.locator(".if-alerts")
        assert alerts.get_by_text("critical", exact=True).count() == 1
        assert alerts.get_by_text("warning", exact=True).count() == 1
        assert alerts.get_by_text(
            "Capability information", exact=True
        ).count() == 1

        actions = page.locator(".if-composer-actions")
        # Spec #43 U7: Send is gone (Enter is the sole submit, with a static
        # hint), End is the red text-only rename, +Chat is the new action.
        assert actions.locator("button").all_inner_texts() == [
            "End",
            "+Chat",
        ]
        assert page.locator(".if-composer-hint").inner_text() == "enter to send"
        boxes = actions.locator("button").evaluate_all(
            """(buttons) => buttons.map((button) => {
              const box = button.getBoundingClientRect();
              return { x: box.x, y: box.y };
            })"""
        )
        assert abs(boxes[0]["y"] - boxes[1]["y"]) < 1
        assert boxes[1]["x"] > boxes[0]["x"]
        page.evaluate(
            "ifControl(ifAttach, { type: 'lifecycle', lifecycle: 'ended' })"
        )
        # SC-167: both actions terminate the session, so an ended pane hides
        # both. +Chat used to survive here, offering to end a chat that was
        # already over.
        assert page.get_by_role("button", name="End", exact=True).is_hidden()
        assert page.get_by_role("button", name="+Chat", exact=True).is_hidden()
        page.evaluate(
            "ifControl(ifAttach, { type: 'lifecycle', lifecycle: 'idle' })"
        )
        assert page.get_by_role("button", name="End", exact=True).is_visible()
        assert page.get_by_role("button", name="+Chat", exact=True).is_visible()
        page.screenshot(
            path=str(_artifact(tmp_path, "interface-details-desktop.png")),
            full_page=True,
        )
    finally:
        desktop.close()

    mobile, page = _open_interface(
        browser, ui_url, width=390, height=900
    )
    try:
        page.get_by_text("Alerts (2)", exact=True).wait_for()
        geometry = page.evaluate(
            """() => ({
              viewport: window.innerWidth,
              documentWidth: document.documentElement.scrollWidth,
              pickerDisplay: getComputedStyle(
                document.querySelector(".if-picker")
              ).display,
              actionsWidth: document.querySelector(
                ".if-composer-actions"
              ).getBoundingClientRect().width,
              paneWidth: document.querySelector(
                ".if-pane"
              ).getBoundingClientRect().width,
              paneRight: document.querySelector(
                ".if-pane"
              ).getBoundingClientRect().right,
              headerClientWidth: document.querySelector("header").clientWidth,
              headerScrollWidth: document.querySelector("header").scrollWidth,
              headerOverflowX: getComputedStyle(
                document.querySelector("header")
              ).overflowX,
            })"""
        )
        assert geometry["documentWidth"] <= geometry["viewport"]
        assert geometry["pickerDisplay"] != "none"
        assert geometry["actionsWidth"] <= geometry["paneWidth"]
        assert geometry["paneRight"] <= geometry["viewport"]
        assert geometry["headerScrollWidth"] > geometry["headerClientWidth"]
        assert geometry["headerOverflowX"] == "auto"
        options = page.locator(".if-picker option").all_inner_texts()
        assert (
            "Code-01 · DEV3 · codex · GPT 5.6 TERRA (launched) · occupied"
            in options
        )
        assert (
            "Code-02 · DEV4 · codex · GPT 5.6 SOL (launched) · occupied"
            in options
        )
        assert page.locator(".if-composer-actions button").all_inner_texts() == [
            "End",
            "+Chat",
        ]
        page.screenshot(
            path=str(_artifact(tmp_path, "interface-details-mobile.png")),
            full_page=True,
        )
    finally:
        mobile.close()


def test_new_chat_relaunches_the_same_launch_triple_from_the_session_payload(
    browser, ui_url
):
    """+Chat's acceptance, end to end in the real browser: the relaunch POST
    carries the ENDED session's harness, model route and effort.

    This exists because the node-driven chain tests set ``st.launchEffort``
    themselves, so they pin what the chain does with the triple but not that the
    triple is READ from the session payload at all — a mutation nulling that
    wiring left every one of them green. Here the effort's only source is
    SESSION["launch_effort"] served by the mock API, so the wiring is on the
    path being tested.
    """
    state = {"started": None, "terminated": None, "ended": False}

    def relaunch_api(route) -> None:
        request = route.request
        path = request.url.split("/api", 1)[-1]
        if path == "/interface/termination-requests":
            state["terminated"] = request.post_data_json
            state["ended"] = True
            return _json(route, {"terminated": True})
        if path == "/interface/sessions" and request.method == "POST":
            state["started"] = request.post_data_json
            return _json(route, {"session_id": 9}, status=201)
        if path == "/interface/shells" and state["ended"]:
            return _json(route, {"shells": [{**SHELL, "availability": "starting",
                                             "session_id": None}]})
        return _mock_api(route)

    context, page = _open_interface(
        browser, ui_url, height=1000, api_handler=relaunch_api
    )
    dialogs: list[str] = []

    def accept(dialog):
        dialogs.append(dialog.message)
        dialog.accept()

    page.on("dialog", accept)
    try:
        page.get_by_role("button", name="+Chat", exact=True).click()
        # The chain ends in a re-render onto the starting pane, so the occupied
        # composer going away is the signal that both POSTs have been made.
        page.wait_for_function("() => !document.querySelector('.if-composer')")
        assert state["terminated"] == {"session_id": 7, "force": False}
        # The launch triple, reused verbatim — effort included.
        assert state["started"] == {
            "shell_id": 3,
            "harness": "codex",
            "model": "gpt-5.6-terra",
            "effort": "high",
        }, state["started"]
        # One confirm, naming both effects and the route being reused.
        assert len(dialogs) == 1, dialogs
        assert "End session #7" in dialogs[0]
        assert "GPT 5.6 TERRA" in dialogs[0]
        assert "discarded" in dialogs[0]
    finally:
        context.close()


def test_enter_sends_one_frame_and_open_silent_stream_retains_draft(
    browser, ui_url
):
    context, page = _open_interface(browser, ui_url, height=1000)
    try:
        composer = page.locator(".if-composer-input")
        composer.fill("one composed turn")
        page.locator(".if-composer-hint").wait_for(state="visible")
        # There is no Send button to watch for readiness any more (U7), so wait
        # on the condition its disabled state actually encoded: the draft's
        # dirty transition has landed, so Enter yields SENT and not QUEUED.
        page.wait_for_function(
            "() => ifAttach && ifAttach.browserComposerState === 'dirty'"
            " && !ifAttach.browserComposerSyncing"
        )
        page.evaluate("ifAttach.composerAckTimeoutMs = 40")

        composer.press("Enter")
        page.wait_for_function("window.__inputFrames.length === 1")
        frame = page.evaluate("window.__inputFrames[0]")
        payload = bytes(frame[9:]).decode()
        assert payload == "one composed turn\r"
        assert composer.input_value() == "one composed turn"

        page.locator(".if-composer .if-note").filter(
            has_text="message acknowledgement timed out"
        ).wait_for()
        assert composer.input_value() == "one composed turn"
        assert "delivery is unknown" in page.locator(
            ".if-composer .if-note"
        ).inner_text()
        assert page.evaluate("window.__inputFrames.length") == 1
        assert page.evaluate("window.__lastWs.readyState") == 1
        assert page.evaluate("ifAttach.halted") is True
    finally:
        context.close()


def test_not_occupied_end_chat_detaches_into_preserving_recovery(
    browser, ui_url
):
    state: dict[str, object] = {"lost": False, "recovery_body": None}
    preview = {
        "observation_id": "obs-not-occupied",
        "expires_in_s": 120,
        "classification": "stale_durable_lock",
        "legal_actions": ["recover"],
        "evidence": {
            "shell": {"shell_id": 3, "shortname": "DEV3"},
            "session": {
                "session_id": 7,
                "generation": 1,
                "occupancy": "unreconciled",
                "lifecycle": "lost",
            },
        },
        "evidence_projection": [
            {
                "key": "classification",
                "label": "classification",
                "value": "stale_durable_lock",
            },
            {
                "key": "session",
                "label": "session",
                "value": "session #7 · generation 1 · unreconciled/lost",
            },
            {
                "key": "process",
                "label": "process",
                "value": "PID absent · pane gone",
            },
            {
                "key": "worktree",
                "label": "worktree",
                "value": "not clean · preserve by default",
            },
        ],
    }

    def recovery_api(route) -> None:
        request = route.request
        path = request.url.split("/api", 1)[-1]
        if path == "/interface/termination-requests":
            state["lost"] = True
            return _json(route, {
                "error": {
                    "code": "not_occupied",
                    "message": "session 7 is unreconciled — termination "
                    "needs a verified identity",
                },
            }, status=409)
        if path == "/interface/shells":
            shell = {
                **SHELL,
                "availability": "lost" if state["lost"] else "occupied",
            }
            return _json(route, {"shells": [shell]})
        if path == "/interface/shells/3/recovery":
            if request.method == "GET":
                return _json(route, preview)
            state["recovery_body"] = request.post_data_json
            return _json(route, {
                "shell_id": 3,
                "shortname": "DEV3",
                "classification": "stale_durable_lock",
                "mode": "recover",
                "availability": "available",
                "closed": {"alerts_resolved": 1, "parked": []},
                "worktree": {"preserved": True},
                "unread_messages": 0,
            })
        return _mock_api(route)

    context, page = _open_interface(
        browser, ui_url, height=1000, api_handler=recovery_api
    )
    page.on("dialog", lambda dialog: dialog.accept())
    try:
        page.get_by_role("button", name="End", exact=True).click()
        preview_button = page.get_by_role("button", name="Preview recovery")
        preview_button.wait_for()
        assert page.evaluate("window.__lastWs.readyState") == 3
        assert page.locator(".if-term").count() == 0

        preview_button.click()
        recover = page.get_by_role("button", name="Recover", exact=True)
        recover.wait_for()
        discard = page.get_by_text(
            "Discard worktree changes", exact=False
        ).locator("input")
        assert discard.is_checked() is False

        recover.click()
        page.get_by_text("Recovery result", exact=True).wait_for()
        assert state["recovery_body"] == {
            "observation_id": "obs-not-occupied",
            "mode": "recover",
            "preserve_worktree": True,
        }
        assert "Worktree preserved" in page.locator(
            ".if-recovery-result"
        ).inner_text()
    finally:
        context.close()


# --------------------------------------------------------------------------
# Terminal grid fit (spec 43 U6) — driven against the REAL vendored xterm.
#
# Every test above fakes the terminal, so none of them can see the defect this
# section exists for: the grid used to be derived from hardcoded cell metrics
# (9x17px), and the cell this font stack actually renders is 18px tall. The row
# count therefore overshot the box and the bottom rows painted below `.if-term`'s
# clipped edge — hiding precisely the lines a harness anchors to the bottom,
# like the final option of a question prompt.
#
# So these load the real xterm and the real FitAddon and assert the geometry of
# what the browser actually painted. The assertions are invariants rather than
# pinned pixel counts, so they hold under any font, zoom, or platform metrics —
# and go red whenever the grid is derived from an assumed cell size again.


def _open_interface_real_terminal(
    browser, ui_url: str, *, css_width: int, css_height: int,
    zoom: float = 1.0,
):
    """Attach the Interface pane with the genuine xterm + FitAddon.

    Only the socket is stubbed. Browser zoom is emulated the way Chromium
    implements it: the layout viewport shrinks by the zoom factor while the
    device pixel ratio grows by it.
    """
    context = browser.new_context(
        viewport={"width": css_width, "height": css_height},
        device_scale_factor=zoom,
    )
    page = context.new_page()
    page.add_init_script(WS_STUB)
    page.route("**/api/**", _mock_api)
    page.goto(f"{ui_url}/#interface/DEV3", wait_until="networkidle")
    page.locator(".if-term .xterm-rows").wait_for()
    page.wait_for_function("window.__wsResizeFrames.length > 0")
    return context, page


# Written through the app's own 0x00 output path, so the bytes reach the
# terminal exactly as the broker would deliver them rather than via a shape
# hand-authored into the DOM.
def _write_output(page, payload: str) -> None:
    page.evaluate(
        """(text) => {
          const bytes = new TextEncoder().encode(text);
          const frame = new Uint8Array(bytes.length + 1);
          frame[0] = 0x00;
          frame.set(bytes, 1);
          window.__lastWs.onmessage({ data: frame.buffer });
        }""",
        payload,
    )


FINAL_OPTION = "3) explain the diff — FINAL OPTION"


def _write_question_prompt(page) -> None:
    """Reproduce the reported repro: a bottom-anchored question prompt whose
    last option is the last thing on screen."""
    rows = page.evaluate(
        "document.querySelector('.if-term .xterm-rows').children.length"
    )
    prompt = (
        "\r\n" * (rows + 3)
        + "Apply this change?\r\n  1) yes\r\n  2) no\r\n  " + FINAL_OPTION
    )
    _write_output(page, prompt)
    page.wait_for_function(
        """(needle) => {
          const rows = document.querySelector('.if-term .xterm-rows');
          const last = rows.children[rows.children.length - 1];
          return last && last.textContent.includes(needle);
        }""",
        arg=FINAL_OPTION,
    )


def _grid(page) -> dict[str, float]:
    return page.evaluate(
        """() => {
          const card = document.querySelector(".if-term");
          const rowsElement = card.querySelector(".xterm-rows");
          const style = getComputedStyle(card);
          const box = card.getBoundingClientRect();
          const rows = Array.from(rowsElement.children);
          const first = rows[0].getBoundingClientRect();
          const last = rows[rows.length - 1].getBoundingClientRect();
          // Everything here is border-box, so getComputedStyle().height is the
          // BORDER box — the exact trap this unit fixes. Derive the content box
          // from the edges instead, or the test measures the wrong thing too.
          const contentTop = box.top
            + parseFloat(style.borderTopWidth) + parseFloat(style.paddingTop);
          const contentBottom = box.bottom
            - parseFloat(style.borderBottomWidth)
            - parseFloat(style.paddingBottom);
          const contentLeft = box.left
            + parseFloat(style.borderLeftWidth) + parseFloat(style.paddingLeft);
          const contentRight = box.right
            - parseFloat(style.borderRightWidth)
            - parseFloat(style.paddingRight);
          return {
            rowCount: rows.length,
            cellHeight: first.height,
            // `overflow: hidden` clips at the padding box; the grid is sized
            // against the content box, which is the stricter of the two.
            contentBottom,
            clipBottom: box.bottom - parseFloat(style.borderBottomWidth),
            contentRight,
            gridTop: first.top,
            gridRight: first.right,
            lastRowBottom: last.bottom,
            lastRowText: rows[rows.length - 1].textContent,
            availableHeight: contentBottom - contentTop,
            availableWidth: contentRight - contentLeft,
          };
        }"""
    )


def _assert_grid_fits(grid: dict[str, float], label: str) -> None:
    cell = grid["cellHeight"]
    assert cell > 0, f"{label}: no rendered cell to measure"

    # The acceptance itself: the operator can read the bottom line.
    assert grid["lastRowBottom"] <= grid["contentBottom"] + 0.5, (
        f"{label}: last row overflows the terminal card by "
        f"{grid['lastRowBottom'] - grid['contentBottom']:.1f}px "
        f"({grid['rowCount']} rows x {cell}px cell in "
        f"{grid['availableHeight']}px)"
    )

    # ...and it is not bought by shrinking the grid: any unused strip must be
    # smaller than one more row, or we are wasting space the operator paid for.
    slack = grid["availableHeight"] - grid["rowCount"] * cell
    assert 0 <= slack < cell, (
        f"{label}: grid leaves {slack:.1f}px unused with a {cell}px cell — "
        f"the fit is not tracking the real metrics"
    )


@pytest.mark.parametrize(
    ("label", "css_width", "css_height", "zoom"),
    [
        # 100% zoom, tall and short panes.
        ("tall-100", 1600, 1400, 1.0),
        ("default-100", 1600, 1000, 1.0),
        ("short-100", 1600, 760, 1.0),
        # Chromium zoom: the CSS viewport shrinks as the device ratio grows.
        ("zoom-80", 2000, 1250, 0.8),
        ("zoom-125", 1280, 800, 1.25),
        # The mobile layout's shorter pane (`.if-pane` goes full width).
        ("mobile", 620, 900, 1.0),
    ],
)
def test_terminal_grid_keeps_the_last_row_visible(
    browser, ui_url, tmp_path, label, css_width, css_height, zoom
):
    """The whole point of the unit: whatever the real cell turns out to be, the
    last row a harness paints is inside the card the operator can see."""
    context, page = _open_interface_real_terminal(
        browser, ui_url, css_width=css_width, css_height=css_height, zoom=zoom
    )
    try:
        _write_question_prompt(page)
        grid = _grid(page)
        assert FINAL_OPTION in grid["lastRowText"]
        _assert_grid_fits(grid, label)
        # The spec's bottom-edge acceptance is an eyeball, judged together with
        # U4's padding line — so ship the pane it is judged on.
        page.locator(".if-pane").screenshot(
            path=str(_artifact(tmp_path, f"interface-grid-{label}.png"))
        )
    finally:
        context.close()


def test_terminal_grid_refits_and_reports_the_measured_size_to_tmux(
    browser, ui_url
):
    """Resizing re-measures, and the row count the browser painted is exactly
    the one forwarded down term.onResize -> ifSendResize -> tmux."""
    context, page = _open_interface_real_terminal(
        browser, ui_url, css_width=1600, css_height=900
    )
    try:
        _write_question_prompt(page)
        before = _grid(page)
        _assert_grid_fits(before, "before-resize")
        assert page.evaluate("window.__wsResizeFrames.at(-1).rows") == (
            before["rowCount"]
        )

        page.set_viewport_size({"width": 1600, "height": 1300})
        page.wait_for_function(
            "(rows) => window.__wsResizeFrames.at(-1).rows !== rows",
            arg=before["rowCount"],
        )
        _write_question_prompt(page)
        after = _grid(page)

        assert after["rowCount"] > before["rowCount"]
        assert after["cellHeight"] == before["cellHeight"]
        _assert_grid_fits(after, "after-resize")
        # The grid the operator sees and the grid tmux is told about are one
        # number — the resize path this unit deliberately left unchanged.
        assert page.evaluate("window.__wsResizeFrames.at(-1).rows") == (
            after["rowCount"]
        )
    finally:
        context.close()


def test_terminal_bottom_edge_carries_no_dead_chrome(browser, ui_url):
    """Spec #43 U4's padding line, measured rather than eyeballed: the card's
    own 6px bottom padding plus the pane's 8px row-gap put 14px of dead space
    between the last terminal row and the composer. Only the card's 1px border
    may remain — this is the number the QAQC annotation asked to reclaim.
    """
    context, page = _open_interface(browser, ui_url, height=1000)
    try:
        measured = page.evaluate(
            """() => {
              const term = document.querySelector(".if-term");
              const xterm = document.querySelector(".if-term .xterm");
              const composer = document.querySelector(".if-composer");
              return {
                underLastRow: composer.getBoundingClientRect().top -
                              xterm.getBoundingClientRect().bottom,
                padBottom: getComputedStyle(term).paddingBottom,
                padTop: getComputedStyle(term).paddingTop,
              };
            }"""
        )
        assert measured["padBottom"] == "0px"
        # The top/side padding is deliberately kept — only the bottom went.
        assert measured["padTop"] == "6px"
        assert measured["underLastRow"] <= 2, (
            f"{measured['underLastRow']}px of chrome still sits between the "
            "last terminal row and the composer"
        )
    finally:
        context.close()


HEADER_LINES = """() => {
  const head = document.querySelector(".if-head");
  const data = document.querySelector(".if-head-data");
  const context = document.querySelector(".if-work-context");
  const identity = document.querySelector(".if-identity");
  const dataBox = data.getBoundingClientRect();
  const headBox = head.getBoundingClientRect();
  const controls = Array.from(head.children)
    .filter((child) => child !== data)
    .map((child) => child.getBoundingClientRect())
    .filter((rect) => rect.height > 0);
  const centre = (rect) => Math.round(rect.top + rect.height / 2);
  const badge = document.querySelector(".if-work-badge");
  const style = getComputedStyle(context);
  // What else is sharing the data group with the context, and the gap between
  // them — the two terms that turn the group's width into the context's cap.
  const siblings = Array.from(data.children)
    .filter((node) => node !== context && !node.hidden
                      && node.getBoundingClientRect().width > 0);
  const dataGap = parseFloat(getComputedStyle(data).columnGap) || 0;
  // getComputedStyle reports the DECLARED stack, not the family the host
  // actually resolved from it — and the resolved family is the whole question
  // here. Measure the fixture string under each candidate at the same computed
  // font, and whichever ruler matches `need` names the metrics in effect.
  const pen = document.createElement("canvas").getContext("2d");
  const ruler = {};
  for (const family of [style.fontFamily, "system-ui", "Liberation Sans",
                        "Arial", "DejaVu Sans"]) {
    pen.font = `${style.fontStyle} ${style.fontWeight} ${style.fontSize} ` +
               family;
    ruler[family] = Math.round(pen.measureText(context.textContent).width * 10)
                    / 10;
  }
  const width = (node) => Math.round(node.getBoundingClientRect().width);
  return {
    lines: new Set([dataBox, ...controls].map(centre)).size,
    truncated: context.scrollWidth > context.clientWidth,
    controlsRight: Math.max(...controls.map((rect) => rect.right)),
    dataLeft: dataBox.left,
    rightInset: headBox.right - dataBox.right,
    context: context.textContent,
    badge: badge.textContent,
    identity: identity.textContent,
    // The truncation budget, carried so a failure REPORTS it rather than
    // merely denying it: `need` is the string's rendered width and `cap` the
    // space available for it once the badge, the identity, and the gaps have
    // taken theirs out of the data group. `slot` is the width the context was
    // ACTUALLY given, which equals `cap` only while it overflows — a context
    // that fits is sized to its own content, so a green run reports
    // slot == need and says nothing about headroom. Read `cap` for headroom.
    metrics: {
      need: context.scrollWidth,
      slot: context.clientWidth,
      cap: Math.round(dataBox.width - siblings.reduce(
        (sum, node) => sum + node.getBoundingClientRect().width, 0,
      ) - dataGap * (siblings.length)),
      head: Math.round(headBox.width),
      data: Math.round(dataBox.width),
      badge: width(badge),
      identity: width(identity),
      controls: controls.reduce((sum, rect) => sum + rect.width, 0),
      fontSize: style.fontSize,
      fontFamily: style.fontFamily,
      ruler: ruler,
    },
  };
}"""


def _header_report(label: str, metrics: dict) -> str:
    """One line of header geometry, for the assertion messages and the run log."""
    rulers = "  ".join(
        f"[{family.split(',')[0].strip()}]={width}px"
        for family, width in metrics["ruler"].items()
    )
    return (
        f"{label}: context need={metrics['need']}px cap={metrics['cap']}px "
        f"({metrics['cap'] - metrics['need']:+}px spare) slot={metrics['slot']}px · "
        f"head={metrics['head']}px controls={round(metrics['controls'])}px "
        f"data={metrics['data']}px badge={metrics['badge']}px "
        f"identity={metrics['identity']}px · font={metrics['fontSize']} "
        f"{metrics['fontFamily']} · rulers {rulers}"
    )


def test_header_keeps_controls_left_and_work_data_right(browser, ui_url, tmp_path):
    """A long work subject truncates inside the right data zone; it never moves
    the controls or the shell identity onto a second line."""
    # The fixture title is deliberately SHORT, and its length is DERIVED from
    # the runner rather than guessed. `.if-pane` caps at 1300px, so the wide
    # assert has a fixed budget, while the same string renders ~14% wider or
    # narrower depending on which family the host resolves out of the generic
    # stack. Two lengths were reasoned from local measurements and both
    # mispredicted CI, because the canvas rulers below say the sandbox and the
    # runner do not resolve the same font:
    #
    #   ruler at 13.6px, 79-char context   sandbox    ubuntu-latest
    #   [ui-sans-serif] / [system-ui]      471.5px    549.8px
    #   [Liberation Sans] / [Arial]        480.6px    480.6px
    #   [DejaVu Sans]                      437.6px    549.8px
    #
    # The sandbox column has THREE distinct values: this host resolves the
    # generic stack to none of the three named rulers, so its generic row is
    # its own measurement and must never be copied from a named one. On the
    # runner the generic and DejaVu rows genuinely coincide at 549.8px, and
    # carrying that identity back here is how the sandbox generic row was
    # first written as 437.6px.
    #
    # The runner resolves the generic stack to DejaVu Sans — the WIDEST
    # candidate — not to the Arial/Liberation metrics the earlier fix assumed.
    # DejaVu is not installed in the sandbox, so the local DejaVu ruler falls
    # back to something narrower and reads 437.6px; Liberation and Arial are
    # genuinely metric-compatible and agree at 480.6px on both hosts, which
    # makes Liberation the one ruler a local run can trust.
    #
    # The arithmetic, from the 1600px CI run of 3bb3ced (slot=504px, need=550px
    # at 79 chars, font=13.6px). That 504 is the FAILING run's slot — do not
    # reconcile it with the 496 cap below: a slot equals the cap only while the
    # context overflows, so the two are different quantities, not a discrepancy.
    #
    #   px/char under the widest ruler   549.8 / 79     = 6.96px
    #   target                           need <= .75 * slot = 378px
    #   flat cap                         378 / 6.96     = 54 chars of context
    #
    # 54 chars is the ceiling, not the answer — per-character width varies with
    # composition, so the chosen string is measured, not counted. The 33-char
    # sprint prefix plus a 21-char title measures 322.6px under Liberation
    # locally, and the local Liberation ruler reproduces the runner's 480.6px
    # for the old fixture exactly, so scaling by the runner's own DejaVu ratio
    # (549.8 / 480.6) predicts its width there:
    #
    #   Liberation / Arial (both hosts)  322.6px   64.0% of slot   +181px
    #   DejaVu, i.e. the runner          369.1px   73.2% of slot   +135px
    #
    # That 25% margin absorbs a font substitution of 1.56x. The observed spread
    # between the widest and narrowest real candidate is 69.2px on a 550px
    # string — 12.6% — so this clears the whole known spread nearly twice over,
    # where the previous fixture cleared the sandbox by 0px and the runner by
    # -46px. The narrow (700px) assert is unaffected and cannot be tuned: the
    # slot is 0px at that viewport on the runner, so any non-empty string
    # truncates by construction.
    #
    # This does NOT mean the header is adequate. The runner's cap is 496px and
    # a realistic 46-char work context needs 550px there, so a REAL title
    # overflows on CI's font — that is flag #206, a product question, and
    # shortening a test fixture does not touch it. The green below is evidence
    # that this test measures LAYOUT instead of font metrics, which is all it
    # was ever meant to measure. Both evaluations report their own geometry, on
    # a pass as well as a failure; read those numbers before touching the
    # fixture again rather than reasoning from any table.
    titled = {**SESSION, "title": "Wire watcher to inbox"}
    assert len(titled["title"]) == 21

    def titled_api(route) -> None:
        if route.request.url.split("/api", 1)[-1] == "/interface/sessions/7":
            return _json(route, titled)
        return _mock_api(route)

    context, page = _open_interface(
        browser, ui_url, height=1000, width=700, api_handler=titled_api
    )
    report: list[str] = []
    try:
        narrow = page.evaluate(HEADER_LINES)
        report.append(_header_report("700px", narrow["metrics"]))
        assert narrow["lines"] == 1, (
            f"the header laid out on {narrow['lines']} lines: the identity "
            f"wrapped the controls instead of truncating\n{report[-1]}"
        )
        assert narrow["truncated"], (
            "the work context fits at 700px — this test is not measuring "
            "anything; lengthen the fixture title, but stay inside the wide "
            f"budget the 1600px line reports\n{report[-1]}"
        )
        assert narrow["controlsRight"] <= narrow["dataLeft"], (
            "right-side data crossed over the left-side controls"
        )
        assert narrow["rightInset"] <= 14
        assert narrow["badge"] == "Sprint 31"
        assert narrow["identity"] == "Code-01 | DEV3"
        assert narrow["context"].startswith("Interface corrective hardening · ")

        # With room to spare the same hierarchy remains one line, right aligned.
        page.set_viewport_size({"width": 1600, "height": 1000})
        wide = page.evaluate(HEADER_LINES)
        report.append(_header_report("1600px", wide["metrics"]))
        assert wide["lines"] == 1, report[-1]
        assert not wide["truncated"], (
            f"the {len(titled['title'])}-char work context truncates at 1600px "
            "— this host's budget, not the table above:\n"
            f"{report[-1]}\n"
            "The slot is what the data group has left after the badge, the "
            "identity, and two .75rem gaps; the ruler that matches `need` names "
            "the font in effect. Decide from these numbers — do not widen the "
            "slot in CSS and do not guess a fixture length — derive it from "
            "the widest ruler's px/char at 75% of the slot, as above."
        )
        assert wide["controlsRight"] <= wide["dataLeft"], report[-1]
        assert wide["rightInset"] <= 14, report[-1]
    finally:
        # Written on pass as well as failure: an instrumented run that goes
        # green still has to hand back the numbers it measured.
        _artifact(tmp_path, "header-metrics.txt").write_text(
            "\n".join(report) + "\n", encoding="utf-8"
        )
        for line in report:
            print(line)
        context.close()


def _vendor_fault_pane(browser, ui_url, *, routes):
    """The attach pane with the vendored terminal scripts broken as `routes`
    says — no Terminal stub, because the point is what the page does when the
    real ones do not arrive."""
    context = browser.new_context(viewport={"width": 1600, "height": 1000})
    page = context.new_page()
    page.route("**/api/**", _mock_api)
    for pattern, handler in routes:
        page.route(pattern, handler)
    page.goto(f"{ui_url}/#interface/DEV3", wait_until="networkidle")
    return context, page


def test_a_vendor_script_the_server_cannot_serve_is_named_with_its_status(
    browser, ui_url
):
    """Spec #48, in the real browser: the incident was a vendored asset the
    running server had no route for, and the pane answered with a refresh the
    operator dutifully performed several times.

    This is the one place the DOM enumeration is proven — the node-driven
    contract (tests/test_ui_vendor_probe.py) has to stub querySelectorAll, so
    only a real page can show that the selector matches the tags index.html
    actually declares.
    """
    context, page = _vendor_fault_pane(
        browser, ui_url,
        routes=[("**/vendor/xterm/addon-fit.js", lambda route: route.fulfill(
            status=404, content_type="text/plain", body="no such file"))],
    )
    try:
        # The pane holds several .if-note boxes (alerts, composer); the
        # terminal's is the one this unit writes.
        note = page.locator(".if-note").filter(has_text="terminal unavailable")
        note.wait_for(timeout=10_000)
        text = note.inner_text()
        assert "/vendor/xterm/addon-fit.js returned 404" in text, text
        assert "restart" in text, text
        assert "refresh" not in text.lower(), (
            f"the pane still prescribes a refresh for a 404: {text}"
        )
    finally:
        context.close()


def test_a_vendor_script_that_defines_nothing_ends_in_a_sentence_not_a_spinner(
    browser, ui_url
):
    """Served, 200, and empty: the retry has to run out and say so. An
    unbounded retry would leave the operator watching a pane that never
    resolves and never lies either — the same silence in a nicer costume."""
    context, page = _vendor_fault_pane(
        browser, ui_url,
        routes=[(f"**/vendor/xterm/{asset}", lambda route: route.fulfill(
            status=200, content_type="application/javascript", body=""))
            for asset in ("xterm.js", "addon-fit.js")],
    )
    try:
        note = page.locator(".if-note").filter(has_text="did not define")
        note.wait_for(timeout=15_000)
        text = note.inner_text()
        assert "Terminal and FitAddon" in text, text
        assert "refresh" not in text.lower(), text
    finally:
        context.close()
