"""Optional real-browser smoke for the conversation Diff workspace.

The normal suite skips this module when Playwright is absent. The repository's
visual-QA lane provisions Playwright 1.54.0, at which point this becomes an
end-to-end DOM/runtime gate over the actual static UI and mocked read API.
"""

from __future__ import annotations

import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / ".super-coder" / "ui"
CONVERSATION_ID = "cv_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TARGET_ID = "gt_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args) -> None:
        pass


@pytest.fixture
def static_ui():
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        partial(QuietHandler, directory=str(UI)),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def conversation() -> dict:
    return {
        "conversation_id": CONVERSATION_ID,
        "state": "running",
        "version": 4,
        "created_at": "2026-07-30 20:00:00",
        "title": "Diff workspace proof",
        "starred": True,
        "close_requested_at": None,
        "route": {"harness": "codex", "model": "gpt-5.6"},
        "shell": {
            "shell_id": 1,
            "display_name": "CC",
            "shortname": "cc",
        },
    }


def target_page() -> dict:
    return {
        "conversation_id": CONVERSATION_ID,
        "selected_target_id": TARGET_ID,
        "git_fingerprint": "git-fingerprint-1",
        "freshness": {
            "local": "fresh",
            "remote": "fresh",
            "remote_error": None,
        },
        "items": [
            {
                "target_id": TARGET_ID,
                "kind": "workspace",
                "branch": "feat/browser-diff-workspace",
                "base_ref": "origin/main",
                "head_sha": "a" * 40,
                "pr_number": None,
                "lifecycle": "local",
                "title": None,
                "url": None,
                "fingerprint": "target-fingerprint-1",
                "freshness": {
                    "local": "fresh",
                    "remote": "not_applicable",
                },
                "facts": {
                    "checked_out": True,
                    "dirty": True,
                    "pushed": False,
                    "ahead": 1,
                    "behind": 3,
                    "cleanup_pending": False,
                },
            },
            {
                "target_id": "gt_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "kind": "pull_request",
                "branch": "feat/previous-delivery",
                "base_ref": "main",
                "head_sha": "b" * 40,
                "pr_number": 815,
                "lifecycle": "pr_merged",
                "title": "Previous merged delivery",
                "url": "https://example.test/pull/815",
                "fingerprint": "target-fingerprint-merged",
                "freshness": {"local": "stored", "remote": "cached"},
                "facts": {
                    "checked_out": False,
                    "dirty": False,
                    "pushed": True,
                    "ahead": 0,
                    "behind": 40,
                    "cleanup_pending": True,
                },
            },
        ],
    }


def file_items() -> list[dict]:
    common = {
        "old_path": None,
        "staged": False,
        "unstaged": True,
        "binary": False,
        "conflict": False,
        "generated": False,
        "submodule": False,
        "oversized": False,
    }
    return [
        {
            **common,
            "file_id": "rf_1",
            "path": "src/app.js",
            "status": "modified",
            "additions": 18,
            "deletions": 4,
        },
        {
            **common,
            "file_id": "rf_2",
            "path": "src/renamed.js",
            "old_path": "src/old.js",
            "status": "renamed",
            "additions": 2,
            "deletions": 2,
            "staged": True,
            "unstaged": False,
        },
        {
            **common,
            "file_id": "rf_3",
            "path": "assets/logo.bin",
            "status": "added",
            "additions": None,
            "deletions": None,
            "binary": True,
            "staged": True,
            "unstaged": False,
        },
        {
            **common,
            "file_id": "rf_4",
            "path": "notes/new.txt",
            "status": "untracked",
            "additions": 3,
            "deletions": 0,
        },
        {
            **common,
            "file_id": "rf_5",
            "path": "old/deleted.txt",
            "status": "deleted",
            "additions": 0,
            "deletions": 8,
        },
        {
            **common,
            "file_id": "rf_6",
            "path": "src/conflict.js",
            "status": "conflict",
            "additions": 4,
            "deletions": 4,
            "conflict": True,
        },
        {
            **common,
            "file_id": "rf_7",
            "path": "large.txt",
            "status": "modified",
            "additions": None,
            "deletions": None,
            "oversized": True,
        },
    ]


def test_diff_workspace_preserves_live_chat_and_uses_get_only(
    static_ui,
    tmp_path,
):
    requests: list[tuple[str, str, str]] = []
    conditional_requests: list[str] = []
    browser_errors: list[str] = []
    remote_state = {"available": True}
    current = conversation()
    messages = [
        {
            "message_id": index,
            "message_kind": "prompt",
            "body": f"Review fixture message {index} " + ("context " * 12),
            "state": "completed",
        }
        for index in range(1, 22)
    ]
    files = file_items()
    patch = """diff --git a/src/app.js b/src/app.js
index 1111111..2222222 100644
--- a/src/app.js
+++ b/src/app.js
@@ -1,3 +1,4 @@
 const mode = 'chat';
-const oldValue = false;
+const reviewOnly = true;
+const safe = document.createTextNode('patch');
 export { mode };"""

    def fulfill(route, payload, *, status=200, headers=None):
        route.fulfill(
            status=status,
            content_type="application/json",
            headers=headers or {},
            body=json.dumps(payload),
        )

    def route_api(route):
        request = route.request
        parsed = urlparse(request.url)
        query = parse_qs(parsed.query)
        requests.append((request.method, parsed.path, parsed.query))
        if request.headers.get("if-none-match"):
            conditional_requests.append(parsed.path)
        path = parsed.path
        if path == "/api/health":
            return fulfill(route, {"repo": "subfloor"})
        if path == "/api/shells":
            return fulfill(
                route,
                {
                    "shells": [
                        {
                            "shell_id": 1,
                            "display_name": "CC",
                            "shortname": "cc",
                            "flavor": "dev",
                        }
                    ]
                },
            )
        if path == "/api/flavor-defaults":
            return fulfill(
                route,
                {
                    "flavors": {
                        "dev": [
                            {
                                "harness": "codex",
                                "model": "gpt-5.6",
                                "is_default": True,
                            }
                        ]
                    }
                },
            )
        if path == "/api/models":
            return fulfill(route, {"harnesses": {}})
        if path == "/api/sprints":
            return fulfill(route, {"items": []})
        if path == "/api/conversations":
            return fulfill(route, {"items": [current]})
        if path == f"/api/conversations/{CONVERSATION_ID}":
            return fulfill(route, current)
        if path == f"/api/conversations/{CONVERSATION_ID}/messages":
            return fulfill(route, {"items": messages})
        if path == f"/api/conversations/{CONVERSATION_ID}/review-targets":
            if (
                request.headers.get("if-none-match") == '"targets-1"'
                and "refresh" not in query
            ):
                return route.fulfill(
                    status=304,
                    headers={"ETag": '"targets-1"'},
                    body="",
                )
            body = target_page()
            if not remote_state["available"]:
                body["freshness"] = {
                    "local": "fresh",
                    "remote": "unavailable",
                    "remote_error": "fixture GitHub is offline",
                }
            return fulfill(
                route,
                body,
                headers={"ETag": '"targets-1"'},
            )
        if path.endswith("/files") and "/api/review-targets/" in path:
            scope = query.get("scope", ["review"])[0]
            etag = f'"files-{scope}-1"'
            if request.headers.get("if-none-match") == etag:
                return route.fulfill(
                    status=304,
                    headers={"ETag": etag},
                    body="",
                )
            return fulfill(
                route,
                {
                    "target_id": TARGET_ID,
                    "scope": scope,
                    "items": files if scope == "review" else files[:2],
                    "files_truncated": False,
                    "fingerprint": f"files-{scope}-1",
                    "freshness": "local",
                    "next_cursor": None,
                },
                headers={"ETag": etag},
            )
        if path.endswith("/diff") and "/api/review-targets/" in path:
            selected_path = query.get("path", [""])[0]
            binary = selected_path.endswith(".bin")
            oversized = selected_path == "large.txt"
            return fulfill(
                route,
                {
                    "target_id": TARGET_ID,
                    "scope": query.get("scope", ["review"])[0],
                    "path": selected_path,
                    "patch": None if binary or oversized else patch,
                    "sha256": "c" * 64,
                    "truncated": False,
                    "binary": binary,
                    "unavailable_reason": "binary" if binary
                    else "oversized" if oversized
                    else None,
                    "freshness": "local",
                    "fingerprint": "files-review-1",
                },
            )
        if path.endswith("/commits") and "/api/review-targets/" in path:
            return fulfill(
                route,
                {
                    "target_id": TARGET_ID,
                    "items": [
                        {
                            "sha": "d" * 40,
                            "short_sha": "ddddddd",
                            "author": "CC",
                            "authored_at": "2026-07-30T20:30:00Z",
                            "subject": "Build the Diff workspace",
                        }
                    ],
                    "commits_truncated": False,
                    "fingerprint": "commits-1",
                    "next_cursor": None,
                },
            )
        return fulfill(
            route,
            {"error": {"code": "UNMOCKED", "message": path}},
            status=404,
        )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.add_init_script(
            """
            window.__fakeEventSources = [];
            class FakeEventSource {
              constructor(url) {
                this.url = url;
                this.listeners = {};
                this.closed = false;
                window.__fakeEventSources.push(this);
                queueMicrotask(() => this.onopen && this.onopen());
              }
              addEventListener(type, callback) {
                (this.listeners[type] ||= []).push(callback);
              }
              emit(type, payload) {
                for (const callback of this.listeners[type] || [])
                  callback({ data: JSON.stringify(payload) });
              }
              close() { this.closed = true; }
            }
            window.EventSource = FakeEventSource;
            """
        )
        page.route("**/api/**", route_api)
        page.on(
            "pageerror",
            lambda error: browser_errors.append(f"pageerror: {error}"),
        )
        page.on(
            "console",
            lambda message: browser_errors.append(f"console: {message.text}")
            if message.type == "error"
            else None,
        )
        page.goto(
            f"{static_ui}/#interface/cc/{CONVERSATION_ID}",
            wait_until="networkidle",
        )
        composer = page.locator(".chat-composer-input")
        composer.fill("unsent draft survives mode changes")
        transcript = page.locator(".chat-transcript")
        page.wait_for_timeout(100)
        transcript.evaluate(
            "node => { node.scrollTop = 160; node.dispatchEvent(new Event('scroll')); }"
        )
        page.wait_for_timeout(100)
        scroll_before = transcript.evaluate("node => node.scrollTop")
        assert page.evaluate("window.__fakeEventSources.length") == 1

        page.get_by_role("tab", name="Diff").click()
        page.locator(".review-workspace").wait_for()
        assert page.url.endswith(f"/{CONVERSATION_ID}/diff")
        assert composer.input_value() == "unsent draft survives mode changes"
        assert page.locator(".chat-stop-header").is_visible()
        assert page.locator(".chat-stop-header").is_enabled()
        assert page.get_by_text("LOCAL BRANCH", exact=True).is_visible()
        assert page.get_by_text("7 files", exact=False).is_visible()
        assert page.locator(".review-line-add").count() == 2
        assert page.locator(".review-line-delete").count() == 1
        assert page.locator(".status-conflict").count() == 1
        page.get_by_title("assets/logo.bin").click()
        page.get_by_text("Binary file", exact=True).wait_for()
        page.get_by_title("large.txt").click()
        page.get_by_text("Patch too large", exact=True).wait_for()
        page.get_by_title("src/old.js → src/renamed.js").click()
        page.get_by_text("renamed from src/old.js", exact=True).wait_for()
        page.screenshot(path=tmp_path / "diff-desktop.png", full_page=True)

        page.evaluate(
            """
            window.__fakeEventSources[0].emit('assistant.delta', {
              sequence: 9001,
              event_type: 'assistant.delta',
              message_id: 1,
              run_id: 77,
              created_at: '2026-07-30T20:31:00Z',
              payload: { text: 'background completion arrived' }
            });
            """
        )
        page.get_by_role("tab", name="Commits").click()
        page.get_by_text("Build the Diff workspace", exact=True).wait_for()
        page.get_by_role("tab", name="Local only").click()
        page.locator(".review-file-row").first.wait_for()
        page.get_by_role("button", name="Hide binary").click()
        page.locator(".review-target-select").select_option(
            "gt_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        )
        page.get_by_text("PR MERGED", exact=True).wait_for()
        assert page.get_by_text("cleanup pending", exact=False).is_visible()
        assert page.get_by_text("40 behind base", exact=False).is_visible()
        page.locator(".review-target-select").select_option(TARGET_ID)
        remote_state["available"] = False
        page.get_by_role("button", name="Refresh remote").click()
        page.get_by_text("Remote unavailable", exact=False).wait_for()

        page.go_back()
        transcript.wait_for()
        page.wait_for_timeout(100)
        assert composer.input_value() == "unsent draft survives mode changes"
        assert page.get_by_text(
            "background completion arrived",
            exact=True,
        ).is_visible()
        assert abs(transcript.evaluate("node => node.scrollTop") - scroll_before) <= 1
        assert page.evaluate("window.__fakeEventSources.length") == 1

        page.go_forward()
        page.locator(".review-workspace").wait_for()
        page.set_viewport_size({"width": 760, "height": 900})
        page.screenshot(path=tmp_path / "diff-mobile.png", full_page=True)

        current["state"] = "closed"
        page.reload(wait_until="networkidle")
        page.locator(".review-workspace").wait_for()
        assert page.get_by_role("button", name="Close").is_disabled()
        assert page.get_by_text("LOCAL BRANCH", exact=True).is_visible()
        browser.close()

    review_requests = [
        item for item in requests if "/review-targets" in item[1]
    ]
    assert review_requests
    assert {method for method, _path, _query in review_requests} == {"GET"}
    assert any(path.endswith("/files") for path in conditional_requests)
    assert not browser_errors
    assert (tmp_path / "diff-desktop.png").stat().st_size > 10_000
    assert (tmp_path / "diff-mobile.png").stat().st_size > 10_000


def test_chat_performance_uses_bounded_requests_and_keyed_frames(static_ui):
    requests: list[tuple[str, str]] = []
    browser_errors: list[str] = []
    selected = {
        **conversation(),
        "title": "Deep linked fixture",
        "last_activity_at": "2026-07-20 12:00:00",
    }

    def summary(number: int, *, starred: bool = False) -> dict:
        return {
            **conversation(),
            "conversation_id": f"cv_{number:032x}",
            "state": "closed",
            "version": 1,
            "title": f"{'Starred' if starred else 'Recent'} {number:02d}",
            "starred": starred,
            "created_at": f"2026-07-{1 + number % 20:02d} 10:00:00",
            "last_activity_at": f"2026-07-{1 + number % 20:02d} 10:00:00",
        }

    recent = [summary(number) for number in range(1, 61)]
    stars = [summary(100 + number, starred=True) for number in range(3)]
    transcript = {
        "conversation_id": CONVERSATION_ID,
        "projection_version": 1,
        "through_sequence": 7000,
        "controls": {
            "conversation_version": 4,
            "conversation_state": "running",
            "queued_count": 0,
            "active_run_id": 77,
            "close_requested_at": None,
        },
        "items": [
            {
                "item_id": "message:1",
                "kind": "user",
                "order_sequence": 1,
                "message_id": 1,
                "run_id": None,
                "created_at": "2026-07-30 20:00:00",
                "text": "keep this completed node",
                "state": "completed",
                "completed_at": "2026-07-30 20:00:01",
                "text_truncated": False,
            },
            {
                "item_id": "run:77:assistant",
                "kind": "assistant",
                "order_sequence": 2,
                "message_id": 1,
                "run_id": 77,
                "created_at": "2026-07-30 20:00:02",
                "text": "initial",
                "outcome": None,
                "first_sequence": 2,
                "last_sequence": 7000,
                "text_truncated": False,
            },
        ],
        "truncation": None,
    }

    def fulfill(route, payload, *, status=200):
        route.fulfill(
            status=status,
            content_type="application/json",
            body=json.dumps(payload),
        )

    def route_api(route):
        parsed = urlparse(route.request.url)
        query = parse_qs(parsed.query)
        requests.append((parsed.path, parsed.query))
        if parsed.path == "/api/health":
            return fulfill(route, {"repo": "subfloor"})
        if parsed.path == "/api/shells":
            return fulfill(
                route,
                {
                    "shells": [
                        {
                            "shell_id": 1,
                            "display_name": "CC",
                            "shortname": "cc",
                            "flavor": "dev",
                        }
                    ]
                },
            )
        if parsed.path == "/api/sprints":
            return fulfill(route, {"items": []})
        if parsed.path == "/api/models":
            return fulfill(route, {"harnesses": {}})
        if parsed.path == "/api/flavor-defaults":
            return fulfill(route, {"flavors": {}})
        if parsed.path == "/api/conversations":
            if query.get("open") == ["true"]:
                return fulfill(route, {"items": [selected], "next_cursor": None})
            if query.get("starred") == ["true"]:
                return fulfill(route, {"items": stars, "next_cursor": None})
            if query.get("starred") == ["false"]:
                cursor = query.get("cursor", [None])[0]
                start = int(cursor or 0)
                page = recent[start:start + 20]
                next_cursor = str(start + 20) if start + 20 < len(recent) else None
                return fulfill(
                    route,
                    {"items": page, "next_cursor": next_cursor},
                )
            return fulfill(
                route,
                {"items": [selected, *recent[:20]], "next_cursor": None},
            )
        if parsed.path == f"/api/conversations/{CONVERSATION_ID}":
            return fulfill(route, selected)
        if parsed.path == f"/api/conversations/{CONVERSATION_ID}/transcript":
            return fulfill(route, transcript)
        if parsed.path == f"/api/conversations/{CONVERSATION_ID}/messages":
            return fulfill(
                route,
                {
                    "items": [
                        {
                            "message_id": 1,
                            "message_kind": "prompt",
                            "body": "keep this completed node",
                            "state": "completed",
                        }
                    ]
                },
            )
        if parsed.path == f"/api/conversations/{CONVERSATION_ID}/review-targets":
            return fulfill(route, target_page())
        return fulfill(
            route,
            {"error": {"code": "UNMOCKED", "message": parsed.path}},
            status=404,
        )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.add_init_script(
            """
            window.__chatPerf = {
              rafScheduled: 0,
              rafOutstanding: 0,
              maxRafOutstanding: 0,
              markdownParses: 0,
              transcriptReplacements: 0,
              assistantReplacements: 0,
            };
            const nativeFrame = window.requestAnimationFrame.bind(window);
            window.requestAnimationFrame = (callback) => {
              const perf = window.__chatPerf;
              perf.rafScheduled += 1;
              perf.rafOutstanding += 1;
              perf.maxRafOutstanding = Math.max(
                perf.maxRafOutstanding, perf.rafOutstanding);
              return nativeFrame((timestamp) => {
                perf.rafOutstanding -= 1;
                callback(timestamp);
              });
            };
            const nativeReplace = Element.prototype.replaceChildren;
            Element.prototype.replaceChildren = function(...children) {
              if (this.classList?.contains("chat-transcript"))
                window.__chatPerf.transcriptReplacements += 1;
              if (this.classList?.contains("chat-assistant-body"))
                window.__chatPerf.assistantReplacements += 1;
              return nativeReplace.apply(this, children);
            };
            window.__fakeEventSources = [];
            class FakeEventSource {
              constructor(url) {
                this.url = url;
                this.listeners = {};
                window.__fakeEventSources.push(this);
                queueMicrotask(() => this.onopen && this.onopen());
              }
              addEventListener(type, callback) {
                (this.listeners[type] ||= []).push(callback);
              }
              emit(type, payload) {
                for (const callback of this.listeners[type] || [])
                  callback({ data: JSON.stringify(payload) });
              }
              close() {}
            }
            window.EventSource = FakeEventSource;
            """
        )
        page.route("**/api/**", route_api)
        page.on(
            "pageerror",
            lambda error: browser_errors.append(f"pageerror: {error}"),
        )
        page.goto(
            f"{static_ui}/#interface/cc/{CONVERSATION_ID}",
            wait_until="networkidle",
        )
        page.locator(".chat-transcript").wait_for()
        page.evaluate(
            """
            const nativeParse = marked.parse.bind(marked);
            marked.parse = (...args) => {
              window.__chatPerf.markdownParses += 1;
              return nativeParse(...args);
            };
            window.__completedChatNode =
              document.querySelector('.chat-bubble.chat-user');
            """
        )

        assert not [
            path for path, _query in requests
            if path in {"/api/models", "/api/flavor-defaults"}
        ]
        assert sum(
            path.endswith("/transcript") for path, _query in requests
        ) == 1
        assert page.evaluate("window.__fakeEventSources.length") == 1
        assert page.evaluate("window.__fakeEventSources[0].url").endswith(
            f"/events?after={transcript['through_sequence']}"
        )
        assert page.locator(".chat-history-item").count() == 24

        before_more = len(requests)
        page.get_by_role("button", name="More").click()
        page.get_by_text("Recent 21", exact=True).wait_for()
        assert page.locator(".chat-history-item").count() == 44
        added_requests = requests[before_more:]
        assert sum(
            path == "/api/conversations" and "starred=false" in query
            for path, query in added_requests
        ) == 1
        assert not [
            path for path, _query in added_requests
            if path.endswith("/transcript") or "/review-targets" in path
        ]

        page.get_by_role("tab", name="Diff").click()
        page.locator(".review-workspace").wait_for()
        page.evaluate(
            """
            Object.assign(window.__chatPerf, {
              rafScheduled: 0,
              rafOutstanding: 0,
              maxRafOutstanding: 0,
              markdownParses: 0,
              transcriptReplacements: 0,
              assistantReplacements: 0,
            });
            for (let offset = 1; offset <= 500; offset += 1) {
              window.__fakeEventSources[0].emit("assistant.delta", {
                sequence: 7000 + offset,
                event_type: "assistant.delta",
                message_id: 1,
                run_id: 77,
                created_at: "2026-07-30 20:01:00",
                payload: { text: "x" },
              });
            }
            """
        )
        page.wait_for_timeout(100)
        hidden_counts = page.evaluate("window.__chatPerf")
        assert hidden_counts["markdownParses"] == 0
        assert hidden_counts["transcriptReplacements"] == 0
        assert hidden_counts["assistantReplacements"] == 0

        page.get_by_role("tab", name="Chat").click()
        page.get_by_text("initial" + ("x" * 500), exact=True).wait_for()
        catch_up = page.evaluate("window.__chatPerf")
        assert catch_up["maxRafOutstanding"] <= 1
        assert catch_up["markdownParses"] <= 1
        assert catch_up["assistantReplacements"] <= 1
        assert catch_up["transcriptReplacements"] == 0
        assert page.evaluate(
            "window.__completedChatNode === "
            "document.querySelector('.chat-bubble.chat-user')"
        )
        browser.close()

    assert not browser_errors
