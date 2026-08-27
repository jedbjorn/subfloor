#!/usr/bin/env python3
"""Feature #24 event-driven broker ordering and crash-window contracts."""

from __future__ import annotations

import concurrent.futures
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
SCRIPTS = ENGINE / "scripts"
sys.path.insert(0, str(SCRIPTS))

import conversation_boot  # noqa: E402
from conversation_adapters import (  # noqa: E402
    AdapterError,
    ConversationContext,
    InterruptResult,
    KimiAdapter,
    NativeTurn,
    NormalizedEvent,
    OpenCodeAdapter,
    ReconcileResult,
)
from conversation_adapters import base as base_adapter
from conversation_boot import BootDirective  # noqa: E402
from conversation_broker import (  # noqa: E402
    BrokerInvariantError,
    BrokerRun,
    BrokerStore,
    ConversationBroker,
    unexpected_error_code,
)


def apply_schema(con: sqlite3.Connection) -> None:
    con.executescript((ENGINE / "schema.sql").read_text())
    for migration in sorted((ENGINE / "migrations").glob("*.sql")):
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")


def test_unexpected_error_code_retains_only_exception_class() -> None:
    secret = "opaque-provider-secret"

    code = unexpected_error_code(KeyError(secret))

    assert code == "BROKER_RUN_KEYERROR"
    assert secret not in code


class FakeAdapter:
    harness = "codex"

    def __init__(
        self,
        *,
        terminal: str = "run.completed",
        reconcile: ReconcileResult | None = None,
        block: bool = False,
        harness: str = "codex",
        native_session_ref: str = "native-session",
        native_run_ref: str | None = None,
        interrupt_terminal: str = "run.interrupted",
    ) -> None:
        self.harness = harness
        self.terminal = terminal
        self.reconcile_result = reconcile or ReconcileResult(
            "succeeded", True, "fake exact run completed"
        )
        self.block = block
        self.native_session_ref = native_session_ref
        self.native_run_ref = native_run_ref
        self.interrupt_terminal = interrupt_terminal
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
            session_ref=self.native_session_ref,
            run_ref=self.native_run_ref or f"native-run-{self.started}",
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
        event_type = (
            self.interrupt_terminal
            if self.interrupted.is_set()
            else self.terminal
        )
        yield NormalizedEvent(
            event_type,
            {"status": event_type.split(".")[1]},
            interrupt_evidence=(
                "operator" if event_type == "run.interrupted" else None
            ),
        )

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


class BarrierAdapter(FakeAdapter):
    """Prove separate instances of one harness can stream concurrently."""

    def __init__(self, barrier: threading.Barrier, serial: int) -> None:
        super().__init__(native_session_ref=f"native-session-{serial}")
        self.barrier = barrier
        self.crossed = False

    def stream(self, turn: NativeTurn) -> Iterator[NormalizedEvent]:
        yield NormalizedEvent("session.started", {"session_ref": turn.session_ref})
        yield NormalizedEvent("run.started", {"status": "running"})
        self.barrier.wait(2)
        self.crossed = True
        yield NormalizedEvent("assistant.delta", {"text": "hello"})
        yield NormalizedEvent("run.completed", {"status": "completed"})


class SparseDeltaAdapter(FakeAdapter):
    """Pause after one delta so the broker must flush on its own deadline."""

    def __init__(self) -> None:
        super().__init__()
        self.delta_yielded = threading.Event()
        self.release = threading.Event()

    def stream(self, turn: NativeTurn) -> Iterator[NormalizedEvent]:
        yield NormalizedEvent("session.started", {"session_ref": turn.session_ref})
        yield NormalizedEvent("run.started", {"status": "running"})
        yield NormalizedEvent("assistant.delta", {"text": "visible promptly"})
        self.delta_yielded.set()
        self.release.wait(2)
        yield NormalizedEvent("run.completed", {"status": "completed"})


class ReconcileSequenceAdapter(FakeAdapter):
    """End the stream uncertain, then prove running and terminal states."""

    def __init__(self) -> None:
        super().__init__()
        self.results = iter(
            [
                ReconcileResult("running", True, "native run is live"),
                ReconcileResult("succeeded", True, "native run completed"),
            ]
        )

    def stream(self, turn: NativeTurn) -> Iterator[NormalizedEvent]:
        yield NormalizedEvent("session.started", {"session_ref": turn.session_ref})
        yield NormalizedEvent("assistant.delta", {"text": "before recovery"})

    def reconcile(
        self,
        turn: NativeTurn,
        context: ConversationContext,
    ) -> ReconcileResult:
        self.reconciled += 1
        return next(self.results)


class PostActivityStreamErrorAdapter(ReconcileSequenceAdapter):
    """Lose a Kimi-shaped stream after activity, then reconcile exactly."""

    def __init__(self) -> None:
        super().__init__()
        self.harness = "kimi"

    def stream(self, turn: NativeTurn) -> Iterator[NormalizedEvent]:
        yield NormalizedEvent("session.started", {"session_ref": turn.session_ref})
        yield NormalizedEvent("run.started", {"status": "running"})
        yield NormalizedEvent("assistant.delta", {"text": "before stream loss"})
        raise OSError("Kimi stdout stream closed while the process was live")


class BlockingOpenCodeTransport:
    """Hold synchronous message delivery until the broker calls abort."""

    def __init__(self) -> None:
        self.session_ref = "ses_interruptible"
        self.message_started = threading.Event()
        self.aborted = threading.Event()
        self.requests: list[tuple[str, str]] = []
        self.message_count = 0
        self.stream_count = 0

    def request(self, method: str, path: str, *, query=None, body=None):
        self.requests.append((method, path))
        if method == "POST" and path == "/session":
            return {"id": self.session_ref}
        if method == "POST" and path.endswith("/message"):
            self.message_count += 1
            self.message_started.set()
            if self.message_count == 1 and not self.aborted.wait(2):
                raise AssertionError("OpenCode message remained uninterruptible")
            return {"id": f"message-{self.message_count}"}
        if method == "POST" and path.endswith("/abort"):
            self.aborted.set()
            # Let the blocked message response and native idle race ahead of
            # the abort acknowledgement. The adapter must wait for this
            # result before choosing completed versus interrupted.
            time.sleep(0.05)
            return True
        if method == "GET" and path == f"/session/{self.session_ref}":
            return {"id": self.session_ref}
        if method == "GET" and path == "/session/status":
            return {
                self.session_ref: {
                    "type": "idle" if self.aborted.is_set() else "busy"
                }
            }
        raise AssertionError(f"unexpected OpenCode request: {method} {path}")

    def stream(self, path: str, *, query=None):
        self.stream_count += 1
        stream_number = self.stream_count

        def events():
            yield {
                "type": "session.status",
                "properties": {
                    "sessionID": self.session_ref,
                    "status": {"type": "busy"},
                },
            }
            if stream_number == 1:
                if not self.aborted.wait(2):
                    raise AssertionError("OpenCode event stream saw no abort")
                yield {
                    "type": "session.idle",
                    "properties": {"sessionID": self.session_ref},
                }
                return
            yield {
                "type": "message.part.delta",
                "properties": {
                    "sessionID": self.session_ref,
                    "field": "text",
                    "delta": f"resumed-{stream_number}",
                },
            }
            yield {
                "type": "session.idle",
                "properties": {"sessionID": self.session_ref},
            }

        return events()


class StubUrlResponse:
    def __init__(self, payload: bytes = b"", *, lines=()) -> None:
        self.payload = payload
        self.lines = tuple(lines)

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def read(self) -> bytes:
        return self.payload

    def __iter__(self):
        return iter(self.lines)

    def close(self) -> None:
        pass


class RejectingOpenCodeFirstTurnEndpoint:
    """Serve an idle session whose real HTTP prompt request returns 400."""

    def __init__(self) -> None:
        self.session_ref = "ses_empty_first_turn"
        self.prompt_count = 0
        self.native_messages: list[dict] = []
        self.requests: list[tuple[str, str]] = []

    def urlopen(self, request, *, timeout):
        method = request.get_method()
        path = urllib.parse.urlsplit(request.full_url).path
        self.requests.append((method, path))
        if method == "POST" and path == "/session":
            return StubUrlResponse(
                json.dumps({"id": self.session_ref}).encode()
            )
        if method == "GET" and path == "/event":
            return StubUrlResponse(
                lines=(
                    (
                        "data: "
                        + json.dumps({
                            "type": "session.idle",
                            "properties": {"sessionID": self.session_ref},
                        })
                        + "\n"
                    ).encode(),
                    b"\n",
                )
            )
        if method == "POST" and path.endswith("/message"):
            self.prompt_count += 1
            raise urllib.error.HTTPError(
                request.full_url,
                400,
                "Bad Request",
                {},
                None,
            )
        if method == "GET" and path == f"/session/{self.session_ref}":
            return StubUrlResponse(
                json.dumps({"id": self.session_ref}).encode()
            )
        if method == "GET" and path == "/session/status":
            return StubUrlResponse(
                json.dumps({self.session_ref: {"type": "idle"}}).encode()
            )
        raise AssertionError(f"unexpected OpenCode request: {method} {path}")


class ConcurrentOpenCodeFirstTurnTransport:
    """Credential-free exact-session proof for parallel OpenCode prompts."""

    def __init__(self, barrier: threading.Barrier, serial: int) -> None:
        self.barrier = barrier
        self.session_ref = f"ses_parallel_{serial}"
        self.prompt_count = 0
        self.requests: list[tuple[str, str, dict | None]] = []
        self.crossed = False

    def request(self, method: str, path: str, *, query=None, body=None):
        self.requests.append((method, path, body))
        if method == "POST" and path == "/session":
            return {"id": self.session_ref}
        if method == "POST" and path.endswith("/message"):
            self.prompt_count += 1
            return {
                "info": {
                    "role": "assistant",
                    "sessionID": self.session_ref,
                },
                "parts": [{"type": "text", "text": self.session_ref}],
            }
        raise AssertionError(f"unexpected OpenCode request: {method} {path}")

    def stream(self, path: str, *, query=None):
        def events():
            yield {
                "type": "session.idle",
                "properties": {"sessionID": self.session_ref},
            }
            yield {
                "type": "session.status",
                "properties": {
                    "sessionID": self.session_ref,
                    "status": {"type": "busy"},
                },
            }
            self.barrier.wait(2)
            self.crossed = True
            yield {
                "type": "message.part.delta",
                "properties": {
                    "sessionID": self.session_ref,
                    "field": "text",
                    "delta": self.session_ref,
                },
            }
            yield {
                "type": "session.idle",
                "properties": {"sessionID": self.session_ref},
            }

        return events()


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
        for broker in self.brokers:
            broker.join(2)
            self.assertFalse(broker.is_alive(), "broker dispatcher did not stop")
            self.assertTrue(
                broker.wait_idle(2),
                "broker worker did not stop before fixture cleanup",
            )
        self.tmp.cleanup()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def allow_legacy_duplicate_open_chats(self) -> None:
        """Exercise the broker's independent lock against pre-migration data."""
        con = self.connect()
        con.execute("DROP INDEX idx_conversations_one_open_shell")
        con.commit()
        con.close()

    def add_conversation(
        self,
        *,
        shell_id: int = 1,
        state: str = "queued",
        session_ref: str | None = None,
        harness: str = "codex",
        provider: str | None = None,
        model: str | None = None,
        title: str | None = None,
    ) -> str:
        self.serial += 1
        key = f"conversation-{self.serial}"
        con = self.connect()
        con.execute(
            "INSERT INTO conversations "
            "(shell_id,owner_user_id,harness,provider,model,title,worktree,state,"
            "harness_session_ref,creation_idempotency_key,"
            "creation_request_hash) VALUES (?,1,?,?,?,?,?,?,?,?,?)",
            (
                shell_id,
                harness,
                provider,
                model,
                title,
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
        if state != "closed":
            con.execute(
                "INSERT OR REPLACE INTO active_shell_chats (shell_id,chat_id) "
                "VALUES (?,?)",
                (shell_id, conversation_id),
            )
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
        harness: str = "codex",
    ) -> tuple[str, int, int]:
        conversation_id = self.add_conversation(
            state="running",
            harness=harness,
        )
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
        **kwargs,
    ) -> ConversationBroker:
        broker = ConversationBroker(
            self.db_path,
            adapter_factory=factory,
            owner=f"test-broker-{len(self.brokers) + 1}",
            heartbeat_seconds=heartbeat_seconds,
            recovery_seconds=recovery_seconds,
            **kwargs,
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
    def test_normal_run_finishes_without_sprint_domain_writes(self) -> None:
        conversation_id = self.add_conversation()
        self.add_message(conversation_id)
        con = self.connect()
        absent_tables = (
            "sprint_assignment_results",
            "sprint_cancellations",
            "sprint_conversation_bindings",
        )
        present = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertTrue(set(absent_tables).isdisjoint(present))
        self.assertTrue({"sprints", "sprint_events"}.issubset(present))
        self.assertEqual(0, con.execute("SELECT COUNT(*) FROM sprints").fetchone()[0])
        self.assertEqual(
            0, con.execute("SELECT COUNT(*) FROM sprint_events").fetchone()[0]
        )
        con.close()

        store = BrokerStore(self.db_path)
        run = store.claim_next("broker")
        self.assertEqual(run.conversation_id, conversation_id)
        store.mark_starting(run.run_id, "broker")
        store.mark_native_started(
            run.run_id,
            "broker",
            NativeTurn("codex", "session", "runner", self.worktree),
        )
        self.assertTrue(
            store.finish_run(
                run.run_id,
                "succeeded",
                event_type="run.completed",
            )
        )

        con = self.connect()
        try:
            state = con.execute(
                "SELECT state FROM conversations WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()[0]
            self.assertEqual(state, "idle")
            self.assertEqual(
                con.execute(
                    "SELECT state FROM conversation_runs WHERE run_id=?",
                    (run.run_id,),
                ).fetchone()[0],
                "succeeded",
            )
            self.assertEqual(
                (0, 0),
                (
                    con.execute("SELECT COUNT(*) FROM sprints").fetchone()[0],
                    con.execute("SELECT COUNT(*) FROM sprint_events").fetchone()[0],
                ),
            )
        finally:
            con.close()

    def test_native_start_registers_process_and_finalize_clears_it(self) -> None:
        conversation_id = self.add_conversation()
        self.add_message(conversation_id)
        store = BrokerStore(self.db_path)
        run = store.claim_next("broker")
        store.mark_starting(run.run_id, "broker")

        store.mark_native_started(
            run.run_id,
            "broker",
            NativeTurn(
                "codex",
                "native-session",
                "native-run",
                self.worktree,
                process_ref=str(os.getpid()),
            ),
        )

        con = self.connect()
        active = con.execute(
            "SELECT chat_id,process_pid,process_start_ticks "
            "FROM active_shell_chats WHERE shell_id=1"
        ).fetchone()
        run_row = con.execute(
            "SELECT state,process_pid,process_start_ticks,process_group_id "
            "FROM conversation_runs WHERE run_id=?",
            (run.run_id,),
        ).fetchone()
        con.close()
        self.assertEqual(active["chat_id"], conversation_id)
        self.assertEqual(active["process_pid"], os.getpid())
        self.assertGreater(active["process_start_ticks"], 0)
        self.assertEqual(run_row["state"], "running")
        self.assertEqual(run_row["process_pid"], os.getpid())
        self.assertEqual(
            run_row["process_start_ticks"],
            active["process_start_ticks"],
        )
        self.assertEqual(run_row["process_group_id"], os.getpgid(os.getpid()))

        store.finish_run(
            run.run_id,
            "succeeded",
            event_type="run.completed",
        )

        con = self.connect()
        finalized = con.execute(
            "SELECT r.state,a.process_pid,a.process_start_ticks "
            "FROM conversation_runs r JOIN active_shell_chats a "
            "ON a.shell_id=r.shell_id AND a.chat_id=r.conversation_id "
            "WHERE r.run_id=?",
            (run.run_id,),
        ).fetchone()
        con.close()
        self.assertEqual(tuple(finalized), ("succeeded", None, None))

    def test_recovery_leaves_unprotected_process_identity_for_reaper(self) -> None:
        conversation_id, _message_id, run_id = self.add_live_run(
            state="running",
            session_after="native-session",
            runner_ref="native-run",
        )
        con = self.connect()
        con.execute(
            "UPDATE conversation_runs SET process_pid=4242,"
            "process_start_ticks=9001,process_group_id=4242 WHERE run_id=?",
            (run_id,),
        )
        con.execute(
            "UPDATE active_shell_chats SET process_pid=NULL,"
            "process_start_ticks=NULL WHERE chat_id=?",
            (conversation_id,),
        )
        con.commit()
        con.close()

        store = BrokerStore(self.db_path)
        self.assertEqual(store.adopt_recoverable("new-broker", startup=True, limit=8), [])

        con = self.connect()
        con.execute(
            "UPDATE active_shell_chats SET process_pid=4242,"
            "process_start_ticks=9001 WHERE chat_id=?",
            (conversation_id,),
        )
        con.commit()
        con.close()
        adopted = store.adopt_recoverable("new-broker", startup=True, limit=8)
        self.assertEqual([run.run_id for run in adopted], [run_id])

    def test_process_registration_failure_rolls_back_run_start(self) -> None:
        conversation_id = self.add_conversation()
        self.add_message(conversation_id)
        store = BrokerStore(self.db_path)
        run = store.claim_next("broker")
        store.mark_starting(run.run_id, "broker")
        con = self.connect()
        con.executescript(
            "CREATE TRIGGER reject_process_registration "
            "BEFORE UPDATE OF process_pid ON active_shell_chats "
            "WHEN NEW.process_pid IS NOT NULL BEGIN "
            "SELECT RAISE(ABORT,'process registration rejected'); END;"
        )
        con.close()

        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "process registration rejected",
        ):
            store.mark_native_started(
                run.run_id,
                "broker",
                NativeTurn(
                    "codex",
                    "native-session",
                    "native-run",
                    self.worktree,
                    process_ref=str(os.getpid()),
                ),
            )

        con = self.connect()
        durable = con.execute(
            "SELECT r.state,r.harness_session_after,c.harness_session_ref,"
            "a.process_pid,a.process_start_ticks "
            "FROM conversation_runs r JOIN conversations c "
            "ON c.conversation_id=r.conversation_id "
            "JOIN active_shell_chats a ON a.chat_id=c.conversation_id "
            "WHERE r.run_id=?",
            (run.run_id,),
        ).fetchone()
        con.close()
        self.assertEqual(tuple(durable), ("starting", None, None, None, None))

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

    def test_noisy_events_are_appended_in_one_ordered_batch(self) -> None:
        conversation_id = self.add_conversation()
        self.add_message(conversation_id)
        store = BrokerStore(self.db_path)
        run = store.claim_next("broker")
        store.mark_starting(run.run_id, "broker")
        store.mark_native_started(
            run.run_id,
            "broker",
            NativeTurn("codex", "session", "runner", self.worktree),
        )

        sequences = store.append_events(
            run.run_id,
            [
                NormalizedEvent("run.started", {"status": "running"}),
                NormalizedEvent("assistant.delta", {"text": "one"}),
                NormalizedEvent("assistant.delta", {"text": "two"}),
            ],
        )

        self.assertEqual(sequences, [1, 2, 3])
        con = self.connect()
        rows = con.execute(
            "SELECT sequence,event_type,payload FROM conversation_events "
            "WHERE run_id=? ORDER BY sequence",
            (run.run_id,),
        ).fetchall()
        con.close()
        self.assertEqual(
            [(row["sequence"], row["event_type"]) for row in rows],
            [
                (1, "run.started"),
                (2, "assistant.delta"),
                (3, "assistant.delta"),
            ],
        )
        self.assertEqual(json.loads(rows[-1]["payload"])["text"], "two")

    def test_historical_deepseek_usage_stays_normalized_but_inert(self) -> None:
        conversation_id = self.add_conversation(
            harness="deepseek",
            provider="deepseek-official",
            model="deepseek-v4-pro",
            title="Browser route",
        )
        self.add_message(conversation_id)
        store = BrokerStore(self.db_path)
        run = store.claim_next("broker")
        store.mark_starting(run.run_id, "broker")
        store.mark_native_started(
            run.run_id,
            "broker",
            NativeTurn(
                "deepseek",
                "deepseek-session-1",
                "deepseek-run-1",
                self.worktree,
            ),
        )

        store.append_events(
            run.run_id,
            [
                NormalizedEvent(
                    "usage",
                    {"tokens": {
                        "input_tokens": 8,
                        "output_tokens": 3,
                        "reasoning_tokens": 1,
                    }},
                ),
                NormalizedEvent(
                    "usage",
                    {"tokens": {
                        "input_tokens": 2,
                        "output_tokens": 1,
                        "cache_read_tokens": 5,
                    }},
                ),
                NormalizedEvent(
                    "usage",
                    {"tokens": {"input_tokens": -1, "output_tokens": True}},
                ),
            ],
        )

        with self.connect() as con:
            analytics = con.execute(
                "SELECT harness,harness_session_ref FROM session_token_usage"
            ).fetchall()
            events = con.execute(
                "SELECT event_type,payload FROM conversation_events "
                "WHERE conversation_id=? AND event_type='usage' ORDER BY sequence",
                (conversation_id,),
            ).fetchall()
        self.assertEqual(analytics, [])
        self.assertEqual(len(events), 3)
        self.assertEqual(
            [json.loads(row["payload"])["tokens"] for row in events],
            [
                {"input_tokens": 8, "output_tokens": 3, "reasoning_tokens": 1},
                {"input_tokens": 2, "output_tokens": 1, "cache_read_tokens": 5},
                {"input_tokens": -1, "output_tokens": True},
            ],
        )

    def test_non_deepseek_usage_does_not_create_browser_analytics_rows(self) -> None:
        conversation_id = self.add_conversation(harness="codex")
        self.add_message(conversation_id)
        store = BrokerStore(self.db_path)
        run = store.claim_next("broker")
        store.mark_starting(run.run_id, "broker")
        store.mark_native_started(
            run.run_id,
            "broker",
            NativeTurn("codex", "codex-session", "codex-run", self.worktree),
        )

        store.append_event(
            run.run_id,
            NormalizedEvent(
                "usage",
                {"tokens": {"input_tokens": 8, "output_tokens": 3}},
            ),
        )

        with self.connect() as con:
            count = con.execute(
                "SELECT COUNT(*) FROM session_token_usage"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_terminal_observation_is_post_commit_and_cannot_fail_run(self) -> None:
        conversation_id = self.add_conversation()
        self.add_message(conversation_id)
        store = BrokerStore(self.db_path)
        run = store.claim_next("broker")
        store.mark_starting(run.run_id, "broker")
        store.mark_native_started(
            run.run_id,
            "broker",
            NativeTurn(
                "codex",
                "native-session",
                "native-run-observation",
                self.worktree,
            ),
        )

        def observer(db_path, observed_conversation_id):
            probe = sqlite3.connect(db_path, timeout=0.1)
            try:
                probe.execute("BEGIN IMMEDIATE")
                probe.rollback()
            finally:
                probe.close()
            raise RuntimeError("Git unavailable")

        with mock.patch(
            "conversation_broker.conversation_git_targets.observe_and_persist",
            side_effect=observer,
        ) as observe:
            self.assertTrue(
                store.finish_run(
                    run.run_id,
                    "succeeded",
                    event_type="run.completed",
                )
            )

        observe.assert_called_once_with(
            str(self.db_path),
            conversation_id,
            runner=mock.ANY,
            connect=mock.ANY,
            now=None,
        )
        con = self.connect()
        try:
            self.assertEqual(
                con.execute(
                    "SELECT state FROM conversation_runs WHERE run_id=?",
                    (run.run_id,),
                ).fetchone()[0],
                "succeeded",
            )
            self.assertEqual(
                con.execute(
                    "SELECT state FROM conversations "
                    "WHERE conversation_id=?",
                    (conversation_id,),
                ).fetchone()[0],
                "idle",
            )
        finally:
            con.close()

    def test_close_requested_conversation_cannot_dispatch_queued_work(self) -> None:
        conversation_id = self.add_conversation()
        message_id = self.add_message(conversation_id)
        con = self.connect()
        con.execute(
            "INSERT INTO conversation_events "
            "(conversation_id,sequence,event_type,payload) "
            "VALUES (?,1,'conversation.close.requested','{}')",
            (conversation_id,),
        )
        con.commit()
        con.close()

        self.assertIsNone(BrokerStore(self.db_path).claim_next("broker"))

        con = self.connect()
        try:
            message = con.execute(
                "SELECT state FROM conversation_messages WHERE message_id=?",
                (message_id,),
            ).fetchone()[0]
            outbox = con.execute(
                "SELECT state,run_id FROM conversation_outbox WHERE message_id=?",
                (message_id,),
            ).fetchone()
            run_count = con.execute(
                "SELECT COUNT(*) FROM conversation_runs "
                "WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(message, "queued")
        self.assertEqual(tuple(outbox), ("pending", None))
        self.assertEqual(run_count, 0)

    def test_reopened_conversation_dispatches_despite_stale_close_request(
        self,
    ) -> None:
        conversation_id = self.add_conversation()
        message_id = self.add_message(conversation_id)
        con = self.connect()
        con.execute(
            "INSERT INTO conversation_events "
            "(conversation_id,sequence,event_type,payload) "
            "VALUES (?,1,'conversation.close.requested','{}')",
            (conversation_id,),
        )
        con.execute(
            "INSERT INTO conversation_events "
            "(conversation_id,sequence,event_type,payload) "
            "VALUES (?,2,'conversation.reopened','{}')",
            (conversation_id,),
        )
        con.commit()
        con.close()

        store = BrokerStore(self.db_path)
        run = store.claim_next("broker")
        self.assertIsNotNone(run)
        self.assertEqual(run.message_id, message_id)
        self.assertEqual(run.lifecycle_epoch, 2)
        self.assertEqual(run.context().lifecycle_epoch, 2)
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
        store.finish_run(
            run.run_id,
            "succeeded",
            event_type="run.completed",
        )

        con = self.connect()
        try:
            row = con.execute(
                "SELECT state,closed_at FROM conversations "
                "WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
        finally:
            con.close()
        self.assertNotEqual(row["state"], "closed")
        self.assertIsNone(row["closed_at"])

    def test_registry_excludes_an_unlinked_legacy_conversation(self) -> None:
        self.allow_legacy_duplicate_open_chats()
        first_conversation = self.add_conversation(shell_id=1)
        second_conversation = self.add_conversation(shell_id=1)
        self.add_message(first_conversation)
        self.add_message(second_conversation)
        store = BrokerStore(self.db_path)

        first = store.claim_next("broker")
        self.assertIsNotNone(first)
        self.assertEqual(first.conversation_id, second_conversation)
        self.assertIsNone(store.claim_next("broker"))
        store.finish_run(
            first.run_id,
            "failed",
            event_type="run.failed",
            error_code="TEST",
        )
        self.assertIsNone(store.claim_next("broker"))
        con = self.connect()
        self.assertEqual(
            con.execute(
                "SELECT state FROM conversation_messages "
                "WHERE conversation_id=?",
                (first_conversation,),
            ).fetchone()[0],
            "queued",
        )
        con.close()

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

        def claim(owner: str) -> BrokerRun | None:
            barrier.wait()
            return BrokerStore(self.db_path).claim_next(owner)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            workers = [
                pool.submit(claim, f"broker-{index}")
                for index in (1, 2)
            ]
            barrier.wait()
            claimed = [worker.result(timeout=5) for worker in workers]

        self.assertEqual(len(claimed), 2)
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

    def test_transient_event_write_contention_does_not_abandon_stream(
        self,
    ) -> None:
        adapter = FakeAdapter()
        broker = self.start_broker(
            lambda _harness: adapter,
            recovery_seconds=0.01,
        )
        append_events = broker.store.append_events
        attempts = 0

        def contend_once(run_id, events):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise sqlite3.OperationalError("database is locked")
            return append_events(run_id, events)

        broker.store.append_events = contend_once
        conversation_id = self.add_conversation()
        message_id = self.add_message(conversation_id)
        broker.notify()

        deadline = time.monotonic() + 3
        row = None
        while time.monotonic() < deadline:
            con = self.connect()
            row = con.execute(
                "SELECT run_id,state FROM conversation_runs "
                "WHERE trigger_message_id=?",
                (message_id,),
            ).fetchone()
            con.close()
            if row is not None and row["state"] == "succeeded":
                break
            time.sleep(0.01)

        self.assertIsNotNone(row)
        self.assertEqual(row["state"], "succeeded")
        self.assertGreaterEqual(attempts, 2)
        con = self.connect()
        event_types = [
            item[0]
            for item in con.execute(
                "SELECT event_type FROM conversation_events "
                "WHERE run_id=? ORDER BY sequence",
                (row["run_id"],),
            )
        ]
        con.close()
        self.assertEqual(
            event_types,
            [
                "session.started",
                "run.started",
                "assistant.delta",
                "run.completed",
            ],
        )
        self.assertEqual(adapter.reconciled, 0)

    def test_sparse_delta_flushes_without_waiting_for_the_next_native_event(
        self,
    ) -> None:
        adapter = SparseDeltaAdapter()
        self.addCleanup(adapter.release.set)
        broker = self.start_broker(
            lambda _harness: adapter,
            event_flush_seconds=0.02,
        )
        conversation_id = self.add_conversation()
        self.add_message(conversation_id)
        broker.notify()
        self.assertTrue(adapter.delta_yielded.wait(1))

        deadline = time.monotonic() + 0.5
        delta_rows = []
        while time.monotonic() < deadline:
            with self.connect() as con:
                delta_rows = con.execute(
                    "SELECT event_type,payload FROM conversation_events "
                    "WHERE conversation_id=? AND event_type='assistant.delta'",
                    (conversation_id,),
                ).fetchall()
            if delta_rows:
                break
            time.sleep(0.01)

        self.assertEqual(len(delta_rows), 1)
        self.assertEqual(
            json.loads(delta_rows[0]["payload"])["text"],
            "visible promptly",
        )
        adapter.release.set()

    def test_event_flush_writes_at_most_the_configured_batch_size(self) -> None:
        store = mock.Mock()
        store.append_events.side_effect = lambda _run_id, events: list(
            range(1, len(events) + 1)
        )
        broker = ConversationBroker(
            self.db_path,
            store=store,
            event_batch_size=2,
        )
        pending = [
            NormalizedEvent("assistant.delta", {"text": str(index)})
            for index in range(5)
        ]

        self.assertTrue(broker._flush_events(7, pending, wait=False))

        written = store.append_events.call_args.args[1]
        self.assertEqual([event.payload["text"] for event in written], ["0", "1"])
        self.assertEqual(
            [event.payload["text"] for event in pending],
            ["2", "3", "4"],
        )

    def test_busy_retry_stops_at_the_configured_attempt_budget(self) -> None:
        broker = ConversationBroker(
            self.db_path,
            recovery_seconds=0.001,
            busy_retry_attempts=3,
        )
        attempts = 0

        def always_busy() -> None:
            nonlocal attempts
            attempts += 1
            raise sqlite3.OperationalError("database is locked")

        persisted, result = broker._retry_busy(always_busy, wait=True)

        self.assertFalse(persisted)
        self.assertIsNone(result)
        self.assertEqual(attempts, 3)

    def test_reconciled_cancellation_persists_native_evidence(self) -> None:
        _conversation, _message, run_id = self.add_live_run(
            state="running",
            session_after="native-session",
            runner_ref="native-run-cancelled",
        )
        adapter = FakeAdapter(
            reconcile=ReconcileResult(
                "cancelled",
                True,
                "native transcript proves cancellation",
                "native",
            )
        )
        broker = self.start_broker(lambda _harness: adapter)

        self.wait_run_state(run_id, "cancelled")
        self.assertTrue(broker.wait_idle())
        with self.connect() as con:
            terminal = con.execute(
                "SELECT event_type,payload FROM conversation_events "
                "WHERE run_id=? ORDER BY sequence DESC LIMIT 1",
                (run_id,),
            ).fetchone()

        self.assertEqual(terminal["event_type"], "run.interrupted")
        payload = json.loads(terminal["payload"])
        self.assertEqual(payload["interrupt_evidence"], "native")
        self.assertTrue(payload["reconciled"])

    def test_same_harness_runs_stream_concurrently_on_different_shells(
        self,
    ) -> None:
        barrier = threading.Barrier(2)
        adapters: list[BarrierAdapter] = []

        def factory(_harness: str) -> BarrierAdapter:
            adapter = BarrierAdapter(barrier, len(adapters) + 1)
            adapters.append(adapter)
            return adapter

        broker = self.start_broker(factory)
        first = self.add_conversation(shell_id=1, harness="codex")
        second = self.add_conversation(shell_id=2, harness="codex")
        first_message = self.add_message(first)
        second_message = self.add_message(second)
        broker.notify()

        deadline = time.monotonic() + 3
        states: dict[int, str] = {}
        while time.monotonic() < deadline:
            con = self.connect()
            states = {
                int(row["trigger_message_id"]): row["state"]
                for row in con.execute(
                    "SELECT trigger_message_id,state FROM conversation_runs "
                    "WHERE trigger_message_id IN (?,?)",
                    (first_message, second_message),
                )
            }
            con.close()
            if states == {
                first_message: "succeeded",
                second_message: "succeeded",
            }:
                break
            time.sleep(0.01)

        self.assertEqual(
            states,
            {
                first_message: "succeeded",
                second_message: "succeeded",
            },
        )
        self.assertEqual(len(adapters), 2)
        self.assertTrue(all(adapter.started == 1 for adapter in adapters))
        self.assertTrue(all(adapter.crossed for adapter in adapters))

    def test_opencode_concurrent_first_turns_submit_once_to_exact_sessions(
        self,
    ) -> None:
        barrier = threading.Barrier(2)
        transports: list[ConcurrentOpenCodeFirstTurnTransport] = []

        def factory(harness: str) -> OpenCodeAdapter:
            self.assertEqual(harness, "opencode")
            transport = ConcurrentOpenCodeFirstTurnTransport(
                barrier, len(transports) + 1
            )
            transports.append(transport)
            return OpenCodeAdapter(
                transport=transport,
                shell_runtime_dir=self.root / "opencode-shells",
            )

        broker = self.start_broker(factory)
        first = self.add_conversation(
            shell_id=1,
            harness="opencode",
            provider="openai",
            model="gpt-test",
        )
        second = self.add_conversation(
            shell_id=2,
            harness="opencode",
            provider="openai",
            model="gpt-test",
        )
        message_ids = (self.add_message(first), self.add_message(second))
        broker.notify()

        deadline = time.monotonic() + 3
        rows = []
        while time.monotonic() < deadline:
            with self.connect() as con:
                rows = con.execute(
                    "SELECT run_id,trigger_message_id,state,harness_session_after "
                    "FROM conversation_runs WHERE trigger_message_id IN (?,?) "
                    "ORDER BY trigger_message_id",
                    message_ids,
                ).fetchall()
            if len(rows) == 2 and all(row["state"] == "succeeded" for row in rows):
                break
            time.sleep(0.01)

        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {row["trigger_message_id"] for row in rows},
            set(message_ids),
        )
        self.assertEqual(
            {row["harness_session_after"] for row in rows},
            {transport.session_ref for transport in transports},
        )
        self.assertEqual([transport.prompt_count for transport in transports], [1, 1])
        self.assertTrue(all(transport.crossed for transport in transports))
        for transport in transports:
            self.assertEqual(
                [request[:2] for request in transport.requests],
                [
                    ("POST", "/session"),
                    ("POST", f"/session/{transport.session_ref}/message"),
                ],
            )
            prompt = transport.requests[1][2]
            self.assertEqual(
                prompt["model"],
                {"providerID": "openai", "modelID": "gpt-test"},
            )
        with self.connect() as con:
            terminal_counts = {
                int(row["run_id"]): con.execute(
                    "SELECT COUNT(*) FROM conversation_events "
                    "WHERE run_id=? AND event_type='run.completed'",
                    (row["run_id"],),
                ).fetchone()[0]
                for row in rows
            }
        self.assertEqual(set(terminal_counts.values()), {1})

    def test_transient_lease_renewal_contention_stays_recoverable(
        self,
    ) -> None:
        adapter = ReconcileSequenceAdapter()
        broker = self.start_broker(
            lambda _harness: adapter,
            recovery_seconds=0.01,
        )
        renew_runs = broker.store.renew_runs
        attempts = 0

        def contend_once(owner, run_ids):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise sqlite3.OperationalError("database is locked")
            return renew_runs(owner, run_ids)

        broker.store.renew_runs = contend_once
        conversation_id = self.add_conversation()
        message_id = self.add_message(conversation_id)
        broker.notify()

        deadline = time.monotonic() + 3
        row = None
        while time.monotonic() < deadline:
            con = self.connect()
            row = con.execute(
                "SELECT run_id,state FROM conversation_runs "
                "WHERE trigger_message_id=?",
                (message_id,),
            ).fetchone()
            con.close()
            if row is not None and row["state"] == "succeeded":
                break
            time.sleep(0.01)

        self.assertIsNotNone(row)
        self.assertEqual(row["state"], "succeeded")
        self.assertGreaterEqual(attempts, 2)
        self.assertEqual(adapter.reconciled, 2)

    def test_post_activity_stream_error_reconciles_without_replay(self) -> None:
        adapter = PostActivityStreamErrorAdapter()
        broker = self.start_broker(
            lambda harness: adapter if harness == "kimi" else None,
            recovery_seconds=1,
        )
        conversation_id = self.add_conversation(harness="kimi")
        message_id = self.add_message(conversation_id)
        broker.notify()

        deadline = time.monotonic() + 3
        row = None
        events = []
        while time.monotonic() < deadline:
            with self.connect() as con:
                row = con.execute(
                    "SELECT run_id,state,error_code,error_detail "
                    "FROM conversation_runs WHERE trigger_message_id=?",
                    (message_id,),
                ).fetchone()
                if row is not None:
                    events = con.execute(
                        "SELECT event_type,payload FROM conversation_events "
                        "WHERE run_id=? ORDER BY sequence",
                        (row["run_id"],),
                    ).fetchall()
            if row is not None and row["state"] == "succeeded":
                break
            time.sleep(0.01)

        if row is None:
            self.fail("post-activity stream failure produced no durable run")
        self.assertEqual(tuple(row)[1:], ("succeeded", None, None))
        self.assertEqual(adapter.started, 1)
        self.assertEqual(adapter.resumed, 0)
        self.assertEqual(adapter.reconciled, 2)
        self.assertEqual(
            [event["event_type"] for event in events],
            [
                "session.started",
                "run.started",
                "assistant.delta",
                "run.completed",
            ],
        )
        terminal = json.loads(events[-1]["payload"])
        self.assertEqual(
            terminal,
            {
                "detail": "native run completed",
                "outcome": "succeeded",
                "proven": True,
                "reconciled": True,
            },
        )
        with self.connect() as con:
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM conversation_runs "
                    "WHERE conversation_id=?",
                    (conversation_id,),
                ).fetchone()[0],
                1,
            )

    def test_kimi_native_identities_are_persisted_before_first_event(
        self,
    ) -> None:
        session_ref = (
            "session_11111111-1111-4111-8111-111111111111"
        )
        run_ref = "kimi-1785365142363-22866"
        adapter = FakeAdapter(
            harness="kimi",
            native_session_ref=session_ref,
            native_run_ref=run_ref,
        )
        broker = self.start_broker(lambda harness: (
            adapter if harness == "kimi" else None
        ))
        order: list[tuple[str, str, str | None]] = []
        mark_native_started = broker.store.mark_native_started
        append_events = broker.store.append_events

        def record_native(run_id, owner, turn):
            order.append(("native", turn.session_ref, turn.run_ref))
            return mark_native_started(run_id, owner, turn)

        def record_events(run_id, events):
            order.extend(("event", event.type, None) for event in events)
            return append_events(run_id, events)

        broker.store.mark_native_started = record_native
        broker.store.append_events = record_events
        conversation_id = self.add_conversation(harness="kimi")
        message_id = self.add_message(conversation_id)
        broker.notify()

        deadline = time.monotonic() + 3
        run_id = None
        while time.monotonic() < deadline:
            con = self.connect()
            row = con.execute(
                "SELECT run_id,state,harness_session_after,runner_ref "
                "FROM conversation_runs WHERE trigger_message_id=?",
                (message_id,),
            ).fetchone()
            con.close()
            if row is not None and row["state"] == "succeeded":
                run_id = row["run_id"]
                break
            time.sleep(0.01)
        self.assertIsNotNone(run_id)
        self.assertEqual(
            order[0],
            ("native", session_ref, run_ref),
        )
        self.assertEqual(order[1][0], "event")
        self.assertEqual(
            (row["harness_session_after"], row["runner_ref"]),
            (session_ref, run_ref),
        )

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

    def test_interrupt_intent_normalizes_adapter_failure_to_cancelled(
        self,
    ) -> None:
        adapter = FakeAdapter(
            block=True,
            interrupt_terminal="run.failed",
        )
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
                break
            time.sleep(0.01)
        self.assertIsNotNone(run_id)

        self.assertTrue(broker.interrupt(run_id))
        self.wait_run_state(run_id, "cancelled")
        con = self.connect()
        terminal = con.execute(
            "SELECT event_type,payload FROM conversation_events "
            "WHERE run_id=? AND event_type LIKE 'run.%' "
            "ORDER BY sequence DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        con.close()
        self.assertEqual(terminal["event_type"], "run.interrupted")
        payload = json.loads(terminal["payload"])
        self.assertEqual(payload["adapter_terminal"], "run.failed")
        self.assertEqual(payload["interrupt_evidence"], "operator")

    def test_opencode_blocking_message_is_interruptible_after_identity(
        self,
    ) -> None:
        transport = BlockingOpenCodeTransport()
        adapter = OpenCodeAdapter(
            transport=transport,
            shell_runtime_dir=self.root / "opencode-shells",
        )
        broker = self.start_broker(
            lambda harness: adapter if harness == "opencode" else None
        )
        conversation_id = self.add_conversation(harness="opencode")
        message_id = self.add_message(conversation_id)
        broker.notify()
        self.assertTrue(
            transport.message_started.wait(1),
            "OpenCode synchronous message dispatch never started",
        )

        con = self.connect()
        run = con.execute(
            "SELECT run_id,state,harness_session_after,runner_ref "
            "FROM conversation_runs WHERE trigger_message_id=?",
            (message_id,),
        ).fetchone()
        con.close()
        self.assertIsNotNone(run)
        self.assertEqual(run["state"], "running")
        self.assertEqual(
            run["harness_session_after"],
            transport.session_ref,
        )
        self.assertTrue(run["runner_ref"])

        second_message_id = self.add_message(conversation_id)
        third_message_id = self.add_message(conversation_id)
        self.assertTrue(broker.interrupt(int(run["run_id"])))
        self.wait_run_state(int(run["run_id"]), "cancelled")
        self.assertTrue(transport.aborted.is_set())
        self.assertIn(
            ("POST", f"/session/{transport.session_ref}/abort"),
            transport.requests,
        )

        con = self.connect()
        events = [
            row[0]
            for row in con.execute(
                "SELECT event_type FROM conversation_events "
                "WHERE run_id=? ORDER BY sequence",
                (run["run_id"],),
            )
        ]
        con.close()
        self.assertLess(
            events.index("run.interrupt.requested"),
            events.index("run.interrupted"),
        )

        deadline = time.monotonic() + 3
        resumed_runs = []
        while time.monotonic() < deadline:
            con = self.connect()
            resumed_runs = con.execute(
                "SELECT trigger_message_id,state,harness_session_before,"
                "harness_session_after FROM conversation_runs "
                "WHERE conversation_id=? ORDER BY run_id",
                (conversation_id,),
            ).fetchall()
            con.close()
            if (
                len(resumed_runs) == 3
                and resumed_runs[-1]["state"] == "succeeded"
            ):
                break
            time.sleep(0.01)
        self.assertEqual(
            [row["trigger_message_id"] for row in resumed_runs],
            [message_id, second_message_id, third_message_id],
        )
        self.assertEqual(
            [row["state"] for row in resumed_runs],
            ["cancelled", "succeeded", "succeeded"],
        )
        self.assertEqual(
            [
                (
                    row["harness_session_before"],
                    row["harness_session_after"],
                )
                for row in resumed_runs[1:]
            ],
            [
                (transport.session_ref, transport.session_ref),
                (transport.session_ref, transport.session_ref),
            ],
        )
        self.assertEqual(transport.message_count, 3)

    def test_opencode_first_turn_submission_failure_is_not_reclassified_idle(
        self,
    ) -> None:
        endpoint = RejectingOpenCodeFirstTurnEndpoint()
        transport = base_adapter.UrlHttpTransport("http://opencode.test")
        adapter = OpenCodeAdapter(
            transport=transport,
            shell_runtime_dir=self.root / "opencode-shells",
        )
        broker = self.start_broker(
            lambda harness: adapter if harness == "opencode" else None,
        )
        conversation_id = self.add_conversation(harness="opencode")
        message_id = self.add_message(conversation_id)
        with mock.patch.object(
            base_adapter.urllib.request,
            "urlopen",
            side_effect=endpoint.urlopen,
        ):
            broker.notify()

            deadline = time.monotonic() + 3
            row = None
            while time.monotonic() < deadline:
                with self.connect() as con:
                    row = con.execute(
                        "SELECT run_id,state,harness_session_after,error_code,"
                        "error_detail FROM conversation_runs "
                        "WHERE trigger_message_id=?",
                        (message_id,),
                    ).fetchone()
                if row is not None and row["state"] in {
                    "failed", "unknown", "cancelled", "succeeded"
                }:
                    break
                time.sleep(0.01)
            self.assertTrue(broker.wait_idle())

        if row is None:
            self.fail("OpenCode first-turn failure produced no durable run")
        self.assertEqual(row["state"], "unknown")
        self.assertEqual(row["harness_session_after"], endpoint.session_ref)
        self.assertEqual(row["error_code"], "HARNESS_SUBMISSION_FAILED")
        self.assertEqual(
            row["error_detail"],
            f"POST /session/{endpoint.session_ref}/message returned HTTP 400",
        )
        self.assertEqual(endpoint.prompt_count, 1)
        self.assertEqual(endpoint.native_messages, [])
        self.assertEqual(
            endpoint.requests,
            [
                ("POST", "/session"),
                ("GET", "/event"),
                ("POST", f"/session/{endpoint.session_ref}/message"),
            ],
        )
        with self.connect() as con:
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM conversation_runs "
                    "WHERE conversation_id=?",
                    (conversation_id,),
                ).fetchone()[0],
                1,
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

    def test_kimi_running_crash_rebuilds_exact_run_slice(self) -> None:
        sessions_root = self.root / "kimi-sessions"
        session_ref = "session_dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        session_dir = sessions_root / "wd_recovered" / session_ref
        wire = session_dir / "agents" / "main" / "wire.jsonl"
        wire.parent.mkdir(parents=True)
        (session_dir / "state.json").write_text(
            json.dumps({"workDir": str(self.worktree)}),
            encoding="utf-8",
        )
        with wire.open("w", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "type": "turn.prompt",
                        "input": [{"type": "text", "text": "hello"}],
                        "time": 8200,
                    }
                )
                + "\n"
            )
            stream.write(
                json.dumps(
                    {
                        "type": "context.append_loop_event",
                        "event": {
                            "type": "step.end",
                            "finishReason": "end_turn",
                        },
                    }
                )
                + "\n"
            )
            stream.write(
                json.dumps(
                    {
                        "type": "usage.record",
                        "usageScope": "turn",
                        "usage": {"inputOther": 4, "output": 2},
                    }
                )
                + "\n"
            )
        _conversation, _message, run_id = self.add_live_run(
            state="running",
            session_after=session_ref,
            runner_ref="kimi-8200-0",
            harness="kimi",
        )
        adapter = KimiAdapter(sessions_root=sessions_root)

        broker = self.start_broker(
            lambda harness: adapter if harness == "kimi" else None
        )

        self.wait_run_state(run_id, "succeeded")
        self.assertTrue(broker.wait_idle())
        con = self.connect()
        run = con.execute(
            "SELECT state,error_code,error_detail FROM conversation_runs "
            "WHERE run_id=?",
            (run_id,),
        ).fetchone()
        events = con.execute(
            "SELECT event_type,payload FROM conversation_events "
            "WHERE run_id=? ORDER BY sequence",
            (run_id,),
        ).fetchall()
        con.close()
        self.assertEqual(tuple(run), ("succeeded", None, None))
        self.assertEqual([row["event_type"] for row in events], ["run.completed"])
        self.assertEqual(
            json.loads(events[0]["payload"]),
            {
                "detail": (
                    "Kimi exact run slice contains end_turn and "
                    "turn-scoped usage"
                ),
                "outcome": "succeeded",
                "proven": True,
                "reconciled": True,
            },
        )

    def test_leased_crash_is_safe_to_start_once(self) -> None:
        _conversation, _message, run_id = self.add_live_run(state="leased")
        adapter = FakeAdapter()
        broker = self.start_broker(lambda _harness: adapter)
        self.wait_run_state(run_id, "succeeded")
        self.assertEqual(adapter.started, 1)
        self.assertEqual(adapter.resumed, 0)
        self.assertTrue(broker.wait_idle())


class BootSeamPreparer:
    """The real conversation_boot seam as the broker's launch_preparer.

    Exercises binding, validation, and byte restoration against the test DB
    without dragging in the rest of run.prepare_launch; compose is a probe so
    tests can count compositions and change the volatile shell state between
    turns. ``last_content`` is the exact byte string this turn resolved and
    materialized, for dispatch-edge assertions."""

    def __init__(
        self,
        db_path: Path,
        compose,
        *,
        archive_id: int,
        write_failures: int = 0,
    ) -> None:
        self.db_path = db_path
        self.compose = compose
        self.archive_id = archive_id
        self.write_failures = write_failures
        self.compose_calls = 0
        self.last_content: str | None = None

    def __call__(self, broker_run: BrokerRun):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        try:
            directive = BootDirective(
                conversation_id=broker_run.conversation_id,
                phase="resume" if broker_run.session_before else "start",
            )

            def composing() -> str:
                self.compose_calls += 1
                return self.compose()

            content = conversation_boot.resolve_boot(con, directive, composing)
        finally:
            con.close()
        if self.write_failures:
            self.write_failures -= 1
            raise OSError("injected boot write failure")
        conversation_boot.write_boot_files(Path(broker_run.worktree), content)
        self.last_content = content
        return broker_run.context(), self.archive_id


class BootAssertingAdapter(FakeAdapter):
    """At each native dispatch edge, both worktree boot files must already
    hold the exact bytes this turn's preparation resolved."""

    def __init__(self, preparer: BootSeamPreparer, **kwargs) -> None:
        super().__init__(**kwargs)
        self.preparer = preparer
        self.messages: list[str] = []
        self.resume_sessions: list[str] = []

    def _assert_boot_owned(self, context: ConversationContext) -> None:
        expected = self.preparer.last_content
        if expected is None:
            raise AssertionError("native dispatch before boot preparation")
        for name in conversation_boot.BOOT_FILES:
            actual = (context.worktree / name).read_bytes()
            if actual != expected.encode("utf-8"):
                raise AssertionError(f"{name} drifted from the bound snapshot")

    def start(self, context: ConversationContext, message: str) -> NativeTurn:
        self.messages.append(message)
        self._assert_boot_owned(context)
        return super().start(context, message)

    def resume(
        self,
        session_ref: str,
        context: ConversationContext,
        message: str,
    ) -> NativeTurn:
        self.messages.append(message)
        self.resume_sessions.append(session_ref)
        self._assert_boot_owned(context)
        return super().resume(session_ref, context, message)


class BootSnapshotContractTest(ConversationBrokerCase):
    """Spec #163 broker boundaries: bind before start, exact reuse on resume."""

    def setUp(self) -> None:
        super().setUp()
        con = self.connect()
        self.archive_id = int(
            con.execute(
                "INSERT INTO shell_memory_archives "
                "(shell_id,session_id,date,full_narrative) "
                "VALUES (1,'9001','2026-08-19','boot snapshot tests')"
            ).lastrowid
        )
        con.commit()
        con.close()
        self.boot_state = {"marker": "one"}

    def compose(self) -> str:
        return f"boot marker {self.boot_state['marker']}"

    def stamp_ledger(self, applied_at: str = "2000-01-01 00:00:00") -> None:
        """Mark the 0224 migration applied, so later conversations are new."""
        con = self.connect()
        con.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "  filename TEXT PRIMARY KEY,"
            "  applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        con.execute(
            "INSERT OR REPLACE INTO schema_migrations (filename,applied_at) "
            "VALUES ('0224_conversation_boot_snapshots.sql',?)",
            (applied_at,),
        )
        con.commit()
        con.close()

    def snapshot_row(self, conversation_id: str):
        con = self.connect()
        row = con.execute(
            "SELECT content,content_sha256,content_bytes,binding_origin "
            "FROM conversation_boot_snapshots WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        con.close()
        return row

    def requeue(self, conversation_id: str) -> int:
        """Post the next message as the API would: conversation back to queued."""
        con = self.connect()
        con.execute(
            "UPDATE conversations SET state='queued' WHERE conversation_id=?",
            (conversation_id,),
        )
        con.commit()
        con.close()
        return self.add_message(conversation_id)

    def add_message_body(self, conversation_id: str, body: str) -> int:
        self.serial += 1
        key = f"message-{self.serial}"
        con = self.connect()
        message_id = con.execute(
            "INSERT INTO conversation_messages "
            "(conversation_id,sender_kind,sender_ref,message_kind,body,"
            "idempotency_key,request_hash,state) "
            "VALUES (?,'user','1','prompt',?,?,?,'queued')",
            (conversation_id, body, key, f"hash-{key}"),
        ).lastrowid
        con.execute(
            "INSERT INTO conversation_outbox (conversation_id,message_id) "
            "VALUES (?,?)",
            (conversation_id, message_id),
        )
        con.commit()
        con.close()
        return int(message_id)

    def file_states(self) -> dict:
        return {
            name: (
                (self.worktree / name).read_bytes(),
                (self.worktree / name).stat().st_mtime_ns,
            )
            for name in conversation_boot.BOOT_FILES
        }

    def test_first_turn_binds_before_native_start(self) -> None:
        self.stamp_ledger()
        preparer = BootSeamPreparer(
            self.db_path, self.compose, archive_id=self.archive_id
        )
        adapter = BootAssertingAdapter(preparer)
        broker = self.start_broker(
            lambda _harness: adapter, launch_preparer=preparer
        )
        conversation_id = self.add_conversation()
        message_id = self.add_message(conversation_id)
        broker.notify()

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            con = self.connect()
            state = con.execute(
                "SELECT state FROM conversation_runs WHERE trigger_message_id=?",
                (message_id,),
            ).fetchone()
            con.close()
            if state and state[0] == "succeeded":
                break
            time.sleep(0.01)
        else:
            self.fail("first turn did not finish")

        row = self.snapshot_row(conversation_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["content"], "boot marker one")
        self.assertEqual(row["binding_origin"], "new_conversation")
        self.assertEqual(adapter.messages, ["hello"])
        self.assertEqual(preparer.compose_calls, 1)

    def test_resume_reuses_bytes_and_never_rewrites_matching_files(self) -> None:
        self.stamp_ledger()
        preparer = BootSeamPreparer(
            self.db_path, self.compose, archive_id=self.archive_id
        )
        adapter = BootAssertingAdapter(preparer)
        broker = self.start_broker(
            lambda _harness: adapter, launch_preparer=preparer
        )
        conversation_id = self.add_conversation()
        first_message = self.add_message(conversation_id)
        broker.notify()
        self.wait_message_run(conversation_id, first_message, "succeeded")
        digest = self.snapshot_row(conversation_id)["content_sha256"]
        files = self.file_states()

        # Volatile boot inputs change; the conversation's bytes must not.
        self.boot_state["marker"] = "two"
        second_message = self.requeue(conversation_id)
        broker.notify()
        self.wait_message_run(conversation_id, second_message, "succeeded")

        self.assertEqual(adapter.started, 1)
        self.assertEqual(adapter.resumed, 1)
        self.assertEqual(adapter.messages, ["hello", "hello"])
        self.assertEqual(preparer.compose_calls, 1)
        self.assertEqual(
            self.snapshot_row(conversation_id)["content_sha256"], digest
        )
        self.assertEqual(self.file_states(), files)

    def test_resume_restores_files_replaced_by_another_chat(self) -> None:
        self.stamp_ledger()
        preparer = BootSeamPreparer(
            self.db_path, self.compose, archive_id=self.archive_id
        )
        adapter = BootAssertingAdapter(preparer)
        broker = self.start_broker(
            lambda _harness: adapter, launch_preparer=preparer
        )
        conversation_id = self.add_conversation()
        first_message = self.add_message(conversation_id)
        broker.notify()
        self.wait_message_run(conversation_id, first_message, "succeeded")
        digest = self.snapshot_row(conversation_id)["content_sha256"]

        # A different chat (or any external lifecycle) replaced the shared
        # worktree files; resume must restore the stored bytes before dispatch.
        for name in conversation_boot.BOOT_FILES:
            (self.worktree / name).write_text("another chat's boot")
        second_message = self.requeue(conversation_id)
        broker.notify()
        self.wait_message_run(conversation_id, second_message, "succeeded")

        self.assertEqual(adapter.resumed, 1)
        self.assertEqual(preparer.compose_calls, 1)
        self.assertEqual(
            self.snapshot_row(conversation_id)["content_sha256"], digest
        )
        for name in conversation_boot.BOOT_FILES:
            self.assertEqual(
                (self.worktree / name).read_bytes(),
                b"boot marker one",
            )

    def test_boot_write_failure_keeps_snapshot_and_retry_does_not_recompose(
        self,
    ) -> None:
        self.stamp_ledger()
        preparer = BootSeamPreparer(
            self.db_path,
            self.compose,
            archive_id=self.archive_id,
            write_failures=1,
        )
        adapter = BootAssertingAdapter(preparer)
        broker = self.start_broker(
            lambda _harness: adapter, launch_preparer=preparer
        )
        conversation_id = self.add_conversation()
        first_message = self.add_message(conversation_id)
        broker.notify()
        self.wait_message_run(conversation_id, first_message, "failed")

        # Committed before the failed write; no prompt ever reached a native
        # session for the first message.
        row = self.snapshot_row(conversation_id)
        self.assertIsNotNone(row)
        self.assertEqual(adapter.started, 0)
        self.assertEqual(adapter.resumed, 0)

        second_message = self.requeue(conversation_id)
        broker.notify()
        self.wait_message_run(conversation_id, second_message, "succeeded")

        self.assertEqual(preparer.compose_calls, 1)
        self.assertEqual(
            self.snapshot_row(conversation_id)["content_sha256"],
            row["content_sha256"],
        )
        self.assertEqual(adapter.messages, ["hello"])

    def test_unbound_legacy_conversation_adopts_once_on_first_resume(
        self,
    ) -> None:
        # No schema_migrations ledger in this fixture: the conversation cannot
        # be proven post-migration, so the legacy adoption rule applies.
        preparer = BootSeamPreparer(
            self.db_path, self.compose, archive_id=self.archive_id
        )
        adapter = BootAssertingAdapter(
            preparer, native_session_ref="native-old"
        )
        broker = self.start_broker(
            lambda _harness: adapter, launch_preparer=preparer
        )
        conversation_id = self.add_conversation(session_ref="native-old")
        first_message = self.add_message(conversation_id)
        broker.notify()
        self.wait_message_run(conversation_id, first_message, "succeeded")

        row = self.snapshot_row(conversation_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["binding_origin"], "legacy_first_resume")
        self.assertEqual(adapter.resumed, 1)
        self.assertEqual(adapter.started, 0)

        second_message = self.requeue(conversation_id)
        broker.notify()
        self.wait_message_run(conversation_id, second_message, "succeeded")
        self.assertEqual(preparer.compose_calls, 1)
        self.assertEqual(
            self.snapshot_row(conversation_id)["content_sha256"],
            row["content_sha256"],
        )
        self.assertEqual(adapter.resumed, 2)

    def test_unbound_post_migration_conversation_fails_closed(self) -> None:
        self.stamp_ledger()

        preparer = BootSeamPreparer(
            self.db_path, self.compose, archive_id=self.archive_id
        )
        adapter = BootAssertingAdapter(preparer)
        broker = self.start_broker(
            lambda _harness: adapter, launch_preparer=preparer
        )
        conversation_id = self.add_conversation(session_ref="native-new")
        message_id = self.add_message(conversation_id)
        broker.notify()
        self.wait_message_run(conversation_id, message_id, "failed")

        con = self.connect()
        error = con.execute(
            "SELECT error_code FROM conversation_runs "
            "WHERE trigger_message_id=?",
            (message_id,),
        ).fetchone()[0]
        con.close()
        self.assertEqual(error, "BOOT_SNAPSHOT_MISSING")
        self.assertIsNone(self.snapshot_row(conversation_id))
        self.assertEqual(adapter.started, 0)
        self.assertEqual(adapter.resumed, 0)
        self.assertEqual(preparer.compose_calls, 0)

    def wait_message_run(
        self, conversation_id: str, message_id: int, state: str
    ) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            con = self.connect()
            row = con.execute(
                "SELECT state FROM conversation_runs "
                "WHERE conversation_id=? AND trigger_message_id=?",
                (conversation_id, message_id),
            ).fetchone()
            con.close()
            if row is not None and row[0] == state:
                return
            time.sleep(0.01)
        self.fail(
            f"message {message_id} run did not reach {state}"
        )

    def test_close_reopen_with_rotation_restores_original_snapshot(self) -> None:
        self.stamp_ledger()
        preparer = BootSeamPreparer(
            self.db_path, self.compose, archive_id=self.archive_id
        )
        adapter = BootAssertingAdapter(
            preparer, native_session_ref="native-A"
        )
        broker = self.start_broker(
            lambda _harness: adapter, launch_preparer=preparer
        )
        chat_a = self.add_conversation()
        first = self.add_message(chat_a)
        broker.notify()
        self.wait_message_run(chat_a, first, "succeeded")
        digest_a = self.snapshot_row(chat_a)["content_sha256"]
        self.assertEqual(
            self.snapshot_row(chat_a)["content"], "boot marker one"
        )

        # Close A, then run a different chat B on the same shell: the shared
        # worktree files move to B's freshly composed snapshot.
        con = self.connect()
        con.execute(
            "UPDATE conversations SET state='closed',closed_at=datetime('now') "
            "WHERE conversation_id=?",
            (chat_a,),
        )
        con.commit()
        con.close()
        self.boot_state["marker"] = "two"
        chat_b = self.add_conversation()
        second = self.add_message(chat_b)
        broker.notify()
        self.wait_message_run(chat_b, second, "succeeded")
        digest_b = self.snapshot_row(chat_b)["content_sha256"]
        self.assertNotEqual(digest_a, digest_b)
        self.assertEqual(
            self.snapshot_row(chat_b)["content"], "boot marker two"
        )
        for name in conversation_boot.BOOT_FILES:
            self.assertEqual(
                (self.worktree / name).read_bytes(), b"boot marker two"
            )

        # Reopen A (the API's closed->idle walk + active-chat rotation): the
        # API auto-closes the idle open chat B first, then A's original
        # snapshot is restored and the original native session resumes.
        con = self.connect()
        con.execute(
            "UPDATE conversations SET state='closed',closed_at=datetime('now') "
            "WHERE conversation_id=?",
            (chat_b,),
        )
        con.execute(
            "UPDATE conversations SET state='idle',closed_at=NULL "
            "WHERE conversation_id=?",
            (chat_a,),
        )
        con.execute(
            "INSERT OR REPLACE INTO active_shell_chats (shell_id,chat_id) "
            "VALUES (1,?)",
            (chat_a,),
        )
        con.commit()
        con.close()
        third = self.requeue(chat_a)
        broker.notify()
        self.wait_message_run(chat_a, third, "succeeded")

        self.assertEqual(adapter.started, 2)  # A's first turn, B's first turn
        self.assertEqual(adapter.resumed, 1)  # A's reopen resumes native-A
        self.assertEqual(preparer.compose_calls, 2)  # one per conversation
        self.assertEqual(
            self.snapshot_row(chat_a)["content_sha256"], digest_a
        )
        for name in conversation_boot.BOOT_FILES:
            self.assertEqual(
                (self.worktree / name).read_bytes(), b"boot marker one"
            )

    def test_broker_restart_between_turns_reuses_the_snapshot(self) -> None:
        self.stamp_ledger()
        preparer = BootSeamPreparer(
            self.db_path, self.compose, archive_id=self.archive_id
        )
        first_adapter = BootAssertingAdapter(preparer)
        broker = self.start_broker(
            lambda _harness: first_adapter, launch_preparer=preparer
        )
        conversation_id = self.add_conversation()
        first = self.add_message(conversation_id)
        broker.notify()
        self.wait_message_run(conversation_id, first, "succeeded")
        digest = self.snapshot_row(conversation_id)["content_sha256"]
        broker.stop()
        broker.join(2)
        self.assertFalse(broker.is_alive())

        # Engine restart: a fresh broker process resumes the same conversation.
        second_adapter = BootAssertingAdapter(preparer)
        restarted = self.start_broker(
            lambda _harness: second_adapter, launch_preparer=preparer
        )
        second = self.requeue(conversation_id)
        restarted.notify()
        self.wait_message_run(conversation_id, second, "succeeded")

        self.assertEqual(preparer.compose_calls, 1)
        self.assertEqual(second_adapter.resumed, 1)
        self.assertEqual(second_adapter.started, 0)
        self.assertEqual(
            self.snapshot_row(conversation_id)["content_sha256"], digest
        )

    def test_every_supported_harness_resumes_exact_session_and_prompt(self) -> None:
        """Claude, Codex, Kimi, and OpenCode chats each bind one snapshot and
        resume the persisted native session with only the next queued prompt."""
        self.stamp_ledger()
        preparer = BootSeamPreparer(
            self.db_path, self.compose, archive_id=self.archive_id
        )
        adapters = {
            harness: BootAssertingAdapter(
                preparer,
                harness=harness,
                native_session_ref=f"native-{harness}",
            )
            for harness in ("claude", "codex", "kimi", "opencode")
        }
        broker = self.start_broker(
            lambda harness: adapters[harness], launch_preparer=preparer
        )
        for harness, adapter in adapters.items():
            with self.subTest(harness=harness):
                conversation_id = self.add_conversation(harness=harness)
                first = self.add_message_body(
                    conversation_id, f"first {harness} prompt"
                )
                broker.notify()
                self.wait_message_run(conversation_id, first, "succeeded")
                digest = self.snapshot_row(conversation_id)["content_sha256"]

                con = self.connect()
                con.execute(
                    "UPDATE conversations SET state='queued' "
                    "WHERE conversation_id=?",
                    (conversation_id,),
                )
                con.commit()
                con.close()
                second = self.add_message_body(
                    conversation_id, f"second {harness} prompt"
                )
                broker.notify()
                self.wait_message_run(conversation_id, second, "succeeded")

                self.assertEqual(adapter.started, 1)
                self.assertEqual(adapter.resumed, 1)
                self.assertEqual(
                    adapter.messages,
                    [f"first {harness} prompt", f"second {harness} prompt"],
                )
                self.assertEqual(
                    adapter.resume_sessions, [f"native-{harness}"]
                )
                self.assertEqual(
                    self.snapshot_row(conversation_id)["content_sha256"],
                    digest,
                )
                con = self.connect()
                con.execute(
                    "UPDATE conversations SET state='closed',"
                    "closed_at=datetime('now') WHERE conversation_id=?",
                    (conversation_id,),
                )
                con.commit()
                con.close()

        # Exactly one composition per conversation across all four harnesses.
        self.assertEqual(preparer.compose_calls, 4)


if __name__ == "__main__":
    unittest.main()
