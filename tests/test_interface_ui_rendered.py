"""Rendered Interface layout and live-resize coverage for spec #33.

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
    "alerts": 1,
}
SESSION = {
    "session_id": 7,
    "generation": 1,
    "attachable": True,
    "identity_verified": True,
    "harness": "codex",
    "model_route": "gpt-5.6-sol",
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
        return _json(route, {"shells": [SHELL]})
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
        return _json(route, {"bindings": []})
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
        return _json(route, {"alerts": [ALERT]})
    return _json(route, {})


INIT_SCRIPT = r"""
window.__wsInstances = 0;
window.__wsResizeFrames = [];
window.__terminalResizes = [];

class RenderedWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  constructor() {
    window.__wsInstances += 1;
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
  }
  close() { this.readyState = RenderedWebSocket.CLOSED; }
}
window.WebSocket = RenderedWebSocket;

class RenderedTerminal {
  constructor() {
    this.rows = 24;
    this.cols = 80;
    this._resize = null;
  }
  open(container) {
    const terminal = document.createElement("div");
    terminal.className = "xterm";
    terminal.textContent = "Rendered xterm viewport";
    container.append(terminal);
  }
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
"""


def _open_interface(browser, ui_url: str, *, height: int):
    context = browser.new_context(viewport={"width": 1600, "height": height})
    page = context.new_page()
    page.add_init_script(INIT_SCRIPT)
    page.route("**/vendor/xterm/xterm.js", lambda route: route.fulfill(
        status=200, content_type="application/javascript", body=""
    ))
    page.route("**/api/**", _mock_api)
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
          const gap = parseFloat(getComputedStyle(pane).rowGap);
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
              pane.getBoundingClientRect().height -
              nonTermHeight -
              gap * Math.max(children.length - 1, 0),
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


def test_local_artifact_mode_keeps_save_and_disables_publish(browser, ui_url):
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
        assert "local artifacts" in page.locator("#status").text_content()
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

        page.set_viewport_size({"width": 1600, "height": 900})
        page.wait_for_timeout(100)
        short = _layout(page)
        assert 500 <= short["termHeight"] <= 502
        assert short["pageScrolls"] is True
        page.screenshot(path=str(_artifact(tmp_path, "interface-short.png")),
                        full_page=True)
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

        page.get_by_role("button", name="Alert history").click()
        page.locator(".if-history").wait_for()
        expanded = _layout(page)
        assert expanded["termHeight"] >= 500
        assert expanded["pageScrolls"] is True
    finally:
        context.close()
