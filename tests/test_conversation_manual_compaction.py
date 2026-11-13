"""Feature #95, spec #272: the manual compact button (AC14, AC15) and the
cold-return notice (AC11).

Hermetic. The harness lines are handwritten in the shapes the installed
harnesses emitted when each manual trigger was run once on a scratch session
(Claude 2.1.322 ``-p /compact``, Codex 0.167.1 ``thread/compact/start``,
OpenCode 1.19.5 ``POST /session/{id}/summarize``); nothing here starts one.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import time
import unittest
from collections.abc import Mapping
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / ".super-coder" / "api"))

import active_chat_registry  # noqa: E402
import conversation_compaction  # noqa: E402
import conversation_events  # noqa: E402
import conversation_reaper  # noqa: E402
import conversation_routes  # noqa: E402
from conversation_broker import BrokerStore  # noqa: E402
from conversation_adapters import (  # noqa: E402
    ADAPTER_TYPES,
    AdapterError,
    ClaudeAdapter,
    CodexAdapter,
    ConversationContext,
    KimiAdapter,
    NativeTurn,
    NormalizedEvent,
    OpenCodeAdapter,
)
from conversation_adapters import opencode as opencode_adapter  # noqa: E402
from conversation_adapters.base import (  # noqa: E402
    ConversationAdapter,
    compaction_event,
    usage_event,
)
from test_conversation_api import ConversationApiCase  # noqa: E402
from test_conversation_compaction import (  # noqa: E402
    CLAUDE_INIT,
    LINE,
    SESSION,
    AdapterCase,
    DispatchCase,
    FakeCodexRpc,
    RecordingAdapter,
    codex_usage,
    of_type,
)
from test_conversation_ui import APP, STYLE, run_js  # noqa: E402

# ── Recorded shapes ─────────────────────────────────────────────────────────

# A resumed headless ``/compact`` run (Claude 2.1.322), in stream order. The
# ``init`` line arrives after the status lines, the boundary states the
# trigger and both sizes, and the ``result`` has the zero-turn shape of a
# resume preamble but names the command it ran.
CLAUDE_COMPACTING = {
    "type": "system", "subtype": "status", "status": "compacting",
    "session_id": SESSION,
}
CLAUDE_COMPACTED = {
    "type": "system", "subtype": "status", "status": None,
    "compact_result": "success", "session_id": SESSION,
}
CLAUDE_MANUAL_BOUNDARY = {
    "type": "system", "subtype": "compact_boundary", "session_id": SESSION,
    "uuid": "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff",
    "compact_metadata": {
        "trigger": "manual", "pre_tokens": 27888, "post_tokens": 7628,
        "cumulative_dropped_tokens": 20260, "duration_ms": 4407,
    },
}
CLAUDE_SUMMARY = {
    "type": "user", "session_id": SESSION,
    "message": {"role": "user", "content": (
        "This session is being continued from a previous conversation. "
        "Summary: PRIVATE SUMMARY TEXT"
    )},
}
CLAUDE_LOCAL_STDOUT = {
    "type": "user", "session_id": SESSION, "isReplay": True,
    "message": {"role": "user", "content": (
        "<local-command-stdout>Compacted </local-command-stdout>"
    )},
}
CLAUDE_COMPACT_RESULT = {
    "type": "result", "subtype": "success", "is_error": False,
    "session_id": SESSION, "num_turns": 0, "stop_reason": None, "result": "",
    "local_command": "compact",
    "usage": {"input_tokens": 0, "cache_creation_input_tokens": 0,
              "cache_read_input_tokens": 0, "output_tokens": 0},
}
# What Claude writes instead when there is nothing to compact: a locally
# authored assistant message and the same successful ``result``.
CLAUDE_REFUSAL = {
    "type": "assistant", "session_id": SESSION,
    "message": {"model": "<synthetic>", "role": "assistant",
                "content": [{"type": "text",
                             "text": "Error: No messages to compact"}]},
}
CLAUDE_COMPACT_FAILED = {
    "type": "system", "subtype": "status", "status": None,
    "compact_result": "failed", "compact_error": "Compaction was interrupted",
    "session_id": SESSION,
}

# The Codex app-server after ``thread/compact/start`` on a resumed thread
# (0.167.1): the last turn's usage is replayed under its old turn id, then
# the compaction runs as a turn of its own, with a new id.
CODEX_REPLAYED_USAGE = codex_usage(input_tokens=84635, total=84640, window=475000)
COMPACT_TURN = "turn-compact"


def compact_note(method: str, **params: Any) -> dict[str, Any]:
    return {"method": method,
            "params": {"threadId": "thread-1", "turnId": COMPACT_TURN, **params}}


def compact_usage(*, input_tokens: int, total: int) -> dict[str, Any]:
    note = codex_usage(input_tokens=input_tokens, total=total, window=475000)
    note["params"]["turnId"] = COMPACT_TURN
    return note


CODEX_COMPACT_STARTED = compact_note(
    "turn/started", turn={"id": COMPACT_TURN, "status": "inProgress"})
CODEX_COMPACT_ITEM = {"id": "item-manual-1", "type": "contextCompaction"}
CODEX_COMPACT_COMPLETED = compact_note(
    "turn/completed", turn={"id": COMPACT_TURN, "status": "completed"})
CODEX_MANUAL_SEQUENCE = [
    {"method": "thread/status/changed",
     "params": {"threadId": "thread-1", "status": {"type": "idle"}}},
    CODEX_REPLAYED_USAGE,
    CODEX_COMPACT_STARTED,
    compact_note("item/started", item=CODEX_COMPACT_ITEM),
    compact_usage(input_tokens=0, total=18766),
    compact_note("item/completed", item=CODEX_COMPACT_ITEM),
    CODEX_COMPACT_COMPLETED,
]


class CompactingCodexRpc(FakeCodexRpc):
    """A Codex app-server that accepts (or refuses) the compact request."""

    def __init__(self, notifications=None, *, refuse: str | None = None) -> None:
        super().__init__(notifications)
        self.refuse = refuse

    def request(self, method: str, params: Mapping[str, Any]) -> Any:
        if method == "turn/interrupt":
            self.requests.append((method, dict(params)))
            return {}
        if method == "thread/compact/start":
            self.requests.append((method, dict(params)))
            if self.refuse:
                raise AdapterError(
                    "HARNESS_PROTOCOL_ERROR",
                    f"Codex thread/compact/start failed: {self.refuse}",
                )
            return {}
        return super().request(method, params)


OPENCODE_SESSION = "ses_manual"


def opencode_event(kind: str, **properties: Any) -> dict[str, Any]:
    return {"type": kind,
            "properties": {"sessionID": OPENCODE_SESSION, **properties}}


# ``POST /session/{id}/summarize`` on OpenCode 1.19.5: the session goes busy,
# a ``summary`` assistant message streams, then ``session.compacted`` and
# ``session.idle``; the request answers ``true``.
OPENCODE_SUMMARY_MESSAGE = opencode_event(
    "message.updated",
    info={"id": "msg-summary", "role": "assistant", "summary": True,
          "mode": "compaction", "sessionID": OPENCODE_SESSION,
          "tokens": {"total": 13671, "input": 13657, "output": 14,
                     "reasoning": 0, "cache": {"read": 0, "write": 0}}},
)
OPENCODE_MANUAL_SEQUENCE = [
    opencode_event("session.status", status={"type": "busy"}),
    OPENCODE_SUMMARY_MESSAGE,
    opencode_event("message.part.delta", messageID="msg-summary",
                   partID="part-1", field="text", delta="PRIVATE SUMMARY TEXT"),
    opencode_event("session.compacted"),
    opencode_event("session.status", status={"type": "idle"}),
    opencode_event("session.idle"),
]


class FakeOpenCodeServer:
    def __init__(
        self, events: list[dict[str, Any]], *, refuse: bool = False,
        messages: list[dict[str, Any]] | None = None,
    ):
        self.events = events
        self.refuse = refuse
        self.messages = messages or []
        self.requests: list[tuple[str, str, Any]] = []

    def request(self, method, path, *, query=None, body=None):
        self.requests.append((method, path, body))
        if method == "GET" and path == f"/session/{OPENCODE_SESSION}":
            return {"id": OPENCODE_SESSION, "title": "chat"}
        if method == "GET" and path == f"/session/{OPENCODE_SESSION}/message":
            return self.messages
        if method == "POST" and path == f"/session/{OPENCODE_SESSION}/message":
            return {"info": {"role": "assistant", "sessionID": OPENCODE_SESSION},
                    "parts": []}
        if path == "/session/status":
            return {OPENCODE_SESSION: {"type": "idle"}}
        if method == "POST" and path.endswith("/summarize"):
            if self.refuse:
                raise AdapterError(
                    "HARNESS_PROTOCOL_ERROR", "Session not found: ses_manual"
                )
            return True
        if method == "POST" and path.endswith("/abort"):
            return True
        raise AssertionError(f"unexpected OpenCode request: {method} {path}")

    def stream(self, path, *, query=None):
        return iter(self.events)


# ── AC14: the request reaches each harness ──────────────────────────────────


class ClaudeManualCompactTest(AdapterCase):
    """Claude: a resumed headless run whose prompt is ``/compact``."""

    def compact(self, rows: list[dict[str, Any]]):
        adapter, runner = self.claude(rows)
        path = adapter._session_path(SESSION, self.worktree)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"type": "user", "sessionId": SESSION}) + "\n")
        turn = adapter.compact(SESSION, self.context(compact_window=120000))
        return runner, turn, list(adapter.stream(turn))

    def test_launch_is_a_resumed_run_whose_whole_prompt_is_the_command(self):
        runner, turn, _events = self.compact([CLAUDE_COMPACT_RESULT])
        [(argv, _env)] = runner.calls
        self.assertEqual(argv[argv.index("-p") + 1], "/compact")
        self.assertEqual(argv[argv.index("--resume") + 1], SESSION)
        self.assertNotIn("--session-id", argv)
        # Nothing is appended to the command: no re-ground line, no text.
        self.assertNotIn(LINE, " ".join(argv))
        self.assertTrue(turn.metadata["compaction"])

    def test_recorded_run_is_one_manual_compaction_and_a_terminal(self) -> None:
        _runner, turn, events = self.compact([
            CLAUDE_COMPACTING, CLAUDE_COMPACTED, CLAUDE_INIT,
            CLAUDE_MANUAL_BOUNDARY, CLAUDE_SUMMARY, CLAUDE_LOCAL_STDOUT,
            CLAUDE_COMPACT_RESULT,
        ])
        [compacted] = of_type(events, "context.compacted")
        self.assertEqual(compacted.payload["trigger"], "manual")
        self.assertEqual(compacted.payload["tokens_before"], 27888)
        self.assertEqual(compacted.payload["tokens_after"], 7628)
        # The zero-turn result names its command: it ends the run instead of
        # being skipped as a resume preamble.
        self.assertEqual(events[-1].type, "run.completed")
        self.assertEqual(turn.metadata["terminal"], "run.completed")
        self.assertEqual(of_type(events, "run.failed"), [])
        # The summary text is never an event.
        self.assertNotIn("PRIVATE SUMMARY TEXT", json.dumps(
            [dict(event.payload) for event in events]))

    def test_a_refusal_ends_the_run_failed_with_claudes_reason(self) -> None:
        _runner, _turn, events = self.compact([
            CLAUDE_INIT, CLAUDE_REFUSAL, CLAUDE_COMPACT_RESULT,
        ])
        self.assertEqual(of_type(events, "context.compacted"), [])
        self.assertEqual(events[-1].type, "run.failed")
        self.assertEqual(events[-1].payload["error"], "HARNESS_COMPACT_REFUSED")
        self.assertEqual(events[-1].payload["detail"], "No messages to compact")

    def test_a_refusal_line_does_not_override_a_real_boundary(self) -> None:
        _runner, _turn, events = self.compact([
            CLAUDE_INIT, CLAUDE_REFUSAL, CLAUDE_MANUAL_BOUNDARY,
            CLAUDE_COMPACT_RESULT,
        ])
        self.assertEqual(len(of_type(events, "context.compacted")), 1)
        self.assertEqual(events[-1].type, "run.completed")
        self.assertEqual(of_type(events, "run.failed"), [])

    def test_the_requests_cost_is_read_from_model_usage(self) -> None:
        # The run's ``usage`` is all zeros; the summarising request is
        # counted in ``modelUsage`` only.
        result = {**CLAUDE_COMPACT_RESULT, "modelUsage": {"claude-test-model": {
            "inputTokens": 2000, "outputTokens": 815,
            "cacheReadInputTokens": 32955, "cacheCreationInputTokens": 22812,
            "contextWindow": 200000}}}
        _runner, _turn, events = self.compact([
            CLAUDE_INIT, CLAUDE_MANUAL_BOUNDARY, result])
        [usage] = of_type(events, "usage")
        self.assertEqual(usage.payload["tokens"], {
            "input_tokens": 2000, "output_tokens": 815,
            "cache_read_input_tokens": 32955,
            "cache_creation_input_tokens": 22812,
        })
        self.assertNotIn("context_tokens", usage.payload)
        # A refused run made no request and states no usage at all.
        _runner, _turn, refused = self.compact([
            CLAUDE_INIT, CLAUDE_REFUSAL, CLAUDE_COMPACT_RESULT])
        self.assertEqual(of_type(refused, "usage"), [])

    def test_a_failed_compaction_status_ends_the_run_failed(self) -> None:
        _runner, _turn, events = self.compact([
            CLAUDE_COMPACTING, CLAUDE_COMPACT_FAILED, CLAUDE_INIT,
            CLAUDE_COMPACT_RESULT,
        ])
        self.assertEqual(events[-1].type, "run.failed")
        self.assertEqual(events[-1].payload["detail"], "Compaction was interrupted")

    def test_a_run_with_no_boundary_and_no_reason_completes_unreported(self):
        _runner, _turn, events = self.compact([CLAUDE_INIT, CLAUDE_COMPACT_RESULT])
        self.assertEqual(of_type(events, "context.compacted"), [])
        self.assertEqual(events[-1].type, "run.completed")

    def test_an_ordinary_resume_preamble_is_still_not_a_terminal(self) -> None:
        preamble = {**CLAUDE_COMPACT_RESULT}
        del preamble["local_command"]
        adapter, _runner = self.claude()
        self.assertTrue(adapter._is_resume_preamble(preamble))
        self.assertFalse(adapter._is_resume_preamble(CLAUDE_COMPACT_RESULT))

    def test_a_lost_session_is_refused_before_any_launch(self) -> None:
        adapter, runner = self.claude()
        with self.assertRaises(AdapterError) as raised:
            adapter.compact(SESSION, self.context())
        self.assertEqual(raised.exception.code, "HARNESS_SESSION_LOST")
        self.assertEqual(runner.calls, [])


class CodexManualCompactTest(AdapterCase):
    """Codex: ``thread/compact/start`` on the resumed thread."""

    def compact(self, notifications, **rpc):
        native = CompactingCodexRpc(notifications, **rpc)
        adapter = CodexAdapter(rpc=native)
        turn = adapter.compact("thread-1", self.context(context_tokens=84640))
        return native, adapter, turn

    def test_request_is_resume_then_compact_and_no_turn_is_started(self) -> None:
        native, adapter, turn = self.compact(CODEX_MANUAL_SEQUENCE)
        self.assertEqual(
            [method for method, _params in native.requests],
            ["thread/resume", "thread/compact/start"],
        )
        self.assertEqual(native.requests[1][1], {"threadId": "thread-1"})
        self.assertTrue(adapter.compaction_requested)
        self.assertTrue(turn.metadata["compaction"])

    def test_recorded_sequence_is_one_manual_compaction_with_both_sizes(self):
        _native, adapter, turn = self.compact(CODEX_MANUAL_SEQUENCE)
        events = list(adapter.stream(turn))
        [compacted] = of_type(events, "context.compacted")
        self.assertEqual(compacted.payload["trigger"], "manual")
        self.assertEqual(compacted.payload["tokens_before"], 84640)
        self.assertEqual(compacted.payload["tokens_after"], 18766)
        self.assertEqual(events[-1].type, "run.completed")
        # The compaction's own turn id is adopted from ``turn/started``.
        self.assertEqual(turn.run_ref, COMPACT_TURN)
        # The tag is spent: a later compaction is automatic again.
        self.assertFalse(adapter.compaction_requested)

    def test_accepted_request_with_no_signal_completes_unreported(self) -> None:
        _native, adapter, turn = self.compact([])
        events = list(adapter.stream(turn))
        self.assertEqual(of_type(events, "context.compacted"), [])
        self.assertEqual(events[-1].type, "run.completed")
        self.assertEqual(events[-1].payload["compaction_wait"], "elapsed")
        self.assertFalse(adapter.compaction_requested)

    def test_the_wait_for_a_signal_is_bounded(self) -> None:
        class SilentRpc(CompactingCodexRpc):
            waits: list[float] = []

            def next_notification(self, timeout: float):
                self.waits.append(timeout)
                return None

        native = SilentRpc([])
        adapter = CodexAdapter(rpc=native, compact_start_seconds=0.3)
        turn = adapter.compact("thread-1", self.context())
        started = time.monotonic()
        events = list(adapter.stream(turn))
        self.assertEqual(events[-1].type, "run.completed")
        self.assertEqual(events[-1].payload["compaction_wait"], "elapsed")
        # Waited in short slices, and never past the bound.
        self.assertTrue(native.waits)
        self.assertTrue(all(0 < wait <= 0.25 for wait in native.waits))
        self.assertLess(time.monotonic() - started, 5)

    def test_stop_before_the_turn_is_known_ends_the_wait_as_stopped(self) -> None:
        class SilentRpc(CompactingCodexRpc):
            def next_notification(self, timeout: float):
                time.sleep(min(timeout, 0.01))
                return None

        native = SilentRpc([])
        adapter = CodexAdapter(rpc=native, compact_start_seconds=30.0)
        turn = adapter.compact("thread-1", self.context())
        # No turn id yet: nothing can be sent to Codex, the wait just ends.
        self.assertTrue(adapter.interrupt(turn).acknowledged)
        started = time.monotonic()
        events = list(adapter.stream(turn))
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(events[-1].type, "run.interrupted")
        self.assertEqual(events[-1].interrupt_evidence, "operator")
        self.assertEqual(turn.metadata["terminal"], "run.interrupted")
        self.assertNotIn(
            "turn/interrupt", [method for method, _params in native.requests])
        self.assertFalse(adapter.compaction_requested)

    def test_stop_after_adoption_names_the_real_turn(self) -> None:
        native, adapter, turn = self.compact(CODEX_MANUAL_SEQUENCE[:3])
        stream = adapter.stream(turn)
        seen = [next(stream), next(stream)]      # session.started, run.started
        self.assertEqual(seen[-1].type, "run.started")
        self.assertEqual(turn.run_ref, COMPACT_TURN)
        adapter.interrupt(turn)
        self.assertEqual(native.requests[-1], (
            "turn/interrupt", {"threadId": "thread-1", "turnId": COMPACT_TURN}))

    def test_a_compaction_flushed_at_the_end_of_the_wait_is_still_manual(self):
        # The item completed, but neither its size after nor the turn's end
        # arrived before the wait ran out.
        _native, adapter, turn = self.compact([
            CODEX_COMPACT_STARTED,
            compact_note("item/started", item=CODEX_COMPACT_ITEM),
            compact_note("item/completed", item=CODEX_COMPACT_ITEM),
        ])
        events = list(adapter.stream(turn))
        [compacted] = of_type(events, "context.compacted")
        self.assertEqual(compacted.payload["trigger"], "manual")
        self.assertEqual(events[-1].type, "run.completed")
        self.assertFalse(adapter.compaction_requested)

    def test_the_updates_around_the_item_are_not_this_runs_usage(self) -> None:
        # Codex states no tokens for the compaction request: the updates are
        # the last turn's, replayed, and its estimate of the new context.
        _native, adapter, turn = self.compact(CODEX_MANUAL_SEQUENCE)
        self.assertEqual(of_type(adapter.stream(turn), "usage"), [])

    def test_a_refused_request_raises_and_clears_the_tag(self) -> None:
        native = CompactingCodexRpc([], refuse="thread is busy")
        adapter = CodexAdapter(rpc=native)
        with self.assertRaises(AdapterError) as raised:
            adapter.compact("thread-1", self.context())
        self.assertEqual(raised.exception.code, "HARNESS_COMPACT_REFUSED")
        self.assertIn("thread is busy", raised.exception.detail)
        self.assertFalse(adapter.compaction_requested)

    def test_a_failed_compaction_turn_ends_the_run_failed(self) -> None:
        _native, adapter, turn = self.compact([
            CODEX_COMPACT_STARTED,
            compact_note("turn/completed", turn={
                "id": COMPACT_TURN, "status": "failed",
                "error": {"message": "stream disconnected"}}),
        ])
        events = list(adapter.stream(turn))
        self.assertEqual(of_type(events, "context.compacted"), [])
        self.assertEqual(events[-1].type, "run.failed")

    def test_another_threads_turn_is_never_adopted(self) -> None:
        other = {"method": "turn/started", "params": {
            "threadId": "thread-other",
            "turn": {"id": "turn-other", "status": "inProgress"}}}
        _native, adapter, turn = self.compact([other, *CODEX_MANUAL_SEQUENCE])
        list(adapter.stream(turn))
        self.assertEqual(turn.run_ref, COMPACT_TURN)


# Recorded on a live install (Codex app-server, 258,400 window): three
# compactions and the request that followed each. The zero-input update's
# total is Codex's estimate of the rebuilt context (its instructions plus the
# summary), about 25k whatever the chat held. On a small chat that is more
# than the chat had, and more than the next request then reads.
#   (trigger, tokens_before, tokens_after, next request's context_tokens)
CODEX_LIVE_COMPACTIONS = (
    ("manual", 21143, 24965, 22108),
    ("manual", 22492, 25101, 22232),
    ("auto", 118967, 26343, 23306),
)


def live_usage(*, input_tokens: int, total: int) -> dict[str, Any]:
    note = codex_usage(input_tokens=input_tokens, total=total, window=258400)
    note["params"]["turnId"] = COMPACT_TURN
    return note


class CompactionThatFreedNothingTest(AdapterCase):
    """A figure after that is not lower than the figure before is not shown
    as a reduction; the event keeps both figures and the meter moves on."""

    def manual(self, before: int, after: int) -> NormalizedEvent:
        native = CompactingCodexRpc([
            {"method": "thread/status/changed",
             "params": {"threadId": "thread-1", "status": {"type": "idle"}}},
            # The last turn's usage, replayed: 5 output tokens on the input.
            codex_usage(input_tokens=before - 5, total=before, window=258400),
            CODEX_COMPACT_STARTED,
            compact_note("item/started", item=CODEX_COMPACT_ITEM),
            live_usage(input_tokens=0, total=after),
            compact_note("item/completed", item=CODEX_COMPACT_ITEM),
            CODEX_COMPACT_COMPLETED,
        ])
        adapter = CodexAdapter(rpc=native)
        turn = adapter.compact("thread-1", self.context(context_tokens=before))
        [compacted] = of_type(list(adapter.stream(turn)), "context.compacted")
        return compacted

    def test_recorded_small_chat_keeps_both_figures_and_says_nothing_dropped(
        self,
    ) -> None:
        label = conversation_routes._compaction_label
        expected = {21143: "21k", 22492: "22k"}
        for trigger, before, after, _next in CODEX_LIVE_COMPACTIONS[:2]:
            with self.subTest(before=before):
                compacted = self.manual(before, after)
                # The stored event is what the harness stated, unchanged.
                self.assertEqual(compacted.payload["trigger"], trigger)
                self.assertEqual(compacted.payload["tokens_before"], before)
                self.assertEqual(compacted.payload["tokens_after"], after)
                self.assertGreater(after, before)
                self.assertEqual(
                    label(dict(compacted.payload), "codex"),
                    f"Context compacted · {expected[before]}, "
                    "nothing to drop · manual",
                )

    def test_recorded_automatic_compaction_still_shows_the_reduction(self) -> None:
        trigger, before, after, _next = CODEX_LIVE_COMPACTIONS[2]
        self.assertEqual(
            conversation_routes._compaction_label(
                {"trigger": trigger, "tokens_before": before,
                 "tokens_after": after}, "codex"),
            "Context compacted · 119k → 26k · automatic",
        )

    def test_the_rule_is_the_same_for_every_harness_and_for_equal_figures(
        self,
    ) -> None:
        label = conversation_routes._compaction_label
        grew = {"trigger": "manual", "tokens_before": 21143,
                "tokens_after": 24965}
        same = {"trigger": "auto", "tokens_before": 8000, "tokens_after": 8000}
        for harness in ("claude", "codex", "opencode", None):
            with self.subTest(harness=harness):
                self.assertEqual(
                    label(grew, harness),
                    "Context compacted · 21k, nothing to drop · manual")
                self.assertEqual(
                    label(same, harness),
                    "Context compacted · 8k, nothing to drop · automatic")
        # One token lower is a reduction, and is shown as one.
        self.assertEqual(
            label({"trigger": "manual", "tokens_before": 21143,
                   "tokens_after": 21142}, "codex"),
            "Context compacted · 21k → 21k · manual")
        # A single stated figure is worded as before.
        self.assertEqual(
            label({"trigger": "manual", "tokens_before": 21143}),
            "Context compacted · from 21k · manual")

    def test_the_meter_settles_on_the_next_requests_own_figure(self) -> None:
        read = conversation_compaction.meter_reading
        for trigger, before, after, following in CODEX_LIVE_COMPACTIONS:
            with self.subTest(before=before):
                last = {"sequence": 1, "event_type": "usage", "created_at": "t1",
                        "payload": {"scope": "request",
                                    "context_tokens": before - 5}}
                marker = {"sequence": 2, "event_type": "context.compacted",
                          "created_at": "t2",
                          "payload": {"trigger": trigger,
                                      "tokens_before": before,
                                      "tokens_after": after}}
                request = {"sequence": 3, "event_type": "usage",
                           "created_at": "t3",
                           "payload": {"scope": "request",
                                       "context_tokens": following}}
                # Until a request reports, the harness's own figure stands.
                self.assertEqual(read([marker, last])["context_tokens"], after)
                settled = read([request, marker, last])
                self.assertEqual(settled["context_tokens"], following)
                self.assertEqual(settled["sequence"], 3)
                self.assertLess(following, after)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_page_marker_and_meter_for_a_compaction_that_freed_nothing() -> None:
    script = JS_PRELUDE + HELPERS + r"""
const live = %s;
const out = {labels: [], meter: [], claude: null, equal: null};
for (const [trigger, before, after, following] of live) {
  const payload = {trigger, tokens_before: before, tokens_after: after};
  out.labels.push(chatCompactionLabel(payload, "codex"));
  const context = {context_tokens: before - 5, context_window: 258400};
  chatApplyContextEvent(context,
    {event_type: "context.compacted", payload, created_at: "t2"});
  const atMarker = context.context_tokens;
  chatApplyContextEvent(context, {event_type: "usage", created_at: "t3",
    payload: {scope: "request", context_tokens: following}});
  out.meter.push([atMarker, context.context_tokens]);
}
out.claude = chatCompactionLabel(
  {trigger: "manual", tokens_before: 21143, tokens_after: 24965}, "claude");
out.equal = chatCompactionLabel(
  {trigger: "auto", tokens_before: 8000, tokens_after: 8000});
console.log(JSON.stringify(out));
""" % json.dumps(CODEX_LIVE_COMPACTIONS)
    result = run_js(script)
    label = conversation_routes._compaction_label
    assert result["labels"] == [
        "Context compacted · 21k, nothing to drop · manual",
        "Context compacted · 22k, nothing to drop · manual",
        "Context compacted · 119k → 26k · automatic",
    ]
    # The page and the server word every recorded marker the same way.
    assert result["labels"] == [
        label({"trigger": trigger, "tokens_before": before,
               "tokens_after": after}, "codex")
        for trigger, before, after, _next in CODEX_LIVE_COMPACTIONS
    ]
    assert result["claude"] == "Context compacted · 21k, nothing to drop · manual"
    assert result["equal"] == "Context compacted · 8k, nothing to drop · automatic"
    # The live meter ends on the next request's own figure each time.
    assert result["meter"] == [
        [after, following]
        for _trigger, _before, after, following in CODEX_LIVE_COMPACTIONS
    ]


class OpenCodeManualCompactTest(AdapterCase):
    """OpenCode: ``POST /session/{id}/summarize`` on the engine-owned server."""

    def adapter(self, server: FakeOpenCodeServer) -> OpenCodeAdapter:
        return OpenCodeAdapter(
            transport=server, shell_runtime_dir=self.root / "runtime-shells")

    def context(self, **fields: Any) -> ConversationContext:
        return super().context(provider="opencode", model="glm-test", **fields)

    def test_request_names_the_session_and_the_chats_model(self) -> None:
        server = FakeOpenCodeServer(OPENCODE_MANUAL_SEQUENCE)
        adapter = self.adapter(server)
        turn = adapter.compact(
            OPENCODE_SESSION, self.context(context_tokens=13657))
        events = list(adapter.stream(turn))
        posts = [request for request in server.requests if request[0] == "POST"]
        self.assertEqual(posts, [(
            "POST", f"/session/{OPENCODE_SESSION}/summarize",
            {"providerID": "opencode", "modelID": "glm-test"},
        )])
        [compacted] = of_type(events, "context.compacted")
        self.assertEqual(compacted.payload["trigger"], "manual")
        self.assertEqual(compacted.payload["tokens_before"], 13657)
        self.assertEqual(events[-1].type, "run.completed")
        # The summary is never passed on. The summarising request's tokens
        # are, as usage with no context size: they are a cost, not a reading.
        self.assertEqual(of_type(events, "assistant.delta"), [])
        [usage] = of_type(events, "usage")
        self.assertEqual(usage.payload["tokens"]["input"], 13657)
        self.assertNotIn("context_tokens", usage.payload)

    def test_a_summarise_request_that_never_answers_is_bounded(self) -> None:
        release = threading.Event()

        class HungServer(FakeOpenCodeServer):
            def request(self, method, path, *, query=None, body=None):
                if method == "POST" and path.endswith("/summarize"):
                    self.requests.append((method, path, body))
                    release.wait(10)
                    raise AdapterError("HARNESS_UNAVAILABLE", "aborted")
                if method == "POST" and path.endswith("/abort"):
                    release.set()
                return super().request(method, path, query=query, body=body)

            def stream(self, path, *, query=None):
                class Silent:
                    def __iter__(self):
                        return self

                    def __next__(self):
                        release.wait(10)
                        raise StopIteration

                    def close(self):
                        release.set()

                return Silent()

        server = HungServer([])
        adapter = self.adapter(server)
        turn = adapter.compact(OPENCODE_SESSION, self.context())
        started = time.monotonic()
        with mock.patch.object(opencode_adapter, "COMPACT_REQUEST_SECONDS", 0.2):
            with self.assertRaises(AdapterError) as raised:
                list(adapter.stream(turn))
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(raised.exception.code, "HARNESS_COMPACT_TIMEOUT")
        self.assertIn("within 0.2 seconds", raised.exception.detail)
        # The hung request was abandoned.
        self.assertIn(
            ("POST", f"/session/{OPENCODE_SESSION}/abort", None), server.requests)

    def test_the_wait_for_idle_after_the_answer_is_bounded(self) -> None:
        done = threading.Event()

        class NeverIdle(FakeOpenCodeServer):
            def stream(self, path, *, query=None):
                events = iter(self.events)

                class Open:
                    def __iter__(self):
                        return self

                    def __next__(self):
                        try:
                            return next(events)
                        except StopIteration:
                            done.wait(10)
                            raise

                    def close(self):
                        done.set()

                return Open()

        # Busy, compacted, and then no idle at all.
        server = NeverIdle(OPENCODE_MANUAL_SEQUENCE[:4])
        adapter = self.adapter(server)
        turn = adapter.compact(OPENCODE_SESSION, self.context())
        started = time.monotonic()
        with mock.patch.object(opencode_adapter, "COMPACT_SETTLE_SECONDS", 0.2):
            events = list(adapter.stream(turn))
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(len(of_type(events, "context.compacted")), 1)
        self.assertEqual(events[-1].type, "run.completed")
        self.assertEqual(events[-1].payload["compaction_wait"], "elapsed")

    def test_size_before_is_the_chats_reading_never_the_summarisers(self) -> None:
        # The summarising request reports its own, much smaller, input side.
        summariser = opencode_event(
            "message.updated",
            info={"id": "msg-summary", "role": "assistant", "summary": True,
                  "sessionID": OPENCODE_SESSION,
                  "tokens": {"total": 520, "input": 449, "output": 71,
                             "reasoning": 0, "cache": {"read": 0, "write": 0}}},
        )
        events = [OPENCODE_MANUAL_SEQUENCE[0], summariser,
                  *OPENCODE_MANUAL_SEQUENCE[3:]]
        adapter = self.adapter(FakeOpenCodeServer(events))
        recorded = list(adapter.stream(adapter.compact(
            OPENCODE_SESSION, self.context(context_tokens=13657))))
        [compacted] = of_type(recorded, "context.compacted")
        self.assertEqual(compacted.payload["tokens_before"], 13657)
        # With no recorded reading the figure is left out, not borrowed.
        adapter = self.adapter(FakeOpenCodeServer(events))
        recorded = list(adapter.stream(adapter.compact(
            OPENCODE_SESSION, self.context())))
        [compacted] = of_type(recorded, "context.compacted")
        self.assertNotIn("tokens_before", compacted.payload)

    def test_idle_without_a_compaction_signal_completes_unreported(self) -> None:
        server = FakeOpenCodeServer([
            opencode_event("session.status", status={"type": "busy"}),
            opencode_event("session.idle"),
        ])
        adapter = self.adapter(server)
        events = list(adapter.stream(adapter.compact(OPENCODE_SESSION, self.context())))
        self.assertEqual(of_type(events, "context.compacted"), [])
        self.assertEqual(events[-1].type, "run.completed")

    def test_a_refused_request_fails_the_run(self) -> None:
        server = FakeOpenCodeServer([], refuse=True)
        adapter = self.adapter(server)
        turn = adapter.compact(OPENCODE_SESSION, self.context())
        with self.assertRaises(AdapterError) as raised:
            list(adapter.stream(turn))
        self.assertIn("Session not found", raised.exception.detail)

    # Stored messages as ``GET /session/{id}/message`` returns them (1.19.5):
    # a reply names ``providerID`` / ``modelID``; an operator message names
    # ``model``; the compaction agent's own reply is marked ``summary``.
    USER = {"info": {"role": "user", "model": {
        "providerID": "ollama-cloud", "modelID": "glm-user"}}, "parts": []}
    REPLY = {"info": {"role": "assistant", "providerID": "opencode",
                      "modelID": "glm-reply", "mode": "build"}, "parts": []}
    SUMMARY = {"info": {"role": "assistant", "providerID": "opencode",
                        "modelID": "compaction-model", "summary": True,
                        "mode": "compaction"}, "parts": []}

    def default_route_compact(self, messages):
        """A chat on a harness-default route: the context names no model."""
        server = FakeOpenCodeServer(OPENCODE_MANUAL_SEQUENCE, messages=messages)
        adapter = self.adapter(server)
        turn = adapter.compact(OPENCODE_SESSION, AdapterCase.context(self))
        list(adapter.stream(turn))
        return [request[2] for request in server.requests
                if request[1].endswith("/summarize")]

    def test_a_harness_default_chat_uses_the_model_of_its_last_reply(self):
        self.assertEqual(
            self.default_route_compact([self.USER, self.REPLY, self.USER]),
            [{"providerID": "opencode", "modelID": "glm-reply"}],
        )

    def test_the_compaction_agents_own_reply_is_not_the_chats_model(self):
        self.assertEqual(
            self.default_route_compact([self.USER, self.REPLY, self.SUMMARY]),
            [{"providerID": "opencode", "modelID": "glm-reply"}],
        )

    def test_with_no_reply_the_last_operator_messages_model_is_used(self):
        self.assertEqual(
            self.default_route_compact([self.USER, self.SUMMARY]),
            [{"providerID": "ollama-cloud", "modelID": "glm-user"}],
        )

    def test_a_named_route_is_used_without_asking_the_session(self) -> None:
        server = FakeOpenCodeServer(
            OPENCODE_MANUAL_SEQUENCE, messages=[self.REPLY])
        adapter = self.adapter(server)
        list(adapter.stream(adapter.compact(OPENCODE_SESSION, self.context())))
        self.assertNotIn(
            ("GET", f"/session/{OPENCODE_SESSION}/message", None), server.requests)
        self.assertIn(
            ("POST", f"/session/{OPENCODE_SESSION}/summarize",
             {"providerID": "opencode", "modelID": "glm-test"}),
            server.requests,
        )

    def test_a_chat_whose_model_cannot_be_determined_says_why(self) -> None:
        for messages in ([], [{"info": {"role": "assistant"}}, {"parts": []}]):
            with self.subTest(messages=messages):
                server = FakeOpenCodeServer([], messages=messages)
                adapter = self.adapter(server)
                with self.assertRaises(AdapterError) as raised:
                    adapter.compact(OPENCODE_SESSION, AdapterCase.context(self))
                self.assertEqual(
                    raised.exception.code, "HARNESS_COMPACT_MODEL_UNKNOWN")
                self.assertEqual(
                    raised.exception.detail,
                    "OpenCode needs a provider and model to summarise a chat; "
                    "this chat names none and its session records none to use",
                )
                # Nothing was asked of the model.
                self.assertEqual(
                    [r for r in server.requests if r[0] == "POST"], [])


class OpenCodeAutomaticCompactionTest(AdapterCase):
    """An automatic OpenCode compaction: its size before is the chat's last
    request before the summarising one, never the summarising request."""

    REQUEST = opencode_event(
        "message.updated",
        info={"id": "msg-reply", "role": "assistant", "mode": "build",
              "sessionID": OPENCODE_SESSION,
              "tokens": {"total": 80120, "input": 2000, "output": 120,
                         "reasoning": 0, "cache": {"read": 77000, "write": 1000}}},
    )
    SUMMARISER = opencode_event(
        "message.updated",
        info={"id": "msg-summary", "role": "assistant", "summary": True,
              "mode": "compaction", "sessionID": OPENCODE_SESSION,
              "tokens": {"total": 520, "input": 449, "output": 71,
                         "reasoning": 0, "cache": {"read": 0, "write": 0}}},
    )

    def turn(self, events, **context) -> list[NormalizedEvent]:
        adapter = OpenCodeAdapter(
            transport=FakeOpenCodeServer(events),
            shell_runtime_dir=self.root / "runtime-shells")
        native = adapter.resume(
            OPENCODE_SESSION,
            self.context(provider="opencode", model="glm-test", **context),
            "carry on",
        )
        return list(adapter.stream(native))

    def tail(self) -> list[dict[str, Any]]:
        return [self.SUMMARISER, opencode_event("session.compacted"),
                opencode_event("session.idle")]

    def test_before_is_the_request_seen_before_the_summarising_one(self) -> None:
        events = self.turn([
            opencode_event("session.status", status={"type": "busy"}),
            self.REQUEST, *self.tail(),
        ], context_tokens=60000)
        [compacted] = of_type(events, "context.compacted")
        self.assertEqual(compacted.payload["tokens_before"], 80000)
        self.assertEqual(compacted.payload["trigger"], "unknown")
        self.assertNotIn("tokens_after", compacted.payload)
        # The summarising request is recorded as usage, but never as a
        # reading of the chat's context.
        readings = [event.payload.get("context_tokens")
                    for event in of_type(events, "usage")]
        self.assertEqual(readings, [80000, None])

    def test_with_no_request_this_turn_before_is_the_chats_last_reading(self):
        events = self.turn([
            opencode_event("session.status", status={"type": "busy"}),
            *self.tail(),
        ], context_tokens=70000)
        [compacted] = of_type(events, "context.compacted")
        self.assertEqual(compacted.payload["tokens_before"], 70000)

    @staticmethod
    def text_part(message: str, part: str, text: str) -> list[dict[str, Any]]:
        """One text part as the bus streams it: opened, deltas, then whole."""
        def updated(value: str) -> dict[str, Any]:
            return opencode_event("message.part.updated", part={
                "id": part, "messageID": message, "type": "text",
                "sessionID": OPENCODE_SESSION, "text": value})
        half = len(text) // 2
        return [
            updated(""),
            opencode_event("message.part.delta", messageID=message,
                           partID=part, field="text", delta=text[:half]),
            opencode_event("message.part.delta", messageID=message,
                           partID=part, field="text", delta=text[half:]),
            updated(text),
        ]

    def summary_stream(self, marker: dict[str, Any]) -> list[NormalizedEvent]:
        announced = opencode_event("message.updated", info={
            "id": "msg-summary", "role": "assistant", "agent": "compaction",
            "sessionID": OPENCODE_SESSION, **marker})
        later = opencode_event("message.updated", info={
            "id": "msg-later", "role": "assistant", "mode": "build",
            "sessionID": OPENCODE_SESSION})
        return self.turn([
            opencode_event("session.status", status={"type": "busy"}),
            self.REQUEST,
            *self.text_part("msg-reply", "part-reply", "Here is the answer."),
            # The summary message is announced before any of its text.
            announced,
            *self.text_part("msg-summary", "part-summary", "PRIVATE SUMMARY TEXT"),
            self.SUMMARISER,
            opencode_event("session.compacted"),
            later,
            *self.text_part("msg-later", "part-later", " And the rest."),
            opencode_event("session.idle"),
        ])

    def test_summary_text_is_never_passed_on_and_replies_are_unaffected(self):
        markers = (
            {"summary": True, "mode": "compaction"},   # as 1.19.5 sends it
            {"summary": True},
            {"mode": "compaction"},
        )
        for marker in markers:
            with self.subTest(marker=marker):
                events = self.summary_stream(marker)
                text = "".join(
                    event.payload["text"]
                    for event in of_type(events, "assistant.delta"))
                # The reply before the compaction and the one after it both
                # stream in full; the summary between them does not.
                self.assertEqual(text, "Here is the answer. And the rest.")
                self.assertNotIn("PRIVATE SUMMARY TEXT", json.dumps(
                    [dict(event.payload) for event in events]))
                # The marker is what the operator sees instead.
                [compacted] = of_type(events, "context.compacted")
                self.assertEqual(compacted.payload["tokens_before"], 80000)
                self.assertEqual(events[-1].type, "run.completed")

    def test_an_operator_messages_summary_object_hides_nothing(self) -> None:
        # OpenCode also writes ``summary: {"diffs": []}`` on user messages.
        events = self.turn([
            opencode_event("session.status", status={"type": "busy"}),
            opencode_event("message.updated", info={
                "id": "msg-user", "role": "user", "summary": {"diffs": []},
                "sessionID": OPENCODE_SESSION}),
            opencode_event("message.updated", info={
                "id": "msg-reply", "role": "assistant", "mode": "build",
                "sessionID": OPENCODE_SESSION}),
            *self.text_part("msg-reply", "part-reply", "An ordinary reply."),
            opencode_event("session.idle"),
        ])
        self.assertEqual(
            "".join(event.payload["text"]
                    for event in of_type(events, "assistant.delta")),
            "An ordinary reply.",
        )
        self.assertEqual(of_type(events, "context.compacted"), [])

    def test_with_no_reading_at_all_before_is_left_out(self) -> None:
        events = self.turn([
            opencode_event("session.status", status={"type": "busy"}),
            *self.tail(),
        ])
        [compacted] = of_type(events, "context.compacted")
        self.assertNotIn("tokens_before", compacted.payload)


class TriggerAvailabilityTest(unittest.TestCase):
    """AC15: no button for a harness without a confirmed trigger."""

    def test_only_harnesses_with_a_confirmed_trigger_declare_one(self) -> None:
        declared = {
            harness: adapter.manual_compaction
            for harness, adapter in ADAPTER_TYPES.items()
        }
        self.assertEqual(declared, {
            "claude": True, "codex": True, "opencode": True, "kimi": False,
        })

    def test_a_harness_without_a_trigger_refuses_the_request(self) -> None:
        with self.assertRaises(AdapterError) as raised:
            ConversationAdapter.compact(
                mock.Mock(harness="kimi"), "session", mock.Mock())
        self.assertEqual(raised.exception.code, "HARNESS_COMPACT_UNSUPPORTED")
        self.assertIs(KimiAdapter.compact, ConversationAdapter.compact)


# ── AC14: the broker records the request and exactly one result ─────────────


class CompactingAdapter(RecordingAdapter):
    """A harness whose manual compaction ends the way the test says."""

    def __init__(self, *, result: str = "compacted", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.result = result
        self.compactions: list[str] = []

    def compact(self, session_ref, context):
        self.compactions.append(session_ref)
        if self.result == "refused":
            raise AdapterError("HARNESS_COMPACT_REFUSED", "thread is busy")
        return NativeTurn(
            harness=self.harness,
            session_ref=session_ref,
            run_ref=f"compact-{len(self.compactions)}",
            worktree=context.checked_worktree(),
            metadata={"compaction": True},
        )

    def stream(self, turn):
        if not turn.metadata.get("compaction"):
            yield from super().stream(turn)
            return
        yield NormalizedEvent("session.started", {"session_ref": turn.session_ref})
        yield NormalizedEvent("run.started", {"status": "running"})
        # What a harness streams while it summarises. None of it is kept.
        yield NormalizedEvent("assistant.delta", {"text": "PRIVATE SUMMARY TEXT"})
        yield usage_event({"inputTokens": 90000, "outputTokens": 700}, "fixture",
                          scope="request", context_tokens=90000)
        if self.result == "renamed":
            # The harness names the turn only after dispatch (Codex).
            turn.run_ref = "turn-adopted"
            yield NormalizedEvent("run.started", {"status": "running"})
        if self.result == "stopped":
            yield NormalizedEvent(
                "run.interrupted", {"status": "cancelled"},
                interrupt_evidence="operator")
            return
        if self.result == "compacted":
            # The harness states no trigger; the engine asked.
            yield compaction_event(trigger="unknown", tokens_before=90000,
                                   tokens_after=12000, native_type="fixture")
        if self.result == "failed":
            yield NormalizedEvent("run.failed", {
                "status": "failed", "error": "HARNESS_COMPACT_REFUSED",
                "detail": "No messages to compact"})
            return
        yield NormalizedEvent("run.completed", {"status": "completed"})


class ManualCompactBrokerTest(DispatchCase):
    def press(self, conversation_id: str) -> int:
        """The rows the compact route writes for one press."""
        self.serial += 1
        key = f"compact-{self.serial}"
        con = self.connect()
        con.execute(
            "UPDATE conversations SET state='queued' WHERE conversation_id=?",
            (conversation_id,),
        )
        message_id = int(con.execute(
            "INSERT INTO conversation_messages "
            "(conversation_id,sender_kind,sender_ref,message_kind,body,"
            "idempotency_key,request_hash,state) "
            "VALUES (?,'engine',?,'control',?,?,?,'queued')",
            (conversation_id, conversation_compaction.MANUAL_COMPACT_SENDER,
             conversation_compaction.MANUAL_COMPACT_BODY, key, f"hash-{key}"),
        ).lastrowid)
        con.execute(
            "INSERT INTO conversation_outbox (conversation_id,message_id) "
            "VALUES (?,?)",
            (conversation_id, message_id),
        )
        sequence = con.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM conversation_events "
            "WHERE conversation_id=?", (conversation_id,),
        ).fetchone()[0]
        con.execute(
            "INSERT INTO conversation_events "
            "(conversation_id,sequence,event_type,payload,message_id) "
            "VALUES (?,?,'compaction.requested',?,?)",
            (conversation_id, sequence,
             json.dumps({"message_id": message_id, "initiated_by": "engine"}),
             message_id),
        )
        con.commit()
        con.close()
        return message_id

    def settle(self, broker, message_id: int) -> str:
        broker.notify()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            con = self.connect()
            row = con.execute(
                "SELECT state FROM conversation_runs WHERE trigger_message_id=?",
                (message_id,),
            ).fetchone()
            con.close()
            if row is not None and row[0] in (
                "succeeded", "failed", "cancelled", "unknown"
            ):
                self.assertTrue(broker.wait_idle())
                return row[0]
            time.sleep(0.01)
        self.fail(f"compaction {message_id} did not finish")

    def events(self, conversation_id: str, after: int = 0) -> list[dict]:
        con = self.connect()
        rows = con.execute(
            "SELECT sequence,event_type,payload,message_id,run_id "
            "FROM conversation_events WHERE conversation_id=? AND sequence>? "
            "ORDER BY sequence",
            (conversation_id, after),
        ).fetchall()
        con.close()
        return [
            {"sequence": row[0], "type": row[1], "payload": json.loads(row[2]),
             "message_id": row[3], "run_id": row[4]}
            for row in rows
        ]

    def state(self, conversation_id: str) -> str:
        con = self.connect()
        state = con.execute(
            "SELECT state FROM conversations WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()[0]
        con.close()
        return state

    def pressed(self, result: str):
        adapter = CompactingAdapter(result=result)
        broker = self.start_broker(lambda _harness: adapter)
        conversation_id = self.add_conversation()
        self.deliver(broker, conversation_id, "start the work")
        before = self.events(conversation_id)[-1]["sequence"]
        message_id = self.press(conversation_id)
        outcome = self.settle(broker, message_id)
        return adapter, broker, conversation_id, message_id, outcome, before

    def results(self, conversation_id: str, after: int) -> list[dict]:
        return [
            event for event in self.events(conversation_id, after)
            if event["type"] in (
                "context.compacted", "compaction.failed", "compaction.not_reported"
            )
        ]

    def test_one_press_is_one_request_and_one_manual_marker(self) -> None:
        adapter, _broker, conversation_id, message_id, outcome, before = (
            self.pressed("compacted")
        )
        self.assertEqual(outcome, "succeeded")
        # One request reached the harness, as a compaction: no message sent.
        self.assertEqual(adapter.compactions, ["native-session"])
        self.assertEqual(adapter.dispatched, ["start the work"])
        [marker] = self.results(conversation_id, before)
        self.assertEqual(marker["type"], "context.compacted")
        self.assertEqual(marker["payload"]["trigger"], "manual")
        self.assertEqual(marker["payload"]["tokens_before"], 90000)
        self.assertEqual(marker["payload"]["tokens_after"], 12000)
        self.assertEqual(marker["payload"]["request_message_id"], message_id)
        # The marker is on the chat, not on the request's run: the transcript
        # lists operator turns and the marker has to show without one.
        self.assertIsNone(marker["message_id"])
        self.assertIsNone(marker["run_id"])
        self.assertEqual(self.state(conversation_id), "idle")

    def test_no_operator_message_and_no_summary_are_stored(self) -> None:
        _adapter, _broker, conversation_id, message_id, _outcome, before = (
            self.pressed("compacted")
        )
        con = self.connect()
        operator = con.execute(
            "SELECT COUNT(*) FROM conversation_messages "
            "WHERE conversation_id=? AND sender_kind='user'",
            (conversation_id,),
        ).fetchone()[0]
        request = con.execute(
            "SELECT sender_kind,sender_ref,message_kind,state "
            "FROM conversation_messages WHERE message_id=?",
            (message_id,),
        ).fetchone()
        con.close()
        self.assertEqual(operator, 1)   # "start the work", and nothing else
        self.assertEqual(
            tuple(request), ("engine", "compact-button", "control", "completed"))
        recorded = self.events(conversation_id, before)
        self.assertEqual(recorded[0]["type"], "compaction.requested")
        self.assertEqual(recorded[0]["payload"]["initiated_by"], "engine")
        kinds = {event["type"] for event in recorded}
        self.assertNotIn("assistant.delta", kinds)
        self.assertNotIn("message.reground", kinds)
        self.assertNotIn("PRIVATE SUMMARY TEXT", json.dumps(recorded))

    def test_the_requests_tokens_are_recorded_but_never_as_the_context(self):
        _adapter, broker, conversation_id, message_id, _outcome, before = (
            self.pressed("compacted")
        )
        [usage] = [event for event in self.events(conversation_id, before)
                   if event["type"] == "usage"]
        self.assertEqual(
            usage["payload"]["tokens"], {"inputTokens": 90000, "outputTokens": 700})
        self.assertEqual(usage["payload"]["scope"], "compaction")
        self.assertNotIn("context_tokens", usage["payload"])
        # On the request's own run, not on the chat.
        self.assertEqual(usage["message_id"], message_id)
        self.assertIsNotNone(usage["run_id"])
        # The meter reads what the compaction left, not the summarising
        # request's 90k input side.
        state = broker.store.compaction_state(conversation_id)
        self.assertEqual(state.context_tokens, 12000)

    def test_a_request_that_spent_nothing_records_no_usage(self) -> None:
        self.assertIsNone(conversation_compaction.compaction_usage_payload(
            {"tokens": {"input_tokens": 0, "output_tokens": 0}, "scope": "turn"}))
        self.assertIsNone(conversation_compaction.compaction_usage_payload({}))

    def test_a_press_does_not_spend_a_reground_line_already_owed(self) -> None:
        # The first turn compacted on its own, so a line is owed. The press
        # must not take it: the next real message still carries it.
        adapter = CompactingAdapter(result="silent", compact_on={1})
        broker = self.start_broker(lambda _harness: adapter)
        conversation_id = self.add_conversation()
        self.deliver(broker, conversation_id, "start the work")
        self.assertIsNotNone(
            broker.store.compaction_state(conversation_id).pending_compaction)
        self.settle(broker, self.press(conversation_id))
        self.assertEqual(self.reground_events(conversation_id), [])
        self.assertIsNotNone(
            broker.store.compaction_state(conversation_id).pending_compaction)
        following = self.deliver(broker, conversation_id, "carry on")
        self.assertEqual(adapter.dispatched[-1], f"carry on\n\n{LINE}")
        [(reground_message, _payload)] = self.reground_events(conversation_id)
        self.assertEqual(reground_message, following)

    def test_a_stopped_request_is_the_stopped_line_not_the_notice(self) -> None:
        _adapter, _broker, conversation_id, message_id, outcome, before = (
            self.pressed("stopped")
        )
        self.assertEqual(outcome, "cancelled")
        [stopped] = self.results(conversation_id, before)
        self.assertEqual(stopped["type"], "compaction.failed")
        self.assertEqual(stopped["payload"]["outcome"], "cancelled")
        self.assertEqual(stopped["payload"]["request_message_id"], message_id)
        self.assertEqual(
            conversation_routes._compaction_failed_label(stopped["payload"]),
            "Compaction stopped — the context was not compacted",
        )
        self.assertEqual(self.state(conversation_id), "idle")

    def test_a_turn_named_after_dispatch_is_what_the_run_row_names(self) -> None:
        _adapter, _broker, _conversation_id, message_id, outcome, _before = (
            self.pressed("renamed")
        )
        self.assertEqual(outcome, "succeeded")
        con = self.connect()
        runner_ref = con.execute(
            "SELECT runner_ref FROM conversation_runs WHERE trigger_message_id=?",
            (message_id,),
        ).fetchone()[0]
        con.close()
        self.assertEqual(runner_ref, "turn-adopted")

    def test_the_first_real_message_after_it_carries_the_reground_line(self):
        adapter, broker, conversation_id, _message_id, _outcome, _before = (
            self.pressed("compacted")
        )
        following = self.deliver(broker, conversation_id, "carry on")
        after = self.deliver(broker, conversation_id, "and again")
        self.assertEqual(
            adapter.dispatched,
            ["start the work", f"carry on\n\n{LINE}", "and again"],
        )
        [(reground_message, _payload)] = self.reground_events(conversation_id)
        self.assertEqual(reground_message, following)
        self.assertNotEqual(reground_message, after)

    def test_a_failed_request_is_an_error_line_and_no_marker(self) -> None:
        _adapter, _broker, conversation_id, message_id, outcome, before = (
            self.pressed("failed")
        )
        self.assertEqual(outcome, "failed")
        [failed] = self.results(conversation_id, before)
        self.assertEqual(failed["type"], "compaction.failed")
        self.assertEqual(failed["payload"]["detail"], "No messages to compact")
        self.assertEqual(failed["payload"]["request_message_id"], message_id)
        self.assertIsNone(failed["run_id"])
        # The session is as it was: the chat is not left in error.
        self.assertEqual(self.state(conversation_id), "idle")

    def test_a_refusal_before_the_stream_is_an_error_line_too(self) -> None:
        _adapter, _broker, conversation_id, _message_id, outcome, before = (
            self.pressed("refused")
        )
        self.assertNotEqual(outcome, "succeeded")
        [failed] = self.results(conversation_id, before)
        self.assertEqual(failed["type"], "compaction.failed")
        self.assertEqual(failed["payload"]["error"], "HARNESS_COMPACT_REFUSED")
        self.assertEqual(failed["payload"]["detail"], "thread is busy")
        self.assertEqual(self.state(conversation_id), "idle")

    def test_a_run_with_no_compaction_signal_is_the_not_reported_notice(self):
        _adapter, _broker, conversation_id, message_id, outcome, before = (
            self.pressed("silent")
        )
        self.assertEqual(outcome, "succeeded")
        [notice] = self.results(conversation_id, before)
        self.assertEqual(notice["type"], "compaction.not_reported")
        # The outcome names its request's cause (spec #275).
        self.assertEqual(
            notice["payload"],
            {"request_message_id": message_id, "cause": "operator"},
        )
        self.assertEqual(self.state(conversation_id), "idle")

    def test_a_failed_request_owes_no_reground_line(self) -> None:
        adapter, broker, conversation_id, _message_id, _outcome, _before = (
            self.pressed("failed")
        )
        self.deliver(broker, conversation_id, "carry on")
        self.assertEqual(adapter.dispatched, ["start the work", "carry on"])

    def test_a_chat_with_no_session_is_refused_without_reaching_the_harness(self):
        adapter = CompactingAdapter()
        broker = self.start_broker(lambda _harness: adapter)
        conversation_id = self.add_conversation()
        message_id = self.press(conversation_id)
        outcome = self.settle(broker, message_id)
        self.assertEqual(outcome, "failed")
        self.assertEqual(adapter.compactions, [])
        [failed] = self.results(conversation_id, 0)
        self.assertEqual(failed["payload"]["error"], "COMPACTION_NO_SESSION")

    def test_a_message_sent_meanwhile_waits_for_the_compaction(self) -> None:
        adapter = CompactingAdapter()
        broker = self.start_broker(lambda _harness: adapter)
        conversation_id = self.add_conversation()
        self.deliver(broker, conversation_id, "start the work")
        request = self.press(conversation_id)
        message = self.send(conversation_id, "sent while compacting")
        self.settle(broker, request)
        self.settle(broker, message)
        con = self.connect()
        order = [row[0] for row in con.execute(
            "SELECT trigger_message_id FROM conversation_runs "
            "WHERE conversation_id=? ORDER BY run_id", (conversation_id,),
        ).fetchall()]
        con.close()
        self.assertEqual(order[-2:], [request, message])
        self.assertEqual(
            adapter.dispatched[-1], f"sent while compacting\n\n{LINE}")


class CancelledRequestTest(DispatchCase):
    """A request cancelled without the broker finishing it still ends in a
    line: the reaper, and a chat closed or displaced with the request queued."""

    press = ManualCompactBrokerTest.press
    events = ManualCompactBrokerTest.events
    results = ManualCompactBrokerTest.results

    def running_request(
        self, *, ordinary: bool = False, harness: str = "codex",
        session_ref: str = "native-session",
    ) -> tuple[str, int, int]:
        """A manual compaction (or an ordinary turn) a dead broker left running."""
        conversation_id = self.add_conversation(
            state="running", session_ref=session_ref, harness=harness)
        message_id = (
            self.send(conversation_id, "hello") if ordinary
            else self.press(conversation_id)
        )
        con = self.connect()
        con.execute(
            "UPDATE conversations SET state='running' WHERE conversation_id=?",
            (conversation_id,),
        )
        con.execute(
            "UPDATE conversation_messages SET state='running' WHERE message_id=?",
            (message_id,),
        )
        run_id = int(con.execute(
            "INSERT INTO conversation_runs "
            "(conversation_id,shell_id,trigger_message_id,state,lease_owner,"
            "lease_expires_at,started_at,heartbeat_at,process_pid,"
            "process_start_ticks,process_group_id) "
            "VALUES (?,1,?,'running','dead-broker','2000-01-01 00:00:00',"
            "datetime('now'),datetime('now'),4242,9001,4242)",
            (conversation_id, message_id),
        ).lastrowid)
        con.execute(
            "UPDATE conversation_outbox SET state='claimed',"
            "claim_owner='dead-broker',claimed_at=datetime('now'),"
            "lease_expires_at='2000-01-01 00:00:00',attempts=attempts+1 "
            "WHERE message_id=?",
            (message_id,),
        )
        con.execute(
            "UPDATE conversation_outbox SET state='dispatched',run_id=?,"
            "dispatched_at=datetime('now') WHERE message_id=?",
            (run_id, message_id),
        )
        con.commit()
        con.close()
        return conversation_id, message_id, run_id

    def reap(self, conversation_id: str, message_id: int, run_id: int) -> bool:
        candidate = conversation_reaper.ReaperCandidate(
            run_id=run_id, conversation_id=conversation_id,
            message_id=message_id, pid=4242, start_ticks=9001,
            process_group_id=4242, started_at=datetime.now(timezone.utc),
            last_signal=None, signaled_at=None,
        )
        return conversation_reaper.ReaperStore(self.db_path).finish_interrupted(
            candidate, "orphaned after broker death", process_exited=True)

    def test_a_reaped_request_ends_in_the_stopped_line(self) -> None:
        conversation_id, message_id, run_id = self.running_request()
        self.assertTrue(self.reap(conversation_id, message_id, run_id))
        [stopped] = self.results(conversation_id, 0)
        self.assertEqual(stopped["type"], "compaction.failed")
        self.assertEqual(stopped["payload"], {
            "outcome": "cancelled", "error": "CONVERSATION_RUN_REAPED",
            "detail": "orphaned after broker death",
            "request_message_id": message_id, "cause": "operator",
        })
        # On the chat, like every result.
        self.assertIsNone(stopped["run_id"])
        self.assertIsNone(stopped["message_id"])

    def test_a_reaped_request_that_already_compacted_keeps_its_marker(self):
        conversation_id, message_id, run_id = self.running_request()
        BrokerStore(self.db_path).append_event(run_id, compaction_event(
            trigger="unknown", tokens_before=90000, tokens_after=12000,
            native_type="fixture"))
        self.assertTrue(self.reap(conversation_id, message_id, run_id))
        [marker] = self.results(conversation_id, 0)
        self.assertEqual(marker["type"], "context.compacted")

    def test_a_reaped_ordinary_turn_writes_no_compaction_line(self) -> None:
        conversation_id, message_id, run_id = self.running_request(ordinary=True)
        self.assertTrue(self.reap(conversation_id, message_id, run_id))
        self.assertEqual(self.results(conversation_id, 0), [])

    def test_a_request_queued_when_its_chat_is_displaced_ends_stopped(self):
        conversation_id = self.add_conversation(session_ref="native-session")
        message_id = self.press(conversation_id)
        con = self.connect()
        with con:
            closed = active_chat_registry.close_for_displacement(
                con, 1, allow_live_process=True)
        con.close()
        self.assertEqual(closed.chat_id, conversation_id)
        [stopped] = self.results(conversation_id, 0)
        self.assertEqual(stopped["type"], "compaction.failed")
        self.assertEqual(stopped["payload"], {
            "outcome": "cancelled", "error": "CONVERSATION_DISPLACED",
            "request_message_id": message_id, "cause": "operator",
        })


RECOVERED_SESSION = "77777777-7777-4777-8777-000000000002"


def stored(kind: str, **fields: Any) -> dict[str, Any]:
    return {"type": kind, "sessionId": RECOVERED_SESSION, **fields}


def stored_message(kind: str, content: Any) -> dict[str, Any]:
    return stored(kind, message={"role": kind, "content": content})


# What a ``/compact`` run appends to the session file (Claude 2.1.322), read
# from the scratch sessions the live checks left behind.
STORED_COMMAND = [
    stored_message("user", "<local-command-caveat>The command below was run "
                           "directly in Claude Code</local-command-caveat>"),
    stored_message("user", "<command-name>/compact</command-name>"),
]
STORED_BOUNDARY = stored(
    "system", subtype="compact_boundary",
    uuid="cccccccc-dddd-4eee-8fff-000000000001",
    content="Conversation compacted",
    compactMetadata={"trigger": "manual", "preTokens": 27888,
                     "postTokens": 7628, "durationMs": 4407},
)
STORED_COMPACTED = [
    STORED_BOUNDARY,
    stored_message("user", "This session is being continued from a previous "
                           "conversation. PRIVATE SUMMARY TEXT"),
    *STORED_COMMAND,
    stored_message("user", "<local-command-stdout>Compacted </local-command-stdout>"),
]
# A refusal is not an assistant message in the file: it is the command's own
# output, kept as a ``system`` entry.
STORED_REFUSED = [
    *STORED_COMMAND,
    stored("system", subtype="local_command", level="info", content=(
        "<local-command-stdout>Not enough messages to compact."
        "</local-command-stdout>")),
]
STORED_NOTHING_TO_COMPACT = stored(
    "system", subtype="local_command", level="info", content=(
        "<local-command-stderr>Error: No messages to compact"
        "</local-command-stderr>"))
# What an earlier run left before this one launched. None of it is this
# run's: an old boundary and an old refusal prove nothing about it.
STORED_EARLIER = [
    stored_message("user", "first"),
    stored_message("assistant", [{"type": "text", "text": "stale reply"}]),
    {**STORED_BOUNDARY, "uuid": "cccccccc-dddd-4eee-8fff-00000000000f"},
    STORED_NOTHING_TO_COMPACT,
    stored("cost-state"),
]


def stored_lines(rows: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(row) + "\n" for row in rows)


class RecoveredClaudeCompactionTest(DispatchCase):
    """A ``/compact`` run recovered after a broker restart, with its process
    gone: reconciled from what it appended to the session file."""

    press = ManualCompactBrokerTest.press
    events = ManualCompactBrokerTest.events
    results = ManualCompactBrokerTest.results
    state = ManualCompactBrokerTest.state
    running_request = CancelledRequestTest.running_request

    def recover(self, appended: list[dict[str, Any]], *, ordinary: bool = False):
        conversation_id, message_id, run_id = self.running_request(
            ordinary=ordinary, harness="claude", session_ref=RECOVERED_SESSION)
        adapter = ClaudeAdapter(config_dir=self.root / "claude-config")
        transcript = adapter._session_path(
            RECOVERED_SESSION, self.worktree.resolve())
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(stored_lines(STORED_EARLIER))
        offset = transcript.stat().st_size
        with transcript.open("a") as stream:
            stream.write(stored_lines(appended))
        con = self.connect()
        con.execute(
            "UPDATE conversation_runs SET harness_session_after=?,"
            "runner_ref='claude-run' WHERE run_id=?",
            (RECOVERED_SESSION, run_id),
        )
        con.commit()
        con.close()
        # A process identity nothing alive answers to: reconcile, not attach.
        self.name_live_process(
            conversation_id, run_id, pid=4194305, start_ticks=9001,
            transcript_path=str(transcript), transcript_offset=offset,
        )
        seen: dict = {}
        reconcile = adapter.reconcile

        def recording(turn, context):
            seen.update(turn.metadata)
            return reconcile(turn, context)

        adapter.reconcile = recording
        broker = self.start_broker(lambda _harness: adapter)
        deadline = time.monotonic() + 5
        outcome = None
        while time.monotonic() < deadline:
            con = self.connect()
            outcome = con.execute(
                "SELECT state FROM conversation_runs WHERE run_id=?", (run_id,),
            ).fetchone()[0]
            con.close()
            if outcome in ("succeeded", "failed", "cancelled", "unknown"):
                break
            time.sleep(0.01)
        self.assertTrue(broker.wait_idle())
        return conversation_id, message_id, outcome, seen

    def test_the_recovered_turn_is_still_a_compaction(self) -> None:
        _conversation_id, _message_id, _outcome, seen = self.recover(
            STORED_COMPACTED)
        self.assertIs(seen["compaction"], True)
        self.assertTrue(seen["recovered"])

    def test_an_ordinary_recovered_turn_carries_no_such_flag(self) -> None:
        _conversation_id, _message_id, outcome, seen = self.recover(
            [stored_message("user", "second"),
             stored_message("assistant", [{"type": "text", "text": "own reply"}])],
            ordinary=True)
        self.assertNotIn("compaction", seen)
        # And is still settled by its own reply, as before.
        self.assertEqual(outcome, "succeeded")

    def test_a_recovered_compaction_keeps_its_marker(self) -> None:
        conversation_id, message_id, outcome, _seen = self.recover(STORED_COMPACTED)
        self.assertEqual(outcome, "succeeded")
        [marker] = self.results(conversation_id, 0)
        self.assertEqual(marker["type"], "context.compacted")
        self.assertEqual(marker["payload"]["trigger"], "manual")
        self.assertEqual(marker["payload"]["tokens_before"], 27888)
        self.assertEqual(marker["payload"]["tokens_after"], 7628)
        self.assertEqual(marker["payload"]["request_message_id"], message_id)
        self.assertIsNone(marker["run_id"])
        self.assertEqual(self.state(conversation_id), "idle")
        self.assertNotIn(
            "PRIVATE SUMMARY TEXT", json.dumps(self.events(conversation_id)))

    def test_a_recovered_refusal_is_the_error_line_with_claudes_reason(self):
        conversation_id, message_id, outcome, _seen = self.recover(STORED_REFUSED)
        self.assertEqual(outcome, "failed")
        [failed] = self.results(conversation_id, 0)
        self.assertEqual(failed["type"], "compaction.failed")
        self.assertEqual(
            failed["payload"]["detail"], "Not enough messages to compact.")
        self.assertEqual(failed["payload"]["request_message_id"], message_id)
        self.assertEqual(
            conversation_routes._compaction_failed_label(failed["payload"]),
            "Compaction failed — Not enough messages to compact.",
        )
        self.assertEqual(self.state(conversation_id), "idle")

    def test_the_other_refusal_claude_writes_reads_the_same_way(self) -> None:
        conversation_id, _message_id, outcome, _seen = self.recover(
            [*STORED_COMMAND, STORED_NOTHING_TO_COMPACT])
        self.assertEqual(outcome, "failed")
        [failed] = self.results(conversation_id, 0)
        self.assertEqual(failed["payload"]["detail"], "No messages to compact")

    def test_a_run_that_appended_nothing_is_not_settled_by_an_earlier_one(self):
        # The file holds an older boundary and an older refusal, both before
        # this run's offset.
        conversation_id, _message_id, outcome, _seen = self.recover([])
        self.assertEqual(outcome, "unknown")
        [failed] = self.results(conversation_id, 0)
        self.assertEqual(failed["type"], "compaction.failed")
        self.assertEqual(failed["payload"]["error"], "HARNESS_OUTCOME_UNKNOWN")


class AttachedClaudeCompactionTest(AdapterCase):
    """The same recovery while the process is still alive: the transcript is
    tailed, and the run ends on what it appended, not on a reply."""

    def attach(self, appended: list[dict[str, Any]]) -> list[NormalizedEvent]:
        adapter, _runner = self.claude()
        path = adapter._session_path(RECOVERED_SESSION, self.worktree)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(stored_lines(STORED_EARLIER))
        offset = path.stat().st_size
        with path.open("a") as stream:
            stream.write(stored_lines(appended))
        turn = NativeTurn(
            harness="claude", session_ref=RECOVERED_SESSION,
            run_ref="claude-recovered", worktree=self.worktree,
            metadata={"transcript_path": str(path), "transcript_offset": offset,
                      "compaction": True},
        )
        with mock.patch.object(
            type(adapter), "_identity_alive", side_effect=[True, False]
        ):
            return list(adapter.attach(turn))

    def test_a_boundary_is_passed_on_and_the_run_completes(self) -> None:
        events = self.attach(STORED_COMPACTED)
        [compacted] = of_type(events, "context.compacted")
        self.assertEqual(compacted.payload["trigger"], "manual")
        self.assertEqual(events[-1].type, "run.completed")

    def test_a_refusal_fails_the_run_with_claudes_reason(self) -> None:
        events = self.attach(STORED_REFUSED)
        self.assertEqual(of_type(events, "context.compacted"), [])
        self.assertEqual(events[-1].type, "run.failed")
        self.assertEqual(events[-1].payload["error"], "HARNESS_COMPACT_REFUSED")
        self.assertEqual(
            events[-1].payload["detail"], "Not enough messages to compact.")

    def test_nothing_appended_completes_unreported(self) -> None:
        events = self.attach([])
        self.assertEqual(of_type(events, "context.compacted"), [])
        self.assertEqual(events[-1].type, "run.completed")


# ── AC14, AC15: the route and what the chat view is told ────────────────────


class ManualCompactApiCase(ConversationApiCase):
    def conversation(
        self, *, number: int, harness: str = "codex", state: str = "idle",
        session: str | None = "native-session", activity: str | None = None,
    ) -> str:
        conversation_id = f"cv_{number:032x}"
        timestamp = activity or "2026-07-30 12:00:00"
        with closing(self.connect()) as con:
            con.execute(
                "INSERT INTO conversations "
                "(conversation_id,shell_id,owner_user_id,harness,effort,"
                "worktree,state,title,harness_session_ref,"
                "creation_idempotency_key,creation_request_hash,created_at,"
                "last_activity_at,closed_at) "
                "VALUES (?,1,1,?,'high',?,?,?,?,?,?,?,?,?)",
                (
                    conversation_id, harness,
                    str(self.root / ".sc-worktrees" / "dev"), state,
                    f"Fixture {number}", session, f"fixture-{number}",
                    f"hash-{number}", timestamp, timestamp,
                    timestamp if state == "closed" else None,
                ),
            )
            con.commit()
        return conversation_id

    def press(self, conversation_id: str, key: str = "press-1"):
        return self.request(
            "POST", f"/api/conversations/{conversation_id}/compactions",
            body={}, key=key,
        )

    def transcript(self, conversation_id: str) -> dict:
        status, _, transcript = self.request(
            "GET", f"/api/conversations/{conversation_id}/transcript")
        self.assertEqual(status, 200, transcript)
        return transcript

    def detail(self, conversation_id: str) -> dict:
        status, _, detail = self.request(
            "GET", f"/api/conversations/{conversation_id}")
        self.assertEqual(status, 200, detail)
        return detail

    def add_event(self, conversation_id: str, kind: str, payload: dict) -> None:
        with closing(self.connect()) as con:
            sequence = con.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM conversation_events "
                "WHERE conversation_id=?", (conversation_id,),
            ).fetchone()[0]
            con.execute(
                "INSERT INTO conversation_events "
                "(conversation_id,sequence,event_type,payload) VALUES (?,?,?,?)",
                (conversation_id, sequence, kind, json.dumps(payload)),
            )
            con.commit()


class ManualCompactRouteTest(ManualCompactApiCase):
    def test_a_press_queues_one_engine_request_and_no_operator_message(self):
        conversation_id = self.conversation(number=901)
        status, _, body = self.press(conversation_id)
        self.assertEqual(status, 202, body)
        message_id = body["compaction"]["message_id"]
        with closing(self.connect()) as con:
            message = con.execute(
                "SELECT sender_kind,sender_ref,message_kind,body,state "
                "FROM conversation_messages WHERE conversation_id=?",
                (conversation_id,),
            ).fetchall()
            outbox = con.execute(
                "SELECT message_id,state FROM conversation_outbox "
                "WHERE conversation_id=?", (conversation_id,),
            ).fetchall()
            events = con.execute(
                "SELECT event_type,payload,message_id FROM conversation_events "
                "WHERE conversation_id=?", (conversation_id,),
            ).fetchall()
        self.assertEqual(
            [tuple(row) for row in message],
            [("engine", "compact-button", "control", "/compact", "queued")],
        )
        self.assertEqual([tuple(row) for row in outbox], [(message_id, "pending")])
        [(kind, payload, event_message)] = [tuple(row) for row in events]
        self.assertEqual(kind, "compaction.requested")
        self.assertEqual(event_message, message_id)
        self.assertEqual(json.loads(payload), {
            "message_id": message_id, "initiated_by": "engine",
            "requested_by_user_id": 1, "harness": "codex", "cause": "operator",
        })
        # Nothing of the request is a transcript item.
        transcript = self.transcript(conversation_id)
        self.assertEqual(transcript["items"], [])
        self.assertEqual(transcript["controls"]["queued_count"], 0)
        self.assertEqual(transcript["controls"]["compaction"], {
            "supported": True, "available": True, "enabled": False,
            "pending": True, "message_id": message_id,
        })

    def test_a_second_press_is_refused_and_a_retry_is_the_same_press(self):
        conversation_id = self.conversation(number=902)
        _, _, first = self.press(conversation_id, "press-a")
        status, _, again = self.press(conversation_id, "press-a")
        self.assertEqual((status, again), (202, first))
        status, _, second = self.press(conversation_id, "press-b")
        self.assertEqual(status, 409, second)
        self.assertEqual(second["error"]["code"], "COMPACTION_PENDING")
        with closing(self.connect()) as con:
            count = con.execute(
                "SELECT COUNT(*) FROM conversation_messages "
                "WHERE conversation_id=?", (conversation_id,),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_a_press_is_refused_whenever_the_button_is_not_live(self) -> None:
        cases = (
            (903, {"state": "running"}, "CONVERSATION_BUSY"),
            (904, {"session": None}, "COMPACTION_NO_SESSION"),
            (905, {"harness": "kimi"}, "COMPACTION_UNSUPPORTED"),
            (906, {"state": "closed"}, "CONVERSATION_CLOSED"),
        )
        for number, fields, code in cases:
            with self.subTest(code=code):
                conversation_id = self.conversation(number=number, **fields)
                status, _, body = self.press(conversation_id, f"press-{number}")
                self.assertEqual(status, 409, body)
                self.assertEqual(body["error"]["code"], code)
                with closing(self.connect()) as con:
                    self.assertEqual(con.execute(
                        "SELECT COUNT(*) FROM conversation_messages "
                        "WHERE conversation_id=?", (conversation_id,),
                    ).fetchone()[0], 0)
                    # One open chat per shell: clear the way for the next.
                    con.execute(
                        "DELETE FROM conversations WHERE conversation_id=?",
                        (conversation_id,),
                    )
                    con.commit()

    def test_the_request_needs_its_key_and_takes_no_text(self) -> None:
        conversation_id = self.conversation(number=907)
        status, _, body = self.request(
            "POST", f"/api/conversations/{conversation_id}/compactions", body={})
        self.assertEqual(body["error"]["code"], "IDEMPOTENCY_KEY_REQUIRED")
        # The button takes no text and no instructions.
        for field in ("text", "instructions"):
            with self.subTest(field=field):
                status, _, body = self.request(
                    "POST", f"/api/conversations/{conversation_id}/compactions",
                    body={field: "keep the plan"}, key=f"press-{field}",
                )
                self.assertEqual(status, 422, body)
        with closing(self.connect()) as con:
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM conversation_messages "
                "WHERE conversation_id=?", (conversation_id,),
            ).fetchone()[0], 0)

    def test_closing_a_chat_with_the_request_queued_ends_it_stopped(self) -> None:
        conversation_id = self.conversation(number=908)
        _, _, pressed = self.press(conversation_id)
        version = self.detail(conversation_id)["version"]
        status, _, body = self.request(
            "PATCH", f"/api/conversations/{conversation_id}",
            body={"state": "closed", "version": version},
        )
        self.assertEqual(status, 200, body)
        with closing(self.connect()) as con:
            rows = con.execute(
                "SELECT payload,message_id,run_id FROM conversation_events "
                "WHERE conversation_id=? AND event_type='compaction.failed'",
                (conversation_id,),
            ).fetchall()
        [(payload, message_id, run_id)] = [tuple(row) for row in rows]
        self.assertEqual(json.loads(payload), {
            "outcome": "cancelled", "error": "CONVERSATION_CLOSING",
            "request_message_id": pressed["compaction"]["message_id"],
            "cause": "operator",
        })
        self.assertEqual((message_id, run_id), (None, None))
        labels = [item["label"] for item in self.transcript(conversation_id)["items"]]
        self.assertEqual(
            labels, ["Compaction stopped — the context was not compacted"])


class CompactButtonDataTest(ManualCompactApiCase):
    """AC15: the data the button is painted from."""

    def controls(self, **fields) -> dict:
        conversation_id = self.conversation(**fields)
        detail = self.detail(conversation_id)["compaction"]
        self.assertEqual(
            detail, self.transcript(conversation_id)["controls"]["compaction"])
        with closing(self.connect()) as con:
            con.execute(
                "DELETE FROM conversations WHERE conversation_id=?",
                (conversation_id,),
            )
            con.commit()
        return detail

    def test_shown_and_live_for_an_idle_chat_with_a_session(self) -> None:
        self.assertEqual(self.controls(number=911), {
            "supported": True, "available": True, "enabled": True,
            "pending": False, "message_id": None,
        })

    def test_greyed_out_during_a_turn(self) -> None:
        for number, state in ((912, "running"), (913, "queued"), (914, "waiting")):
            with self.subTest(state=state):
                controls = self.controls(number=number, state=state)
                self.assertTrue(controls["available"])
                self.assertFalse(controls["enabled"])

    def test_hidden_without_a_session_and_for_a_harness_with_no_trigger(self):
        no_session = self.controls(number=915, session=None)
        self.assertEqual(
            (no_session["supported"], no_session["available"]), (True, False))
        kimi = self.controls(number=916, harness="kimi")
        self.assertEqual((kimi["supported"], kimi["available"]), (False, False))
        self.assertFalse(kimi["enabled"])
        for harness in ("claude", "opencode"):
            with self.subTest(harness=harness):
                self.assertTrue(self.controls(number=917, harness=harness)["enabled"])

    def test_the_history_list_carries_no_button_state(self) -> None:
        self.conversation(number=918)
        status, _, listing = self.request("GET", "/api/conversations?open=true")
        self.assertEqual(status, 200, listing)
        self.assertNotIn("compaction", listing["items"][0])


class ManualCompactTranscriptTest(ManualCompactApiCase):
    """The three results, as the loaded transcript shows them."""

    def activities(self, conversation_id: str) -> list[dict]:
        return [
            item for item in self.transcript(conversation_id)["items"]
            if item["kind"] == "activity"
        ]

    def test_the_marker_shows_without_a_turn_and_moves_the_meter(self) -> None:
        conversation_id = self.conversation(number=921)
        self.add_event(conversation_id, "context.compacted", {
            "trigger": "manual", "tokens_before": 90000, "tokens_after": 12000,
            "request_message_id": 7,
        })
        [marker] = self.activities(conversation_id)
        self.assertEqual(marker["label"], "Context compacted · 90k → 12k · manual")
        self.assertEqual(marker["compaction"]["trigger"], "manual")
        self.assertEqual(
            self.transcript(conversation_id)["context"]["context_tokens"], 12000)

    def test_the_error_line_and_the_not_reported_notice(self) -> None:
        conversation_id = self.conversation(number=922)
        self.add_event(conversation_id, "compaction.failed", {
            "outcome": "failed", "error": "HARNESS_COMPACT_REFUSED",
            "detail": "No messages\nto compact", "request_message_id": 7,
        })
        self.add_event(conversation_id, "compaction.failed", {
            "outcome": "cancelled", "request_message_id": 8,
        })
        self.add_event(conversation_id, "compaction.failed", {
            "outcome": "unknown", "request_message_id": 9,
        })
        self.add_event(conversation_id, "compaction.not_reported", {
            "request_message_id": 10,
        })
        self.assertEqual(
            [(item["activity_type"], item["label"])
             for item in self.activities(conversation_id)],
            [
                ("compaction.failed", "Compaction failed — No messages to compact"),
                ("compaction.failed",
                 "Compaction stopped — the context was not compacted"),
                ("compaction.failed",
                 "Compaction failed — the context was not compacted"),
                ("compaction.not_reported",
                 "Compaction not reported — the harness finished without "
                 "reporting a compaction"),
            ],
        )

    def test_claudes_marker_says_the_figure_after_is_the_summary(self) -> None:
        conversation_id = self.conversation(number=923, harness="claude")
        stored = {"trigger": "auto", "tokens_before": 172000, "tokens_after": 13000}
        self.add_event(conversation_id, "context.compacted", stored)
        [marker] = self.activities(conversation_id)
        self.assertEqual(
            marker["label"], "Context compacted · 172k → 13k summary · automatic")
        # The stored event and its projection are unchanged.
        self.assertEqual(marker["compaction"], stored)
        with closing(self.connect()) as con:
            payload = con.execute(
                "SELECT payload FROM conversation_events WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()[0]
        self.assertEqual(json.loads(payload), stored)

    def test_marker_wording_per_harness(self) -> None:
        label = conversation_routes._compaction_label
        both = {"trigger": "manual", "tokens_before": 172000, "tokens_after": 13000}
        self.assertEqual(
            label(both, "claude"), "Context compacted · 172k → 13k summary · manual")
        for harness in ("codex", "opencode", None):
            self.assertEqual(
                label(both, harness), "Context compacted · 172k → 13k · manual")
        self.assertEqual(
            label({"tokens_after": 13000}, "claude"),
            "Context compacted · 13k summary")
        self.assertEqual(
            label({"tokens_after": 13000}, "codex"), "Context compacted · now 13k")
        self.assertEqual(
            label({"tokens_before": 172000}, "claude"),
            "Context compacted · from 172k")


# ── AC11: the cold-return notice ────────────────────────────────────────────

NOW = datetime(2026, 11, 12, 12, 0, 0, tzinfo=timezone.utc)


def ago(minutes: float) -> str:
    return (NOW - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


class ColdReturnRuleTest(unittest.TestCase):
    def test_thresholds_are_sixty_thousand_tokens_and_sixty_minutes(self) -> None:
        self.assertEqual(
            conversation_compaction.cold_return_settings({}),
            {"threshold_tokens": 60000, "threshold_minutes": 60},
        )

    def test_due_only_when_large_and_idle(self) -> None:
        due = conversation_compaction.cold_return_due
        limits = conversation_compaction.cold_return_settings({})
        self.assertTrue(due(90000, ago(120), NOW, limits))
        # At the token threshold counts; below it does not.
        self.assertTrue(due(60000, ago(61), NOW, limits))
        self.assertFalse(due(59999, ago(600), NOW, limits))
        # More than sixty minutes: exactly sixty is not yet.
        self.assertFalse(due(90000, ago(60), NOW, limits))
        self.assertTrue(due(90000, ago(60.1), NOW, limits))
        self.assertFalse(due(90000, ago(5), NOW, limits))

    def test_an_unknown_size_or_time_is_never_a_reason_to_warn(self) -> None:
        due = conversation_compaction.cold_return_due
        self.assertFalse(due(None, ago(600), NOW))
        self.assertFalse(due(90000, None, NOW))
        self.assertFalse(due(90000, "not a time", NOW))
        self.assertFalse(due(True, ago(600), NOW))

    def test_both_thresholds_are_engine_settings(self) -> None:
        limits = conversation_compaction.cold_return_settings({
            "SC_CHAT_COLD_RETURN_TOKENS": "20_000",
            "SC_CHAT_COLD_RETURN_MINUTES": "5",
        })
        self.assertEqual(limits, {"threshold_tokens": 20000, "threshold_minutes": 5})
        self.assertTrue(
            conversation_compaction.cold_return_due(25000, ago(6), NOW, limits))
        # An unusable value falls back to the default.
        self.assertEqual(
            conversation_compaction.cold_return_settings({
                "SC_CHAT_COLD_RETURN_TOKENS": "lots",
                "SC_CHAT_COLD_RETURN_MINUTES": "0",
            }),
            {"threshold_tokens": 60000, "threshold_minutes": 60},
        )


class ColdReturnProjectionTest(ManualCompactApiCase):
    @staticmethod
    def stamp(minutes: float) -> str:
        return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime(
            "%Y-%m-%d %H:%M:%S")

    def model_activity(
        self, conversation_id: str, *, tokens: int, minutes: float,
        active: bool = False,
    ) -> str:
        """One turn whose usage the model reported ``minutes`` ago."""
        stamp = self.stamp(minutes)
        with closing(self.connect()) as con:
            message_id = int(con.execute(
                "INSERT INTO conversation_messages "
                "(conversation_id,sender_kind,sender_ref,message_kind,body,"
                "idempotency_key,request_hash,state,completed_at) "
                "VALUES (?,'user','operator','prompt','hello',?,?,?,?)",
                (conversation_id, f"turn-{stamp}", f"hash-{stamp}",
                 "running" if active else "completed",
                 None if active else stamp),
            ).lastrowid)
            run_id = int(con.execute(
                "INSERT INTO conversation_runs "
                "(conversation_id,shell_id,trigger_message_id,lease_owner,"
                "lease_expires_at,state,started_at,ended_at) "
                "SELECT ?,shell_id,?,'fixture','2099-01-01 00:00:00',?,?,? "
                "FROM conversations WHERE conversation_id=?",
                (conversation_id, message_id,
                 "running" if active else "succeeded", stamp,
                 None if active else stamp, conversation_id),
            ).lastrowid)
            start = con.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM conversation_events "
                "WHERE conversation_id=?", (conversation_id,),
            ).fetchone()[0]
            events = (
                ("message.accepted", {"text": "hello"}, None),
                ("run.started", {}, run_id),
                ("assistant.delta", {"text": "working"}, run_id),
                ("usage", {"tokens": {"inputTokens": tokens}, "scope": "request",
                           "context_tokens": tokens, "context_window": 258400},
                 run_id),
            )
            for offset, (kind, payload, run) in enumerate(events, start=1):
                con.execute(
                    "INSERT INTO conversation_events "
                    "(conversation_id,sequence,event_type,payload,message_id,"
                    "run_id,created_at) VALUES (?,?,?,?,?,?,?)",
                    (conversation_id, start + offset, kind, json.dumps(payload),
                     message_id, run, stamp),
                )
            con.commit()
        return stamp

    def cold(
        self, *, number: int, tokens: int, minutes: float,
        row_minutes: float | None = None, **fields,
    ) -> dict:
        """``minutes`` since the model answered; ``row_minutes`` since the
        conversation row was last stamped (the same unless stated)."""
        conversation_id = self.conversation(
            number=number,
            activity=self.stamp(minutes if row_minutes is None else row_minutes),
            **fields,
        )
        stamp = self.model_activity(
            conversation_id, tokens=tokens, minutes=minutes,
            active=fields.get("state") == "running")
        cold = self.transcript(conversation_id)["cold_return"]
        # The single-chat read reports the same two settings.
        self.assertEqual(
            self.detail(conversation_id)["cold_return"],
            {key: cold[key] for key in ("threshold_tokens", "threshold_minutes")},
        )
        self.assertEqual(cold["last_model_activity_at"], stamp)
        return cold

    def test_idle_is_measured_from_model_activity_not_the_rows_stamp(self):
        # Closed, reopened, renamed or stopped a minute ago: the row was just
        # stamped, but the model last answered two hours ago.
        self.assertTrue(self.cold(
            number=941, tokens=90000, minutes=120, row_minutes=1)["due"])

    def test_a_stale_row_stamp_does_not_make_a_fresh_chat_cold(self) -> None:
        self.assertFalse(self.cold(
            number=942, tokens=90000, minutes=5, row_minutes=600)["due"])

    def test_a_refused_compact_press_is_not_model_activity(self) -> None:
        conversation_id = self.conversation(number=943, activity=self.stamp(120))
        self.model_activity(conversation_id, tokens=90000, minutes=120)
        self.add_event(conversation_id, "compaction.requested", {"message_id": 9})
        self.add_event(conversation_id, "compaction.failed", {
            "outcome": "failed", "request_message_id": 9})
        self.assertTrue(self.transcript(conversation_id)["cold_return"]["due"])
        # A compaction that happened is: the model answered just now.
        self.add_event(conversation_id, "context.compacted", {
            "trigger": "manual", "tokens_before": 90000, "tokens_after": 70000})
        self.assertFalse(self.transcript(conversation_id)["cold_return"]["due"])

    def test_due_for_a_closed_chat_a_message_would_reopen(self) -> None:
        # Opening another chat on the shell closes this one: the usual way
        # back is to a closed chat that still has its session.
        self.assertTrue(self.cold(
            number=944, tokens=90000, minutes=120, state="closed")["due"])

    def test_not_due_for_a_closed_chat_with_no_session_to_reopen(self) -> None:
        self.assertFalse(self.cold(
            number=945, tokens=90000, minutes=120, state="closed",
            session=None)["due"])

    def test_not_due_for_a_chat_the_engine_manages(self) -> None:
        # The fixture's participant chat, in a run that is still armed.
        seed = next(
            getattr(self, name) for name in dir(ConversationApiCase)
            if name.startswith("seed_") and name.endswith("_conversation")
            and name != "seed_conversation"
        )
        conversation_id = seed()
        managed = next(
            key for key in self.detail(conversation_id) if key.endswith("_managed"))
        self.assertTrue(self.detail(conversation_id)[managed])
        with closing(self.connect()) as con:
            con.execute(
                "UPDATE conversations SET harness_session_ref='native-session' "
                "WHERE conversation_id=?", (conversation_id,),
            )
            con.commit()
        self.model_activity(conversation_id, tokens=90000, minutes=120)
        transcript = self.transcript(conversation_id)
        self.assertEqual(transcript["context"]["context_tokens"], 90000)
        self.assertFalse(transcript["cold_return"]["due"])

    def test_the_server_states_its_own_clock(self) -> None:
        cold = self.cold(number=946, tokens=90000, minutes=120)
        stated = datetime.strptime(
            cold["server_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        self.assertLess(
            abs((datetime.now(timezone.utc) - stated).total_seconds()), 60)

    def test_due_for_a_large_idle_chat_with_its_thresholds(self) -> None:
        cold = self.cold(number=931, tokens=90000, minutes=120)
        self.assertTrue(cold["due"])
        self.assertEqual(
            (cold["threshold_tokens"], cold["threshold_minutes"]), (60000, 60))
        # The names say these are the settings: the chat's own size (90,000
        # here) is the meter's ``context_tokens``, never a field of this one.
        self.assertNotIn("context_tokens", cold)
        self.assertNotIn("idle_minutes", cold)

    def test_not_due_for_a_small_chat(self) -> None:
        self.assertFalse(self.cold(number=932, tokens=20000, minutes=600)["due"])

    def test_not_due_for_a_recently_used_chat(self) -> None:
        self.assertFalse(self.cold(number=933, tokens=90000, minutes=5)["due"])

    def test_not_due_while_a_turn_is_running(self) -> None:
        self.assertFalse(
            self.cold(number=934, tokens=90000, minutes=120, state="running")["due"])

    def test_settings_move_the_rule(self) -> None:
        settings = {"SC_CHAT_COLD_RETURN_TOKENS": "10000",
                    "SC_CHAT_COLD_RETURN_MINUTES": "1"}
        with mock.patch.dict(os.environ, settings):
            cold = self.cold(number=935, tokens=20000, minutes=5)
        self.assertTrue(cold["due"])
        self.assertEqual(
            (cold["threshold_tokens"], cold["threshold_minutes"]), (10000, 1))


# ── The chat view ───────────────────────────────────────────────────────────

HELPERS = APP[
    APP.index("function chatContextTokenLabel"):
    APP.index("function chatCollapseOtherReasoning")
]
JS_PRELUDE = (
    "const fmt = (n) => n.toLocaleString('en-US');\n"
    "const el = (tag, props = {}) => ({tag, ...props});\n"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_compact_button_state_from_the_data_the_server_sends() -> None:
    """AC15: hidden, greyed out and live, from the same data as the server."""
    script = JS_PRELUDE + HELPERS + r"""
const on = {supported: true, available: true, enabled: true, pending: false};
const state = (conversation, requesting) => {
  const {visible, disabled, pending, label} =
    chatCompactButtonState(conversation, requesting);
  return {visible, disabled, pending, label};
};
console.log(JSON.stringify({
  idle: state({state: "idle", compaction: on}),
  error: state({state: "error", compaction: on}),
  running: state({state: "running", active_run_id: 4, compaction: on}),
  queued: state({state: "queued", compaction: on}),
  waiting: state({state: "waiting", compaction: on}),
  pending: state({state: "idle", compaction: {...on, pending: true}}),
  requesting: state({state: "idle", compaction: on}, true),
  closing: state({state: "idle", close_requested_at: "t", compaction: on}),
  closed: state({state: "closed", compaction: on}),
  noSession: state({state: "idle",
    compaction: {supported: true, available: false, pending: false}}),
  kimi: state({state: "idle",
    compaction: {supported: false, available: false, pending: false}}),
  unknown: state({state: "idle"}),
  tips: [
    chatCompactButtonState({state: "idle", compaction: on}).title,
    chatCompactButtonState({state: "running", compaction: on}).title,
    chatCompactButtonState({state: "idle", compaction: {...on, pending: true}}).title,
  ],
}));
"""
    result = run_js(script)
    live = {"visible": True, "disabled": False, "pending": False, "label": "Compact"}
    greyed = {**live, "disabled": True}
    compacting = {"visible": True, "disabled": True, "pending": True,
                  "label": "Compacting…"}
    hidden = {"visible": False, "disabled": True, "pending": False,
              "label": "Compact"}
    assert result["idle"] == live
    assert result["error"] == live
    # Greyed out and inert during a turn, and while a compaction is pending.
    assert result["running"] == greyed
    assert result["queued"] == greyed
    assert result["waiting"] == greyed
    assert result["closing"] == greyed
    assert result["pending"] == compacting
    assert result["requesting"] == compacting
    # Absent for a chat with no session, a harness with no trigger, a closed
    # chat, and a conversation whose state the server has not sent.
    assert result["noSession"] == hidden
    assert result["kimi"] == hidden
    assert result["closed"] == hidden
    assert result["unknown"] == hidden
    # The tooltip says the press costs a model request.
    assert "costs one model request" in result["tips"][0]
    assert result["tips"][1] == "Available when the current turn has finished."
    assert result["tips"][2] == "A compaction is in progress."


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_result_lines_and_claude_marker_wording_match_the_server() -> None:
    script = JS_PRELUDE + HELPERS + r"""
const both = {trigger: "auto", tokens_before: 172000, tokens_after: 13000};
console.log(JSON.stringify({
  marker: [
    chatCompactionLabel(both, "claude"), chatCompactionLabel(both, "codex"),
    chatCompactionLabel(both, "opencode"), chatCompactionLabel(both),
    chatCompactionLabel({tokens_after: 13000}, "claude"),
    chatCompactionLabel({tokens_before: 172000}, "claude"),
  ],
  failed: [
    chatCompactionFailedLabel({outcome: "failed", error: "HARNESS_COMPACT_REFUSED",
      detail: "No messages\nto compact"}),
    chatCompactionFailedLabel({outcome: "cancelled"}),
    chatCompactionFailedLabel({outcome: "unknown"}),
    chatCompactionFailedLabel({outcome: "failed", error: "HARNESS_TIMEOUT"}),
  ],
  notReported: chatCompactionNotReportedLabel(),
  titles: [chatCompactionMarkerTitle("claude"), chatCompactionMarkerTitle("codex")],
}));
"""
    result = run_js(script)
    label = conversation_routes._compaction_label
    both = {"trigger": "auto", "tokens_before": 172000, "tokens_after": 13000}
    assert result["marker"] == [
        "Context compacted · 172k → 13k summary · automatic",
        "Context compacted · 172k → 13k · automatic",
        "Context compacted · 172k → 13k · automatic",
        "Context compacted · 172k → 13k · automatic",
        "Context compacted · 13k summary",
        "Context compacted · from 172k",
    ]
    assert result["marker"][:4] == [
        label(both, "claude"), label(both, "codex"), label(both, "opencode"),
        label(both),
    ]
    failed = conversation_routes._compaction_failed_label
    assert result["failed"] == [
        "Compaction failed — No messages to compact",
        "Compaction stopped — the context was not compacted",
        "Compaction failed — the context was not compacted",
        "Compaction failed — HARNESS_TIMEOUT",
    ]
    assert result["failed"] == [
        failed({"outcome": "failed", "error": "HARNESS_COMPACT_REFUSED",
                "detail": "No messages\nto compact"}),
        failed({"outcome": "cancelled"}),
        failed({"outcome": "unknown"}),
        failed({"outcome": "failed", "error": "HARNESS_TIMEOUT"}),
    ]
    assert result["notReported"] == conversation_routes.COMPACTION_NOT_REPORTED_LABEL
    assert "summary alone" in result["titles"][0]
    assert "summary alone" not in result["titles"][1]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_cold_return_rule_in_the_chat_view() -> None:
    """AC11: the same thresholds, read against the page's own clock."""
    # The conversation field that marks a chat the engine manages for a
    # participant, as the rule itself reads it.
    managed_flag = re.search(
        r"conversation\?\.(\w+_managed)\b", HELPERS[HELPERS.index(
            "function chatColdReturnDue"):]).group(1)
    script = JS_PRELUDE + f"const MANAGED_FLAG = {json.dumps(managed_flag)};\n" + HELPERS + r"""
const now = Date.parse("2026-11-12T12:00:00Z");
const ago = (minutes) => new Date(now - minutes * 60000).toISOString()
  .slice(0, 19).replace("T", " ");
const chat = (tokens, minutes, extra = {}) => ({
  state: "idle", scope: "normal",
  // The conversation row's own stamp is never the rule's clock.
  last_activity_at: ago(0),
  context: {context_tokens: tokens},
  cold_return: {threshold_tokens: 60000, threshold_minutes: 60,
    last_model_activity_at: ago(minutes)}, ...extra});
const due = (...args) => chatColdReturnDue(chat(...args), now);
// A browser clock three hours fast, corrected by the server's stated time.
const skew = 3 * 3600000;
const skewed = (minutes) => {
  const conversation = chat(90000, minutes);
  conversation.cold_return = chatColdReturnClock(
    {...conversation.cold_return, server_time: ago(0)}, now + skew);
  return {offset: conversation.cold_return.clock_offset_ms,
    due: chatColdReturnDue(conversation, now + skew),
    // An hour later on that same wrong clock.
    later: chatColdReturnDue(conversation, now + skew + 3600000)};
};
console.log(JSON.stringify({
  largeIdle: due(90000, 120),
  atTokenThreshold: due(60000, 61),
  belowTokens: due(59999, 600),
  exactlySixtyMinutes: due(90000, 60),
  justOver: due(90000, 60.1),
  recent: due(90000, 5),
  error: due(90000, 120, {state: "error"}),
  running: due(90000, 120, {state: "running"}),
  queued: due(90000, 120, {state: "queued"}),
  closedReopenable: due(90000, 120, {state: "closed"}),
  closedManaged: due(90000, 120, {state: "closed", scope: "other"}),
  closedNoContext: due(null, 120, {state: "closed"}),
  closing: due(90000, 120, {close_requested_at: "t"}),
  skewedRecent: skewed(5),
  skewedIdle: skewed(120),
  keptClock: chatColdReturnClock(null, now, {clock_offset_ms: 7}),
  noServerTime: chatColdReturnClock({threshold_minutes: 60}, now).clock_offset_ms,
  returnable: [chatColdReturnable({state: "idle"}),
    chatColdReturnable({state: "closed", scope: "normal"}),
    chatColdReturnable({state: "closed", scope: "other"}),
    chatColdReturnable({state: "running"})],
  managed: due(90000, 120, {[MANAGED_FLAG]: true}),
  compacting: due(90000, 120, {compaction: {pending: true}}),
  unknownSize: due(null, 120),
  unknownTime: chatColdReturnDue({...chat(90000, 120),
    cold_return: {threshold_tokens: 60000, threshold_minutes: 60}}, now),
  defaults: chatColdReturnDue({...chat(90000, 120),
    cold_return: {last_model_activity_at: ago(120)}}, now),
  settings: chatColdReturnDue({...chat(25000, 6),
    cold_return: {threshold_tokens: 20000, threshold_minutes: 5,
      last_model_activity_at: ago(6)}}, now),
  text: chatColdReturnText(chat(90400, 120)),
  textUnknown: chatColdReturnText({}),
  engineTime: [chatEngineTime("2026-11-12 11:00:00"),
    chatEngineTime("2026-11-12T11:00:00Z"), chatEngineTime(""), chatEngineTime(null)],
  expected: Date.parse("2026-11-12T11:00:00Z"),
}));
"""
    result = run_js(script)
    assert result["largeIdle"] is True
    assert result["atTokenThreshold"] is True
    assert result["belowTokens"] is False
    assert result["exactlySixtyMinutes"] is False
    assert result["justOver"] is True
    assert result["recent"] is False
    assert result["error"] is True
    # Never during a turn, for a closing chat, for a managed participant, or
    # while a compaction is already pending.
    for name in ("running", "queued", "closing", "managed", "compacting"):
        assert result[name] is False, name
    # A closed chat a message would reopen counts: it is the usual way back.
    assert result["closedReopenable"] is True
    assert result["closedManaged"] is False
    assert result["closedNoContext"] is False
    assert result["returnable"] == [True, True, False, False]
    # The rule runs on the server's clock: a browser three hours fast does
    # not make a chat used five minutes ago look cold, and still sees a cold
    # one, and an hour later on that clock is an hour later on the server's.
    assert result["skewedRecent"] == {
        "offset": 3 * 3600000, "due": False, "later": True}
    assert result["skewedIdle"]["due"] is True
    assert result["keptClock"] == {"clock_offset_ms": 7}
    assert result["noServerTime"] == 0
    assert result["unknownSize"] is False
    assert result["unknownTime"] is False
    assert result["defaults"] is True
    assert result["settings"] is True
    assert result["text"] == (
        "This chat is large and its cache has likely expired, so the next "
        "message re-reads about 90k tokens at full price."
    )
    assert "its whole context" in result["textUnknown"]
    expected = result["expected"]
    assert result["engineTime"] == [expected, expected, None, None]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_live_events_of_a_manual_compaction_in_the_chat_view() -> None:
    """Each press ends in exactly one line; the request's own run shows none."""
    labels = APP[APP.index("  const activityLabel = (event) =>"):
                 APP.index("  const reduceEvent = (event) =>")]
    reducer = APP[APP.index("  const reduceEvent = (event) =>"):
                  APP.index("  chatOpenStream(\n", APP.index("  const reduceEvent = (event) =>"))]
    script = JS_PRELUDE + HELPERS + r"""
const requests = [];
const reconcileTranscript = () => requests.push("transcript");
const refresh = () => requests.push("refresh");
const paint = () => {}, scheduleTranscript = () => {};
const clearImageAttachments = () => {};
const chatTrackLiveTranscriptItem = (state, itemId) => state.order.push(itemId);
const chatQueuedCount = () => 0;
let stopRequest = null;
const onWakeDelivered = null;
let transcriptState, messages, conversation;
const reset = () => {
  transcriptState = {lastSequence: 40, items: new Map(), order: [],
    dirty: new Set(), assistantAnchors: new Map(), assistantSegments: new Map()};
  messages = [];
  conversation = {state: "idle", active_run_id: null, route: {harness: "codex"},
    shell: {shell_id: 1},
    context: {context_tokens: 90000, context_window: 258400},
    compaction: {supported: true, available: true, pending: false, message_id: null}};
};
""" + labels + reducer + r"""
let sequence = 40;
const emit = (event_type, payload = {}, ids = {}) => reduceEvent({event_type,
  sequence: ++sequence, payload, message_id: ids.message ?? null,
  run_id: ids.run ?? null, created_at: "t"});
const lines = () => transcriptState.order.map((id) => {
  const item = transcriptState.items.get(id);
  return [item.activity_type, item.label];
});
const press = () => {
  reset(); sequence = 40;
  emit("compaction.requested", {message_id: 9}, {message: 9});
  const requested = {...conversation.compaction, state: conversation.state};
  emit("session.started", {}, {message: 9, run: 3});
  emit("run.started", {}, {message: 9, run: 3});
  const running = {state: conversation.state, run: conversation.active_run_id,
    button: chatCompactButtonState(conversation).disabled};
  return {requested, running};
};
const finish = (extra) => ({...extra, lines: lines(), state: conversation.state,
  pending: conversation.compaction.pending,
  button: chatCompactButtonState(conversation).disabled,
  meter: conversation.context.context_tokens});

const compacted = press();
emit("context.compacted", {trigger: "manual", tokens_before: 90000,
  tokens_after: 12000, request_message_id: 9});
emit("run.completed", {}, {message: 9, run: 3});
const marker = finish(compacted);

press();
emit("run.failed", {error: "HARNESS_COMPACT_REFUSED"}, {message: 9, run: 3});
emit("compaction.failed", {outcome: "failed", error: "HARNESS_COMPACT_REFUSED",
  detail: "No messages to compact", request_message_id: 9});
const failed = finish({});

press();
emit("run.process.ended", {label: "Turn process ended"}, {message: 9, run: 3});
emit("run.completed", {}, {message: 9, run: 3});
emit("compaction.not_reported", {request_message_id: 9});
const unreported = finish({});

// An ordinary failed turn is still shown, and still leaves the chat in error.
reset(); sequence = 40;
emit("run.failed", {error: "boom"}, {message: 2, run: 5});
const ordinary = finish({});
console.log(JSON.stringify({marker, failed, unreported, ordinary,
  refreshed: requests.includes("refresh")}));
"""
    result = run_js(script)
    marker = result["marker"]
    assert marker["requested"] == {
        "supported": True, "available": True, "pending": True, "message_id": 9,
        "state": "queued",
    }
    # Greyed out while the requested compaction runs.
    assert marker["running"] == {"state": "running", "run": 3, "button": True}
    assert marker["lines"] == [
        ["context.compacted", "Context compacted · 90k → 12k · manual"],
    ]
    assert (marker["state"], marker["pending"], marker["button"]) == (
        "idle", False, False)
    assert marker["meter"] == 12000      # the second confirmation
    failed = result["failed"]
    assert failed["lines"] == [
        ["compaction.failed", "Compaction failed — No messages to compact"],
    ]
    # No "Turn failed" line beside it, and the chat is not left in error.
    assert (failed["state"], failed["pending"], failed["button"]) == (
        "idle", False, False)
    assert failed["meter"] == 90000
    unreported = result["unreported"]
    assert unreported["lines"] == [
        ["compaction.not_reported",
         "Compaction not reported — the harness finished without reporting "
         "a compaction"],
    ]
    assert (unreported["state"], unreported["pending"]) == ("idle", False)
    assert result["ordinary"]["lines"] == [["run.failed", "Turn failed — boom"]]
    assert result["ordinary"]["state"] == "error"
    assert result["refreshed"] is True


RENDER_OPEN_STUBS = r"""
class Element {
  constructor(tag, props) {
    Object.assign(this, {tag, children: [], hidden: false, disabled: false,
      value: "", title: "", textContent: ""}, props);
    this.classList = {toggle() {}, add() {}, remove() {}};
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  insertBefore(child, next) {
    this.children.splice(this.children.indexOf(next), 0, child);
  }
  setAttribute() {}
  focus() { if (this.onfocus) this.onfocus(); }
}
const el = (tag, props, ...children) => {
  const node = new Element(tag, props); node.append(...children); return node;
};
const all = (node) => node instanceof Element
  ? [node, ...node.children.flatMap(all)] : [];
const fmt = (n) => n.toLocaleString("en-US");
const host = el("section", {});
const readController = new AbortController(), readSignal = readController.signal;
let chatReadController = readController;
let chatRenderGeneration = 1, chatRouteMode = "chat", chatModeController = null;
let chatPendingSend = null;
const CHAT_MODES = ["chat", "diff"];
const listeners = {};
const document = {body: el("body", {}), hidden: false,
  addEventListener(type, handler) { listeners[type] = handler; }};
const requestAnimationFrame = () => 1, cancelAnimationFrame = () => {};
const chatReviewWorkspace = () => ({setMode() {}});
const chatQueuedCount = () => 0;
const chatStartedLabel = () => "started";
const chatHeaderLabel = () => "header";
const chatConversationName = () => "fixture";
const chatTranscriptAtBottom = () => true;
const chatFlushTranscript = () => {};
const chatTrackLiveTranscriptItem = () => {};
const chatWithShellRelease = (action) => action();
const wakeSessionWatch = () => {};
const toasts = [];
const toast = (text) => toasts.push(text);
let keys = 0;
const requestKey = () => `key-${++keys}`;
let confirmed = true;
const confirm = () => confirmed;
let newChats = 0;
let chatNewChatAction = () => { newChats += 1; };
let reduce = null;
const chatOpenStream = (id, generation, after, onEvent) => { reduce = onEvent; };
const chatRead = () => Promise.resolve({harness_status: {codex: {installed: true,
  enabled: true, healthy: true, surfaces: {browser: true}}}});
const posts = [];
let refuse = null;
const chatApi = async (url, method = "GET", body = null, key = null) => {
  if (method === "POST") {
    posts.push({url, body, key});
    if (refuse) throw refuse;
    if (url.endsWith("/compactions"))
      return {compaction: {message_id: 9, state: "queued"}};
    return {message: {message_id: 2, state: "queued", created_at: "t"}};
  }
  throw new Error("unexpected request " + url);
};
let rereads = [];
const chatRefreshConversation = (id, generation, onUpdate) => rereads.push(onUpdate);
const now = Date.now();
const ago = (minutes) => new Date(now - minutes * 60000).toISOString()
  .slice(0, 19).replace("T", " ");
const open = async (overrides = {}) => {
  const conversation = {conversation_id: "cv_fixture", scope: "normal",
    route: {harness: "codex"}, shell: {shortname: "dev", display_name: "Dev",
      shell_id: 1},
    // The row's own stamp is fresh: the rule must not read it.
    last_activity_at: ago(0)};
  const snapshot = {conversation_id: "cv_fixture", projection_version: 3,
    through_sequence: 10,
    controls: {conversation_state: overrides.state ?? "idle",
      conversation_version: 1, active_run_id: null, close_requested_at: null,
      compaction: overrides.compaction ?? {supported: true, available: true,
        enabled: true, pending: false, message_id: null}},
    context: {context_tokens: overrides.tokens ?? 90400, context_window: 258400,
      compact_window: 120000},
    cold_return: {threshold_tokens: 60000, threshold_minutes: 60,
      last_model_activity_at: ago(overrides.minutes ?? 120),
      server_time: ago(0)},
    items: []};
  host.replaceChildren();
  posts.length = 0; refuse = null; keys = 0; rereads = [];
  await chatRenderOpen(host, conversation, snapshot, readSignal);
  await new Promise(setImmediate);
  const find = (test) => all(host).find(test);
  const byText = (text) => find((node) => node.textContent === text);
  return {
    notice: find((node) => node.className === "chat-cold-return"),
    noticeText: find((node) => node.className === "chat-cold-return-text"),
    compact: find((node) => node.className === "act chat-compact"),
    left: find((node) => node.className === "chat-compose-left"),
    meter: find((node) => node.className === "chat-context-meter"),
    composer: find((node) => node.className === "chat-composer-input"),
    send: byText("Send"),
    coldCompact: byText("Compact now"),
    coldContinue: byText("Continue here"),
    coldNewChat: byText("New chat"),
  };
};
"""


def render_open_script(body: str) -> str:
    state_helpers = APP[APP.index("function chatTranscriptPageItems"):
                        APP.index("function chatTranscriptEvictPage")]
    availability = APP[APP.index("function chatHarnessUnavailableReason"):
                       APP.index("function chatStartedLabel")]
    render_open = APP[APP.index("async function chatRenderOpen"):
                      APP.index("async function renderInterface")]
    return (
        RENDER_OPEN_STUBS + HELPERS + state_helpers + availability + render_open
        + "(async () => {\n" + body
        + "\n})().catch((error) => { console.error(error); process.exitCode = 1; });"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_button_sits_bottom_left_and_one_press_is_one_request() -> None:
    """AC15 and AC14 in the rendered composer."""
    result = run_js(render_open_script(r"""
  const view = await open({minutes: 5});
  const placed = {inLeft: view.left.children.includes(view.compact),
    order: view.left.children.map((node) => node.className),
    meter: view.meter.textContent, hidden: view.compact.hidden,
    disabled: view.compact.disabled, label: view.compact.textContent,
    title: view.compact.title, notice: view.notice.hidden};
  // Two clicks before the server answers are one request.
  const first = view.compact.onclick();
  const second = view.compact.onclick();
  const during = {disabled: view.compact.disabled, label: view.compact.textContent};
  await Promise.all([first, second]);
  const pressed = {posts: [...posts], disabled: view.compact.disabled,
    label: view.compact.textContent};
  await view.compact.onclick();   // still pending: inert
  const inert = posts.length;
  // The marker arrives; the button is live again and the meter has dropped.
  reduce({event_type: "context.compacted", sequence: 11, message_id: null,
    run_id: null, created_at: "t", payload: {trigger: "manual",
      tokens_before: 90400, tokens_after: 12000, request_message_id: 9}});
  reduce({event_type: "run.completed", sequence: 12, message_id: 9, run_id: 3,
    created_at: "t", payload: {}});
  const done = {disabled: view.compact.disabled, label: view.compact.textContent,
    meter: view.meter.textContent};
  // The page re-reads the conversation after the run. That read carries no
  // meter: the page keeps the one it has.
  rereads.at(-1)({conversation_id: "cv_fixture", scope: "normal", state: "idle",
    route: {harness: "codex"}, shell: {shortname: "dev", shell_id: 1},
    version: 2, compaction: {supported: true, available: true, enabled: true,
      pending: false, message_id: null},
    cold_return: {threshold_tokens: 60000, threshold_minutes: 60}}, []);
  const reread = {meter: view.meter.textContent, hidden: view.meter.hidden,
    button: view.compact.textContent};

  const running = await open({minutes: 5, state: "running"});
  const turn = {hidden: running.compact.hidden, disabled: running.compact.disabled};
  await running.compact.onclick();
  const refusedByState = posts.length;

  const kimi = await open({minutes: 5, compaction: {supported: false,
    available: false, enabled: false, pending: false, message_id: null}});
  const noSession = await open({minutes: 5, compaction: {supported: true,
    available: false, enabled: false, pending: false, message_id: null}});

  const failing = await open({minutes: 5});
  refuse = {code: "COMPACTION_PENDING", message: "already in progress"};
  await failing.compact.onclick();
  const refused = {toasts: [...toasts], disabled: failing.compact.disabled,
    label: failing.compact.textContent};
  console.log(JSON.stringify({placed, during, pressed, inert, done, reread, turn,
    refusedByState, kimiHidden: kimi.compact.hidden,
    noSessionHidden: noSession.compact.hidden, refused}));
"""))
    placed = result["placed"]
    assert placed["inLeft"] is True
    assert placed["order"] == ["chat-context-meter", "act chat-compact"]
    # The share is of the compact window, not the model's 258k (#451).
    assert placed["meter"] == "Context 90k · 75% of 120k compact window"
    assert (placed["hidden"], placed["disabled"], placed["label"]) == (
        False, False, "Compact")
    assert "costs one model request" in placed["title"]
    assert placed["notice"] is True      # a recently used chat: no notice
    assert result["during"] == {"disabled": True, "label": "Compacting…"}
    assert result["pressed"]["posts"] == [
        {"url": "/conversations/cv_fixture/compactions", "body": {}, "key": "key-1"},
    ]
    assert result["pressed"]["disabled"] is True
    assert result["inert"] == 1
    assert result["done"] == {
        "disabled": False, "label": "Compact", "meter": "Context 12k · 10% of 120k compact window"}
    assert result["reread"] == {
        "meter": "Context 12k · 10% of 120k compact window", "hidden": False, "button": "Compact"}
    # Greyed out and inert during a running turn.
    assert result["turn"] == {"hidden": False, "disabled": True}
    assert result["refusedByState"] == 0
    assert result["kimiHidden"] is True
    assert result["noSessionHidden"] is True
    assert result["refused"]["toasts"] == ["COMPACTION_PENDING: already in progress"]
    assert result["refused"]["label"] == "Compact"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_cold_return_notice_shows_offers_and_is_dismissed() -> None:
    """AC11: the notice on a large idle chat, its actions, and its dismissal."""
    result = run_js(render_open_script(r"""
  const shown = (view) => ({hidden: view.notice.hidden,
    text: view.noticeText.textContent,
    actions: [view.coldCompact, view.coldContinue, view.coldNewChat]
      .filter((node) => !node.hidden).map((node) => node.textContent),
    composer: view.composer.disabled, send: view.send.disabled});

  let view = await open();
  const large = shown(view);
  // "Continue here" dismisses it, and coming back does not raise it again.
  view.coldContinue.onclick();
  const continued = view.notice.hidden;
  view.composer.focus();
  listeners.visibilitychange();
  const stays = view.notice.hidden;

  // Sending dismisses it, and the send is never blocked by it.
  view = await open();
  view.composer.value = "carry on";
  await view.send.onclick();
  const sent = {hidden: view.notice.hidden, posts: posts.map((post) => post.url)};

  // Compacting from the notice is the button's own request, and dismisses it.
  view = await open();
  await view.coldCompact.onclick();
  const compacted = {hidden: view.notice.hidden,
    posts: posts.map((post) => post.url), button: view.compact.textContent};

  // "New chat" asks first, then takes the existing close-and-start path.
  view = await open();
  confirmed = false;
  view.coldNewChat.onclick();
  const declined = {hidden: view.notice.hidden, newChats};
  confirmed = true;
  view.coldNewChat.onclick();
  const started = {hidden: view.notice.hidden, newChats,
    reminder: view.coldNewChat.title};

  // Returning to a chat left open raises it without a reload.
  view = await open({minutes: 5});
  const fresh = view.notice.hidden;
  Date.now = () => now + 3 * 3600000;
  listeners.visibilitychange();
  const returned = view.notice.hidden;
  Date.now = () => now;

  // A browser clock three hours fast does not make a fresh chat look cold.
  Date.now = () => now + 3 * 3600000;
  const skewed = (await open({minutes: 5})).notice.hidden;
  Date.now = () => now;

  // A closed chat that a message would reopen: the usual way back.
  const closed = shown(await open({state: "closed"}));

  const small = shown(await open({tokens: 20000}));
  const recent = shown(await open({minutes: 5}));
  const running = shown(await open({state: "running"}));
  const noButton = shown(await open({compaction: {supported: false,
    available: false, enabled: false, pending: false, message_id: null}}));
  console.log(JSON.stringify({large, continued, stays, sent, compacted, declined,
    started, fresh, returned, skewed, closed,
    small: small.hidden, recent: recent.hidden,
    running: running.hidden, noButton}));
"""))
    large = result["large"]
    assert large["hidden"] is False
    assert large["text"] == (
        "This chat is large and its cache has likely expired, so the next "
        "message re-reads about 90k tokens at full price."
    )
    assert large["actions"] == ["Compact now", "Continue here", "New chat"]
    # A suggestion: the composer and Send stay usable.
    assert (large["composer"], large["send"]) == (False, False)
    assert result["continued"] is True
    assert result["stays"] is True
    assert result["sent"] == {
        "hidden": True, "posts": ["/conversations/cv_fixture/messages"]}
    assert result["compacted"] == {
        "hidden": True, "posts": ["/conversations/cv_fixture/compactions"],
        "button": "Compacting…",
    }
    assert result["declined"] == {"hidden": False, "newChats": 0}
    assert result["started"]["hidden"] is True
    assert result["started"]["newChats"] == 1
    assert "Only control-plane state carries over" in result["started"]["reminder"]
    assert result["fresh"] is True
    assert result["returned"] is False
    assert result["skewed"] is True
    # Shown on a closed chat too; with no live session to compact there, it
    # offers no compaction, and the composer still takes the reopening send.
    assert result["closed"]["hidden"] is False
    assert result["closed"]["actions"] == ["Continue here", "New chat"]
    assert result["closed"]["composer"] is False
    # None on a small chat, a recently used chat, or during a running turn.
    assert result["small"] is True
    assert result["recent"] is True
    assert result["running"] is True
    # Where there is no button, the notice offers no compaction.
    assert result["noButton"]["hidden"] is False
    assert result["noButton"]["actions"] == ["Continue here", "New chat"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_a_reread_keeps_the_last_compact_requests_id() -> None:
    """A transcript or conversation re-read between the run's end and its
    last events must not turn those events into a "Turn failed" line."""
    script = JS_PRELUDE + HELPERS + r"""
const finished = {supported: true, available: true, enabled: true,
  pending: false, message_id: null};
console.log(JSON.stringify({
  kept: chatKeepCompactionRequest(finished, {pending: true, message_id: 9}),
  newer: chatKeepCompactionRequest({...finished, pending: true, message_id: 12},
    {pending: false, message_id: 9}),
  first: chatKeepCompactionRequest(finished, undefined),
  absent: chatKeepCompactionRequest(undefined, {pending: true, message_id: 9}),
}));
"""
    result = run_js(script)
    assert result["kept"]["message_id"] == 9
    assert result["kept"]["pending"] is False
    assert result["newer"]["message_id"] == 12
    assert result["first"]["message_id"] is None
    assert result["absent"] == {"pending": True, "message_id": 9}
    # Both re-reads go through it: the transcript snapshot and the
    # conversation read.
    install = APP[APP.index("  const installSnapshot = (snapshot) =>"):
                  APP.index("  const reconcileTranscript = async")]
    adopt = APP[APP.index("  const adoptConversation = (next) =>"):
                APP.index("  let streamStatus = ")]
    assert "chatKeepCompactionRequest(" in install
    assert "chatKeepCompactionRequest(" in adopt
    assert "context: previous.context" in adopt


def test_chat_view_wires_the_button_the_notice_and_the_listeners() -> None:
    # Bottom left of the composer, beside the meter.
    assert 'className: "chat-compose-left" }, contextMeter)' in APP
    assert "composeLeft.append(compact);" in APP
    assert 'className: "act chat-compact"' in APP
    assert "unavailable, availabilityRetry, coldReturn," in APP
    assert "/compactions`" in APP
    for kind in ("compaction.requested", "compaction.failed",
                 "compaction.not_reported"):
        assert kind in conversation_events.CONVERSATION_EVENT_TYPES
        assert f'"{kind}"' in APP
    assert ".chat-compose-left .act.chat-compact:disabled" in STYLE
    assert ".chat-cold-return {" in STYLE
    assert ".chat-bubble.chat-activity.chat-compaction-failed {" in STYLE


def test_thresholds_are_named_constants_on_both_sides() -> None:
    assert conversation_compaction.COLD_RETURN_TOKENS == 60_000
    assert conversation_compaction.COLD_RETURN_IDLE_MINUTES == 60
    assert "const CHAT_COLD_RETURN_TOKENS = 60000;" in APP
    assert "const CHAT_COLD_RETURN_IDLE_MINUTES = 60;" in APP
