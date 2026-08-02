"""Cross-harness browser journey and crash-window release gate."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "api"))
sys.path.insert(0, str(ENGINE / "scripts"))

import conversation_broker
import conversation_routes
from conversation_adapters import (
    ConversationContext,
    InterruptResult,
    NativeTurn,
    NormalizedEvent,
    ReconcileResult,
)

REQUIRED_HARNESSES = ("claude", "codex", "kimi", "opencode")
ACTIONABLE_BOUNDARY_HARNESSES = {"codex", "opencode"}
TERMINAL_RUN_STATES = {"succeeded", "failed", "cancelled", "unknown"}


def apply_schema(con: sqlite3.Connection) -> None:
    con.executescript((ENGINE / "schema.sql").read_text())
    for migration in sorted((ENGINE / "migrations").glob("*.sql")):
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")


def decoded(response):
    status, headers, body = response
    return status, dict(headers), json.loads(body)


class NativeHarnessState:
    """Harness-owned state that intentionally outlives broker instances."""

    def __init__(self, harness: str) -> None:
        self.harness = harness
        self.sessions: set[str] = set()
        self.started = 0
        self.resumed: list[str] = []
        self.reconciled: list[tuple[str, str]] = []
        self.run_serial = 0
        self.segmented_trace = False
        self.actionable_boundaries = False
        self.stream_barrier: threading.Barrier | None = None
        self.stream_lock = threading.Lock()
        self.active_streams = 0
        self.max_active_streams = 0

    def next_run(self, prefix: str) -> str:
        self.run_serial += 1
        return f"{self.harness}-{prefix}-{self.run_serial}"


class ReleaseGateAdapter:
    """Deterministic native boundary used by the full broker/API journey."""

    def __init__(self, state: NativeHarnessState) -> None:
        self.harness = state.harness
        self.state = state
        self.interrupted = threading.Event()

    def start(
        self,
        context: ConversationContext,
        message: str,
    ) -> NativeTurn:
        context.checked_worktree()
        self.state.started += 1
        session_ref = f"{self.harness}-session-{self.state.started}"
        self.state.sessions.add(session_ref)
        return NativeTurn(
            harness=self.harness,
            session_ref=session_ref,
            run_ref=self.state.next_run("start"),
            worktree=context.worktree,
        )

    def resume(
        self,
        session_ref: str,
        context: ConversationContext,
        message: str,
    ) -> NativeTurn:
        context.checked_worktree()
        if session_ref not in self.state.sessions:
            raise AssertionError(
                f"{self.harness} received an unknown session: {session_ref}"
            )
        self.state.resumed.append(session_ref)
        return NativeTurn(
            harness=self.harness,
            session_ref=session_ref,
            run_ref=self.state.next_run("resume"),
            worktree=context.worktree,
        )

    def stream(self, turn: NativeTurn) -> Iterator[NormalizedEvent]:
        with self.state.stream_lock:
            self.state.active_streams += 1
            self.state.max_active_streams = max(
                self.state.max_active_streams,
                self.state.active_streams,
            )
        try:
            if self.state.stream_barrier is not None:
                self.state.stream_barrier.wait(timeout=3)
            yield NormalizedEvent(
                "session.started",
                {"session_ref": turn.session_ref},
            )
            yield NormalizedEvent("run.started", {"status": "running"})
            if self.state.segmented_trace:
                yield NormalizedEvent(
                    "assistant.delta",
                    {"text": f"{self.harness} before tool"},
                )
                yield NormalizedEvent("tool.started", {"name": "write"})
                yield NormalizedEvent("tool.completed", {"name": "write"})
                yield NormalizedEvent(
                    "assistant.delta",
                    {"text": f"{self.harness} after tool"},
                )
                if self.state.actionable_boundaries:
                    yield NormalizedEvent(
                        "permission.requested",
                        {"detail": "approve release-gate write"},
                    )
                    yield NormalizedEvent(
                        "assistant.delta",
                        {"text": f"{self.harness} after permission"},
                    )
                    yield NormalizedEvent(
                        "input.requested",
                        {"detail": "choose release-gate target"},
                    )
                    yield NormalizedEvent(
                        "assistant.delta",
                        {"text": f"{self.harness} after input"},
                    )
            else:
                yield NormalizedEvent(
                    "assistant.delta",
                    {"text": f"{self.harness} completed the turn"},
                )
            yield NormalizedEvent("run.completed", {"status": "succeeded"})
        finally:
            with self.state.stream_lock:
                self.state.active_streams -= 1

    def interrupt(self, turn: NativeTurn) -> InterruptResult:
        self.interrupted.set()
        return InterruptResult(True)

    def reconcile(
        self,
        turn: NativeTurn,
        context: ConversationContext,
    ) -> ReconcileResult:
        context.checked_worktree()
        if turn.session_ref not in self.state.sessions:
            return ReconcileResult(
                "unknown",
                False,
                f"{self.harness} session is not in the native store",
            )
        self.state.reconciled.append((turn.session_ref, turn.run_ref))
        return ReconcileResult(
            "succeeded",
            True,
            f"{self.harness} proved the exact native run completed",
        )

    def close(self) -> None:
        return None


class CrossHarnessReleaseGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.db_path = self.root / "shell.db"
        con = sqlite3.connect(self.db_path)
        apply_schema(con)
        con.execute(
            "INSERT INTO users (user_id,username,is_active) "
            "VALUES (1,'operator',1)"
        )
        con.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (1,'Developer','dev','dev','prompt',1),"
            "(2,'Developer 2','dev2','dev','prompt',1)"
        )
        con.commit()
        con.close()
        (self.root / ".sc-worktrees" / "dev").mkdir(parents=True)
        (self.root / ".sc-worktrees" / "dev2").mkdir(parents=True)

        self.active_broker: conversation_broker.ConversationBroker | None = None
        self.brokers: list[conversation_broker.ConversationBroker] = []
        self.patches = (
            mock.patch.object(conversation_routes, "DB_PATH", self.db_path),
            mock.patch.object(
                conversation_routes.run_mod,
                "REPO_ROOT",
                self.root,
            ),
            mock.patch.object(
                conversation_routes,
                "_wait_for_cli_release",
                return_value=None,
            ),
            mock.patch.object(
                conversation_routes,
                "_live_shell_session",
                return_value=None,
            ),
            mock.patch.object(
                conversation_routes.conversation_broker,
                "notify_commit",
                side_effect=self.notify_broker,
            ),
        )
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        for broker in self.brokers:
            broker.stop()
            broker.join(2)
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def notify_broker(self) -> bool:
        if self.active_broker is None or not self.active_broker.is_alive():
            return False
        self.active_broker.notify()
        return True

    def start_broker(
        self,
        state: NativeHarnessState,
    ) -> conversation_broker.ConversationBroker:
        def factory(harness: str) -> ReleaseGateAdapter:
            self.assertEqual(harness, state.harness)
            return ReleaseGateAdapter(state)

        broker = conversation_broker.ConversationBroker(
            self.db_path,
            adapter_factory=factory,
            owner=f"release-gate-{state.harness}-{len(self.brokers) + 1}",
            heartbeat_seconds=60,
            recovery_seconds=60,
        )
        self.brokers.append(broker)
        self.active_broker = broker
        broker.start()
        self.assertTrue(broker.wait_started())
        return broker

    @staticmethod
    def headers(*, key: str | None = None) -> str:
        values = {"Host": "localhost:8800"}
        if key is not None:
            values["Idempotency-Key"] = key
        return "\r\n".join(f"{name}: {value}" for name, value in values.items())

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        key: str | None = None,
    ):
        return decoded(
            conversation_routes.handle(
                method,
                path,
                self.headers(key=key),
                json.dumps(body).encode() if body is not None else b"",
            )
        )

    def wait_for_run_count(self, count: int, timeout: float = 3) -> list:
        deadline = time.monotonic() + timeout
        rows = []
        while time.monotonic() < deadline:
            con = self.connect()
            rows = con.execute(
                "SELECT run_id,state,harness_session_before,"
                "harness_session_after,runner_ref "
                "FROM conversation_runs ORDER BY run_id"
            ).fetchall()
            con.close()
            if len(rows) >= count and all(
                row["state"] in TERMINAL_RUN_STATES for row in rows[:count]
            ):
                return rows
            time.sleep(0.01)
        self.fail(f"{count} conversation run(s) did not become terminal")

    def expire_run(self, run_id: int) -> None:
        con = self.connect()
        con.execute(
            "UPDATE conversation_runs SET lease_expires_at="
            "'2000-01-01 00:00:00' WHERE run_id=?",
            (run_id,),
        )
        con.commit()
        con.close()

    def stop_broker(
        self,
        broker: conversation_broker.ConversationBroker,
    ) -> None:
        broker.stop()
        broker.join(2)
        self.active_broker = None

    def assert_release_journey(self, harness: str) -> None:
        self.assertIn(harness, REQUIRED_HARNESSES)
        state = NativeHarnessState(harness)
        state.segmented_trace = True
        state.actionable_boundaries = harness in ACTIONABLE_BOUNDARY_HARNESSES
        first_broker = self.start_broker(state)
        create_body = {"shell_id": 1, "harness": harness}
        if harness == "opencode":
            create_body["model"] = "openai/test-model"
        status, _, conversation = self.request(
            "POST",
            "/api/conversations",
            body=create_body,
            key=f"{harness}-create",
        )
        self.assertEqual(status, 201, conversation)
        conversation_id = conversation["conversation_id"]

        status, _, first = self.request(
            "POST",
            f"/api/conversations/{conversation_id}/messages",
            body={"text": "remember release-gate-token"},
            key=f"{harness}-first",
        )
        self.assertEqual(status, 202, first)
        first_rows = self.wait_for_run_count(1)
        session_ref = first_rows[0]["harness_session_after"]
        self.assertEqual(state.started, 1)
        self.assertEqual(state.resumed, [])
        self.assertEqual(first_rows[0]["state"], "succeeded")
        self.assertIsNone(first_rows[0]["harness_session_before"])
        self.assertIn(session_ref, state.sessions)

        self.stop_broker(first_broker)
        second_broker = self.start_broker(state)

        status, _, restored = self.request(
            "GET",
            f"/api/conversations/{conversation_id}",
        )
        self.assertEqual(status, 200, restored)
        self.assertEqual(restored["state"], "idle")
        status, _, history = self.request(
            "GET",
            f"/api/conversations/{conversation_id}/messages",
        )
        self.assertEqual(status, 200, history)
        self.assertEqual(
            [item["state"] for item in history["items"]],
            ["completed"],
        )
        replay = conversation_routes._event_batch(conversation_id, 0)
        self.assertIn(
            "run.completed",
            [event["event_type"] for event in replay],
        )
        self.assertNotIn(session_ref, json.dumps(replay))

        status, _, second = self.request(
            "POST",
            f"/api/conversations/{conversation_id}/messages",
            body={"text": "what token did the first turn retain?"},
            key=f"{harness}-second",
        )
        self.assertEqual(status, 202, second)
        second_rows = self.wait_for_run_count(2)
        self.assertEqual([row["state"] for row in second_rows], [
            "succeeded",
            "succeeded",
        ])
        self.assertEqual(state.started, 1)
        self.assertEqual(state.resumed, [session_ref])
        self.assertEqual(
            (
                second_rows[1]["harness_session_before"],
                second_rows[1]["harness_session_after"],
            ),
            (session_ref, session_ref),
        )
        self.assertNotEqual(
            first_rows[0]["runner_ref"],
            second_rows[1]["runner_ref"],
        )
        self.assert_segmented_transcript(
            conversation_id,
            harness,
            [int(row["run_id"]) for row in second_rows],
        )

        status, _, duplicate = self.request(
            "POST",
            f"/api/conversations/{conversation_id}/messages",
            body={"text": "what token did the first turn retain?"},
            key=f"{harness}-second",
        )
        self.assertEqual(status, 202, duplicate)
        self.assertEqual(
            duplicate["message"]["message_id"],
            second["message"]["message_id"],
        )
        self.assertEqual(len(self.wait_for_run_count(2)), 2)

        self.stop_broker(second_broker)
        status, _, starting_message = self.request(
            "POST",
            f"/api/conversations/{conversation_id}/messages",
            body={"text": "crash before native identity is durable"},
            key=f"{harness}-starting-crash",
        )
        self.assertEqual(status, 202, starting_message)
        store = conversation_broker.BrokerStore(self.db_path)
        starting_run = store.claim_next("dead-starting")
        self.assertIsNotNone(starting_run)
        store.mark_starting(starting_run.run_id, "dead-starting")
        self.expire_run(starting_run.run_id)

        starting_recovery = self.start_broker(state)
        starting_rows = self.wait_for_run_count(3)
        self.assertEqual(starting_rows[-1]["state"], "unknown")
        self.assertEqual(state.started, 1)
        self.assertEqual(state.resumed, [session_ref])
        self.assertEqual(state.reconciled, [])
        self.stop_broker(starting_recovery)

        status, _, replacement = self.request(
            "POST",
            "/api/conversations",
            body=create_body,
            key=f"{harness}-replacement",
        )
        self.assertEqual(status, 201, replacement)
        replacement_id = replacement["conversation_id"]
        status, _, running_message = self.request(
            "POST",
            f"/api/conversations/{replacement_id}/messages",
            body={"text": "crash after native identity is durable"},
            key=f"{harness}-running-crash",
        )
        self.assertEqual(status, 202, running_message)
        running_run = store.claim_next("dead-running")
        self.assertIsNotNone(running_run)
        store.mark_starting(running_run.run_id, "dead-running")
        turn = ReleaseGateAdapter(state).start(
            running_run.context(),
            running_run.body,
        )
        store.mark_native_started(
            running_run.run_id,
            "dead-running",
            turn,
        )
        self.expire_run(running_run.run_id)
        starts_before_recovery = state.started
        resumes_before_recovery = list(state.resumed)

        running_recovery = self.start_broker(state)
        running_rows = self.wait_for_run_count(4)
        self.assertEqual(running_rows[-1]["state"], "succeeded")
        self.assertEqual(state.started, starts_before_recovery)
        self.assertEqual(state.resumed, resumes_before_recovery)
        self.assertEqual(
            state.reconciled,
            [(turn.session_ref, turn.run_ref)],
        )
        self.stop_broker(running_recovery)

    def assert_segmented_transcript(
        self,
        conversation_id: str,
        harness: str,
        run_ids: list[int],
    ) -> None:
        with closing(self.connect()) as con:
            boundary_sequences = {
                (int(row["run_id"]), row["event_type"]): int(row["sequence"])
                for row in con.execute(
                    "SELECT run_id,event_type,sequence FROM conversation_events "
                    "WHERE conversation_id=? AND run_id IS NOT NULL AND "
                    "event_type IN ('tool.completed','permission.requested',"
                    "'input.requested')",
                    (conversation_id,),
                )
            }

        expected = []
        for run_id in run_ids:
            expected_boundary_types = {"tool.completed"}
            if harness in ACTIONABLE_BOUNDARY_HARNESSES:
                expected_boundary_types.update({
                    "permission.requested",
                    "input.requested",
                })
            self.assertEqual(
                {
                    event_type
                    for event_run_id, event_type in boundary_sequences
                    if event_run_id == run_id
                },
                expected_boundary_types,
            )
            tool_sequence = boundary_sequences[(run_id, "tool.completed")]
            expected.extend([
                (
                    f"run:{run_id}:assistant:0",
                    "assistant",
                    f"{harness} before tool",
                ),
                (
                    f"run:{run_id}:assistant:{tool_sequence}",
                    "assistant",
                    f"{harness} after tool",
                ),
            ])
            if harness not in ACTIONABLE_BOUNDARY_HARNESSES:
                continue
            permission_sequence = boundary_sequences[
                (run_id, "permission.requested")
            ]
            input_sequence = boundary_sequences[(run_id, "input.requested")]
            expected.extend([
                (
                    f"event:{permission_sequence}",
                    "permission.requested",
                    "Waiting for permission",
                ),
                (
                    f"run:{run_id}:assistant:{permission_sequence}",
                    "assistant",
                    f"{harness} after permission",
                ),
                (
                    f"event:{input_sequence}",
                    "input.requested",
                    "Waiting for input",
                ),
                (
                    f"run:{run_id}:assistant:{input_sequence}",
                    "assistant",
                    f"{harness} after input",
                ),
            ])

        status, _, transcript = self.request(
            "GET",
            f"/api/conversations/{conversation_id}/transcript",
        )
        self.assertEqual(status, 200, transcript)
        self.assertEqual(transcript["projection_version"], 2)
        self.assertIsNone(transcript["truncation"])
        projected = []
        for item in transcript["items"]:
            if item["kind"] == "user":
                continue
            projected.append((
                item["item_id"],
                (
                    item["activity_type"]
                    if item["kind"] == "activity"
                    else item["kind"]
                ),
                item.get("label", item.get("text")),
            ))
        self.assertEqual(projected, expected)
        item_ids = [item["item_id"] for item in transcript["items"]]
        self.assertEqual(len(item_ids), len(expected) + len(run_ids))
        self.assertEqual(len(item_ids), len(set(item_ids)))
        encoded = json.dumps(transcript)
        self.assertNotIn("tool.started", encoded)
        self.assertNotIn("tool.completed", encoded)

    def test_opencode_release_journey_and_crash_windows(self) -> None:
        self.assert_release_journey("opencode")

    def test_claude_release_journey_and_crash_windows(self) -> None:
        self.assert_release_journey("claude")

    def test_codex_release_journey_and_crash_windows(self) -> None:
        self.assert_release_journey("codex")

    def test_kimi_release_journey_and_crash_windows(self) -> None:
        self.assert_release_journey("kimi")

    def test_same_adapter_conversations_keep_segment_ids_run_scoped(self) -> None:
        state = NativeHarnessState("codex")
        state.segmented_trace = True
        state.stream_barrier = threading.Barrier(2)
        conversation_ids = []
        for number in range(2):
            status, _, created = self.request(
                "POST",
                "/api/conversations",
                body={"shell_id": number + 1, "harness": "codex"},
                key=f"segmented-create-{number}",
            )
            self.assertEqual(status, 201, created)
            conversation_id = created["conversation_id"]
            conversation_ids.append(conversation_id)
            status, _, accepted = self.request(
                "POST",
                f"/api/conversations/{conversation_id}/messages",
                body={"text": f"segmented response {number}"},
                key=f"segmented-message-{number}",
            )
            self.assertEqual(status, 202, accepted)

        first_broker = self.start_broker(state)
        second_broker = self.start_broker(state)
        rows = self.wait_for_run_count(2)
        self.stop_broker(first_broker)
        self.stop_broker(second_broker)
        self.assertEqual(state.max_active_streams, 2)
        self.assertEqual([row["state"] for row in rows], ["succeeded", "succeeded"])

        for conversation_id, row in zip(conversation_ids, rows, strict=True):
            status, _, transcript = self.request(
                "GET",
                f"/api/conversations/{conversation_id}/transcript",
            )
            self.assertEqual(status, 200, transcript)
            run_id = int(row["run_id"])
            assistant = [
                item for item in transcript["items"] if item["kind"] == "assistant"
            ]
            self.assertEqual(
                [(item["item_id"], item["text"]) for item in assistant],
                [
                    (f"run:{run_id}:assistant:0", "codex before tool"),
                    (f"run:{run_id}:assistant:7", "codex after tool"),
                ],
            )


if __name__ == "__main__":
    unittest.main()
