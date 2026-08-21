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
WAKE_INDICATOR = APP[
    APP.index("function chatWakePendingIndicator"):
    APP.index("function chatPaintShellStatus")
]
SHELL_INDICATORS = APP[
    APP.index("function chatUnreadBadge(shell)"):
    APP.index("function chatHeaderLabel(conversation)")
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


def test_sprint_badge_enters_the_current_conversation_without_a_wake():
    interface = APP[APP.index("async function renderInterface"):
                    APP.index("// ── Tabs + boot")]
    badge = APP[APP.index("function chatSprintBadge"):
                APP.index("function chatPaintShellStatus")]
    assert "sprint.current_conversation_id" in badge
    assert "location.hash = chatHash(" in badge
    assert "chatApi(" not in badge
    assert "Sprint ${sprint.sprint_id}" in badge
    assert "${sprint.role} · ${sprint.disposition}" in badge
    assert "chat-sprint-meta" not in badge
    assert "chat-sprint-pill" not in APP
    assert ".chat-sprint-pill" not in STYLE
    assert ".chat-sprint-badge" in STYLE
    badge_style = STYLE[
        STYLE.index(".chat-sprint-badge {"):STYLE.index(".chat-sprint-badge:hover")
    ]
    assert "position: absolute" not in badge_style
    assert "pointer-events: auto" in badge_style
    assert "color: var(--warn)" in badge_style


def test_shell_card_orders_left_identity_and_right_status_cluster():
    interface = APP[APP.index("async function renderInterface"):
                    APP.index("// ── Tabs + boot")]
    identity = interface[interface.index("const identity = el("):
                         interface.index("const button = el(")]
    assert identity.index('className: "chat-shell-shortname"') < identity.index(
        'className: "chat-shell-identity-separator"'
    ) < identity.index('className: "chat-shell-name"')

    status = interface[interface.index('const status = el("span"'):
                       interface.index("rail.append(shellRow)")]
    assert 'className: "chat-shell-status"' in status
    assert "chatPaintShellIndicators(statusItem, item)" in status
    assert "shellStatusItems.set" in status
    assert "chatPaintShellIndicators(target, next)" in interface

    status_style = STYLE[STYLE.index(".chat-shell-status {"):
                         STYLE.index(".chat-sprint-badge {")]
    assert "grid-column: 2" in status_style
    assert "justify-self: end" in status_style
    assert "pointer-events: none" in status_style


def test_future_pending_wake_renders_a_red_clock_with_approximate_tooltip():
    script = r"""
function el(tag, props = {}, ...children) {
  return { tag, ...props, text: children.join("") };
}
""" + WAKE_INDICATOR + r"""
const now = Date.parse("2099-07-31T12:00:00Z");
const future = chatWakePendingIndicator(
  { pending_wake_available_at: "2099-07-31 12:00:15" }, now);
const expired = chatWakePendingIndicator(
  { pending_wake_available_at: "2099-07-31 11:59:59" }, now);
const malformed = chatWakePendingIndicator(
  { pending_wake_available_at: "not-a-date" }, now);
console.log(JSON.stringify({ future, expired, malformed }));
"""
    result = run_js(script)
    assert result == {
        "future": {
            "tag": "span",
            "className": "chat-shell-wake",
            "title": "wake message pending — delivering in ~15s",
            "ariaLabel": "wake message pending — delivering in ~15s",
            "text": "◷",
        },
        "expired": None,
        "malformed": None,
    }
    wake_style = STYLE[
        STYLE.index(".chat-shell-wake {"):
        STYLE.index("}", STYLE.index(".chat-shell-wake {")) + 1
    ]
    assert "color: #ef545f" in wake_style


def test_pending_wake_indicator_refreshes_without_a_new_polling_loop():
    interface = APP[APP.index("async function renderInterface"):
                    APP.index("// ── Tabs + boot")]
    assert 'const shellProjectionRequest = api("/shells")' in POLL_BLOCK
    assert "const { shells: nextShells } = await shellProjectionRequest" in POLL_BLOCK
    assert "paintShellIndicators(nextShells)" in POLL_BLOCK
    assert "refreshWakeIndicators" in interface
    assert "onWakeDelivered(conversation.shell.shell_id)" in APP
    assert APP.count("setInterval(pollHistory, CHAT_HISTORY_POLL_MS)") == 1


def test_shell_indicator_poll_adds_changes_and_removes_sprint_badges():
    script = r"""
class FakeElement {
  constructor(tag) {
    this.tag = tag;
    this.nodeType = 1;
    this.children = [];
    this.className = "";
    this.hidden = false;
    this.classList = {
      toggle: (name, on) => {
        const names = new Set(this.className.split(" ").filter(Boolean));
        if (on) names.add(name); else names.delete(name);
        this.className = [...names].join(" ");
      },
    };
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  get textContent() {
    return this.children.map((child) =>
      typeof child === "string" ? child : child.textContent).join("");
  }
}
const el = (tag, props = {}, ...children) => {
  const node = Object.assign(new FakeElement(tag), props);
  node.append(...children);
  return node;
};
globalThis.location = {hash: ""};
const chatHash = (shell, conversation) => `${shell}/${conversation}`;
""" + SHELL_INDICATORS + r"""
const target = {row: new FakeElement("div"), status: new FakeElement("span")};

chatPaintShellIndicators(target, {
  shortname: "DEV1",
  sprint: {sprint_id: 12, role: "developer", disposition: "assigned",
           current_conversation_id: "cv12"},
});
const first = {
  rowClass: target.row.className,
  status: target.status.textContent,
};

chatPaintShellIndicators(target, {
  shortname: "DEV1",
  sprint: {sprint_id: 13, role: "reviewer", disposition: "review",
           current_conversation_id: "cv13"},
});
const changedBadge = target.status.children[0];
changedBadge.onclick();
const changed = {
  rowClass: target.row.className,
  status: target.status.textContent,
  title: changedBadge.title,
  hash: location.hash,
};

chatPaintShellIndicators(target, {shortname: "DEV1", sprint: null});
const removed = {
  rowClass: target.row.className,
  status: target.status.textContent,
  hidden: target.status.hidden,
};
console.log(JSON.stringify({first, changed, removed}));
"""
    result = run_js(script)
    assert result == {
        "first": {
            "rowClass": "has-assignment",
            "status": "Sprint 12",
        },
        "changed": {
            "rowClass": "has-assignment",
            "status": "Sprint 13",
            "title": "Sprint 13 · reviewer · review",
            "hash": "DEV1/cv13",
        },
        "removed": {
            "rowClass": "",
            "status": "",
            "hidden": True,
        },
    }


def test_sprint_conversations_are_not_closed_by_normal_chat_controls():
    interface = APP[APP.index("const CHAT_HARNESSES"):
                    APP.index("// ── Tabs + boot")]
    close_for_switch = interface[
        interface.index("async function chatCloseForSwitch"):
        interface.index("function chatBubble")
    ]
    assert 'if (conversation.scope === "sprint") return true' in close_for_switch
    assert 'const sprintScoped = conversation.scope === "sprint"' in interface
    assert (
        "const sprintManaged = Boolean(conversation.sprint_managed)" in interface
    )
    assert "close.hidden = sprintManaged" in interface
    assert "hidden: Boolean(conversation.sprint_managed)" in interface


def test_start_chat_has_default_and_configured_paths_without_terminal_controls():
    interface = APP[APP.index("const CHAT_HARNESSES"):
                    APP.index("// ── Tabs + boot")]
    assert "const CHAT_HARNESSES_FROM_SERVER = (defaults)" in interface
    assert "defaults.harness_status" in interface
    assert "status.surfaces?.browser" in interface
    assert ': LEGACY_CHAT_HARNESSES;' in interface
    assert "const availableHarnesses = CHAT_HARNESSES_FROM_SERVER(defaults);" in interface
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
    assert 'const CHAT_HARNESS_DEFAULT_VALUE = "__sc_harness_default__"' in interface
    assert "No connected provider models available" in interface
    assert "connected providers" in interface
    assert 'ariaLabel: "Thinking level"' in interface
    assert 'el("label", { className: "k" }, "Thinking level")' in interface
    assert "thinkingLevelState(harness, catalog, model, preferred)" in interface
    assert "unavailable || exactRouteMissing" in interface
    assert "model && (state.disabled || !state.selected)" in interface
    assert "Refresh & verify Default Models before saving this route." in APP
    assert '"Start chat"' in interface
    assert "harness: harnessSelect.value" in interface
    assert "if (modelSelect.value) body.model = modelSelect.value" in interface
    assert "body.model = null" in interface
    assert "if (effortSelect.value) body.effort = effortSelect.value" in interface
    assert "xterm" not in interface.lower()
    assert "tmux" not in interface.lower()
    assert "attach" not in interface.lower()


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_deepseek_config_requires_an_available_exact_server_route():
    availability = APP[
        APP.index("function chatHarnessUnavailableReason"):
        APP.index("function chatStartedLabel")
    ]
    models = APP[
        APP.index("function chatModelOptions"):
        APP.index("function chatCreateConversation")
    ]
    script = r"""
const CHAT_HARNESS_DEFAULT_VALUE = "__sc_harness_default__";
function el(_tag, attrs = {}) { return {...attrs}; }
function select() {
  return {
    children: [],
    replaceChildren() { this.children = []; },
    append(value) { this.children.push(value); },
    get options() { return this.children; },
  };
}
""" + availability + models + r"""
const catalog = {harnesses: {
  deepseek: {models: [
    {id: "deepseek-v4-pro", availability: "available"},
    {id: "stale", availability: "unavailable"},
  ]},
  codex: {models: [{id: "gpt-test", availability: "available"}]},
}};
const deepseek = select();
chatModelOptions(deepseek, catalog, "deepseek", null);
const codex = select();
chatModelOptions(codex, catalog, "codex", "gpt-test");
console.log(JSON.stringify({
  deepseek: deepseek.children,
  codex: codex.children,
  disabled: chatHarnessUnavailableReason({
    installed: true, enabled: false, healthy: false,
    surfaces: {browser: true}, unavailable_reason: "HARNESS_DISABLED",
  }),
  healthy: chatHarnessUnavailableReason({
    installed: true, enabled: true, healthy: true,
    surfaces: {browser: true}, unavailable_reason: null,
  }),
}));
"""

    result = run_js(script)
    assert result == {
        "deepseek": [
            {
                "value": "",
                "textContent": "Choose an exact model",
                "disabled": True,
                "selected": True,
            },
            {"value": "deepseek-v4-pro", "textContent": "deepseek-v4-pro"},
        ],
        "codex": [
            {"value": "", "textContent": "Use shell default — gpt-test"},
            {
                "value": "__sc_harness_default__",
                "textContent": "Use harness default",
            },
            {"value": "gpt-test", "textContent": "gpt-test"},
        ],
        "disabled": "HARNESS_DISABLED",
        "healthy": None,
    }


def test_historical_unavailable_harness_keeps_reads_and_controls_but_not_composer():
    interface = APP[APP.index("const CHAT_HARNESSES"):
                    APP.index("// ── Tabs + boot")]
    open_chat = interface[
        interface.index("async function chatRenderOpen"):
        interface.index("async function renderInterface")
    ]
    assert "chatLoadHarnessDefaults().catch(() => null)" in interface
    assert "history remains readable" in open_chat
    assert "composer.disabled = Boolean(unavailableReason)" in open_chat
    assert "send.disabled = Boolean(unavailableReason)" in open_chat
    assert "stop.disabled = conversation.state !== \"running\"" in open_chat
    assert "close.disabled = sprintManaged || closed || closing" in open_chat
    assert 'textContent: "Analytics"' in open_chat
    assert "chatReviewWorkspace(reviewHost, conversation)" in open_chat
    assert ".chat-harness-unavailable" in STYLE


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


def test_reasoning_streams_as_distinct_assistant_segments_without_approval_ui():
    interface = APP[APP.index("const CHAT_HARNESSES"):
                    APP.index("// ── Tabs + boot")]
    reducer = interface[
        interface.index("const reduceEvent = (event) =>"):
        interface.index("chatOpenStream(", interface.index(
            "const reduceEvent = (event) =>"))
    ]
    assert 'event.payload?.segment === "reasoning"' in reducer
    assert "previousSegment !== segment" in reducer
    assert "anchor = sequence" in reducer
    assert "assistantSegments.delete(event.run_id)" in reducer
    assert 'bubble.classList.add("chat-reasoning")' in interface
    assert 'segment === "reasoning" ? "Reasoning"' in interface
    assert ".chat-bubble.chat-assistant.chat-reasoning" in STYLE
    assert "approval control" not in interface.lower()
    assert "approval button" not in interface.lower()


def test_transcript_installs_snapshot_then_coalesces_keyed_live_updates():
    interface = APP[APP.index("const CHAT_HARNESSES"):
                    APP.index("// ── Tabs + boot")]
    keyed = interface[interface.index("function chatCreateTranscriptState"):
                      interface.index("async function renderInterface")]

    assert "snapshot.projection_version !== 3" in interface
    assert "chatTranscriptPageItems(snapshot)" in keyed
    assert "items.has(item.item_id)" in interface
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
    assert 'type === "usage"' in keyed
    assert "assistant.context_tokens = contextTokens" in keyed
    assert "transcriptState.throughSequence" in keyed
    assert "/events?after=${afterSequence}" in interface
    assert "/transcript`" in keyed
    assert "/messages?limit=100" not in interface[
      interface.index("const loadTranscript = async"):
      interface.index("await loadTranscript()")
    ]


def test_transcript_window_keeps_five_twenty_turn_pages_and_live_tail():
    helpers = APP[
        APP.index("function chatTranscriptPageItems"):
        APP.index("function chatTranscriptItemNode")
    ]
    script = r"""
const CHAT_TRANSCRIPT_PAGE_TURNS = 20;
const CHAT_TRANSCRIPT_MAX_PAGES = 5;
""" + helpers + r"""
function snapshot(conversationId, first, last, olderCursor) {
  return {
    conversation_id: conversationId,
    projection_version: 3,
    through_sequence: 140,
    controls: {active_run_id: null},
    older_cursor: olderCursor,
    truncation: last < 140 ? {reason: "turn_limit"} : null,
    items: Array.from({length: last - first + 1}, (_, offset) => {
      const turn = first + offset;
      return {
        item_id: `message:${turn}`,
        kind: "user",
        order_sequence: turn,
        message_id: turn,
        run_id: null,
        text: `prompt ${turn}`,
        state: "completed",
      };
    }),
  };
}
const state = chatCreateTranscriptState(snapshot("cv_test", 121, 140, "c120"));
for (const [first, last, cursor] of [
  [101, 120, "c100"], [81, 100, "c80"], [61, 80, "c60"],
  [41, 60, "c40"], [21, 40, "c20"], [1, 20, null],
]) chatMergeOlderTranscriptPage(state, snapshot("cv_test", first, last, cursor));

let overlap = "";
try {
  chatMergeOlderTranscriptPage(state, snapshot("cv_test", 1, 20, null));
} catch (error) { overlap = error.message; }

const beforeLiveUsers = state.order.filter(
  (id) => state.items.get(id)?.kind === "user",
).length;
state.items.set("message:141", {
  item_id: "message:141", kind: "user", order_sequence: 141,
  message_id: 141, run_id: null, text: "prompt 141", state: "queued",
});
chatTrackLiveTranscriptItem(state, "message:141", true);
const users = state.order.filter((id) => state.items.get(id)?.kind === "user");
console.log(JSON.stringify({
  pages: state.pages.length,
  beforeLiveUsers,
  users: users.length,
  hasOldest: state.items.has("message:1"),
  hasTailStart: state.items.has("message:121"),
  hasLive: state.items.has("message:141"),
  displaced: state.windowDisplaced,
  overlap,
}));
"""
    assert run_js(script) == {
        "pages": 5,
        "beforeLiveUsers": 100,
        "users": 81,
        "hasOldest": False,
        "hasTailStart": True,
        "hasLive": True,
        "displaced": True,
        "overlap": "Transcript history page overlaps loaded turns.",
    }


def test_transcript_history_load_is_retryable_and_preserves_scroll_anchor():
    keyed = APP[
        APP.index("function chatCreateTranscriptState"):
        APP.index("async function renderInterface")
    ]
    assert "CHAT_TRANSCRIPT_PAGE_TURNS = 20" in APP
    assert "CHAT_TRANSCRIPT_MAX_PAGES = 5" in APP
    assert "if (!cursor || pageState.olderLoading) return" in keyed
    assert "chatMergeOlderTranscriptPage(pageState, snapshot)" in keyed
    assert "pageState.olderError = error" in keyed
    assert "state.pendingPrepend" in keyed
    assert "anchor.offsetTop - anchorOffset" in keyed
    assert 'className: "chat-transcript-history"' in keyed
    assert 'className: "chat-transcript-window-gap"' in keyed
    assert "await reconcileTranscript(true)" in keyed
    assert ".chat-transcript-history" in STYLE
    assert ".chat-transcript-window-gap" in STYLE


def test_segmented_transcript_source_contract_is_versioned_and_run_scoped():
    interface = APP[APP.index("const CHAT_HARNESSES"):
                    APP.index("// ── Tabs + boot")]
    keyed = interface[interface.index("function chatCreateTranscriptState"):
                      interface.index("async function renderInterface")]

    assert "snapshot.projection_version !== 3" in interface
    assert "chatTranscriptPageItems(snapshot)" in keyed
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
    assert "transcript.scrollTop = followTail" in interface
    assert "? transcript.scrollHeight" in interface
    assert ": anchor?.isConnected ? anchor.offsetTop - anchorOffset : previousTop" in interface
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
    assert "const reopenable = closed && !sprintScoped" in interface
    assert "composer.disabled = Boolean(unavailableReason)" in interface
    assert "|| closing || (closed && !reopenable)" in interface
    assert "send.disabled = Boolean(unavailableReason)" in interface
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


def test_message_bubbles_show_local_creation_time_and_omit_invalid_values():
    helper = APP[
        APP.index("function chatMessageTimeLabel"):
        APP.index("function chatShellLabel")
    ]
    bubble = APP[
        APP.index("function chatContextTokenLabel"):
        APP.index("function chatTranscriptAtBottom")
    ]
    script = r"""
process.env.TZ = "UTC";
function el(tag, props = {}, ...children) {
  return {
    tag, ...props, children,
    classList: { add() {} },
    append(...items) { this.children.push(...items); },
  };
}
function mdBlock(body) { return el("div", { className: "md" }, body); }
function chatShellLabel() { return "Shell"; }
const fmt = (value) => value.toLocaleString("en-US");
""" + helper + bubble + r"""
const createdAt = "2026-08-06T06:17:00+02:00";
const user = chatBubble("user", "hello", "completed", null, createdAt);
const userWithTokens = chatBubble("user", "hello", "", null, createdAt, 12345);
const assistant = chatBubble("assistant", "hello", "", null, createdAt, 12345);
const assistantWithoutTokens = chatBubble("assistant", "hello", "", null, createdAt);
const activity = chatBubble("activity", "working");
console.log(JSON.stringify({
  valid: chatMessageTimeLabel(createdAt),
  missing: chatMessageTimeLabel(null),
  invalid: chatMessageTimeLabel("not-a-time"),
  header: {
    className: user.children[0].className,
    who: user.children[0].children[0].children[0],
    timeTag: user.children[0].children[1].tag,
    timeClass: user.children[0].children[1].className,
    dateTime: user.children[0].children[1].dateTime,
    time: user.children[0].children[1].children[0],
  },
  activityHeaderItems: activity.children[0].children.length,
  assistantToken: {
    className: assistant.children.at(-1).className,
    text: assistant.children.at(-1).children[0],
  },
  userHasToken: userWithTokens.children.some(
    (child) => child.className === "chat-context-tokens"),
  assistantWithoutTokensHasToken: assistantWithoutTokens.children.some(
    (child) => child.className === "chat-context-tokens"),
}));
"""
    assert run_js(script) == {
        "valid": "04:17",
        "missing": "",
        "invalid": "",
        "header": {
            "className": "chat-bubble-head",
            "who": "You",
            "timeTag": "time",
            "timeClass": "chat-message-time",
            "dateTime": "2026-08-06T06:17:00+02:00",
            "time": "04:17",
        },
        "activityHeaderItems": 1,
        "assistantToken": {
            "className": "chat-context-tokens",
            "text": "12,345 tok",
        },
        "userHasToken": False,
        "assistantWithoutTokensHasToken": False,
    }
    transcript_item = APP[
        APP.index("function chatTranscriptItemNode"):
        APP.index("function chatUpdateTranscriptNode")
    ]
    assert "item.created_at" in transcript_item


def test_shell_rail_mail_badge_only_renders_for_unread_messages():
    helper = APP[
        APP.index("function chatUnreadBadge(shell)"):
        APP.index("function chatHeaderLabel(conversation)")
    ]
    script = r"""
const document = {
  createElement(tag) {
    return {
      tag, nodeType: 1, children: [],
      append(...children) { this.children.push(...children); },
    };
  },
  createTextNode(value) { return { nodeType: 3, textContent: String(value) }; },
};
const el = (t, props = {}, ...kids) => {
  const n = Object.assign(document.createElement(t), props);
  for (const k of kids) n.append(k?.nodeType ? k : document.createTextNode(k ?? ""));
  return n;
};
""" + helper + r"""
const none = chatUnreadBadge({ unread_message_count: 0 });
const badge = chatUnreadBadge({ unread_message_count: 3 });
console.log(JSON.stringify({
  none,
  badge: {
    tag: badge.tag,
    className: badge.className,
    title: badge.title,
    ariaLabel: badge.ariaLabel,
    text: badge.children[0].textContent,
  },
}));
"""
    assert run_js(script) == {
        "none": None,
        "badge": {
            "tag": "span",
            "className": "chat-shell-mail",
            "title": "3 unread messages",
            "ariaLabel": "3 unread messages",
            "text": "📩",
        },
    }
    interface = APP[
        APP.index("async function renderInterface"):
        APP.index("// ── Tabs + boot")
    ]
    assert "const mail = chatUnreadBadge(shell)" in SHELL_INDICATORS
    assert "chatPaintShellIndicators(statusItem, item)" in interface
    assert ".chat-shell-mail" in STYLE


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
    loader = APP[APP.index("function chatLoadHarnessDefaults"):
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
const shellCalls = [];
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
async function api(path) {
  shellCalls.push(path);
  if (path === "/shells") return {shells: []};
  throw new Error(`unexpected API request: ${path}`);
}
const historyItems = new Map([["cv1", {id: "selected"}]]);
const shellItems = new Map([[7, {}]]);
const acceptSummary = (conversation) => conversation;
const chatPaintHistoryItem = (item, conversation) => {
  painted.push([item.id, conversation.state, conversation.version]);
};
const chatPaintShellState = () => {};
const paintWakeIndicators = () => {};
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
  console.log(JSON.stringify({
    calls, shellCalls, painted, selected: selectedConversation,
  }));
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
    assert result["shellCalls"] == ["/shells", "/shells", "/shells"]
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
        ".chat-context-tokens",
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
    assert "grid-template-columns: 280px 270px minmax(0, 1fr)" in STYLE
    assert "grid-template-columns: 210px 210px minmax(0, 1fr)" in STYLE
    assert "grid-template-columns: 101px minmax(0, 1fr)" in STYLE
    bubble = STYLE[STYLE.index(".chat-bubble {"):
                   STYLE.index(".chat-bubble.chat-activity")]
    assert "max-width: min(840px, 85%)" in bubble
    assert "min-width: min(640px, 65%)" in bubble
    assert ".chat-working-dots" in STYLE
    assert "@keyframes chat-working-dot" in STYLE
    assert ".chat-queue-state" in STYLE
