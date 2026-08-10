"""Total, side-effect-free Sprint progress-carrier projection."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path[:0] = [str(ENGINE / "scripts")]

import sprint_domain
import sprint_health
import sprint_message_delivery
import sprint_runtime
from conversation_adapters import NativeTurn
from conversation_broker import BrokerStore

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


def apply_schema(con: sqlite3.Connection) -> None:
    con.executescript((ENGINE / "schema.sql").read_text())
    for migration in sorted((ENGINE / "migrations").glob("*.sql")):
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")


class SprintHealthCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "health.db"
        self.con = sqlite3.connect(self.db)
        self.addCleanup(self.con.close)
        self.con.row_factory = sqlite3.Row
        apply_schema(self.con)
        self._seed_identity()
        self.sprint_id = self._new_sprint()

    def _seed_identity(self) -> None:
        self.con.execute(
            "INSERT INTO users (user_id,username,is_active) VALUES (1,'operator',1)"
        )
        rows = [
            (1, "Planner", "PLN1", "planner"),
            (2, "Reviewer", "REV1", "reviewer"),
        ] + [
            (shell_id, f"Developer {shell_id}", f"DEV{shell_id}", "dev")
            for shell_id in range(3, 16)
        ]
        self.con.executemany(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id,api_key) "
            "VALUES (?,?,?,?,?,1,?)",
            [(*row, "prompt", f"token-{row[0]}") for row in rows],
        )
        self.feature_id = int(
            self.con.execute(
                "INSERT INTO roadmap (title,roadmap_status) "
                "VALUES ('Health feature','in_progress')"
            ).lastrowid
        )

    def _new_sprint(
        self,
        *,
        lifecycle: str = "armed",
        armed_at: str = "2026-08-10 11:45:00",
    ) -> int:
        sprint_id = int(
            self.con.execute(
                "INSERT INTO sprints "
                "(feature_id,originating_planner_shell_id,merge_grant_enabled,created_at) "
                "VALUES (?,1,1,'2026-08-10 09:00:00')",
                (self.feature_id,),
            ).lastrowid
        )
        for shell_id, role in [(1, "planner"), (2, "reviewer")] + [
            (shell_id, "developer") for shell_id in range(3, 16)
        ]:
            self.con.execute(
                "INSERT INTO sprint_participants "
                "(sprint_id,shell_id,role,harness,disposition) "
                "VALUES (?,?,?,'codex','idle')",
                (sprint_id, shell_id, role),
            )
        if lifecycle != "prepared":
            self.con.execute(
                "UPDATE sprints SET lifecycle='armed',armed_at=? WHERE sprint_id=?",
                (armed_at, sprint_id),
            )
        if lifecycle == "paused":
            self.con.execute(
                "UPDATE sprints SET lifecycle='paused',paused_at=? WHERE sprint_id=?",
                (armed_at, sprint_id),
            )
        elif lifecycle == "completed":
            self.con.execute(
                "UPDATE sprints SET lifecycle='completed',terminal_outcome='success',"
                "completed_at=? WHERE sprint_id=?",
                (armed_at, sprint_id),
            )
        return sprint_id

    def add_unit(
        self,
        disposition: str,
        *,
        developer: int,
        updated_at: str = "2026-08-10 10:00:00",
    ) -> int:
        return int(
            self.con.execute(
                "INSERT INTO sprint_work_units "
                "(sprint_id,assigned_shell_id,reviewer_shell_id,title,expected_output,"
                "planned_wave,disposition,updated_at,completed_at) "
                "VALUES (?,?,2,?,?,0,?,?,?)",
                (
                    self.sprint_id,
                    developer,
                    f"Unit {developer}",
                    "output",
                    disposition,
                    updated_at,
                    updated_at if disposition in {"completed", "cancelled"} else None,
                ),
            ).lastrowid
        )

    def add_event(
        self,
        event_type: str,
        *,
        at: str,
        work_unit_id: int | None = None,
        payload: dict | None = None,
    ) -> int:
        body = dict(payload or {})
        if work_unit_id is not None:
            body["work_unit_id"] = work_unit_id
        return int(
            self.con.execute(
                "INSERT INTO sprint_events "
                "(sprint_id,event_type,actor_kind,payload,created_at) "
                "VALUES (?,?,'system',?,?)",
                (self.sprint_id, event_type, json.dumps(body), at),
            ).lastrowid
        )

    def heartbeat(self, name: str, at: str, interval: int = 5) -> None:
        self.con.execute(
            "INSERT INTO daemon_heartbeats (name,beat_at,interval_s) VALUES (?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET beat_at=excluded.beat_at,"
            "interval_s=excluded.interval_s",
            (name, at, interval),
        )

    def add_pr(
        self,
        unit_id: int,
        state: str,
        *,
        at: str,
        number: int,
    ) -> int:
        owner = self.con.execute(
            "SELECT participant_id FROM sprint_participants p "
            "JOIN sprint_work_units u ON u.sprint_id=p.sprint_id "
            "AND u.assigned_shell_id=p.shell_id WHERE u.work_unit_id=?",
            (unit_id,),
        ).fetchone()[0]
        pr_id = int(
            self.con.execute(
                "INSERT INTO sprint_registered_prs "
                "(sprint_id,owner_participant_id,repository,pr_number,registered_at) "
                "VALUES (?,?,'acme/repo',?,?)",
                (self.sprint_id, owner, number, at),
            ).lastrowid
        )
        self.con.execute(
            "INSERT INTO sprint_pr_work_units "
            "(sprint_id,registered_pr_id,work_unit_id) VALUES (?,?,?)",
            (self.sprint_id, pr_id, unit_id),
        )
        self.con.execute(
            "INSERT INTO sprint_pr_transitions "
            "(registered_pr_id,normalized_state,transition_key,observed_at) "
            "VALUES (?,?,?,?)",
            (pr_id, state, f"pr-{number}-{state}", at),
        )
        return pr_id

    def add_message(
        self,
        *,
        unit_id: int | None,
        receiver: int,
        kind: str = "notification",
        disposition: str | None = None,
        read_at: str | None = None,
        delivered_at: str | None = None,
        intent: str = "information",
        requires_reply: bool = False,
        reply_to: int | None = None,
        created_at: str = "2026-08-10 11:50:00",
    ) -> int:
        recipient = self.con.execute(
            "SELECT participant_id FROM sprint_participants "
            "WHERE sprint_id=? AND shell_id=?",
            (self.sprint_id, receiver),
        ).fetchone()[0]
        sender_shell = 1 if receiver != 1 else 3
        sender = self.con.execute(
            "SELECT participant_id FROM sprint_participants "
            "WHERE sprint_id=? AND shell_id=?",
            (self.sprint_id, sender_shell),
        ).fetchone()[0]
        actionable = disposition is not None
        return int(
            self.con.execute(
                "INSERT INTO wake_message "
                "(sprint_id,sender_shell_id,receiver_shell_id,from_participant_id,"
                "to_participant_id,work_unit_id,message_kind,body,declared_type,"
                "actionable,disposition,read_at,delivered_at,idempotency_key,created_at,"
                "intent,requires_reply,reply_to_message_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.sprint_id,
                    sender_shell,
                    receiver,
                    sender,
                    recipient,
                    unit_id,
                    kind,
                    "bounded test message",
                    "force-new" if kind in {"work_assignment", "review_request"} else "re-enter",
                    int(actionable),
                    disposition,
                    read_at,
                    delivered_at,
                    f"message-{self.sprint_id}-{receiver}-{created_at}-{unit_id}-{kind}-{reply_to}",
                    created_at,
                    intent,
                    int(requires_reply),
                    reply_to,
                ),
            ).lastrowid
        )

    def add_wake(
        self,
        message_id: int,
        *,
        receiver: int,
        state: str,
        created_at: str = "2026-08-10 11:50:00",
    ) -> int:
        participant = self.con.execute(
            "SELECT participant_id FROM sprint_participants "
            "WHERE sprint_id=? AND shell_id=?",
            (self.sprint_id, receiver),
        ).fetchone()[0]
        wake_id = int(
            self.con.execute(
                "INSERT INTO sprint_wake_outbox "
                "(sprint_id,participant_id,receiver_shell_id,state,attempt_count,"
                "idempotency_key,created_at,available_at,delivered_at,failed_at) "
                "VALUES (?,?,?,?,0,?,?,?,?,?)",
                (
                    self.sprint_id,
                    participant,
                    receiver,
                    state,
                    f"wake-{message_id}",
                    created_at,
                    created_at,
                    created_at if state == "delivered" else None,
                    created_at if state == "failed" else None,
                ),
            ).lastrowid
        )
        self.con.execute(
            "INSERT INTO sprint_wake_messages (sprint_id,wake_id,message_id) "
            "VALUES (?,?,?)",
            (self.sprint_id, wake_id, message_id),
        )
        return wake_id

    def add_live_run(
        self,
        message_id: int,
        wake_id: int,
        *,
        shell_id: int,
        suffix: str,
        active: bool = True,
        creation_wake_id: int | None = None,
        creation_generation: int | None = None,
        provider: str | None = None,
    ) -> int:
        generation = self.con.execute(
            "SELECT conversation_generation FROM sprints WHERE sprint_id=?",
            (self.sprint_id,),
        ).fetchone()[0]
        conversation = f"cv_{suffix}"
        conversation_generation = creation_generation or generation
        conversation_wake = creation_wake_id or wake_id
        creation_key = (
            f"generation:{conversation_generation}:wake:{conversation_wake}"
        )
        wake_key = self.con.execute(
            "SELECT idempotency_key FROM sprint_wake_outbox WHERE wake_id=?",
            (wake_id,),
        ).fetchone()[0]
        self.con.execute(
            "INSERT INTO conversations "
            "(conversation_id,shell_id,owner_user_id,harness,provider,worktree,state,title,"
            "creation_idempotency_key,creation_request_hash,conversation_scope) "
            "VALUES (?,?,1,'codex',?,'/work','running','test',?,?,'sprint')",
            (
                conversation,
                shell_id,
                provider,
                creation_key,
                suffix,
            ),
        )
        participant_id = self.con.execute(
            "SELECT participant_id FROM sprint_participants "
            "WHERE sprint_id=? AND shell_id=?",
            (self.sprint_id, shell_id),
        ).fetchone()[0]
        self.con.execute(
            "INSERT INTO sprint_participant_conversations "
            "(sprint_participant_id,conversation_id) VALUES (?,?)",
            (participant_id, conversation),
        )
        prompt_id = int(
            self.con.execute(
                "INSERT INTO conversation_messages "
                "(conversation_id,sender_kind,sender_ref,message_kind,body,"
                "idempotency_key,request_hash,state) "
                "VALUES (?,'engine','wake','prompt','prompt',?,?, 'running')",
                (conversation, wake_key, suffix),
            ).lastrowid
        )
        run_id = int(
            self.con.execute(
                "INSERT INTO conversation_runs "
                "(conversation_id,shell_id,trigger_message_id,state,lease_owner,"
                "lease_expires_at,started_at,heartbeat_at) "
                "VALUES (?,?,?,'running','test','2026-08-10 12:10:00',"
                "'2026-08-10 11:55:00','2026-08-10 11:59:59')",
                (conversation, shell_id, prompt_id),
            ).lastrowid
        )
        self.con.execute(
            "INSERT INTO sprint_wake_attempts "
            "(wake_id,attempt_number,target_conversation_id,native_run_ref,outcome,attempted_at) "
            "VALUES (?,1,?,?,'delivered','2026-08-10 11:55:00')",
            (wake_id, conversation, f"conversation-run:{run_id}"),
        )
        if active:
            self.con.execute(
                "INSERT INTO active_shell_chats "
                "(shell_id,chat_id,process_pid,process_start_ticks,updated_at) "
                "VALUES (?,?,123,456,'2026-08-10 11:59:59')",
                (shell_id, conversation),
            )
        return run_id

    def recover_terminal_wake(
        self,
        message_id: int,
        wake_id: int,
        *,
        shell_id: int,
    ) -> int:
        run_id = self.add_live_run(
            message_id,
            wake_id,
            shell_id=shell_id,
            suffix=f"recovery-{message_id}",
        )
        turn = self.con.execute(
            "SELECT r.conversation_id,r.trigger_message_id "
            "FROM conversation_runs r WHERE r.run_id=?",
            (run_id,),
        ).fetchone()
        self.con.execute(
            "UPDATE sprint_wake_outbox SET state='delivered',"
            "attempt_count=1,delivered_at='2026-08-10 11:54:00' WHERE wake_id=?",
            (wake_id,),
        )
        self.con.execute(
            "UPDATE wake_message SET delivered_at='2026-08-10 11:54:00' "
            "WHERE message_id=?",
            (message_id,),
        )
        self.con.execute(
            "UPDATE conversation_runs SET state='succeeded',"
            "ended_at='2026-08-10 11:54:30' WHERE run_id=?",
            (run_id,),
        )
        self.con.execute(
            "UPDATE conversation_messages SET state='completed',"
            "completed_at='2026-08-10 11:54:30' WHERE message_id=?",
            (int(turn["trigger_message_id"]),),
        )
        self.con.execute(
            "UPDATE conversations SET state='idle' WHERE conversation_id=?",
            (str(turn["conversation_id"]),),
        )
        self.con.execute(
            "DELETE FROM active_shell_chats WHERE shell_id=?",
            (shell_id,),
        )
        self.con.commit()
        recovered = sprint_domain.SprintLifecycleStore(
            self.con
        ).reconcile_unread_pickup(self.sprint_id, trigger="health-test")
        self.assertEqual(1, len(recovered))
        return recovered[0]

    def project(self, *, now: datetime = NOW) -> dict:
        self.con.commit()
        return sprint_health.SprintHealthProjection(self.con, now=now).project(
            self.sprint_id
        )

    def test_lifecycle_gate_and_every_armed_entry_floor(self) -> None:
        unit = self.add_unit("active", developer=3)
        projected = self.project()
        self.assertEqual("waiting_external", projected["health"]["condition"])
        self.assertEqual(
            "2026-08-10T11:45:00Z",
            projected["work_units"][unit]["since"],
        )
        self.assertEqual("no_progress_grace", projected["work_units"][unit]["cause"])

        self.con.execute(
            "UPDATE sprints SET lifecycle='paused',paused_at='2026-08-10 11:59:00' "
            "WHERE sprint_id=?",
            (self.sprint_id,),
        )
        paused = self.project()
        self.assertEqual("paused", paused["health"]["condition"])
        self.assertIsNone(paused["health"]["since"])
        self.assertEqual("paused", paused["work_units"][unit]["condition"])

        self.con.execute(
            "UPDATE sprints SET lifecycle='armed',armed_at='2026-08-10 11:59:30',"
            "paused_at=NULL WHERE sprint_id=?",
            (self.sprint_id,),
        )
        resumed = self.project()
        self.assertEqual("2026-08-10T11:59:30Z", resumed["work_units"][unit]["since"])

    def test_every_legal_disposition_has_a_total_stage_projection(self) -> None:
        dispositions = [
            "planned", "ready", "active", "blocked", "in_review", "fixing",
            "merge_ready", "completed", "cancelled",
        ]
        units = {
            disposition: self.add_unit(disposition, developer=index + 3)
            for index, disposition in enumerate(dispositions)
        }
        self.heartbeat("sprint-runtime", "2026-08-10 12:00:00")
        for disposition in ("ready",):
            message = self.add_message(
                unit_id=units[disposition], receiver=3 + dispositions.index(disposition),
                kind="work_assignment", disposition="pending",
            )
            self.add_wake(message, receiver=3 + dispositions.index(disposition), state="pending")
        projected = self.project()["work_units"]
        self.assertEqual(
            "work_unit_ready",
            projected[units["planned"]]["next_expected_event"]["code"],
        )
        self.assertEqual("wake_pending", projected[units["ready"]]["cause"])
        self.assertEqual(
            "developer_evidence",
            projected[units["active"]]["next_expected_event"]["code"],
        )
        self.assertEqual("blocked_grace", projected[units["blocked"]]["cause"])
        self.assertEqual(
            "review_verdict",
            projected[units["in_review"]]["next_expected_event"]["code"],
        )
        self.assertEqual(
            "replacement_pr_transition",
            projected[units["fixing"]]["next_expected_event"]["code"],
        )
        self.assertEqual(
            "merge_observed",
            projected[units["merge_ready"]]["next_expected_event"]["code"],
        )
        self.assertEqual("terminal", projected[units["completed"]]["condition"])
        self.assertEqual("terminal", projected[units["cancelled"]]["condition"])

    def test_runtime_and_watcher_fail_only_when_applicable(self) -> None:
        planned = self.add_unit("planned", developer=3)
        active_without_pr = self.add_unit("active", developer=4)
        active_with_pr = self.add_unit("active", developer=5)
        self.add_pr(active_with_pr, "pending", at="2026-08-10 11:50:00", number=10)
        projected = self.project()["work_units"]
        self.assertEqual("runtime_missing", projected[planned]["cause"])
        self.assertEqual("no_progress_grace", projected[active_without_pr]["cause"])
        self.assertEqual("watcher_missing", projected[active_with_pr]["cause"])

    def test_exact_request_wake_generation_attributes_only_one_review_lane(self) -> None:
        first = self.add_unit("in_review", developer=3)
        second = self.add_unit("in_review", developer=4)
        for index, unit in enumerate((first, second), start=1):
            message = self.add_message(
                unit_id=unit,
                receiver=2,
                kind="review_request",
                disposition="accepted",
                read_at="2026-08-10 11:54:00",
                delivered_at="2026-08-10 11:53:00",
            )
            wake = self.add_wake(message, receiver=2, state="delivered")
            self.add_event(
                "review.requested",
                at="2026-08-10 11:54:00",
                work_unit_id=unit,
                payload={"message_id": message},
            )
            if index == 1:
                self.add_live_run(message, wake, shell_id=2, suffix="review_one")
        projected = self.project()["work_units"]
        self.assertEqual("run_active", projected[first]["cause"])
        self.assertEqual("progressing", projected[first]["condition"])
        self.assertEqual("no_progress_grace", projected[second]["cause"])
        self.assertEqual("waiting_external", projected[second]["condition"])

    def test_default_runtime_message_reference_resolves_exact_broker_run(self) -> None:
        unit = self.add_unit("active", developer=3)
        assignment = self.add_message(
            unit_id=unit,
            receiver=3,
            kind="work_assignment",
            disposition="accepted",
            read_at="2026-08-10 10:05:00",
            created_at="2026-08-10 10:00:00",
        )
        wake = self.add_wake(
            assignment,
            receiver=3,
            state="pending",
            created_at="2026-08-10 10:00:00",
        )
        self.add_event(
            "work_unit.accepted",
            at="2026-08-10 10:05:00",
            work_unit_id=unit,
            payload={"message_id": assignment},
        )
        self.con.commit()

        delivered = sprint_message_delivery.SprintWakeDeliveryService(
            self.con,
            force_new_quiet_seconds=0,
        ).deliver_once(
            "health-runtime",
            lambda conversation_id, prompt, key: sprint_runtime.enqueue_conversation_turn(
                self.db,
                conversation_id,
                prompt,
                key,
            ),
        )
        self.assertEqual((wake, "delivered", 1), (
            delivered.wake_id,
            delivered.state,
            delivered.attempt_number,
        ))
        attempt = self.con.execute(
            "SELECT attempt_id,target_conversation_id,native_run_ref "
            "FROM sprint_wake_attempts WHERE wake_id=?",
            (wake,),
        ).fetchone()
        native_message = self.con.execute(
            "SELECT message_id,conversation_id FROM conversation_messages "
            "WHERE conversation_id=? AND idempotency_key=("
            "SELECT idempotency_key FROM sprint_wake_outbox WHERE wake_id=?)",
            (attempt["target_conversation_id"], wake),
        ).fetchone()
        self.assertEqual(
            f"conversation-message:{int(native_message['message_id'])}",
            attempt["native_run_ref"],
        )
        queued = self.project(
            now=datetime.now(timezone.utc) + timedelta(minutes=1)
        )
        self.assertEqual(
            ("waiting_external", "pickup_active", []),
            (
                queued["work_units"][unit]["condition"],
                queued["work_units"][unit]["cause"],
                queued["work_units"][unit]["unreadable_signals"],
            ),
        )

        broker = BrokerStore(self.db)
        run = broker.claim_next("health-broker")
        self.assertEqual(
            (native_message["conversation_id"], int(native_message["message_id"])),
            (run.conversation_id, run.message_id),
        )
        broker.mark_starting(run.run_id, "health-broker")
        worktree = Path(
            self.con.execute(
                "SELECT worktree FROM conversations WHERE conversation_id=?",
                (run.conversation_id,),
            ).fetchone()[0]
        )
        broker.mark_native_started(
            run.run_id,
            "health-broker",
            NativeTurn(
                "codex",
                "health-session",
                "health-native-run",
                worktree,
                process_ref=str(os.getpid()),
            ),
        )
        unrelated_message_id = int(
            self.con.execute(
                "INSERT INTO conversation_messages "
                "(conversation_id,sender_kind,sender_ref,message_kind,body,"
                "idempotency_key,request_hash,state,completed_at) "
                "VALUES (?,'engine','test','prompt','unrelated',"
                "'unrelated-run','unrelated-hash','completed',datetime('now'))",
                (run.conversation_id,),
            ).lastrowid
        )
        unrelated_run_id = int(
            self.con.execute(
                "INSERT INTO conversation_runs "
                "(conversation_id,shell_id,trigger_message_id,state,lease_owner,"
                "lease_expires_at,started_at,heartbeat_at,ended_at) "
                "VALUES (?,3,?,'succeeded','other','2026-08-10 12:00:00',"
                "'2026-08-10 11:00:00','2026-08-10 11:01:00',"
                "'2026-08-10 11:02:00')",
                (run.conversation_id, unrelated_message_id),
            ).lastrowid
        )
        self.assertGreater(unrelated_run_id, run.run_id)

        projected = self.project(
            now=datetime.now(timezone.utc) + timedelta(minutes=1)
        )

        self.assertEqual(
            ("progressing", "run_active", "live"),
            (
                projected["work_units"][unit]["condition"],
                projected["work_units"][unit]["cause"],
                projected["work_units"][unit]["activity"],
            ),
        )
        self.assertEqual(
            {"kind": "run", "id": run.run_id},
            {
                "kind": projected["work_units"][unit]["last_evidence"]["kind"],
                "id": projected["work_units"][unit]["last_evidence"]["id"],
            },
        )
        self.assertEqual([], projected["work_units"][unit]["unreadable_signals"])
        self.assertEqual([], projected["health"]["unreadable_signals"])

    def test_reenter_run_uses_participant_link_not_creation_wake(self) -> None:
        unit = self.add_unit("fixing", developer=3, updated_at="2026-08-10 11:54:00")
        self.add_pr(unit, "red", at="2026-08-10 11:54:00", number=20)
        self.heartbeat("sprint-pr-watcher", "2026-08-10 12:00:00", 30)
        earlier = self.add_message(
            unit_id=None,
            receiver=3,
            delivered_at="2026-08-10 11:45:00",
            created_at="2026-08-10 11:44:00",
        )
        earlier_wake = self.add_wake(
            earlier,
            receiver=3,
            state="delivered",
            created_at="2026-08-10 11:44:00",
        )
        verdict = self.add_message(
            unit_id=unit,
            receiver=3,
            disposition="accepted",
            read_at="2026-08-10 11:55:00",
            delivered_at="2026-08-10 11:54:30",
            created_at="2026-08-10 11:54:00",
        )
        verdict_wake = self.add_wake(
            verdict,
            receiver=3,
            state="delivered",
            created_at="2026-08-10 11:54:00",
        )
        self.add_event(
            "review.changes_requested",
            at="2026-08-10 11:54:00",
            work_unit_id=unit,
            payload={"message_id": verdict},
        )
        run_id = self.add_live_run(
            verdict,
            verdict_wake,
            shell_id=3,
            suffix="reenter_fix",
            creation_wake_id=earlier_wake,
        )

        projected = self.project()["work_units"][unit]

        self.assertEqual(("progressing", "run_active", "live"), (
            projected["condition"], projected["cause"], projected["activity"]
        ))
        self.assertEqual(
            {"kind": "run", "id": run_id, "at": "2026-08-10T11:59:59Z"},
            projected["last_evidence"],
        )

    def test_live_run_rejects_participant_chat_from_stale_generation(self) -> None:
        unit = self.add_unit("active", developer=3, updated_at="2026-08-10 11:50:00")
        assignment = self.add_message(
            unit_id=unit,
            receiver=3,
            kind="work_assignment",
            disposition="accepted",
            read_at="2026-08-10 11:50:00",
            delivered_at="2026-08-10 11:49:00",
            created_at="2026-08-10 11:48:00",
        )
        wake = self.add_wake(
            assignment,
            receiver=3,
            state="delivered",
            created_at="2026-08-10 11:48:00",
        )
        self.add_event(
            "work_unit.accepted",
            at="2026-08-10 11:50:00",
            work_unit_id=unit,
            payload={"message_id": assignment},
        )
        self.add_live_run(
            assignment,
            wake,
            shell_id=3,
            suffix="stale_generation",
            creation_generation=999,
        )

        projected = self.project()["work_units"][unit]

        self.assertEqual(
            ("waiting_external", "no_progress_grace", "unknown"),
            (projected["condition"], projected["cause"], projected["activity"]),
        )

    def test_new_review_request_excludes_older_accepted_live_run(self) -> None:
        unit = self.add_unit("in_review", developer=3, updated_at="2026-08-10 11:58:00")
        older = self.add_message(
            unit_id=unit,
            receiver=2,
            kind="review_request",
            disposition="accepted",
            read_at="2026-08-10 11:50:00",
            delivered_at="2026-08-10 11:49:00",
            created_at="2026-08-10 11:48:00",
        )
        older_wake = self.add_wake(
            older, receiver=2, state="delivered", created_at="2026-08-10 11:48:00"
        )
        self.add_live_run(older, older_wake, shell_id=2, suffix="stale_review")
        current = self.add_message(
            unit_id=unit,
            receiver=2,
            kind="review_request",
            disposition="pending",
            created_at="2026-08-10 11:58:00",
        )
        self.add_wake(
            current, receiver=2, state="pending", created_at="2026-08-10 11:58:00"
        )
        self.add_event(
            "review.requested",
            at="2026-08-10 11:58:00",
            work_unit_id=unit,
            payload={"message_id": current},
        )
        self.heartbeat("sprint-runtime", "2026-08-10 12:00:00")

        projected = self.project()["work_units"][unit]

        self.assertEqual(("waiting_external", "wake_pending", "idle"), (
            projected["condition"], projected["cause"], projected["activity"]
        ))
        self.assertEqual([{"message_id": current}], projected["message_refs"])

    def test_ci_boundaries_and_watcher_precedence_are_exact(self) -> None:
        self.con.execute(
            "UPDATE sprints SET armed_at='2026-08-10 10:00:00' WHERE sprint_id=?",
            (self.sprint_id,),
        )
        unit = self.add_unit("active", developer=3, updated_at="2026-08-10 10:00:00")
        self.add_pr(unit, "pending", at="2026-08-10 10:30:00", number=11)
        self.heartbeat("sprint-pr-watcher", "2026-08-10 12:00:00", 30)
        before = self.project(
            now=datetime(2026, 8, 10, 11, 59, 59, tzinfo=timezone.utc)
        )["work_units"][unit]
        at_boundary = self.project()["work_units"][unit]
        self.assertEqual(("waiting_external", "ci_pending"), (before["condition"], before["cause"]))
        self.assertEqual(
            ("attention", "ci_stuck"),
            (at_boundary["condition"], at_boundary["cause"]),
        )
        self.assertEqual("2026-08-10T10:30:00Z", at_boundary["since"])

    def test_ci_pending_clock_ignores_later_owner_evidence(self) -> None:
        self.con.execute(
            "UPDATE sprints SET armed_at='2026-08-10 10:00:00' WHERE sprint_id=?",
            (self.sprint_id,),
        )
        unit = self.add_unit("active", developer=3, updated_at="2026-08-10 10:00:00")
        self.add_pr(unit, "pending", at="2026-08-10 10:00:00", number=21)
        assignment = self.add_message(
            unit_id=unit,
            receiver=3,
            kind="work_assignment",
            disposition="accepted",
            read_at="2026-08-10 11:20:00",
            delivered_at="2026-08-10 11:19:00",
            created_at="2026-08-10 11:18:00",
        )
        self.add_wake(
            assignment,
            receiver=3,
            state="delivered",
            created_at="2026-08-10 11:18:00",
        )
        self.heartbeat("sprint-pr-watcher", "2026-08-10 11:30:00", 30)

        before = self.project(
            now=datetime(2026, 8, 10, 11, 29, 59, tzinfo=timezone.utc)
        )["work_units"][unit]
        boundary = self.project(
            now=datetime(2026, 8, 10, 11, 30, 0, tzinfo=timezone.utc)
        )["work_units"][unit]

        self.assertEqual(
            ("waiting_external", "ci_pending", "2026-08-10T10:00:00Z", 5399),
            (before["condition"], before["cause"], before["since"], before["age_seconds"]),
        )
        self.assertEqual(
            ("attention", "ci_stuck", "2026-08-10T10:00:00Z", 5400),
            (
                boundary["condition"],
                boundary["cause"],
                boundary["since"],
                boundary["age_seconds"],
            ),
        )

    def test_thirty_minute_no_progress_and_unpicked_wake_boundaries_are_exact(self) -> None:
        self.con.execute(
            "UPDATE sprints SET armed_at='2026-08-10 11:30:00' WHERE sprint_id=?",
            (self.sprint_id,),
        )
        idle = self.add_unit(
            "active", developer=3, updated_at="2026-08-10 11:30:00"
        )
        ready = self.add_unit(
            "ready", developer=4, updated_at="2026-08-10 11:30:00"
        )
        message = self.add_message(
            unit_id=ready,
            receiver=4,
            kind="work_assignment",
            disposition="pending",
            created_at="2026-08-10 11:30:00",
        )
        self.add_wake(
            message,
            receiver=4,
            state="pending",
            created_at="2026-08-10 11:30:00",
        )
        self.heartbeat("sprint-runtime", "2026-08-10 12:00:00")

        before = self.project(
            now=datetime(2026, 8, 10, 11, 59, 59, tzinfo=timezone.utc)
        )["work_units"]
        boundary = self.project()["work_units"]
        self.assertEqual(
            ("waiting_external", "no_progress_grace"),
            (before[idle]["condition"], before[idle]["cause"]),
        )
        self.assertEqual(
            ("attention", "no_progress_carrier"),
            (boundary[idle]["condition"], boundary[idle]["cause"]),
        )
        self.assertEqual(
            ("waiting_external", "wake_pending"),
            (before[ready]["condition"], before[ready]["cause"]),
        )
        self.assertEqual(
            ("attention", "wake_pending"),
            (boundary[ready]["condition"], boundary[ready]["cause"]),
        )

    def test_pending_wake_retry_restarts_the_exact_boundary(self) -> None:
        self.con.execute(
            "UPDATE sprints SET armed_at='2026-08-10 10:00:00' WHERE sprint_id=?",
            (self.sprint_id,),
        )
        unit = self.add_unit("ready", developer=3, updated_at="2026-08-10 10:00:00")
        message = self.add_message(
            unit_id=unit,
            receiver=3,
            kind="work_assignment",
            disposition="pending",
            created_at="2026-08-10 10:00:00",
        )
        wake = self.add_wake(
            message, receiver=3, state="pending", created_at="2026-08-10 10:00:00"
        )
        self.con.execute(
            "UPDATE sprint_wake_outbox SET attempt_count=2,"
            "available_at='2026-08-10 11:30:00' WHERE wake_id=?",
            (wake,),
        )
        self.con.execute(
            "INSERT INTO sprint_wake_attempts "
            "(wake_id,attempt_number,outcome,error_detail,attempted_at) "
            "VALUES (?,2,'failed','temporary delivery fault','2026-08-10 11:30:00')",
            (wake,),
        )
        self.add_event(
            "work_unit.ready",
            at="2026-08-10 10:00:00",
            work_unit_id=unit,
            payload={"message_id": message, "wake_id": wake},
        )
        self.heartbeat("sprint-runtime", "2026-08-10 12:00:00")

        before = self.project(
            now=datetime(2026, 8, 10, 11, 59, 59, tzinfo=timezone.utc)
        )["work_units"][unit]
        boundary = self.project()["work_units"][unit]

        self.assertEqual(("waiting_external", "wake_pending"), (
            before["condition"], before["cause"]
        ))
        self.assertEqual(("attention", "wake_pending"), (
            boundary["condition"], boundary["cause"]
        ))
        self.assertEqual("2026-08-10T11:30:00Z", boundary["since"])
        self.assertEqual(1800, boundary["age_seconds"])

    def test_lease_expiry_requeue_restarts_pending_wake_boundary(self) -> None:
        self.con.execute(
            "UPDATE sprints SET armed_at='2026-08-10 10:00:00' WHERE sprint_id=?",
            (self.sprint_id,),
        )
        unit = self.add_unit("ready", developer=3, updated_at="2026-08-10 10:00:00")
        message = self.add_message(
            unit_id=unit,
            receiver=3,
            kind="work_assignment",
            disposition="pending",
            created_at="2026-08-10 10:00:00",
        )
        wake = self.add_wake(
            message, receiver=3, state="pending", created_at="2026-08-10 10:00:00"
        )
        self.add_event(
            "work_unit.ready",
            at="2026-08-10 10:00:00",
            work_unit_id=unit,
            payload={"message_id": message, "wake_id": wake},
        )
        clock = [datetime(2026, 8, 10, 11, 29, tzinfo=timezone.utc)]
        delivery = sprint_message_delivery.SprintWakeDeliveryService(
            self.con,
            now=lambda: clock[0],
            force_new_quiet_seconds=0,
        )
        self.con.commit()

        lease = delivery.claim_next("crashed-worker", lease_seconds=60)
        self.assertEqual(wake, lease.wake_id if lease else None)
        self.assertEqual(
            ("delivering", "2026-08-10 11:29:00", "2026-08-10 11:30:00"),
            tuple(
                self.con.execute(
                    "SELECT state,claimed_at,lease_expires_at "
                    "FROM sprint_wake_outbox WHERE wake_id=?",
                    (wake,),
                ).fetchone()
            ),
        )

        clock[0] = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(1, delivery.requeue_expired())
        self.assertEqual(
            ("pending", "2026-08-10 12:00:00", None, None),
            tuple(
                self.con.execute(
                    "SELECT state,available_at,claimed_at,lease_expires_at "
                    "FROM sprint_wake_outbox WHERE wake_id=?",
                    (wake,),
                ).fetchone()
            ),
        )
        self.heartbeat("sprint-runtime", "2026-08-10 12:30:00")

        before = self.project(
            now=datetime(2026, 8, 10, 12, 29, 59, tzinfo=timezone.utc)
        )["work_units"][unit]
        boundary = self.project(
            now=datetime(2026, 8, 10, 12, 30, 0, tzinfo=timezone.utc)
        )["work_units"][unit]

        self.assertEqual(
            ("waiting_external", "wake_pending", "2026-08-10T12:00:00Z", 1799),
            (before["condition"], before["cause"], before["since"], before["age_seconds"]),
        )
        self.assertEqual(
            ("attention", "wake_pending", "2026-08-10T12:00:00Z", 1800),
            (
                boundary["condition"],
                boundary["cause"],
                boundary["since"],
                boundary["age_seconds"],
            ),
        )

    def test_reply_scope_and_linkage_do_not_cross_unit_boundary(self) -> None:
        self.con.execute(
            "UPDATE sprints SET armed_at='2026-08-10 10:00:00' WHERE sprint_id=?",
            (self.sprint_id,),
        )
        unit = self.add_unit("active", developer=3, updated_at="2026-08-10 10:00:00")
        unit_wait = self.add_message(
            unit_id=unit,
            receiver=1,
            read_at="2026-08-10 11:20:00",
            delivered_at="2026-08-10 11:15:00",
            intent="decision",
            requires_reply=True,
            created_at="2026-08-10 11:10:00",
        )
        sprint_wait = self.add_message(
            unit_id=None,
            receiver=2,
            read_at="2026-08-10 11:50:00",
            delivered_at="2026-08-10 11:49:00",
            intent="question",
            requires_reply=True,
            created_at="2026-08-10 11:48:00",
        )
        projected = self.project()
        self.assertEqual("reply_overdue", projected["work_units"][unit]["cause"])
        self.assertEqual([{"message_id": unit_wait}], projected["work_units"][unit]["message_refs"])
        roots = projected["health"]["root_causes"]
        self.assertEqual(
            {f"work_unit:{unit}:reply_overdue", f"sprint:message:{sprint_wait}"},
            {root["root_id"] for root in roots},
        )
        self.add_message(
            unit_id=unit,
            receiver=3,
            reply_to=unit_wait,
            created_at="2026-08-10 11:59:00",
        )
        resolved = self.project()
        self.assertNotEqual("reply_overdue", resolved["work_units"][unit]["cause"])
        self.assertNotIn(
            f"work_unit:{unit}:reply_overdue",
            {root["root_id"] for root in resolved["health"]["root_causes"]},
        )

    def test_old_open_reply_survives_bounded_message_history_until_linked(self) -> None:
        self.con.execute(
            "UPDATE sprints SET armed_at='2026-08-10 10:00:00' WHERE sprint_id=?",
            (self.sprint_id,),
        )
        unit = self.add_unit("active", developer=3, updated_at="2026-08-10 10:00:00")
        required = self.add_message(
            unit_id=unit,
            receiver=1,
            read_at="2026-08-10 10:30:00",
            delivered_at="2026-08-10 10:29:00",
            intent="decision",
            requires_reply=True,
            created_at="2026-08-10 10:28:00",
        )
        for index in range(100):
            self.add_message(
                unit_id=unit,
                receiver=3,
                created_at=(
                    f"2026-08-10 11:{index // 60:02d}:{index % 60:02d}"
                ),
            )

        open_wait = self.project()["work_units"][unit]

        self.assertEqual(("attention", "reply_overdue"), (
            open_wait["condition"], open_wait["cause"]
        ))
        self.assertEqual([{"message_id": required}], open_wait["message_refs"])

        reply = self.add_message(
            unit_id=unit,
            receiver=3,
            reply_to=required,
            created_at="2026-08-10 11:59:00",
        )
        resolved = self.project()["work_units"][unit]

        self.assertNotEqual("reply_overdue", resolved["cause"])
        self.assertNotIn(
            {"message_id": required},
            resolved["message_refs"],
        )
        self.assertGreater(reply, required)

    def test_sprint_reply_roots_are_bounded_with_exact_truncation_counts(self) -> None:
        self.con.execute(
            "UPDATE sprints SET armed_at='2026-08-10 10:00:00' WHERE sprint_id=?",
            (self.sprint_id,),
        )
        self.add_unit("active", developer=3, updated_at="2026-08-10 11:50:00")
        message_ids = []
        for index in range(125):
            stamp = f"2026-08-10 10:{index // 60:02d}:{index % 60:02d}"
            message_ids.append(
                self.add_message(
                    unit_id=None,
                    receiver=2,
                    read_at=stamp,
                    delivered_at=stamp,
                    intent="decision",
                    requires_reply=True,
                    created_at=stamp,
                )
            )

        first = self.project()["health"]

        self.assertEqual(("attention", "2026-08-10T10:00:00Z"), (
            first["condition"], first["since"]
        ))
        self.assertEqual(125, first["root_cause_count"])
        self.assertEqual(125, first["attention_count"])
        self.assertTrue(first["root_causes_truncated"])
        self.assertEqual(100, len(first["root_causes"]))
        self.assertEqual(
            f"sprint:message:{message_ids[0]}", first["root_causes"][0]["root_id"]
        )
        visible_ids = [root["root_id"] for root in first["root_causes"]]

        recent = self.add_message(
            unit_id=None,
            receiver=2,
            read_at="2026-08-10 11:59:00",
            delivered_at="2026-08-10 11:59:00",
            intent="decision",
            requires_reply=True,
            created_at="2026-08-10 11:59:00",
        )
        second = self.project()["health"]

        self.assertEqual(126, second["root_cause_count"])
        self.assertEqual(125, second["attention_count"])
        self.assertEqual(visible_ids, [root["root_id"] for root in second["root_causes"]])
        self.assertNotIn(
            f"sprint:message:{recent}",
            [root["root_id"] for root in second["root_causes"]],
        )

    def test_blocked_recovery_requires_exact_unit_provenance_after_coalescing(self) -> None:
        clock_now = datetime.now(timezone.utc).replace(microsecond=0)
        stale_at = (clock_now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        self.con.execute(
            "UPDATE sprints SET armed_at=? WHERE sprint_id=?",
            (stale_at, self.sprint_id),
        )
        stale = self.add_unit("blocked", developer=3, updated_at=stale_at)
        planner_recovery = self.add_unit(
            "blocked", developer=4, updated_at=stale_at
        )
        scoped_blocker = self.add_unit(
            "blocked", developer=5, updated_at=stale_at
        )
        for receiver, kind, created in (
            (3, "work_assignment", "2026-08-10 10:00:00"),
            (2, "review_request", "2026-08-10 10:01:00"),
        ):
            message = self.add_message(
                unit_id=stale,
                receiver=receiver,
                kind=kind,
                disposition="pending",
                created_at=created,
            )
            self.add_wake(message, receiver=receiver, state="pending", created_at=created)

        self.con.commit()
        messages = sprint_message_delivery.SprintMessageStore(self.con)
        original = messages.relay(
            self.sprint_id,
            from_shell_id=4,
            to_shortname="PLN1",
            body="Planner recovery carrier",
            idempotency_key="health-real-recovery",
            work_unit_id=planner_recovery,
        )
        recovery_wake = self.recover_terminal_wake(
            original.message_id,
            original.wake_id,
            shell_id=1,
        )
        unrelated = messages.relay(
            self.sprint_id,
            from_shell_id=3,
            to_shortname="PLN1",
            body="Ordinary blocked-unit information",
            idempotency_key="health-unrelated-coalesced",
            work_unit_id=stale,
        )
        self.assertEqual(recovery_wake, unrelated.wake_id)
        blocker = messages.relay(
            self.sprint_id,
            from_shell_id=1,
            to_shortname="DEV5",
            body="Explicit blocker",
            idempotency_key="health-explicit-blocker",
            intent="blocker",
            requires_reply=True,
            work_unit_id=scoped_blocker,
        )
        projection_now = clock_now + timedelta(minutes=1)
        self.heartbeat(
            "sprint-runtime",
            projection_now.strftime("%Y-%m-%d %H:%M:%S"),
        )

        projected = self.project(now=projection_now)["work_units"]

        self.assertEqual(
            ("attention", "blocked_unowned", [], "PLN1"),
            (
                projected[stale]["condition"],
                projected[stale]["cause"],
                projected[stale]["message_refs"],
                projected[stale]["owner"]["participants"][0]["shortname"],
            ),
        )
        self.assertEqual(
            ("wake_pending", [{"message_id": original.message_id}], "PLN1"),
            (
                projected[planner_recovery]["cause"],
                projected[planner_recovery]["message_refs"],
                projected[planner_recovery]["owner"]["participants"][0]["shortname"],
            ),
        )
        self.assertEqual(
            ("wake_pending", [{"message_id": blocker.message_id}], "DEV5"),
            (
                projected[scoped_blocker]["cause"],
                projected[scoped_blocker]["message_refs"],
                projected[scoped_blocker]["owner"]["participants"][0]["shortname"],
            ),
        )

    def test_unknown_native_run_evidence_is_sanitized_and_uses_its_own_clock(self) -> None:
        self.con.execute(
            "UPDATE sprints SET armed_at='2026-08-10 10:00:00' WHERE sprint_id=?",
            (self.sprint_id,),
        )
        raw_refs = (None, "native-run-malformed", "conversation-run:999999")
        units: list[tuple[int, int]] = []
        for offset, raw_ref in enumerate(raw_refs, start=3):
            unit = self.add_unit("active", developer=offset)
            message = self.add_message(
                unit_id=unit,
                receiver=offset,
                kind="work_assignment",
                disposition="accepted",
                read_at="2026-08-10 10:05:00",
                delivered_at="2026-08-10 10:05:00",
                created_at="2026-08-10 10:00:00",
            )
            wake = self.add_wake(
                message,
                receiver=offset,
                state="delivered",
                created_at="2026-08-10 10:05:00",
            )
            attempt_id = int(
                self.con.execute(
                    "INSERT INTO sprint_wake_attempts "
                    "(wake_id,attempt_number,native_run_ref,outcome,attempted_at) "
                    "VALUES (?,1,?,'delivered','2026-08-10 10:30:00')",
                    (wake, raw_ref),
                ).lastrowid
            )
            self.add_event(
                "work_unit.accepted",
                at="2026-08-10 10:00:00",
                work_unit_id=unit,
                payload={"message_id": message},
            )
            units.append((unit, attempt_id))

        before = self.project(
            now=datetime(2026, 8, 10, 10, 59, 59, tzinfo=timezone.utc)
        )
        boundary = self.project(
            now=datetime(2026, 8, 10, 11, 0, 0, tzinfo=timezone.utc)
        )

        for unit, attempt_id in units:
            expected_signal = {
                "kind": "native_run",
                "id": attempt_id,
                "at": "2026-08-10T10:30:00Z",
            }
            self.assertEqual(
                ("waiting_external", "no_progress_grace", 1799),
                (
                    before["work_units"][unit]["condition"],
                    before["work_units"][unit]["cause"],
                    before["work_units"][unit]["age_seconds"],
                ),
            )
            self.assertEqual(
                (
                    "attention",
                    "unreadable_evidence",
                    "2026-08-10T10:30:00Z",
                    1800,
                    [expected_signal],
                ),
                (
                    boundary["work_units"][unit]["condition"],
                    boundary["work_units"][unit]["cause"],
                    boundary["work_units"][unit]["since"],
                    boundary["work_units"][unit]["age_seconds"],
                    boundary["work_units"][unit]["unreadable_signals"],
                ),
            )
            self.assertIn(expected_signal, boundary["health"]["unreadable_signals"])
        rendered = json.dumps(boundary, sort_keys=True)
        self.assertNotIn("native-run-malformed", rendered)
        self.assertNotIn("conversation-run:999999", rendered)

    def test_later_exact_carrier_reset_and_newer_unreadable_fact_order_clocks(self) -> None:
        self.con.execute(
            "UPDATE sprints SET armed_at='2026-08-10 10:00:00' WHERE sprint_id=?",
            (self.sprint_id,),
        )

        def add_episode(
            developer: int,
            *,
            unreadable_at: str,
            carrier_started_at: str,
            carrier_ended_at: str,
        ) -> tuple[int, int]:
            unit = self.add_unit("active", developer=developer)
            assignment = self.add_message(
                unit_id=unit,
                receiver=developer,
                kind="work_assignment",
                disposition="accepted",
                read_at="2026-08-10 10:00:00",
                delivered_at="2026-08-10 10:00:00",
                created_at="2026-08-10 10:00:00",
            )
            assignment_wake = self.add_wake(
                assignment,
                receiver=developer,
                state="delivered",
                created_at="2026-08-10 10:00:00",
            )
            unreadable_attempt = int(
                self.con.execute(
                    "INSERT INTO sprint_wake_attempts "
                    "(wake_id,attempt_number,native_run_ref,outcome,attempted_at) "
                    "VALUES (?,1,'unreadable-native','delivered',?)",
                    (assignment_wake, unreadable_at),
                ).lastrowid
            )
            self.add_event(
                "work_unit.accepted",
                at="2026-08-10 10:00:00",
                work_unit_id=unit,
                payload={"message_id": assignment},
            )

            recovery = self.add_message(
                unit_id=unit,
                receiver=developer,
                delivered_at=carrier_started_at,
                created_at=carrier_started_at,
            )
            recovery_wake = self.add_wake(
                recovery,
                receiver=developer,
                state="delivered",
                created_at=carrier_started_at,
            )
            self.con.execute(
                "UPDATE sprint_wake_outbox SET idempotency_key=? WHERE wake_id=?",
                (
                    f"sprint-recovery:{self.sprint_id}:health:{unit}",
                    recovery_wake,
                ),
            )
            run_id = self.add_live_run(
                recovery,
                recovery_wake,
                shell_id=developer,
                suffix=f"carrier-reset-{unit}",
            )
            run = self.con.execute(
                "SELECT conversation_id,trigger_message_id FROM conversation_runs "
                "WHERE run_id=?",
                (run_id,),
            ).fetchone()
            self.con.execute(
                "UPDATE sprint_wake_attempts SET attempted_at=? WHERE wake_id=?",
                (carrier_started_at, recovery_wake),
            )
            self.con.execute(
                "UPDATE conversation_runs SET state='succeeded',started_at=?,"
                "heartbeat_at=?,ended_at=? WHERE run_id=?",
                (
                    carrier_started_at,
                    carrier_started_at,
                    carrier_ended_at,
                    run_id,
                ),
            )
            self.con.execute(
                "UPDATE conversation_messages SET state='completed',completed_at=? "
                "WHERE message_id=?",
                (carrier_ended_at, int(run["trigger_message_id"])),
            )
            self.con.execute(
                "UPDATE conversations SET state='idle' WHERE conversation_id=?",
                (str(run["conversation_id"]),),
            )
            self.con.execute(
                "DELETE FROM active_shell_chats WHERE shell_id=?",
                (developer,),
            )
            return unit, unreadable_attempt

        later_carrier, later_carrier_attempt = add_episode(
            3,
            unreadable_at="2026-08-10 10:00:00",
            carrier_started_at="2026-08-10 10:40:00",
            carrier_ended_at="2026-08-10 10:50:00",
        )
        newer_unreadable, newer_unreadable_attempt = add_episode(
            4,
            unreadable_at="2026-08-10 10:50:00",
            carrier_started_at="2026-08-10 10:10:00",
            carrier_ended_at="2026-08-10 10:20:00",
        )

        before = self.project(
            now=datetime(2026, 8, 10, 11, 19, 59, tzinfo=timezone.utc)
        )
        boundary = self.project(
            now=datetime(2026, 8, 10, 11, 20, 0, tzinfo=timezone.utc)
        )

        for unit, attempt_id in (
            (later_carrier, later_carrier_attempt),
            (newer_unreadable, newer_unreadable_attempt),
        ):
            signal = {
                "kind": "native_run",
                "id": attempt_id,
                "at": (
                    "2026-08-10T10:00:00Z"
                    if unit == later_carrier
                    else "2026-08-10T10:50:00Z"
                ),
            }
            self.assertEqual(
                (
                    "waiting_external",
                    "no_progress_grace",
                    "2026-08-10T10:50:00Z",
                    1799,
                    [signal],
                ),
                (
                    before["work_units"][unit]["condition"],
                    before["work_units"][unit]["cause"],
                    before["work_units"][unit]["since"],
                    before["work_units"][unit]["age_seconds"],
                    before["work_units"][unit]["unreadable_signals"],
                ),
            )
            self.assertEqual(
                ("attention", "unreadable_evidence", 1800),
                (
                    boundary["work_units"][unit]["condition"],
                    boundary["work_units"][unit]["cause"],
                    boundary["work_units"][unit]["age_seconds"],
                ),
            )

    def test_joined_unknown_run_is_sanitized_unreadable_evidence(self) -> None:
        self.con.execute(
            "UPDATE sprints SET armed_at='2026-08-10 10:00:00' WHERE sprint_id=?",
            (self.sprint_id,),
        )
        unit = self.add_unit("active", developer=3)
        assignment = self.add_message(
            unit_id=unit,
            receiver=3,
            kind="work_assignment",
            disposition="accepted",
            read_at="2026-08-10 10:00:00",
            delivered_at="2026-08-10 10:00:00",
            created_at="2026-08-10 10:00:00",
        )
        wake = self.add_wake(
            assignment,
            receiver=3,
            state="delivered",
            created_at="2026-08-10 10:00:00",
        )
        self.add_event(
            "work_unit.accepted",
            at="2026-08-10 10:00:00",
            work_unit_id=unit,
            payload={"message_id": assignment},
        )
        run_id = self.add_live_run(
            assignment,
            wake,
            shell_id=3,
            suffix="unknown-outcome",
        )
        run = self.con.execute(
            "SELECT conversation_id,trigger_message_id FROM conversation_runs "
            "WHERE run_id=?",
            (run_id,),
        ).fetchone()
        attempt_id = int(
            self.con.execute(
                "SELECT attempt_id FROM sprint_wake_attempts WHERE wake_id=?",
                (wake,),
            ).fetchone()[0]
        )
        self.con.execute(
            "UPDATE sprint_wake_attempts SET attempted_at='2026-08-10 10:00:00' "
            "WHERE attempt_id=?",
            (attempt_id,),
        )
        self.con.execute(
            "UPDATE conversation_runs SET state='unknown',"
            "started_at='2026-08-10 10:20:00',"
            "heartbeat_at='2026-08-10 10:25:00',"
            "ended_at='2026-08-10 10:30:00',"
            "error_code='HARNESS_OUTCOME_UNKNOWN',"
            "error_detail='secret native reconciliation detail' WHERE run_id=?",
            (run_id,),
        )
        self.con.execute(
            "UPDATE conversation_messages SET state='failed',"
            "completed_at='2026-08-10 10:30:00' WHERE message_id=?",
            (int(run["trigger_message_id"]),),
        )
        self.con.execute(
            "UPDATE conversations SET state='error' WHERE conversation_id=?",
            (str(run["conversation_id"]),),
        )
        self.con.execute("DELETE FROM active_shell_chats WHERE shell_id=3")

        before = self.project(
            now=datetime(2026, 8, 10, 10, 59, 59, tzinfo=timezone.utc)
        )
        boundary = self.project(
            now=datetime(2026, 8, 10, 11, 0, 0, tzinfo=timezone.utc)
        )
        signal = {
            "kind": "native_run",
            "id": attempt_id,
            "at": "2026-08-10T10:30:00Z",
        }

        self.assertEqual(
            ("waiting_external", "no_progress_grace", 1799),
            (
                before["work_units"][unit]["condition"],
                before["work_units"][unit]["cause"],
                before["work_units"][unit]["age_seconds"],
            ),
        )
        self.assertEqual(
            (
                "attention",
                "unreadable_evidence",
                "2026-08-10T10:30:00Z",
                1800,
                [signal],
            ),
            (
                boundary["work_units"][unit]["condition"],
                boundary["work_units"][unit]["cause"],
                boundary["work_units"][unit]["since"],
                boundary["work_units"][unit]["age_seconds"],
                boundary["work_units"][unit]["unreadable_signals"],
            ),
        )
        self.assertEqual([signal], boundary["health"]["unreadable_signals"])
        rendered = json.dumps(boundary, sort_keys=True)
        self.assertNotIn("secret native reconciliation detail", rendered)
        self.assertNotIn("HARNESS_OUTCOME_UNKNOWN", rendered)

    def test_plural_dependency_roots_preserve_mixed_conditions(self) -> None:
        attention = self.add_unit("active", developer=3, updated_at="2026-08-10 09:00:00")
        infrastructure = self.add_unit("active", developer=4, updated_at="2026-08-10 09:00:00")
        dependent = self.add_unit("planned", developer=5, updated_at="2026-08-10 09:00:00")
        self.con.execute(
            "UPDATE sprints SET armed_at='2026-08-10 10:00:00' WHERE sprint_id=?",
            (self.sprint_id,),
        )
        self.add_pr(infrastructure, "red", at="2026-08-10 11:50:00", number=12)
        self.con.executemany(
            "INSERT INTO sprint_work_unit_dependencies "
            "(sprint_id,work_unit_id,depends_on_work_unit_id) VALUES (?,?,?)",
            (
                (self.sprint_id, dependent, attention),
                (self.sprint_id, dependent, infrastructure),
            ),
        )
        projected = self.project()
        self.assertEqual(
            [attention, infrastructure],
            projected["work_units"][dependent]["root_work_unit_ids"],
        )
        self.assertEqual("infrastructure", projected["health"]["condition"])
        self.assertEqual(1, projected["health"]["attention_count"])
        self.assertEqual(
            {attention, infrastructure},
            set(projected["health"]["root_work_unit_ids"]),
        )

    def test_rootless_dependency_aggregate_keeps_oldest_winning_clock(self) -> None:
        upstream = self.add_unit("active", developer=3)
        dependent = self.add_unit("planned", developer=4)
        message = self.add_message(
            unit_id=upstream,
            receiver=3,
            kind="work_assignment",
            disposition="accepted",
            read_at="2026-08-10 11:50:00",
            delivered_at="2026-08-10 11:49:00",
        )
        wake = self.add_wake(message, receiver=3, state="delivered")
        self.add_event(
            "work_unit.accepted",
            at="2026-08-10 11:50:00",
            work_unit_id=upstream,
            payload={"message_id": message},
        )
        self.add_live_run(message, wake, shell_id=3, suffix="developer_progress")
        self.con.execute(
            "INSERT INTO sprint_work_unit_dependencies "
            "(sprint_id,work_unit_id,depends_on_work_unit_id) VALUES (?,?,?)",
            (self.sprint_id, dependent, upstream),
        )
        projected = self.project()
        self.assertEqual("waiting_dependency", projected["health"]["condition"])
        self.assertEqual([], projected["health"]["root_causes"])
        self.assertEqual([], projected["work_units"][dependent]["root_work_unit_ids"])

    def test_rootless_progress_and_external_aggregates_keep_oldest_clock(self) -> None:
        self.con.execute(
            "UPDATE sprints SET armed_at='2026-08-10 11:00:00' WHERE sprint_id=?",
            (self.sprint_id,),
        )
        running_units: list[int] = []
        for developer, heartbeat in ((3, "2026-08-10 11:50:00"), (4, "2026-08-10 11:55:00")):
            unit = self.add_unit(
                "active", developer=developer, updated_at="2026-08-10 11:10:00"
            )
            message = self.add_message(
                unit_id=unit,
                receiver=developer,
                kind="work_assignment",
                disposition="accepted",
                read_at="2026-08-10 11:10:00",
                delivered_at="2026-08-10 11:09:00",
                created_at="2026-08-10 11:08:00",
            )
            wake = self.add_wake(
                message,
                receiver=developer,
                state="delivered",
                created_at="2026-08-10 11:08:00",
            )
            self.add_event(
                "work_unit.accepted",
                at="2026-08-10 11:10:00",
                work_unit_id=unit,
                payload={"message_id": message},
            )
            run_id = self.add_live_run(
                message,
                wake,
                shell_id=developer,
                suffix=f"rootless-progress-{developer}",
            )
            self.con.execute(
                "UPDATE conversation_runs SET heartbeat_at=? WHERE run_id=?",
                (heartbeat, run_id),
            )
            running_units.append(unit)

        progressing = self.project()
        self.assertEqual(
            ("progressing", "2026-08-10T11:50:00Z", 600, [], []),
            (
                progressing["health"]["condition"],
                progressing["health"]["since"],
                progressing["health"]["age_seconds"],
                progressing["health"]["root_work_unit_ids"],
                progressing["health"]["root_causes"],
            ),
        )
        self.assertEqual(
            ["2026-08-10T11:50:00Z", "2026-08-10T11:55:00Z"],
            [progressing["work_units"][unit]["since"] for unit in running_units],
        )

        external_units = [
            self.add_unit("active", developer=5, updated_at="2026-08-10 11:40:00"),
            self.add_unit("active", developer=6, updated_at="2026-08-10 11:45:00"),
        ]
        waiting = self.project()
        self.assertEqual(
            ("waiting_external", "2026-08-10T11:40:00Z", 1200, [], []),
            (
                waiting["health"]["condition"],
                waiting["health"]["since"],
                waiting["health"]["age_seconds"],
                waiting["health"]["root_work_unit_ids"],
                waiting["health"]["root_causes"],
            ),
        )
        self.assertEqual(
            ["no_progress_grace", "no_progress_grace"],
            [waiting["work_units"][unit]["cause"] for unit in external_units],
        )

    def test_historical_quota_escalations_replay_without_new_liveness_wakes(self) -> None:
        account_id = int(
            self.con.execute(
                "INSERT INTO harness_quota_account (provider,account_ref) "
                "VALUES ('anthropic','historical-reviewer')"
            ).lastrowid
        )
        self.con.execute(
            "INSERT INTO harness_quota_window "
            "(account_pk,window_kind,used_percent,resets_at,captured_at,status) "
            "VALUES (?,'five_hour',100,'2026-08-10 13:00:00',"
            "'2026-08-10 11:59:00','ok')",
            (account_id,),
        )
        sprint_ids: list[int] = []
        case = 0
        for sprint_index in range(8):
            if sprint_index:
                self.sprint_id = self._new_sprint()
            sprint_ids.append(self.sprint_id)
            case_count = 3 if sprint_index < 5 else 2
            for _ in range(case_count):
                unit = self.add_unit(
                    "in_review",
                    developer=3 + (case % 13),
                    updated_at="2026-08-10 11:50:00",
                )
                request = self.add_message(
                    unit_id=unit,
                    receiver=2,
                    kind="review_request",
                    disposition="accepted",
                    read_at="2026-08-10 11:54:00",
                    delivered_at="2026-08-10 11:53:00",
                    created_at=f"2026-08-10 11:{case:02d}:00",
                )
                wake = self.add_wake(
                    request,
                    receiver=2,
                    state="delivered",
                    created_at=f"2026-08-10 11:{case:02d}:00",
                )
                self.add_event(
                    "review.requested",
                    at="2026-08-10 11:54:00",
                    work_unit_id=unit,
                    payload={"message_id": request},
                )
                run_id = self.add_live_run(
                    request,
                    wake,
                    shell_id=2,
                    suffix=f"historical-review-{case}",
                    provider="anthropic",
                )
                reviewer_participant_id = int(
                    self.con.execute(
                        "SELECT participant_id FROM sprint_participants "
                        "WHERE sprint_id=? AND shell_id=2",
                        (self.sprint_id,),
                    ).fetchone()[0]
                )
                self.con.execute(
                    "INSERT INTO sprint_liveness_expectations "
                    "(message_id,sprint_id,participant_id,accepted_at,last_strong_at,"
                    "last_strong_key,next_evaluation_at,escalated_at) "
                    "VALUES (?,?,?,'2026-08-10 11:54:00','2026-08-10 11:54:00',"
                    "?,'2026-08-10 11:59:00','2026-08-10 11:59:00')",
                    (
                        request,
                        self.sprint_id,
                        reviewer_participant_id,
                        f"message.accepted:{request}",
                    ),
                )
                escalation = self.add_message(
                    unit_id=None,
                    receiver=1,
                    kind="escalation",
                    created_at=f"2026-08-10 11:{case:02d}:30",
                )
                self.add_event(
                    "liveness.escalated",
                    at=f"2026-08-10 11:{case:02d}:30",
                    payload={
                        "expectation_message_id": request,
                        "escalation_message_id": escalation,
                    },
                )
                self.con.commit()
                history_before = (
                    self.con.execute(
                        "SELECT COUNT(*) FROM sprint_liveness_expectations"
                    ).fetchone()[0],
                    self.con.execute(
                        "SELECT COUNT(*) FROM sprint_events "
                        "WHERE event_type LIKE 'liveness.%'"
                    ).fetchone()[0],
                    self.con.execute(
                        "SELECT COUNT(*) FROM wake_message "
                        "WHERE message_kind IN ('nudge','escalation')"
                    ).fetchone()[0],
                )
                changes_before = self.con.total_changes

                projected = self.project()

                self.assertEqual(changes_before, self.con.total_changes)
                self.assertEqual(
                    history_before,
                    (
                        self.con.execute(
                            "SELECT COUNT(*) FROM sprint_liveness_expectations"
                        ).fetchone()[0],
                        self.con.execute(
                            "SELECT COUNT(*) FROM sprint_events "
                            "WHERE event_type LIKE 'liveness.%'"
                        ).fetchone()[0],
                        self.con.execute(
                            "SELECT COUNT(*) FROM wake_message "
                            "WHERE message_kind IN ('nudge','escalation')"
                        ).fetchone()[0],
                    ),
                )
                self.assertEqual(
                    ("progressing", "run_active", "exhausted", []),
                    (
                        projected["work_units"][unit]["condition"],
                        projected["work_units"][unit]["cause"],
                        projected["work_units"][unit]["capacity"]["state"],
                        projected["work_units"][unit]["root_work_unit_ids"],
                    ),
                )
                active_conversation = self.con.execute(
                    "SELECT chat_id FROM active_shell_chats WHERE shell_id=2"
                ).fetchone()[0]
                trigger_message_id = self.con.execute(
                    "SELECT trigger_message_id FROM conversation_runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0]
                self.con.execute(
                    "UPDATE conversation_runs SET state='succeeded',"
                    "ended_at='2026-08-10 12:00:00' WHERE run_id=?",
                    (run_id,),
                )
                self.con.execute(
                    "UPDATE conversation_messages SET state='completed',"
                    "completed_at='2026-08-10 12:00:00' WHERE message_id=?",
                    (trigger_message_id,),
                )
                self.con.execute(
                    "UPDATE conversations SET state='idle' WHERE conversation_id=?",
                    (active_conversation,),
                )
                self.con.execute("DELETE FROM active_shell_chats WHERE shell_id=2")
                self.con.execute(
                    "UPDATE conversations SET state='closed',"
                    "closed_at='2026-08-10 12:00:00' WHERE conversation_id=?",
                    (active_conversation,),
                )
                self.con.commit()
                case += 1
            self.con.execute(
                "UPDATE sprints SET lifecycle='completed',terminal_outcome='success',"
                "completed_at='2026-08-10 12:00:00' WHERE sprint_id=?",
                (self.sprint_id,),
            )
            self.con.commit()

        self.assertEqual(8, len(set(sprint_ids)))
        self.assertEqual(21, case)
        self.assertEqual(
            (21, 21, 0, 21),
            (
                self.con.execute(
                    "SELECT COUNT(*) FROM sprint_liveness_expectations"
                ).fetchone()[0],
                self.con.execute(
                    "SELECT COUNT(*) FROM sprint_events "
                    "WHERE event_type='liveness.escalated'"
                ).fetchone()[0],
                self.con.execute(
                    "SELECT COUNT(*) FROM wake_message WHERE message_kind='nudge'"
                ).fetchone()[0],
                self.con.execute(
                    "SELECT COUNT(*) FROM wake_message "
                    "WHERE message_kind='escalation'"
                ).fetchone()[0],
            ),
        )

    def test_all_terminal_armed_sprint_projects_closeout_handoff_and_idle(self) -> None:
        self.con.execute(
            "UPDATE sprints SET armed_at='2026-08-10 10:00:00' WHERE sprint_id=?",
            (self.sprint_id,),
        )
        self.add_unit("completed", developer=3)
        self.add_unit("cancelled", developer=4)
        event_id = self.add_event(
            "sprint.delivery_terminal",
            at="2026-08-10 11:00:00",
            payload={"terminal_count": 2, "completed_count": 1, "cancelled_count": 1},
        )
        message = self.add_message(
            unit_id=None,
            receiver=2,
            delivered_at=None,
            created_at="2026-08-10 11:00:00",
        )
        self.con.execute(
            "UPDATE wake_message SET idempotency_key=? WHERE message_id=?",
            (f"sprint:{self.sprint_id}:delivery-terminal:2", message),
        )
        self.add_wake(message, receiver=2, state="pending", created_at="2026-08-10 11:00:00")
        for index in range(100):
            self.add_message(
                unit_id=None,
                receiver=3,
                created_at=f"2026-08-10 11:{index // 60 + 1:02d}:{index % 60:02d}",
            )
        handoff = self.project()
        self.assertEqual(("waiting_external", "conformance_handoff"), (
            handoff["health"]["condition"],
            handoff["health"]["root_causes"][0]["cause"]
            if handoff["health"]["root_causes"]
            else "conformance_handoff",
        ))
        self.assertEqual("2026-08-10T11:00:00Z", handoff["health"]["since"])

        self.con.execute(
            "UPDATE sprint_wake_outbox SET state='delivered',delivered_at='2026-08-10 11:00:00'"
        )
        idle = self.project()
        self.assertEqual("attention", idle["health"]["condition"])
        self.assertEqual("conformance_idle", idle["health"]["root_causes"][0]["cause"])
        self.assertEqual(
            f"sprint:closeout:{event_id}", idle["health"]["root_causes"][0]["root_id"]
        )
        self.assertEqual(
            [{"message_id": message}],
            idle["health"]["root_causes"][0]["message_refs"],
        )

    def test_closeout_boundary_and_finished_carrier_restart_the_clock(self) -> None:
        self.con.execute(
            "UPDATE sprints SET armed_at='2026-08-10 10:00:00' WHERE sprint_id=?",
            (self.sprint_id,),
        )
        self.add_unit("completed", developer=3, updated_at="2026-08-10 10:30:00")
        event_id = self.add_event(
            "sprint.delivery_terminal",
            at="2026-08-10 11:00:00",
            payload={"terminal_count": 1, "completed_count": 1},
        )
        message = self.add_message(
            unit_id=None,
            receiver=2,
            delivered_at="2026-08-10 11:00:00",
            created_at="2026-08-10 11:00:00",
        )
        self.con.execute(
            "UPDATE wake_message SET idempotency_key=? WHERE message_id=?",
            (f"sprint:{self.sprint_id}:delivery-terminal:1", message),
        )
        wake = self.add_wake(
            message,
            receiver=2,
            state="delivered",
            created_at="2026-08-10 11:00:00",
        )

        before = self.project(
            now=datetime(2026, 8, 10, 11, 29, 59, tzinfo=timezone.utc)
        )["health"]
        boundary = self.project(
            now=datetime(2026, 8, 10, 11, 30, 0, tzinfo=timezone.utc)
        )["health"]
        self.assertEqual(
            ("waiting_external", "2026-08-10T11:00:00Z", 1799, []),
            (
                before["condition"],
                before["since"],
                before["age_seconds"],
                before["root_causes"],
            ),
        )
        self.assertEqual(
            (
                "attention",
                "conformance_idle",
                "2026-08-10T11:00:00Z",
                1800,
                f"sprint:closeout:{event_id}",
                [{"message_id": message}],
                "conformance_recorded",
            ),
            (
                boundary["condition"],
                boundary["root_causes"][0]["cause"],
                boundary["since"],
                boundary["age_seconds"],
                boundary["root_causes"][0]["root_id"],
                boundary["root_causes"][0]["message_refs"],
                boundary["root_causes"][0]["next_expected_event"]["code"],
            ),
        )

        run_id = self.add_live_run(
            message,
            wake,
            shell_id=2,
            suffix="closeout-carrier",
        )
        active = self.project()["health"]
        self.assertEqual(("progressing", "2026-08-10T11:59:59Z"), (
            active["condition"], active["since"]
        ))
        self.con.execute(
            "UPDATE conversation_runs SET state='succeeded',"
            "heartbeat_at='2026-08-10 12:00:00',ended_at='2026-08-10 12:00:00' "
            "WHERE run_id=?",
            (run_id,),
        )
        run = self.con.execute(
            "SELECT conversation_id,trigger_message_id FROM conversation_runs "
            "WHERE run_id=?",
            (run_id,),
        ).fetchone()
        self.con.execute(
            "UPDATE conversation_messages SET state='completed',"
            "completed_at='2026-08-10 12:00:00' WHERE message_id=?",
            (run["trigger_message_id"],),
        )
        self.con.execute(
            "UPDATE conversations SET state='idle' WHERE conversation_id=?",
            (run["conversation_id"],),
        )
        self.con.execute("DELETE FROM active_shell_chats WHERE shell_id=2")

        reset_before = self.project(
            now=datetime(2026, 8, 10, 12, 29, 59, tzinfo=timezone.utc)
        )["health"]
        reset_boundary = self.project(
            now=datetime(2026, 8, 10, 12, 30, 0, tzinfo=timezone.utc)
        )["health"]
        self.assertEqual(
            ("waiting_external", "2026-08-10T12:00:00Z", 1799),
            (
                reset_before["condition"],
                reset_before["since"],
                reset_before["age_seconds"],
            ),
        )
        self.assertEqual(
            ("attention", "2026-08-10T12:00:00Z", 1800, run_id),
            (
                reset_boundary["condition"],
                reset_boundary["since"],
                reset_boundary["age_seconds"],
                reset_boundary["root_causes"][0]["last_evidence"]["id"],
            ),
        )

    def test_projection_performs_only_bounded_reads(self) -> None:
        self.add_unit("active", developer=3)
        self.con.commit()
        statements: list[str] = []
        self.con.set_trace_callback(statements.append)
        before = self.con.total_changes
        projected = sprint_health.SprintHealthProjection(
            self.con, now=NOW
        ).project(self.sprint_id)
        self.con.set_trace_callback(None)
        self.assertEqual(before, self.con.total_changes)
        self.assertEqual("waiting_external", projected["health"]["condition"])
        forbidden = ("INSERT ", "UPDATE ", "DELETE ", "REPLACE ")
        self.assertFalse(
            [
                statement
                for statement in statements
                if statement.lstrip().upper().startswith(forbidden)
            ]
        )
        self.assertLessEqual(len(statements), 20)


if __name__ == "__main__":
    unittest.main()
