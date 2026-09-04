#!/usr/bin/env python3
"""Feature #31 orphan-process reaper identity and ladder contracts."""

from __future__ import annotations

import signal
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
SCRIPTS = ENGINE / "scripts"
sys.path.insert(0, str(SCRIPTS))

import conversation_reaper
from conversation_broker import BrokerStore
from conversation_reaper import (
    ConversationReaper,
    ProcessSnapshot,
    ReaperConfig,
    ReaperStore,
)


def apply_schema(con: sqlite3.Connection) -> None:
    con.executescript((ENGINE / "schema.sql").read_text())
    for migration in sorted((ENGINE / "migrations").glob("*.sql")):
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class ConversationReaperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "shell.db"
        con = sqlite3.connect(self.db_path)
        apply_schema(con)
        con.execute("INSERT INTO users (user_id,username) VALUES (1,'operator')")
        con.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (1,'Dev','dev1','dev','prompt',1)"
        )
        con.commit()
        con.close()
        self.clock = MutableClock(datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc))
        self.serial = 0

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def add_run(
        self,
        *,
        pid: int = 4242,
        start_ticks: int = 9001,
        process_group_id: int = 4242,
        age_seconds: float = 60.0,
        protected: bool = False,
    ) -> tuple[str, int, int]:
        self.serial += 1
        key = f"reaper-{self.serial}"
        started = self.clock.value - timedelta(seconds=age_seconds)
        con = self.connect()
        con.execute(
            "INSERT INTO conversations "
            "(shell_id,owner_user_id,harness,worktree,state,"
            "creation_idempotency_key,creation_request_hash) "
            "VALUES (1,1,'codex','/tmp/reaper','running',?,?)",
            (key, f"hash-{key}"),
        )
        conversation_id = str(
            con.execute(
                "SELECT conversation_id FROM conversations "
                "WHERE creation_idempotency_key=?",
                (key,),
            ).fetchone()[0]
        )
        message_id = int(
            con.execute(
                "INSERT INTO conversation_messages "
                "(conversation_id,sender_kind,sender_ref,message_kind,body,"
                "idempotency_key,request_hash,state) "
                "VALUES (?,'user','1','prompt','hello',?,?,'running')",
                (conversation_id, f"message-{key}", f"message-hash-{key}"),
            ).lastrowid
        )
        run_id = int(
            con.execute(
                "INSERT INTO conversation_runs "
                "(conversation_id,shell_id,trigger_message_id,state,lease_owner,"
                "lease_expires_at,started_at,heartbeat_at,process_pid,"
                "process_start_ticks,process_group_id) "
                "VALUES (?,1,?,'running','dead-broker','2000-01-01 00:00:00',"
                "?,?,?,?,?)",
                (
                    conversation_id,
                    message_id,
                    started.strftime("%Y-%m-%d %H:%M:%S"),
                    started.strftime("%Y-%m-%d %H:%M:%S"),
                    pid,
                    start_ticks,
                    process_group_id,
                ),
            ).lastrowid
        )
        con.execute(
            "INSERT INTO active_shell_chats "
            "(shell_id,chat_id,process_pid,process_start_ticks) VALUES (1,?,?,?)",
            (
                conversation_id,
                pid if protected else None,
                start_ticks if protected else None,
            ),
        )
        con.commit()
        con.close()
        return conversation_id, message_id, run_id

    def build_reaper(
        self,
        snapshot: ProcessSnapshot | None,
        *,
        native_interrupt=None,
        signal_group=None,
        young_grace_seconds: float = 30.0,
    ) -> ConversationReaper:
        return ConversationReaper(
            self.db_path,
            store=ReaperStore(self.db_path, clock=self.clock),
            config=ReaperConfig(
                heartbeat_seconds=60,
                term_grace_seconds=15,
                kill_grace_seconds=15,
                young_grace_seconds=young_grace_seconds,
            ),
            clock=self.clock,
            process_reader=lambda _pid: snapshot,
            native_interrupt=native_interrupt,
            signal_group=signal_group or (lambda _group, _value: None),
        )

    def finish_unknown(self, run_id: int) -> None:
        self.assertTrue(
            BrokerStore(self.db_path, clock=self.clock).finish_run(
                run_id,
                "unknown",
                event_type="run.unknown",
                payload={"detail": "adopted outcome could not be proven"},
                error_code="HARNESS_OUTCOME_UNKNOWN",
                error_detail="adopted outcome could not be proven",
            )
        )

    def finish_succeeded(self, run_id: int, ended_at: str) -> None:
        """The lingering shape: the broker proved a terminal for the run while
        its process kept working, so the run is finished and the pid identity
        stays on the row."""
        con = self.connect()
        con.execute(
            "UPDATE conversation_runs SET state='succeeded',ended_at=? "
            "WHERE run_id=?",
            (ended_at, run_id),
        )
        con.commit()
        con.close()

    def unlink_registry(self) -> None:
        con = self.connect()
        con.execute(
            "UPDATE active_shell_chats SET process_pid=NULL,process_start_ticks=NULL"
        )
        con.commit()
        con.close()

    def test_lingering_run_is_reapable_only_once_the_chat_is_closed(self) -> None:
        # The lockout: a finished run whose process is still alive holds the
        # shell. While the chat is open the registry link protects it; closing
        # the chat unlinks the row and the survivor becomes reapable.
        _conversation, _message, run_id = self.add_run(protected=True)
        self.finish_succeeded(run_id, "2026-08-02 11:59:30")
        store = ReaperStore(self.db_path, clock=self.clock)

        self.assertEqual([], [c.run_id for c in store.candidates()])

        self.unlink_registry()

        self.assertEqual([run_id], [c.run_id for c in store.candidates()])

    def test_reaping_a_lingering_run_only_records_the_outcome(self) -> None:
        conversation_id, message_id, run_id = self.add_run()
        self.finish_succeeded(run_id, "2026-08-02 11:59:30")
        reaper = self.build_reaper(None)          # the process is gone

        self.assertEqual(reaper.sweep_once(), 1)
        self.assertEqual(reaper.sweep_once(), 0)  # exactly once, never a loop

        con = self.connect()
        run = con.execute(
            "SELECT state,ended_at,error_code FROM conversation_runs "
            "WHERE run_id=?",
            (run_id,),
        ).fetchone()
        message_state = con.execute(
            "SELECT state FROM conversation_messages WHERE message_id=?",
            (message_id,),
        ).fetchone()[0]
        conversation_state = con.execute(
            "SELECT state FROM conversations WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()[0]
        events = con.execute(
            "SELECT event_type,payload FROM conversation_events WHERE run_id=? "
            "ORDER BY sequence",
            (run_id,),
        ).fetchall()
        con.close()
        # The broker's terminal is the run's terminal — the reaper never
        # overwrites a proven outcome with its own guess.
        self.assertEqual(tuple(run), ("succeeded", "2026-08-02 11:59:30", None))
        self.assertEqual(message_state, "running")
        self.assertEqual(conversation_state, "running")
        self.assertEqual([row["event_type"] for row in events], ["run.reaped"])
        self.assertIn('"run_state":"succeeded"', events[0]["payload"])
        self.assertIn(
            '"reason":"recorded process exited before reaper signal"',
            events[0]["payload"],
        )

    def test_lingering_candidate_climbs_the_same_ladder(self) -> None:
        _conversation, _message, run_id = self.add_run()
        self.finish_succeeded(run_id, "2026-08-02 11:59:30")
        snapshot = ProcessSnapshot(4242, 9001, 4242)
        interrupted: list[int] = []
        signals: list[tuple[int, signal.Signals]] = []

        def reaper() -> ConversationReaper:
            return self.build_reaper(
                snapshot,
                native_interrupt=interrupted.append,
                signal_group=lambda group, value: signals.append((group, value)),
            )

        self.assertEqual(reaper().sweep_once(), 1)
        self.clock.advance(15)
        self.assertEqual(reaper().sweep_once(), 1)
        self.clock.advance(15)
        self.assertEqual(reaper().sweep_once(), 1)
        self.assertEqual(interrupted, [run_id])
        self.assertEqual(signals, [(4242, signal.SIGTERM), (4242, signal.SIGKILL)])

        con = self.connect()
        run = con.execute(
            "SELECT state,reaper_last_signal FROM conversation_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        events = con.execute(
            "SELECT event_type FROM conversation_events WHERE run_id=?",
            (run_id,),
        ).fetchall()
        con.close()
        self.assertEqual(tuple(run), ("succeeded", "SIGKILL"))
        self.assertEqual([row[0] for row in events], ["run.reaped"])

    def test_exact_registry_identity_is_the_only_protection(self) -> None:
        _conversation, _message, protected_run = self.add_run(protected=True)
        snapshot = ProcessSnapshot(4242, 9001, 4242)
        reaper = self.build_reaper(snapshot)

        self.assertEqual(reaper.sweep_once(), 0)

        con = self.connect()
        con.execute(
            "UPDATE active_shell_chats SET process_pid=NULL,process_start_ticks=NULL"
        )
        con.commit()
        con.close()
        interrupted: list[int] = []
        reaper = self.build_reaper(snapshot, native_interrupt=interrupted.append)

        self.assertEqual(reaper.sweep_once(), 1)
        self.assertEqual(interrupted, [protected_run])
        con = self.connect()
        row = con.execute(
            "SELECT state,reaper_last_signal FROM conversation_runs WHERE run_id=?",
            (protected_run,),
        ).fetchone()
        con.close()
        self.assertEqual(tuple(row), ("running", "interrupt"))

    def test_young_process_grace_has_no_side_effects(self) -> None:
        _conversation, _message, run_id = self.add_run(age_seconds=29)
        interrupted: list[int] = []
        signals: list[tuple[int, signal.Signals]] = []
        reaper = self.build_reaper(
            ProcessSnapshot(4242, 9001, 4242),
            native_interrupt=interrupted.append,
            signal_group=lambda group, value: signals.append((group, value)),
        )

        self.assertEqual(reaper.sweep_once(), 0)
        self.assertEqual(interrupted, [])
        self.assertEqual(signals, [])
        con = self.connect()
        row = con.execute(
            "SELECT state,reaper_last_signal FROM conversation_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        con.close()
        self.assertEqual(tuple(row), ("running", None))

    def test_recycled_pid_is_never_signalled_and_finishes_interrupted(self) -> None:
        conversation_id, message_id, run_id = self.add_run()
        signals: list[tuple[int, signal.Signals]] = []
        reaper = self.build_reaper(
            ProcessSnapshot(4242, 9999, 4242),
            signal_group=lambda group, value: signals.append((group, value)),
        )

        self.assertEqual(reaper.sweep_once(), 1)
        self.assertEqual(signals, [])
        con = self.connect()
        run = con.execute(
            "SELECT state,error_code FROM conversation_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        message = con.execute(
            "SELECT state FROM conversation_messages WHERE message_id=?",
            (message_id,),
        ).fetchone()[0]
        event = con.execute(
            "SELECT event_type,payload FROM conversation_events "
            "WHERE conversation_id=? AND run_id=?",
            (conversation_id, run_id),
        ).fetchone()
        con.close()
        self.assertEqual(tuple(run), ("cancelled", "CONVERSATION_RUN_REAPED"))
        self.assertEqual(message, "cancelled")
        self.assertEqual(event["event_type"], "run.interrupted")
        self.assertIn(
            '"reason":"recorded process identity exited or was recycled"',
            event["payload"],
        )

    def test_adopted_unknown_live_process_is_swept_once(self) -> None:
        conversation_id, message_id, run_id = self.add_run()
        self.finish_unknown(run_id)
        interrupted: list[int] = []
        signals: list[tuple[int, signal.Signals]] = []
        reaper = self.build_reaper(
            ProcessSnapshot(4242, 9001, 4242),
            native_interrupt=interrupted.append,
            signal_group=lambda group, value: signals.append((group, value)),
        )

        self.assertEqual(reaper.sweep_once(), 1)
        self.clock.advance(15)
        self.assertEqual(reaper.sweep_once(), 1)
        self.clock.advance(15)
        self.assertEqual(reaper.sweep_once(), 1)
        self.assertEqual(reaper.sweep_once(), 0)
        self.assertEqual(interrupted, [run_id])
        self.assertEqual(
            signals,
            [(4242, signal.SIGTERM), (4242, signal.SIGKILL)],
        )

        con = self.connect()
        run = con.execute(
            "SELECT state,error_code,process_pid,process_start_ticks,"
            "process_group_id,reaper_last_signal FROM conversation_runs "
            "WHERE run_id=?",
            (run_id,),
        ).fetchone()
        message_state = con.execute(
            "SELECT state FROM conversation_messages WHERE message_id=?",
            (message_id,),
        ).fetchone()[0]
        conversation_state = con.execute(
            "SELECT state FROM conversations WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()[0]
        events = con.execute(
            "SELECT event_type FROM conversation_events WHERE run_id=? "
            "ORDER BY sequence",
            (run_id,),
        ).fetchall()
        con.close()
        self.assertEqual(
            tuple(run),
            ("unknown", "HARNESS_OUTCOME_UNKNOWN", 4242, 9001, 4242, "SIGKILL"),
        )
        self.assertEqual(message_state, "failed")
        self.assertEqual(conversation_state, "error")
        self.assertEqual(
            [row["event_type"] for row in events],
            ["run.unknown", "run.reaped"],
        )

    def test_unknown_run_without_process_identity_is_untouched(self) -> None:
        _conversation_id, _message_id, run_id = self.add_run()
        self.finish_unknown(run_id)
        con = self.connect()
        con.execute(
            "UPDATE conversation_runs SET process_pid=NULL,"
            "process_start_ticks=NULL,process_group_id=NULL WHERE run_id=?",
            (run_id,),
        )
        con.commit()
        con.close()
        interrupted: list[int] = []
        signals: list[tuple[int, signal.Signals]] = []
        reaper = self.build_reaper(
            None,
            native_interrupt=interrupted.append,
            signal_group=lambda group, value: signals.append((group, value)),
        )

        self.assertEqual(reaper.sweep_once(), 0)
        self.assertEqual(interrupted, [])
        self.assertEqual(signals, [])
        con = self.connect()
        run = con.execute(
            "SELECT state,process_pid,process_start_ticks,process_group_id,"
            "reaper_last_signal FROM conversation_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        events = con.execute(
            "SELECT event_type FROM conversation_events WHERE run_id=? "
            "ORDER BY sequence",
            (run_id,),
        ).fetchall()
        con.close()
        self.assertEqual(tuple(run), ("unknown", None, None, None, None))
        self.assertEqual([row["event_type"] for row in events], ["run.unknown"])

    def test_ladder_persists_across_heartbeats_and_kills_process_group(self) -> None:
        conversation_id, message_id, run_id = self.add_run()
        snapshot = ProcessSnapshot(4242, 9001, 4242)
        interrupted: list[int] = []
        signals: list[tuple[int, signal.Signals]] = []

        def reaper() -> ConversationReaper:
            return self.build_reaper(
                snapshot,
                native_interrupt=interrupted.append,
                signal_group=lambda group, value: signals.append((group, value)),
            )

        self.assertEqual(reaper().sweep_once(), 1)
        self.assertEqual(interrupted, [run_id])
        self.assertEqual(signals, [])

        self.clock.advance(14)
        self.assertEqual(reaper().sweep_once(), 0)
        self.clock.advance(1)
        self.assertEqual(reaper().sweep_once(), 1)
        self.assertEqual(signals, [(4242, signal.SIGTERM)])

        self.clock.advance(14)
        self.assertEqual(reaper().sweep_once(), 0)
        self.clock.advance(1)
        self.assertEqual(reaper().sweep_once(), 1)
        self.assertEqual(
            signals,
            [(4242, signal.SIGTERM), (4242, signal.SIGKILL)],
        )

        con = self.connect()
        run = con.execute(
            "SELECT state,reaper_last_signal,reaper_signaled_at "
            "FROM conversation_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        message = con.execute(
            "SELECT state FROM conversation_messages WHERE message_id=?",
            (message_id,),
        ).fetchone()[0]
        events = con.execute(
            "SELECT event_type FROM conversation_events "
            "WHERE conversation_id=? AND run_id=? ORDER BY sequence",
            (conversation_id, run_id),
        ).fetchall()
        con.close()
        self.assertEqual(run["state"], "cancelled")
        self.assertEqual(run["reaper_last_signal"], "SIGKILL")
        self.assertEqual(run["reaper_signaled_at"], "2026-08-02 12:00:30")
        self.assertEqual(message, "cancelled")
        self.assertEqual([row[0] for row in events], ["run.interrupted"])

    def test_tunables_use_spec_defaults_and_accept_overrides(self) -> None:
        self.assertEqual(
            ReaperConfig.from_env({}),
            ReaperConfig(60.0, 15.0, 15.0, 30.0),
        )
        self.assertEqual(
            ReaperConfig.from_env(
                {
                    "SC_REAPER_HEARTBEAT_SECONDS": "5",
                    "SC_REAPER_TERM_GRACE_SECONDS": "6",
                    "SC_REAPER_KILL_GRACE_SECONDS": "7",
                    "SC_REAPER_YOUNG_GRACE_SECONDS": "8",
                }
            ),
            ReaperConfig(5.0, 6.0, 7.0, 8.0),
        )

    def test_service_heartbeat_is_durable(self) -> None:
        store = ReaperStore(self.db_path, clock=self.clock)
        store.heartbeat(60)
        con = self.connect()
        heartbeat = con.execute(
            "SELECT name,interval_s FROM daemon_heartbeats "
            "WHERE name='conversation-reaper'"
        ).fetchone()
        con.close()
        self.assertEqual(tuple(heartbeat), ("conversation-reaper", 60))

    def test_stop_service_stops_joins_and_clears_the_worker(self) -> None:
        worker = mock.Mock()
        with mock.patch.object(conversation_reaper, "_SERVICE", worker):
            conversation_reaper.stop_service()
            self.assertIsNone(conversation_reaper.service())

        self.assertEqual(
            worker.mock_calls,
            [mock.call.stop(), mock.call.join()],
        )

    def test_schema_rejects_partial_identity_and_ladder_state(self) -> None:
        _conversation, _message, run_id = self.add_run()
        con = self.connect()
        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "conversation run process identity must be complete",
        ):
            con.execute(
                "UPDATE conversation_runs SET process_group_id=NULL WHERE run_id=?",
                (run_id,),
            )
        con.rollback()
        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "conversation run reaper signal must have a timestamp",
        ):
            con.execute(
                "UPDATE conversation_runs SET reaper_last_signal='interrupt' "
                "WHERE run_id=?",
                (run_id,),
            )
        con.rollback()
        row = con.execute(
            "SELECT process_pid,process_start_ticks,process_group_id,"
            "reaper_last_signal,reaper_signaled_at FROM conversation_runs "
            "WHERE run_id=?",
            (run_id,),
        ).fetchone()
        con.close()
        self.assertEqual(tuple(row), (4242, 9001, 4242, None, None))


if __name__ == "__main__":
    unittest.main()
