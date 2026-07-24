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


INIT_SCRIPT = r"""
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


def _open_interface(
    browser, ui_url: str, *, height: int, width: int = 1600,
    api_handler=_mock_api,
):
    context = browser.new_context(viewport={"width": width, "height": height})
    page = context.new_page()
    page.add_init_script(INIT_SCRIPT)
    page.route("**/vendor/xterm/xterm.js", lambda route: route.fulfill(
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

        page.set_viewport_size({"width": 1600, "height": 700})
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
        rail_models = page.locator(".if-row-sub").all_inner_texts()
        assert "DEV3 · codex · GPT 5.6 TERRA" in rail_models
        assert "DEV4 · codex · GPT 5.6 SOL" in rail_models
        assert page.locator(".if-alerts").get_attribute("class").endswith(
            "critical"
        )
        assert page.locator(".if-details").get_attribute("open") is None
        assert page.locator(".if-alerts").get_attribute("open") is None

        page.get_by_text("Details", exact=True).click()
        details = page.locator(".if-details")
        assert "model GPT 5.6 TERRA" in details.inner_text()
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
        assert actions.locator("button").all_inner_texts() == [
            "Send",
            "End chat",
        ]
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
        assert page.get_by_role("button", name="End chat").is_hidden()
        page.evaluate(
            "ifControl(ifAttach, { type: 'lifecycle', lifecycle: 'idle' })"
        )
        assert page.get_by_role("button", name="End chat").is_visible()
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
            "Code-01 · DEV3 · codex · GPT 5.6 TERRA · occupied"
            in options
        )
        assert (
            "Code-02 · DEV4 · codex · GPT 5.6 SOL · occupied"
            in options
        )
        assert page.locator(".if-composer-actions button").all_inner_texts() == [
            "Send",
            "End chat",
        ]
        page.screenshot(
            path=str(_artifact(tmp_path, "interface-details-mobile.png")),
            full_page=True,
        )
    finally:
        mobile.close()


def test_enter_sends_one_frame_and_open_silent_stream_retains_draft(
    browser, ui_url
):
    context, page = _open_interface(browser, ui_url, height=1000)
    try:
        composer = page.locator(".if-composer-input")
        composer.fill("one composed turn")
        page.get_by_role("button", name="Send").wait_for(state="visible")
        page.wait_for_function(
            "() => !document.querySelector('.if-composer-actions button').disabled"
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
        page.get_by_role("button", name="End chat").click()
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
