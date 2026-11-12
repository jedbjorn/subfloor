"""Feature #95, spec #272: compaction event, context meter, re-ground line,
earlier compact window.

Hermetic. Every harness line below is handwritten in the shape the installed
harness emits (Claude 2.1.322 stream and transcript, Codex 0.167.1 app-server
notifications, OpenCode 1.19.5 bus events); nothing here starts a harness.
The acceptance criteria named in each class are the rows of the spec's table.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from collections.abc import Iterable, Iterator, Mapping
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / ".super-coder" / "api"))

import conversation_compaction  # noqa: E402
import conversation_events  # noqa: E402
from conversation_adapters import (  # noqa: E402
    ClaudeAdapter,
    CodexAdapter,
    ConversationContext,
    NativeTurn,
    NormalizedEvent,
)
from conversation_adapters import codex as codex_adapter  # noqa: E402
from conversation_adapters.base import (  # noqa: E402
    NORMALIZED_EVENTS,
    compaction_event,
    load_manifest,
)
from conversation_adapters.opencode import _OpenCodeProjection  # noqa: E402
from conversation_broker import BrokerStore  # noqa: E402
from test_conversation_api import ConversationApiCase  # noqa: E402
from test_conversation_broker import (  # noqa: E402
    ConversationBrokerCase,
    FakeAdapter,
)
from test_conversation_ui import APP, STYLE, run_js  # noqa: E402

SESSION = "11111111-2222-4333-8444-555555555555"
BOUNDARY_UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
LINE = conversation_compaction.REGROUND_LINE


def of_type(events: Iterable[NormalizedEvent], kind: str) -> list[NormalizedEvent]:
    return [event for event in events if event.type == kind]


class FakeProcess:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.stdout = io.StringIO("".join(json.dumps(row) + "\n" for row in rows))
        self.pid = 4321
        self.returncode: int | None = None

    def wait(self) -> int:
        self.returncode = 0
        return 0

    def poll(self) -> int | None:
        return self.returncode


class FakeRunner:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def spawn(self, argv, *, cwd, env):
        self.calls.append((list(argv), dict(env)))
        flag = "--resume" if "--resume" in argv else "--session-id"
        session = argv[argv.index(flag) + 1]
        # The fixture lines belong to whichever session this launch names.
        return FakeProcess([
            {**row, "session_id": session} if "session_id" in row else row
            for row in self.rows
        ])


def stream_delta(text: str) -> dict[str, Any]:
    return {
        "type": "stream_event",
        "session_id": SESSION,
        "parent_tool_use_id": None,
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": text},
        },
    }


def request_usage(
    *, uncached: int, cache_read: int, cache_write: int, output: int,
    parent: str | None = None,
) -> dict[str, Any]:
    """One model request's closing ``message_delta``, as Claude streams it."""
    return {
        "type": "stream_event",
        "session_id": SESSION,
        "parent_tool_use_id": parent,
        "event": {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {
                "input_tokens": uncached,
                "cache_creation_input_tokens": cache_write,
                "cache_read_input_tokens": cache_read,
                "output_tokens": output,
            },
        },
    }


CLAUDE_INIT = {
    "type": "system", "subtype": "init", "session_id": SESSION,
    "model": "claude-test-model",
}
CLAUDE_BOUNDARY = {
    "type": "system",
    "subtype": "compact_boundary",
    "session_id": SESSION,
    "uuid": BOUNDARY_UUID,
    "compact_metadata": {
        "trigger": "auto",
        "pre_tokens": 118432,
        "post_tokens": 13638,
        "duration_ms": 41200,
    },
}
CLAUDE_RESULT = {
    "type": "result", "subtype": "success", "is_error": False,
    "session_id": SESSION, "num_turns": 1, "stop_reason": "end_turn",
    "result": "done",
    # The turn total: every request of the turn summed.
    "usage": {
        "input_tokens": 22,
        "cache_creation_input_tokens": 5500,
        "cache_read_input_tokens": 82010,
        "output_tokens": 910,
    },
    "modelUsage": {
        "claude-test-model": {"contextWindow": 200000, "inputTokens": 22},
        "claude-helper-model": {"contextWindow": 1000000, "inputTokens": 9},
    },
}


class AdapterCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        patch = mock.patch("run.linked_vm_configured", return_value=False)
        patch.start()
        self.addCleanup(patch.stop)

    def context(self, **fields: Any) -> ConversationContext:
        return ConversationContext(worktree=self.worktree, **fields)

    def claude(self, rows: list[dict[str, Any]] | None = None):
        runner = FakeRunner(rows)
        return ClaudeAdapter(runner=runner, config_dir=self.root / "claude"), runner


class ClaudeCompactionEventTest(AdapterCase):
    """AC1: a Claude compaction becomes one event, and the turn completes."""

    def test_stream_boundary_is_one_event_with_trigger_and_tokens_before(self) -> None:
        adapter, _runner = self.claude([
            CLAUDE_INIT,
            stream_delta("before"),
            CLAUDE_BOUNDARY,
            # The summary Claude writes back as a user entry is not an event.
            {"type": "user", "session_id": SESSION, "isCompactSummary": True,
             "message": {"role": "user", "content": "This session is being continued…"}},
            stream_delta(" after"),
            CLAUDE_RESULT,
        ])
        events = list(adapter.stream(adapter.start(self.context(), "hello")))

        compacted = of_type(events, "context.compacted")
        self.assertEqual(len(compacted), 1)
        self.assertEqual(
            compacted[0].payload,
            {
                "trigger": "auto",
                "tokens_before": 118432,
                "tokens_after": 13638,
                "compaction_ref": BOUNDARY_UUID,
            },
        )
        self.assertEqual(events[-1].type, "run.completed")
        self.assertEqual(
            "".join(e.payload["text"] for e in of_type(events, "assistant.delta")),
            "before after",
        )
        # No summary text is stored, and the boundary is not a tool event.
        self.assertNotIn("being continued", json.dumps([e.payload for e in events]))
        self.assertEqual(of_type(events, "tool.started"), [])

    def test_manual_trigger_and_missing_post_tokens_stay_as_stated(self) -> None:
        adapter, _runner = self.claude()
        boundary = {
            "type": "system", "subtype": "compact_boundary",
            "compact_metadata": {"trigger": "manual", "pre_tokens": 60200},
        }
        [event] = adapter._normalize(boundary)
        self.assertEqual(
            event.payload, {"trigger": "manual", "tokens_before": 60200}
        )
        # A boundary with no metadata still marks the compaction, with no
        # invented figure and no invented trigger.
        [bare] = adapter._normalize(
            {"type": "system", "subtype": "compact_boundary"}
        )
        self.assertEqual(bare.payload, {"trigger": "unknown"})

    def test_event_is_part_of_the_shared_vocabulary(self) -> None:
        self.assertIn("context.compacted", NORMALIZED_EVENTS)
        self.assertIn(
            "context.compacted", conversation_events.CONVERSATION_EVENT_TYPES
        )
        for harness in ("claude", "codex", "opencode", "kimi"):
            self.assertIn(
                "context.compacted",
                load_manifest(harness)["conversation"]["normalized_events"],
            )


class ClaudeTranscriptRecoveryTest(AdapterCase):
    """AC3: restart recovery records the same single event."""

    def transcript_rows(self) -> list[dict[str, Any]]:
        return [
            {"type": "assistant", "sessionId": SESSION, "cwd": str(self.worktree),
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": "before"}]}},
            # The stored spelling: camelCase, and no ``message``.
            {"type": "system", "subtype": "compact_boundary",
             "sessionId": SESSION, "uuid": BOUNDARY_UUID,
             "content": "Conversation compacted",
             "compactMetadata": {
                 "trigger": "auto", "preTokens": 118432, "postTokens": 13638,
                 "durationMs": 41200,
                 "preservedSegment": {"headUuid": "x"},
             }},
            {"type": "user", "sessionId": SESSION, "isCompactSummary": True,
             "message": {"role": "user",
                         "content": "This session is being continued…"}},
            {"type": "assistant", "sessionId": SESSION,
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": "after"}],
                         "usage": {"input_tokens": 3,
                                   "cache_read_input_tokens": 13000,
                                   "cache_creation_input_tokens": 900,
                                   "output_tokens": 40}}},
        ]

    def attach(self, adapter: ClaudeAdapter) -> list[NormalizedEvent]:
        path = adapter._session_path(SESSION, self.worktree)
        path.parent.mkdir(parents=True)
        path.write_text(
            "".join(json.dumps(row) + "\n" for row in self.transcript_rows())
        )
        turn = NativeTurn(
            harness="claude", session_ref=SESSION, run_ref="claude-recovered",
            worktree=self.worktree,
            metadata={"transcript_path": str(path), "transcript_offset": 0},
        )
        with mock.patch.object(
            ClaudeAdapter, "_identity_alive", side_effect=[True, False]
        ):
            return list(adapter.attach(turn))

    def test_attach_path_reads_the_camel_case_boundary_once(self) -> None:
        adapter, _runner = self.claude()
        events = self.attach(adapter)

        compacted = of_type(events, "context.compacted")
        self.assertEqual(len(compacted), 1)
        self.assertEqual(compacted[0].payload["tokens_before"], 118432)
        # The same event the live stream records for this boundary.
        [streamed] = adapter._normalize(CLAUDE_BOUNDARY)
        self.assertEqual(compacted[0].payload, streamed.payload)
        self.assertEqual(events[-1].type, "run.completed")
        self.assertEqual(events[-1].payload["result"], "after")
        self.assertNotIn("being continued", json.dumps([e.payload for e in events]))
        # The recovered meter is the last request in the transcript.
        self.assertEqual(
            of_type(events, "usage")[-1].payload["context_tokens"], 13903
        )


class RecoveryDedupeTest(ConversationBrokerCase):
    """AC3: a boundary seen on the pipe and again on replay is one marker."""

    def test_replayed_boundary_is_not_recorded_twice(self) -> None:
        _conversation_id, _message_id, run_id = self.add_live_run(
            state="running", session_after="native-session",
            runner_ref="native-run", expired=False, harness="claude",
        )
        store = BrokerStore(self.db_path)
        [streamed] = ClaudeAdapter._compaction(
            CLAUDE_BOUNDARY, "system.compact_boundary"
        )
        [replayed] = ClaudeAdapter._compaction(
            {"type": "system", "subtype": "compact_boundary",
             "uuid": BOUNDARY_UUID,
             "compactMetadata": {"trigger": "auto", "preTokens": 118432,
                                 "postTokens": 13638}},
            "transcript.compact_boundary",
        )
        self.assertEqual(len(store.append_events(run_id, [streamed])), 1)
        self.assertEqual(
            store.append_events(
                run_id, [replayed, NormalizedEvent("assistant.delta", {"text": "x"})]
            ).__len__(),
            1,
        )
        self.assertEqual(store.append_event(run_id, replayed), 0)
        con = self.connect()
        count = con.execute(
            "SELECT COUNT(*) FROM conversation_events "
            "WHERE event_type='context.compacted'"
        ).fetchone()[0]
        con.close()
        self.assertEqual(count, 1)


class FakeCodexRpc:
    def __init__(self, notifications: list[dict[str, Any]] | None = None) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.queued = notifications or []

    def request(self, method: str, params: Mapping[str, Any]) -> Any:
        self.requests.append((method, dict(params)))
        if method in {"thread/start", "thread/resume"}:
            return {"thread": {"id": "thread-1", "cwd": params["cwd"]}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1", "status": "inProgress"}}
        raise AssertionError(f"unexpected Codex request: {method}")

    def notify(self, method: str, params: Mapping[str, Any]) -> None:
        pass

    def notifications(self) -> Iterator[Mapping[str, Any]]:
        return iter(self.queued)

    def close(self) -> None:
        pass


def codex_note(method: str, **params: Any) -> dict[str, Any]:
    return {"method": method,
            "params": {"threadId": "thread-1", "turnId": "turn-1", **params}}


def codex_usage(*, input_tokens: int, total: int, window: int | None = 258400):
    return codex_note(
        "thread/tokenUsage/updated",
        tokenUsage={
            "total": {"totalTokens": 9_000_000, "inputTokens": 8_900_000,
                      "cachedInputTokens": 8_000_000, "outputTokens": 100_000,
                      "reasoningOutputTokens": 0},
            "last": {"totalTokens": total, "inputTokens": input_tokens,
                     "cachedInputTokens": max(0, input_tokens - 2000),
                     "outputTokens": max(0, total - input_tokens),
                     "reasoningOutputTokens": 0},
            "modelContextWindow": window,
        },
    )


COMPACTION_ITEM = {"id": "item-compact-1", "type": "contextCompaction"}
TURN_STARTED = codex_note("turn/started", turn={"id": "turn-1", "status": "inProgress"})
TURN_COMPLETED = codex_note("turn/completed", turn={"id": "turn-1", "status": "completed"})
# What Codex writes right after a compaction: no input side, and its estimate
# of the new context as the total (seen in this host's session store).
AFTER_COMPACTION = codex_usage(input_tokens=0, total=30194)


class CodexCompactionEventTest(AdapterCase):
    """AC2: a Codex compaction becomes one event; no tool event; nothing for
    the deprecated notification."""

    def run_turn(self, notifications, **context) -> list[NormalizedEvent]:
        adapter = CodexAdapter(rpc=FakeCodexRpc(notifications))
        if context.pop("resume", False):
            turn = adapter.resume("thread-1", self.context(**context), "hello")
        else:
            turn = adapter.start(self.context(**context), "hello")
        return list(adapter.stream(turn))

    def test_item_and_notification_record_one_compaction(self) -> None:
        events = self.run_turn([
            TURN_STARTED,
            codex_usage(input_tokens=242105, total=245363),
            codex_note("item/started", item=COMPACTION_ITEM),
            codex_note("thread/compacted"),
            AFTER_COMPACTION,
            codex_note("item/completed", item=COMPACTION_ITEM),
            codex_note("item/agentMessage/delta", delta="continuing"),
            TURN_COMPLETED,
        ])

        compacted = of_type(events, "context.compacted")
        self.assertEqual(len(compacted), 1)
        self.assertEqual(
            compacted[0].payload,
            {
                "trigger": "auto",
                "tokens_before": 245363,
                "tokens_after": 30194,
                "compaction_ref": "item-compact-1",
            },
        )
        self.assertEqual(compacted[0].native_type, "item/completed")
        # The item is no longer a tool event, and the deprecated
        # notification produced nothing of its own.
        self.assertEqual(of_type(events, "tool.started"), [])
        self.assertEqual(of_type(events, "tool.completed"), [])
        self.assertNotIn("thread/compacted", [e.native_type for e in events])
        self.assertEqual(events[-1].type, "run.completed")

    def test_deprecated_notification_alone_records_nothing(self) -> None:
        events = self.run_turn([
            TURN_STARTED, codex_note("thread/compacted"), TURN_COMPLETED,
        ])
        self.assertEqual(
            [event.type for event in events],
            ["session.started", "run.started", "run.completed"],
        )

    def test_size_after_is_read_when_the_update_follows_the_item(self) -> None:
        events = self.run_turn([
            TURN_STARTED,
            codex_usage(input_tokens=200000, total=201000),
            codex_note("item/started", item=COMPACTION_ITEM),
            codex_note("item/completed", item=COMPACTION_ITEM),
            AFTER_COMPACTION,
            TURN_COMPLETED,
        ])
        types = [event.type for event in events]
        [compacted] = of_type(events, "context.compacted")
        self.assertEqual(compacted.payload["tokens_before"], 201000)
        self.assertEqual(compacted.payload["tokens_after"], 30194)
        # Marker first, then the reading that produced its figure.
        self.assertLess(
            types.index("context.compacted"), len(types) - 1 - types[::-1].index("usage")
        )

    def test_compaction_at_turn_start_reads_before_from_the_last_turn(self) -> None:
        events = self.run_turn(
            [
                TURN_STARTED,
                codex_note("item/started", item=COMPACTION_ITEM),
                AFTER_COMPACTION,
                codex_note("item/completed", item=COMPACTION_ITEM),
                TURN_COMPLETED,
            ],
            resume=True,
            context_tokens=49526,
        )
        [compacted] = of_type(events, "context.compacted")
        self.assertEqual(compacted.payload["tokens_before"], 49526)
        self.assertEqual(compacted.payload["tokens_after"], 30194)

    def test_a_count_the_engine_cannot_read_stays_empty(self) -> None:
        events = self.run_turn([
            TURN_STARTED,
            codex_note("item/started", item=COMPACTION_ITEM),
            codex_note("item/completed", item=COMPACTION_ITEM),
            TURN_COMPLETED,
        ])
        [compacted] = of_type(events, "context.compacted")
        self.assertEqual(
            compacted.payload,
            {"trigger": "auto", "compaction_ref": "item-compact-1"},
        )
        self.assertEqual(events[-1].type, "run.completed")

    def test_back_to_back_compactions_do_not_share_a_reading(self) -> None:
        second = {"id": "item-compact-2", "type": "contextCompaction"}
        events = self.run_turn([
            TURN_STARTED,
            codex_usage(input_tokens=200000, total=201000),
            codex_note("item/started", item=COMPACTION_ITEM),
            AFTER_COMPACTION,
            codex_note("item/completed", item=COMPACTION_ITEM),
            codex_note("item/started", item=second),
            codex_note("item/completed", item=second),
            codex_usage(input_tokens=0, total=12000),
            TURN_COMPLETED,
        ])
        self.assertEqual(
            [
                (e.payload.get("tokens_before"), e.payload.get("tokens_after"))
                for e in of_type(events, "context.compacted")
            ],
            [(201000, 30194), (30194, 12000)],
        )

    def test_engine_requested_compaction_is_manual_once(self) -> None:
        adapter = CodexAdapter(rpc=FakeCodexRpc([
            TURN_STARTED,
            codex_usage(input_tokens=80000, total=80500),
            codex_note("item/started", item=COMPACTION_ITEM),
            AFTER_COMPACTION,
            codex_note("item/completed", item=COMPACTION_ITEM),
            codex_usage(input_tokens=31000, total=31200),
            codex_note("item/started", item={"id": "c2", "type": "contextCompaction"}),
            codex_usage(input_tokens=0, total=12000),
            codex_note("item/completed", item={"id": "c2", "type": "contextCompaction"}),
            TURN_COMPLETED,
        ]))
        turn = adapter.start(self.context(), "hello")
        adapter.compaction_requested = True
        events = list(adapter.stream(turn))
        self.assertEqual(
            [e.payload["trigger"] for e in of_type(events, "context.compacted")],
            ["manual", "auto"],
        )


class OpenCodeSignalTest(unittest.TestCase):
    def test_session_compacted_is_one_event_with_no_invented_figures(self) -> None:
        projection = _OpenCodeProjection()
        assistant = {
            "type": "message.updated",
            "properties": {"info": {
                "id": "msg-1", "role": "assistant", "sessionID": "ses_1",
                "tokens": {"input": 1200, "output": 300, "reasoning": 0,
                           "cache": {"read": 80000, "write": 2800}},
            }},
        }
        [usage] = of_type(projection.normalize(assistant), "usage")
        self.assertEqual(usage.payload["scope"], "request")
        self.assertEqual(usage.payload["context_tokens"], 84000)
        self.assertNotIn("context_window", usage.payload)

        [compacted] = projection.normalize(
            {"type": "session.compacted", "properties": {"sessionID": "ses_1"}}
        )
        self.assertEqual(compacted.type, "context.compacted")
        self.assertEqual(
            compacted.payload, {"trigger": "unknown", "tokens_before": 84000}
        )


class ClaudeMeterTest(AdapterCase):
    """AC4: the Claude meter is the last request, not the turn sum."""

    def turn_events(self) -> list[NormalizedEvent]:
        adapter, _runner = self.claude([
            CLAUDE_INIT,
            stream_delta("one"),
            request_usage(uncached=10, cache_read=40000, cache_write=2000, output=400),
            # A delegated agent's request is not the chat's context.
            request_usage(uncached=9, cache_read=7000, cache_write=100, output=60,
                          parent="toolu_agent"),
            stream_delta("two"),
            request_usage(uncached=12, cache_read=42010, cache_write=3500, output=510),
            CLAUDE_RESULT,
        ])
        return list(adapter.stream(adapter.start(self.context(), "hello")))

    def test_context_tokens_equals_the_last_requests_input_side(self) -> None:
        usage = of_type(self.turn_events(), "usage")

        self.assertEqual(
            [(e.payload["scope"], e.payload.get("context_tokens")) for e in usage],
            [("request", 42010), ("subagent", None), ("request", 45522),
             ("turn", None)],
        )
        last_request_input_side = 12 + 42010 + 3500
        rows = [
            {"sequence": index, "event_type": "usage",
             "payload": dict(event.payload), "created_at": "t"}
            for index, event in enumerate(usage, 1)
        ]
        meter = conversation_compaction.meter_reading(reversed(rows))
        self.assertEqual(meter["context_tokens"], last_request_input_side)
        # Not the turn sum (87,532), which the per-reply label used to show.
        self.assertNotEqual(meter["context_tokens"], 22 + 5500 + 82010)

    def test_window_is_the_chats_model_and_only_when_stated(self) -> None:
        turn_total = of_type(self.turn_events(), "usage")[-1]
        self.assertEqual(turn_total.payload["context_window"], 200000)

        adapter, _runner = self.claude()
        # Two models and no init naming the chat's: which one is unknown.
        ambiguous = dict(CLAUDE_RESULT)
        [usage, _terminal] = adapter._normalize(ambiguous)
        self.assertNotIn("context_window", usage.payload)
        # No modelUsage at all: still no window, never a default.
        bare = {k: v for k, v in CLAUDE_RESULT.items() if k != "modelUsage"}
        [usage, _terminal] = adapter._normalize(bare)
        self.assertNotIn("context_window", usage.payload)


class CodexMeterTest(AdapterCase):
    """AC5: the Codex meter carries the window."""

    def test_usage_records_context_tokens_and_context_window(self) -> None:
        adapter = CodexAdapter(rpc=FakeCodexRpc([
            TURN_STARTED,
            codex_usage(input_tokens=15477, total=15482, window=475000),
            TURN_COMPLETED,
        ]))
        events = list(adapter.stream(adapter.start(self.context(), "hello")))
        [usage] = of_type(events, "usage")
        self.assertEqual(usage.payload["context_tokens"], 15477)
        self.assertEqual(usage.payload["context_window"], 475000)
        self.assertEqual(usage.payload["scope"], "request")
        # ``last``, not the thread's running total.
        self.assertEqual(usage.payload["tokens"]["inputTokens"], 15477)

    def test_unknown_window_is_left_out(self) -> None:
        adapter = CodexAdapter(rpc=FakeCodexRpc())
        [usage] = adapter._normalize(
            codex_usage(input_tokens=15477, total=15482, window=None)
        )
        self.assertEqual(usage.payload["context_tokens"], 15477)
        self.assertNotIn("context_window", usage.payload)


class MeterStoreTest(ConversationBrokerCase):
    """AC5, through the store: both figures are recorded and read back."""

    def test_recorded_usage_feeds_the_next_dispatch_and_the_meter(self) -> None:
        conversation_id, _message_id, run_id = self.add_live_run(
            state="running", session_after="native-session",
            runner_ref="native-run", expired=False,
        )
        store = BrokerStore(self.db_path)
        adapter = CodexAdapter(rpc=FakeCodexRpc())
        store.append_events(
            run_id,
            adapter._normalize(codex_usage(input_tokens=84000, total=84300)),
        )
        state = store.compaction_state(conversation_id)
        self.assertEqual(state.context_tokens, 84000)
        self.assertEqual(state.context_window, 258400)
        self.assertIsNone(state.pending_compaction)

        store.append_events(
            run_id,
            [compaction_event(trigger="auto", tokens_before=84300,
                              native_type="fixture")],
        )
        state = store.compaction_state(conversation_id)
        # The compaction stated no size after: the old figure is not kept.
        self.assertIsNone(state.context_tokens)
        self.assertEqual(state.context_window, 258400)
        self.assertIsNotNone(state.pending_compaction)


class RecordingAdapter(FakeAdapter):
    """Records what was dispatched; compacts during the turns it is told to."""

    def __init__(self, *, compact_on: Iterable[int] = (), harness: str = "codex"):
        super().__init__(harness=harness)
        self.compact_on = set(compact_on)
        self.dispatched: list[str] = []
        self.contexts: list[ConversationContext] = []
        self.turns = 0

    def start(self, context, message):
        self.dispatched.append(message)
        self.contexts.append(context)
        return super().start(context, message)

    def resume(self, session_ref, context, message):
        self.dispatched.append(message)
        self.contexts.append(context)
        return super().resume(session_ref, context, message)

    def stream(self, turn):
        self.turns += 1
        yield NormalizedEvent("session.started", {"session_ref": turn.session_ref})
        yield NormalizedEvent("run.started", {"status": "running"})
        yield NormalizedEvent("assistant.delta", {"text": "hello"})
        if self.turns in self.compact_on:
            yield compaction_event(
                trigger="auto", tokens_before=118000, tokens_after=14000,
                native_type="fixture",
            )
        yield NormalizedEvent("run.completed", {"status": "completed"})


class DispatchCase(ConversationBrokerCase):
    def send(self, conversation_id: str, body: str, sender_kind: str = "user") -> int:
        self.serial += 1
        key = f"message-{self.serial}"
        con = self.connect()
        con.execute(
            "UPDATE conversations SET state='queued' WHERE conversation_id=?",
            (conversation_id,),
        )
        message_id = con.execute(
            "INSERT INTO conversation_messages "
            "(conversation_id,sender_kind,sender_ref,message_kind,body,"
            "idempotency_key,request_hash,state) "
            "VALUES (?,?,'1','prompt',?,?,?,'queued')",
            (conversation_id, sender_kind, body, key, f"hash-{key}"),
        ).lastrowid
        con.execute(
            "INSERT INTO conversation_outbox (conversation_id,message_id) "
            "VALUES (?,?)",
            (conversation_id, message_id),
        )
        con.commit()
        con.close()
        return int(message_id)

    def deliver(self, broker, conversation_id: str, body: str, **kwargs) -> int:
        message_id = self.send(conversation_id, body, **kwargs)
        broker.notify()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            con = self.connect()
            row = con.execute(
                "SELECT state FROM conversation_runs WHERE trigger_message_id=?",
                (message_id,),
            ).fetchone()
            con.close()
            if row is not None and row[0] == "succeeded":
                self.assertTrue(broker.wait_idle())
                return message_id
            time.sleep(0.01)
        self.fail(f"message {message_id} did not complete")

    def stored_body(self, message_id: int) -> str:
        con = self.connect()
        body = con.execute(
            "SELECT body FROM conversation_messages WHERE message_id=?",
            (message_id,),
        ).fetchone()[0]
        con.close()
        return body

    def reground_events(self, conversation_id: str) -> list[tuple[int, dict]]:
        con = self.connect()
        rows = con.execute(
            "SELECT message_id,payload FROM conversation_events "
            "WHERE conversation_id=? AND event_type='message.reground' "
            "ORDER BY sequence",
            (conversation_id,),
        ).fetchall()
        con.close()
        return [(int(row[0]), json.loads(row[1])) for row in rows]


class RegroundLineTest(DispatchCase):
    """AC6 and AC7."""

    def test_line_is_sent_once_after_a_compaction(self) -> None:
        """AC6: dispatched text ends with the line; stored body unchanged;
        the next message carries none."""
        adapter = RecordingAdapter(compact_on={1})
        broker = self.start_broker(lambda _harness: adapter)
        conversation_id = self.add_conversation()

        first = self.deliver(broker, conversation_id, "start the work")
        second = self.deliver(broker, conversation_id, "carry on")
        third = self.deliver(broker, conversation_id, "and again")

        self.assertEqual(adapter.dispatched[0], "start the work")
        self.assertTrue(adapter.dispatched[1].endswith(LINE))
        self.assertEqual(adapter.dispatched[1], f"carry on\n\n{LINE}")
        self.assertEqual(adapter.dispatched[2], "and again")
        # The stored messages keep the operator's text.
        self.assertEqual(self.stored_body(first), "start the work")
        self.assertEqual(self.stored_body(second), "carry on")
        self.assertEqual(self.stored_body(third), "and again")
        # The line is recorded as engine-added, on the message that carried it.
        [(message_id, payload)] = self.reground_events(conversation_id)
        self.assertEqual(message_id, second)
        self.assertEqual(payload["added_by"], "engine")
        self.assertEqual(payload["text"], LINE)

    def test_engine_wakes_carry_the_line_like_operator_messages(self) -> None:
        adapter = RecordingAdapter(compact_on={1})
        broker = self.start_broker(lambda _harness: adapter)
        conversation_id = self.add_conversation()
        self.deliver(broker, conversation_id, "start")
        wake = self.deliver(
            broker, conversation_id, "Sprint 12: work unit 3 is ready",
            sender_kind="engine",
        )
        self.assertEqual(
            adapter.dispatched[1], f"Sprint 12: work unit 3 is ready\n\n{LINE}"
        )
        self.assertEqual(self.stored_body(wake), "Sprint 12: work unit 3 is ready")

    def test_several_compactions_before_one_message_add_one_line(self) -> None:
        adapter = RecordingAdapter(compact_on={1, 2, 4})
        broker = self.start_broker(lambda _harness: adapter)
        conversation_id = self.add_conversation()
        for body in ("one", "two", "three", "four", "five", "six"):
            self.deliver(broker, conversation_id, body)
        self.assertEqual(
            [text.endswith(LINE) for text in adapter.dispatched],
            # compacted in turns 1, 2 and 4 -> owed on messages 2, 3 and 5.
            [False, True, True, False, True, False],
        )
        self.assertEqual(adapter.dispatched[1].count(LINE), 1)
        self.assertEqual(len(self.reground_events(conversation_id)), 3)

    def test_no_compaction_no_added_text(self) -> None:
        """AC7: dispatched text equals the stored body exactly."""
        adapter = RecordingAdapter()
        broker = self.start_broker(lambda _harness: adapter)
        conversation_id = self.add_conversation()
        bodies = ["first  message\n", "second", "  third with spaces  "]
        ids = [self.deliver(broker, conversation_id, body) for body in bodies]

        self.assertEqual(adapter.dispatched, bodies)
        self.assertEqual([self.stored_body(i) for i in ids], adapter.dispatched)
        self.assertEqual(self.reground_events(conversation_id), [])

    def test_line_never_reaches_the_boot_file_or_system_prompt(self) -> None:
        adapter = RecordingAdapter(compact_on={1})
        broker = self.start_broker(lambda _harness: adapter)
        conversation_id = self.add_conversation()
        self.deliver(broker, conversation_id, "one")
        self.deliver(broker, conversation_id, "two")
        for context in adapter.contexts:
            self.assertNotIn(LINE, context.boot_content or "")
            self.assertNotIn(LINE, json.dumps(dict(context.env)))


class CompactWindowSettingTest(unittest.TestCase):
    def test_default_off_and_passthrough(self) -> None:
        window = conversation_compaction.compact_window
        self.assertEqual(window("claude", {}), 120000)
        self.assertEqual(window("codex", {}), 120000)
        self.assertIsNone(window("claude", {"SC_CHAT_COMPACT_WINDOW_CLAUDE": "off"}))
        self.assertIsNone(window("codex", {"SC_CHAT_COMPACT_WINDOW_CODEX": " OFF "}))
        # One setting per harness: turning Claude off leaves Codex on.
        self.assertEqual(
            window("codex", {"SC_CHAT_COMPACT_WINDOW_CLAUDE": "off"}), 120000
        )
        # Passed through as set; the engine invents no floor.
        self.assertEqual(
            window("claude", {"SC_CHAT_COMPACT_WINDOW_CLAUDE": "40000"}), 40000
        )
        self.assertEqual(
            window("codex", {"SC_CHAT_COMPACT_WINDOW_CODEX": "200_000"}), 200000
        )
        # Kimi and OpenCode get no window in this delivery.
        self.assertIsNone(window("kimi", {}))
        self.assertIsNone(window("opencode", {}))

    def test_unusable_value_falls_back_to_the_default(self) -> None:
        with self.assertLogs(conversation_compaction.LOG, "WARNING"):
            self.assertEqual(
                conversation_compaction.compact_window(
                    "claude", {"SC_CHAT_COMPACT_WINDOW_CLAUDE": "soon"}
                ),
                120000,
            )


class ClaudeCompactWindowTest(AdapterCase):
    """AC8, Claude: the launch command."""

    def launch(self, **context) -> tuple[list[str], dict[str, str]]:
        adapter, runner = self.claude()
        with mock.patch.dict(
            os.environ, {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "900000"}
        ):
            adapter.start(self.context(**context), "hello")
        return runner.calls[0]

    def test_default_window_is_on_the_command_and_the_env_override_is_cleared(self):
        default = conversation_compaction.compact_window("claude", {})
        argv, env = self.launch(compact_window=default)
        self.assertEqual(argv[argv.index("--autocompact") + 1], "120000")
        self.assertNotIn("CLAUDE_CODE_AUTO_COMPACT_WINDOW", env)

    def test_resume_carries_the_window_too(self) -> None:
        adapter, runner = self.claude()
        path = adapter._session_path(SESSION, self.worktree)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"type": "user", "sessionId": SESSION}) + "\n")
        adapter.resume(SESSION, self.context(compact_window=120000), "again")
        argv, _env = runner.calls[0]
        self.assertIn("--resume", argv)
        self.assertEqual(argv[argv.index("--autocompact") + 1], "120000")

    def test_off_restores_the_harness_default(self) -> None:
        off = conversation_compaction.compact_window(
            "claude", {"SC_CHAT_COMPACT_WINDOW_CLAUDE": "off"}
        )
        argv, env = self.launch(compact_window=off)
        self.assertNotIn("--autocompact", argv)
        # With nothing passed, the operator's own environment is left alone.
        self.assertEqual(env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "900000")

    def test_model_with_a_smaller_window_keeps_the_harness_default(self) -> None:
        argv, _env = self.launch(compact_window=120000, context_window=100000)
        self.assertNotIn("--autocompact", argv)
        # A window at or above the setting takes it.
        argv, _env = self.launch(compact_window=120000, context_window=120000)
        self.assertIn("--autocompact", argv)


class CodexCompactWindowTest(AdapterCase):
    """AC8, Codex: the launch command and the thread config."""

    def launch(self, *, resume: bool = False, **context):
        rpc = FakeCodexRpc()
        with mock.patch.object(
            codex_adapter, "JsonLineRpcProcess", return_value=rpc
        ) as process:
            adapter = CodexAdapter()
            if resume:
                adapter.resume("thread-1", self.context(**context), "hello")
            else:
                adapter.start(self.context(**context), "hello")
        return process.call_args.kwargs["argv"], dict(rpc.requests[0][1])

    def test_default_window_is_on_the_launch_and_in_the_thread_config(self) -> None:
        default = conversation_compaction.compact_window("codex", {})
        for resume in (False, True):
            with self.subTest(resume=resume):
                argv, params = self.launch(resume=resume, compact_window=default)
                override = argv.index("model_auto_compact_token_limit=120000")
                self.assertEqual(argv[override - 1], "-c")
                self.assertLess(override, argv.index("app-server"))
                self.assertEqual(
                    params["config"], {"model_auto_compact_token_limit": 120000}
                )

    def test_off_restores_the_harness_default(self) -> None:
        off = conversation_compaction.compact_window(
            "codex", {"SC_CHAT_COMPACT_WINDOW_CODEX": "off"}
        )
        for resume in (False, True):
            with self.subTest(resume=resume):
                argv, params = self.launch(resume=resume, compact_window=off)
                self.assertFalse([a for a in argv if "auto_compact" in a])
                self.assertNotIn("config", params)

    def test_model_with_a_smaller_window_keeps_the_harness_default(self) -> None:
        argv, params = self.launch(
            resume=True, compact_window=120000, context_window=96000
        )
        self.assertFalse([a for a in argv if "auto_compact" in a])
        self.assertNotIn("config", params)


class BrokerCompactWindowTest(DispatchCase):
    """AC8, end to end: the setting reaches the adapter's context by default."""

    def dispatch_context(self, harness: str, env: dict[str, str]):
        adapter = RecordingAdapter(harness=harness)
        cleared = {
            name: "" for name in conversation_compaction.COMPACT_WINDOW_ENV.values()
        }
        with mock.patch.dict(os.environ, {**cleared, **env}):
            broker = self.start_broker(lambda _harness: adapter)
            conversation_id = self.add_conversation(harness=harness)
            self.deliver(broker, conversation_id, "hello")
        # One open chat per shell: close this one so the next case can open.
        con = self.connect()
        con.execute(
            "UPDATE conversations SET state='closed',"
            "closed_at=CURRENT_TIMESTAMP WHERE conversation_id=?",
            (conversation_id,),
        )
        con.execute(
            "DELETE FROM active_shell_chats WHERE chat_id=?", (conversation_id,)
        )
        con.commit()
        con.close()
        return adapter.contexts[0]

    def test_window_is_present_by_default_and_absent_when_off(self) -> None:
        self.assertEqual(self.dispatch_context("codex", {}).compact_window, 120000)
        self.assertEqual(
            self.dispatch_context("claude", {}).effective_compact_window(), 120000
        )
        self.assertIsNone(
            self.dispatch_context(
                "codex", {"SC_CHAT_COMPACT_WINDOW_CODEX": "off"}
            ).compact_window
        )
        self.assertIsNone(self.dispatch_context("kimi", {}).compact_window)
        self.assertIsNone(self.dispatch_context("opencode", {}).compact_window)

    def test_recorded_smaller_window_switches_the_setting_off_next_turn(self) -> None:
        class SmallWindowAdapter(RecordingAdapter):
            def stream(self, turn):
                yield NormalizedEvent("run.started", {"status": "running"})
                yield NormalizedEvent(
                    "usage",
                    {"tokens": {"inputTokens": 9000}, "scope": "request",
                     "context_tokens": 9000, "context_window": 96000},
                )
                yield NormalizedEvent("run.completed", {"status": "completed"})

        adapter = SmallWindowAdapter()
        broker = self.start_broker(lambda _harness: adapter)
        conversation_id = self.add_conversation()
        with mock.patch.dict(os.environ, {"SC_CHAT_COMPACT_WINDOW_CODEX": ""}):
            self.deliver(broker, conversation_id, "one")
            self.deliver(broker, conversation_id, "two")
        first, second = adapter.contexts
        self.assertEqual(first.effective_compact_window(), 120000)
        self.assertEqual(second.context_window, 96000)
        self.assertEqual(second.context_tokens, 9000)
        self.assertIsNone(second.effective_compact_window())


class TranscriptMarkerAndMeterTest(ConversationApiCase):
    """The chat view's data: the marker (AC1-3) and the meter (AC4-5)."""

    def transcript(self, events: tuple[tuple[str, dict], ...], number: int) -> dict:
        with closing(self.connect()) as con:
            conversation_id = self.seed_conversation(
                con, number=number, state="closed"
            )
            self.seed_segmented_trace(
                con, conversation_id=conversation_id, events=events
            )
            con.commit()
        status, _, transcript = self.request(
            "GET", f"/api/conversations/{conversation_id}/transcript"
        )
        self.assertEqual(status, 200, transcript)
        return transcript

    def test_marker_shows_before_and_after_and_the_meter_follows_it(self) -> None:
        transcript = self.transcript(
            (
                ("assistant.delta", {"text": "working"}),
                ("usage", {"tokens": {"inputTokens": 123000}, "scope": "request",
                           "context_tokens": 123000, "context_window": 258400}),
                ("context.compacted", {"trigger": "auto", "tokens_before": 123400,
                                       "tokens_after": 28100,
                                       "compaction_ref": "item-1"}),
                ("message.reground", {"added_by": "engine", "text": LINE,
                                      "compaction_sequence": 5}),
            ),
            number=801,
        )
        activities = {
            item["activity_type"]: item
            for item in transcript["items"] if item["kind"] == "activity"
        }
        marker = activities["context.compacted"]
        self.assertEqual(
            marker["label"], "Context compacted · 123k → 28k · automatic"
        )
        self.assertEqual(
            marker["compaction"],
            {"trigger": "auto", "tokens_before": 123400, "tokens_after": 28100},
        )
        self.assertIn("Re-ground line added", activities["message.reground"]["label"])
        assistant = next(i for i in transcript["items"] if i["kind"] == "assistant")
        self.assertEqual(assistant["context_tokens"], 123000)
        self.assertEqual(transcript["context"]["context_tokens"], 28100)
        self.assertEqual(transcript["context"]["context_window"], 258400)

    def test_unknown_figures_stay_blank(self) -> None:
        transcript = self.transcript(
            (
                ("assistant.delta", {"text": "working"}),
                ("usage", {"tokens": {"input_tokens": 60000}, "scope": "request",
                           "context_tokens": 60000}),
                ("context.compacted", {"trigger": "unknown"}),
            ),
            number=802,
        )
        marker = next(
            i for i in transcript["items"]
            if i.get("activity_type") == "context.compacted"
        )
        self.assertEqual(marker["label"], "Context compacted")
        self.assertEqual(
            marker["compaction"],
            {"trigger": "unknown", "tokens_before": None, "tokens_after": None},
        )
        # No size after the compaction and no window from the harness.
        self.assertIsNone(transcript["context"]["context_tokens"])
        self.assertIsNone(transcript["context"]["context_window"])

    def test_turn_total_is_never_shown_as_the_context(self) -> None:
        transcript = self.transcript(
            (
                ("assistant.delta", {"text": "answer"}),
                ("usage", {"tokens": {"input_tokens": 12, "cache_read_input_tokens": 42010,
                                      "cache_creation_input_tokens": 3500,
                                      "output_tokens": 510},
                           "scope": "request", "context_tokens": 45522}),
                ("usage", {"tokens": {"input_tokens": 22, "cache_read_input_tokens": 82010,
                                      "cache_creation_input_tokens": 5500,
                                      "output_tokens": 910},
                           "scope": "turn", "context_window": 200000}),
            ),
            number=803,
        )
        assistant = next(i for i in transcript["items"] if i["kind"] == "assistant")
        self.assertEqual(assistant["context_tokens"], 45522)
        self.assertEqual(
            transcript["context"],
            {
                "context_tokens": 45522,
                "context_window": 200000,
                "measured_at": transcript["context"]["measured_at"],
                "sequence": transcript["context"]["sequence"],
            },
        )

    def test_usage_recorded_before_this_feature_still_labels_the_reply(self) -> None:
        transcript = self.transcript(
            (
                ("assistant.delta", {"text": "answer"}),
                ("usage", {"tokens": {"input_tokens": 10000, "output_tokens": 345,
                                      "cache_read_tokens": 2000}}),
            ),
            number=804,
        )
        assistant = next(i for i in transcript["items"] if i["kind"] == "assistant")
        self.assertEqual(assistant["context_tokens"], 12345)
        # But a figure of unknown meaning never drives the meter.
        self.assertIsNone(transcript["context"]["context_tokens"])


CONTEXT_HELPERS = APP[
    APP.index("function chatContextTokenLabel"):
    APP.index("function chatCollapseOtherReasoning")
]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_chat_view_marker_and_meter_wording() -> None:
    script = (
        "const fmt = (n) => n.toLocaleString('en-US');\n"
        "const el = (tag, props = {}) => ({tag, ...props});\n"
        + CONTEXT_HELPERS
        + r"""
const node = {hidden: false, textContent: "", title: "",
  classList: {flags: {}, toggle(name, on) { this.flags[name] = on; }}};
const meter = (context) => {
  chatPaintContextMeter(node, context);
  return {hidden: node.hidden, text: node.textContent, high: node.classList.flags["chat-context-high"]};
};
const context = {context_tokens: null, context_window: null};
const steps = [];
const apply = (event_type, payload) => {
  const windowChanged = chatApplyContextEvent(context, {event_type, payload, created_at: "t"});
  steps.push({windowChanged, ...context});
};
apply("usage", {scope: "request", context_tokens: 84000});
apply("usage", {scope: "turn", context_window: 258400});
apply("usage", {scope: "subagent", tokens: {input_tokens: 5}});
apply("usage", {context_tokens: 999999});
apply("context.compacted", {trigger: "auto", tokens_before: 123400, tokens_after: 28100});
apply("context.compacted", {trigger: "unknown"});
console.log(JSON.stringify({
  reply: [
    chatContextTokenLabel(84000, 258400), chatContextTokenLabel(84000, null),
    chatContextTokenLabel(850, null), chatContextTokenLabel(null, 258400),
    chatContextTokenLabel(84000, 0),
  ],
  meter: [
    meter({context_tokens: 84000, context_window: 258400}),
    meter({context_tokens: 84000, context_window: null}),
    meter({context_tokens: null, context_window: 258400}),
    meter(undefined),
    meter({context_tokens: 230000, context_window: 258400}),
  ],
  marker: [
    chatCompactionLabel({trigger: "auto", tokens_before: 123400, tokens_after: 28100}),
    chatCompactionLabel({trigger: "manual", tokens_before: 60200}),
    chatCompactionLabel({trigger: "unknown", tokens_after: 28100}),
    chatCompactionLabel({trigger: "unknown"}),
    chatCompactionLabel({}),
  ],
  steps,
}));
"""
    )
    result = run_js(script)
    assert result["reply"] == ["84k · 33%", "84k tok", "850 tok", "", "84k tok"]
    assert result["meter"] == [
        {"hidden": False, "text": "Context 84k · 33% of 258k", "high": False},
        {"hidden": False, "text": "Context 84k", "high": False},
        # A window with no size, and no reading at all, show nothing.
        {"hidden": True, "text": "", "high": False},
        {"hidden": True, "text": "", "high": False},
        {"hidden": False, "text": "Context 230k · 89% of 258k", "high": True},
    ]
    assert result["marker"] == [
        "Context compacted · 123k → 28k · automatic",
        "Context compacted · from 60k · manual",
        "Context compacted · now 28k",
        "Context compacted",
        "Context compacted",
    ]
    sizes = [(s["windowChanged"], s["context_tokens"], s["context_window"])
             for s in result["steps"]]
    assert sizes == [
        (False, 84000, None),
        (True, 84000, 258400),      # the window arrives; replies repaint
        (False, 84000, 258400),     # a delegated agent's request is ignored
        (False, 84000, 258400),     # an unscoped figure never moves the meter
        (False, 28100, 258400),     # the meter drops with the compaction
        (False, None, 258400),      # no size stated: unknown, not the old one
    ]


def test_chat_view_wires_the_marker_meter_and_listeners() -> None:
    assert '"context.compacted", "message.reground"' in APP
    assert 'activity.classList.add("chat-compaction-marker")' in APP
    assert "chatPaintContextMeter(contextMeter, conversation.context);" in APP
    # The meter sits in the left group of the composer's bottom row.
    assert 'className: "chat-compose-left" }, contextMeter)' in APP
    assert "composeLeft, pending, send, stop" in APP
    assert ".chat-compose-left {" in STYLE
    assert ".chat-bubble.chat-activity.chat-compaction-marker {" in STYLE


def test_server_and_browser_word_the_marker_the_same_way() -> None:
    import conversation_routes

    assert conversation_routes._compaction_label(
        {"trigger": "auto", "tokens_before": 123400, "tokens_after": 28100}
    ) == "Context compacted · 123k → 28k · automatic"
    assert conversation_routes._compaction_label(
        {"trigger": "manual", "tokens_before": 60200}
    ) == "Context compacted · from 60k · manual"
    assert conversation_routes._compaction_label({}) == "Context compacted"
