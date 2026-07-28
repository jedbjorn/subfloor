#!/usr/bin/env python3
"""Standalone chat DB, D1 serialization, migration, and boundary proofs."""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import interface_chat  # noqa: E402
import interface_runtime  # noqa: E402
import map_repo  # noqa: E402
import snapshot  # noqa: E402


class ChatStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp_obj = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_obj.name)
        self.db = self.tmp / "interface_chat.db"
        self.store = interface_chat.ChatStore(self.db)
        self.assertEqual(self.store.migrate(), ["0001_initial"])

    def tearDown(self):
        self.tmp_obj.cleanup()

    def create_session(
        self, session_id: str = "s1", *, provider: str | None = None
    ) -> str:
        self.store.create_session(
            session_id,
            shell_id=7,
            harness="claude",
            cwd=str(self.tmp),
            provider_session_id=provider,
        )
        return session_id

    def begin(self, session_id: str, action: str = "composer", prompt: str = "p"):
        return self.store.request_action(
            session_id,
            action,
            prompt=prompt,
            anchor={"version": 1, "status": "missing", "reason": "test"},
            turn_id=f"turn-{session_id}",
        )

    def test_migration_rerun_is_noop_and_ledger_is_chat_only(self):
        self.assertEqual(self.store.migrate(), [])
        with self.store.connect() as con:
            rows = con.execute(
                "SELECT migration_id, checksum_sha256 "
                "FROM chat_schema_migrations"
            ).fetchall()
            tables = {
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertEqual([row["migration_id"] for row in rows], ["0001_initial"])
        self.assertEqual(len(rows[0]["checksum_sha256"]), 64)
        self.assertIn("chat_sessions", tables)
        self.assertIn("chat_transcript_cursors", tables)
        self.assertNotIn("schema_migrations", tables)
        self.assertNotIn("shells", tables)

    def test_failed_followup_migration_rolls_back_its_partial_schema(self):
        migrations = self.tmp / "migrations"
        migrations.mkdir()
        shutil.copy2(
            ROOT / ".super-coder" / "chat_migrations" / "0001_initial.sql",
            migrations / "0001_initial.sql",
        )
        (migrations / "0002_injected_failure.sql").write_text(
            "CREATE TABLE must_rollback (value TEXT);\n"
            "INSERT INTO table_that_does_not_exist VALUES (1);\n"
        )
        failed_db = self.tmp / "failed.db"
        runtime = interface_chat.ChatRuntime(failed_db, migrations)
        runtime.start()
        self.assertFalse(runtime.available)
        self.assertIn("0002_injected_failure", runtime.unavailable_reason)
        with interface_chat.connect_chat(failed_db) as con:
            tables = {
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            applied = [
                row[0]
                for row in con.execute(
                    "SELECT migration_id FROM chat_schema_migrations"
                )
            ]
        self.assertNotIn("must_rollback", tables)
        self.assertEqual(applied, ["0001_initial"])

    def test_table_driven_state_action_matrix(self):
        cases = [
            ("idle_chat", "composer", "accepted", "running_headless", 2),
            ("idle_chat", "wake", "accepted", "running_headless", 2),
            ("idle_chat", "toggle", "toggle_queued", "idle_chat", 0),
            ("running_headless", "composer", "turn_busy", "running_headless", 2),
            ("running_headless", "wake", "wake_queued", "running_headless", 2),
            ("running_headless", "toggle", "toggle_queued", "running_headless", 2),
            ("hosted_terminal", "composer", "hosted_terminal", "hosted_terminal", 0),
            ("hosted_terminal", "wake", "hosted_terminal", "hosted_terminal", 0),
            ("hosted_terminal", "toggle", "hosted_terminal", "hosted_terminal", 0),
        ]
        for index, (state, action, status, final_state, event_count) in enumerate(cases):
            with self.subTest(state=state, action=action):
                session_id = self.create_session(f"matrix-{index}")
                if state == "running_headless":
                    self.begin(session_id, prompt="winner")
                elif state == "hosted_terminal":
                    with self.store.connect() as con:
                        con.execute(
                            "UPDATE chat_sessions SET host_mode='hosted_terminal' "
                            "WHERE session_id=?",
                            (session_id,),
                        )
                result = self.store.request_action(
                    session_id,
                    action,
                    prompt="loser" if action != "toggle" else None,
                    anchor=(
                        {"version": 1, "status": "missing", "reason": "matrix"}
                        if action != "toggle"
                        else None
                    ),
                    turn_id=f"matrix-turn-{index}",
                )
                session = self.store.session(session_id)
                with self.store.connect() as con:
                    count = con.execute(
                        "SELECT COUNT(*) FROM chat_events WHERE session_id=?",
                        (session_id,),
                    ).fetchone()[0]
                    stored = "\n".join(
                        row[0]
                        for row in con.execute(
                            "SELECT payload_json FROM chat_events "
                            "WHERE session_id=? ORDER BY event_seq",
                            (session_id,),
                        )
                    )
                self.assertEqual(result.status, status)
                self.assertEqual(session["host_mode"], final_state)
                self.assertEqual(count, event_count)
                if state == "running_headless" and action == "composer":
                    self.assertIn("winner", stored)
                    self.assertNotIn("loser", stored)

    def test_concurrent_composers_commit_one_prompt_and_reject_the_other(self):
        session_id = self.create_session("concurrent")
        barrier = threading.Barrier(3)
        results: list[tuple[str, str]] = []

        def submit(name: str) -> None:
            barrier.wait()
            result = self.store.request_action(
                session_id,
                "composer",
                prompt=name,
                anchor={"version": 1, "status": "missing", "reason": "race"},
                turn_id=f"turn-{name}",
            )
            results.append((name, result.status))

        threads = [
            threading.Thread(target=submit, args=("alpha",)),
            threading.Thread(target=submit, args=("bravo",)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(status for _, status in results), ["accepted", "turn_busy"])
        winner = next(name for name, status in results if status == "accepted")
        loser = next(name for name, status in results if status == "turn_busy")
        with self.store.connect() as con:
            rows = con.execute(
                "SELECT kind, payload_json FROM chat_events "
                "WHERE session_id=? ORDER BY event_seq",
                (session_id,),
            ).fetchall()
        self.assertEqual([row["kind"] for row in rows], ["user_message", "turn_started"])
        payloads = "\n".join(row["payload_json"] for row in rows)
        self.assertIn(winner, payloads)
        self.assertNotIn(loser, payloads)

    def test_composer_wake_order_and_coalesced_boundary_requests(self):
        composer_first = self.create_session("composer-first")
        winner = self.begin(composer_first, prompt="human")
        self.assertEqual(winner.status, "accepted")
        self.assertEqual(
            self.store.request_action(
                composer_first,
                "wake",
                prompt="fixed wake",
                anchor={"version": 1, "status": "missing"},
                turn_id="unused-wake",
            ).status,
            "wake_queued",
        )
        self.assertEqual(
            self.store.request_action(
                composer_first,
                "wake",
                prompt="fixed wake",
                anchor={"version": 1, "status": "missing"},
                turn_id="also-unused",
            ).status,
            "wake_queued",
        )
        self.assertEqual(self.store.session(composer_first)["wake_pending"], 1)

        wake_first = self.create_session("wake-first")
        accepted = self.store.request_action(
            wake_first,
            "wake",
            prompt="fixed wake",
            anchor={"version": 1, "status": "missing"},
            turn_id="wake-turn",
        )
        rejected = self.store.request_action(
            wake_first,
            "composer",
            prompt="never queued",
            anchor={"version": 1, "status": "missing"},
            turn_id="composer-turn",
        )
        self.assertEqual((accepted.status, rejected.status), ("accepted", "turn_busy"))
        with self.store.connect() as con:
            payloads = "\n".join(
                row[0]
                for row in con.execute(
                    "SELECT payload_json FROM chat_events WHERE session_id=?",
                    (wake_first,),
                )
            )
        self.assertIn("fixed wake", payloads)
        self.assertNotIn("never queued", payloads)

    def test_concurrent_composer_wake_race_has_one_turn_and_no_content_queue(self):
        session_id = self.create_session("composer-wake-race")
        barrier = threading.Barrier(3)
        results = []

        def act(action: str, prompt: str) -> None:
            barrier.wait()
            result = self.store.request_action(
                session_id,
                action,
                prompt=prompt,
                anchor={"version": 1, "status": "missing", "reason": "race"},
                turn_id=f"race-{action}",
            )
            results.append((action, result.status))

        threads = [
            threading.Thread(target=act, args=("composer", "human payload")),
            threading.Thread(target=act, args=("wake", "fixed wake")),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        statuses = dict(results)
        self.assertEqual(
            statuses["composer"] in {"accepted", "turn_busy"}, True
        )
        self.assertEqual(
            statuses["wake"] in {"accepted", "wake_queued"}, True
        )
        self.assertEqual(
            (statuses["composer"] == "accepted")
            + (statuses["wake"] == "accepted"),
            1,
        )
        with self.store.connect() as con:
            turns = con.execute(
                "SELECT source FROM chat_turns WHERE session_id=?", (session_id,)
            ).fetchall()
            user_payloads = [
                json.loads(row[0])["text"]
                for row in con.execute(
                    "SELECT payload_json FROM chat_events "
                    "WHERE session_id=? AND kind='user_message'",
                    (session_id,),
                )
            ]
        self.assertEqual(len(turns), 1)
        expected_prompt = (
            "human payload" if statuses["composer"] == "accepted" else "fixed wake"
        )
        self.assertEqual(user_payloads, [expected_prompt])

    def test_toggle_coalesces_and_boundary_consumer_takes_it_before_wake(self):
        session_id = self.create_session("boundary")
        active = self.begin(session_id)
        for _ in range(3):
            self.assertEqual(
                self.store.request_action(session_id, "toggle").status,
                "toggle_queued",
            )
        self.store.request_action(
            session_id,
            "wake",
            prompt="fixed",
            anchor={"version": 1, "status": "missing"},
            turn_id="unused",
        )
        state = self.store.session(session_id)
        self.assertEqual((state["toggle_pending"], state["wake_pending"]), (1, 1))
        self.store.complete_turn(active.turn_id, [], exit_code=0)
        self.assertEqual(self.store.consume_boundary_request(session_id), "toggle")
        self.assertEqual(self.store.consume_boundary_request(session_id), "wake")
        self.assertIsNone(self.store.consume_boundary_request(session_id))

    def test_concurrent_toggle_requests_coalesce_to_one_boundary_bit(self):
        session_id = self.create_session("toggle-race")
        active = self.begin(session_id)
        barrier = threading.Barrier(3)
        statuses = []

        def toggle() -> None:
            barrier.wait()
            statuses.append(
                self.store.request_action(session_id, "toggle").status
            )

        threads = [threading.Thread(target=toggle) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(statuses, ["toggle_queued", "toggle_queued"])
        self.assertEqual(self.store.session(session_id)["toggle_pending"], 1)
        self.store.complete_turn(active.turn_id, [], exit_code=0)
        self.assertEqual(self.store.consume_boundary_request(session_id), "toggle")
        self.assertIsNone(self.store.consume_boundary_request(session_id))

    def test_malformed_json_is_rejected_before_any_commit(self):
        session_id = self.create_session("malformed")
        active = self.begin(session_id)
        with self.store.connect() as con:
            before = con.execute(
                "SELECT COUNT(*) FROM chat_events WHERE session_id=?",
                (session_id,),
            ).fetchone()[0]
        with self.assertRaisesRegex(interface_chat.ChatStoreError, "not valid JSON"):
            self.store.append_event_json(
                active.turn_id,
                kind="message_completed",
                role="assistant",
                payload_json='{"broken":',
            )
        with self.store.connect() as con:
            after = con.execute(
                "SELECT COUNT(*) FROM chat_events WHERE session_id=?",
                (session_id,),
            ).fetchone()[0]
            turn_state = con.execute(
                "SELECT state FROM chat_turns WHERE turn_id=?", (active.turn_id,)
            ).fetchone()[0]
        self.assertEqual((before, after, turn_state), (2, 2, "running"))

    def test_events_are_append_only_and_month_index_matches_created_at(self):
        session_id = self.create_session("append-only")
        active = self.begin(session_id)
        self.store.append_events(
            active.turn_id,
            [
                {
                    "kind": "message_completed",
                    "role": "assistant",
                    "payload": {"text": "durable"},
                }
            ],
        )
        with self.store.connect() as con:
            row = con.execute(
                "SELECT event_key, month_key, created_at FROM chat_events "
                "WHERE kind='message_completed'"
            ).fetchone()
            indexes = {
                item[1] for item in con.execute("PRAGMA index_list(chat_events)")
            }
            self.assertEqual(row["month_key"], row["created_at"][:7])
            self.assertIn("chat_events_month_session_seq", indexes)
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                con.execute(
                    "UPDATE chat_events SET role='system' WHERE event_key=?",
                    (row["event_key"],),
                )
            con.rollback()
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                con.execute(
                    "DELETE FROM chat_events WHERE event_key=?", (row["event_key"],)
                )
            con.rollback()

    def test_health_counter_saturates_without_overflow(self):
        self.store.increment_health("claude", ["unknown:future"])
        with self.store.connect() as con:
            con.execute(
                "UPDATE chat_health SET count=?",
                (interface_chat.MAX_HEALTH_COUNT,),
            )
        self.store.increment_health("claude", ["unknown:future"])
        with self.store.connect() as con:
            row = con.execute(
                "SELECT counter_key, count FROM chat_health"
            ).fetchone()
        self.assertEqual(
            tuple(row), ("unknown:future", interface_chat.MAX_HEALTH_COUNT)
        )


class ChatRuntimeIsolationTest(unittest.IsolatedAsyncioTestCase):
    async def test_chat_migration_failure_does_not_disable_tmux_runtime(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            bad_migrations = tmp / "migrations"
            bad_migrations.mkdir()
            (bad_migrations / "0001_bad.sql").write_text("BROKEN SQL;\n")
            runtime = interface_runtime.InterfaceRuntime(
                str(tmp / "shell.db"),
                run_dir=str(tmp / "run"),
                chat_db_path=str(tmp / "chat.db"),
                chat_migrations_dir=str(bad_migrations),
            )
            runtime._check_available = mock.Mock(return_value=None)
            runtime.shadow.start = mock.AsyncMock()
            runtime._occupied_sessions = mock.Mock(return_value=[])
            runtime.reattach_all = mock.AsyncMock(
                return_value={"reattached": [], "lost": []}
            )

            class FakeWake:
                def start(self, _loop):
                    return None

                def startup_pass(self):
                    return None

            with mock.patch.object(
                interface_runtime.interface_wake,
                "WakeCoordinator",
                return_value=FakeWake(),
            ), mock.patch.object(interface_runtime.interface_wake, "bind"):
                await runtime.start()
                self.assertFalse(runtime.chat.available)
                self.assertIn("migration failed", runtime.chat.unavailable_reason)
                self.assertTrue(runtime.available)
                self.assertEqual(runtime.unavailable_reason, "")
                runtime.shadow.stop = mock.AsyncMock()
                await runtime.stop()


class ChatPersistenceBoundaryTest(unittest.TestCase):
    def test_git_snapshot_map_and_code_boundaries_have_positive_controls(self):
        ignored_control = subprocess.run(
            ["git", "check-ignore", "-q", "--", ".super-coder/shell_db.db"],
            cwd=ROOT,
        )
        ignored_chat = subprocess.run(
            ["git", "check-ignore", "-q", "--", ".sc-state/local/interface_chat.db"],
            cwd=ROOT,
        )
        ignored_wal = subprocess.run(
            [
                "git",
                "check-ignore",
                "-q",
                "--",
                ".sc-state/local/interface_chat.db-wal",
            ],
            cwd=ROOT,
        )
        tracked_control = subprocess.run(
            ["git", "check-ignore", "-q", "--", ".super-coder/scripts/interface_chat.py"],
            cwd=ROOT,
        )
        self.assertEqual(ignored_control.returncode, 0)
        self.assertEqual(ignored_chat.returncode, 0)
        self.assertEqual(ignored_wal.returncode, 0)
        self.assertNotEqual(tracked_control.returncode, 0)

        source_skip = set(map_repo.SKIP_DIRS) - {".super-coder"}
        self.assertTrue(
            map_repo.path_is_skipped(
                (".sc-state", "local", "interface_chat.db"),
                source_skip,
                map_repo.SKIP_FILES,
            )
        )
        self.assertFalse(
            map_repo.path_is_skipped(
                (".super-coder", "scripts", "interface_chat.py"),
                source_skip,
                map_repo.SKIP_FILES,
            )
        )

        self.assertIn("interface_sessions", snapshot.PER_INSTANCE_TABLES)
        self.assertFalse(
            any(name.startswith("chat_") for name in snapshot.PER_INSTANCE_TABLES)
        )

        chat_sources = "\n".join(
            (SCRIPTS / name).read_text()
            for name in ("interface_chat.py", "interface_claude_driver.py")
        )
        server_source = (ROOT / ".super-coder" / "api" / "server.py").read_text()
        self.assertIn("shell_db.db", server_source)
        self.assertNotIn("shell_db.db", chat_sources)
        self.assertNotIn("content.sql", chat_sources)
        self.assertNotIn("sc mem", chat_sources)


if __name__ == "__main__":
    unittest.main()
