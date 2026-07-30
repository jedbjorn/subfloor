"""Browser Diff mode and read-only review workspace contracts."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / ".super-coder" / "ui" / "app.js").read_text()
STYLE = (ROOT / ".super-coder" / "ui" / "style.css").read_text()


def diff_source() -> str:
    return APP[
        APP.index("const CHAT_REVIEW_POLL_MS"):
        APP.index("async function renderInterface")
    ]


def test_diff_is_a_deep_linked_coequal_mode_without_rebuilding_chat():
    source = diff_source()

    assert "const CHAT_REVIEW_POLL_MS = 2000" in source
    assert 'const CHAT_MODES = ["chat", "diff"]' in source
    assert "function chatModeHash(" in source
    assert 'mode === "diff" ? "/diff" : ""' in source
    assert 'textContent: "Chat"' in source
    assert 'textContent: "Diff"' in source
    assert "history.pushState" in source
    assert "transcriptHost.hidden = currentMode !== \"chat\"" in source
    assert "composerRow.hidden = currentMode !== \"chat\"" in source
    assert "reviewHost.hidden = currentMode !== \"diff\"" in source
    mode_switch = source[
        source.index("const selectMode ="):
        source.index("chatModeButton.onclick", source.index("const selectMode ="))
    ]
    assert "chatRenderOpen(" not in mode_switch
    assert "chatOpenStream(" not in mode_switch
    assert "chatApi(" not in mode_switch


def test_diff_keeps_truthful_header_controls_and_chat_state_alive():
    source = diff_source()

    assert 'className: "chat-mode-switch"' in source
    assert 'ariaLabel: "Conversation mode"' in source
    assert "actions.insertBefore(headerStop, close)" in source
    assert "headerStop.hidden = currentMode !== \"diff\"" in source
    assert "headerStop.onclick = () => stop.click()" in source
    assert "stop.onclick = async () =>" in source
    assert "chatOpenStream(" in source
    assert "chatStopStream()" not in source[
        source.index("const selectMode ="):
        source.index("const paint =", source.index("const selectMode ="))
    ]


def test_review_client_is_get_only_and_uses_server_issued_identities():
    source = diff_source()
    client = source[
        source.index("async function reviewApi"):
        source.index("function reviewLifecycleLabel")
    ]

    assert 'method: "GET"' in client
    assert '"If-None-Match"' in client
    assert "response.status === 304" in client
    assert "Cache-Control" not in client
    for verb in ("POST", "PATCH", "PUT", "DELETE"):
        assert f'"{verb}"' not in client
    assert "/review-targets/${" in source
    assert "encodeURIComponent(file.path)" in source
    assert "cwd=" not in source
    assert "ref=" not in source


def test_workspace_has_targets_scopes_filters_tree_patch_and_commits():
    source = diff_source()

    for text in (
        "Review changes",
        "Local only",
        "Commits",
        "Filter paths",
        "Hide viewed",
        "Hide generated",
        "Hide binary",
        "Hide deleted",
        "Refresh remote",
        "No review targets yet.",
        "No files match this scope and filter.",
        "No commits in this change set.",
    ):
        assert text in source
    for class_name in (
        "review-summary",
        "review-target-select",
        "review-scope-switch",
        "review-file-tree",
        "review-file-row",
        "review-patch",
        "review-commit-list",
        "review-typed-state",
    ):
        assert class_name in source
    assert "function reviewPatchRows" in source
    assert 'document.createTextNode' in APP
    assert "innerHTML" not in source[source.index("function reviewPatchRows"):]


def test_viewed_state_is_ephemeral_and_scoped_to_target_fingerprint():
    source = diff_source()

    assert "const chatReviewViewed = new Map()" in source
    assert "function reviewViewedKey(targetId, fingerprint)" in source
    assert "reviewViewedKey(state.targetId, state.fileFingerprint)" in source
    assert "viewedFiles().add(file.path)" in source
    assert "chatReviewViewed.get(key)" in source


def test_local_polling_is_visible_single_flight_and_stops_in_chat():
    source = diff_source()

    assert "document.hidden || state.mode !== \"diff\" || state.pollInFlight" in source
    assert "state.pollInFlight = true" in source
    assert "finally { state.pollInFlight = false; }" in source
    assert "setInterval(pollReview, CHAT_REVIEW_POLL_MS)" in source
    assert "clearInterval(state.pollTimer)" in source
    assert 'document.addEventListener("visibilitychange", visibilityRefresh)' in source
    assert 'document.removeEventListener("visibilitychange", visibilityRefresh)' in source


def test_diff_layout_is_full_width_bounded_and_responsive():
    for selector in (
        ".chat-review-host",
        ".review-workspace",
        ".review-summary",
        ".review-body",
        ".review-file-tree",
        ".review-patch",
        ".review-line",
        ".review-line-add",
        ".review-line-delete",
        ".review-typed-state",
    ):
        assert selector in STYLE
    assert "grid-template-columns: minmax(220px, 300px) minmax(0, 1fr)" in STYLE
    assert "@media (max-width: 900px)" in STYLE
    responsive = STYLE[STYLE.index("@media (max-width: 900px)"):]
    assert "grid-template-columns: minmax(0, 1fr)" in responsive
