"""Static browser contract checks for spec #30 requirements 18-20.

The UI is deliberately build-free vanilla JS/CSS. These checks pin the shared
picker and responsive terminal behavior without inventing a second JS runtime
or duplicating application logic in a test fixture.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / ".super-coder" / "ui" / "app.js").read_text()
CSS = (ROOT / ".super-coder" / "ui" / "style.css").read_text()
PICKER = APP[APP.index("function dmModelPicker"):
             APP.index("async function renderDefaultModels")]


def test_shared_picker_is_list_only_and_exact_route_only():
    assert 'label: "Harness default"' in PICKER
    assert 'm.availability === "available"' in PICKER
    assert "allMode" not in PICKER
    assert "use as typed" not in PICKER
    assert "dm-fam" not in PICKER
    assert "dm-raw" not in PICKER
    assert "ArrowDown" in PICKER and "ArrowUp" in PICKER
    assert "choices[highlighted].value" in PICKER
    assert APP.count("dmModelPicker(") == 3  # definition + both consumers


def test_interface_terminal_has_exact_caps_and_live_resize_reporting():
    assert "#view-interface { max-width: calc(230px + 1rem + 1300px); }" in CSS
    assert "max-width: 1300px" in CSS
    assert "max-height: 850px" in CSS
    assert "ResizeObserver(fit)" in APP
    assert "ws.onopen" in APP
    assert "ifSendResize(a, term.rows, term.cols)" in APP
    assert "new WebSocket(" in APP


def test_browser_only_downgrades_exact_writer_conflict():
    assert 'e.code !== "writer_held"' in APP
    assert "ifStartingPane" in APP
    assert 'textContent: "Cancel start"' in APP
    assert "sess.attachable" in APP


def test_alerts_render_provenance_action_and_durable_acknowledgement():
    assert "a.meaning" in APP
    assert "a.next_action" in APP
    assert "a.generation" in APP
    assert "/acknowledge" in APP
    assert "Alert history" in APP
