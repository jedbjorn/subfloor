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


def test_open_chat_restore_matches_the_flat_shell_projection():
    assert (
        "shells.find((item) => item.shell_id === openConversation.shell.shell_id)"
        in APP
    )
    assert (
        "shells.find((item) => item.shell.shell_id "
        "=== openConversation.shell.shell_id)"
        not in APP
    )


def test_start_chat_has_default_and_configured_paths_without_terminal_controls():
    interface = APP[APP.index("const CHAT_HARNESSES"):
                    APP.index("// ── Tabs + boot")]
    assert 'const CHAT_HARNESSES = ["opencode", "claude", "codex"]' in interface
    assert 'const CHAT_CONFIGURE_ROUTE = "configure"' in interface
    assert 'textContent: "＋ Chat"' in interface
    assert 'textContent: "Configure"' in interface
    assert "const conversation = await chatCreateConversation(shell);" in interface
    assert "{ shell_id: shell.shell_id, ...fields }" in interface
    assert "chatRouteConversation === CHAT_CONFIGURE_ROUTE" in interface
    assert "await chatRenderNew(pane, shell, defaults, catalog)" in interface
    assert "Use shell default" in interface
    assert "Use harness default" in interface
    assert 'harness !== "opencode" || connectedDefault' in interface
    assert "No connected provider models available" in interface
    assert "providers connected in OpenCode" in interface
    assert "submit.disabled = !ready" in interface
    assert '"Start chat"' in interface
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


def test_transcript_hides_routine_tools_but_keeps_actionable_activity():
    interface = APP[APP.index("const CHAT_HARNESSES"):
                    APP.index("// ── Tabs + boot")]
    activity = interface[
        interface.index("function chatActivity"):
        interface.index("function chatAssistantRuns")
    ]
    assert '"tool.started"' not in activity
    assert '"tool.completed"' not in activity
    assert '"permission.requested"' in activity
    assert '"input.requested"' in activity
    assert '"run.failed"' in activity
    assert '"run.interrupted"' in activity
    assert '"run.unknown"' in activity


def test_transcript_follow_pauses_for_reading_and_offers_jump_to_latest():
    interface = APP[APP.index("const CHAT_HARNESSES"):
                    APP.index("// ── Tabs + boot")]
    assert (
        "transcript.scrollHeight - transcript.scrollTop "
        "- transcript.clientHeight <= 32"
    ) in interface
    assert "const previousTop = transcript.scrollTop" in interface
    assert (
        "transcript.scrollTop = shouldFollow() "
        "? transcript.scrollHeight : previousTop"
    ) in interface
    assert 'className: "chat-jump-latest"' in interface
    assert 'ariaLabel: "Jump to latest message"' in interface
    assert "transcript.onscroll = updateTranscriptFollow" in interface
    assert "jumpToLatest.hidden = followTranscriptTail" in interface
    assert "transcript.scrollTop = transcript.scrollHeight" in interface


def test_composer_is_retry_safe_and_has_turn_controls():
    interface = APP[APP.index("const CHAT_HARNESSES"):
                    APP.index("// ── Tabs + boot")]
    assert "chatPendingSend.key" in interface
    assert "retry keeps this exact send" in interface
    assert 'event.key === "Enter" && !event.shiftKey' in interface
    assert "function chatQueuedCount(messages)" in interface
    assert 'className: "chat-queue-state"' in interface
    assert 'queueState.textContent = `${queued} queued`' in interface
    assert 'conversation.state !== "running"' in interface
    assert 'event.event_type === "run.started") message.state = "running"' in interface
    assert 'textContent: "Interrupt"' in interface
    assert 'textContent: "Retry"' in interface
    assert 'textContent: "Close"' in interface
    assert 'textContent: "Analytics"' in interface
    assert 'textContent: "Stop"' in interface
    stop_control = interface[
        interface.index("const stop ="):
        interface.index("const pending =", interface.index("const stop ="))
    ]
    assert "disabled: true" in stop_control
    assert ".onclick" not in stop_control
    assert 'className: "chat-title-button"' in interface
    assert 'title: "Rename conversation"' in interface
    assert 'textContent: "Rename"' not in interface
    assert "actions.append(analytics, interrupt, close)" in interface
    assert "chatCloseForSwitch(selectedConversation)" in interface
    assert "Finish the current turn and queued messages before switching chats." in interface
    assert 'item.state !== "closed"' in interface
    shell_switch = interface[
        interface.index('button.onclick = () => {', interface.index("chat-shell-name")):
        interface.index("rail.append(button);", interface.index("chat-shell-name"))
    ]
    assert "chatCloseForSwitch" not in shell_switch


def test_conversation_identity_uses_shell_context_and_neutral_user_label():
    interface = APP[APP.index("const CHAT_HARNESSES"):
                    APP.index("// ── Tabs + boot")]
    assert 'kind === "user" ? "You"' in interface
    assert "chatShellLabel(conversation)" in interface
    assert "chatHeaderLabel(conversation)" in interface
    assert "chatStartedLabel(conversation)" in interface
    assert '"Untitled chat"' in interface
    assert 'className: "chat-history-context"' in interface
    assert 'className: "chat-shell-shortname"' in interface
    assert '(active ? " active-chat" : "")' in interface


def test_interface_owns_scroll_with_fixed_history_and_conversation_controls():
    assert "body.interface-view { height: 100dvh; overflow: hidden; }" in STYLE
    assert "height: calc(100dvh - 52px);" in STYLE
    assert ".chat-layout {" in STYLE
    assert "height: 100%; min-height: 0; background: var(--panel2);" in STYLE
    assert ".chat-rail { padding: .8rem .6rem; overflow-y: auto; }" in STYLE
    assert "min-height: 0; overflow: hidden; background: var(--panel);" in STYLE
    assert (
        ".chat-history-list { flex: 1 1 auto; min-height: 0; "
        "overflow-y: auto; padding: .5rem; }"
    ) in STYLE
    assert "height: 100%; overflow-y: auto;" in STYLE


def test_shell_rail_is_flat_but_retains_flavor_order():
    interface = APP[APP.index("async function renderInterface"):
                    APP.index("// ── Tabs + boot")]
    assert "const orderedShells = orderedFlavors.flatMap" in interface
    assert "for (const item of orderedShells)" in interface
    assert 'className: "chat-shell-group"' not in interface
    assert ".chat-shell-group" not in STYLE


def test_history_header_places_configure_under_identity_and_centers_chat_action():
    interface = APP[APP.index('side.append(el("div", { className: "chat-history-head" }'):
                    APP.index('const history = el("div"', APP.index(
                        'side.append(el("div", { className: "chat-history-head" }'))]
    assert 'className: "chat-history-shell"' in interface
    assert "configure)," in interface
    assert "newChat));" in interface
    assert 'className: "chat-history-actions"' not in interface
    assert ".chat-history-head > .act" in STYLE
    assert "display: block; background: transparent;" in STYLE


def test_layout_retains_shell_rail_chat_history_and_bubble_transcript():
    for selector in (
        ".chat-layout",
        ".chat-rail",
        ".chat-history",
        ".chat-transcript",
        ".chat-bubble.chat-user",
        ".chat-bubble.chat-assistant",
        ".chat-composer",
        ".chat-jump-latest",
        ".chat-jump-latest[hidden]",
        ".chat-compose-actions .chat-stop:disabled",
        ".chat-title-button",
        ".chat-shell.active-chat::before",
    ):
        assert selector in STYLE
    assert "grid-template-columns: 260px 260px minmax(0, 1fr)" in STYLE
    assert "grid-template-columns: 210px 210px minmax(0, 1fr)" in STYLE
    assert "grid-template-columns: 101px minmax(0, 1fr)" in STYLE
    assert ".chat-working-dots" in STYLE
    assert "@keyframes chat-working-dot" in STYLE
    assert ".chat-queue-state" in STYLE
