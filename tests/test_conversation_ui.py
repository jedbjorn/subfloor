"""Minimal browser-native conversation UI contracts."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / ".super-coder" / "ui" / "app.js").read_text()
INDEX = (ROOT / ".super-coder" / "ui" / "index.html").read_text()
STYLE = (ROOT / ".super-coder" / "ui" / "style.css").read_text()
POLL_BLOCK = APP[
    APP.index("  let historyPollInFlight = false;"):
    APP.index(
        "  chatHistoryPollTimer = setInterval(pollHistory, CHAT_HISTORY_POLL_MS);"
    ) + len("  chatHistoryPollTimer = setInterval(pollHistory, CHAT_HISTORY_POLL_MS);")
]


def run_js(script: str) -> dict:
    proc = subprocess.run(
        ["node", "-e", script], text=True, capture_output=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_interface_is_a_first_class_reload_safe_view():
    assert '<button data-tab="interface">Chats</button>' in INDEX
    assert 'id="view-interface"' in INDEX
    assert 'interface: ["#view-interface", renderInterface]' in APP
    assert 'raw === "interface" || raw.startsWith("interface/")' in APP
    assert "chatRouteShell = decodeURIComponent(shell)" in APP
    assert "chatRouteConversation = decodeURIComponent(conversation)" in APP


def test_admin_shells_stay_on_the_rail_but_chat_is_cli_only():
    interface = APP[APP.index("async function renderInterface"):
                    APP.index("// ── Tabs + boot")]
    # The rail never filters admin out — the notice replaces the pane instead,
    # and it hands over the exact terminal commands (repo_root from /api/shells).
    assert "chatAdminCliOnly(shell)" in interface
    assert "disabled: adminCliOnly" in interface
    assert "chatAdminCliOnlyNotice(shell, repoRoot)" in interface
    assert "repo_root: repoRoot" in interface
    assert '(shell?.flavor || "") === "admin"' in APP
    assert "cd ${repoRoot" in APP
    assert "make dos-e s=${shell.shortname}" in APP
    assert ".chat-admin-cli-only" in STYLE
    assert ".chat-admin-commands" in STYLE


def test_open_chat_restore_matches_the_flat_shell_projection():
    assert "item.shell_id === openConversation.shell.shell_id" in APP
    assert (
        "shells.find((item) => item.shell.shell_id "
        "=== openConversation.shell.shell_id)"
        not in APP
    )


def test_sprint_pill_enters_the_current_conversation_without_a_wake():
    interface = APP[APP.index("async function renderInterface"):
                    APP.index("// ── Tabs + boot")]
    pill = interface[interface.index('className: "chat-sprint-pill"'):
                     interface.index("shellRow.append(pill)")]
    assert "sprint.current_conversation_id" in pill
    assert "location.hash = chatHash(" in pill
    assert "chatApi(" not in pill
    assert "Sprint ${sprint.sprint_id}" in pill
    assert "${sprint.role} · ${sprint.disposition}" in pill
    assert ".chat-sprint-pill" in STYLE
    assert "color: var(--warn)" in STYLE[
        STYLE.index(".chat-sprint-pill"):STYLE.index(".chat-sprint-meta")
    ]


def test_sprint_conversations_are_not_closed_by_normal_chat_controls():
    interface = APP[APP.index("const CHAT_HARNESSES"):
                    APP.index("// ── Tabs + boot")]
    close_for_switch = interface[
        interface.index("async function chatCloseForSwitch"):
        interface.index("function chatBubble")
    ]
    assert 'if (conversation.scope === "sprint") return true' in close_for_switch
    assert 'const sprintManaged = conversation.scope === "sprint"' in interface
    assert "close.hidden = sprintManaged" in interface
    assert 'hidden: conversation.scope === "sprint"' in interface


def test_start_chat_has_default_and_configured_paths_without_terminal_controls():
    interface = APP[APP.index("const CHAT_HARNESSES"):
                    APP.index("// ── Tabs + boot")]
    assert (
        'const CHAT_HARNESSES = ["opencode", "claude", "codex", "kimi"]'
        in interface
    )
    assert "const availableHarnesses = CHAT_HARNESSES;" in interface
    assert 'shell.flavor === "conductor"' not in interface
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


def test_transcript_installs_snapshot_then_coalesces_keyed_live_updates():
    interface = APP[APP.index("const CHAT_HARNESSES"):
                    APP.index("// ── Tabs + boot")]
    keyed = interface[interface.index("function chatCreateTranscriptState"):
                      interface.index("async function renderInterface")]

    assert "snapshot.projection_version !== 2" in keyed
    assert "items.has(item.item_id)" in keyed
    assert "nodes: new Map()" in keyed
    assert "dirty: new Set(items.keys())" in keyed
    assert "transcript.replaceChildren(...nodes)" in keyed
    assert "chatUpdateTranscriptNode(node, item, retry)" in keyed
    assert 'node.querySelector(".chat-assistant-body")' in keyed
    assert "body.replaceChildren(...rendered.childNodes)" in keyed
    assert "if (transcriptState.frame !== null) return" in keyed
    assert "transcriptState.frame = requestAnimationFrame" in keyed
    assert 'if (currentMode !== "chat")' in keyed
    assert "transcriptState.hiddenDirty = true" in keyed
    assert "sequence !== transcriptState.lastSequence + 1" in keyed
    assert "if (reconcilePromise" in keyed
    assert "`run:${runId}:assistant:${anchor}`" in keyed
    assert "assistant.text += event.payload?.text || \"\"" in keyed
    assert "transcriptState.throughSequence" in keyed
    assert "/events?after=${afterSequence}" in interface
    assert "/transcript`" in keyed
    assert "/messages?limit=100" not in interface[
      interface.index("const loadTranscript = async"):
      interface.index("await loadTranscript()")
    ]


def test_segmented_transcript_source_contract_is_versioned_and_run_scoped():
    interface = APP[APP.index("const CHAT_HARNESSES"):
                    APP.index("// ── Tabs + boot")]
    keyed = interface[interface.index("function chatCreateTranscriptState"):
                      interface.index("async function renderInterface")]

    assert "snapshot.projection_version !== 2" in keyed
    assert "assistant_cursor" in keyed
    assert "segment_anchor_sequence" in keyed
    assert "tool.started" in keyed
    assert "tool.completed" in keyed
    assert "permission.requested" in keyed
    assert "input.requested" in keyed
    assert ":assistant:${" in keyed


def test_connection_status_is_attached_to_transcript_without_visible_copy():
    interface = APP[APP.index("const CHAT_HARNESSES"):
                    APP.index("// ── Tabs + boot")]
    assert 'let streamStatus = "connecting"' in interface
    assert 'source.onopen = () => onState("connected")' in interface
    assert 'source.onerror = () => onState("reconnecting")' in interface
    assert "transcriptHost.title = `Connection: ${connectionLabel}`" in interface
    assert "`Conversation transcript; connection ${connectionLabel.toLowerCase()}`" in interface
    assert '"stream-disconnected"' in interface
    assert 'const state = el("div", { className: "chat-pane-state" })' not in interface
    assert "state.replaceChildren(chatStatePill" not in interface
    assert 'className: "chat-stream-state"' not in interface
    assert ".chat-stream-state" not in STYLE
    assert ".chat-state.state-idle.stream-disconnected" not in STYLE


def test_transcript_hides_routine_tools_but_keeps_actionable_activity():
    interface = APP[APP.index("const CHAT_HARNESSES"):
                    APP.index("// ── Tabs + boot")]
    reducer = interface[interface.index("const reduceEvent = (event) =>"):
                        interface.index("chatOpenStream(", interface.index(
                            "const reduceEvent = (event) =>"))]
    activity = reducer[
        reducer.index('} else if ([\n      "permission.requested"'):
        reducer.index("transcriptState.lastSequence = sequence")
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
        "transcript.scrollTop = followTail "
        "? transcript.scrollHeight : previousTop"
    ) in interface
    assert "const followTail = shouldFollow()" in interface
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
    assert 'type === "run.started") message.state = "running"' in interface
    assert 'textContent: "Interrupt"' not in interface
    assert 'textContent: "Retry"' in interface
    assert 'textContent: "Close"' in interface
    assert 'textContent: "Analytics"' in interface
    assert 'textContent: "Stop"' in interface
    stop_control = interface[
        interface.index("const stop ="):
        interface.index("const pending =", interface.index("const stop ="))
    ]
    assert 'title: "Interrupt the active turn"' in stop_control
    assert "disabled: true" not in stop_control
    stop_handler = interface[
        interface.index("stop.onclick ="):
        interface.index("composer.onkeydown", interface.index("stop.onclick ="))
    ]
    assert 'conversation.state !== "running"' in stop_handler
    assert "if (!stopRequest) stopRequest = { key: requestKey() }" in stop_handler
    assert "/interruptions`" in stop_handler
    assert '"POST", {}, stopRequest.key' in stop_handler
    assert "stopRequest = null" in stop_handler
    assert (
        'stop.disabled = conversation.state !== "running" || closing '
        "|| Boolean(stopRequest)"
        in interface
    )
    assert 'stop.textContent = stopRequest ? "Stopping…" : "Stop"' in interface
    assert (
        '["run.completed", "run.failed", "run.interrupted", "run.unknown"]'
        in interface
    )
    assert 'className: "chat-title-button"' in interface
    assert 'title: "Rename conversation"' in interface
    assert 'textContent: "Rename"' not in interface
    assert "actions.append(analytics, close)" in interface
    assert "/interruptions" in interface
    close_handler = interface[
        interface.index("close.onclick ="):
        interface.index("actions.append", interface.index("close.onclick ="))
    ]
    assert "confirm(" not in close_handler
    assert "const latest = await chatApi(" in close_handler
    assert "{ version: latest.version, state: \"closed\" }" in close_handler
    assert 'close.textContent = "Closing…"' in close_handler
    assert "const closing = !closed && Boolean(conversation.close_requested_at)" in interface
    assert "close.disabled = sprintManaged || closed || closing" in interface
    assert 'close.textContent = closing ? "Closing…" : "Close"' in interface
    assert '"conversation.close.requested"' in interface
    assert "chatCloseForSwitch(selectedConversation)" in interface
    assert "Finish the current turn and queued messages before switching chats." in interface
    assert 'item.state !== "closed"' in interface
    shell_switch = interface[
        interface.index('button.onclick = () => {', interface.index("chat-shell-name")):
        interface.index("rail.append(shellRow);", interface.index("chat-shell-name"))
    ]
    assert "chatCloseForSwitch" not in shell_switch


def test_closed_chat_composer_offers_reopen():
    interface = APP[APP.index("const CHAT_HARNESSES"):
                    APP.index("// ── Tabs + boot")]
    assert "const reopenable = closed && !sprintManaged" in interface
    assert "composer.disabled = closing || (closed && !reopenable)" in interface
    assert "send.disabled = closing || (closed && !reopenable)" in interface
    assert (
        "This conversation is closed — send a message to reopen it."
        in interface
    )
    assert '"conversation.reopened"' in interface
    reopen_reduce = interface[
        interface.index('if (type === "conversation.reopened")'):
        interface.index('if (["run.completed", "run.failed"',
                        interface.index(
                            'if (type === "conversation.reopened")'))
    ]
    assert 'conversation.state = "idle"' in reopen_reduce
    assert "conversation.closed_at = null" in reopen_reduce
    assert "conversation.close_requested_at = null" in reopen_reduce


def test_chat_switch_refetches_authoritative_version_before_close():
    interface = APP[APP.index("const CHAT_HARNESSES"):
                    APP.index("// ── Tabs + boot")]
    close_for_switch = interface[
        interface.index("async function chatCloseForSwitch"):
        interface.index("function chatBubble")
    ]
    assert "const latest = await chatApi(" in close_for_switch
    assert 'if (latest.state === "closed") return true' in close_for_switch
    assert '["idle", "waiting", "error"].includes(latest.state)' in close_for_switch
    assert "{ version: latest.version, state: \"closed\" }" in close_for_switch
    assert "Object.assign(conversation, closed)" in close_for_switch
    assert "{ version: conversation.version" not in close_for_switch


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
    assert "chatPaintShellState(button, openByShell.get(item.shell_id))" in interface


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


def test_shell_rail_hides_labels_but_retains_ordered_flavor_dividers():
    interface = APP[APP.index("async function renderInterface"):
                    APP.index("// ── Tabs + boot")]
    assert (
        '"cartographer", "admin", "planner", "dev", "reviewer", "devops"'
    ) in APP
    assert "const shells = allShells;" in interface
    assert "const orderedShells = orderedFlavors.flatMap" in interface
    assert "for (const item of orderedShells)" in interface
    assert 'const flavor = item.flavor || "bespoke"' in interface
    assert "previousFlavor && flavor !== previousFlavor" in interface
    assert 'className: "chat-shell-divider", role: "separator"' in interface
    assert 'className: "chat-shell-group"' not in interface
    assert ".chat-shell-group" not in STYLE
    assert ".chat-shell-divider" in STYLE
    assert ".sprint-board" in STYLE
    assert ".an-sprint" not in STYLE


def test_history_metadata_and_all_shell_accents_poll_without_repainting_interface():
    interface = APP[APP.index("const CHAT_HARNESSES"):
                    APP.index("// ── Tabs + boot")]
    assert "const CHAT_HISTORY_POLL_MS = 2000" in interface
    assert "if (chatHistoryPollTimer) clearInterval(chatHistoryPollTimer)" in interface
    assert "const historyItems = new Map()" in interface
    assert "const shellItems = new Map()" in interface
    assert "historyItems.set(conversation.conversation_id, item)" in interface
    assert "shellItems.set(item.shell_id, button)" in interface
    assert "`/conversations?open=true&limit=100${suffix}`" in interface
    assert "generation !== chatRenderGeneration || !chatHistoryPollTimer" in interface
    assert "historyItems.get(conversation.conversation_id)" in interface
    assert "chatPaintHistoryItem(item, conversation)" in interface
    assert "item.name.textContent = chatConversationName(conversation)" in interface
    assert "chatPaintStar(item.star, Boolean(conversation.starred))" in interface
    assert "selectedConversation = acceptSummary(conversation)" in interface
    assert "const nextOpenByShell = new Map()" in interface
    assert "nextOpenByShell.set(" in interface
    assert "for (const [shellId, button] of shellItems)" in interface
    assert "chatPaintShellState(button, nextOpenByShell.get(shellId))" in interface
    assert "if (document.hidden || historyPollInFlight) return" in interface
    assert "finally { historyPollInFlight = false; }" in interface
    assert "setInterval(pollHistory, CHAT_HISTORY_POLL_MS)" in interface
    poll = interface[interface.index("const pollHistory = async"):
                     interface.index("chatHistoryPollTimer = setInterval")]
    assert "renderInterface(" not in poll


def test_interface_arrival_defers_configuration_and_phases_history_requests():
    interface = APP[APP.index("async function renderInterface"):
                    APP.index("// ── Tabs + boot")]
    loader = APP[APP.index("function chatLoadConfiguration"):
                 APP.index("function chatStopStream")]

    assert 'api("/models")' not in interface
    assert 'api("/flavor-defaults")' not in interface
    assert 'api("/flavor-defaults")' in loader
    assert 'api("/models")' in loader
    assert "if (chatConfiguration) return Promise.resolve(chatConfiguration)" in loader
    assert "if (chatConfigurationPromise) return chatConfigurationPromise" in loader
    assert "chatConfigurationPromise = null" in loader
    assert 'chatApi("/conversations?open=true&limit=100")' in interface
    assert "starred=false&limit=20" in interface
    assert "starred=true&limit=100" in interface
    assert "const recentPage = await recentRequest" in interface
    assert "loadStars();" in interface
    assert "await loadStars()" not in interface
    assert 'textContent: "Retry"' in interface
    assert "chatRenderNew(pane, shell, defaults, catalog)" in interface


def test_history_more_and_deep_links_are_keyed_and_failure_isolated():
    interface = APP[APP.index("async function renderInterface"):
                    APP.index("// ── Tabs + boot")]

    assert "const detailRequest = deepLinked" in interface
    assert "chatApi(`/conversations/${chatRouteConversation}`)" in interface
    assert "selectedConversation.shell.shortname !== chatRouteShell" in interface
    assert "history.replaceState(" in interface
    assert "const summaries = new Map()" in interface
    assert "const historyItems = new Map()" in interface
    assert "const starredIds = new Set()" in interface
    assert "if (moreInFlight || !moreCursor) return" in interface
    assert "const requestedCursor = moreCursor" in interface
    assert "moreCursor = requestedCursor" in interface
    assert 'more.textContent = "Retry"' in interface
    assert "if (!recentIds.includes(conversation.conversation_id))" in interface
    assert "selected ? [selected] : []" in interface
    assert "if (starsInFlight) return" in interface
    assert "Starred chats unavailable" in interface


def test_history_poll_only_reconciles_open_pages_without_advancing_history():
    interface = APP[APP.index("async function renderInterface"):
                    APP.index("// ── Tabs + boot")]
    poll = interface[interface.index("const pollHistory = async"):
                     interface.index(
                       "chatHistoryPollTimer = setInterval",
                       interface.index("const pollHistory = async"),
                     )]

    assert "`/conversations?open=true&limit=100${suffix}`" in poll
    assert "cursor = page.next_cursor" in poll
    assert "starred=true" not in poll
    assert "starred=false" not in poll
    assert "moreCursor" not in poll
    assert "renderInterface(" not in poll
    assert "historyItems.get(conversation.conversation_id)" in poll
    assert "chatPaintShellState(button, nextOpenByShell.get(shellId))" in poll


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_history_poll_reconciles_selected_conversation_after_it_closes():
    script = r"""
globalThis.document = {hidden: false};
let scheduled = null;
globalThis.setInterval = (callback) => { scheduled = callback; return 1; };
const calls = [];
const painted = [];
let openPoll = 0;
async function chatApi(path) {
  calls.push(path);
  if (path === "/conversations?open=true&limit=100") {
    openPoll += 1;
    return openPoll === 1
      ? {items: [{conversation_id: "cv1", state: "running", version: 2,
          shell: {shell_id: 7}}], next_cursor: null}
      : {items: [], next_cursor: null};
  }
  if (path === "/conversations/cv1")
    return {conversation_id: "cv1", state: "closed", version: 3,
      shell: {shell_id: 7}};
  throw new Error(`unexpected request: ${path}`);
}
const historyItems = new Map([["cv1", {id: "selected"}]]);
const shellItems = new Map([[7, {}]]);
const acceptSummary = (conversation) => conversation;
const chatPaintHistoryItem = (item, conversation) => {
  painted.push([item.id, conversation.state, conversation.version]);
};
const chatPaintShellState = () => {};
let selectedConversation = {
  conversation_id: "cv1", state: "idle", version: 1, shell: {shell_id: 7},
};
const generation = 4;
let chatRenderGeneration = 4;
let chatHistoryPollTimer = 1;
const CHAT_HISTORY_POLL_MS = 2000;
""" + POLL_BLOCK + r"""
(async () => {
  await scheduled();
  await scheduled();
  await scheduled();
  console.log(JSON.stringify({calls, painted, selected: selectedConversation}));
})().catch((error) => {
  console.error(error.stack || error);
  process.exit(1);
});
"""
    result = run_js(script)
    assert result["calls"] == [
        "/conversations?open=true&limit=100",
        "/conversations?open=true&limit=100",
        "/conversations/cv1",
        "/conversations?open=true&limit=100",
    ]
    assert result["painted"] == [
        ["selected", "running", 2],
        ["selected", "closed", 3],
    ]
    assert result["selected"]["state"] == "closed"
    assert result["selected"]["version"] == 3


def test_history_card_has_independent_durable_star_button():
    interface = APP[APP.index("const CHAT_HARNESSES"):
                    APP.index("// ── Tabs + boot")]
    history = interface[interface.index('const history = el("div"'):
                        interface.index("const renderHistory =")]

    assert 'const card = el("div", {' in history
    assert 'className: "chat-history-open"' in history
    assert 'className: "chat-history-star"' in history
    assert "open.append(context, name, state)" in history
    assert "card.append(open, star)" in history
    assert "event.stopPropagation()" in history
    assert '{ version: current.version, starred: !current.starred }' in history
    assert "acceptSummary(updated)" in history
    assert "renderHistory()" in history
    assert 'button.textContent = starred ? "★" : "☆"' in interface
    assert 'button.setAttribute("aria-pressed", String(starred))' in interface
    assert ".chat-history-star:hover, .chat-history-star.starred" in STYLE
    assert "color: #f4cf4a; opacity: 1;" in STYLE


def test_working_state_is_plain_animated_transcript_text_not_a_header_pill():
    interface = APP[APP.index("const CHAT_HARNESSES"):
                    APP.index("// ── Tabs + boot")]
    transcript = interface[interface.index("function chatFlushTranscript"):
                           interface.index("async function chatRenderOpen")]
    header = interface[interface.index("async function chatRenderOpen"):
                       interface.index("async function submit()")]
    indicator_style = STYLE[STYLE.index(".chat-working-indicator {"):
                            STYLE.index("}", STYLE.index(".chat-working-indicator {"))]

    assert 'indicator.append("<Working>", chatWorkingDots())' in interface
    assert 'className: "chat-working-indicator"' in interface
    assert 'role: "status"' in interface
    assert 'if (conversation.state === "running" && !working)' in transcript
    assert "transcript.append(chatWorkingIndicator())" in transcript
    assert "chatStatePill(conversation.state)" not in transcript
    assert "header.append(title, queueState, actions)" in header
    assert "chatStatePill(conversation.state)" not in header
    assert "border" not in indicator_style
    assert "background" not in indicator_style


def test_shell_accent_colors_cover_idle_working_waiting_and_error_states():
    interface = APP[APP.index("const CHAT_HARNESSES"):
                    APP.index("// ── Tabs + boot")]
    assert '"state-idle", "state-queued", "state-running"' in interface
    assert '"state-waiting", "state-error"' in interface
    assert 'button.classList.add("active-chat", `state-${state}`)' in interface
    assert ".chat-shell.state-idle { --shell-state-color: var(--ok); }" in STYLE
    assert ".chat-shell.state-queued," in STYLE
    assert ".chat-shell.state-running," in STYLE
    assert ".chat-shell.state-waiting { --shell-state-color: var(--accent); }" in STYLE
    assert ".chat-shell.state-error { --shell-state-color: #ef7d86; }" in STYLE


def test_redline_chat_text_is_one_pixel_larger():
    assert "font-size: 15px;" in STYLE[STYLE.index(".chat-shell {"):
                                      STYLE.index(".chat-shell:hover")]
    assert "background: var(--panel); font-size: 15px;" in STYLE
    assert "font-size: calc(.62rem + 1px)" in STYLE


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
        ".chat-working-indicator",
        ".chat-history-open",
        ".chat-history-star",
    ):
        assert selector in STYLE
    assert "grid-template-columns: 260px 260px minmax(0, 1fr)" in STYLE
    assert "grid-template-columns: 210px 210px minmax(0, 1fr)" in STYLE
    assert "grid-template-columns: 101px minmax(0, 1fr)" in STYLE
    assert ".chat-working-dots" in STYLE
    assert "@keyframes chat-working-dot" in STYLE
    assert ".chat-queue-state" in STYLE
