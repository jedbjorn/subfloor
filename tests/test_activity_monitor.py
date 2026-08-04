"""Mechanical activity monitor, wake backoff, and engine recovery gates."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
MIGRATIONS = ENGINE / "migrations"
sys.path.insert(0, str(ENGINE / "scripts"))

import activity_monitor  # noqa: E402
import conversation_reaper  # noqa: E402
import db_driver  # noqa: E402
import sprint_domain  # noqa: E402
import sprint_message_delivery  # noqa: E402
import sprint_runtime  # noqa: E402


def apply_schema(con: sqlite3.Connection) -> None:
    con.executescript((ENGINE / "schema.sql").read_text())
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")


class ActivityMonitorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "activity.db"
        self.con = db_driver.connect(self.db_path)
        self.addCleanup(self.con.close)
        apply_schema(self.con)
        self.con.execute("INSERT INTO users (user_id,username) VALUES (1,'operator')")
        self.con.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (1,'Developer','DEV1','dev','prompt',1)"
        )
        self.con.commit()

    def add_live_chat(self, *, idle_seconds: int) -> tuple[str, int, int]:
        self.con.execute(
            "INSERT INTO conversations "
            "(shell_id,owner_user_id,harness,worktree,state,last_activity_at,"
            "creation_idempotency_key,creation_request_hash) "
            "VALUES (1,1,'codex','/tmp/activity','running',datetime('now', ?),"
            "'activity-chat','activity-hash')",
            (f"-{idle_seconds} seconds",),
        )
        chat_id = str(
            self.con.execute(
                "SELECT conversation_id FROM conversations "
                "WHERE creation_idempotency_key='activity-chat'"
            ).fetchone()[0]
        )
        message_id = int(
            self.con.execute(
                "INSERT INTO conversation_messages "
                "(conversation_id,sender_kind,sender_ref,message_kind,body,"
                "idempotency_key,request_hash,state) "
                "VALUES (?,'engine','test','prompt','hung turn',"
                "'activity-message','activity-request','running')",
                (chat_id,),
            ).lastrowid
        )
        outbox_id = int(
            self.con.execute(
                "INSERT INTO conversation_outbox (conversation_id,message_id) "
                "VALUES (?,?)",
                (chat_id, message_id),
            ).lastrowid
        )
        self.con.execute(
            "UPDATE conversation_outbox SET state='claimed',claim_owner='broker',"
            "claimed_at=datetime('now'),lease_expires_at='2999-01-01 00:00:00' "
            "WHERE outbox_id=?",
            (outbox_id,),
        )
        run_id = int(
            self.con.execute(
                "INSERT INTO conversation_runs "
                "(conversation_id,shell_id,trigger_message_id,state,lease_owner,"
                "lease_expires_at,started_at,heartbeat_at,process_pid,"
                "process_start_ticks,process_group_id) "
                "VALUES (?,1,?,'running','broker','2999-01-01 00:00:00',"
                "datetime('now', ?),datetime('now'),4242,9001,4242)",
                (chat_id, message_id, f"-{idle_seconds} seconds"),
            ).lastrowid
        )
        self.con.execute(
            "UPDATE conversation_outbox SET state='dispatched',run_id=?,"
            "dispatched_at=datetime('now') WHERE outbox_id=?",
            (run_id, outbox_id),
        )
        self.con.execute(
            "INSERT INTO active_shell_chats "
            "(shell_id,chat_id,process_pid,process_start_ticks) "
            "VALUES (1,?,4242,9001)",
            (chat_id,),
        )
        self.con.commit()
        return chat_id, message_id, run_id

    def test_config_defaults_to_sixty_minutes_and_falls_back_for_invalid_values(
        self,
    ) -> None:
        self.assertEqual(
            3600,
            activity_monitor.ActivityMonitorConfig.from_env(
                {}
            ).inactivity_ceiling_seconds,
        )
        self.assertEqual(
            90,
            activity_monitor.ActivityMonitorConfig.from_env(
                {"SC_CHAT_INACTIVITY_CEILING": "90"}
            ).inactivity_ceiling_seconds,
        )
        for value in ("0", "-1", "later", "nan", "inf"):
            with self.subTest(value=value), self.assertLogs(
                "super_coder.activity_monitor", level="WARNING"
            ) as logs:
                config = activity_monitor.ActivityMonitorConfig.from_env(
                    {"SC_CHAT_INACTIVITY_CEILING": value}
                )
            self.assertEqual(3600, config.inactivity_ceiling_seconds)
            self.assertEqual(1, len(logs.output))
            self.assertIn("using default 3600 seconds", logs.output[0])

    def test_malformed_ceiling_does_not_kill_runtime_thread(self) -> None:
        runtime = sprint_runtime.SprintRuntimeService(
            self.db_path,
            owner="invalid-activity-config-test",
            pulse_seconds=60,
        )

        with (
            mock.patch.dict(
                activity_monitor.os.environ,
                {"SC_CHAT_INACTIVITY_CEILING": "later"},
            ),
            self.assertLogs("super_coder.activity_monitor", level="WARNING"),
        ):
            runtime.start()
            try:
                started = runtime.wait_started()
                alive = runtime.is_alive()
            finally:
                runtime.stop()
                runtime.join(timeout=5)

        self.assertTrue(started)
        self.assertTrue(alive)
        self.assertFalse(runtime.is_alive())

    def test_stale_live_chat_closes_and_unlinks_without_finishing_run(self) -> None:
        chat_id, message_id, run_id = self.add_live_chat(idle_seconds=3601)
        monitor = activity_monitor.ActivityMonitor(
            self.con,
            config=activity_monitor.ActivityMonitorConfig(3600),
        )

        outcome = monitor.tick()

        self.assertEqual((chat_id,), outcome.closed_chat_ids)
        self.assertEqual((), outcome.recovered_wake_ids)
        self.assertEqual(
            ("closed", "running", "running", 0),
            tuple(
                self.con.execute(
                    "SELECT c.state,r.state,m.state,"
                    "(SELECT COUNT(*) FROM active_shell_chats WHERE shell_id=1) "
                    "FROM conversations c JOIN conversation_runs r "
                    "ON r.conversation_id=c.conversation_id "
                    "JOIN conversation_messages m ON m.message_id=r.trigger_message_id "
                    "WHERE c.conversation_id=? AND r.run_id=? AND m.message_id=?",
                    (chat_id, run_id, message_id),
                ).fetchone()
            ),
        )
        event = self.con.execute(
            "SELECT event_type,payload,run_id FROM conversation_events "
            "WHERE conversation_id=? ORDER BY sequence DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        self.assertEqual(("conversation.closed", run_id), (event[0], event[2]))
        self.assertEqual(
            {
                "inactivity_ceiling_seconds": 3600,
                "reason": "chat inactivity ceiling exceeded",
                "state": "closed",
            },
            json.loads(event[1]),
        )
        interrupted: list[int] = []
        reaper = conversation_reaper.ConversationReaper(
            self.db_path,
            config=conversation_reaper.ReaperConfig(
                heartbeat_seconds=60,
                term_grace_seconds=15,
                kill_grace_seconds=15,
                young_grace_seconds=0,
            ),
            process_reader=lambda _pid: conversation_reaper.ProcessSnapshot(
                4242, 9001, 4242
            ),
            native_interrupt=interrupted.append,
        )
        self.assertEqual(1, reaper.sweep_once())
        self.assertEqual([run_id], interrupted)
        self.assertEqual(
            ("running", "interrupt"),
            tuple(
                self.con.execute(
                    "SELECT state,reaper_last_signal FROM conversation_runs "
                    "WHERE run_id=?",
                    (run_id,),
                ).fetchone()
            ),
        )

    def test_fresh_live_chat_is_not_closed(self) -> None:
        chat_id, _message_id, _run_id = self.add_live_chat(idle_seconds=3599)

        outcome = activity_monitor.ActivityMonitor(
            self.con,
            config=activity_monitor.ActivityMonitorConfig(3600),
        ).tick()

        self.assertFalse(outcome.changed)
        self.assertEqual(
            ("running", chat_id),
            tuple(
                self.con.execute(
                    "SELECT c.state,a.chat_id FROM conversations c "
                    "JOIN active_shell_chats a ON a.chat_id=c.conversation_id "
                    "WHERE c.conversation_id=?",
                    (chat_id,),
                ).fetchone()
            ),
        )

    def test_runtime_closes_hung_chat_then_dispatches_queued_wake(self) -> None:
        chat_id, _message_id, _run_id = self.add_live_chat(idle_seconds=3601)
        sent = sprint_message_delivery.SprintMessageStore(self.con).send_to_shell(
            1,
            message_kind="system",
            body="queued behind hung turn",
            idempotency_key="hung-turn-wake",
        )
        runtime = sprint_runtime.SprintRuntimeService(
            self.db_path,
            owner="activity-runtime-test",
            activity_config=activity_monitor.ActivityMonitorConfig(3600),
        )

        self.assertTrue(runtime.pulse_once())

        wake = self.con.execute(
            "SELECT state,attempt_count FROM sprint_wake_outbox WHERE wake_id=?",
            (sent.wake_id,),
        ).fetchone()
        active = self.con.execute(
            "SELECT chat_id,process_pid FROM active_shell_chats WHERE shell_id=1"
        ).fetchone()
        self.assertEqual(("delivered", 1), tuple(wake))
        self.assertNotEqual(chat_id, active["chat_id"])
        self.assertIsNone(active["process_pid"])
        self.assertEqual(
            "closed",
            self.con.execute(
                "SELECT state FROM conversations WHERE conversation_id=?",
                (chat_id,),
            ).fetchone()[0],
        )
        native = self.con.execute(
            "SELECT body,idempotency_key,state FROM conversation_messages "
            "WHERE conversation_id=? AND sender_ref='sprint-runtime'",
            (active["chat_id"],),
        ).fetchone()
        wake_key = self.con.execute(
            "SELECT idempotency_key FROM sprint_wake_outbox WHERE wake_id=?",
            (sent.wake_id,),
        ).fetchone()[0]
        expected_prompt = (
            "Wake-message delivery. Act on every message below.\n\n"
            f"## wake_message #{sent.message_id} (declared Re-Enter)\n\n"
            "queued behind hung turn"
        )
        self.assertEqual(
            (
                expected_prompt,
                wake_key,
                "queued",
            ),
            tuple(native),
        )

    def test_force_new_waits_for_ceiling_reaper_and_quiet_before_delivery(self) -> None:
        chat_id, _message_id, run_id = self.add_live_chat(idle_seconds=3601)
        sent = sprint_message_delivery.SprintMessageStore(self.con).send_to_shell(
            1,
            message_kind="system",
            body="fresh chat after the hung turn",
            idempotency_key="hung-turn-force-new",
            declared_type="force-new",
        )
        runtime = sprint_runtime.SprintRuntimeService(
            self.db_path,
            owner="force-new-ceiling-test",
            activity_config=activity_monitor.ActivityMonitorConfig(3600),
        )

        with mock.patch.dict(
            sprint_message_delivery.os.environ,
            {"SC_SPRINT_FORCE_NEW_QUIET_SECONDS": "10"},
        ):
            self.assertTrue(runtime.pulse_once())

        first = self.con.execute(
            "SELECT state,attempt_count,quiet_since FROM sprint_wake_outbox "
            "WHERE wake_id=?",
            (sent.wake_id,),
        ).fetchone()
        self.assertEqual(("pending", 0), tuple(first[:2]))
        self.assertIsNotNone(first["quiet_since"])
        self.assertEqual(
            ("closed", 0),
            tuple(
                self.con.execute(
                    "SELECT state,(SELECT COUNT(*) FROM active_shell_chats "
                    "WHERE shell_id=1) FROM conversations WHERE conversation_id=?",
                    (chat_id,),
                ).fetchone()
            ),
        )
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_wake_attempts WHERE wake_id=?",
                (sent.wake_id,),
            ).fetchone()[0],
        )

        interrupted: list[int] = []
        reaper = conversation_reaper.ConversationReaper(
            self.db_path,
            config=conversation_reaper.ReaperConfig(
                heartbeat_seconds=60,
                term_grace_seconds=15,
                kill_grace_seconds=15,
                young_grace_seconds=0,
            ),
            process_reader=lambda _pid: conversation_reaper.ProcessSnapshot(
                4242, 9001, 4242
            ),
            native_interrupt=interrupted.append,
        )
        self.assertEqual(1, reaper.sweep_once())
        self.assertEqual([run_id], interrupted)

        self.con.execute(
            "UPDATE sprint_wake_outbox SET quiet_since=datetime('now','-10 seconds') "
            "WHERE wake_id=?",
            (sent.wake_id,),
        )
        self.con.commit()
        with mock.patch.dict(
            sprint_message_delivery.os.environ,
            {"SC_SPRINT_FORCE_NEW_QUIET_SECONDS": "10"},
        ):
            self.assertTrue(runtime.pulse_once())

        delivered = self.con.execute(
            "SELECT state,attempt_count,quiet_since FROM sprint_wake_outbox "
            "WHERE wake_id=?",
            (sent.wake_id,),
        ).fetchone()
        active = self.con.execute(
            "SELECT chat_id,process_pid FROM active_shell_chats WHERE shell_id=1"
        ).fetchone()
        self.assertEqual(("delivered", 1, None), tuple(delivered))
        self.assertNotEqual(chat_id, active["chat_id"])
        self.assertIsNone(active["process_pid"])
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM conversation_messages "
                "WHERE conversation_id=? AND sender_ref='sprint-runtime'",
                (active["chat_id"],),
            ).fetchone()[0],
        )

    def test_failed_engine_wake_is_recovered_once_after_terminal_backoff(self) -> None:
        chat_id, _message_id, _run_id = self.add_live_chat(idle_seconds=1)
        sent = sprint_message_delivery.SprintMessageStore(self.con).send_to_shell(
            1,
            message_kind="system",
            body="recover engine wake",
            idempotency_key="engine-terminal-recovery",
        )
        lifecycle = sprint_domain.SprintLifecycleStore(self.con)
        observed_backoffs: list[int] = []
        for attempt, expected in enumerate((15, 60, 180), start=1):
            self.assertEqual(
                attempt,
                lifecycle.record_wake_failure(
                    sent.wake_id,
                    f"failure {attempt}",
                    target_conversation_id=chat_id,
                ),
            )
            observed_backoffs.append(
                int(
                    self.con.execute(
                        "SELECT CAST(strftime('%s',w.available_at) AS INTEGER)-"
                        "CAST(strftime('%s',a.attempted_at) AS INTEGER) "
                        "FROM sprint_wake_outbox w JOIN sprint_wake_attempts a "
                        "ON a.wake_id=w.wake_id AND a.attempt_number=? "
                        "WHERE w.wake_id=?",
                        (attempt, sent.wake_id),
                    ).fetchone()[0]
                )
            )
            self.assertEqual(expected, observed_backoffs[-1])

        self.assertEqual(
            ("closed", 0),
            tuple(
                self.con.execute(
                    "SELECT state,(SELECT COUNT(*) FROM active_shell_chats "
                    "WHERE shell_id=1) FROM conversations WHERE conversation_id=?",
                    (chat_id,),
                ).fetchone()
            ),
        )
        closed_event = self.con.execute(
            "SELECT event_type,payload FROM conversation_events "
            "WHERE conversation_id=? ORDER BY sequence DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        self.assertEqual("conversation.closed", closed_event["event_type"])
        self.assertEqual(
            {
                "reason": "wake delivery exhausted",
                "state": "closed",
                "wake_id": sent.wake_id,
            },
            json.loads(closed_event["payload"]),
        )

        monitor = activity_monitor.ActivityMonitor(self.con)
        first = monitor.tick()
        second = monitor.tick()

        self.assertEqual(1, len(first.recovered_wake_ids))
        replacement = first.recovered_wake_ids[0]
        self.assertEqual((), second.recovered_wake_ids)
        self.assertEqual(
            [(sent.wake_id, "failed", 3), (replacement, "pending", 0)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT wake_id,state,attempt_count FROM sprint_wake_outbox "
                    "WHERE wake_id IN (?,?) ORDER BY wake_id",
                    (sent.wake_id, replacement),
                )
            ],
        )
        joined = self.con.execute(
            "SELECT joined.wake_id,message.delivered_at,message.read_at "
            "FROM sprint_wake_messages joined JOIN wake_message message "
            "USING (message_id) WHERE message.message_id=?",
            (sent.message_id,),
        ).fetchone()
        self.assertEqual((replacement, None, None), tuple(joined))
        self.assertEqual(
            180,
            int(
                self.con.execute(
                    "SELECT CAST(strftime('%s',available_at) AS INTEGER)-"
                    "CAST(strftime('%s',created_at) AS INTEGER) "
                    "FROM sprint_wake_outbox WHERE wake_id=?",
                    (replacement,),
                ).fetchone()[0]
            ),
        )

        delivery = sprint_message_delivery.SprintWakeDeliveryService(self.con)
        recovery_outcomes = []
        for attempt in range(1, 4):
            self.con.execute(
                "UPDATE sprint_wake_outbox SET available_at=datetime('now') "
                "WHERE wake_id=?",
                (replacement,),
            )
            self.con.commit()
            recovery_outcomes.append(
                delivery.deliver_once(
                    f"recovery-failure-{attempt}",
                    lambda *_args: (_ for _ in ()).throw(
                        RuntimeError("shell will not boot")
                    ),
                )
            )

        after_recovery_failure = monitor.tick()
        repeated_recovery_scan = monitor.tick()

        self.assertEqual(
            [
                (replacement, "pending", 1),
                (replacement, "pending", 2),
                (replacement, "failed", 3),
            ],
            [
                (outcome.wake_id, outcome.state, outcome.attempt_number)
                for outcome in recovery_outcomes
            ],
        )
        self.assertEqual((), after_recovery_failure.recovered_wake_ids)
        self.assertEqual((), repeated_recovery_scan.recovered_wake_ids)
        self.assertEqual(
            2,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_wake_outbox"
            ).fetchone()[0],
            "a failed recovery wake must not create a recovery-of-recovery",
        )
        self.assertEqual(
            (replacement, None, None),
            tuple(
                self.con.execute(
                    "SELECT joined.wake_id,message.delivered_at,message.read_at "
                    "FROM sprint_wake_messages joined JOIN wake_message message "
                    "USING (message_id) WHERE message.message_id=?",
                    (sent.message_id,),
                ).fetchone()
            ),
        )
        flags = self.con.execute(
            "SELECT display_name,priority,description,shell_id,resolved "
            "FROM flags WHERE display_name LIKE '[Engine] % unbootable%'"
        ).fetchall()
        self.assertEqual(1, len(flags))
        self.assertEqual(
            (
                "[Engine] DEV1 unbootable after wake recovery",
                "High",
                (
                    "Engine wake recovery exhausted its three-attempt budget "
                    f"(wake #{replacement}, key "
                    f"engine-recovery:failed-wake:{sent.wake_id}). Undelivered "
                    "messages remain attached to the terminal wake; manual "
                    "operator recovery is required."
                ),
                1,
                0,
            ),
            tuple(flags[0]),
        )
        recovery_chat_id = self.con.execute(
            "SELECT target_conversation_id FROM sprint_wake_attempts "
            "WHERE wake_id=? AND attempt_number=3",
            (replacement,),
        ).fetchone()[0]
        recovery_event = self.con.execute(
            "SELECT payload FROM conversation_events "
            "WHERE conversation_id=? AND event_type='conversation.closed' "
            "ORDER BY sequence DESC LIMIT 1",
            (recovery_chat_id,),
        ).fetchone()
        self.assertEqual(
            {
                "reason": "engine wake recovery exhausted; shell unbootable",
                "state": "closed",
                "unbootable_shell": True,
                "wake_id": replacement,
            },
            json.loads(recovery_event[0]),
        )

    def test_historical_failed_engine_wake_is_not_recovered(self) -> None:
        sent = sprint_message_delivery.SprintMessageStore(self.con).send_to_shell(
            1,
            message_kind="system",
            body="historical engine wake",
            idempotency_key="historical-engine-wake",
        )
        lifecycle = sprint_domain.SprintLifecycleStore(self.con)
        for attempt in range(1, 4):
            self.assertEqual(
                attempt,
                lifecycle.record_wake_failure(sent.wake_id, f"failure {attempt}"),
            )
        self.con.execute(
            "UPDATE sprint_wake_outbox SET created_at=datetime('now','-2 days') "
            "WHERE wake_id=?",
            (sent.wake_id,),
        )
        self.con.commit()

        outcome = activity_monitor.ActivityMonitor(self.con).tick()

        self.assertEqual((), outcome.recovered_wake_ids)
        self.assertEqual(
            (sent.wake_id, "failed", 3, None, None),
            tuple(
                self.con.execute(
                    "SELECT joined.wake_id,wake.state,wake.attempt_count,"
                    "message.delivered_at,message.read_at "
                    "FROM sprint_wake_messages joined "
                    "JOIN sprint_wake_outbox wake USING (wake_id) "
                    "JOIN wake_message message USING (message_id) "
                    "WHERE message.message_id=?",
                    (sent.message_id,),
                ).fetchone()
            ),
        )

    def test_terminal_failure_does_not_close_a_replacement_active_chat(self) -> None:
        chat_id, _message_id, _run_id = self.add_live_chat(idle_seconds=1)
        self.con.execute(
            "INSERT INTO conversations "
            "(shell_id,owner_user_id,harness,worktree,state,closed_at,"
            "creation_idempotency_key,creation_request_hash) "
            "VALUES (1,1,'codex','/tmp/activity','closed',datetime('now'),"
            "'old-delivery-target','old-delivery-hash')"
        )
        old_target = str(
            self.con.execute(
                "SELECT conversation_id FROM conversations "
                "WHERE creation_idempotency_key='old-delivery-target'"
            ).fetchone()[0]
        )
        self.con.commit()
        sent = sprint_message_delivery.SprintMessageStore(self.con).send_to_shell(
            1,
            message_kind="system",
            body="failed against displaced target",
            idempotency_key="displaced-target-failure",
        )
        lifecycle = sprint_domain.SprintLifecycleStore(self.con)

        for attempt in range(1, 4):
            self.assertEqual(
                attempt,
                lifecycle.record_wake_failure(
                    sent.wake_id,
                    f"failure {attempt}",
                    target_conversation_id=old_target,
                ),
            )

        self.assertEqual(
            (chat_id, "running", 4242, 9001),
            tuple(
                self.con.execute(
                    "SELECT a.chat_id,c.state,a.process_pid,a.process_start_ticks "
                    "FROM active_shell_chats a JOIN conversations c "
                    "ON c.conversation_id=a.chat_id WHERE a.shell_id=1"
                ).fetchone()
            ),
        )


if __name__ == "__main__":
    unittest.main()
