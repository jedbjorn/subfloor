"""Feature #95, spec #275 (engine core, task #923): per-harness compaction
settings, threshold compaction, idle compaction and the cause on records.

Hermetic. The harness lines are the recorded shapes the spec #272 suites use
(Claude 2.1.322 stream, Codex 0.167.1 app-server notifications, OpenCode
1.19.5 bus events); nothing here starts a harness. The idle scheduler runs on
an injected clock through the reaper's own heartbeat step. Each class names
the acceptance criteria (spec #275 table) it proves.

Kimi: the adapter does not yet emit per-request usage or a compaction event
(task #924 adds them, behind the trigger gate), so the Kimi threshold cases
below use a recorded-shape ``usage`` event from a fake and a patched
``manual_compaction``; the Kimi wire-stream cases belong to task #924.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
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

import compaction_settings
import conversation_compaction
import conversation_reaper
import conversation_routes
import db_driver
import snapshot
import test_conversation_compaction
from conversation_adapters import (
    ClaudeAdapter,
    CodexAdapter,
    KimiAdapter,
    NormalizedEvent,
)
from conversation_adapters.base import (
    TERMINAL_EVENTS,
    compaction_event,
    usage_event,
)
from conversation_adapters.opencode import _OpenCodeProjection
from conversation_broker import BrokerStore
from test_conversation_api import ConversationApiCase
from test_conversation_broker import ConversationBrokerCase
from test_conversation_compaction import (
    CLAUDE_INIT,
    CLAUDE_RESULT,
    COMPACTION_ITEM,
    LINE,
    TURN_COMPLETED,
    TURN_STARTED,
    DispatchCase,
    FakeCodexRpc,
    FakeRunner,
    codex_note,
    codex_usage,
    request_usage,
)
from test_conversation_manual_compaction import (
    CompactingAdapter,
    ManualCompactBrokerTest,
)
from test_conversation_ui import APP, run_js

ENV_NAMES = tuple(conversation_compaction.COMPACT_WINDOW_ENV.values())
CLEARED_ENV = {name: "" for name in ENV_NAMES}
T0 = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)


def stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class SettingsCase(ConversationBrokerCase):
    """A migrated engine DB with the compaction env cleared."""

    def setUp(self) -> None:
        super().setUp()
        patch = mock.patch.dict(os.environ, CLEARED_ENV)
        patch.start()
        self.addCleanup(patch.stop)

    def db(self):
        con = db_driver.connect(self.db_path)
        self.addCleanup(con.close)
        return con

    def save(self, harness: str, **changes: Any):
        with closing(db_driver.connect(self.db_path)) as con:
            return compaction_settings.save(con, harness, changes, user_id=1)

    def resolve(self, harness: str, env: dict[str, str] | None = None):
        with closing(db_driver.connect(self.db_path)) as con:
            return compaction_settings.resolve(con, harness, env)

    # ── Event-log fixtures ─────────────────────────────────────────────────

    def chat(
        self,
        harness: str = "claude",
        *,
        shell_id: int = 1,
        state: str = "idle",
        session_ref: str | None = "native-session",
    ) -> str:
        return self.add_conversation(
            shell_id=shell_id, harness=harness, state=state,
            session_ref=session_ref,
        )

    def event(
        self,
        conversation_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        at: datetime | None = None,
        message_id: int | None = None,
    ) -> int:
        con = self.connect()
        sequence = con.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM conversation_events "
            "WHERE conversation_id=?", (conversation_id,),
        ).fetchone()[0]
        con.execute(
            "INSERT INTO conversation_events "
            "(conversation_id,sequence,event_type,payload,message_id,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (conversation_id, sequence, event_type, json.dumps(payload),
             message_id, stamp(at or T0)),
        )
        con.commit()
        con.close()
        return int(sequence)

    def usage(self, conversation_id: str, size: int, at: datetime) -> int:
        return self.event(
            conversation_id, "usage",
            {"tokens": {"inputTokens": size}, "scope": "request",
             "context_tokens": size},
            at=at,
        )

    def requests(self, conversation_id: str) -> list[dict[str, Any]]:
        con = self.connect()
        rows = con.execute(
            "SELECT e.payload,m.sender_ref,m.idempotency_key,m.state "
            "FROM conversation_events e JOIN conversation_messages m "
            "ON m.message_id=e.message_id "
            "WHERE e.conversation_id=? AND e.event_type='compaction.requested' "
            "ORDER BY e.sequence",
            (conversation_id,),
        ).fetchall()
        con.close()
        return [
            {**json.loads(row[0]), "_sender": row[1], "_key": row[2],
             "_state": row[3]}
            for row in rows
        ]

    def settle_request(
        self,
        conversation_id: str,
        message_id: int,
        outcome: str,
        *,
        at: datetime,
        after: int | None = None,
        cause: str = "idle",
    ) -> None:
        """End a queued engine request the way the broker would record it:
        the request's own usage, then its one result, and the chat idle."""
        con = self.connect()
        con.execute(
            "UPDATE conversation_messages SET state='running' WHERE message_id=?",
            (message_id,),
        )
        con.execute(
            "UPDATE conversation_messages SET state='completed',"
            "completed_at=? WHERE message_id=?", (stamp(at), message_id),
        )
        con.execute(
            "UPDATE conversation_outbox SET state='cancelled' "
            "WHERE message_id=?", (message_id,),
        )
        con.execute(
            "UPDATE conversations SET state='idle' WHERE conversation_id=?",
            (conversation_id,),
        )
        con.commit()
        con.close()
        self.event(
            conversation_id, "usage",
            {"tokens": {"inputTokens": 140000, "outputTokens": 5000},
             "scope": "compaction"},
            at=at, message_id=message_id,
        )
        if outcome == "compacted":
            self.event(
                conversation_id, "context.compacted",
                {"trigger": "manual", "tokens_before": 140000,
                 "tokens_after": after, "request_message_id": message_id,
                 "cause": cause},
                at=at,
            )
        elif outcome == "failed":
            self.event(
                conversation_id, "compaction.failed",
                {"outcome": "failed", "error": "HARNESS_COMPACT_REFUSED",
                 "request_message_id": message_id, "cause": cause},
                at=at,
            )
        else:
            self.event(
                conversation_id, "compaction.not_reported",
                {"request_message_id": message_id, "cause": cause}, at=at,
            )

    def heartbeat(self, now: datetime) -> list[str]:
        """One reaper heartbeat's idle step, on an injected clock. A new
        reaper each time: the scheduler holds nothing between beats."""
        reaper = conversation_reaper.ConversationReaper(
            self.db_path,
            clock=lambda: now,
            config=conversation_reaper.ReaperConfig(),
        )
        return reaper.idle_once()


# ── AC2: precedence, and the source of each value ───────────────────────────


class PrecedenceTest(SettingsCase):
    """AC2. Saved, then environment, then default; the environment covers
    auto compaction only, and only for Claude and Codex."""

    def test_defaults_per_harness_with_their_source(self) -> None:
        expected = {
            "claude": (True, 250000, 50, 125000),
            "codex": (True, 250000, 25, 125000),
            "kimi": (True, 250000, 25, 125000),
            "opencode": (False, 250000, None, 125000),
        }
        for harness, values in expected.items():
            with self.subTest(harness=harness):
                settings = self.resolve(harness)
                self.assertEqual(
                    (settings.auto_enabled, settings.trigger_tokens,
                     settings.idle_after_minutes, settings.idle_min_tokens),
                    values,
                )
                self.assertEqual(
                    set(settings.as_dict()["sources"].values()), {"default"}
                )
                self.assertFalse(settings.saved)
        self.assertIsNone(self.resolve("vibe"))

    def test_environment_supplies_the_trigger_for_its_harness_only(self) -> None:
        env = {"SC_CHAT_COMPACT_WINDOW_CLAUDE": "180000",
               "SC_CHAT_COMPACT_WINDOW_CODEX": "200_000"}
        claude = self.resolve("claude", env)
        self.assertEqual(claude.trigger_tokens, 180000)
        self.assertEqual(claude.sources["trigger_tokens"], "environment")
        self.assertEqual(claude.sources["auto_enabled"], "environment")
        # Idle compaction is the dropdown's alone.
        self.assertEqual(claude.sources["idle_after_minutes"], "default")
        self.assertEqual(claude.idle_after_minutes, 50)
        self.assertEqual(self.resolve("codex", env).trigger_tokens, 200000)
        # The env names map to claude and codex only.
        for harness in ("kimi", "opencode"):
            with self.subTest(harness=harness):
                settings = self.resolve(harness, env)
                self.assertEqual(settings.trigger_tokens, 250000)
                self.assertEqual(
                    set(settings.sources.values()), {"default"}
                )

    def test_environment_off_turns_auto_off_and_leaves_idle_alone(self) -> None:
        env = {"SC_CHAT_COMPACT_WINDOW_CLAUDE": "off",
               "SC_CHAT_COMPACT_WINDOW_CODEX": "off"}
        claude = self.resolve("claude", env)
        self.assertFalse(claude.auto_enabled)
        self.assertIsNone(claude.effective_trigger)
        self.assertEqual(claude.sources["auto_enabled"], "environment")
        self.assertEqual(claude.sources["trigger_tokens"], "default")
        self.assertEqual(claude.idle_after_minutes, 50)
        self.assertTrue(claude.idle_enabled)
        # Off for Claude and Codex changes nothing for Kimi or OpenCode.
        self.assertTrue(self.resolve("kimi", env).auto_enabled)
        self.assertFalse(self.resolve("opencode", env).auto_enabled)

    def test_environment_off_still_lets_claude_compact_while_idle(self) -> None:
        conversation_id = self.chat("claude")
        self.usage(conversation_id, 140000, T0)
        with mock.patch.dict(os.environ, {"SC_CHAT_COMPACT_WINDOW_CLAUDE": "off"}):
            self.assertEqual(
                self.heartbeat(T0 + timedelta(minutes=50)), [conversation_id]
            )
        [request] = self.requests(conversation_id)
        self.assertEqual(request["cause"], "idle")

    def test_an_unusable_environment_value_is_the_default(self) -> None:
        with self.assertLogs(compaction_settings.LOG, "WARNING"):
            settings = self.resolve(
                "claude", {"SC_CHAT_COMPACT_WINDOW_CLAUDE": "soon"}
            )
        self.assertEqual(settings.trigger_tokens, 250000)
        self.assertEqual(settings.sources["trigger_tokens"], "default")

    def test_a_saved_row_wins_and_the_first_save_copies_the_environment(self):
        env = {"SC_CHAT_COMPACT_WINDOW_CLAUDE": "180000"}
        with mock.patch.dict(os.environ, env):
            saved = self.save("claude", idle_after_minutes=30)
        # The whole row is written: the environment's trigger came with it.
        self.assertEqual(saved.trigger_tokens, 180000)
        self.assertEqual(saved.idle_after_minutes, 30)
        self.assertEqual(set(saved.sources.values()), {"saved"})
        # From now on the environment is ignored for Claude.
        later = self.resolve("claude", {"SC_CHAT_COMPACT_WINDOW_CLAUDE": "300000"})
        self.assertEqual(later.trigger_tokens, 180000)
        self.assertEqual(later.sources["trigger_tokens"], "saved")
        off = self.resolve("claude", {"SC_CHAT_COMPACT_WINDOW_CLAUDE": "off"})
        self.assertTrue(off.auto_enabled)
        # Codex has no row: its environment still applies.
        self.assertEqual(
            self.resolve("codex", {"SC_CHAT_COMPACT_WINDOW_CODEX": "90000"})
            .trigger_tokens,
            90000,
        )

    def test_a_first_save_under_environment_off_keeps_auto_off(self) -> None:
        with mock.patch.dict(os.environ, {"SC_CHAT_COMPACT_WINDOW_CODEX": "off"}):
            saved = self.save("codex", idle_min_tokens=150000)
        self.assertFalse(saved.auto_enabled)
        self.assertEqual(saved.trigger_tokens, 250000)
        self.assertFalse(self.resolve("codex").auto_enabled)

    def test_refused_values_leave_the_saved_row_unchanged(self) -> None:
        self.save("codex", trigger_tokens=180000)
        refused: list[tuple[str, dict[str, Any]]] = [
            ("codex", {"trigger_tokens": 9999}),
            ("codex", {"idle_min_tokens": 5000}),
            ("codex", {"trigger_tokens": True}),
            ("codex", {"trigger_tokens": 150000.0}),
            ("codex", {"idle_after_minutes": 45}),      # past Codex's 40
            ("kimi", {"idle_after_minutes": 30}),       # past Kimi's 25
            ("codex", {"idle_after_minutes": 7}),       # not a dropdown value
            ("codex", {"auto_enabled": "yes"}),
            ("codex", {"provider": "openai"}),
            ("codex", {}),
            ("vibe", {"trigger_tokens": 180000}),
            ("anthropic", {"trigger_tokens": 180000}),
        ]
        for harness, changes in refused:
            with (
                self.subTest(harness=harness, changes=changes),
                self.assertRaises(compaction_settings.SettingsError),
            ):
                self.save(harness, **changes)
        settings = self.resolve("codex")
        self.assertEqual(settings.trigger_tokens, 180000)
        self.assertEqual(settings.idle_after_minutes, 25)
        self.assertIsNone(self.resolve("kimi").as_dict()["updated_at"])

    def test_dropdown_choices_stop_below_each_cache_life(self) -> None:
        self.assertEqual(compaction_settings.idle_choices("claude")[-1], 55)
        self.assertEqual(compaction_settings.idle_choices("codex")[-1], 40)
        self.assertEqual(compaction_settings.idle_choices("kimi")[-1], 25)
        self.assertEqual(compaction_settings.idle_choices("opencode")[-1], 55)
        card = self.resolve("opencode").as_dict()
        self.assertFalse(card["cache_life_measured"])
        self.assertEqual(card["cache_life_minutes"], 60)
        # Off is saved as None.
        saved = self.save("claude", idle_after_minutes=None)
        self.assertIsNone(saved.idle_after_minutes)
        self.assertFalse(saved.idle_enabled)


# ── AC1: settings persist per harness and apply with no restart ─────────────


class SettingsApplyNextTurnTest(DispatchCase):
    """AC1. A saved trigger reaches the next dispatched Claude command and
    Codex thread configuration, with the broker running throughout."""

    def setUp(self) -> None:
        super().setUp()
        patch = mock.patch.dict(os.environ, CLEARED_ENV)
        patch.start()
        self.addCleanup(patch.stop)
        vm = mock.patch("run.linked_vm_configured", return_value=False)
        vm.start()
        self.addCleanup(vm.stop)

    def save(self, harness: str, **changes: Any) -> None:
        with closing(db_driver.connect(self.db_path)) as con:
            compaction_settings.save(con, harness, changes)

    def test_codex_thread_config_takes_the_saved_trigger_next_turn(self) -> None:
        from test_conversation_compaction import RecordingAdapter

        adapter = RecordingAdapter(harness="codex")
        broker = self.start_broker(lambda _harness: adapter)
        conversation_id = self.add_conversation(harness="codex")
        self.deliver(broker, conversation_id, "one")
        self.save("codex", trigger_tokens=180000)
        self.deliver(broker, conversation_id, "two")
        first, second = adapter.contexts
        self.assertEqual(first.compact_window, 250000)
        self.assertEqual(second.compact_window, 180000)
        params = CodexAdapter._thread_params(second)
        self.assertEqual(params["config"], {"model_auto_compact_token_limit": 180000})
        argv = CodexAdapter._compact_launch_args(second)
        self.assertEqual(argv, ["-c", "model_auto_compact_token_limit=180000"])

    def test_claude_command_takes_the_saved_trigger_next_turn(self) -> None:
        from test_conversation_compaction import RecordingAdapter

        adapter = RecordingAdapter(harness="claude")
        broker = self.start_broker(lambda _harness: adapter)
        conversation_id = self.add_conversation(harness="claude")
        self.deliver(broker, conversation_id, "one")
        self.save("claude", trigger_tokens=300000)
        self.deliver(broker, conversation_id, "two")
        self.save("claude", auto_enabled=False)
        self.deliver(broker, conversation_id, "three")
        runner = FakeRunner()
        claude = ClaudeAdapter(runner=runner, config_dir=self.root / "claude")
        for context in adapter.contexts:
            claude.start(context, "hello")
        windows = [
            argv[argv.index("--autocompact") + 1] if "--autocompact" in argv
            else None
            for argv, _env in runner.calls
        ]
        self.assertEqual(windows, ["250000", "300000", None])

    def test_saved_rows_survive_a_rebuild_through_the_snapshot(self) -> None:
        self.assertIn("chat_compaction_settings", snapshot.PER_INSTANCE_TABLES)
        self.assertLess(
            snapshot.PER_INSTANCE_TABLES.index("users"),
            snapshot.PER_INSTANCE_TABLES.index("chat_compaction_settings"),
        )
        self.save("kimi", idle_after_minutes=15)
        # A new connection (a new process) reads the same row.
        with closing(db_driver.connect(self.db_path)) as con:
            kimi = compaction_settings.resolve(con, "kimi")
        assert kimi is not None
        self.assertEqual(kimi.idle_after_minutes, 15)


class MeterReadsTheSavedTriggerTest(ConversationApiCase):
    """Spec #275 "Context meter": the chat's compact window is the saved
    trigger in effect, read inside the projection's fixed read budget."""

    transcript = test_conversation_compaction.TranscriptMarkerAndMeterTest.transcript

    def test_meter_window_follows_the_saved_trigger(self) -> None:
        events = (
            ("assistant.delta", {"text": "answer"}),
            ("usage", {"tokens": {"inputTokens": 84000}, "scope": "request",
                       "context_tokens": 84000, "context_window": 258400}),
        )
        with mock.patch.dict(os.environ, CLEARED_ENV):
            self.assertEqual(
                self.transcript(events, number=901)["context"]["compact_window"],
                250000,
            )
            with closing(db_driver.connect(self.db_path)) as con:
                compaction_settings.save(con, "codex", {"trigger_tokens": 180000})
            self.assertEqual(
                self.transcript(events, number=902)["context"]["compact_window"],
                180000,
            )
            with closing(db_driver.connect(self.db_path)) as con:
                compaction_settings.save(con, "codex", {"auto_enabled": False})
            self.assertIsNone(
                self.transcript(events, number=903)["context"]["compact_window"]
            )


# ── AC5: pass-through keeps its rules ───────────────────────────────────────


class PassThroughTest(DispatchCase):
    """AC5."""

    def setUp(self) -> None:
        super().setUp()
        patch = mock.patch.dict(os.environ, CLEARED_ENV)
        patch.start()
        self.addCleanup(patch.stop)
        vm = mock.patch("run.linked_vm_configured", return_value=False)
        vm.start()
        self.addCleanup(vm.stop)

    def claude_argv(self, **context: Any) -> list[str]:
        from conversation_adapters import ConversationContext

        runner = FakeRunner()
        adapter = ClaudeAdapter(runner=runner, config_dir=self.root / "claude")
        adapter.start(ConversationContext(worktree=self.worktree, **context), "hi")
        return runner.calls[0][0]

    def test_claude_flag_only_inside_its_100k_to_1m_range(self) -> None:
        cases = {
            99_999: None, 100_000: "100000", 250_000: "250000",
            1_000_000: "1000000", 1_000_001: None,
        }
        for window, expected in cases.items():
            with self.subTest(window=window):
                argv = self.claude_argv(compact_window=window)
                found = (
                    argv[argv.index("--autocompact") + 1]
                    if "--autocompact" in argv else None
                )
                self.assertEqual(found, expected)

    def test_claude_flag_absent_for_a_model_with_a_smaller_window(self) -> None:
        argv = self.claude_argv(compact_window=250000, context_window=200000)
        self.assertNotIn("--autocompact", argv)

    def test_codex_key_present(self) -> None:
        from conversation_adapters import ConversationContext

        context = ConversationContext(worktree=self.worktree, compact_window=150000)
        self.assertEqual(
            CodexAdapter._thread_params(context)["config"],
            {"model_auto_compact_token_limit": 150000},
        )

    def test_only_claude_and_codex_take_the_trigger(self) -> None:
        from test_conversation_compaction import BrokerCompactWindowTest

        self.assertTrue(ClaudeAdapter.compact_window_native)
        self.assertTrue(CodexAdapter.compact_window_native)
        self.assertFalse(KimiAdapter.compact_window_native)
        from conversation_adapters import OpenCodeAdapter

        self.assertFalse(OpenCodeAdapter.compact_window_native)
        with closing(db_driver.connect(self.db_path)) as con:
            compaction_settings.save(
                con, "opencode", {"auto_enabled": True, "trigger_tokens": 20000}
            )
        dispatch = BrokerCompactWindowTest.dispatch_context
        self.assertIsNone(dispatch(self, "kimi", {}).compact_window)
        self.assertIsNone(dispatch(self, "opencode", {}).compact_window)
        self.assertEqual(dispatch(self, "codex", {}).compact_window, 250000)


# ── AC4: a turn ending above the trigger gets exactly one compaction ────────


class ThresholdRecordedStreamTest(SettingsCase):
    """AC4, through the store with each harness's recorded stream."""

    def setUp(self) -> None:
        super().setUp()
        vm = mock.patch("run.linked_vm_configured", return_value=False)
        vm.start()
        self.addCleanup(vm.stop)
        self.store = BrokerStore(self.db_path)

    def turn(self, harness: str, events: list[NormalizedEvent]) -> tuple[str, int]:
        """One finished turn with ``events`` recorded on it."""
        conversation_id, _message_id, run_id = self.add_live_run(
            state="running", session_after="native-session",
            runner_ref="native-run", expired=False, harness=harness,
        )
        con = self.connect()
        con.execute(
            "UPDATE conversations SET harness_session_ref='native-session' "
            "WHERE conversation_id=?", (conversation_id,),
        )
        con.commit()
        con.close()
        recorded = [event for event in events if event.type not in TERMINAL_EVENTS]
        if recorded:
            self.store.append_events(run_id, recorded)
        self.assertTrue(
            self.store.finish_run(run_id, "succeeded", event_type="run.completed")
        )
        return conversation_id, run_id

    def claude_events(self, *, cache_read: int) -> list[NormalizedEvent]:
        from conversation_adapters import ConversationContext

        runner = FakeRunner([
            CLAUDE_INIT,
            request_usage(uncached=10, cache_read=cache_read, cache_write=1000,
                          output=400),
            CLAUDE_RESULT,
        ])
        adapter = ClaudeAdapter(runner=runner, config_dir=self.root / "claude")
        context = ConversationContext(worktree=self.worktree)
        return list(adapter.stream(adapter.start(context, "hello")))

    def codex_events(self, notifications) -> list[NormalizedEvent]:
        from conversation_adapters import ConversationContext

        adapter = CodexAdapter(rpc=FakeCodexRpc(notifications))
        context = ConversationContext(worktree=self.worktree)
        return list(adapter.stream(adapter.start(context, "hello")))

    @staticmethod
    def opencode_events(cache_read: int) -> list[NormalizedEvent]:
        return _OpenCodeProjection().normalize({
            "type": "message.updated",
            "properties": {"info": {
                "id": "msg-1", "role": "assistant", "sessionID": "ses_1",
                "tokens": {"input": 1200, "output": 300, "reasoning": 0,
                           "cache": {"read": cache_read, "write": 2800}},
            }},
        })

    def assert_one_threshold(self, conversation_id: str, run_id: int,
                             size: int, trigger: int) -> None:
        [request] = self.requests(conversation_id)
        self.assertEqual(request["cause"], "threshold")
        self.assertEqual(request["initiated_by"], "engine")
        self.assertNotIn("requested_by_user_id", request)
        self.assertEqual(request["trigger_tokens"], trigger)
        self.assertEqual(request["context_tokens"], size)
        # Through the shared helper: the button's sender and body, a
        # deterministic key, queued behind nothing.
        self.assertEqual(request["_sender"], "compact-button")
        self.assertEqual(request["_key"], f"threshold:{run_id}")
        self.assertEqual(request["_state"], "queued")
        con = self.connect()
        state = con.execute(
            "SELECT state FROM conversations WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()[0]
        con.close()
        self.assertEqual(state, "queued")

    def test_claude_turn_above_the_trigger(self) -> None:
        self.save("claude", trigger_tokens=150000)
        conversation_id, run_id = self.turn(
            "claude", self.claude_events(cache_read=200000)
        )
        self.assert_one_threshold(conversation_id, run_id, 201010, 150000)

    def test_codex_turn_above_the_trigger(self) -> None:
        self.save("codex", trigger_tokens=150000)
        conversation_id, run_id = self.turn("codex", self.codex_events([
            TURN_STARTED, codex_usage(input_tokens=215900, total=216150),
            TURN_COMPLETED,
        ]))
        self.assert_one_threshold(conversation_id, run_id, 215900, 150000)

    def test_opencode_turn_above_the_trigger_with_auto_turned_on(self) -> None:
        events = self.opencode_events(180000)
        conversation_id, _run_id = self.turn("opencode", events)
        # OpenCode's auto compaction is off by default: nothing.
        self.assertEqual(self.requests(conversation_id), [])
        con = self.connect()
        con.execute(
            "UPDATE conversations SET state='closed',closed_at=CURRENT_TIMESTAMP "
            "WHERE conversation_id=?", (conversation_id,),
        )
        con.execute("DELETE FROM active_shell_chats WHERE chat_id=?",
                    (conversation_id,))
        con.commit()
        con.close()
        self.save("opencode", auto_enabled=True, trigger_tokens=150000)
        conversation_id, run_id = self.turn("opencode", events)
        self.assert_one_threshold(conversation_id, run_id, 184000, 150000)

    def test_kimi_turn_above_the_trigger_with_a_fake_trigger(self) -> None:
        # Kimi's request usage and trigger come with task #924; until then a
        # request-scope reading from a fake and a patched trigger stand in.
        events = [usage_event({"inputOther": 2000, "inputCacheRead": 270000},
                              "usage.record", scope="request",
                              context_tokens=272000)]
        with mock.patch.object(KimiAdapter, "manual_compaction", True):
            conversation_id, run_id = self.turn("kimi", events)
        self.assert_one_threshold(conversation_id, run_id, 272000, 250000)

    def test_kimi_without_a_trigger_gets_no_request(self) -> None:
        events = [usage_event({"inputOther": 2000}, "usage.record",
                              scope="request", context_tokens=272000)]
        conversation_id, _run_id = self.turn("kimi", events)
        self.assertEqual(self.requests(conversation_id), [])

    def test_none_after_a_turn_that_already_compacted(self) -> None:
        self.save("codex", trigger_tokens=150000)
        conversation_id, _run_id = self.turn("codex", self.codex_events([
            TURN_STARTED,
            codex_usage(input_tokens=215900, total=216150),
            codex_note("item/started", item=COMPACTION_ITEM),
            codex_usage(input_tokens=0, total=18692),
            codex_note("item/completed", item=COMPACTION_ITEM),
            # Grown past the trigger again within the same turn.
            codex_usage(input_tokens=200000, total=200300),
            TURN_COMPLETED,
        ]))
        self.assertEqual(self.requests(conversation_id), [])
        con = self.connect()
        [payload] = [json.loads(row[0]) for row in con.execute(
            "SELECT payload FROM conversation_events WHERE conversation_id=? "
            "AND event_type='context.compacted'", (conversation_id,),
        ).fetchall()]
        con.close()
        # The harness compacted on its own: cause ``harness``.
        self.assertEqual(payload["cause"], "harness")

    def test_none_when_auto_compaction_is_off(self) -> None:
        self.save("codex", auto_enabled=False, trigger_tokens=150000)
        conversation_id, _run_id = self.turn("codex", self.codex_events([
            TURN_STARTED, codex_usage(input_tokens=215900, total=216150),
            TURN_COMPLETED,
        ]))
        self.assertEqual(self.requests(conversation_id), [])

    def test_none_below_the_trigger(self) -> None:
        conversation_id, _run_id = self.turn("codex", self.codex_events([
            TURN_STARTED, codex_usage(input_tokens=215900, total=216150),
            TURN_COMPLETED,
        ]))
        # Default trigger 250,000; the chat is at 215,900.
        self.assertEqual(self.requests(conversation_id), [])

    def test_a_retried_turn_end_cannot_queue_twice(self) -> None:
        self.save("codex", trigger_tokens=150000)
        conversation_id, run_id = self.turn("codex", self.codex_events([
            TURN_STARTED, codex_usage(input_tokens=215900, total=216150),
            TURN_COMPLETED,
        ]))
        con = db_driver.connect(self.db_path)
        try:
            trigger_message_id = con.execute(
                "SELECT trigger_message_id FROM conversation_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
            with db_driver.write_transaction(con, "test.retry"):
                again = conversation_compaction.threshold_compaction(
                    con, conversation_id, run_id=run_id,
                    trigger_message_id=trigger_message_id,
                )
        finally:
            con.close()
        assert again is not None
        self.assertTrue(again.existing)
        self.assertEqual(len(self.requests(conversation_id)), 1)


class ThresholdBrokerTest(DispatchCase):
    """AC4 and AC8 end to end: the turn ends above the trigger, the broker
    runs the one compaction next, the marker names the cause and the next
    message carries the re-ground line."""

    settle = ManualCompactBrokerTest.settle
    events = ManualCompactBrokerTest.events

    def setUp(self) -> None:
        super().setUp()
        patch = mock.patch.dict(os.environ, CLEARED_ENV)
        patch.start()
        self.addCleanup(patch.stop)

    def test_one_threshold_compaction_runs_after_the_turn(self) -> None:
        adapter = SizedAdapter(sizes=[260000, 20000])
        broker = self.start_broker(lambda _harness: adapter)
        conversation_id = self.add_conversation()
        self.deliver(broker, conversation_id, "grow the chat")
        request = wait_for_request(self, conversation_id)
        self.assertEqual(request["cause"], "threshold")
        self.assertEqual(self.settle(broker, request["message_id"]), "succeeded")
        self.assertEqual(adapter.compactions, ["native-session"])
        [marker] = [e for e in self.events(conversation_id)
                    if e["type"] == "context.compacted"]
        self.assertEqual(marker["payload"]["cause"], "threshold")
        self.assertEqual(marker["payload"]["trigger_tokens"], 250000)
        self.assertEqual(
            conversation_routes._compaction_label(marker["payload"], "codex"),
            "Context compacted at the trigger · 90k → 12k",
        )
        following = self.deliver(broker, conversation_id, "carry on")
        self.assertEqual(adapter.dispatched[-1], f"carry on\n\n{LINE}")
        self.assertEqual(
            [m for m, _ in self.reground_events(conversation_id)], [following]
        )
        # The compaction's own run is not a turn, and the small turn after
        # it is under the trigger: still exactly one request.
        self.assertEqual(
            len([e for e in self.events(conversation_id)
                 if e["type"] == "compaction.requested"]),
            1,
        )


class SizedAdapter(CompactingAdapter):
    """A harness whose turns end at the sizes given, in order."""

    def __init__(self, *, sizes, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.sizes = list(sizes)

    def stream(self, turn):
        if turn.metadata.get("compaction"):
            yield from super().stream(turn)
            return
        self.turns += 1
        yield NormalizedEvent("session.started", {"session_ref": turn.session_ref})
        yield NormalizedEvent("run.started", {"status": "running"})
        yield NormalizedEvent("assistant.delta", {"text": "hello"})
        size = self.sizes.pop(0) if self.sizes else 1000
        yield usage_event({"inputTokens": size}, "fixture", scope="request",
                          context_tokens=size)
        if self.turns in self.compact_on:
            yield compaction_event(trigger="auto", tokens_before=size,
                                   tokens_after=14000, native_type="fixture")
        yield NormalizedEvent("run.completed", {"status": "completed"})


def wait_for_request(case, conversation_id: str, count: int = 1) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        found = [
            event["payload"] for event in case.events(conversation_id)
            if event["type"] == "compaction.requested"
        ]
        if len(found) >= count:
            return found[count - 1]
        time.sleep(0.01)
    raise AssertionError("no compaction was requested")


# ── AC6: the idle rule, one case per rule, once per idle period ─────────────


class IdleRuleTest(SettingsCase):
    """AC6. Claude: wait 50, cache life 60, idle size 125k."""

    def setUp(self) -> None:
        super().setUp()
        self.conversation_id = self.chat("claude")
        self.activity = self.usage(self.conversation_id, 140000, T0)

    def at(self, minutes: float) -> list[str]:
        return self.heartbeat(T0 + timedelta(minutes=minutes))

    def test_fires_once_on_the_first_heartbeat_after_the_wait(self) -> None:
        self.assertEqual(self.at(49), [])
        self.assertEqual(self.at(50), [self.conversation_id])
        self.assertEqual(self.at(51), [])
        self.assertEqual(self.at(55), [])
        [request] = self.requests(self.conversation_id)
        self.assertEqual(request["cause"], "idle")
        self.assertNotIn("requested_by_user_id", request)
        self.assertEqual(request["_sender"], "compact-button")
        self.assertTrue(request["_key"].startswith("idle:"))

    def test_rule_1_wait_off(self) -> None:
        self.save("claude", idle_after_minutes=None)
        self.assertEqual(self.at(51), [])

    def test_rule_1_no_manual_trigger(self) -> None:
        kimi = self.chat("kimi", shell_id=2)
        self.usage(kimi, 300000, T0)
        # Kimi's wait is 25 and its cache life 30, but Kimi has no trigger.
        self.assertNotIn(kimi, self.at(26))
        self.assertEqual(self.requests(kimi), [])

    def test_rule_2_closed_or_closing(self) -> None:
        self.event(self.conversation_id, "conversation.close.requested", {})
        self.assertEqual(self.at(51), [])
        con = self.connect()
        con.execute(
            "UPDATE conversations SET state='closed',closed_at=CURRENT_TIMESTAMP "
            "WHERE conversation_id=?", (self.conversation_id,),
        )
        con.commit()
        con.close()
        self.assertEqual(self.at(52), [])

    def test_rule_3_turn_running_or_queued(self) -> None:
        self.add_message(self.conversation_id, state="queued")
        con = self.connect()
        con.execute(
            "UPDATE conversations SET state='queued' WHERE conversation_id=?",
            (self.conversation_id,),
        )
        con.commit()
        con.close()
        self.assertEqual(self.at(51), [])

    def test_rule_4_under_the_idle_size(self) -> None:
        self.usage(self.conversation_id, 100000, T0)
        self.assertEqual(self.at(51), [])

    def test_rule_5_wait_not_passed(self) -> None:
        self.assertEqual(self.at(49.9), [])

    def test_rule_6_past_the_cache_life(self) -> None:
        self.assertEqual(self.at(60), [])
        self.assertEqual(self.at(90), [])

    def test_rule_7_already_requested_this_period(self) -> None:
        self.event(
            self.conversation_id, "compaction.requested",
            {"cause": "idle", "initiated_by": "engine"},
            at=T0 + timedelta(minutes=50),
        )
        self.assertEqual(self.at(51), [])

    def first_request(self) -> int:
        self.assertEqual(self.at(50), [self.conversation_id])
        return int(self.requests(self.conversation_id)[0]["message_id"])

    def test_still_large_after_a_completed_idle_compaction(self) -> None:
        message_id = self.first_request()
        self.settle_request(
            self.conversation_id, message_id, "compacted",
            at=T0 + timedelta(minutes=50, seconds=30), after=130000,
        )
        self.assertEqual(self.at(52), [])
        self.assertEqual(self.at(59), [])
        self.assertEqual(len(self.requests(self.conversation_id)), 1)

    def test_a_failed_idle_compaction_is_not_retried(self) -> None:
        message_id = self.first_request()
        self.settle_request(
            self.conversation_id, message_id, "failed",
            at=T0 + timedelta(minutes=50, seconds=30),
        )
        self.assertEqual(self.at(52), [])
        self.assertEqual(len(self.requests(self.conversation_id)), 1)

    def test_a_not_reported_idle_compaction_is_not_retried(self) -> None:
        message_id = self.first_request()
        self.settle_request(
            self.conversation_id, message_id, "not_reported",
            at=T0 + timedelta(minutes=50, seconds=30),
        )
        self.assertEqual(self.at(52), [])
        self.assertEqual(len(self.requests(self.conversation_id)), 1)

    def test_a_threshold_compactions_records_do_not_move_last_activity(self):
        # A threshold compaction ten minutes after the turn left the chat at
        # 130k. Its usage and marker are the engine's own: the idle period
        # still starts at the turn, so the wait is up at T0+50.
        self.event(
            self.conversation_id, "usage",
            {"tokens": {"inputTokens": 140000}, "scope": "compaction"},
            at=T0 + timedelta(minutes=10),
        )
        self.event(
            self.conversation_id, "context.compacted",
            {"trigger": "manual", "tokens_before": 140000,
             "tokens_after": 130000, "request_message_id": 999,
             "cause": "threshold"},
            at=T0 + timedelta(minutes=10),
        )
        self.assertEqual(self.at(50), [self.conversation_id])

    def test_a_harness_compaction_does_move_last_activity(self) -> None:
        self.event(
            self.conversation_id, "context.compacted",
            {"trigger": "auto", "tokens_before": 300000,
             "tokens_after": 130000, "cause": "harness"},
            at=T0 + timedelta(minutes=10),
        )
        self.assertEqual(self.at(50), [])
        self.assertEqual(self.at(60), [self.conversation_id])

    def test_a_new_turn_opens_a_new_period_that_fires_once(self) -> None:
        message_id = self.first_request()
        self.settle_request(
            self.conversation_id, message_id, "compacted",
            at=T0 + timedelta(minutes=50, seconds=30), after=130000,
        )
        t1 = T0 + timedelta(minutes=70)
        self.usage(self.conversation_id, 150000, t1)
        self.assertEqual(self.heartbeat(t1 + timedelta(minutes=49)), [])
        self.assertEqual(
            self.heartbeat(t1 + timedelta(minutes=50)), [self.conversation_id]
        )
        self.assertEqual(self.heartbeat(t1 + timedelta(minutes=51)), [])
        self.assertEqual(len(self.requests(self.conversation_id)), 2)

    def test_a_failure_in_the_idle_step_never_stops_the_reaper(self) -> None:
        reaper = conversation_reaper.ConversationReaper(
            self.db_path, clock=lambda: T0 + timedelta(minutes=51),
            config=conversation_reaper.ReaperConfig(),
        )
        with mock.patch.object(
            conversation_compaction, "run_idle_compaction",
            side_effect=RuntimeError("boom"),
        ), mock.patch.object(reaper, "sweep_once", return_value=0) as sweep:
            reaper.beat()
        sweep.assert_called_once()
        self.assertEqual(self.requests(self.conversation_id), [])

    def test_the_verdict_names_the_failing_rule(self) -> None:
        verdict = conversation_compaction.idle_verdict
        self.assertTrue(verdict(self.db(), self.conversation_id,
                                T0 + timedelta(minutes=50)).due)
        self.assertEqual(
            verdict(self.db(), self.conversation_id,
                    T0 + timedelta(minutes=10)).failed_rule,
            5,
        )
        self.assertEqual(
            verdict(self.db(), self.conversation_id,
                    T0 + timedelta(minutes=61)).failed_rule,
            6,
        )


# ── AC7: idle state survives an engine restart ──────────────────────────────


class IdleRestartTest(SettingsCase):
    """AC7. Each heartbeat is a fresh reaper: a restart loses nothing."""

    def test_restart_with_a_qualifying_chat_gives_one_then_none(self) -> None:
        conversation_id = self.chat("codex")
        self.usage(conversation_id, 140000, T0)
        # Before the restart the wait has not passed.
        self.assertEqual(self.heartbeat(T0 + timedelta(minutes=20)), [])
        # The engine restarts; the first heartbeat after the wait fires.
        self.assertEqual(
            self.heartbeat(T0 + timedelta(minutes=26)), [conversation_id]
        )
        # It restarts again: nothing is queued twice.
        self.assertEqual(self.heartbeat(T0 + timedelta(minutes=27)), [])
        self.assertEqual(len(self.requests(conversation_id)), 1)


class IdleBrokerTest(DispatchCase):
    """AC6-AC8 end to end: the idle request runs through the broker, its
    marker says why, and the next message carries the re-ground line."""

    settle = ManualCompactBrokerTest.settle
    events = ManualCompactBrokerTest.events

    def setUp(self) -> None:
        super().setUp()
        patch = mock.patch.dict(os.environ, CLEARED_ENV)
        patch.start()
        self.addCleanup(patch.stop)

    def test_idle_compaction_runs_and_the_next_message_re_grounds(self) -> None:
        adapter = SizedAdapter(sizes=[140000, 20000])
        broker = self.start_broker(lambda _harness: adapter)
        conversation_id = self.add_conversation()
        self.deliver(broker, conversation_id, "grow the chat")
        later = datetime.now(timezone.utc) + timedelta(minutes=26)
        reaper = conversation_reaper.ConversationReaper(
            self.db_path, clock=lambda: later,
            config=conversation_reaper.ReaperConfig(),
        )
        self.assertEqual(reaper.idle_once(), [conversation_id])
        request = wait_for_request(self, conversation_id)
        self.assertEqual(self.settle(broker, request["message_id"]), "succeeded")
        [marker] = [e for e in self.events(conversation_id)
                    if e["type"] == "context.compacted"]
        self.assertEqual(marker["payload"]["cause"], "idle")
        self.assertEqual(
            conversation_routes._compaction_label(marker["payload"], "codex"),
            "Context compacted while idle · 90k → 12k",
        )
        following = self.deliver(broker, conversation_id, "back from lunch")
        self.assertEqual(adapter.dispatched[-1], f"back from lunch\n\n{LINE}")
        self.assertEqual(
            [m for m, _ in self.reground_events(conversation_id)], [following]
        )
        # The marker settled the chat at 12k: a later beat in the same
        # period finds nothing to do.
        self.assertEqual(reaper.idle_once(), [])

    def test_a_failed_idle_compaction_names_its_cause(self) -> None:
        adapter = SizedAdapter(sizes=[140000], result="failed")
        broker = self.start_broker(lambda _harness: adapter)
        conversation_id = self.add_conversation()
        self.deliver(broker, conversation_id, "grow the chat")
        later = datetime.now(timezone.utc) + timedelta(minutes=26)
        reaper = conversation_reaper.ConversationReaper(
            self.db_path, clock=lambda: later,
            config=conversation_reaper.ReaperConfig(),
        )
        self.assertEqual(reaper.idle_once(), [conversation_id])
        request = wait_for_request(self, conversation_id)
        self.assertEqual(self.settle(broker, request["message_id"]), "failed")
        [failed] = [e for e in self.events(conversation_id)
                    if e["type"] == "compaction.failed"]
        self.assertEqual(failed["payload"]["cause"], "idle")
        # Not retried in the same idle period.
        self.assertEqual(reaper.idle_once(), [])


# ── AC8: markers and records name the cause ─────────────────────────────────


class CauseRecordTest(SettingsCase):
    """AC8."""

    def enqueue(self, conversation_id: str, cause: str, key: str):
        with closing(db_driver.connect(self.db_path)) as con:
            return conversation_compaction.request_compaction(
                con, conversation_id, cause, idempotency_key=key, user_id=1,
            )

    def test_requested_by_user_id_only_with_operator(self) -> None:
        for number, cause in enumerate(("operator", "threshold", "idle"), 1):
            with self.subTest(cause=cause):
                conversation_id = self.chat("codex", shell_id=1)
                result = self.enqueue(conversation_id, cause, f"k-{number}")
                self.assertTrue(result.queued)
                [request] = self.requests(conversation_id)
                self.assertEqual(request["cause"], cause)
                self.assertEqual(request["initiated_by"], "engine")
                self.assertEqual(
                    "requested_by_user_id" in request, cause == "operator"
                )
                con = self.connect()
                con.execute(
                    "UPDATE conversations SET state='idle' WHERE conversation_id=?",
                    (conversation_id,),
                )
                con.execute(
                    "UPDATE conversations SET state='closed',"
                    "closed_at=CURRENT_TIMESTAMP WHERE conversation_id=?",
                    (conversation_id,),
                )
                con.execute("DELETE FROM active_shell_chats WHERE chat_id=?",
                            (conversation_id,))
                con.commit()
                con.close()

    def test_the_helper_refuses_as_the_button_does(self) -> None:
        conversation_id = self.chat("codex", session_ref=None)
        self.assertEqual(
            self.enqueue(conversation_id, "idle", "a").refusal,
            "COMPACTION_NO_SESSION",
        )
        kimi = self.chat("kimi", shell_id=2)
        refused = self.enqueue(kimi, "threshold", "b")
        self.assertEqual(refused.refusal, "COMPACTION_UNSUPPORTED")
        self.assertEqual(refused.detail, "kimi has no manual compaction trigger")
        self.assertEqual(self.requests(conversation_id), [])
        self.assertEqual(self.requests(kimi), [])
        with self.assertRaises(ValueError):
            self.enqueue(conversation_id, "harness", "c")

    def test_each_cause_has_its_wording(self) -> None:
        label = conversation_routes._compaction_label
        figures = {"tokens_before": 141000, "tokens_after": 29000}
        self.assertEqual(
            label({**figures, "trigger": "manual", "cause": "operator"}),
            "Context compacted · 141k → 29k",
        )
        self.assertEqual(
            label({**figures, "trigger": "manual", "cause": "threshold",
                   "trigger_tokens": 120000}),
            "Context compacted at the trigger · 141k → 29k",
        )
        self.assertEqual(
            label({**figures, "trigger": "manual", "cause": "idle"}),
            "Context compacted while idle · 141k → 29k",
        )
        self.assertEqual(
            label({**figures, "trigger": "auto", "cause": "harness"}, "codex"),
            "Context compacted by Codex · 141k → 29k",
        )
        self.assertEqual(
            label({**figures, "trigger": "auto", "cause": "harness"}, "claude"),
            "Context compacted by Claude · 141k → 29k summary",
        )
        self.assertEqual(
            label({"trigger": "manual", "cause": "threshold",
                   "tokens_before": 300000, "tokens_after": 260000,
                   "trigger_tokens": 250000}),
            "Context compacted at the trigger · 300k → 260k · "
            "still at or above the trigger",
        )
        self.assertEqual(label({"cause": "idle"}), "Context compacted while idle")
        # A marker recorded before causes existed keeps its wording.
        self.assertEqual(
            label({**figures, "trigger": "auto"}),
            "Context compacted · 141k → 29k · automatic",
        )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_browser_words_each_cause_like_the_server() -> None:
    helpers = APP[
        APP.index("const CHAT_COMPACTION_HARNESS_NAMES"):
        APP.index("function chatCompactionMarkerTitle")
    ]
    payloads = [
        ({"trigger": "manual", "cause": "operator", "tokens_before": 141000,
          "tokens_after": 29000}, "codex"),
        ({"trigger": "manual", "cause": "threshold", "tokens_before": 300000,
          "tokens_after": 260000, "trigger_tokens": 250000}, "codex"),
        ({"trigger": "manual", "cause": "idle", "tokens_before": 141000,
          "tokens_after": 29000}, "claude"),
        ({"trigger": "auto", "cause": "harness", "tokens_before": 141000,
          "tokens_after": 29000}, "opencode"),
        ({"trigger": "auto", "tokens_before": 141000, "tokens_after": 29000},
         "codex"),
    ]
    script = (
        "const chatTokenCount = (v) => (Number.isInteger(v) && v >= 0) ? v : null;\n"
        "const chatShortTokens = (v) => v >= 1000 ? `${Math.round(v / 1000)}k` : String(v);\n"
        + helpers
        + "\nconst cases = " + json.dumps(payloads) + ";\n"
        + "console.log(JSON.stringify(cases.map(([p, h]) => chatCompactionLabel(p, h))));"
    )
    labels = run_js(script)
    assert labels == [
        conversation_routes._compaction_label(payload, harness)
        for payload, harness in payloads
    ]
    assert labels[2] == "Context compacted while idle · 141k → 29k summary"
