"""Feature #95, spec #275, task #924: Kimi's context meter, compaction event
and manual compaction trigger.

Hermetic. AC9 reads a recorded Kimi Code 2.5.3 / K3.1 Pro main wire
(``fixtures/conversations/kimi/k3.1-turn-then-compaction-main-wire.jsonl``):
one resumed turn of two model steps (a tool call, then ``end_turn``), then a
manual compaction made over ACP, recorded on the delivery host on 2026-11-13.
Long strings are elided and the compaction's ``summary`` / ``contextSummary``
are replaced by sentinels, so the tests can prove no summary is stored.

AC9a, the trigger, passed its gate live on the same host (the delivery
report lists the evidence). Here it runs against a fake ``kimi acp`` server
that appends the recorded compaction records when it receives ``/compact``.
"""
from __future__ import annotations

import json
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / ".super-coder" / "api"))

import conversation_compaction
import db_driver
from conversation_adapters import (
    AdapterError,
    ConversationContext,
    KimiAdapter,
    NativeTurn,
)
from test_conversation_adapters import FakeKimiRunner
from test_conversation_compaction import DispatchCase

FIXTURE = (
    ROOT / "tests" / "fixtures" / "conversations" / "kimi"
    / "k3.1-turn-then-compaction-main-wire.jsonl"
)
RECORDS: list[dict[str, Any]] = [
    json.loads(line) for line in FIXTURE.read_text().splitlines()
]
BEGIN = next(
    index for index, raw in enumerate(RECORDS)
    if raw["type"] == "full_compaction.begin"
)
# The turn (prompt to prompt.completed) and the compaction after it.
TURN = RECORDS[:BEGIN]
COMPACTION = RECORDS[BEGIN:]
PROMPT = "".join(part["text"] for part in RECORDS[0]["input"])
SESSION = "session_585d0219-06f0-4cbf-a548-39b7e1a1018d"
MODEL = "kimi-code/k3.1-pro"
WINDOW = 1_048_576
TURN_USAGE = [
    raw for raw in TURN
    if raw["type"] == "usage.record" and raw["usageScope"] == "turn"
]
APPLIED = next(raw for raw in COMPACTION if raw["type"] == "context.apply_compaction")
SESSION_USAGE = next(
    raw for raw in COMPACTION
    if raw["type"] == "usage.record" and raw["usageScope"] == "session"
)
SENTINELS = ("PRIVATE KIMI SUMMARY TEXT", "PRIVATE KIMI CONTEXT SUMMARY TEXT")


def kimi_home(root: Path) -> Path:
    """A Kimi data root with the model's window in its ``config.toml``."""
    home = root / "kimi-home"
    (home / "sessions").mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(
        f'default_model = "{MODEL}"\n\n'
        f'[models."{MODEL}"]\n'
        'provider = "managed:kimi-code"\n'
        'model = "k3.1-pro"\n'
        f"max_context_size = {WINDOW}\n"
    )
    return home


def turn_plan(records: list[dict[str, Any]]) -> dict[str, Any]:
    """A FakeKimiRunner plan that writes ``records`` as the turn's wire."""
    return {
        "session_ref": SESSION,
        "prompt_time": records[0]["time"],
        "after_prompt": records[1:],
        "stdout_lines": [
            {"role": "assistant", "content": "done"},
            {"role": "meta", "type": "session.resume_hint", "session_id": SESSION},
        ],
    }


def recorded_kimi_events(root: Path, worktree: Path, records=None) -> list:
    """The real Kimi adapter's events for one turn of the recorded wire."""
    home = kimi_home(root)
    runner = FakeKimiRunner(home / "sessions", worktree)
    runner.queue(**turn_plan(TURN if records is None else records))
    adapter = KimiAdapter(runner=runner, sessions_root=home / "sessions",
                          identity_timeout=1.0)
    context = ConversationContext(worktree=worktree)
    return list(adapter.stream(adapter.start(context, PROMPT)))


class FakeAcpProcess:
    def __init__(self) -> None:
        self.pid = 4242
        self.returncode: int | None = None
        self.signals: list[int] = []

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def send_signal(self, value: int) -> None:
        self.signals.append(value)
        self.returncode = -value


class FakeAcp:
    """``kimi acp`` as the gate saw it: ``/compact`` answers at once and the
    compaction's records land on the main wire."""

    def __init__(self, wire: Path, appended: list[dict[str, Any]], *,
                 said: str = "Context compaction started.") -> None:
        self.wire = wire
        self.appended = appended
        self.said = said
        self.process = FakeAcpProcess()
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.agent_text: list[str] = ["history replayed by session/load"]
        self.closed = False

    def request(self, method: str, params, timeout=None) -> Any:
        self.requests.append((method, dict(params)))
        if method == "session/prompt":
            self.agent_text.append(self.said)
            with self.wire.open("a", encoding="utf-8") as stream:
                for raw in self.appended:
                    stream.write(json.dumps(raw) + "\n")
            return {"stopReason": "end_turn"}
        return {}

    def close(self) -> None:
        self.closed = True
        if self.process.returncode is None:
            self.process.returncode = 0


class KimiCase(DispatchCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = kimi_home(self.root)
        self.sessions = self.home / "sessions"
        self.runner = FakeKimiRunner(self.sessions, self.worktree)
        self.acps: list[FakeAcp] = []
        self.acp_records: list[dict[str, Any]] = COMPACTION
        self.acp_said = "Context compaction started."

    def acp_factory(self, argv, *, cwd, env):
        self.acp_argv = (list(argv), Path(cwd))
        wire = next(self.sessions.glob(f"*/{SESSION}")) / "agents/main/wire.jsonl"
        acp = FakeAcp(wire, self.acp_records, said=self.acp_said)
        self.acps.append(acp)
        return acp

    def adapter(self, **kwargs: Any) -> KimiAdapter:
        return KimiAdapter(
            runner=self.runner,
            sessions_root=self.sessions,
            identity_timeout=1.0,
            acp_factory=self.acp_factory,
            acp_settle_seconds=0,
            **kwargs,
        )

    def context(self) -> ConversationContext:
        return ConversationContext(worktree=self.worktree)

    def stored_session(self) -> None:
        """The chat's Kimi session on disk: the recorded turn, no compaction."""
        self.runner.write_session(
            self.sessions, SESSION, self.worktree, PROMPT,
            prompt_time=TURN[0]["time"], after_prompt=TURN[1:],
        )

    def events(self, conversation_id: str) -> list[dict[str, Any]]:
        con = self.connect()
        rows = con.execute(
            "SELECT sequence,event_type,payload,message_id,run_id "
            "FROM conversation_events WHERE conversation_id=? ORDER BY sequence",
            (conversation_id,),
        ).fetchall()
        con.close()
        return [
            {"sequence": row[0], "type": row[1], "payload": json.loads(row[2]),
             "message_id": row[3], "run_id": row[4], "raw": row[2]}
            for row in rows
        ]

    def wait_run(self, message_id: int) -> str:
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
                return row[0]
            time.sleep(0.01)
        self.fail(f"message {message_id} did not finish")


# ── AC9: the meter and the compaction event, from the recorded wire ────────


class KimiMeterAndEventTest(KimiCase):
    """AC9 through the adapter and the broker's store."""

    def test_one_request_reading_per_turn_record_with_its_native_time(self):
        events = recorded_kimi_events(self.root, self.worktree, RECORDS)
        readings = [
            event.payload for event in events
            if event.type == "usage" and event.payload["scope"] == "request"
        ]
        # Two model steps, two ``usageScope: turn`` records, two readings;
        # the compaction's ``session`` record gives none.
        self.assertEqual(len(TURN_USAGE), 2)
        self.assertEqual(len(readings), len(TURN_USAGE))
        for reading, record in zip(readings, TURN_USAGE):
            usage = record["usage"]
            self.assertEqual(reading["requested_at"], record["time"])
            self.assertEqual(
                reading["context_tokens"],
                usage["inputOther"] + usage["inputCacheRead"]
                + usage["inputCacheCreation"],
            )
            self.assertEqual(reading["context_window"], WINDOW)
            self.assertEqual(reading["tokens"], {
                "input_tokens": usage["inputOther"],
                "output_tokens": usage["output"],
                "cache_read_tokens": usage["inputCacheRead"],
                "cache_write_tokens": usage["inputCacheCreation"],
            })
        self.assertNotIn(
            SESSION_USAGE["time"], [reading["requested_at"] for reading in readings]
        )

    def test_the_turn_total_is_kept_with_scope_turn(self) -> None:
        events = recorded_kimi_events(self.root, self.worktree, RECORDS)
        [total] = [
            event.payload for event in events
            if event.type == "usage" and event.payload["scope"] == "turn"
        ]
        self.assertEqual(total["tokens"], {
            "input_tokens": sum(r["usage"]["inputOther"] for r in TURN_USAGE),
            "output_tokens": sum(r["usage"]["output"] for r in TURN_USAGE),
            "cache_read_tokens": sum(
                r["usage"]["inputCacheRead"] for r in TURN_USAGE),
            "cache_write_tokens": 0,
        })
        self.assertNotIn("context_tokens", total)
        self.assertNotIn("requested_at", total)
        # Readings first, in wire order, then the total, then the end.
        self.assertEqual(
            [event.type for event in events][-5:],
            ["usage", "usage", "context.compacted", "usage", "run.completed"],
        )

    def test_one_compacted_event_with_both_figures(self) -> None:
        events = recorded_kimi_events(self.root, self.worktree, RECORDS)
        [compacted] = [e for e in events if e.type == "context.compacted"]
        self.assertEqual(compacted.payload, {
            "trigger": "manual",   # full_compaction.begin.source
            "tokens_before": APPLIED["tokensBefore"],
            "tokens_after": APPLIED["tokensAfter"],
            "compaction_ref": f"{SESSION}@{APPLIED['time']}",
        })
        self.assertEqual(compacted.native_type, "context.apply_compaction")

    def test_no_window_is_guessed_without_kimis_config(self) -> None:
        (self.home / "config.toml").unlink()
        self.runner.queue(**turn_plan(TURN))
        adapter = self.adapter()
        events = list(adapter.stream(adapter.start(self.context(), PROMPT)))
        readings = [e.payload for e in events
                    if e.type == "usage" and e.payload["scope"] == "request"]
        self.assertEqual(len(readings), 2)
        for reading in readings:
            self.assertNotIn("context_window", reading)
            self.assertIn("context_tokens", reading)

    def test_through_the_store_no_payload_holds_a_summary(self) -> None:
        self.runner.queue(**turn_plan(RECORDS))
        broker = self.start_broker(lambda _harness: self.adapter())
        conversation_id = self.add_conversation(harness="kimi", state="idle")
        self.deliver(broker, conversation_id, PROMPT)
        stored = self.events(conversation_id)
        readings = [e["payload"] for e in stored if e["type"] == "usage"
                    and e["payload"]["scope"] == "request"]
        self.assertEqual(
            [r["requested_at"] for r in readings],
            [r["time"] for r in TURN_USAGE],
        )
        [total] = [e["payload"] for e in stored if e["type"] == "usage"
                   and e["payload"]["scope"] == "turn"]
        self.assertNotIn("context_tokens", total)
        [compacted] = [e["payload"] for e in stored
                       if e["type"] == "context.compacted"]
        self.assertEqual(
            (compacted["tokens_before"], compacted["tokens_after"]),
            (APPLIED["tokensBefore"], APPLIED["tokensAfter"]),
        )
        # Kimi compacted on its own inside the turn: no engine request.
        self.assertEqual(compacted["cause"], "harness")
        for event in stored:
            self.assertNotIn("summary", event["payload"])
            self.assertNotIn("contextSummary", event["payload"])
            for sentinel in SENTINELS:
                self.assertNotIn(sentinel, event["raw"])
        # The meter reads what the compaction left.
        with closing(db_driver.connect(self.db_path)) as con:
            meter = conversation_compaction.read_meter(con, conversation_id)
        self.assertEqual(meter["context_tokens"], APPLIED["tokensAfter"])
        self.assertEqual(meter["context_window"], WINDOW)


# ── AC9a: the manual trigger (ACP ``/compact``), behind the passed gate ────


class KimiManualTriggerTest(KimiCase):
    def test_the_gate_result_is_declared(self) -> None:
        self.assertTrue(KimiAdapter.manual_compaction)
        self.assertFalse(KimiAdapter.compact_window_native)
        self.assertTrue(conversation_compaction.manual_trigger_supported("kimi"))
        compact = KimiAdapter().manifest["conversation"]["compact"]
        self.assertEqual(compact["command"], ["acp"])
        self.assertEqual(compact["prompt"], "/compact")
        self.assertEqual(compact["checked_cli_version"], "2.5.3")

    def test_compact_asks_acp_for_slash_compact_on_the_stored_session(self):
        self.stored_session()
        adapter = self.adapter()
        turn = adapter.compact(SESSION, self.context())
        [acp] = self.acps
        self.assertEqual(self.acp_argv, (["kimi", "acp"], self.worktree.resolve()))
        self.assertEqual([method for method, _ in acp.requests], [
            "initialize", "session/load", "session/prompt",
        ])
        load, prompt = acp.requests[1][1], acp.requests[2][1]
        self.assertEqual(load["sessionId"], SESSION)
        self.assertEqual(load["cwd"], str(self.worktree.resolve()))
        self.assertEqual(prompt["prompt"], [{"type": "text", "text": "/compact"}])
        # The run is found by the compaction's begin record, as a turn is by
        # its prompt.
        begin = COMPACTION[0]
        self.assertTrue(turn.metadata["compaction"])
        self.assertEqual(turn.metadata["prompt_time"], begin["time"])
        self.assertTrue(turn.run_ref.startswith(f"kimi-{begin['time']}-"))
        self.assertFalse(acp.closed)   # held open until the compaction ends

        events = list(adapter.stream(turn))
        self.assertEqual([e.type for e in events], [
            "session.started", "run.started", "context.compacted", "usage",
            "run.completed",
        ])
        compacted, usage = events[2].payload, events[3].payload
        self.assertEqual(
            (compacted["trigger"], compacted["tokens_before"],
             compacted["tokens_after"]),
            ("manual", APPLIED["tokensBefore"], APPLIED["tokensAfter"]),
        )
        # The compaction request's own cost, read from its session record.
        self.assertEqual(usage["scope"], "compaction")
        self.assertEqual(usage["tokens"]["output_tokens"],
                         SESSION_USAGE["usage"]["output"])
        self.assertNotIn("context_tokens", usage)
        self.assertTrue(acp.closed)
        self.assertEqual(turn.metadata["terminal"], "run.completed")

    def test_a_cancelled_compaction_fails_with_the_harness_words(self) -> None:
        self.stored_session()
        self.acp_records = [
            COMPACTION[0], {"type": "full_compaction.cancel", "time": 9}
        ]
        adapter = self.adapter()
        events = list(adapter.stream(adapter.compact(SESSION, self.context())))
        self.assertEqual(events[-1].type, "run.failed")
        self.assertEqual(events[-1].payload["error"], "HARNESS_COMPACT_REFUSED")
        # Only what the agent said to /compact, not the replayed history.
        self.assertEqual(events[-1].payload["detail"], "Context compaction started.")
        self.assertNotIn("context.compacted", [e.type for e in events])

    def test_no_compaction_begun_is_a_refusal_and_closes_acp(self) -> None:
        self.stored_session()
        self.acp_records = []
        self.acp_said = "Nothing to compact."
        with self.assertRaises(AdapterError) as raised:
            self.adapter().compact(SESSION, self.context())
        self.assertEqual(raised.exception.code, "HARNESS_COMPACT_REFUSED")
        self.assertEqual(raised.exception.detail, "Nothing to compact.")
        self.assertTrue(self.acps[0].closed)

    def test_no_end_within_the_bound_is_not_reported(self) -> None:
        self.stored_session()
        self.acp_records = [COMPACTION[0]]
        adapter = self.adapter(compaction_timeout=0.2)
        events = list(adapter.stream(adapter.compact(SESSION, self.context())))
        self.assertEqual([e.type for e in events][-1], "run.completed")
        self.assertNotIn("context.compacted", [e.type for e in events])
        self.assertTrue(self.acps[0].closed)

    def test_a_recovered_compaction_reconciles_from_the_wire(self) -> None:
        self.stored_session()
        live = self.adapter().compact(SESSION, self.context())
        recovered = NativeTurn(
            harness="kimi", session_ref=SESSION, run_ref=live.run_ref,
            worktree=self.worktree.resolve(),
            metadata={"recovered": True, "compaction": True},
        )
        adapter = self.adapter()
        result = adapter.reconcile(recovered, self.context())
        self.assertEqual((result.outcome, result.proven), ("succeeded", True))
        [marker] = adapter.recovered_events(recovered, self.context())
        self.assertEqual(marker.payload["tokens_after"], APPLIED["tokensAfter"])
        self.assertEqual(marker.payload["compaction_ref"],
                         f"{SESSION}@{APPLIED['time']}")

    def test_the_button_compacts_a_kimi_chat_through_the_broker(self) -> None:
        self.runner.queue(**turn_plan(TURN))
        broker = self.start_broker(lambda _harness: self.adapter())
        conversation_id = self.add_conversation(harness="kimi", state="idle")
        self.deliver(broker, conversation_id, PROMPT)
        with closing(db_driver.connect(self.db_path)) as con:
            request = conversation_compaction.request_compaction(
                con, conversation_id, "operator",
                idempotency_key="press-1", user_id=1,
            )
        self.assertTrue(request.queued, request)
        broker.notify()
        self.assertEqual(self.wait_run(request.message_id), "succeeded")
        self.assertTrue(broker.wait_idle())
        stored = self.events(conversation_id)
        [asked] = [e["payload"] for e in stored
                   if e["type"] == "compaction.requested"]
        self.assertEqual((asked["cause"], asked["harness"]), ("operator", "kimi"))
        [marker] = [e for e in stored if e["type"] == "context.compacted"]
        self.assertEqual(marker["payload"]["cause"], "operator")
        self.assertEqual(marker["payload"]["request_message_id"],
                         request.message_id)
        self.assertEqual(
            (marker["payload"]["tokens_before"], marker["payload"]["tokens_after"]),
            (APPLIED["tokensBefore"], APPLIED["tokensAfter"]),
        )
        [cost] = [e["payload"] for e in stored if e["type"] == "usage"
                  and e["message_id"] == request.message_id]
        self.assertEqual(cost["scope"], "compaction")
        self.assertNotIn("compaction.failed", [e["type"] for e in stored])
        self.assertNotIn("compaction.not_reported", [e["type"] for e in stored])
        for event in stored:
            for sentinel in SENTINELS:
                self.assertNotIn(sentinel, event["raw"])
        # The next message re-grounds and still finds its own turn.
        self.deliver(broker, conversation_id, "next")
        self.assertEqual(len(self.reground_events(conversation_id)), 1)
