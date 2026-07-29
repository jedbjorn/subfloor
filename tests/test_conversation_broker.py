#!/usr/bin/env python3
"""Feature #24 event-driven broker ordering and crash-window contracts."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
SCRIPTS = ENGINE / "scripts"
sys.path.insert(0, str(SCRIPTS))

from conversation_adapters import (  # noqa: E402
    ConversationContext,
    InterruptResult,
    NativeTurn,
    NormalizedEvent,
    ReconcileResult,
)
from conversation_broker import (  # noqa: E402
    BrokerInvariantError,
    BrokerRun,
    BrokerStore,
    ConversationBroker,
)


def apply_schema(con: sqlite3.Connection) -> None:
    con.executescript((ENGINE / "schema.sql").read_text())
    for migration in sorted((ENGINE / "migrations").glob("*.sql")):
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")


class FakeAdapter:
    harness = "codex"

    def __init__(
        self,
        *,
        terminal: str = "run.completed",
        reconcile: ReconcileResult | None = None,
        block: bool = False,
    ) -> None:
        self.terminal = terminal
        self.reconcile_result = reconcile or ReconcileResult(
            "succeeded", True, "fake exact run completed"
        )
        self.block = block
        self.interrupted = threading.Event()
        self.started = 0
        self.resumed = 0
        self.reconciled = 0
        self.closed = 0

    def start(
        self,
        context: ConversationContext,
        message: str,
    ) -> NativeTurn:
        self.started += 1
        return NativeTurn(
            harness=self.harness,
            session_ref="native-session",
            run_ref=f"native-run-{self.started}",
            worktree=context.checked_worktree(),
        )

    def resume(
        self,
        session_ref: str,
        context: ConversationContext,
        message: str,
    ) -> NativeTurn:
        self.resumed += 1
        return NativeTurn(
            harness=self.harness,
            session_ref=session_ref,
            run_ref=f"native-resume-{self.resumed}",
            worktree=context.checked_worktree(),
        )

    def stream(self, turn: NativeTurn) -> Iterator[NormalizedEvent]:
        yield NormalizedEvent("session.started", {"session_ref": turn.session_ref})
        yield NormalizedEvent("run.started", {"status": "running"})
        yield NormalizedEvent("assistant.delta", {"text": "hello"})
        if self.block:
            self.interrupted.wait(5)
        event_type = "run.interrupted" if self.interrupted.is_set() else self.terminal
        yield NormalizedEvent(event_type, {"status": event_type.split(".")[1]})

    def interrupt(self, turn: NativeTurn) -> InterruptResult:
        self.interrupted.set()
        return InterruptResult(True)

    def reconcile(
        self,
        turn: NativeTurn,
        context: ConversationContext,
    ) -> ReconcileResult:
        self.reconciled += 1
        return self.reconcile_result

    def close(self) -> None:
        self.closed += 1


class ConversationBrokerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "shell.db"
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        con = sqlite3.connect(self.db_path)
        apply_schema(con)
        con.execute("INSERT INTO users (user_id,username) VALUES (1,'operator')")
        for shell_id in (1, 2):
            con.execute(
                "INSERT INTO shells "
                "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
                "VALUES (?,?,?,?,?,1)",
                (
                    shell_id,
                    f"Shell {shell_id}",
                    f"sh{shell_id}",
                    "dev",
                    "prompt",
                ),
            )
        con.commit()
        con.close()
        self.serial = 0
        self.brokers: list[ConversationBroker] = []

    def tearDown(self) -> None:
        for broker in self.brokers:
            broker.stop()
            broker.join(2)
        self.tmp.cleanup()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def allow_legacy_duplicate_open_chats(self) -> None:
        """Exercise the broker's independent lock against pre-migration data."""
        con = self.connect()
        con.execute("DROP INDEX idx_conversations_live_normal_shell")
        con.commit()
        con.close()

    def add_conversation(
        self,
        *,
        shell_id: int = 1,
        state: str = "queued",
        session_ref: str | None = None,
    ) -> str:
        self.serial += 1
        key = f"conversation-{self.serial}"
        con = self.connect()
        con.execute(
            "INSERT INTO conversations "
            "(shell_id,owner_user_id,harness,worktree,state,"
            "harness_session_ref,creation_idempotency_key,"
            "creation_request_hash) VALUES (?,1,'codex',?,?,?,?,?)",
            (
                shell_id,
                str(self.worktree),
                state,
                session_ref,
                key,
                f"hash-{key}",
            ),
        )
        conversation_id = con.execute(
            "SELECT conversation_id FROM conversations "
            "WHERE creation_idempotency_key=?",
            (key,),
        ).fetchone()[0]
        con.commit()
        con.close()
        return conversation_id

    def add_message(self, conversation_id: str, *, state: str = "queued") -> int:
        self.serial += 1
        key = f"message-{self.serial}"
        con = self.connect()
        message_id = con.execute(
            "INSERT INTO conversation_messages "
            "(conversation_id,sender_kind,sender_ref,message_kind,body,"
            "idempotency_key,request_hash,state) "
            "VALUES (?,'user','1','prompt','hello',?,?,?)",
            (conversation_id, key, f"hash-{key}", state),
        ).lastrowid
        con.execute(
            "INSERT INTO conversation_outbox (conversation_id,message_id) VALUES (?,?)",
            (conversation_id, message_id),
        )
        con.commit()
        con.close()
        return int(message_id)

    def add_live_run(
        self,
        *,
        state: str,
        session_after: str | None = None,
        runner_ref: str | None = None,
        expired: bool = True,
    ) -> tuple[str, int, int]:
        conversation_id = self.add_conversation(state="running")
        message_id = self.add_message(conversation_id, state="running")
        con = self.connect()
        started_at = None if state == "leased" else "2026-07-29 00:00:00"
        run_id = con.execute(
            "INSERT INTO conversation_runs "
            "(conversation_id,shell_id,trigger_message_id,"
            "harness_session_after,runner_ref,state,lease_owner,"
            "lease_expires_at,started_at,heartbeat_at) "
            "VALUES (?,1,?,?,?,?,?,?,?,?)",
            (
                conversation_id,
                message_id,
                session_after,
                runner_ref,
                state,
                "dead-broker",
                ("2000-01-01 00:00:00" if expired else "2999-01-01 00:00:00"),
                started_at,
                started_at,
            ),
        ).lastrowid
        con.execute(
            "UPDATE conversation_outbox SET state='claimed',claim_owner='dead',"
            "claimed_at='2026-07-29 00:00:00',"
            "lease_expires_at='2026-07-29 00:10:00' WHERE message_id=?",
            (message_id,),
        )
        con.execute(
            "UPDATE conversation_outbox SET state='dispatched',run_id=?,"
            "dispatched_at='2026-07-29 00:00:01' WHERE message_id=?",
            (run_id, message_id),
        )
        con.commit()
        con.close()
        return conversation_id, message_id, int(run_id)

    def start_broker(
        self,
        factory,
        *,
        heartbeat_seconds: int = 60,
        recovery_seconds: int = 60,
    ) -> ConversationBroker:
        broker = ConversationBroker(
            self.db_path,
            adapter_factory=factory,
            owner=f"test-broker-{len(self.brokers) + 1}",
            heartbeat_seconds=heartbeat_seconds,
            recovery_seconds=recovery_seconds,
        )
        self.brokers.append(broker)
        broker.start()
        self.assertTrue(broker.wait_started())
        return broker

    def wait_run_state(self, run_id: int, state: str, timeout: float = 3) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            con = self.connect()
            row = con.execute(
                "SELECT state FROM conversation_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            con.close()
            if row is not None and row[0] == state:
                return
            time.sleep(0.01)
        self.fail(f"run {run_id} did not reach {state}")


class StoreContractTest(ConversationBrokerCase):
    def test_prepared_shell_archive_is_bound_while_the_run_is_leased(self) -> None:
        conversation_id = self.add_conversation()
        self.add_message(conversation_id)
        con = self.connect()
        con.execute(
            "INSERT INTO shell_memory_archives "
            "(archive_id,shell_id,session_id,date) VALUES (42,1,'session-42','2026-07-29')"
        )
        con.commit()
        con.close()
        store = BrokerStore(self.db_path)
        run = store.claim_next("broker")

        store.bind_archive(run.run_id, "broker", 42)

        con = self.connect()
        row = con.execute(
            "SELECT archive_id,state FROM conversation_runs WHERE run_id=?",
            (run.run_id,),
        ).fetchone()
        con.close()
        self.assertEqual(tuple(row), (42, "leased"))

    def test_turns_are_ordered_and_terminal_commit_queues_the_next(self) -> None:
        conversation_id = self.add_conversation()
        first = self.add_message(conversation_id)
        second = self.add_message(conversation_id)
        store = BrokerStore(self.db_path)

        run = store.claim_next("broker")
        self.assertIsNotNone(run)
        self.assertEqual(run.message_id, first)
        self.assertIsNone(store.claim_next("broker"))

        store.mark_starting(run.run_id, "broker")
        store.mark_native_started(
            run.run_id,
            "broker",
            NativeTurn(
                "codex",
                "native-session",
                "native-run-1",
                self.worktree,
            ),
        )
        store.append_event(
            run.run_id,
            NormalizedEvent("run.started", {"status": "running"}),
        )
        store.finish_run(
            run.run_id,
            "succeeded",
            event_type="run.completed",
        )

        next_run = store.claim_next("broker")
        self.assertIsNotNone(next_run)
        self.assertEqual(next_run.message_id, second)
        con = self.connect()
        first_state = con.execute(
            "SELECT state FROM conversation_messages WHERE message_id=?",
            (first,),
        ).fetchone()[0]
        sequences = [
            row[0]
            for row in con.execute(
                "SELECT sequence FROM conversation_events "
                "WHERE conversation_id=? ORDER BY sequence",
                (conversation_id,),
            )
        ]
        con.close()
        self.assertEqual(first_state, "completed")
        self.assertEqual(sequences, [1, 2])

    def test_shell_mutation_lock_blocks_other_conversation(self) -> None:
        self.allow_legacy_duplicate_open_chats()
        first_conversation = self.add_conversation(shell_id=1)
        second_conversation = self.add_conversation(shell_id=1)
        self.add_message(first_conversation)
        self.add_message(second_conversation)
        store = BrokerStore(self.db_path)

        first = store.claim_next("broker")
        self.assertIsNotNone(first)
        self.assertIsNone(store.claim_next("broker"))
        store.finish_run(
            first.run_id,
            "failed",
            event_type="run.failed",
            error_code="TEST",
        )
        second = store.claim_next("broker")
        self.assertIsNotNone(second)
        self.assertEqual(second.conversation_id, second_conversation)

    def test_different_shells_can_hold_live_runs(self) -> None:
        first_conversation = self.add_conversation(shell_id=1)
        second_conversation = self.add_conversation(shell_id=2)
        self.add_message(first_conversation)
        self.add_message(second_conversation)
        store = BrokerStore(self.db_path)

        first = store.claim_next("broker")
        second = store.claim_next("broker")
        self.assertEqual(
            {first.shell_id, second.shell_id},
            {1, 2},
        )

    def test_concurrent_claims_cannot_bypass_shell_lock(self) -> None:
        self.allow_legacy_duplicate_open_chats()
        first_conversation = self.add_conversation(shell_id=1)
        second_conversation = self.add_conversation(shell_id=1)
        self.add_message(first_conversation)
        self.add_message(second_conversation)
        barrier = threading.Barrier(3)
        claimed: list[BrokerRun | None] = []

        def claim(owner: str) -> None:
            barrier.wait()
            claimed.append(BrokerStore(self.db_path).claim_next(owner))

        workers = [
            threading.Thread(target=claim, args=(f"broker-{index}",))
            for index in (1, 2)
        ]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join()

        self.assertEqual(sum(run is not None for run in claimed), 1)
        con = self.connect()
        self.assertEqual(
            con.execute(
                "SELECT COUNT(*) FROM conversation_runs "
                "WHERE state IN ('leased','starting','running')"
            ).fetchone()[0],
            1,
        )
        con.close()

    def test_startup_does_not_steal_an_unexpired_lease(self) -> None:
        _conversation, _message, run_id = self.add_live_run(
            state="running",
            session_after="native-session",
            runner_ref="native-run-existing",
            expired=False,
        )
        store = BrokerStore(self.db_path)

        self.assertEqual(
            store.adopt_recoverable("new-broker", startup=True, limit=8),
            [],
        )
        con = self.connect()
        row = con.execute(
            "SELECT lease_owner,state FROM conversation_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        con.close()
        self.assertEqual(tuple(row), ("dead-broker", "running"))

    def test_expired_pre_run_claim_is_requeued(self) -> None:
        conversation_id = self.add_conversation()
        message_id = self.add_message(conversation_id)
        con = self.connect()
        con.execute(
            "UPDATE conversation_outbox SET state='claimed',claim_owner='dead',"
            "claimed_at='2000-01-01 00:00:00',"
            "lease_expires_at='2000-01-01 00:00:01' WHERE message_id=?",
            (message_id,),
        )
        con.commit()
        con.close()
        store = BrokerStore(
            self.db_path,
            clock=lambda: datetime(2026, 7, 29, tzinfo=timezone.utc),
        )

        self.assertEqual(store.requeue_expired_claims(), 1)
        run = store.claim_next("broker")
        self.assertIsNotNone(run)

    def test_native_identity_capture_rejects_a_different_worktree(self) -> None:
        conversation_id = self.add_conversation()
        self.add_message(conversation_id)
        store = BrokerStore(self.db_path)
        run = store.claim_next("broker")
        store.mark_starting(run.run_id, "broker")
        other = self.root / "other-worktree"
        other.mkdir()

        with self.assertRaises(BrokerInvariantError) as caught:
            store.mark_native_started(
                run.run_id,
                "broker",
                NativeTurn(
                    "codex",
                    "native-session",
                    "native-run",
                    other,
                ),
            )
        self.assertEqual(caught.exception.code, "HARNESS_WORKTREE_MISMATCH")
        con = self.connect()
        row = con.execute(
            "SELECT state,harness_session_after FROM conversation_runs WHERE run_id=?",
            (run.run_id,),
        ).fetchone()
        con.close()
        self.assertEqual(tuple(row), ("starting", None))


class ServiceContractTest(ConversationBrokerCase):
    def test_routine_dispatch_requires_post_commit_notification(self) -> None:
        adapters: list[FakeAdapter] = []

        def factory(_harness: str) -> FakeAdapter:
            adapter = FakeAdapter()
            adapters.append(adapter)
            return adapter

        broker = self.start_broker(factory)
        conversation_id = self.add_conversation()
        message_id = self.add_message(conversation_id)
        time.sleep(0.1)
        con = self.connect()
        self.assertEqual(
            con.execute("SELECT COUNT(*) FROM conversation_runs").fetchone()[0],
            0,
        )
        con.close()

        broker.notify()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            con = self.connect()
            row = con.execute(
                "SELECT r.run_id,r.state FROM conversation_runs r "
                "WHERE r.trigger_message_id=?",
                (message_id,),
            ).fetchone()
            con.close()
            if row is not None and row["state"] == "succeeded":
                break
            time.sleep(0.01)
        else:
            self.fail("post-commit notification did not dispatch the turn")
        self.assertEqual(len(adapters), 1)
        self.assertEqual(adapters[0].started, 1)

    def test_interrupt_intent_precedes_terminal_interruption(self) -> None:
        adapter = FakeAdapter(block=True)
        broker = self.start_broker(lambda _harness: adapter)
        conversation_id = self.add_conversation()
        self.add_message(conversation_id)
        broker.notify()
        deadline = time.monotonic() + 3
        run_id = None
        while time.monotonic() < deadline:
            active = broker.active_run_ids()
            if active:
                run_id = active[0]
                con = self.connect()
                state = con.execute(
                    "SELECT state FROM conversation_runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0]
                con.close()
                if state == "running":
                    break
            time.sleep(0.01)
        self.assertIsNotNone(run_id)

        self.assertTrue(broker.interrupt(run_id))
        self.wait_run_state(run_id, "cancelled")
        con = self.connect()
        events = [
            row[0]
            for row in con.execute(
                "SELECT event_type FROM conversation_events "
                "WHERE run_id=? ORDER BY sequence",
                (run_id,),
            )
        ]
        con.close()
        self.assertLess(
            events.index("run.interrupt.requested"),
            events.index("run.interrupted"),
        )

    def test_starting_crash_without_exact_identity_becomes_unknown(self) -> None:
        _conversation, _message, run_id = self.add_live_run(state="starting")
        adapter = FakeAdapter()
        broker = self.start_broker(lambda _harness: adapter)
        self.wait_run_state(run_id, "unknown")
        self.assertEqual(adapter.started, 0)
        self.assertEqual(adapter.resumed, 0)
        self.assertEqual(adapter.reconciled, 0)
        self.assertTrue(broker.wait_idle())

    def test_running_crash_reconciles_exact_run_without_replay(self) -> None:
        _conversation, _message, run_id = self.add_live_run(
            state="running",
            session_after="native-session",
            runner_ref="native-run-existing",
        )
        adapter = FakeAdapter(
            reconcile=ReconcileResult(
                "succeeded",
                True,
                "thread/read proved the exact native run completed",
            )
        )
        broker = self.start_broker(lambda _harness: adapter)
        self.wait_run_state(run_id, "succeeded")
        self.assertEqual(adapter.started, 0)
        self.assertEqual(adapter.resumed, 0)
        self.assertEqual(adapter.reconciled, 1)
        self.assertTrue(broker.wait_idle())

    def test_leased_crash_is_safe_to_start_once(self) -> None:
        _conversation, _message, run_id = self.add_live_run(state="leased")
        adapter = FakeAdapter()
        broker = self.start_broker(lambda _harness: adapter)
        self.wait_run_state(run_id, "succeeded")
        self.assertEqual(adapter.started, 1)
        self.assertEqual(adapter.resumed, 0)
        self.assertTrue(broker.wait_idle())


if __name__ == "__main__":
    unittest.main()
