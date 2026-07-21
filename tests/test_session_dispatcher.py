#!/usr/bin/env python3
"""Hermetic wake-dispatch tests with fake provider adapters."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"
sys.path.insert(0, str(ENGINE / "scripts"))

import session_control  # noqa: E402
import session_dispatcher as dispatcher  # noqa: E402


def make_db(path: str = ":memory:") -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=5, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA.read_text())
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(
        "INSERT INTO users (user_id, username, is_active) VALUES (1, 'T', 1);"
        "INSERT INTO shells (shell_id, display_name, shortname, flavor, "
        "system_prompt, user_id, api_key) "
        "VALUES (1, 'Planner', 'PLN1', 'planner', 'x', 1, 'planner-token');"
        "INSERT INTO shell_memory_archives "
        "(archive_id, shell_id, session_id, date, harness, provider, model) "
        "VALUES (10, 1, '0007', '2026-07-21', 'fake', 'test', 'model');"
        "INSERT INTO shell_session_bindings "
        "(binding_id, archive_id, shell_id, harness, native_session_id, "
        "state, managed) VALUES (20, 10, 1, 'fake', 'native-1', 'idle', 1);"
    )
    con.commit()
    return con


def add_message(con: sqlite3.Connection, body: str = "secret event body") -> int:
    mid = con.execute(
        "INSERT INTO shell_messages (from_shell_id, to_shell_id, body, kind) "
        "VALUES (1, 1, ?, 'result')", (body,)
    ).lastrowid
    con.commit()
    return mid


class FakeAdapter:
    def __init__(self, *, state: str = "idle", deliver=None, resume=None):
        self.state = state
        self.deliver_fn = deliver or (lambda _binding, _prompt: None)
        self.resume_fn = resume or (lambda _binding, _prompt: None)
        self.prompts: list[str] = []
        self.deliveries = 0
        self.resumes = 0

    def status(self, _binding: dict) -> str:
        return self.state

    def deliver(self, binding: dict, prompt: str) -> None:
        self.deliveries += 1
        self.prompts.append(prompt)
        self.deliver_fn(binding, prompt)

    def resume(self, binding: dict, prompt: str) -> None:
        self.resumes += 1
        self.prompts.append(prompt)
        self.resume_fn(binding, prompt)


def no_owner(_con, _binding_id, **_kwargs) -> str:
    return "vacant"


class DispatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.con = make_db()
        self.addCleanup(self.con.close)
        logs = tempfile.TemporaryDirectory()
        self.addCleanup(logs.cleanup)
        self.attempt_log = dispatcher.AttemptLog(Path(logs.name))

    def poll(self, adapter: FakeAdapter, *, api=True) -> int:
        return dispatcher.poll_once(
            self.con,
            adapter_factory=lambda _binding: adapter,
            api_probe=lambda _binding, _base: api,
            reconcile=no_owner,
            lease_preflight=lambda _con, _binding_id, **_kwargs: None,
            attempt_log=self.attempt_log,
        )

    def jobs(self) -> list[tuple]:
        return [tuple(row) for row in self.con.execute(
            "SELECT trigger_message_id, state, attempt_count, last_error "
            "FROM session_wake_jobs ORDER BY trigger_message_id"
        )]

    def test_coalesces_messages_and_acknowledges_only_through_read_at(self):
        first = add_message(self.con, "event A private body")
        second = add_message(self.con, "event B private body")

        def acknowledge(_binding, prompt):
            self.assertEqual(dispatcher.WAKE_PROMPT, prompt)
            self.assertNotIn("event A", prompt)
            self.assertNotIn("event B", prompt)
            self.con.execute(
                "UPDATE shell_messages SET read_at=datetime('now') "
                "WHERE message_id IN (?, ?)", (first, second)
            )
            self.con.commit()

        adapter = FakeAdapter(deliver=acknowledge)
        self.assertEqual(1, self.poll(adapter))
        self.assertEqual((1, 0), (adapter.deliveries, adapter.resumes))
        self.assertEqual(
            [(first, "done", 1, None), (second, "done", 1, None)],
            self.jobs(),
        )
        binding = self.con.execute(
            "SELECT state, last_error FROM shell_session_bindings WHERE binding_id=20"
        ).fetchone()
        self.assertEqual(("idle", None), tuple(binding))

    def test_message_arriving_during_turn_gets_an_audit_row(self):
        first = add_message(self.con, "event A")
        arrived: list[int] = []

        def acknowledge_with_arrival(_binding, _prompt):
            arrived.append(add_message(self.con, "event B during turn"))
            self.con.execute(
                "UPDATE shell_messages SET read_at=datetime('now') "
                "WHERE message_id IN (?, ?)", (first, arrived[0])
            )
            self.con.commit()

        self.assertEqual(1, self.poll(FakeAdapter(deliver=acknowledge_with_arrival)))
        self.assertEqual(
            [(first, "done", 1, None), (arrived[0], "done", 0, None)],
            self.jobs(),
        )
        self.assertEqual(2, self.con.execute(
            "SELECT COUNT(*) FROM session_wake_jobs"
        ).fetchone()[0])

    def test_api_down_never_starts_or_consumes_an_attempt(self):
        message_id = add_message(self.con)
        adapter = FakeAdapter()
        self.assertEqual(0, self.poll(adapter, api=False))
        self.assertEqual((0, 0), (adapter.deliveries, adapter.resumes))
        self.assertEqual([(message_id, "queued", 0, None)], self.jobs())
        binding = self.con.execute(
            "SELECT state, last_error FROM shell_session_bindings WHERE binding_id=20"
        ).fetchone()
        self.assertEqual(
            ("idle", "engine API unavailable for authenticated inbox read"),
            tuple(binding),
        )

    def test_dormant_probe_cannot_resume_over_a_validated_live_owner(self):
        message_id = add_message(self.con)
        adapter = FakeAdapter(state="dormant")
        attempted = dispatcher.poll_once(
            self.con,
            adapter_factory=lambda _binding: adapter,
            api_probe=lambda _binding, _base: True,
            reconcile=lambda _con, _binding_id, **_kwargs: "live",
            lease_preflight=lambda *_args, **_kwargs: self.fail(
                "preflight must not run over a validated live owner"),
            attempt_log=self.attempt_log,
        )
        self.assertEqual(0, attempted)
        self.assertEqual((0, 0), (adapter.deliveries, adapter.resumes))
        self.assertEqual([(message_id, "queued", 0, None)], self.jobs())

    def test_dormant_resume_runs_lease_preflight_before_adapter(self):
        message_id = add_message(self.con)
        calls: list[str] = []

        def resume(_binding, _prompt):
            calls.append("resume")
            self.con.execute(
                "UPDATE shell_messages SET read_at=datetime('now') "
                "WHERE message_id=?", (message_id,)
            )
            self.con.commit()

        adapter = FakeAdapter(state="dormant", resume=resume)

        def preflight(_con, _binding_id, **_kwargs):
            calls.append("preflight")

        attempted = dispatcher.poll_once(
            self.con,
            adapter_factory=lambda _binding: adapter,
            api_probe=lambda _binding, _base: True,
            reconcile=no_owner,
            lease_preflight=preflight,
            attempt_log=self.attempt_log,
        )
        self.assertEqual(1, attempted)
        self.assertEqual(["preflight", "resume"], calls)
        self.assertEqual((0, 1), (adapter.deliveries, adapter.resumes))
        self.assertEqual([(message_id, "done", 1, None)], self.jobs())

    def test_unacknowledged_failures_retry_then_enter_error_without_reading_message(self):
        message_id = add_message(self.con)

        def fail(_binding, _prompt):
            raise RuntimeError("Bearer super-secret token=also-secret transport down")

        adapter = FakeAdapter(deliver=fail)
        for attempt in range(1, dispatcher.MAX_ATTEMPTS + 1):
            self.assertEqual(1, self.poll(adapter))
            row = self.con.execute(
                "SELECT state, attempt_count, last_error FROM session_wake_jobs "
                "WHERE trigger_message_id=?", (message_id,)
            ).fetchone()
            self.assertEqual(attempt, row["attempt_count"])
            self.assertNotIn("super-secret", row["last_error"])
            self.assertNotIn("also-secret", row["last_error"])
            if attempt < dispatcher.MAX_ATTEMPTS:
                self.assertEqual("queued", row["state"])
                delay = self.con.execute(
                    "SELECT CAST(strftime('%s', available_at) AS INTEGER) - "
                    "CAST(strftime('%s', 'now') AS INTEGER) "
                    "FROM session_wake_jobs WHERE trigger_message_id=?",
                    (message_id,),
                ).fetchone()[0]
                expected = dispatcher.RETRY_DELAYS[attempt - 1]
                self.assertGreaterEqual(delay, expected - 1)
                self.assertLessEqual(delay, expected)
                self.con.execute(
                    "UPDATE session_wake_jobs SET available_at=datetime('now','-1 second') "
                    "WHERE trigger_message_id=?", (message_id,)
                )
                self.con.commit()
            else:
                self.assertEqual("failed", row["state"])

        binding = self.con.execute(
            "SELECT state, last_error FROM shell_session_bindings WHERE binding_id=20"
        ).fetchone()
        self.assertEqual("error", binding["state"])
        self.assertNotIn("super-secret", binding["last_error"])
        self.assertIsNone(self.con.execute(
            "SELECT read_at FROM shell_messages WHERE message_id=?", (message_id,)
        ).fetchone()[0])
        self.assertEqual(dispatcher.MAX_ATTEMPTS, adapter.deliveries)

    def test_crash_left_running_job_is_requeued_without_starting_a_second_writer(self):
        message_id = add_message(self.con)
        session_control.reconstruct_wake_jobs(self.con)
        self.con.commit()
        batch = dispatcher.claim_batch(self.con, 20)
        self.assertEqual((message_id,), batch.message_ids)
        adapter = FakeAdapter(state="dormant")

        self.assertEqual(0, self.poll(adapter))
        self.assertEqual((0, 0), (adapter.deliveries, adapter.resumes))
        row = self.con.execute(
            "SELECT state, attempt_count, last_error FROM session_wake_jobs"
        ).fetchone()
        self.assertEqual(
            ("queued", 1, "dispatcher restarted before inbox acknowledgement"),
            tuple(row),
        )
        self.assertEqual("dormant", self.con.execute(
            "SELECT state FROM shell_session_bindings WHERE binding_id=20"
        ).fetchone()[0])


class ConcurrentClaimTest(unittest.TestCase):
    def test_two_dispatchers_claim_exactly_one_batch(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = str(Path(tmp.name) / "dispatcher.db")
        seed = make_db(path)
        add_message(seed)
        session_control.reconstruct_wake_jobs(seed)
        seed.commit()
        seed.close()
        barrier = threading.Barrier(2)
        claims: list[dispatcher.WakeBatch | None] = []

        def claim() -> None:
            con = sqlite3.connect(path, timeout=5)
            con.row_factory = sqlite3.Row
            try:
                barrier.wait(timeout=5)
                claims.append(dispatcher.claim_batch(con, 20))
            finally:
                con.close()

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(1, sum(batch is not None for batch in claims))

        with sqlite3.connect(path) as con:
            row = con.execute(
                "SELECT state, attempt_count FROM session_wake_jobs"
            ).fetchone()
            binding_state = con.execute(
                "SELECT state FROM shell_session_bindings WHERE binding_id=20"
            ).fetchone()[0]
        self.assertEqual(("running", 1), row)
        self.assertEqual("dispatching", binding_state)


class SanitizationTest(unittest.TestCase):
    def test_sanitizes_common_secret_shapes_and_bounds_length(self):
        error = dispatcher.sanitize_error(
            "Authorization=abc token:xyz https://user:pass@example.test/ " + "x" * 900
        )
        self.assertNotIn("abc", error)
        self.assertNotIn("xyz", error)
        self.assertNotIn("pass", error)
        self.assertIn("[REDACTED]", error)
        self.assertEqual(dispatcher.MAX_ERROR_CHARS, len(error))

    def test_heartbeat_upserts_one_named_dispatcher_row(self):
        con = make_db()
        self.addCleanup(con.close)
        dispatcher.beat(con, 1)
        dispatcher.beat(con, 3)
        rows = [tuple(row) for row in con.execute(
            "SELECT name, interval_s FROM daemon_heartbeats ORDER BY name"
        )]
        self.assertEqual([("session-dispatcher", 3)], rows)

    def test_attempt_log_is_private_bounded_and_redacted(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        log = dispatcher.AttemptLog(Path(tmp.name))
        for index in range(dispatcher.LOG_LINES + 3):
            log.write(7, "failed", index=index, error="Bearer secret-value")
        path = Path(tmp.name) / "binding-7.jsonl"
        lines = path.read_text().splitlines()
        self.assertEqual(dispatcher.LOG_LINES, len(lines))
        self.assertNotIn("secret-value", "\n".join(lines))
        self.assertIn("[REDACTED]", lines[-1])
        self.assertEqual(0o600, path.stat().st_mode & 0o777)


if __name__ == "__main__":
    unittest.main()
