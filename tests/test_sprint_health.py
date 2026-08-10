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
HISTORICAL_REPLAY = (
    ROOT / "tests" / "fixtures" / "sprint_health" / "historical_replay.json"
)


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
            (2, "Reviewer 1", "REV1", "reviewer"),
            (16, "Reviewer 2", "REV2", "reviewer"),
            (17, "Reviewer 3", "REV3", "reviewer"),
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
        conversation_generation: str = "",
    ) -> int:
        sprint_id = int(
            self.con.execute(
                "INSERT INTO sprints "
                "(feature_id,originating_planner_shell_id,merge_grant_enabled,"
                "conversation_generation,created_at) "
                "VALUES (?,1,1,?,'2026-08-10 09:00:00')",
                (self.feature_id, conversation_generation),
            ).lastrowid
        )
        for shell_id, role in [
            (1, "planner"),
            (2, "reviewer"),
            (16, "reviewer"),
            (17, "reviewer"),
        ] + [
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
        reviewer: int = 2,
        updated_at: str = "2026-08-10 10:00:00",
    ) -> int:
        return int(
            self.con.execute(
                "INSERT INTO sprint_work_units "
                "(sprint_id,assigned_shell_id,reviewer_shell_id,title,expected_output,"
                "planned_wave,disposition,updated_at,completed_at) "
                "VALUES (?,?,?,?,?,0,?,?,?)",
                (
                    self.sprint_id,
                    developer,
                    reviewer,
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
        started_at: str = "2026-08-10 11:55:00",
        heartbeat_at: str = "2026-08-10 11:59:59",
        lease_expires_at: str = "2026-08-10 12:10:00",
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
                "VALUES (?,?,?,'running','test',?,?,?)",
                (
                    conversation,
                    shell_id,
                    prompt_id,
                    lease_expires_at,
                    started_at,
                    heartbeat_at,
                ),
            ).lastrowid
        )
        self.con.execute(
            "INSERT INTO sprint_wake_attempts "
            "(wake_id,attempt_number,target_conversation_id,native_run_ref,outcome,attempted_at) "
            "VALUES (?,1,?,?,'delivered',?)",
            (wake_id, conversation, f"conversation-run:{run_id}", started_at),
        )
        if active:
            self.con.execute(
                "INSERT INTO active_shell_chats "
                "(shell_id,chat_id,process_pid,process_start_ticks,updated_at) "
                "VALUES (?,?,123,456,?)",
                (shell_id, conversation, heartbeat_at),
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

    def replace_armed_sprint(
        self,
        *,
        armed_at: str,
        conversation_generation: str = "",
    ) -> int:
        self.con.execute(
            "UPDATE sprints SET lifecycle='completed',terminal_outcome='success',"
            "completed_at=? WHERE lifecycle='armed'",
            (armed_at,),
        )
        return self._new_sprint(
            armed_at=armed_at,
            conversation_generation=conversation_generation,
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

    def test_pr_and_reply_carrier_matrix_honors_floor_boundary_and_reset(self) -> None:
        cases = (
            ("red", "active", "pr_red_unowned", "developer_evidence"),
            ("green", "active", "green_handoff_idle", "developer_evidence"),
            ("green", "merge_ready", "merge_idle", "merge_observed"),
            ("reply_unread", "active", "reply_unread", "linked_reply"),
            ("reply_waiting", "active", "reply_overdue", "linked_reply"),
            ("blocked", "blocked", "blocked_unowned", "blocker_resolved"),
        )
        before_at = datetime(2026, 8, 10, 11, 59, 59, tzinfo=timezone.utc)
        boundary_at = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)
        reset_before_at = datetime(2026, 8, 10, 12, 44, 59, tzinfo=timezone.utc)
        reset_boundary_at = datetime(2026, 8, 10, 12, 45, 0, tzinfo=timezone.utc)

        for index, (carrier, disposition, boundary_cause, next_code) in enumerate(cases):
            with self.subTest(carrier=carrier):
                self.sprint_id = self.replace_armed_sprint(
                    armed_at="2026-08-10 11:30:00"
                )
                unit = self.add_unit(
                    disposition, developer=3, updated_at="2026-08-10 10:00:00"
                )
                pr_id = None
                reply_id = None
                if carrier in {"red", "green"}:
                    pr_id = self.add_pr(
                        unit,
                        carrier,
                        at="2026-08-10 10:00:00",
                        number=100 + index,
                    )
                    self.heartbeat("sprint-pr-watcher", "2026-08-10 13:00:00", 30)
                elif carrier == "reply_unread":
                    reply_id = self.add_message(
                        unit_id=unit,
                        receiver=1,
                        delivered_at="2026-08-10 10:00:00",
                        intent="question",
                        requires_reply=True,
                        created_at="2026-08-10 09:59:00",
                    )
                elif carrier == "reply_waiting":
                    reply_id = self.add_message(
                        unit_id=unit,
                        receiver=1,
                        delivered_at="2026-08-10 09:59:00",
                        read_at="2026-08-10 10:00:00",
                        intent="decision",
                        requires_reply=True,
                        created_at="2026-08-10 09:58:00",
                    )

                before = self.project(now=before_at)["work_units"][unit]
                boundary = self.project(now=boundary_at)["work_units"][unit]
                grace_cause = (
                    "reply_unread"
                    if carrier == "reply_unread"
                    else "reply_waiting"
                    if carrier == "reply_waiting"
                    else "blocked_grace"
                    if carrier == "blocked"
                    else "no_progress_grace"
                )
                grace_condition = (
                    "waiting_decision"
                    if carrier == "reply_waiting"
                    else "waiting_external"
                )
                self.assertEqual(
                    (grace_condition, grace_cause, "2026-08-10T11:30:00Z", 1799),
                    (
                        before["condition"],
                        before["cause"],
                        before["since"],
                        before["age_seconds"],
                    ),
                )
                self.assertEqual(
                    ("attention", boundary_cause, "2026-08-10T11:30:00Z", 1800),
                    (
                        boundary["condition"],
                        boundary["cause"],
                        boundary["since"],
                        boundary["age_seconds"],
                    ),
                )
                self.assertEqual(next_code, boundary["next_expected_event"]["code"])

                if pr_id is not None:
                    self.con.execute(
                        "INSERT INTO sprint_pr_transitions "
                        "(registered_pr_id,normalized_state,transition_key,observed_at) "
                        "VALUES (?,?,?,?)",
                        (pr_id, carrier, f"reset-{carrier}-{index}", "2026-08-10 12:15:00"),
                    )
                elif carrier == "reply_unread":
                    self.con.execute(
                        "UPDATE wake_message SET delivered_at='2026-08-10 12:15:00' "
                        "WHERE message_id=?",
                        (reply_id,),
                    )
                elif carrier == "reply_waiting":
                    self.add_message(
                        unit_id=unit,
                        receiver=3,
                        reply_to=reply_id,
                        created_at="2026-08-10 12:14:00",
                    )
                    self.add_message(
                        unit_id=unit,
                        receiver=1,
                        delivered_at="2026-08-10 12:14:30",
                        read_at="2026-08-10 12:15:00",
                        intent="decision",
                        requires_reply=True,
                        created_at="2026-08-10 12:14:00",
                    )
                else:
                    self.add_event(
                        "work_unit.replanned",
                        at="2026-08-10 12:15:00",
                        work_unit_id=unit,
                    )

                reset_before = self.project(now=reset_before_at)["work_units"][unit]
                reset_boundary = self.project(now=reset_boundary_at)["work_units"][unit]
                self.assertEqual(
                    (grace_condition, grace_cause, "2026-08-10T12:15:00Z", 1799),
                    (
                        reset_before["condition"],
                        reset_before["cause"],
                        reset_before["since"],
                        reset_before["age_seconds"],
                    ),
                )
                self.assertEqual(
                    ("attention", boundary_cause, "2026-08-10T12:15:00Z", 1800),
                    (
                        reset_boundary["condition"],
                        reset_boundary["cause"],
                        reset_boundary["since"],
                        reset_boundary["age_seconds"],
                    ),
                )

    def test_machinery_carrier_matrix_honors_due_floor_and_recovery(self) -> None:
        with self.subTest(carrier="runtime_missing"):
            self.sprint_id = self.replace_armed_sprint(
                armed_at="2026-08-10 11:30:00"
            )
            unit = self.add_unit(
                "planned", developer=3, updated_at="2026-08-10 10:00:00"
            )
            missing = self.project(
                now=datetime(2026, 8, 10, 11, 30, 0, tzinfo=timezone.utc)
            )["work_units"][unit]
            self.assertEqual(
                ("infrastructure", "runtime_missing", "2026-08-10T11:30:00Z"),
                (missing["condition"], missing["cause"], missing["since"]),
            )
            self.heartbeat("sprint-runtime", "2026-08-10 11:30:00", 5)
            self.assertEqual(
                "no_progress_grace",
                self.project(
                    now=datetime(2026, 8, 10, 11, 30, 0, tzinfo=timezone.utc)
                )["work_units"][unit]["cause"],
            )

        with self.subTest(carrier="watcher_missing"):
            self.sprint_id = self.replace_armed_sprint(
                armed_at="2026-08-10 11:30:00"
            )
            unit = self.add_unit(
                "active", developer=3, updated_at="2026-08-10 10:00:00"
            )
            self.add_pr(unit, "pending", at="2026-08-10 10:00:00", number=200)
            missing = self.project(
                now=datetime(2026, 8, 10, 11, 30, 0, tzinfo=timezone.utc)
            )["work_units"][unit]
            self.assertEqual(
                ("infrastructure", "watcher_missing", "2026-08-10T11:30:00Z"),
                (missing["condition"], missing["cause"], missing["since"]),
            )
            self.heartbeat("sprint-pr-watcher", "2026-08-10 11:30:00", 30)
            self.assertEqual(
                "ci_pending",
                self.project(
                    now=datetime(2026, 8, 10, 11, 30, 0, tzinfo=timezone.utc)
                )["work_units"][unit]["cause"],
            )

        with self.subTest(carrier="runtime_stale"):
            self.sprint_id = self.replace_armed_sprint(
                armed_at="2026-08-10 11:30:00"
            )
            unit = self.add_unit("planned", developer=3, updated_at="2026-08-10 10:00:00")
            self.heartbeat("sprint-runtime", "2026-08-10 11:29:45", 5)
            due = self.project(
                now=datetime(2026, 8, 10, 11, 30, 0, tzinfo=timezone.utc)
            )["work_units"][unit]
            stale = self.project(
                now=datetime(2026, 8, 10, 11, 30, 1, tzinfo=timezone.utc)
            )["work_units"][unit]
            self.assertEqual(("waiting_external", "no_progress_grace"), (
                due["condition"], due["cause"]
            ))
            self.assertEqual(
                ("infrastructure", "runtime_stale", "2026-08-10T11:30:00Z", 1),
                (stale["condition"], stale["cause"], stale["since"], stale["age_seconds"]),
            )
            self.heartbeat("sprint-runtime", "2026-08-10 12:14:45", 5)
            recovered = self.project(
                now=datetime(2026, 8, 10, 12, 15, 0, tzinfo=timezone.utc)
            )["work_units"][unit]
            restale = self.project(
                now=datetime(2026, 8, 10, 12, 15, 1, tzinfo=timezone.utc)
            )["work_units"][unit]
            self.assertEqual("no_progress_carrier", recovered["cause"])
            self.assertEqual(("runtime_stale", "2026-08-10T12:15:00Z"), (
                restale["cause"], restale["since"]
            ))

        with self.subTest(carrier="watcher_stale"):
            self.sprint_id = self.replace_armed_sprint(
                armed_at="2026-08-10 11:30:00"
            )
            unit = self.add_unit("active", developer=3, updated_at="2026-08-10 10:00:00")
            self.add_pr(unit, "pending", at="2026-08-10 10:00:00", number=201)
            watcher_window = 3 * (30 + sprint_health.sprint_pr_watcher.GITHUB_TIMEOUT_SECONDS)
            first_beat = datetime(2026, 8, 10, 11, 30, tzinfo=timezone.utc) - timedelta(
                seconds=watcher_window
            )
            self.heartbeat("sprint-pr-watcher", first_beat.strftime("%Y-%m-%d %H:%M:%S"), 30)
            due = self.project(
                now=datetime(2026, 8, 10, 11, 30, 0, tzinfo=timezone.utc)
            )["work_units"][unit]
            stale = self.project(
                now=datetime(2026, 8, 10, 11, 30, 1, tzinfo=timezone.utc)
            )["work_units"][unit]
            self.assertEqual("ci_pending", due["cause"])
            self.assertEqual(
                ("watcher_stale", "2026-08-10T11:30:00Z", 1),
                (stale["cause"], stale["since"], stale["age_seconds"]),
            )
            second_due = datetime(2026, 8, 10, 12, 15, tzinfo=timezone.utc)
            second_beat = second_due - timedelta(seconds=watcher_window)
            self.heartbeat("sprint-pr-watcher", second_beat.strftime("%Y-%m-%d %H:%M:%S"), 30)
            self.assertEqual(
                "ci_pending", self.project(now=second_due)["work_units"][unit]["cause"]
            )
            restale = self.project(now=second_due + timedelta(seconds=1))["work_units"][unit]
            self.assertEqual(("watcher_stale", "2026-08-10T12:15:00Z"), (
                restale["cause"], restale["since"]
            ))

        with self.subTest(carrier="wake_failed"):
            self.sprint_id = self.replace_armed_sprint(
                armed_at="2026-08-10 11:30:00"
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
                message, receiver=3, state="failed", created_at="2026-08-10 10:00:00"
            )
            self.heartbeat("sprint-runtime", "2026-08-10 12:30:00", 5)
            floored = self.project(
                now=datetime(2026, 8, 10, 11, 30, 0, tzinfo=timezone.utc)
            )["work_units"][unit]
            self.assertEqual(("waiting_external", "no_progress_grace"), (
                floored["condition"], floored["cause"]
            ))
            self.con.execute(
                "UPDATE sprint_wake_outbox SET failed_at='2026-08-10 12:15:00' "
                "WHERE wake_id=?",
                (wake,),
            )
            failed = self.project(
                now=datetime(2026, 8, 10, 12, 15, 0, tzinfo=timezone.utc)
            )["work_units"][unit]
            self.assertEqual(("infrastructure", "wake_failed", "2026-08-10T12:15:00Z"), (
                failed["condition"], failed["cause"], failed["since"]
            ))
            self.con.execute(
                "UPDATE sprint_wake_outbox SET state='pending',failed_at=NULL,"
                "available_at='2026-08-10 12:20:00' WHERE wake_id=?",
                (wake,),
            )
            recovered = self.project(
                now=datetime(2026, 8, 10, 12, 20, 0, tzinfo=timezone.utc)
            )["work_units"][unit]
            self.assertEqual(("waiting_external", "wake_pending", "2026-08-10T12:20:00Z"), (
                recovered["condition"], recovered["cause"], recovered["since"]
            ))

        with self.subTest(carrier="pickup_exhausted"):
            self.sprint_id = self.replace_armed_sprint(
                armed_at="2026-08-10 11:30:00"
            )
            unit = self.add_unit("active", developer=3, updated_at="2026-08-10 10:00:00")
            self.add_event(
                "wake.pickup_exhausted",
                at="2026-08-10 10:00:00",
                work_unit_id=unit,
            )
            floored = self.project(
                now=datetime(2026, 8, 10, 11, 30, 0, tzinfo=timezone.utc)
            )["work_units"][unit]
            self.assertEqual("no_progress_grace", floored["cause"])
            self.add_event(
                "wake.pickup_exhausted",
                at="2026-08-10 12:15:00",
                work_unit_id=unit,
            )
            exhausted = self.project(
                now=datetime(2026, 8, 10, 12, 15, 0, tzinfo=timezone.utc)
            )["work_units"][unit]
            self.assertEqual(
                ("infrastructure", "pickup_exhausted", "2026-08-10T12:15:00Z"),
                (exhausted["condition"], exhausted["cause"], exhausted["since"]),
            )
            self.con.execute(
                "UPDATE sprints SET armed_at='2026-08-10 12:20:00' WHERE sprint_id=?",
                (self.sprint_id,),
            )
            recovered = self.project(
                now=datetime(2026, 8, 10, 12, 20, 0, tzinfo=timezone.utc)
            )["work_units"][unit]
            self.assertEqual(("waiting_external", "no_progress_grace", "2026-08-10T12:20:00Z"), (
                recovered["condition"], recovered["cause"], recovered["since"]
            ))

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

    def test_chain_fork_multilevel_fanin_and_cycle_topologies_are_total(self) -> None:
        self.con.execute(
            "UPDATE sprints SET armed_at='2026-08-10 10:00:00' WHERE sprint_id=?",
            (self.sprint_id,),
        )
        attention = self.add_unit(
            "active", developer=3, updated_at="2026-08-10 10:00:00"
        )
        chain = self.add_unit(
            "planned", developer=4, updated_at="2026-08-10 10:00:00"
        )
        chain_leaf = self.add_unit(
            "planned", developer=5, updated_at="2026-08-10 10:00:00"
        )
        fork_left = self.add_unit(
            "planned", developer=6, updated_at="2026-08-10 10:00:00"
        )
        fork_right = self.add_unit(
            "planned", developer=7, updated_at="2026-08-10 10:00:00"
        )
        infrastructure = self.add_unit(
            "active", developer=8, updated_at="2026-08-10 10:00:00"
        )
        fanin = self.add_unit(
            "planned", developer=9, updated_at="2026-08-10 10:00:00"
        )
        self.add_pr(
            infrastructure, "red", at="2026-08-10 11:50:00", number=301
        )
        edges = (
            (chain, attention),
            (chain_leaf, chain),
            (fork_left, chain),
            (fork_right, chain),
            (fanin, chain_leaf),
            (fanin, fork_left),
            (fanin, fork_right),
            (fanin, infrastructure),
        )
        self.con.executemany(
            "INSERT INTO sprint_work_unit_dependencies "
            "(sprint_id,work_unit_id,depends_on_work_unit_id) VALUES (?,?,?)",
            [(self.sprint_id, unit, upstream) for unit, upstream in edges],
        )

        projected = self.project()

        for unit in (chain, chain_leaf, fork_left, fork_right):
            self.assertEqual(
                [attention],
                projected["work_units"][unit]["root_work_unit_ids"],
                unit,
            )
        self.assertEqual(
            [chain_leaf, fork_left, fork_right, infrastructure],
            projected["work_units"][fanin]["waiting_on_work_unit_ids"],
        )
        self.assertEqual(
            [attention, infrastructure],
            projected["work_units"][fanin]["root_work_unit_ids"],
        )
        self.assertEqual(
            ("infrastructure", 1, [attention, infrastructure]),
            (
                projected["health"]["condition"],
                projected["health"]["attention_count"],
                projected["health"]["root_work_unit_ids"],
            ),
        )
        self.assertEqual(
            {
                f"work_unit:{attention}:no_progress_carrier",
                f"work_unit:{infrastructure}:watcher_missing",
            },
            {root["root_id"] for root in projected["health"]["root_causes"]},
        )

        self.sprint_id = self.replace_armed_sprint(
            armed_at="2026-08-10 10:00:00"
        )
        cycle_left = self.add_unit(
            "planned", developer=10, updated_at="2026-08-10 10:00:00"
        )
        cycle_right = self.add_unit(
            "planned", developer=11, updated_at="2026-08-10 10:00:00"
        )
        self.con.executemany(
            "INSERT INTO sprint_work_unit_dependencies "
            "(sprint_id,work_unit_id,depends_on_work_unit_id) VALUES (?,?,?)",
            (
                (self.sprint_id, cycle_left, cycle_right),
                (self.sprint_id, cycle_right, cycle_left),
            ),
        )

        cycle = self.project()

        self.assertEqual(
            ("waiting_dependency", [], []),
            (
                cycle["health"]["condition"],
                cycle["health"]["root_work_unit_ids"],
                cycle["health"]["root_causes"],
            ),
        )
        self.assertEqual([], cycle["work_units"][cycle_left]["root_work_unit_ids"])
        self.assertEqual([], cycle["work_units"][cycle_right]["root_work_unit_ids"])
        self.assertEqual(
            [{"kind": "dependency_cycle", "id": cycle_left}],
            cycle["health"]["unreadable_signals"],
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

    def test_sanitized_historical_corpus_quota_replays_source_episodes(self) -> None:
        fixture = json.loads(HISTORICAL_REPLAY.read_text())
        summary = fixture["source_summary"]
        sprints = fixture["sprints"]
        episodes = fixture["quota_reviewer_episodes"]
        topology = fixture["quota_episode_topology"]

        expected_sprints = [
            ("history-01", "G1", 16, 0, 1, 12),
            ("history-02", "G2", 45, 21, 0, 6),
            ("history-03", "G3", 2, 0, 0, 0),
            ("history-04", "G4", 2, 0, 0, 0),
            ("history-05", "G5", 11, 0, 0, 1),
            ("history-06", "G6", 11, 0, 1, 0),
            ("history-07", "G7", 2, 0, 0, 0),
            ("history-08", "G8", 16, 0, 0, 0),
        ]
        expected_episodes = [
            ("E01", "UA", "R1", "W01", "C01", 301, 115, "changes_requested"),
            ("E02", "UB", "R2", "W02", "C02", 302, 100, "changes_requested"),
            ("E03", "UA", "R1", "W03", "C03", 304, 163, "changes_requested"),
            ("E04", "UB", "R2", "W04", "C04", 300, 338, "changes_requested"),
            ("E05", "UA", "R1", "W05", "C05", 302, 321, "changes_requested"),
            ("E06", "UB", "R2", "W06", "C06", 304, 394, "changes_requested"),
            ("E07", "UA", "R1", "W07", "C07", 302, 295, "approved"),
            ("E08", "UB", "R2", "W08", "C08", 301, 406, "changes_requested"),
            ("E09", "UB", "R2", "W09", "C09", 302, 248, "approved"),
            ("E10", "UC", "R3", "W10", "C10", 303, 384, "changes_requested"),
            ("E11", "UD", "R1", "W11", "C11", 303, 162, "changes_requested"),
            ("E12", "UC", "R3", "W12", "C12", 300, 479, "changes_requested"),
            ("E13", "UD", "R1", "W13", "C13", 301, 374, "changes_requested"),
            ("E14", "UC", "R3", "W14", "C14", 301, 429, "approved"),
            ("E15", "UD", "R1", "W15", "C15", 303, 205, "approved"),
            ("E16", "UE", "R2", "W16", "C16", 300, 135, "changes_requested"),
            ("E17", "UE", "R2", "W17", "C17", 304, 249, "changes_requested"),
            ("E18", "UE", "R2", "W18", "C18", 304, 229, "approved"),
            ("E19", "UF", "R1", "W19", "C19", 307, 348, "approved"),
            ("E20", "UG", "R2", "W20", "C20", 302, 13, "approved"),
            ("E21", "UH", "R2", "W21", "C21", 303, 350, "approved"),
        ]
        actual_sprints = [
            (
                sprint["key"],
                sprint["generation_alias"],
                sprint["liveness_expectation_count"],
                sprint["quota_reviewer_episode_count"],
                sprint["nudge_count"],
                sprint["wake_requeues"],
            )
            for sprint in sprints
        ]
        actual_episodes = [
            (
                episode["sequence"],
                episode["unit_alias"],
                episode["reviewer_alias"],
                episode["wake_alias"],
                episode["conversation_alias"],
                episode["accepted_to_escalation_seconds"],
                episode["escalation_to_verdict_seconds"],
                episode["verdict"],
            )
            for episode in episodes
        ]
        self.assertEqual(3, fixture["schema_version"])
        self.assertEqual(expected_sprints, actual_sprints)
        self.assertEqual(expected_episodes, actual_episodes)
        self.assertEqual(
            (8, 105, 21, 2, 19),
            (
                summary["sprint_count"],
                summary["liveness_expectations"],
                summary["quota_reviewer_episodes"],
                summary["liveness_nudges"],
                summary["wake_requeues"],
            ),
        )
        self.assertEqual(
            ("history-02", "G2", 1, 1, False, False, False, False),
            (
                topology["sprint_key"],
                topology["generation_alias"],
                topology["messages_per_wake"],
                topology["successful_attempts_per_wake"],
                topology["shared_wakes"],
                topology["shared_conversations"],
                topology["stale_generations"],
                topology["requeues_include_episode_wakes"],
            ),
        )
        self.assertEqual(
            (105, 21, 2, 19),
            (
                sum(sprint[2] for sprint in expected_sprints),
                sum(sprint[3] for sprint in expected_sprints),
                sum(sprint[4] for sprint in expected_sprints),
                sum(sprint[5] for sprint in expected_sprints),
            ),
        )
        self.assertEqual(8, len({sprint[1] for sprint in expected_sprints}))
        self.assertEqual(
            (["UA", "UB", "UC", "UD", "UE", "UF", "UG", "UH"], ["R1", "R2", "R3"]),
            (
                list(dict.fromkeys(episode[1] for episode in expected_episodes)),
                list(dict.fromkeys(episode[2] for episode in expected_episodes)),
            ),
        )
        self.assertEqual(21, len({episode[3] for episode in expected_episodes}))
        self.assertEqual(21, len({episode[4] for episode in expected_episodes}))

        sanitized = json.dumps(fixture, sort_keys=True)
        for forbidden in (
            "/home/",
            "account_ref",
            "shell_id",
            "participant_id",
            "process_pid",
            "process_start_ticks",
            "native_run_ref",
            "message_body",
        ):
            self.assertNotIn(forbidden, sanitized)

        account_id = int(
            self.con.execute(
                "INSERT INTO harness_quota_account (provider,account_ref) "
                "VALUES ('anthropic','sanitized-replay-route')"
            ).lastrowid
        )
        replay_base = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)
        reviewer_shells = {"R1": 2, "R2": 16, "R3": 17}
        developer_shells = {
            "UA": 3,
            "UB": 4,
            "UC": 5,
            "UD": 6,
            "UE": 7,
            "UF": 8,
            "UG": 9,
            "UH": 10,
        }
        generation_tokens = {
            f"G{index}": f"{index:032x}" for index in range(1, 9)
        }
        source_sprint_id_by_key: dict[str, int] = {}
        episode_records: list[dict] = []

        def stamp(value: datetime) -> str:
            return value.strftime("%Y-%m-%d %H:%M:%S")

        def parse(value: str) -> datetime:
            parsed = datetime.fromisoformat(value)
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed

        def history_counts() -> tuple[int, int, int, int]:
            return (
                int(self.con.execute(
                    "SELECT COUNT(*) FROM sprint_liveness_expectations"
                ).fetchone()[0]),
                int(self.con.execute(
                    "SELECT COUNT(*) FROM sprint_events "
                    "WHERE event_type LIKE 'liveness.%'"
                ).fetchone()[0]),
                int(self.con.execute(
                    "SELECT COUNT(*) FROM wake_message WHERE message_kind='nudge'"
                ).fetchone()[0]),
                int(self.con.execute(
                    "SELECT COUNT(*) FROM wake_message WHERE message_kind='escalation'"
                ).fetchone()[0]),
            )

        for sprint_index, sprint_shape in enumerate(sprints):
            with self.subTest(sprint=sprint_shape["key"]):
                self.sprint_id = self.replace_armed_sprint(
                    armed_at=stamp(replay_base),
                    conversation_generation=generation_tokens[
                        sprint_shape["generation_alias"]
                    ],
                )
                source_sprint_id_by_key[sprint_shape["key"]] = self.sprint_id
                reviewer_participant_id = int(
                    self.con.execute(
                        "SELECT participant_id FROM sprint_participants "
                        "WHERE sprint_id=? AND shell_id=2",
                        (self.sprint_id,),
                    ).fetchone()[0]
                )
                generic_expectation_count = (
                    sprint_shape["liveness_expectation_count"]
                    - sprint_shape["quota_reviewer_episode_count"]
                )
                for expectation_index in range(generic_expectation_count):
                    accepted_at = replay_base + timedelta(
                        seconds=2000 + sprint_index * 200 + expectation_index
                    )
                    message = self.add_message(
                        unit_id=None,
                        receiver=2,
                        read_at=stamp(accepted_at),
                        delivered_at=stamp(accepted_at - timedelta(seconds=1)),
                        created_at=stamp(accepted_at - timedelta(seconds=2)),
                    )
                    self.con.execute(
                        "INSERT INTO sprint_liveness_expectations "
                        "(message_id,sprint_id,participant_id,accepted_at,last_strong_at,"
                        "last_strong_key,next_evaluation_at,escalated_at) "
                        "VALUES (?,?,?,?,?,?,?,NULL)",
                        (
                            message,
                            self.sprint_id,
                            reviewer_participant_id,
                            stamp(accepted_at),
                            stamp(accepted_at),
                            f"message.accepted:{message}",
                            stamp(accepted_at + timedelta(minutes=10)),
                        ),
                    )

                sprint_episodes = (
                    episodes
                    if sprint_shape["key"] == topology["sprint_key"]
                    else []
                )
                unit_ids: dict[str, int] = {}
                if sprint_episodes:
                    unit_reviewers = {
                        episode["unit_alias"]: episode["reviewer_alias"]
                        for episode in sprint_episodes
                    }
                    for unit_alias, developer in developer_shells.items():
                        unit_ids[unit_alias] = self.add_unit(
                            "in_review",
                            developer=developer,
                            reviewer=reviewer_shells[unit_reviewers[unit_alias]],
                            updated_at=stamp(replay_base),
                        )

                for episode_index, episode in enumerate(sprint_episodes):
                    reviewer_shell = reviewer_shells[episode["reviewer_alias"]]
                    accepted_at = replay_base + timedelta(
                        hours=1, seconds=episode_index * 1200
                    )
                    escalated_at = accepted_at + timedelta(
                        seconds=episode["accepted_to_escalation_seconds"]
                    )
                    verdict_at = escalated_at + timedelta(
                        seconds=episode["escalation_to_verdict_seconds"]
                    )
                    unit = unit_ids[episode["unit_alias"]]
                    request = self.add_message(
                        unit_id=unit,
                        receiver=reviewer_shell,
                        kind="review_request",
                        disposition="accepted",
                        read_at=stamp(accepted_at),
                        delivered_at=stamp(accepted_at - timedelta(seconds=1)),
                        created_at=stamp(accepted_at - timedelta(seconds=2)),
                    )
                    wake_id = self.add_wake(
                        request,
                        receiver=reviewer_shell,
                        state="delivered",
                        created_at=stamp(accepted_at - timedelta(seconds=1)),
                    )
                    self.con.execute(
                        "UPDATE sprint_wake_outbox SET attempt_count=1 "
                        "WHERE wake_id=?",
                        (wake_id,),
                    )
                    self.add_event(
                        "review.requested",
                        at=stamp(accepted_at),
                        work_unit_id=unit,
                        payload={"message_id": request},
                    )
                    participant_id = int(
                        self.con.execute(
                            "SELECT participant_id FROM sprint_participants "
                            "WHERE sprint_id=? AND shell_id=?",
                            (self.sprint_id, reviewer_shell),
                        ).fetchone()[0]
                    )
                    self.con.execute(
                        "INSERT INTO sprint_liveness_expectations "
                        "(message_id,sprint_id,participant_id,accepted_at,last_strong_at,"
                        "last_strong_key,next_evaluation_at,escalated_at) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (
                            request,
                            self.sprint_id,
                            participant_id,
                            stamp(accepted_at),
                            stamp(accepted_at),
                            f"message.accepted:{request}",
                            stamp(escalated_at),
                            stamp(escalated_at),
                        ),
                    )
                    escalation = self.add_message(
                        unit_id=None,
                        receiver=1,
                        kind="escalation",
                        created_at=stamp(escalated_at),
                    )
                    self.add_event(
                        "liveness.escalated",
                        at=stamp(escalated_at),
                        payload={
                            "expectation_message_id": request,
                            "escalation_message_id": escalation,
                        },
                    )
                    project_at = escalated_at + timedelta(seconds=1)
                    self.con.execute(
                        "INSERT INTO harness_quota_window "
                        "(account_pk,window_kind,used_percent,resets_at,captured_at,status) "
                        "VALUES (?,'five_hour',100,?,?,'ok') "
                        "ON CONFLICT(account_pk,window_kind,COALESCE(scope,'')) "
                        "DO UPDATE SET used_percent=excluded.used_percent,"
                        "resets_at=excluded.resets_at,captured_at=excluded.captured_at,"
                        "status=excluded.status",
                        (
                            account_id,
                            stamp(project_at + timedelta(hours=1)),
                            stamp(project_at - timedelta(seconds=1)),
                        ),
                    )
                    run_id = self.add_live_run(
                        request,
                        wake_id,
                        shell_id=reviewer_shell,
                        suffix=f"source-{episode['conversation_alias']}",
                        provider="anthropic",
                        started_at=stamp(escalated_at - timedelta(seconds=1)),
                        heartbeat_at=stamp(project_at),
                        lease_expires_at=stamp(project_at + timedelta(minutes=10)),
                    )
                    self.con.commit()
                    before_counts = history_counts()
                    before_changes = self.con.total_changes

                    projected = self.project(now=project_at)

                    self.assertEqual(before_changes, self.con.total_changes)
                    self.assertEqual(before_counts, history_counts())
                    health = projected["work_units"][unit]
                    self.assertEqual(
                        ("progressing", "run_active", "exhausted", [], []),
                        (
                            health["condition"],
                            health["cause"],
                            health["capacity"]["state"],
                            health["root_work_unit_ids"],
                            health["unreadable_signals"],
                        ),
                    )

                    run = self.con.execute(
                        "SELECT conversation_id,trigger_message_id,started_at "
                        "FROM conversation_runs WHERE run_id=?",
                        (run_id,),
                    ).fetchone()
                    ended_at = project_at + timedelta(seconds=1)
                    self.con.execute(
                        "UPDATE conversation_runs SET state='succeeded',heartbeat_at=?,"
                        "ended_at=? WHERE run_id=?",
                        (stamp(ended_at), stamp(ended_at), run_id),
                    )
                    self.con.execute(
                        "UPDATE conversation_messages SET state='completed',completed_at=? "
                        "WHERE message_id=?",
                        (stamp(ended_at), run["trigger_message_id"]),
                    )
                    self.con.execute(
                        "UPDATE conversations SET state='idle' "
                        "WHERE conversation_id=?",
                        (run["conversation_id"],),
                    )
                    self.con.execute(
                        "UPDATE conversations SET state='closed',closed_at=? "
                        "WHERE conversation_id=?",
                        (stamp(ended_at), run["conversation_id"]),
                    )
                    self.con.execute(
                        "DELETE FROM active_shell_chats WHERE shell_id=?",
                        (reviewer_shell,),
                    )
                    verdict_event = (
                        "review.approved"
                        if episode["verdict"] == "approved"
                        else "review.changes_requested"
                    )
                    self.add_event(
                        verdict_event,
                        at=stamp(verdict_at),
                        work_unit_id=unit,
                        payload={"request_message_id": request},
                    )

                    wake_row = self.con.execute(
                        "SELECT state,attempt_count,idempotency_key "
                        "FROM sprint_wake_outbox WHERE wake_id=?",
                        (wake_id,),
                    ).fetchone()
                    wake_message_count = int(
                        self.con.execute(
                            "SELECT COUNT(*) FROM sprint_wake_messages "
                            "WHERE wake_id=?",
                            (wake_id,),
                        ).fetchone()[0]
                    )
                    attempt = self.con.execute(
                        "SELECT attempt_number,target_conversation_id,outcome "
                        "FROM sprint_wake_attempts WHERE wake_id=?",
                        (wake_id,),
                    ).fetchone()
                    conversation = self.con.execute(
                        "SELECT creation_idempotency_key FROM conversations "
                        "WHERE conversation_id=?",
                        (run["conversation_id"],),
                    ).fetchone()
                    self.assertEqual(
                        ("delivered", 1, 1, 1, run["conversation_id"], "delivered"),
                        (
                            wake_row["state"],
                            wake_row["attempt_count"],
                            wake_message_count,
                            attempt["attempt_number"],
                            attempt["target_conversation_id"],
                            attempt["outcome"],
                        ),
                    )
                    self.assertEqual(
                        "generation:"
                        f"{generation_tokens[topology['generation_alias']]}:"
                        f"wake:{wake_id}",
                        conversation["creation_idempotency_key"],
                    )
                    self.assertEqual(
                        (
                            episode["accepted_to_escalation_seconds"],
                            episode["escalation_to_verdict_seconds"],
                        ),
                        (
                            int((escalated_at - accepted_at).total_seconds()),
                            int((verdict_at - escalated_at).total_seconds()),
                        ),
                    )
                    self.assertLessEqual(parse(run["started_at"]), escalated_at)
                    self.assertGreaterEqual(ended_at, escalated_at)
                    episode_records.append(
                        {
                            "sequence": episode["sequence"],
                            "sprint_id": self.sprint_id,
                            "request_id": request,
                            "wake_id": wake_id,
                            "conversation_id": run["conversation_id"],
                            "run_id": run_id,
                        }
                    )

                actual_counts = (
                    int(self.con.execute(
                        "SELECT COUNT(*) FROM sprint_liveness_expectations "
                        "WHERE sprint_id=?",
                        (self.sprint_id,),
                    ).fetchone()[0]),
                    int(self.con.execute(
                        "SELECT COUNT(*) FROM sprint_liveness_expectations "
                        "WHERE sprint_id=? AND escalated_at IS NOT NULL",
                        (self.sprint_id,),
                    ).fetchone()[0]),
                )
                self.assertEqual(
                    (
                        sprint_shape["liveness_expectation_count"],
                        sprint_shape["quota_reviewer_episode_count"],
                    ),
                    actual_counts,
                )
                self.con.execute(
                    "UPDATE sprints SET lifecycle='completed',terminal_outcome='success',"
                    "completed_at=? WHERE sprint_id=?",
                    (stamp(replay_base + timedelta(days=1)), self.sprint_id),
                )

        source_sprint_ids = set(source_sprint_id_by_key.values())
        self.assertEqual(8, len(source_sprint_ids))
        self.assertEqual(
            {source_sprint_id_by_key["history-02"]},
            {record["sprint_id"] for record in episode_records},
        )
        self.assertEqual(
            (21, 21, 21, 21),
            (
                len({record["request_id"] for record in episode_records}),
                len({record["wake_id"] for record in episode_records}),
                len({record["conversation_id"] for record in episode_records}),
                len({record["run_id"] for record in episode_records}),
            ),
        )
        self.assertEqual(
            [episode["sequence"] for episode in episodes],
            [record["sequence"] for record in episode_records],
        )
        sprint_two_id = source_sprint_id_by_key["history-02"]
        self.assertEqual(
            (8, 21, 21, 21, 13, 8),
            (
                self.con.execute(
                    "SELECT COUNT(*) FROM sprint_work_units WHERE sprint_id=?",
                    (sprint_two_id,),
                ).fetchone()[0],
                self.con.execute(
                    "SELECT COUNT(*) FROM wake_message "
                    "WHERE sprint_id=? AND message_kind='review_request'",
                    (sprint_two_id,),
                ).fetchone()[0],
                self.con.execute(
                    "SELECT COUNT(*) FROM sprint_wake_outbox WHERE sprint_id=?",
                    (sprint_two_id,),
                ).fetchone()[0],
                self.con.execute(
                    "SELECT COUNT(*) FROM sprint_wake_attempts a "
                    "JOIN sprint_wake_outbox w ON w.wake_id=a.wake_id "
                    "WHERE w.sprint_id=?",
                    (sprint_two_id,),
                ).fetchone()[0],
                self.con.execute(
                    "SELECT COUNT(*) FROM sprint_events "
                    "WHERE sprint_id=? AND event_type='review.changes_requested'",
                    (sprint_two_id,),
                ).fetchone()[0],
                self.con.execute(
                    "SELECT COUNT(*) FROM sprint_events "
                    "WHERE sprint_id=? AND event_type='review.approved'",
                    (sprint_two_id,),
                ).fetchone()[0],
            ),
        )
        self.assertEqual(
            (105, 21, 0, 21, 0),
            (
                self.con.execute(
                    "SELECT COUNT(*) FROM sprint_liveness_expectations "
                    f"WHERE sprint_id IN ({','.join('?' for _ in source_sprint_ids)})",
                    tuple(source_sprint_ids),
                ).fetchone()[0],
                self.con.execute(
                    "SELECT COUNT(*) FROM sprint_events "
                    "WHERE event_type LIKE 'liveness.%' "
                    f"AND sprint_id IN ({','.join('?' for _ in source_sprint_ids)})",
                    tuple(source_sprint_ids),
                ).fetchone()[0],
                self.con.execute(
                    "SELECT COUNT(*) FROM wake_message WHERE message_kind='nudge' "
                    f"AND sprint_id IN ({','.join('?' for _ in source_sprint_ids)})",
                    tuple(source_sprint_ids),
                ).fetchone()[0],
                self.con.execute(
                    "SELECT COUNT(*) FROM wake_message WHERE message_kind='escalation' "
                    f"AND sprint_id IN ({','.join('?' for _ in source_sprint_ids)})",
                    tuple(source_sprint_ids),
                ).fetchone()[0],
                self.con.execute(
                    "SELECT COUNT(*) FROM sprint_events WHERE event_type='wake.requeued' "
                    f"AND sprint_id IN ({','.join('?' for _ in source_sprint_ids)})",
                    tuple(source_sprint_ids),
                ).fetchone()[0],
            ),
        )
    def test_sanitized_historical_recovery_and_nudge_topology_projects_every_sprint(
        self,
    ) -> None:
        fixture = json.loads(HISTORICAL_REPLAY.read_text())
        snapshots = fixture["historical_snapshots"]
        recoveries = fixture["wake_recoveries"]
        nudges = fixture["historical_nudges"]
        recovery_topology = fixture["recovery_topology"]
        sprints = fixture["sprints"]

        expected_snapshots = [
            ("history-01", "G1", 12, 27, 28, 3),
            ("history-02", "G2", 12, 30, 31, 2),
            ("history-03", "G3", 13, 32, 33, 1),
            ("history-04", "G4", 15, 52, 53, 1),
            ("history-05", "G5", 16, 33, 34, 1),
            ("history-06", "G6", 15, 33, 34, 2),
            ("history-07", "G7", 15, 35, 36, 1),
            ("history-08", "G8", 11, 30, 31, 2),
        ]
        expected_recoveries = [
            ("R01", "history-01", "G1", 0, "P1", "C1", -5, -5, "queued", None, None, 92, 107, "succeeded", "completed", "legacy"),
            ("R02", "history-01", "G1", 364, "P1", "C1", -8, -5, "running", "running", None, 95, 117, "succeeded", "completed", "legacy"),
            ("R03", "history-01", "G1", 379, "P1", "C1", -8, -5, "queued", None, None, 121, 134, "succeeded", "completed", "legacy"),
            ("R04", "history-01", "G1", 1875, "P1", "C1", -8, -5, "running", "running", None, 20, 67, "succeeded", "completed", "legacy"),
            ("R05", "history-01", "G1", 1910, "P1", "C1", -8, -5, "queued", None, None, 50, 65, "succeeded", "completed", "legacy"),
            ("R06", "history-01", "G1", 2355, "P1", "C1", -6, -5, "running", "running", None, 21, 40, "succeeded", "completed", "legacy"),
            ("R07", "history-01", "G1", 2510, "P1", "C1", -7, -5, "running", "running", None, 26, 41, "succeeded", "completed", "legacy"),
            ("R08", "history-01", "G1", 4700, "P1", "C1", -8, -5, "running", "running", None, 18, 33, "succeeded", "completed", "legacy"),
            ("R09", "history-01", "G1", 8261, "RA", "C2", -7, -5, "queued", None, None, 1267, 1296, "succeeded", "completed", "legacy"),
            ("R10", "history-01", "G1", 8261, "P1", "C1", -5, -5, "running", "running", None, 32, 74, "succeeded", "completed", "legacy"),
            ("R11", "history-01", "G1", 8311, "P1", "C1", -8, -5, "queued", None, None, 41, 56, "succeeded", "completed", "legacy"),
            ("R12", "history-01", "G1", 8757, "P1", "C1", -6, -5, "running", "running", None, 78, 91, "succeeded", "completed", "legacy"),
            ("R13", "history-02", "G2", 0, "RB", "C13", -6, -5, "failed", "unknown", "HARNESS_WORKTREE_MISMATCH", 1, 1, "unknown", "failed", "current"),
            ("R14", "history-02", "G2", 5, "RC", "C14", -7, -5, "failed", "unknown", "HARNESS_WORKTREE_MISMATCH", 1, 1, "unknown", "failed", "current"),
            ("R15", "history-02", "G2", 3966, "RC", "C15", -7, -5, "failed", "unknown", "HARNESS_WORKTREE_MISMATCH", 1, 1, "unknown", "failed", "current"),
            ("R16", "history-02", "G2", 3971, "RB", "C16", -7, -5, "failed", "unknown", "HARNESS_WORKTREE_MISMATCH", 1, 1, "unknown", "failed", "current"),
            ("R17", "history-02", "G2", 4062, "RA", "C17", -8, -5, "failed", "unknown", "HARNESS_WORKTREE_MISMATCH", 0, 0, "unknown", "failed", "current"),
            ("R18", "history-02", "G2", 5312, "RA", "C18", -6, -5, "failed", "unknown", "HARNESS_WORKTREE_MISMATCH", 1, 1, "unknown", "failed", "manual-legacy"),
            ("R19", "history-05", "G5", 0, "RA", "C19", -527, -527, "completed", "succeeded", None, 3, 259, "succeeded", "completed", "current"),
        ]
        expected_nudges = [
            ("N1", "history-01", "G1", "E1", "U1", "D1", 1801, 0, 51, 1, 255, "succeeded", "CN1"),
            ("N2", "history-06", "G6", "E2", "U2", "D2", 905, 0, 35, 6, 44, "succeeded", "CN2"),
        ]
        attention_recoveries = {
            "R04",
            "R05",
            "R06",
            "R07",
            "R08",
            "R09",
            "R10",
            "R11",
            "R12",
            "R15",
            "R16",
            "R17",
            "R18",
        }
        self.assertEqual(
            expected_snapshots,
            [
                (
                    row["sprint_key"],
                    row["generation_alias"],
                    row["run_start_offset"],
                    row["accepted_offset"],
                    row["project_offset"],
                    row["accepted_expectation_count"],
                )
                for row in snapshots
            ],
        )
        self.assertEqual(
            expected_recoveries,
            [
                (
                    row["sequence"],
                    row["sprint_key"],
                    row["generation_alias"],
                    row["event_offset"],
                    row["participant_alias"],
                    row["conversation_alias"],
                    row["prior_create_offset"],
                    row["prior_attempt_offset"],
                    row["prior_turn_state"],
                    row["prior_run_state"],
                    row["reason"],
                    row["replacement_run_start_offset"],
                    row["replacement_run_end_offset"],
                    row["replacement_run_state"],
                    row["replacement_turn_state"],
                    row["creation_mode"],
                )
                for row in recoveries
            ],
        )
        self.assertEqual(
            expected_nudges,
            [
                (
                    row["sequence"],
                    row["sprint_key"],
                    row["generation_alias"],
                    row["expectation_alias"],
                    row["unit_alias"],
                    row["participant_alias"],
                    row["accepted_to_nudge_seconds"],
                    row["delivery_offset"],
                    row["read_offset"],
                    row["run_start_offset"],
                    row["run_end_offset"],
                    row["run_state"],
                    row["conversation_alias"],
                )
                for row in nudges
            ],
        )
        self.assertEqual(
            ("pulse", "delivered", "delivered", 1, "notification", "sprint", "unknown-source", 0, 0, False),
            (
                recovery_topology["trigger"],
                recovery_topology["prior_wake_state"],
                recovery_topology["replacement_wake_state"],
                recovery_topology["attempts_per_wake"],
                recovery_topology["replacement_message_kind"],
                recovery_topology["replacement_scope"],
                recovery_topology["prior_membership"],
                recovery_topology["replacement_create_offset"],
                recovery_topology["replacement_attempt_offset"],
                recovery_topology["shared_wakes_or_messages"],
            ),
        )

        replay_base = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)
        generation_tokens = {
            f"G{index}": f"{index:032x}" for index in range(1, 9)
        }
        participant_shells = {
            "P1": 1,
            "RA": 2,
            "RB": 16,
            "RC": 17,
            "D1": 3,
            "D2": 4,
        }
        recovery_bases = {
            "history-01": replay_base + timedelta(seconds=4),
            "history-02": replay_base + timedelta(seconds=131),
            "history-05": replay_base + timedelta(seconds=134),
        }
        snapshot_developers = {
            "history-01": [3, 5, 6],
            "history-02": [7, 8],
            "history-03": [9],
            "history-04": [10],
            "history-05": [11],
            "history-06": [4, 12],
            "history-07": [13],
            "history-08": [14, 15],
        }
        source_sprint_ids: dict[str, int] = {}
        snapshot_sources: dict[str, dict] = {}
        recovery_records: list[dict] = []
        nudge_records: list[dict] = []
        historical_unreadable_times: dict[int, list[datetime]] = {}
        projection_count = 0

        def stamp(value: datetime) -> str:
            return value.strftime("%Y-%m-%d %H:%M:%S")

        def iso(value: datetime) -> str:
            return value.strftime("%Y-%m-%dT%H:%M:%SZ")

        def participant_id(shell_id: int) -> int:
            return int(
                self.con.execute(
                    "SELECT participant_id FROM sprint_participants "
                    "WHERE sprint_id=? AND shell_id=?",
                    (self.sprint_id, shell_id),
                ).fetchone()[0]
            )

        def liveness_counts() -> tuple[int, int, int]:
            return (
                int(self.con.execute(
                    "SELECT COUNT(*) FROM sprint_liveness_expectations "
                    "WHERE sprint_id=?",
                    (self.sprint_id,),
                ).fetchone()[0]),
                int(self.con.execute(
                    "SELECT COUNT(*) FROM sprint_events "
                    "WHERE sprint_id=? AND event_type LIKE 'liveness.%'",
                    (self.sprint_id,),
                ).fetchone()[0]),
                int(self.con.execute(
                    "SELECT COUNT(*) FROM wake_message "
                    "WHERE sprint_id=? AND message_kind IN ('nudge','escalation')",
                    (self.sprint_id,),
                ).fetchone()[0]),
            )

        def project_without_writes(now: datetime) -> dict:
            nonlocal projection_count
            self.con.commit()
            before = (*liveness_counts(), self.con.total_changes)
            projected = self.project(now=now)
            after = (*liveness_counts(), self.con.total_changes)
            self.assertEqual(before, after)
            projection_count += 1
            return projected

        def assert_historical_projection(
            projected: dict,
            *,
            source: dict,
            observed_at: datetime,
            condition: str,
            cause: str,
        ) -> None:
            since = source["run_ended_at"]
            age_seconds = max(0, int((observed_at - since).total_seconds()))
            unit_ids = source["unit_ids"]
            roots = unit_ids if condition == "attention" else []
            next_event = {
                "code": "developer_evidence",
                "detail": "Developer activity or a registered PR transition",
            }

            def owner(shell_id: int) -> dict:
                return {
                    "mode": "single",
                    "participants": [
                        {
                            "role": "developer",
                            "shell_id": shell_id,
                            "shortname": f"DEV{shell_id}",
                        }
                    ],
                }

            expected_root_causes = [
                {
                    "age_seconds": age_seconds,
                    "cause": cause,
                    "condition": condition,
                    "last_evidence": {
                        "at": iso(since),
                        "id": source["run_ids"][unit_id],
                        "kind": "run",
                    },
                    "message_refs": [],
                    "next_expected_event": next_event,
                    "owner": owner(source["unit_owners"][unit_id]),
                    "root_id": f"work_unit:{unit_id}:{cause}",
                    "scope": "work_unit",
                    "since": iso(since),
                    "work_unit_id": unit_id,
                }
                for unit_id in roots
            ]
            selected_unit = source["unit_id"]
            self.assertEqual(
                {
                    "activity": "unknown",
                    "age_seconds": age_seconds,
                    "capacity": {
                        "age_seconds": None,
                        "captured_at": None,
                        "provider": None,
                        "reset_at": None,
                        "state": "unknown",
                    },
                    "cause": cause,
                    "condition": condition,
                    "last_evidence": {
                        "at": iso(since),
                        "id": source["run_ids"][selected_unit],
                        "kind": "run",
                    },
                    "message_refs": [],
                    "next_expected_event": next_event,
                    "owner": owner(source["unit_owners"][selected_unit]),
                    "root_work_unit_ids": (
                        [selected_unit] if condition == "attention" else []
                    ),
                    "since": iso(since),
                    "unreadable_signals": [],
                    "waiting_on_work_unit_ids": [],
                },
                projected["work_units"][selected_unit],
            )
            health = projected["health"]
            self.assertEqual(
                {
                    "age_seconds": age_seconds,
                    "attention_count": len(roots),
                    "condition": condition,
                    "machinery": {
                        "applicable": True,
                        "runtime": {
                            "beat_at": None,
                            "interval_seconds": 5,
                            "state": "missing",
                        },
                        "watcher": {
                            "beat_at": None,
                            "interval_seconds": None,
                            "state": "never-started",
                        },
                    },
                    "root_cause_count": len(roots),
                    "root_causes": expected_root_causes,
                    "root_causes_truncated": False,
                    "root_work_unit_ids": roots,
                    "since": iso(since),
                },
                {
                    key: value
                    for key, value in health.items()
                    if key != "unreadable_signals"
                },
            )
            unreadable = health["unreadable_signals"]
            self.assertEqual(
                [
                    ("native_run", iso(at))
                    for at in historical_unreadable_times[self.sprint_id]
                ],
                [(signal["kind"], signal["at"]) for signal in unreadable],
            )
            self.assertEqual(
                [{"at", "id", "kind"}] * len(unreadable),
                [set(signal) for signal in unreadable],
            )

        def insert_terminal_run(
            conversation_id: str,
            shell_id: int,
            trigger_message_id: int,
            state: str,
            started_at: datetime,
            ended_at: datetime,
            *,
            reason: str | None = None,
        ) -> int:
            return int(
                self.con.execute(
                    "INSERT INTO conversation_runs "
                    "(conversation_id,shell_id,trigger_message_id,state,lease_owner,"
                    "lease_expires_at,started_at,heartbeat_at,ended_at,error_code) "
                    "VALUES (?,?,?,?,'historical-replay',?,?,?,?,?)",
                    (
                        conversation_id,
                        shell_id,
                        trigger_message_id,
                        state,
                        stamp(ended_at),
                        stamp(started_at),
                        stamp(ended_at),
                        stamp(ended_at),
                        reason,
                    ),
                ).lastrowid
            )

        for sprint_index, (sprint_shape, snapshot) in enumerate(
            zip(sprints, snapshots)
        ):
            with self.subTest(snapshot=sprint_shape["key"]):
                self.sprint_id = self.replace_armed_sprint(
                    armed_at=stamp(replay_base),
                    conversation_generation=generation_tokens[
                        sprint_shape["generation_alias"]
                    ],
                )
                source_sprint_ids[sprint_shape["key"]] = self.sprint_id
                historical_unreadable_times[self.sprint_id] = []
                selected_unit = None
                selected_request = None
                selected_wake = None
                selected_conversation = None
                snapshot_runs: list[tuple[int, int, str, int]] = []
                snapshot_unit_ids: list[int] = []
                snapshot_run_ids: dict[int, int] = {}
                snapshot_unit_owners: dict[int, int] = {}
                for expectation_index in range(
                    snapshot["accepted_expectation_count"]
                ):
                    developer = snapshot_developers[sprint_shape["key"]][
                        expectation_index
                    ]
                    accepted_at = replay_base + timedelta(
                        seconds=snapshot["accepted_offset"]
                    )
                    run_started_at = replay_base + timedelta(
                        seconds=snapshot["run_start_offset"]
                    )
                    project_at = replay_base + timedelta(
                        seconds=snapshot["project_offset"]
                    )
                    unit = self.add_unit(
                        "active",
                        developer=developer,
                        updated_at=stamp(accepted_at),
                    )
                    snapshot_unit_ids.append(unit)
                    snapshot_unit_owners[unit] = developer
                    request = self.add_message(
                        unit_id=unit,
                        receiver=developer,
                        kind="work_assignment",
                        disposition="accepted",
                        read_at=stamp(accepted_at),
                        delivered_at=stamp(accepted_at - timedelta(seconds=1)),
                        created_at=stamp(accepted_at - timedelta(seconds=2)),
                    )
                    wake = self.add_wake(
                        request,
                        receiver=developer,
                        state="delivered",
                        created_at=stamp(accepted_at - timedelta(seconds=1)),
                    )
                    self.con.execute(
                        "UPDATE sprint_wake_outbox SET attempt_count=1 "
                        "WHERE wake_id=?",
                        (wake,),
                    )
                    self.add_event(
                        "work_unit.accepted",
                        at=stamp(accepted_at),
                        work_unit_id=unit,
                        payload={"message_id": request},
                    )
                    self.con.execute(
                        "INSERT INTO sprint_liveness_expectations "
                        "(message_id,sprint_id,participant_id,accepted_at,last_strong_at,"
                        "last_strong_key,next_evaluation_at) VALUES (?,?,?,?,?,?,?)",
                        (
                            request,
                            self.sprint_id,
                            participant_id(developer),
                            stamp(accepted_at),
                            stamp(accepted_at),
                            f"message.accepted:{request}",
                            stamp(accepted_at + timedelta(minutes=10)),
                        ),
                    )
                    run_id = self.add_live_run(
                        request,
                        wake,
                        shell_id=developer,
                        suffix=f"snapshot-{sprint_shape['key']}-{expectation_index}",
                        started_at=stamp(run_started_at),
                        heartbeat_at=stamp(project_at),
                        lease_expires_at=stamp(project_at + timedelta(minutes=10)),
                    )
                    snapshot_run_ids[unit] = run_id
                    run = self.con.execute(
                        "SELECT conversation_id,trigger_message_id "
                        "FROM conversation_runs WHERE run_id=?",
                        (run_id,),
                    ).fetchone()
                    snapshot_runs.append(
                        (
                            run_id,
                            int(run["trigger_message_id"]),
                            str(run["conversation_id"]),
                            developer,
                        )
                    )
                    if expectation_index == 0:
                        selected_unit = unit
                        selected_request = request
                        selected_wake = wake
                        selected_conversation = str(run["conversation_id"])

                projected = project_without_writes(
                    replay_base + timedelta(seconds=snapshot["project_offset"])
                )
                assert selected_unit is not None
                unit_health = projected["work_units"][selected_unit]
                self.assertEqual(
                    ("progressing", "run_active", "developer_evidence", []),
                    (
                        unit_health["condition"],
                        unit_health["cause"],
                        unit_health["next_expected_event"]["code"],
                        unit_health["root_work_unit_ids"],
                    ),
                )
                self.assertEqual(
                    ("progressing", [], []),
                    (
                        projected["health"]["condition"],
                        projected["health"]["root_work_unit_ids"],
                        projected["health"]["root_causes"],
                    ),
                )
                self.assertNotIn("cause", projected["health"])
                self.assertNotIn("next_expected_event", projected["health"])
                self.assertEqual(
                    (snapshot["accepted_expectation_count"], 0, 0),
                    liveness_counts(),
                )

                snapshot_end = replay_base + timedelta(
                    seconds=snapshot["project_offset"] + 1
                )
                for run_id, trigger_id, conversation_id, developer in snapshot_runs:
                    self.con.execute(
                        "UPDATE conversation_runs SET state='succeeded',"
                        "heartbeat_at=?,ended_at=? WHERE run_id=?",
                        (stamp(snapshot_end), stamp(snapshot_end), run_id),
                    )
                    self.con.execute(
                        "UPDATE conversation_messages SET state='completed',"
                        "completed_at=? WHERE message_id=?",
                        (stamp(snapshot_end), trigger_id),
                    )
                    self.con.execute(
                        "UPDATE conversations SET state='idle' "
                        "WHERE conversation_id=?",
                        (conversation_id,),
                    )
                    self.con.execute(
                        "DELETE FROM active_shell_chats WHERE shell_id=?",
                        (developer,),
                    )
                snapshot_sources[sprint_shape["key"]] = {
                    "unit_id": selected_unit,
                    "request_id": selected_request,
                    "wake_id": selected_wake,
                    "conversation_id": selected_conversation,
                    "developer": int(
                        self.con.execute(
                            "SELECT assigned_shell_id FROM sprint_work_units "
                            "WHERE work_unit_id=?",
                            (selected_unit,),
                        ).fetchone()[0]
                    ),
                    "accepted_at": replay_base
                    + timedelta(seconds=snapshot["accepted_offset"]),
                    "run_ended_at": snapshot_end,
                    "unit_ids": snapshot_unit_ids,
                    "run_ids": snapshot_run_ids,
                    "unit_owners": snapshot_unit_owners,
                }

                conversation_ids: dict[str, str] = {}
                conversation_shells: dict[str, int] = {}
                sprint_recoveries = [
                    row for row in recoveries
                    if row["sprint_key"] == sprint_shape["key"]
                ]
                for recovery in sprint_recoveries:
                    event_at = recovery_bases[sprint_shape["key"]] + timedelta(
                        seconds=recovery["event_offset"]
                    )
                    shell_id = participant_shells[recovery["participant_alias"]]
                    receiver = participant_id(shell_id)
                    prior_created_at = event_at + timedelta(
                        seconds=recovery["prior_create_offset"]
                    )
                    prior_attempt_at = event_at + timedelta(
                        seconds=recovery["prior_attempt_offset"]
                    )
                    prior_wake = int(
                        self.con.execute(
                            "INSERT INTO sprint_wake_outbox "
                            "(sprint_id,participant_id,receiver_shell_id,state,"
                            "attempt_count,idempotency_key,created_at,available_at,"
                            "delivered_at) VALUES (?,?,?,'delivered',1,?,?,?,?)",
                            (
                                self.sprint_id,
                                receiver,
                                shell_id,
                                f"source-{recovery['sequence']}-prior",
                                stamp(prior_created_at),
                                stamp(prior_created_at),
                                stamp(prior_attempt_at),
                            ),
                        ).lastrowid
                    )
                    replacement_message = self.add_message(
                        unit_id=None,
                        receiver=shell_id,
                        kind="notification",
                        delivered_at=stamp(event_at),
                        created_at=stamp(event_at),
                    )
                    replacement_wake = self.add_wake(
                        replacement_message,
                        receiver=shell_id,
                        state="delivered",
                        created_at=stamp(event_at),
                    )
                    self.con.execute(
                        "UPDATE sprint_wake_outbox SET attempt_count=1 "
                        "WHERE wake_id=?",
                        (replacement_wake,),
                    )

                    alias = recovery["conversation_alias"]
                    conversation_id = conversation_ids.get(alias)
                    if conversation_id is None:
                        conversation_id = (
                            f"cv-recovery-{sprint_shape['key']}-{alias}"
                        )
                        if recovery["creation_mode"] == "current":
                            creation_key = (
                                "generation:"
                                f"{generation_tokens[recovery['generation_alias']]}:"
                                f"wake:{replacement_wake}"
                            )
                        else:
                            creation_key = (
                                f"{recovery['creation_mode']}:{alias}"
                            )
                        self.con.execute(
                            "INSERT INTO conversations "
                            "(conversation_id,shell_id,owner_user_id,harness,worktree,"
                            "state,title,creation_idempotency_key,"
                            "creation_request_hash,conversation_scope) "
                            "VALUES (?,?,1,'codex','/work','running','historical',"
                            "?,?,'sprint')",
                            (
                                conversation_id,
                                shell_id,
                                creation_key,
                                f"source-{alias}",
                            ),
                        )
                        self.con.execute(
                            "INSERT INTO sprint_participant_conversations "
                            "(sprint_participant_id,conversation_id) VALUES (?,?)",
                            (receiver, conversation_id),
                        )
                        conversation_ids[alias] = conversation_id
                        conversation_shells[alias] = shell_id
                    self.assertEqual(shell_id, conversation_shells[alias])
                    conversation_state = self.con.execute(
                        "SELECT state FROM conversations WHERE conversation_id=?",
                        (conversation_id,),
                    ).fetchone()[0]
                    if conversation_state == "closed":
                        self.con.execute(
                            "UPDATE conversations SET state='idle',closed_at=NULL "
                            "WHERE conversation_id=?",
                            (conversation_id,),
                        )
                    self.con.execute(
                        "UPDATE conversations SET state='queued' "
                        "WHERE conversation_id=?",
                        (conversation_id,),
                    )
                    self.con.execute(
                        "UPDATE conversations SET state='running' "
                        "WHERE conversation_id=?",
                        (conversation_id,),
                    )

                    prior_terminal = recovery["prior_turn_state"] in {
                        "failed", "completed"
                    }
                    prior_prompt = int(
                        self.con.execute(
                            "INSERT INTO conversation_messages "
                            "(conversation_id,sender_kind,sender_ref,message_kind,body,"
                            "idempotency_key,request_hash,state,created_at,completed_at) "
                            "VALUES (?,'engine','wake','prompt','sanitized',?,?,?, ?,?)",
                            (
                                conversation_id,
                                f"source-{recovery['sequence']}-prior",
                                f"source-{recovery['sequence']}-prior",
                                recovery["prior_turn_state"],
                                stamp(prior_attempt_at),
                                stamp(event_at) if prior_terminal else None,
                            ),
                        ).lastrowid
                    )
                    prior_run_id = None
                    if recovery["prior_run_state"] == "running":
                        prior_run_id = int(
                            self.con.execute(
                                "INSERT INTO conversation_runs "
                                "(conversation_id,shell_id,trigger_message_id,state,"
                                "lease_owner,lease_expires_at,started_at,heartbeat_at) "
                                "VALUES (?,?,?,'running','historical-replay',?,?,?)",
                                (
                                    conversation_id,
                                    shell_id,
                                    prior_prompt,
                                    stamp(event_at + timedelta(minutes=10)),
                                    stamp(prior_attempt_at),
                                    stamp(event_at),
                                ),
                            ).lastrowid
                        )
                        self.con.execute(
                            "DELETE FROM active_shell_chats WHERE shell_id=?",
                            (shell_id,),
                        )
                        self.con.execute(
                            "INSERT INTO active_shell_chats "
                            "(shell_id,chat_id,process_pid,process_start_ticks,"
                            "updated_at) VALUES (?,?,123,456,?)",
                            (shell_id, conversation_id, stamp(event_at)),
                        )
                    elif recovery["prior_run_state"] in {"unknown", "succeeded"}:
                        prior_run_id = insert_terminal_run(
                            conversation_id,
                            shell_id,
                            prior_prompt,
                            recovery["prior_run_state"],
                            prior_attempt_at,
                            event_at,
                            reason=recovery["reason"],
                        )
                    self.con.execute(
                        "INSERT INTO sprint_wake_attempts "
                        "(wake_id,attempt_number,target_conversation_id,"
                        "native_run_ref,outcome,attempted_at) "
                        "VALUES (?,1,?,'sanitized-prior','delivered',?)",
                        (prior_wake, conversation_id, stamp(prior_attempt_at)),
                    )

                    replacement_prompt = int(
                        self.con.execute(
                            "INSERT INTO conversation_messages "
                            "(conversation_id,sender_kind,sender_ref,message_kind,body,"
                            "idempotency_key,request_hash,state,created_at) "
                            "VALUES (?,'engine','wake','prompt','sanitized',?,?,"
                            "'running',?)",
                            (
                                conversation_id,
                                f"source-{recovery['sequence']}-replacement",
                                f"source-{recovery['sequence']}-replacement",
                                stamp(event_at),
                            ),
                        ).lastrowid
                    )
                    self.con.execute(
                        "INSERT INTO sprint_wake_attempts "
                        "(wake_id,attempt_number,target_conversation_id,"
                        "native_run_ref,outcome,attempted_at) "
                        "VALUES (?,1,?,'sanitized-replacement','delivered',?)",
                        (replacement_wake, conversation_id, stamp(event_at)),
                    )
                    recovery_event = self.add_event(
                        "wake.requeued",
                        at=stamp(event_at),
                        payload={
                            "trigger": recovery_topology["trigger"],
                            "prior_wake_id": prior_wake,
                            "prior_wake_state": recovery_topology[
                                "prior_wake_state"
                            ],
                            "prior_turn_state": {
                                "message": recovery["prior_turn_state"],
                                "run": recovery["prior_run_state"],
                                "reason": recovery["reason"],
                            },
                            "replacement_wake_id": replacement_wake,
                            "replacement_created": True,
                            "replacement_conversation_id": conversation_id,
                        },
                    )
                    historical_unreadable_times[self.sprint_id].append(event_at)
                    projected = project_without_writes(event_at)
                    needs_attention = recovery["sequence"] in attention_recoveries
                    assert_historical_projection(
                        projected,
                        source=snapshot_sources[sprint_shape["key"]],
                        observed_at=event_at,
                        condition=(
                            "attention" if needs_attention else "waiting_external"
                        ),
                        cause=(
                            "no_progress_carrier"
                            if needs_attention
                            else "no_progress_grace"
                        ),
                    )
                    self.assertEqual(
                        (0, 1, 1, conversation_id, conversation_id),
                        (
                            self.con.execute(
                                "SELECT COUNT(*) FROM sprint_wake_messages "
                                "WHERE wake_id=?",
                                (prior_wake,),
                            ).fetchone()[0],
                            self.con.execute(
                                "SELECT COUNT(*) FROM sprint_wake_messages "
                                "WHERE wake_id=?",
                                (replacement_wake,),
                            ).fetchone()[0],
                            self.con.execute(
                                "SELECT COUNT(*) FROM sprint_wake_attempts "
                                "WHERE wake_id=?",
                                (prior_wake,),
                            ).fetchone()[0],
                            self.con.execute(
                                "SELECT target_conversation_id "
                                "FROM sprint_wake_attempts WHERE wake_id=?",
                                (prior_wake,),
                            ).fetchone()[0],
                            self.con.execute(
                                "SELECT target_conversation_id "
                                "FROM sprint_wake_attempts WHERE wake_id=?",
                                (replacement_wake,),
                            ).fetchone()[0],
                        ),
                    )
                    if prior_run_id is not None and recovery["prior_run_state"] == "running":
                        cleanup_at = event_at + timedelta(seconds=1)
                        self.con.execute(
                            "UPDATE conversation_runs SET state='unknown',"
                            "heartbeat_at=?,ended_at=? WHERE run_id=?",
                            (stamp(cleanup_at), stamp(cleanup_at), prior_run_id),
                        )
                        self.con.execute(
                            "UPDATE conversation_messages SET state='failed',"
                            "completed_at=? WHERE message_id=?",
                            (stamp(cleanup_at), prior_prompt),
                        )
                        self.con.execute(
                            "DELETE FROM active_shell_chats WHERE shell_id=?",
                            (shell_id,),
                        )

                    replacement_start = event_at + timedelta(
                        seconds=recovery["replacement_run_start_offset"]
                    )
                    replacement_end = event_at + timedelta(
                        seconds=recovery["replacement_run_end_offset"]
                    )
                    replacement_run = insert_terminal_run(
                        conversation_id,
                        shell_id,
                        replacement_prompt,
                        recovery["replacement_run_state"],
                        replacement_start,
                        replacement_end,
                    )
                    self.con.execute(
                        "UPDATE conversation_messages SET state=?,completed_at=? "
                        "WHERE message_id=?",
                        (
                            recovery["replacement_turn_state"],
                            stamp(replacement_end),
                            replacement_prompt,
                        ),
                    )
                    self.con.execute(
                        "UPDATE conversations SET state='idle' "
                        "WHERE conversation_id=?",
                        (conversation_id,),
                    )
                    self.con.execute(
                        "UPDATE conversations SET state='closed',closed_at=? "
                        "WHERE conversation_id=?",
                        (stamp(replacement_end), conversation_id),
                    )
                    self.con.execute(
                        "DELETE FROM active_shell_chats WHERE shell_id=?",
                        (shell_id,),
                    )
                    recovery_records.append(
                        {
                            "sequence": recovery["sequence"],
                            "sprint_id": self.sprint_id,
                            "prior_wake": prior_wake,
                            "replacement_wake": replacement_wake,
                            "replacement_message": replacement_message,
                            "conversation_id": conversation_id,
                            "replacement_run": replacement_run,
                            "event_id": recovery_event,
                        }
                    )

                sprint_nudges = [
                    row for row in nudges
                    if row["sprint_key"] == sprint_shape["key"]
                ]
                for nudge in sprint_nudges:
                    source = snapshot_sources[sprint_shape["key"]]
                    nudge_at = source["accepted_at"] + timedelta(
                        seconds=nudge["accepted_to_nudge_seconds"]
                    )
                    nudge_message = self.add_message(
                        unit_id=None,
                        receiver=source["developer"],
                        kind="nudge",
                        delivered_at=stamp(
                            nudge_at + timedelta(seconds=nudge["delivery_offset"])
                        ),
                        read_at=stamp(
                            nudge_at + timedelta(seconds=nudge["read_offset"])
                        ),
                        created_at=stamp(nudge_at),
                    )
                    nudge_wake = self.add_wake(
                        nudge_message,
                        receiver=source["developer"],
                        state="delivered",
                        created_at=stamp(nudge_at),
                    )
                    self.con.execute(
                        "UPDATE sprint_wake_outbox SET attempt_count=1 "
                        "WHERE wake_id=?",
                        (nudge_wake,),
                    )
                    nudge_prompt = int(
                        self.con.execute(
                            "INSERT INTO conversation_messages "
                            "(conversation_id,sender_kind,sender_ref,message_kind,body,"
                            "idempotency_key,request_hash,state,created_at) "
                            "VALUES (?,'engine','wake','prompt','sanitized',?,?,"
                            "'running',?)",
                            (
                                source["conversation_id"],
                                f"source-{nudge['sequence']}",
                                f"source-{nudge['sequence']}",
                                stamp(nudge_at),
                            ),
                        ).lastrowid
                    )
                    self.con.execute(
                        "INSERT INTO sprint_wake_attempts "
                        "(wake_id,attempt_number,target_conversation_id,"
                        "native_run_ref,outcome,attempted_at) "
                        "VALUES (?,1,?,'sanitized-nudge','delivered',?)",
                        (
                            nudge_wake,
                            source["conversation_id"],
                            stamp(nudge_at),
                        ),
                    )
                    nudge_event = self.add_event(
                        "liveness.nudged",
                        at=stamp(nudge_at),
                        payload={
                            "expectation_message_id": source["request_id"],
                            "nudge_message_id": nudge_message,
                        },
                    )
                    historical_unreadable_times[self.sprint_id].append(nudge_at)
                    projected = project_without_writes(nudge_at)
                    assert_historical_projection(
                        projected,
                        source=source,
                        observed_at=nudge_at,
                        condition="waiting_external",
                        cause="no_progress_grace",
                    )
                    nudge_start = nudge_at + timedelta(
                        seconds=nudge["run_start_offset"]
                    )
                    nudge_end = nudge_at + timedelta(
                        seconds=nudge["run_end_offset"]
                    )
                    nudge_run = insert_terminal_run(
                        source["conversation_id"],
                        source["developer"],
                        nudge_prompt,
                        nudge["run_state"],
                        nudge_start,
                        nudge_end,
                    )
                    self.con.execute(
                        "UPDATE conversation_messages SET state='completed',"
                        "completed_at=? WHERE message_id=?",
                        (stamp(nudge_end), nudge_prompt),
                    )
                    self.con.execute(
                        "UPDATE conversations SET state='idle' "
                        "WHERE conversation_id=?",
                        (source["conversation_id"],),
                    )
                    creation_key = self.con.execute(
                        "SELECT creation_idempotency_key FROM conversations "
                        "WHERE conversation_id=?",
                        (source["conversation_id"],),
                    ).fetchone()[0]
                    self.assertEqual(
                        "generation:"
                        f"{generation_tokens[nudge['generation_alias']]}:"
                        f"wake:{source['wake_id']}",
                        creation_key,
                    )
                    self.assertEqual(
                        (
                            nudge["accepted_to_nudge_seconds"],
                            nudge["read_offset"],
                            1,
                            source["conversation_id"],
                        ),
                        (
                            int(
                                (
                                    nudge_at - source["accepted_at"]
                                ).total_seconds()
                            ),
                            int(
                                (
                                    datetime.fromisoformat(
                                        self.con.execute(
                                            "SELECT read_at FROM wake_message "
                                            "WHERE message_id=?",
                                            (nudge_message,),
                                        ).fetchone()[0]
                                    )
                                    - nudge_at.replace(tzinfo=None)
                                ).total_seconds()
                            ),
                            self.con.execute(
                                "SELECT COUNT(*) FROM sprint_wake_attempts "
                                "WHERE wake_id=?",
                                (nudge_wake,),
                            ).fetchone()[0],
                            self.con.execute(
                                "SELECT target_conversation_id "
                                "FROM sprint_wake_attempts WHERE wake_id=?",
                                (nudge_wake,),
                            ).fetchone()[0],
                        ),
                    )
                    nudge_records.append(
                        {
                            "sequence": nudge["sequence"],
                            "sprint_id": self.sprint_id,
                            "message_id": nudge_message,
                            "wake_id": nudge_wake,
                            "conversation_id": source["conversation_id"],
                            "run_id": nudge_run,
                            "event_id": nudge_event,
                        }
                    )

                remaining = (
                    sprint_shape["liveness_expectation_count"]
                    - snapshot["accepted_expectation_count"]
                )
                generic_messages: list[int] = []
                for expectation_index in range(remaining):
                    accepted_at = replay_base + timedelta(
                        hours=6,
                        seconds=sprint_index * 1000 + expectation_index,
                    )
                    message = self.add_message(
                        unit_id=None,
                        receiver=2,
                        read_at=stamp(accepted_at),
                        delivered_at=stamp(accepted_at),
                        created_at=stamp(accepted_at),
                    )
                    generic_messages.append(message)
                    self.con.execute(
                        "INSERT INTO sprint_liveness_expectations "
                        "(message_id,sprint_id,participant_id,accepted_at,"
                        "last_strong_at,last_strong_key,next_evaluation_at) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (
                            message,
                            self.sprint_id,
                            participant_id(2),
                            stamp(accepted_at),
                            stamp(accepted_at),
                            f"message.accepted:{message}",
                            stamp(accepted_at + timedelta(minutes=10)),
                        ),
                    )
                if sprint_shape["key"] == "history-02":
                    for escalation_index, expectation_message in enumerate(
                        generic_messages[:21]
                    ):
                        escalated_at = replay_base + timedelta(
                            hours=8, seconds=escalation_index
                        )
                        self.con.execute(
                            "UPDATE sprint_liveness_expectations "
                            "SET escalated_at=? WHERE message_id=?",
                            (stamp(escalated_at), expectation_message),
                        )
                        escalation = self.add_message(
                            unit_id=None,
                            receiver=1,
                            kind="escalation",
                            created_at=stamp(escalated_at),
                        )
                        self.add_event(
                            "liveness.escalated",
                            at=stamp(escalated_at),
                            payload={
                                "expectation_message_id": expectation_message,
                                "escalation_message_id": escalation,
                            },
                        )

                self.assertEqual(
                    (
                        sprint_shape["liveness_expectation_count"],
                        sprint_shape["nudge_count"]
                        + sprint_shape["quota_reviewer_episode_count"],
                        sprint_shape["nudge_count"]
                        + sprint_shape["quota_reviewer_episode_count"],
                        sprint_shape["wake_requeues"],
                    ),
                    (
                        self.con.execute(
                            "SELECT COUNT(*) FROM sprint_liveness_expectations "
                            "WHERE sprint_id=?",
                            (self.sprint_id,),
                        ).fetchone()[0],
                        self.con.execute(
                            "SELECT COUNT(*) FROM sprint_events "
                            "WHERE sprint_id=? AND event_type LIKE 'liveness.%'",
                            (self.sprint_id,),
                        ).fetchone()[0],
                        self.con.execute(
                            "SELECT COUNT(*) FROM wake_message "
                            "WHERE sprint_id=? AND message_kind IN "
                            "('nudge','escalation')",
                            (self.sprint_id,),
                        ).fetchone()[0],
                        self.con.execute(
                            "SELECT COUNT(*) FROM sprint_events "
                            "WHERE sprint_id=? AND event_type='wake.requeued'",
                            (self.sprint_id,),
                        ).fetchone()[0],
                    ),
                )
                self.con.execute(
                    "UPDATE sprints SET lifecycle='completed',"
                    "terminal_outcome='success',completed_at=? "
                    "WHERE sprint_id=?",
                    (stamp(replay_base + timedelta(days=1)), self.sprint_id),
                )

        sprint_ids = tuple(source_sprint_ids.values())
        placeholders = ",".join("?" for _ in sprint_ids)
        self.assertEqual(29, projection_count)
        self.assertEqual(
            (19, 19, 19, 19, 19),
            (
                len(recovery_records),
                len({row["prior_wake"] for row in recovery_records}),
                len({row["replacement_wake"] for row in recovery_records}),
                len({row["replacement_message"] for row in recovery_records}),
                len({row["event_id"] for row in recovery_records}),
            ),
        )
        self.assertEqual(
            (2, 2, 2, 2),
            (
                len(nudge_records),
                len({row["message_id"] for row in nudge_records}),
                len({row["wake_id"] for row in nudge_records}),
                len({row["conversation_id"] for row in nudge_records}),
            ),
        )
        self.assertTrue(
            {
                row["prior_wake"] for row in recovery_records
            }.isdisjoint({row["replacement_wake"] for row in recovery_records})
        )
        self.assertTrue(
            {
                row["replacement_message"] for row in recovery_records
            }.isdisjoint({row["message_id"] for row in nudge_records})
        )
        self.assertEqual(
            (105, 23, 23, 19),
            (
                self.con.execute(
                    "SELECT COUNT(*) FROM sprint_liveness_expectations "
                    f"WHERE sprint_id IN ({placeholders})",
                    sprint_ids,
                ).fetchone()[0],
                self.con.execute(
                    "SELECT COUNT(*) FROM sprint_events "
                    "WHERE event_type LIKE 'liveness.%' "
                    f"AND sprint_id IN ({placeholders})",
                    sprint_ids,
                ).fetchone()[0],
                self.con.execute(
                    "SELECT COUNT(*) FROM wake_message "
                    "WHERE message_kind IN ('nudge','escalation') "
                    f"AND sprint_id IN ({placeholders})",
                    sprint_ids,
                ).fetchone()[0],
                self.con.execute(
                    "SELECT COUNT(*) FROM sprint_events "
                    "WHERE event_type='wake.requeued' "
                    f"AND sprint_id IN ({placeholders})",
                    sprint_ids,
                ).fetchone()[0],
            ),
        )
    def test_synthetic_quota_escalation_matrix_keeps_live_runs_dominant(self) -> None:
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
