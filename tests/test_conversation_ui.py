"""Minimal browser-native conversation UI contracts."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / ".super-coder" / "ui" / "app.js").read_text()
INDEX = (ROOT / ".super-coder" / "ui" / "index.html").read_text()
STYLE = (ROOT / ".super-coder" / "ui" / "style.css").read_text()


def test_interface_is_a_first_class_reload_safe_view():
    assert 'data-tab="interface"' in INDEX
    assert 'id="view-interface"' in INDEX
    assert 'interface: ["#view-interface", renderInterface]' in APP
    assert 'raw === "interface" || raw.startsWith("interface/")' in APP
    assert "chatRouteShell = decodeURIComponent(shell)" in APP
    assert "chatRouteConversation = decodeURIComponent(conversation)" in APP


def test_start_chat_exposes_shell_harness_and_model_without_terminal_controls():
    interface = APP[APP.index("const CHAT_HARNESSES"):
                    APP.index("// ── Tabs + boot")]
    assert 'const CHAT_HARNESSES = ["opencode", "claude", "codex"]' in interface
    assert "Use shell default" in interface
    assert "Use harness default" in interface
    assert '"Start chat"' in interface
    assert "shell_id: shell.shell_id" in interface
    assert "harness: harnessSelect.value" in interface
    assert "if (modelSelect.value) body.model = modelSelect.value" in interface
    assert "xterm" not in interface.lower()
    assert "tmux" not in interface.lower()
    assert "attach" not in interface.lower()


def test_transcript_streams_normalized_events_and_reconnects_natively():
    interface = APP[APP.index("const CHAT_HARNESSES"):
                    APP.index("// ── Tabs + boot")]
    assert "new EventSource" in interface
    assert '"assistant.delta"' in interface
    assert '"tool.started"' in interface
    assert '"permission.requested"' in interface
    assert '"input.requested"' in interface
    assert "source.onerror" in interface
    assert "reconnecting" in interface
    assert "mdBlock(body)" in interface


def test_composer_is_retry_safe_and_has_turn_controls():
    interface = APP[APP.index("const CHAT_HARNESSES"):
                    APP.index("// ── Tabs + boot")]
    assert "chatPendingSend.key" in interface
    assert "retry keeps this exact send" in interface
    assert 'event.key === "Enter" && !event.shiftKey' in interface
    assert 'textContent: "Interrupt"' in interface
    assert 'textContent: "Retry"' in interface
    assert 'textContent: "Rename"' in interface
    assert 'textContent: "Close"' in interface
    assert 'textContent: "Analytics"' in interface


def test_layout_retains_shell_rail_chat_history_and_bubble_transcript():
    for selector in (
        ".chat-layout",
        ".chat-rail",
        ".chat-history",
        ".chat-transcript",
        ".chat-bubble.chat-user",
        ".chat-bubble.chat-assistant",
        ".chat-composer",
    ):
        assert selector in STYLE
    assert "grid-template-columns: 180px 260px minmax(0, 1fr)" in STYLE
