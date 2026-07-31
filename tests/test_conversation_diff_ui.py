"""Browser Diff mode and read-only review workspace contracts."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / ".super-coder" / "ui" / "app.js").read_text()
STYLE = (ROOT / ".super-coder" / "ui" / "style.css").read_text()


def diff_source() -> str:
    return APP[
        APP.index("const CHAT_MODES"):
        APP.index("async function renderInterface")
    ]


def current_workspace_source() -> str:
    return APP[
        APP.index("function chatReviewWorkspace"):
        APP.index("async function chatRenderNew")
    ]


def test_diff_is_a_deep_linked_coequal_mode_without_rebuilding_chat():
    source = diff_source()

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


def test_review_client_posts_observations_and_reads_only_server_issued_files():
    source = current_workspace_source()

    assert "/review-observations`" in source
    assert '{ method: "POST", key: requestKey() }' in source
    assert "selected.file_id" in source
    assert "encodeURIComponent(selected.file_id)" in source
    assert "/review-targets/${" not in source
    assert "encodeURIComponent(file.path)" not in source
    assert "cwd=" not in source
    assert "ref=" not in source


def test_workspace_has_current_changes_shell_files_and_manual_refresh():
    source = current_workspace_source()

    for text in (
        "Changes",
        "Shell files",
        "Dirty",
        "Branch",
        "Commits",
        "Filter paths",
        "Refresh Diff",
        "No code changes",
        "No visible ahead commits.",
        "Remote main unavailable",
        "mirror mismatch",
    ):
        assert text in source
    for class_name in (
        "review-summary",
        "review-scope-switch",
        "review-file-tree",
        "review-file-row",
        "review-patch",
        "review-commit-list",
    ):
        assert class_name in source
    assert "reviewTypedState(" in source
    assert "reviewPatchRows(" in source
    assert "function reviewPatchRows" in APP
    assert 'document.createTextNode' in APP
    patch_renderer = APP[
        APP.index("function reviewPatchRows"):
        APP.index("function reviewFileTree")
    ]
    assert "innerHTML" not in patch_renderer


def test_patch_chevrons_only_render_for_real_change_targets():
    renderer = APP[
        APP.index("function reviewPatchRows"):
        APP.index("function reviewFileTree")
    ]

    for text in (
        'changeStep("next", 0, "Jump to first change")',
        "if (index > 0)",
        "if (index < changeBlocks.length - 1)",
        'target?.closest(".review-patch-wrap")',
        "scroller.scrollTop",
        "left: scroller.scrollLeft",
        'behavior: window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches',
    ):
        assert text in renderer
    assert "disabled:" not in renderer
    assert ".review-change-step" in STYLE
    assert ".review-change-step::before" in STYLE
    assert ".review-change-step:disabled" not in STYLE


def test_diff_observes_once_then_only_refreshes_manually_single_flight():
    source = current_workspace_source()

    assert 'mode === "diff" && !state.loaded && !state.loading' in source
    assert "if (state.refreshInFlight) return" in source
    assert "state.refreshInFlight = true" in source
    assert "state.refreshInFlight = false" in source
    assert "setInterval(" not in source
    assert "visibilitychange" not in source
    assert "poll" not in source.lower()


def test_refresh_reconciliation_preserves_selection_and_both_scroll_axes():
    source = current_workspace_source()

    for text in (
        "navigator.scrollTop",
        "navigator.scrollLeft",
        "patch.scrollTop",
        "patch.scrollLeft",
        "oldSnapshot.fingerprint === next.fingerprint",
        "stillPresent || nearest",
        "preservePatch: Boolean(stillPresent)",
    ):
        assert text in source
    unchanged = source[
        source.index("oldSnapshot.fingerprint === next.fingerprint") - 40:
        source.index("oldSnapshot.fingerprint === next.fingerprint") + 100
    ]
    assert "paint()" not in unchanged


def test_shell_file_body_is_exact_text_not_markdown():
    source = current_workspace_source()

    assert 'textContent: state.shellFile.body' in source
    assert 'className: "review-shell-file review-patch-wrap"' in source
    assert "marked.parse" not in source


def test_diff_layout_is_full_width_bounded_and_responsive():
    for selector in (
        ".chat-review-host",
        ".review-workspace",
        ".review-summary",
        ".review-status",
        ".review-group-switch",
        ".review-change-switch",
        ".review-patch-title",
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
    assert "grid-template-rows: auto minmax(0, 1fr)" in STYLE
    assert "flex: 1 1 auto" in STYLE
    assert "text-overflow: ellipsis" in STYLE
    source = current_workspace_source()
    summary = source[source.index('const summary = el("div"'):source.index(
        'const groupSwitch = el("div"'
    )]
    assert "...(sectionSwitch ? [sectionSwitch] : [])" in summary
    assert "summaryStatus" in summary
    assert "patchHeader(" in source
    assert "}, summary);" in source
    assert 'el("span", { title: detail }, detail)' in source
    assert "@media (max-width: 900px)" in STYLE
    responsive = STYLE[STYLE.index("@media (max-width: 900px)"):]
    assert "grid-template-columns: minmax(0, 1fr)" in responsive
