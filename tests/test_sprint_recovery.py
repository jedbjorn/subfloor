"""Stage 8 gates for Sprint pause, resume, abort, and restart recovery."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
API = ROOT / ".super-coder" / "api"
sys.path[:0] = [str(SCRIPTS), str(API), str(ROOT / "tests")]

import server
import sprint_domain
import sprint_message_delivery
import sprint_recovery
from github_pull_requests import GitHubReadError
from test_sprint_pr_watcher import SprintPRWatcherCase, pull_request
from test_sprint_review_loop import SprintReviewLoopCase
from test_sprint_v2_domain import SprintDomainCase


class SprintRecoveryCase(SprintPRWatcherCase):
    def setUp(self) -> None:
        super().setUp()
        self.interrupts: list[int] = []
        self.notifications = 0

    def coordinator(self) -> sprint_recovery.SprintRecoveryCoordinator:
        def interrupt(run_id: int) -> bool:
            self.assertFalse(self.con.in_transaction)
            self.assertEqual(
                "paused",
                self.con.execute(
                    "SELECT lifecycle FROM sprints WHERE sprint_id=?",
                    (self.sprint_id,),
                ).fetchone()[0],
            )
            self.interrupts.append(run_id)
            return True

        def notify() -> bool:
            self.notifications += 1
            return True

        return sprint_recovery.SprintRecoveryCoordinator(
            self.con,
            repo_root=ROOT,
            reader_factory=lambda _repository: self.reader,
            interrupt_run=interrupt,
            notify_commit=notify,
        )

    def add_live_run(self, shell_id: int = 1) -> int:
        participant = self.con.execute(
            "SELECT active.chat_id FROM sprint_participants participant "
            "JOIN active_shell_chats active ON active.shell_id=participant.shell_id "
            "WHERE participant.sprint_id=? AND participant.shell_id=?",
            (self.sprint_id, shell_id),
        ).fetchone()
        conversation_id = str(participant[0])
        token = self.con.execute("SELECT COUNT(*)+1 FROM conversation_runs").fetchone()[
            0
        ]
        message_id = int(
            self.con.execute(
                "INSERT INTO conversation_messages "
                "(conversation_id,sender_kind,sender_ref,message_kind,body,"
                "idempotency_key,request_hash,state) "
                "VALUES (?,'engine','test','prompt','active Sprint turn',?,?,"
                "'running')",
                (conversation_id, f"active:{token}", f"active:{token}"),
            ).lastrowid
        )
        run_id = int(
            self.con.execute(
                "INSERT INTO conversation_runs "
                "(conversation_id,shell_id,trigger_message_id,state,lease_owner,"
                "lease_expires_at,started_at,heartbeat_at) "
                "VALUES (?,?,?,'running','test-broker','2999-01-01 00:00:00',"
                "'2026-08-01 00:00:00','2026-08-01 00:00:00')",
                (conversation_id, shell_id, message_id),
            ).lastrowid
        )
        self.con.execute(
            "UPDATE conversations SET state='queued' WHERE conversation_id=?",
            (conversation_id,),
        )
        self.con.execute(
            "UPDATE conversations SET state='running' WHERE conversation_id=?",
            (conversation_id,),
        )
        self.con.commit()
        return run_id

    def deliver_wake_with_turn(
        self,
        wake_id: int,
        *,
        terminal: bool,
    ) -> tuple[str, str]:
        prompts: list[str] = []

        def deliver(conversation_id: str, prompt: str, key: str) -> str:
            prompts.append(prompt)
            message_state = "completed" if terminal else "queued"
            message_id = int(
                self.con.execute(
                    "INSERT INTO conversation_messages "
                    "(conversation_id,sender_kind,sender_ref,message_kind,body,"
                    "idempotency_key,request_hash,state,completed_at) "
                    "VALUES (?,'engine','test','prompt',?,?,?, ?,?)",
                    (
                        conversation_id,
                        prompt,
                        key,
                        key,
                        message_state,
                        "2026-08-01 00:00:01" if terminal else None,
                    ),
                ).lastrowid
            )
            if terminal:
                run_id = int(
                    self.con.execute(
                        "INSERT INTO conversation_runs "
                        "(conversation_id,shell_id,trigger_message_id,state,"
                        "lease_owner,lease_expires_at,started_at,ended_at,exit_code) "
                        "SELECT ?,shell_id,?,'succeeded','test-broker',"
                        "'2026-08-01 00:00:01','2026-08-01 00:00:00',"
                        "'2026-08-01 00:00:01',0 FROM conversations "
                        "WHERE conversation_id=?",
                        (conversation_id, message_id, conversation_id),
                    ).lastrowid
                )
                for state in ("queued", "running", "waiting"):
                    self.con.execute(
                        "UPDATE conversations SET state=? WHERE conversation_id=?",
                        (state, conversation_id),
                    )
                self.con.commit()
                return f"conversation-run:{run_id}"
            self.con.execute(
                "UPDATE conversations SET state='queued' WHERE conversation_id=?",
                (conversation_id,),
            )
            self.con.commit()
            return f"conversation-message:{message_id}"

        service = sprint_message_delivery.SprintWakeDeliveryService(self.con)
        while True:
            outcome = service.deliver_once(f"delivery-{wake_id}", deliver)
            self.assertIsNotNone(outcome)
            if outcome.wake_id == wake_id:
                break
        self.assertEqual("delivered", outcome.state)
        return prompts[-1], self.con.execute(
            "SELECT idempotency_key FROM sprint_wake_outbox WHERE wake_id=?",
            (wake_id,),
        ).fetchone()[0]

    def fail_wake_turn(
        self,
        wake_id: int,
        *,
        error_code: str,
        run_state: str = "failed",
        slot_state: str = "busy",
    ) -> None:
        def deliver(conversation_id: str, prompt: str, key: str) -> str:
            detail = (
                "shell 'DEV1' already has a live CLI session "
                f"({slot_state}); close it before starting a browser turn"
            )
            message_id = int(
                self.con.execute(
                    "INSERT INTO conversation_messages "
                    "(conversation_id,sender_kind,sender_ref,message_kind,body,"
                    "idempotency_key,request_hash,state,completed_at) "
                    "VALUES (?,'engine','test','prompt',?,?,?,'failed',"
                    "'2026-08-01 00:00:01')",
                    (conversation_id, prompt, key, key),
                ).lastrowid
            )
            run_id = int(
                self.con.execute(
                    "INSERT INTO conversation_runs "
                    "(conversation_id,shell_id,trigger_message_id,state,"
                    "lease_owner,lease_expires_at,started_at,ended_at,error_code,"
                    "error_detail) SELECT ?,shell_id,?,?,'test-broker',"
                    "'2026-08-01 00:00:01','2026-08-01 00:00:00',"
                    "'2026-08-01 00:00:01',?,? FROM conversations "
                    "WHERE conversation_id=?",
                    (
                        conversation_id,
                        message_id,
                        run_state,
                        error_code,
                        detail,
                        conversation_id,
                    ),
                ).lastrowid
            )
            for state in ("queued", "running", "error"):
                self.con.execute(
                    "UPDATE conversations SET state=? WHERE conversation_id=?",
                    (state, conversation_id),
                )
            self.con.commit()
            return f"conversation-run:{run_id}"

        service = sprint_message_delivery.SprintWakeDeliveryService(self.con)
        while True:
            next_wake = int(
                self.con.execute(
                    "SELECT wake_id FROM sprint_wake_outbox "
                    "WHERE state='pending' AND available_at<=datetime('now') "
                    "ORDER BY wake_id LIMIT 1"
                ).fetchone()[0]
            )
            callback = (
                deliver
                if next_wake == wake_id
                else lambda _conversation, _prompt, _key: "drained-run"
            )
            outcome = service.deliver_once(f"failed-turn-{wake_id}", callback)
            self.assertIsNotNone(outcome)
            if outcome.wake_id == wake_id:
                break
        self.assertEqual("delivered", outcome.state)

    def test_participant_pause_atomically_persists_report_interrupt_and_notice(self):
        self.register()
        run_id = self.add_live_run()
        coordinator = self.coordinator()

        receipt = coordinator.pause(
            self.sprint_id,
            sprint_domain.LifecycleActor("participant", 1),
            reason="GitHub state cannot be trusted",
            detail={"provider": "github"},
        )

        self.assertTrue(receipt.changed)
        self.assertEqual((run_id,), receipt.interrupt_run_ids)
        self.assertEqual([run_id], self.interrupts)
        self.assertEqual(1, self.notifications)
        self.assertEqual(
            ("paused", "run.interrupt.requested"),
            tuple(
                self.con.execute(
                    "SELECT s.lifecycle,e.event_type FROM sprints s "
                    "JOIN conversation_events e ON e.run_id=? "
                    "WHERE s.sprint_id=? "
                    "AND e.event_type='run.interrupt.requested'",
                    (run_id, self.sprint_id),
                ).fetchone()
            ),
        )
        report = json.loads(
            self.con.execute(
                "SELECT body FROM sprint_reports WHERE report_id=?",
                (receipt.report_id,),
            ).fetchone()[0]
        )
        self.assertEqual("GitHub state cannot be trusted", report["reason"])
        self.assertEqual({"kind": "participant", "shell_id": 1}, report["actor"])
        self.assertEqual(
            [run_id], [row["run_id"] for row in report["deterministic"]["active_turns"]]
        )
        self.assertEqual(
            [self.unit_id],
            [row["work_unit_id"] for row in report["deterministic"]["work_units"]],
        )
        self.assertEqual(
            ["red"],
            [
                row["normalized_state"]
                for row in report["deterministic"]["registered_prs"]
            ],
        )
        self.assertEqual("", report["integrity_threat"])
        self.assertEqual("", report["judgment"])
        self.assertEqual("", report["recommendation"])
        self.assertEqual(
            [(None, 3, None, 0, "Sprint 1 paused: GitHub state cannot be trusted")],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT sprint_id,receiver_shell_id,to_participant_id,"
                    "actionable,body FROM wake_message "
                    "WHERE idempotency_key LIKE 'sprint-pause:%'"
                )
            ],
        )
        self.assertEqual(
            [],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT sender_kind,sender_ref,state,body "
                    "FROM conversation_messages "
                    "WHERE idempotency_key LIKE 'sprint-pause:%:planner-conversation'"
                )
            ],
        )
        self.assertEqual(
            [(None, "pending")],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT w.participant_id,w.state FROM sprint_wake_outbox w "
                    "JOIN sprint_wake_messages joined USING(wake_id) "
                    "JOIN wake_message message USING(message_id) "
                    "WHERE message.idempotency_key LIKE 'sprint-pause:%'"
                )
            ],
        )
        pause_wake_id = int(
            self.con.execute(
                "SELECT w.wake_id FROM sprint_wake_outbox w "
                "JOIN sprint_wake_messages joined USING(wake_id) "
                "JOIN wake_message message USING(message_id) "
                "WHERE message.idempotency_key LIKE 'sprint-pause:%'"
            ).fetchone()[0]
        )
        pause_outcome = sprint_message_delivery.SprintWakeDeliveryService(
            self.con
        ).deliver_once(
            "paused-notice-worker",
            lambda conversation, prompt, _key: (
                "pr-event-run" if "GitHub PR event" in prompt else conversation
            ),
        )
        self.assertNotEqual(pause_wake_id, pause_outcome.wake_id)
        pause_outcome = sprint_message_delivery.SprintWakeDeliveryService(
            self.con
        ).deliver_once(
            "paused-notice-worker-2",
            lambda conversation, _prompt, _key: conversation,
        )
        self.assertEqual(pause_wake_id, pause_outcome.wake_id)
        self.assertEqual("delivered", pause_outcome.state)

        replay = coordinator.pause(
            self.sprint_id,
            sprint_domain.LifecycleActor("participant", 1),
            reason="GitHub state cannot be trusted",
        )
        self.assertFalse(replay.changed)
        self.assertEqual(
            (1, 1, 1),
            tuple(
                self.con.execute(
                    "SELECT (SELECT COUNT(*) FROM sprint_reports WHERE report_kind='pause'),"
                    "(SELECT COUNT(*) FROM conversation_events WHERE run_id=? "
                    "AND event_type='run.interrupt.requested'),"
                    "(SELECT COUNT(*) FROM wake_message "
                    "WHERE idempotency_key LIKE 'sprint-pause:%')",
                    (run_id,),
                ).fetchone()
            ),
        )

    def test_terminal_wake_failure_uses_the_same_pause_machinery(self):
        run_id = self.add_live_run()
        wake_id = int(self.send("auto-pause-wake").wake_id)
        store = sprint_domain.SprintLifecycleStore(
            self.con,
            interrupt_run=lambda item: self.interrupts.append(item) or True,
            notify_commit=lambda: True,
        )

        self.assertEqual(1, store.record_wake_failure(wake_id, "one"))
        self.assertEqual(2, store.record_wake_failure(wake_id, "two"))
        self.assertEqual(3, store.record_wake_failure(wake_id, "three"))

        pause_outcome = sprint_message_delivery.SprintWakeDeliveryService(
            self.con
        ).deliver_once(
            "auto-pause-notice-worker",
            lambda conversation, _prompt, _key: conversation,
        )
        self.assertIsNotNone(pause_outcome)
        self.assertEqual("delivered", pause_outcome.state)
        self.assertEqual(
            "wake_delivery_exhausted",
            json.loads(
                self.con.execute(
                    "SELECT body FROM sprint_reports WHERE sprint_id=? "
                    "AND report_kind='pause'",
                    (self.sprint_id,),
                ).fetchone()[0]
            )["reason"],
        )

        self.assertEqual([run_id], self.interrupts)
        report = json.loads(
            self.con.execute(
                "SELECT body FROM sprint_reports WHERE sprint_id=? "
                "AND report_kind='pause'",
                (self.sprint_id,),
            ).fetchone()[0]
        )
        self.assertEqual("wake_delivery_exhausted", report["reason"])
        self.assertEqual(
            {"attempts": 3, "last_error": "three", "wake_id": wake_id},
            report["detail"],
        )
        self.assertEqual(
            [run_id], [row["run_id"] for row in report["deterministic"]["active_turns"]]
        )

        receipt = self.coordinator().resume(
            self.sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
            reason="retry the stranded assignment",
        )
        self.assertEqual(1, len(receipt.requeued_wake_ids))
        replacement = receipt.requeued_wake_ids[0]
        self.assertNotEqual(wake_id, replacement)
        self.assertEqual(
            [(wake_id, "failed", 3), (replacement, "pending", 0)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT wake_id,state,attempt_count FROM sprint_wake_outbox "
                    "WHERE wake_id IN (?,?) ORDER BY wake_id",
                    (wake_id, replacement),
                )
            ],
        )
        self.assertEqual(
            [(replacement,)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT wake_id FROM sprint_wake_messages WHERE sprint_id=?",
                    (self.sprint_id,),
                )
                if row[0] == replacement
            ],
        )
        evidence = json.loads(
            self.con.execute(
                "SELECT payload FROM sprint_events WHERE sprint_id=? "
                "AND event_type='lifecycle.reconciled' ORDER BY event_id DESC LIMIT 1",
                (self.sprint_id,),
            ).fetchone()[0]
        )
        self.assertEqual([replacement], evidence["requeued_wake_ids"])
        self.assertIn(
            replacement,
            [wake["wake_id"] for wake in evidence["pending_wakes"]],
        )
        service = sprint_message_delivery.SprintWakeDeliveryService(self.con)
        while True:
            outcome = service.deliver_once(
                "requeued-wake",
                lambda conversation, _prompt, _key: conversation,
            )
            self.assertIsNotNone(outcome)
            if outcome.wake_id == replacement:
                break
        self.assertEqual(1, outcome.attempt_number)

    def test_resume_requeue_coalesces_with_foreign_pending_receiver_wake(self):
        original_message = self.send("cross-sprint-recovery")
        original_wake_id = int(original_message.wake_id)
        store = sprint_domain.SprintLifecycleStore(
            self.con,
            interrupt_run=lambda _run_id: True,
            notify_commit=lambda: True,
        )
        for error in ("one", "two", "three"):
            store.record_wake_failure(original_wake_id, error)
        pause_notice = sprint_message_delivery.SprintWakeDeliveryService(
            self.con
        ).deliver_once(
            "cross-sprint-pause-notice",
            lambda conversation, _prompt, _key: conversation,
        )
        self.assertIsNotNone(pause_notice)
        self.assertEqual("delivered", pause_notice.state)

        feature_id = int(
            self.con.execute(
                "SELECT feature_id FROM sprints WHERE sprint_id=?",
                (self.sprint_id,),
            ).fetchone()[0]
        )
        foreign_sprint_id = int(
            self.con.execute(
                "INSERT INTO sprints "
                "(feature_id,originating_planner_shell_id,merge_grant_enabled) "
                "VALUES (?,3,1)",
                (feature_id,),
            ).lastrowid
        )
        foreign_developer_id = int(
            self.con.execute(
                "INSERT INTO sprint_participants "
                "(sprint_id,shell_id,role,harness) "
                "VALUES (?,1,'developer','codex')",
                (foreign_sprint_id,),
            ).lastrowid
        )
        self.con.commit()
        foreign_message = self.messages.send(
            foreign_sprint_id,
            to_participant_id=foreign_developer_id,
            message_kind="notification",
            body="foreign prepared sprint backlog",
            idempotency_key="foreign-prepared-backlog",
        )

        receipt = self.coordinator().resume(
            self.sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
            reason="reuse the receiver wake",
        )

        self.assertEqual((foreign_message.wake_id,), receipt.requeued_wake_ids)
        self.assertEqual(
            [(foreign_message.wake_id, foreign_sprint_id, "pending")],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT wake_id,sprint_id,state FROM sprint_wake_outbox "
                    "WHERE receiver_shell_id=1 AND state='pending'"
                )
            ],
        )
        self.assertEqual(
            [original_message.message_id, foreign_message.message_id],
            [
                int(row[0])
                for row in self.con.execute(
                    "SELECT message_id FROM sprint_wake_messages "
                    "WHERE wake_id=? ORDER BY message_id",
                    (foreign_message.wake_id,),
                )
            ],
        )
        lease = sprint_message_delivery.SprintWakeDeliveryService(self.con).claim_next(
            "foreign-recovery-worker"
        )
        self.assertIsNotNone(lease)
        self.assertEqual(foreign_message.wake_id, lease.wake_id)
        self.assertEqual(
            (original_message.message_id, foreign_message.message_id),
            lease.message_ids,
        )

    def test_failed_recovery_wake_is_bounded_and_leaves_manual_evidence(self):
        message = self.send("bounded-recovery-wake")
        original = int(message.wake_id)
        store = sprint_domain.SprintLifecycleStore(
            self.con,
            interrupt_run=lambda _run_id: True,
            notify_commit=lambda: True,
        )
        coordinator = self.coordinator()

        for error in ("original one", "original two", "original three"):
            store.record_wake_failure(original, error)
        first = coordinator.resume(
            self.sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
            reason="attempt one bounded recovery wake",
        )
        self.assertEqual(1, len(first.requeued_wake_ids))
        replacement = first.requeued_wake_ids[0]

        for error in ("fallback one", "fallback two", "fallback three"):
            store.record_wake_failure(replacement, error)
        second = coordinator.resume(
            self.sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
            reason="hand the durable failure evidence to FnB",
        )

        self.assertEqual((), second.requeued_wake_ids)
        self.assertEqual(
            (None, replacement, "failed", 3),
            tuple(
                self.con.execute(
                    "SELECT m.read_at,wm.wake_id,w.state,w.attempt_count "
                    "FROM wake_message m JOIN sprint_wake_messages wm "
                    "USING (message_id) JOIN sprint_wake_outbox w USING (wake_id) "
                    "WHERE m.message_id=?",
                    (message.message_id,),
                ).fetchone()
            ),
        )
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_wake_outbox WHERE sprint_id=? "
                "AND (idempotency_key LIKE 'sprint-recovery:%' "
                "OR idempotency_key LIKE '%:failed-wake:%')",
                (self.sprint_id,),
            ).fetchone()[0],
            "the bounded fallback does not become a recursive recovery loop",
        )

    def test_assignment_shell_busy_retries_then_pauses_without_provider_run(self):
        message = self.send(
            "busy-recovery-chain",
            kind="work_assignment",
            actionable=True,
        )
        original = int(message.wake_id)
        notifications = []
        store = sprint_domain.SprintLifecycleStore(
            self.con,
            interrupt_run=lambda _run_id: True,
            notify_commit=lambda: notifications.append("notified") or True,
        )
        current = original

        for attempt, backoff in zip(
            range(2, 6),
            sprint_domain.WAKE_CONTENTION_BACKOFF_SECONDS,
            strict=True,
        ):
            self.fail_wake_turn(
                current,
                error_code="SHELL_BUSY",
                slot_state="orphan" if attempt == 5 else "busy",
            )
            replacements = store.reconcile_unread_pickup(
                self.sprint_id,
                trigger=f"attempt-{attempt}",
            )
            self.assertEqual(1, len(replacements))
            current = replacements[0]
            wake = self.con.execute(
                "SELECT idempotency_key,"
                "CAST(strftime('%s',available_at) AS INTEGER)-"
                "CAST(strftime('%s',created_at) AS INTEGER) delay "
                "FROM sprint_wake_outbox WHERE wake_id=?",
                (current,),
            ).fetchone()
            self.assertEqual(
                f"sprint-recovery:{self.sprint_id}:busy:{original}:{attempt}",
                wake["idempotency_key"],
            )
            self.assertEqual(backoff, wake["delay"])
            event = json.loads(
                self.con.execute(
                    "SELECT payload FROM sprint_events WHERE sprint_id=? "
                    "AND event_type='wake.requeued' ORDER BY event_id DESC LIMIT 1",
                    (self.sprint_id,),
                ).fetchone()[0]
            )
            self.assertEqual(
                ("shell_busy", attempt, backoff, current),
                (
                    event["classification"],
                    event["attempt"],
                    event["backoff_seconds"],
                    event["replacement_wake_id"],
                ),
            )
            self.assertEqual(
                ("armed", current, 0, 0),
                tuple(
                    self.con.execute(
                        "SELECT s.lifecycle,wm.wake_id,"
                        "(SELECT COUNT(*) FROM sprint_reports report "
                        "WHERE report.sprint_id=s.sprint_id "
                        "AND report.report_kind='pause'),"
                        "(SELECT COUNT(*) FROM conversation_runs run "
                        "WHERE run.harness_session_after IS NOT NULL "
                        "OR run.runner_ref IS NOT NULL) "
                        "FROM sprints s JOIN sprint_wake_messages wm "
                        "ON wm.message_id=? WHERE s.sprint_id=?",
                        (message.message_id, self.sprint_id),
                    ).fetchone()
                ),
            )
            before_changes = self.con.total_changes
            self.assertEqual(
                (),
                store.reconcile_unread_pickup(
                    self.sprint_id,
                    trigger="duplicate-pulse",
                ),
            )
            self.assertEqual(before_changes, self.con.total_changes)
            self.con.execute(
                "UPDATE sprint_wake_outbox SET available_at="
                "datetime('now','-1 second') WHERE wake_id=?",
                (current,),
            )
            self.con.commit()
            if attempt == 2:
                store = sprint_domain.SprintLifecycleStore(
                    self.con,
                    interrupt_run=lambda _run_id: True,
                    notify_commit=lambda: notifications.append("notified") or True,
                )

        self.fail_wake_turn(
            current,
            error_code="SHELL_BUSY",
            slot_state="orphan",
        )
        self.assertEqual(
            (),
            store.reconcile_unread_pickup(
                self.sprint_id,
                trigger="attempt-5-exhausted",
            ),
        )
        self.assertEqual(["notified"], notifications)
        self.assertEqual(
            ("paused", 5, "DEV1", "orphan"),
            tuple(
                self.con.execute(
                    "SELECT s.lifecycle,"
                    "json_extract(r.body,'$.detail.attempts'),"
                    "json_extract(r.body,'$.detail.shell'),"
                    "json_extract(r.body,'$.detail.slot_state') "
                    "FROM sprints s JOIN sprint_reports r USING (sprint_id) "
                    "WHERE s.sprint_id=? AND r.report_kind='pause'",
                    (self.sprint_id,),
                ).fetchone()
            ),
        )
        notice = self.con.execute(
            "SELECT body FROM wake_message WHERE sprint_id IS NULL "
            "AND receiver_shell_id=3 "
            "AND idempotency_key LIKE 'sprint-pause:%'"
        ).fetchone()[0]
        self.assertIn("shell DEV1", notice)
        self.assertIn("5 attempts", notice)
        self.assertIn("last slot state: orphan", notice)
        self.assertEqual(
            4,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_events WHERE sprint_id=? "
                "AND event_type='wake.requeued' "
                "AND json_extract(payload,'$.classification')='shell_busy'",
                (self.sprint_id,),
            ).fetchone()[0],
        )

        receipt = self.coordinator().resume(
            self.sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
            reason="the orphaned slot was cleared",
        )

        self.assertTrue(receipt.changed)
        self.assertEqual(1, len(receipt.requeued_wake_ids))
        reset_wake = receipt.requeued_wake_ids[0]
        self.assertNotEqual(current, reset_wake)
        self.assertEqual(
            (
                "armed",
                "pending",
                f"sprint-resume:{self.sprint_id}:contention-episode:{current}",
                0,
                None,
                1,
                None,
                None,
            ),
            tuple(
                self.con.execute(
                    "SELECT s.lifecycle,w.state,w.idempotency_key,w.attempt_count,"
                    "w.last_error,w.available_at<=datetime('now'),"
                    "w.delivered_at,w.failed_at FROM sprints s "
                    "JOIN sprint_wake_outbox w ON w.sprint_id=s.sprint_id "
                    "WHERE s.sprint_id=? AND w.wake_id=?",
                    (self.sprint_id, reset_wake),
                ).fetchone()
            ),
        )
        self.assertEqual(
            ("delivered", 0, 1),
            tuple(
                self.con.execute(
                    "SELECT w.state,COUNT(wm.message_id),"
                    "(SELECT COUNT(*) FROM sprint_wake_attempts a "
                    "WHERE a.wake_id=w.wake_id) "
                    "FROM sprint_wake_outbox w LEFT JOIN sprint_wake_messages wm "
                    "USING (wake_id) WHERE w.wake_id=? GROUP BY w.wake_id",
                    (current,),
                ).fetchone()
            ),
        )
        reset = json.loads(
            self.con.execute(
                "SELECT payload FROM sprint_events WHERE sprint_id=? "
                "AND event_type='wake.requeued' "
                "AND json_extract(payload,'$.classification')="
                "'contention_episode_reset'",
                (self.sprint_id,),
            ).fetchone()[0]
        )
        self.assertEqual(
            (
                current,
                reset_wake,
                f"sprint-recovery:{self.sprint_id}:busy:{original}:5",
                f"sprint-resume:{self.sprint_id}:contention-episode:{current}",
            ),
            (
                reset["prior_wake_id"],
                reset["replacement_wake_id"],
                reset["prior_idempotency_key"],
                reset["idempotency_key"],
            ),
        )

        self.fail_wake_turn(reset_wake, error_code="SHELL_BUSY")
        fresh_retry = self.lifecycle.reconcile_unread_pickup(
            self.sprint_id,
            trigger="fresh-episode-attempt-2",
        )
        busy_retry = [
            wake_id
            for wake_id in fresh_retry
            if str(
                self.con.execute(
                    "SELECT idempotency_key FROM sprint_wake_outbox WHERE wake_id=?",
                    (wake_id,),
                ).fetchone()[0]
            ).startswith(f"sprint-recovery:{self.sprint_id}:busy:{reset_wake}:")
        ]
        self.assertEqual(1, len(busy_retry))
        self.assertEqual(
            (
                "armed",
                f"sprint-recovery:{self.sprint_id}:busy:{reset_wake}:2",
                4,
            ),
            (
                self.con.execute(
                    "SELECT lifecycle FROM sprints WHERE sprint_id=?",
                    (self.sprint_id,),
                ).fetchone()[0],
                self.con.execute(
                    "SELECT idempotency_key FROM sprint_wake_outbox WHERE wake_id=?",
                    (busy_retry[0],),
                ).fetchone()[0],
                self.con.execute(
                    "SELECT COUNT(*) FROM sprint_wake_outbox "
                    "WHERE sprint_id=? AND idempotency_key LIKE ?",
                    (
                        self.sprint_id,
                        f"sprint-recovery:{self.sprint_id}:busy:{original}:%",
                    ),
                ).fetchone()[0],
            ),
        )

    def test_resume_reports_unchanged_when_reconciliation_repauses(self):
        message = self.send("busy-chain-under-manual-pause")
        current = int(message.wake_id)
        for attempt in range(2, 6):
            self.fail_wake_turn(current, error_code="SHELL_BUSY")
            current = self.lifecycle.reconcile_unread_pickup(
                self.sprint_id,
                trigger=f"prepare-attempt-{attempt}",
            )[0]
            self.con.execute(
                "UPDATE sprint_wake_outbox SET available_at="
                "datetime('now','-1 second') WHERE wake_id=?",
                (current,),
            )
            self.con.commit()
        self.fail_wake_turn(current, error_code="SHELL_BUSY")
        coordinator = self.coordinator()
        coordinator.pause(
            self.sprint_id,
            sprint_domain.LifecycleActor("participant", 1),
            reason="manual pause before the contention pulse",
        )

        receipt = coordinator.resume(
            self.sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
        )

        self.assertFalse(receipt.changed)
        self.assertEqual((), receipt.requeued_wake_ids)
        self.assertEqual(
            ("paused", "wake_contention_exhausted"),
            tuple(
                self.con.execute(
                    "SELECT s.lifecycle,json_extract(r.body,'$.reason') "
                    "FROM sprints s JOIN sprint_reports r USING (sprint_id) "
                    "WHERE s.sprint_id=? ORDER BY r.report_id DESC LIMIT 1",
                    (self.sprint_id,),
                ).fetchone()
            ),
        )

    def test_read_message_stops_pending_shell_busy_chain(self):
        message = self.send("busy-chain-read")
        original = int(message.wake_id)
        self.fail_wake_turn(original, error_code="SHELL_BUSY")
        replacement = self.lifecycle.reconcile_unread_pickup(
            self.sprint_id,
            trigger="first-busy-retry",
        )[0]

        self.messages.mark_read(message.message_id, 1)
        self.con.execute(
            "UPDATE sprint_wake_outbox SET available_at="
            "datetime('now','-1 second') WHERE wake_id=?",
            (replacement,),
        )
        self.con.commit()

        self.assertIsNone(
            sprint_message_delivery.SprintWakeDeliveryService(self.con).claim_next(
                "read-message"
            )
        )
        self.assertEqual(
            (),
            self.lifecycle.reconcile_unread_pickup(
                self.sprint_id,
                trigger="read-message",
            ),
        )
        self.assertEqual(
            "armed",
            self.con.execute(
                "SELECT lifecycle FROM sprints WHERE sprint_id=?",
                (self.sprint_id,),
            ).fetchone()[0],
        )

    def test_shell_busy_chain_adopts_one_existing_pending_wake(self):
        first = self.send("busy-with-followup")
        original = int(first.wake_id)
        self.fail_wake_turn(original, error_code="SHELL_BUSY")
        followup = self.send("already-pending-followup")
        wake_count = self.con.execute(
            "SELECT COUNT(*) FROM sprint_wake_outbox WHERE sprint_id=?",
            (self.sprint_id,),
        ).fetchone()[0]

        replacements = self.lifecycle.reconcile_unread_pickup(
            self.sprint_id,
            trigger="coalesce-busy-recovery",
        )

        self.assertEqual((followup.wake_id,), replacements)
        self.assertEqual(
            wake_count,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_wake_outbox WHERE sprint_id=?",
                (self.sprint_id,),
            ).fetchone()[0],
        )
        self.assertEqual(
            (
                f"sprint-recovery:{self.sprint_id}:busy:{original}:2",
                15,
                2,
            ),
            tuple(
                self.con.execute(
                    "SELECT w.idempotency_key,"
                    "CAST(strftime('%s',w.available_at) AS INTEGER)-"
                    "CAST(strftime('%s',w.created_at) AS INTEGER),"
                    "COUNT(wm.message_id) FROM sprint_wake_outbox w "
                    "JOIN sprint_wake_messages wm USING (wake_id) "
                    "WHERE w.wake_id=? GROUP BY w.wake_id",
                    (followup.wake_id,),
                ).fetchone()
            ),
        )

    def test_busy_recovery_with_non_busy_terminal_enters_pickup_episode(self):
        message = self.send("busy-chain-non-busy-terminal")
        original = int(message.wake_id)
        self.fail_wake_turn(original, error_code="SHELL_BUSY")
        busy_recovery = self.lifecycle.reconcile_unread_pickup(
            self.sprint_id,
            trigger="busy-chain-entry",
        )[0]
        self.con.execute(
            "UPDATE sprint_wake_outbox SET available_at="
            "datetime('now','-1 second') WHERE wake_id=?",
            (busy_recovery,),
        )
        self.con.commit()
        self.fail_wake_turn(
            busy_recovery,
            error_code="HARNESS_ROUTE_MISMATCH",
        )

        replacements = self.lifecycle.reconcile_unread_pickup(
            self.sprint_id,
            trigger="busy-chain-non-busy-terminal",
        )

        self.assertEqual(1, len(replacements))
        pickup_recovery = replacements[0]
        self.assertEqual(
            (
                "armed",
                pickup_recovery,
                f"sprint-recovery:{self.sprint_id}:delivered-unread:{busy_recovery}",
                "pending",
                0,
            ),
            tuple(
                self.con.execute(
                    "SELECT s.lifecycle,wm.wake_id,w.idempotency_key,w.state,"
                    "(SELECT COUNT(*) FROM sprint_events e "
                    "WHERE e.sprint_id=s.sprint_id "
                    "AND e.event_type='wake.pickup_exhausted') "
                    "FROM sprints s JOIN sprint_wake_messages wm "
                    "ON wm.message_id=? JOIN sprint_wake_outbox w "
                    "ON w.wake_id=wm.wake_id WHERE s.sprint_id=?",
                    (message.message_id, self.sprint_id),
                ).fetchone()
            ),
        )

    def test_unknown_first_turn_pauses_without_replacement(self):
        message = self.send("unknown-first-turn")
        original = int(message.wake_id)
        self.fail_wake_turn(
            original,
            error_code="HARNESS_SESSION_DISCOVERY_FAILED",
            run_state="unknown",
        )
        unknown_conversation = str(
            self.con.execute(
                "SELECT target_conversation_id FROM sprint_wake_attempts "
                "WHERE wake_id=? ORDER BY attempt_number DESC LIMIT 1",
                (original,),
            ).fetchone()[0]
        )

        self.assertEqual(
            (),
            self.lifecycle.reconcile_unread_pickup(
                self.sprint_id,
                trigger="unknown-first-turn",
            ),
        )

        self.assertEqual(
            ("paused", None, original, 0),
            tuple(
                self.con.execute(
                    "SELECT s.lifecycle,m.read_at,wm.wake_id,"
                    "(SELECT COUNT(*) FROM sprint_wake_outbox recovery "
                    "WHERE recovery.sprint_id=s.sprint_id "
                    "AND recovery.idempotency_key LIKE 'sprint-recovery:%') "
                    "FROM sprints s JOIN wake_message m ON m.sprint_id=s.sprint_id "
                    "JOIN sprint_wake_messages wm USING (message_id) "
                    "WHERE s.sprint_id=? AND m.message_id=?",
                    (self.sprint_id, message.message_id),
                ).fetchone()
            ),
        )
        exhausted = json.loads(
            self.con.execute(
                "SELECT payload FROM sprint_events WHERE sprint_id=? "
                "AND event_type='wake.pickup_exhausted'",
                (self.sprint_id,),
            ).fetchone()[0]
        )
        self.assertEqual(
            {
                "attempt_count": 1,
                "conversation_id": unknown_conversation,
                "error_code": "HARNESS_SESSION_DISCOVERY_FAILED",
                "failure_class": "native_unknown",
                "message_id": message.message_id,
                "participant_id": self.developer_id,
                "role": "developer",
                "run_state": "unknown",
                "shell": "DEV1",
                "sprint_id": self.sprint_id,
                "wake_id": original,
                "work_unit_id": self.unit_id,
            },
            exhausted,
        )

    def test_monitor_reports_pause_with_empty_liveness_outcomes(self):
        message = self.send("monitor-unknown-first-turn")
        original = int(message.wake_id)
        self.fail_wake_turn(
            original,
            error_code="HARNESS_SESSION_DISCOVERY_FAILED",
            run_state="unknown",
        )

        response = server.sprint_monitor_response(self.con, self.sprint_id)

        self.assertEqual([], response["outcomes"])
        self.assertEqual("paused", response["health"]["condition"])
        self.assertIsNone(response["health"]["since"])
        self.assertEqual([], response["health"]["root_causes"])
        self.assertEqual(
            {
                "action": "paused",
                "requeued_wake_ids": [],
                "pause_reason": "wake_pickup_unknown",
            },
            response["pickup"],
        )
        self.assertEqual("missing", response["runtime"]["state"])
        self.assertEqual(None, response["runtime"]["beat_at"])
        self.assertEqual(5, response["runtime"]["interval_seconds"])

    def test_monitor_reconciles_pickup_once_before_projecting_health(self):
        order: list[str] = []
        original_reconcile = (
            sprint_domain.SprintLifecycleStore.reconcile_unread_pickup
        )

        def reconcile(store, sprint_id, *, trigger):
            order.append("pickup")
            return original_reconcile(store, sprint_id, trigger=trigger)

        def board(_projection, sprint_id):
            self.assertEqual(self.sprint_id, sprint_id)
            self.assertEqual(["pickup"], order)
            order.append("health")
            return {"health": {"condition": "progressing"}}

        with mock.patch.object(
            sprint_domain.SprintLifecycleStore,
            "reconcile_unread_pickup",
            autospec=True,
            side_effect=reconcile,
        ) as pickup, mock.patch.object(
            server.sprint_board.SprintBoardProjection,
            "board",
            autospec=True,
            side_effect=board,
        ):
            response = server.sprint_monitor_response(self.con, self.sprint_id)

        pickup.assert_called_once()
        self.assertEqual("monitor", pickup.call_args.kwargs["trigger"])
        self.assertEqual(["pickup", "health"], order)
        self.assertEqual([], response["outcomes"])
        self.assertEqual({"condition": "progressing"}, response["health"])

    def test_monitor_requeue_is_idempotent_and_never_delivers_a_second_copy(self):
        message = self.send("monitor-requeue")
        original = int(message.wake_id)
        self.fail_wake_turn(original, error_code="HARNESS_ROUTE_MISMATCH")
        self.con.execute(
            "INSERT INTO daemon_heartbeats (name,beat_at,interval_s) "
            "VALUES ('sprint-runtime','2000-01-01 00:00:00',5)"
        )
        self.con.commit()

        first = server.sprint_monitor_response(self.con, self.sprint_id)
        native_before = self.con.execute(
            "SELECT COUNT(*) FROM conversation_messages"
        ).fetchone()[0]
        wakes_before = self.con.execute(
            "SELECT COUNT(*) FROM sprint_wake_outbox"
        ).fetchone()[0]
        second = server.sprint_monitor_response(self.con, self.sprint_id)

        replacement = first["pickup"]["requeued_wake_ids"]
        self.assertEqual(1, len(replacement))
        self.assertNotEqual(original, replacement[0])
        self.assertEqual("requeued", first["pickup"]["action"])
        self.assertEqual([], first["outcomes"])
        self.assertEqual("infrastructure", first["health"]["condition"])
        self.assertEqual(
            ["runtime_stale"],
            [root["cause"] for root in first["health"]["root_causes"]],
        )
        self.assertEqual(
            {
                "state": "stale",
                "beat_at": "2000-01-01 00:00:00",
                "interval_seconds": 5,
            },
            first["runtime"],
        )
        self.assertEqual(
            {
                "action": "none",
                "requeued_wake_ids": [],
                "pause_reason": None,
            },
            second["pickup"],
        )
        self.assertEqual([], second["outcomes"])
        self.assertEqual(first["health"], second["health"])
        self.assertEqual(
            native_before,
            self.con.execute(
                "SELECT COUNT(*) FROM conversation_messages"
            ).fetchone()[0],
        )
        self.assertEqual(
            wakes_before,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_wake_outbox"
            ).fetchone()[0],
        )
        self.assertEqual(
            "pending",
            self.con.execute(
                "SELECT state FROM sprint_wake_outbox WHERE wake_id=?",
                (replacement[0],),
            ).fetchone()[0],
        )

    def test_non_busy_failed_turn_replaces_once_then_exhausts_atomically(self):
        wake_count = self.con.execute(
            "SELECT COUNT(*) FROM sprint_wake_outbox WHERE sprint_id=?",
            (self.sprint_id,),
        ).fetchone()[0]
        message = self.send("non-busy-turn")
        original = int(message.wake_id)
        self.fail_wake_turn(original, error_code="HARNESS_ROUTE_MISMATCH")
        replacement = self.lifecycle.reconcile_unread_pickup(
            self.sprint_id,
            trigger="non-busy",
        )[0]
        self.assertEqual(
            f"sprint-recovery:{self.sprint_id}:delivered-unread:{original}",
            self.con.execute(
                "SELECT idempotency_key FROM sprint_wake_outbox WHERE wake_id=?",
                (replacement,),
            ).fetchone()[0],
        )

        self.con.execute(
            "UPDATE sprint_wake_outbox SET available_at="
            "datetime('now','-1 second') WHERE wake_id=?",
            (replacement,),
        )
        self.con.commit()
        self.fail_wake_turn(replacement, error_code="HARNESS_ROUTE_MISMATCH")
        failed_conversation = str(
            self.con.execute(
                "SELECT target_conversation_id FROM sprint_wake_attempts "
                "WHERE wake_id=? ORDER BY attempt_number DESC LIMIT 1",
                (replacement,),
            ).fetchone()[0]
        )
        other_conversations = {
            str(row["conversation_id"]): str(row["state"])
            for row in self.con.execute(
                "SELECT conversation_id,state FROM conversations "
                "WHERE conversation_id<>?",
                (failed_conversation,),
            )
        }
        transcript_before = int(
            self.con.execute(
                "SELECT COUNT(*) FROM conversation_messages WHERE conversation_id=?",
                (failed_conversation,),
            ).fetchone()[0]
        )

        self.assertEqual(
            (),
            self.lifecycle.reconcile_unread_pickup(
                self.sprint_id,
                trigger="non-busy-recovery-failed",
            ),
        )
        self.assertEqual(
            wake_count + 2,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_wake_outbox WHERE sprint_id=?",
                (self.sprint_id,),
            ).fetchone()[0],
        )
        self.assertEqual(
            ("paused", None, replacement, "closed"),
            tuple(
                self.con.execute(
                    "SELECT s.lifecycle,m.read_at,wm.wake_id,c.state "
                    "FROM sprints s JOIN wake_message m ON m.sprint_id=s.sprint_id "
                    "JOIN sprint_wake_messages wm USING (message_id) "
                    "JOIN conversations c ON c.conversation_id=? "
                    "WHERE s.sprint_id=? AND m.message_id=?",
                    (failed_conversation, self.sprint_id, message.message_id),
                ).fetchone()
            ),
        )
        self.assertEqual(
            transcript_before,
            self.con.execute(
                "SELECT COUNT(*) FROM conversation_messages WHERE conversation_id=?",
                (failed_conversation,),
            ).fetchone()[0],
        )
        self.assertEqual(
            other_conversations,
            {
                str(row["conversation_id"]): str(row["state"])
                for row in self.con.execute(
                    "SELECT conversation_id,state FROM conversations "
                    "WHERE conversation_id<>?",
                    (failed_conversation,),
                )
            },
        )
        closed = json.loads(
            self.con.execute(
                "SELECT payload FROM conversation_events "
                "WHERE conversation_id=? AND event_type='conversation.closed' "
                "ORDER BY sequence DESC LIMIT 1",
                (failed_conversation,),
            ).fetchone()[0]
        )
        self.assertEqual(
            {
                "reason": "wake pickup exhausted",
                "state": "closed",
                "wake_id": replacement,
            },
            closed,
        )
        report = json.loads(
            self.con.execute(
                "SELECT body FROM sprint_reports WHERE sprint_id=? "
                "AND report_kind='pause' ORDER BY report_id DESC LIMIT 1",
                (self.sprint_id,),
            ).fetchone()[0]
        )
        self.assertEqual("wake_pickup_failed", report["reason"])
        self.assertEqual(2, report["detail"]["attempt_count"])
        self.assertEqual("native_failed", report["detail"]["failure_class"])
        self.assertEqual("HARNESS_ROUTE_MISMATCH", report["detail"]["error_code"])
        self.assertIn(
            "already has a live CLI session", report["detail"]["error_detail"]
        )
        notice = self.con.execute(
            "SELECT body FROM wake_message WHERE sprint_id IS NULL "
            "AND receiver_shell_id=3 AND idempotency_key LIKE 'sprint-pause:%'"
        ).fetchall()
        self.assertEqual(1, len(notice))
        self.assertIn("use an authorized resume", notice[0]["body"])

        before_replay = self.con.total_changes
        self.assertEqual(
            (),
            self.lifecycle.reconcile_unread_pickup(
                self.sprint_id,
                trigger="post-pause-replay",
            ),
        )
        self.assertEqual(before_replay, self.con.total_changes)

    def test_succeeded_unread_turn_replaces_once_then_pauses_without_closing_chat(self):
        message = self.send("succeeded-unread")
        original = int(message.wake_id)
        self.deliver_wake_with_turn(original, terminal=True)
        replacement = self.lifecycle.reconcile_unread_pickup(
            self.sprint_id,
            trigger="succeeded-unread-first",
        )[0]
        self.deliver_wake_with_turn(replacement, terminal=True)
        conversation_id = str(
            self.con.execute(
                "SELECT target_conversation_id FROM sprint_wake_attempts "
                "WHERE wake_id=? ORDER BY attempt_number DESC LIMIT 1",
                (replacement,),
            ).fetchone()[0]
        )

        self.assertEqual(
            (),
            self.lifecycle.reconcile_unread_pickup(
                self.sprint_id,
                trigger="succeeded-unread-second",
            ),
        )
        self.assertEqual(
            ("paused", None, replacement, "waiting"),
            tuple(
                self.con.execute(
                    "SELECT s.lifecycle,m.read_at,wm.wake_id,c.state "
                    "FROM sprints s JOIN wake_message m ON m.sprint_id=s.sprint_id "
                    "JOIN sprint_wake_messages wm USING (message_id) "
                    "JOIN conversations c ON c.conversation_id=? "
                    "WHERE s.sprint_id=? AND m.message_id=?",
                    (conversation_id, self.sprint_id, message.message_id),
                ).fetchone()
            ),
        )
        event = json.loads(
            self.con.execute(
                "SELECT payload FROM sprint_events WHERE sprint_id=? "
                "AND event_type='wake.pickup_exhausted'",
                (self.sprint_id,),
            ).fetchone()[0]
        )
        self.assertEqual("WAKE_PICKUP_UNREAD", event["error_code"])
        self.assertEqual("terminal_unread", event["failure_class"])
        self.assertEqual("succeeded", event["run_state"])
        self.assertEqual(2, event["attempt_count"])

    def test_missing_terminal_evidence_pauses_as_integrity_anomaly(self):
        message = self.send("missing-evidence")
        wake_id = int(message.wake_id)
        self.deliver_wake_with_turn(wake_id, terminal=True)
        self.con.execute(
            "UPDATE sprint_wake_attempts SET target_conversation_id=NULL "
            "WHERE wake_id=?",
            (wake_id,),
        )
        self.con.commit()

        self.assertEqual(
            (),
            self.lifecycle.reconcile_unread_pickup(
                self.sprint_id,
                trigger="missing-evidence",
            ),
        )
        event = json.loads(
            self.con.execute(
                "SELECT payload FROM sprint_events WHERE sprint_id=? "
                "AND event_type='wake.pickup_exhausted'",
                (self.sprint_id,),
            ).fetchone()[0]
        )
        self.assertEqual("WAKE_PICKUP_EVIDENCE_INVALID", event["error_code"])
        self.assertEqual("evidence_invalid", event["failure_class"])
        self.assertEqual(
            "wake_pickup_evidence_invalid",
            json.loads(
                self.con.execute(
                    "SELECT body FROM sprint_reports WHERE sprint_id=? "
                    "AND report_kind='pause'",
                    (self.sprint_id,),
                ).fetchone()[0]
            )["reason"],
        )

    def test_resume_requeues_linked_reply_interrupted_by_required_pause(self):
        blocker = self.messages.relay(
            self.sprint_id,
            from_shell_id=1,
            to_shortname="PLN1",
            body="Released work must be recalled before replanning.",
            idempotency_key="participant-send:pause-blocker",
            intent="blocker",
            requires_reply=True,
            work_unit_id=self.unit_id,
        )
        self.deliver_wake_with_turn(int(blocker.wake_id), terminal=True)
        self.assertIsNone(self.messages.mark_read(blocker.message_id, 3))
        reply = self.messages.relay(
            self.sprint_id,
            from_shell_id=3,
            to_shortname="DEV1",
            body="Acknowledged; pausing before the recall and replan.",
            idempotency_key="participant-send:pause-acknowledgement",
            intent="information",
            reply_to_message_id=blocker.message_id,
        )
        _, wake_key = self.deliver_wake_with_turn(
            int(reply.wake_id),
            terminal=False,
        )
        trigger_message_id = int(
            self.con.execute(
                "SELECT message_id FROM conversation_messages "
                "WHERE idempotency_key=?",
                (wake_key,),
            ).fetchone()[0]
        )
        self.con.execute(
            "UPDATE conversation_messages SET state='running' WHERE message_id=?",
            (trigger_message_id,),
        )
        run_id = int(
            self.con.execute(
                "INSERT INTO conversation_runs "
                "(conversation_id,shell_id,trigger_message_id,state,lease_owner,"
                "lease_expires_at,started_at,heartbeat_at) "
                "VALUES (?,1,?,'running','test-broker','2999-01-01 00:00:00',"
                "'2026-08-01 00:00:00','2026-08-01 00:00:00')",
                (self.developer_conversation_id, trigger_message_id),
            ).lastrowid
        )
        self.con.execute(
            "UPDATE conversations SET state='running' WHERE conversation_id=?",
            (self.developer_conversation_id,),
        )
        self.con.commit()

        pause = self.coordinator().pause(
            self.sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
            reason="required restructuring pause",
        )
        self.assertEqual((run_id,), pause.interrupt_run_ids)
        self.con.execute(
            "UPDATE conversation_runs SET state='cancelled',ended_at=datetime('now') "
            "WHERE run_id=?",
            (run_id,),
        )
        self.con.execute(
            "UPDATE conversation_messages SET state='cancelled',"
            "completed_at=datetime('now') WHERE message_id=?",
            (trigger_message_id,),
        )
        self.con.execute(
            "UPDATE conversations SET state='idle' WHERE conversation_id=?",
            (self.developer_conversation_id,),
        )
        self.con.commit()

        resumed = self.coordinator().resume(
            self.sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
            reason="replan complete",
        )

        self.assertTrue(resumed.changed)
        self.assertEqual(1, len(resumed.requeued_wake_ids))
        replacement_wake_id = resumed.requeued_wake_ids[0]
        self.assertEqual(
            (
                "armed",
                blocker.message_id,
                reply.message_id,
                blocker.message_id,
                replacement_wake_id,
                "pending",
                None,
            ),
            tuple(
                self.con.execute(
                    "SELECT s.lifecycle,original.message_id,reply.message_id,"
                    "reply.reply_to_message_id,wm.wake_id,w.state,reply.delivered_at "
                    "FROM sprints s JOIN wake_message original "
                    "ON original.message_id=? JOIN wake_message reply "
                    "ON reply.message_id=? JOIN sprint_wake_messages wm "
                    "ON wm.message_id=reply.message_id JOIN sprint_wake_outbox w "
                    "ON w.wake_id=wm.wake_id WHERE s.sprint_id=?",
                    (blocker.message_id, reply.message_id, self.sprint_id),
                ).fetchone()
            ),
        )
        self.assertEqual(
            [(reply.wake_id, replacement_wake_id, reply.message_id)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT prior_wake_id,replacement_wake_id,message_id "
                    "FROM sprint_wake_recovery_messages WHERE sprint_id=?",
                    (self.sprint_id,),
                )
            ],
        )
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_events WHERE sprint_id=? "
                "AND event_type='wake.pickup_exhausted'",
                (self.sprint_id,),
            ).fetchone()[0],
        )

    def test_resume_resets_exhausted_pickup_as_one_fresh_bounded_episode(self):
        first = self.send(
            "resume-exhausted-first",
            declared_type="re-enter",
        )
        second = self.send(
            "resume-exhausted-second",
            declared_type="new",
        )
        handled = self.send(
            "resume-exhausted-handled",
            declared_type="re-enter",
        )
        terminal = int(first.wake_id)
        self.assertEqual(terminal, second.wake_id)
        self.assertEqual(terminal, handled.wake_id)
        self.fail_wake_turn(terminal, error_code="HARNESS_ROUTE_MISMATCH")
        exhausted = self.lifecycle.reconcile_unread_pickup(
            self.sprint_id,
            trigger="resume-exhausted-first-failure",
        )[0]
        self.con.execute(
            "UPDATE sprint_wake_outbox SET available_at="
            "datetime('now','-1 second') WHERE wake_id=?",
            (exhausted,),
        )
        self.con.commit()
        self.fail_wake_turn(exhausted, error_code="HARNESS_ROUTE_MISMATCH")
        self.assertEqual(
            (),
            self.lifecycle.reconcile_unread_pickup(
                self.sprint_id,
                trigger="resume-exhausted-second-failure",
            ),
        )
        self.assertEqual(
            "paused",
            self.con.execute(
                "SELECT lifecycle FROM sprints WHERE sprint_id=?",
                (self.sprint_id,),
            ).fetchone()[0],
        )
        self.assertIsNone(self.messages.mark_read(handled.message_id, 1))
        old_evidence = tuple(
            self.con.execute(
                "SELECT w.state,w.attempt_count,w.idempotency_key,"
                "(SELECT COUNT(*) FROM sprint_wake_attempts a "
                "WHERE a.wake_id=w.wake_id) "
                "FROM sprint_wake_outbox w WHERE w.wake_id=?",
                (exhausted,),
            ).fetchone()
        )
        exhausted_conversation = str(
            self.con.execute(
                "SELECT target_conversation_id FROM sprint_wake_attempts "
                "WHERE wake_id=? ORDER BY attempt_number DESC LIMIT 1",
                (exhausted,),
            ).fetchone()[0]
        )
        old_conversation_evidence = tuple(
            self.con.execute(
                "SELECT c.state,"
                "(SELECT COUNT(*) FROM conversation_runs r "
                "WHERE r.conversation_id=c.conversation_id),"
                "(SELECT COUNT(*) FROM conversation_messages m "
                "WHERE m.conversation_id=c.conversation_id),"
                "(SELECT COUNT(*) FROM conversation_events e "
                "WHERE e.conversation_id=c.conversation_id) "
                "FROM conversations c WHERE c.conversation_id=?",
                (exhausted_conversation,),
            ).fetchone()
        )
        wake_count_before_resume = int(
            self.con.execute("SELECT COUNT(*) FROM sprint_wake_outbox").fetchone()[0]
        )

        receipt = self.coordinator().resume(
            self.sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
            reason="the participant route was repaired",
        )

        self.assertTrue(receipt.changed)
        self.assertEqual(1, len(receipt.requeued_wake_ids))
        reset_wake = receipt.requeued_wake_ids[0]
        self.assertEqual(
            wake_count_before_resume + 1,
            self.con.execute("SELECT COUNT(*) FROM sprint_wake_outbox").fetchone()[0],
        )
        self.assertEqual(
            (
                "pending",
                f"sprint-resume:{self.sprint_id}:pickup-episode:{exhausted}",
                0,
                None,
                None,
            ),
            tuple(
                self.con.execute(
                    "SELECT state,idempotency_key,attempt_count,delivered_at,failed_at "
                    "FROM sprint_wake_outbox WHERE wake_id=?",
                    (reset_wake,),
                ).fetchone()
            ),
        )
        self.assertEqual(
            [
                (first.message_id, reset_wake, 0, 0),
                (second.message_id, reset_wake, 0, 0),
                (handled.message_id, exhausted, 1, 1),
            ],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT m.message_id,wm.wake_id,m.read_at IS NOT NULL,"
                    "m.delivered_at IS NOT NULL FROM wake_message m "
                    "JOIN sprint_wake_messages wm USING (message_id) "
                    "WHERE m.message_id IN (?,?,?) ORDER BY m.message_id",
                    (first.message_id, second.message_id, handled.message_id),
                )
            ],
        )
        self.assertEqual(
            old_evidence,
            tuple(
                self.con.execute(
                    "SELECT w.state,w.attempt_count,w.idempotency_key,"
                    "(SELECT COUNT(*) FROM sprint_wake_attempts a "
                    "WHERE a.wake_id=w.wake_id) "
                    "FROM sprint_wake_outbox w WHERE w.wake_id=?",
                    (exhausted,),
                ).fetchone()
            ),
        )
        self.assertEqual(
            old_conversation_evidence,
            tuple(
                self.con.execute(
                    "SELECT c.state,"
                    "(SELECT COUNT(*) FROM conversation_runs r "
                    "WHERE r.conversation_id=c.conversation_id),"
                    "(SELECT COUNT(*) FROM conversation_messages m "
                    "WHERE m.conversation_id=c.conversation_id),"
                    "(SELECT COUNT(*) FROM conversation_events e "
                    "WHERE e.conversation_id=c.conversation_id) "
                    "FROM conversations c WHERE c.conversation_id=?",
                    (exhausted_conversation,),
                ).fetchone()
            ),
        )
        reset_events = [
            json.loads(row[0])
            for row in self.con.execute(
                "SELECT payload FROM sprint_events WHERE sprint_id=? "
                "AND event_type='wake.requeued' "
                "AND json_extract(payload,'$.classification')="
                "'pickup_episode_reset'",
                (self.sprint_id,),
            )
        ]
        self.assertEqual(1, len(reset_events))
        self.assertEqual(
            (exhausted, reset_wake, "resume"),
            (
                reset_events[0]["prior_wake_id"],
                reset_events[0]["replacement_wake_id"],
                reset_events[0]["trigger"],
            ),
        )
        reconciliation = json.loads(
            self.con.execute(
                "SELECT payload FROM sprint_events WHERE sprint_id=? "
                "AND event_type='lifecycle.reconciled' "
                "ORDER BY event_id DESC LIMIT 1",
                (self.sprint_id,),
            ).fetchone()[0]
        )
        self.assertEqual([reset_wake], reconciliation["requeued_wake_ids"])
        self.assertEqual(
            [first.message_id, second.message_id],
            reconciliation["unread_message_ids"],
        )

        wake_count = int(
            self.con.execute("SELECT COUNT(*) FROM sprint_wake_outbox").fetchone()[0]
        )
        replay = self.coordinator().resume(
            self.sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
            reason="duplicate authorized resume",
        )
        self.assertFalse(replay.changed)
        self.assertEqual((), replay.requeued_wake_ids)
        self.assertEqual(
            (),
            self.lifecycle.reconcile_unread_pickup(
                self.sprint_id,
                trigger="duplicate-pulse-before-delivery",
            ),
        )
        self.assertEqual(
            wake_count,
            self.con.execute("SELECT COUNT(*) FROM sprint_wake_outbox").fetchone()[0],
        )

        idle_conversation = self._activate_unregistered_chat(
            "resume-exhausted-existing-idle"
        )
        self.fail_wake_turn(reset_wake, error_code="HARNESS_ROUTE_MISMATCH")
        reset_conversation = str(
            self.con.execute(
                "SELECT target_conversation_id FROM sprint_wake_attempts "
                "WHERE wake_id=? ORDER BY attempt_number DESC LIMIT 1",
                (reset_wake,),
            ).fetchone()[0]
        )
        self.assertNotEqual(
            idle_conversation,
            reset_conversation,
            "the coalesced declared New message must rotate at delivery",
        )
        next_episode = self.lifecycle.reconcile_unread_pickup(
            self.sprint_id,
            trigger="resumed-episode-first-failure",
        )
        self.assertEqual(1, len(next_episode))
        bounded_recovery = next_episode[0]
        self.assertEqual(
            f"sprint-recovery:{self.sprint_id}:delivered-unread:{reset_wake}",
            self.con.execute(
                "SELECT idempotency_key FROM sprint_wake_outbox WHERE wake_id=?",
                (bounded_recovery,),
            ).fetchone()[0],
        )
        self.con.execute(
            "UPDATE sprint_wake_outbox SET available_at="
            "datetime('now','-1 second') WHERE wake_id=?",
            (bounded_recovery,),
        )
        self.con.commit()
        self.fail_wake_turn(
            bounded_recovery,
            error_code="HARNESS_ROUTE_MISMATCH",
        )
        self.assertEqual(
            (),
            self.lifecycle.reconcile_unread_pickup(
                self.sprint_id,
                trigger="resumed-episode-second-failure",
            ),
        )
        self.assertEqual(
            ("paused", bounded_recovery, 2, 1),
            tuple(
                self.con.execute(
                    "SELECT s.lifecycle,"
                    "json_extract(r.body,'$.detail.wake_id'),"
                    "json_extract(r.body,'$.detail.attempt_count'),"
                    "(SELECT COUNT(*) FROM sprint_wake_outbox recovery "
                    "WHERE recovery.idempotency_key=?) "
                    "FROM sprints s JOIN sprint_reports r USING (sprint_id) "
                    "WHERE s.sprint_id=? AND r.report_kind='pause' "
                    "ORDER BY r.report_id DESC LIMIT 1",
                    (
                        (
                            f"sprint-recovery:{self.sprint_id}:"
                            f"delivered-unread:{reset_wake}"
                        ),
                        self.sprint_id,
                    ),
                ).fetchone()
            ),
        )

    def test_resume_adopts_one_pending_receiver_wake_for_pickup_reset(self):
        message = self.send("resume-adopt-exhausted")
        original = int(message.wake_id)
        self.fail_wake_turn(original, error_code="HARNESS_ROUTE_MISMATCH")
        exhausted = self.lifecycle.reconcile_unread_pickup(
            self.sprint_id,
            trigger="resume-adopt-first-failure",
        )[0]
        self.con.execute(
            "UPDATE sprint_wake_outbox SET available_at="
            "datetime('now','-1 second') WHERE wake_id=?",
            (exhausted,),
        )
        self.con.commit()
        self.fail_wake_turn(exhausted, error_code="HARNESS_ROUTE_MISMATCH")
        self.assertEqual(
            (),
            self.lifecycle.reconcile_unread_pickup(
                self.sprint_id,
                trigger="resume-adopt-second-failure",
            ),
        )
        followup = self.send("resume-adopt-pending")
        adopted_wake = int(followup.wake_id)
        wake_count = int(
            self.con.execute("SELECT COUNT(*) FROM sprint_wake_outbox").fetchone()[0]
        )

        receipt = self.coordinator().resume(
            self.sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
            reason="adopt the paused receiver backlog",
        )

        self.assertEqual((adopted_wake,), receipt.requeued_wake_ids)
        self.assertEqual(
            wake_count,
            self.con.execute("SELECT COUNT(*) FROM sprint_wake_outbox").fetchone()[0],
        )
        self.assertEqual(
            (
                f"sprint-resume:{self.sprint_id}:pickup-episode:{exhausted}",
                "pending",
                2,
            ),
            tuple(
                self.con.execute(
                    "SELECT w.idempotency_key,w.state,COUNT(wm.message_id) "
                    "FROM sprint_wake_outbox w JOIN sprint_wake_messages wm "
                    "USING (wake_id) WHERE w.wake_id=? GROUP BY w.wake_id",
                    (adopted_wake,),
                ).fetchone()
            ),
        )
        self.assertEqual(
            [(message.message_id, adopted_wake), (followup.message_id, adopted_wake)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT message_id,wake_id FROM sprint_wake_messages "
                    "WHERE message_id IN (?,?) ORDER BY message_id",
                    (message.message_id, followup.message_id),
                )
            ],
        )

    def test_resume_never_replays_delivered_unread_assignment(self):
        assignment = self.send(
            "pickup-assignment",
            kind="work_assignment",
            actionable=True,
        )
        old_wake = int(assignment.wake_id)
        prompt, _ = self.deliver_wake_with_turn(old_wake, terminal=True)
        self.assertEqual(
            sprint_message_delivery.wake_prompt(self.sprint_id, "developer")
            + f"\n\n## wake_message #{assignment.message_id} "
            "(declared Re-Enter)\n\nbody for pickup-assignment",
            prompt,
        )
        self.lifecycle.pause(
            self.sprint_id,
            sprint_domain.LifecycleActor("participant", 1),
            reason="exercise delivered-unread recovery",
        )

        receipt = self.coordinator().resume(
            self.sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
            reason="recover the unread assignment",
        )

        self.assertFalse(receipt.changed)
        self.assertEqual((), receipt.requeued_wake_ids)
        self.assertEqual(
            (old_wake, "delivered", 1),
            tuple(
                self.con.execute(
                    "SELECT wm.wake_id,w.state,w.attempt_count "
                    "FROM sprint_wake_messages wm "
                    "JOIN sprint_wake_outbox w USING (wake_id) "
                    "WHERE wm.message_id=?",
                    (assignment.message_id,),
                ).fetchone()
            ),
        )
        event = json.loads(
            self.con.execute(
                "SELECT payload FROM sprint_events WHERE sprint_id=? "
                "AND event_type='wake.pickup_exhausted' "
                "ORDER BY event_id DESC LIMIT 1",
                (self.sprint_id,),
            ).fetchone()[0]
        )
        self.assertEqual(assignment.message_id, event["message_id"])
        self.assertEqual(self.unit_id, event["work_unit_id"])
        self.assertEqual(old_wake, event["wake_id"])
        self.assertEqual("succeeded", event["run_state"])
        self.assertEqual("ASSIGNMENT_TERMINAL_UNREAD", event["error_code"])
        self.assertEqual("terminal_unread", event["failure_class"])
        self.assertEqual(1, event["attempt_count"])
        report = json.loads(
            self.con.execute(
                "SELECT body FROM sprint_reports WHERE sprint_id=? "
                "AND report_kind='pause' ORDER BY report_id DESC LIMIT 1",
                (self.sprint_id,),
            ).fetchone()[0]
        )
        self.assertEqual("assignment_execution_already_started", report["reason"])
        self.assertEqual(old_wake, report["detail"]["wake_id"])

        self.assertEqual(
            (),
            self.lifecycle.reconcile_unread_pickup(
                self.sprint_id,
                trigger="monitor",
            ),
        )
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_wake_outbox "
                "WHERE idempotency_key LIKE 'sprint-recovery:%'"
            ).fetchone()[0],
        )

    def test_resume_never_replays_interrupted_unread_assignment(self):
        assignment = self.send(
            "interrupted-assignment",
            kind="work_assignment",
            actionable=True,
        )
        wake_id = int(assignment.wake_id)
        _prompt, wake_key = self.deliver_wake_with_turn(wake_id, terminal=False)
        trigger_message_id = int(
            self.con.execute(
                "SELECT message_id FROM conversation_messages "
                "WHERE idempotency_key=?",
                (wake_key,),
            ).fetchone()[0]
        )
        self.con.execute(
            "UPDATE conversation_messages SET state='running' WHERE message_id=?",
            (trigger_message_id,),
        )
        run_id = int(
            self.con.execute(
                "INSERT INTO conversation_runs "
                "(conversation_id,shell_id,trigger_message_id,state,lease_owner,"
                "lease_expires_at,started_at,heartbeat_at) "
                "VALUES (?,1,?,'running','test-broker','2999-01-01 00:00:00',"
                "'2026-08-01 00:00:00','2026-08-01 00:00:00')",
                (self.developer_conversation_id, trigger_message_id),
            ).lastrowid
        )
        self.con.execute(
            "UPDATE conversations SET state='running' WHERE conversation_id=?",
            (self.developer_conversation_id,),
        )
        self.con.commit()

        pause = self.coordinator().pause(
            self.sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
            reason="interrupt the assignment turn",
        )
        self.assertEqual((run_id,), pause.interrupt_run_ids)
        self.con.execute(
            "UPDATE conversation_runs SET state='cancelled',ended_at=datetime('now') "
            "WHERE run_id=?",
            (run_id,),
        )
        self.con.execute(
            "UPDATE conversation_messages SET state='cancelled',"
            "completed_at=datetime('now') WHERE message_id=?",
            (trigger_message_id,),
        )
        self.con.execute(
            "UPDATE conversations SET state='idle' WHERE conversation_id=?",
            (self.developer_conversation_id,),
        )
        self.con.commit()

        receipt = self.coordinator().resume(
            self.sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
            reason="observe the interrupted assignment",
        )

        self.assertFalse(receipt.changed)
        self.assertEqual((), receipt.requeued_wake_ids)
        self.assertEqual(
            (wake_id, "delivered", trigger_message_id, run_id, "cancelled"),
            tuple(
                self.con.execute(
                    "SELECT wm.wake_id,w.state,m.message_id,r.run_id,r.state "
                    "FROM sprint_wake_messages wm "
                    "JOIN sprint_wake_outbox w USING (wake_id) "
                    "JOIN conversation_messages m ON m.idempotency_key=w.idempotency_key "
                    "JOIN conversation_runs r ON r.trigger_message_id=m.message_id "
                    "WHERE wm.message_id=?",
                    (assignment.message_id,),
                ).fetchone()
            ),
        )
        event = json.loads(
            self.con.execute(
                "SELECT payload FROM sprint_events WHERE sprint_id=? "
                "AND event_type='wake.pickup_exhausted' "
                "ORDER BY event_id DESC LIMIT 1",
                (self.sprint_id,),
            ).fetchone()[0]
        )
        self.assertEqual(wake_id, event["wake_id"])
        self.assertEqual("cancelled", event["run_state"])
        self.assertEqual("native_interrupted", event["failure_class"])
        self.assertEqual(
            "ASSIGNMENT_EXECUTION_INTERRUPTED", event["error_code"]
        )
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_wake_outbox "
                "WHERE idempotency_key LIKE 'sprint-recovery:%'"
            ).fetchone()[0],
        )

    def test_resume_repairs_delivered_unread_participant_relay(self):
        relay = self.messages.relay(
            self.sprint_id,
            from_shell_id=3,
            to_shortname="DEV1",
            body="Which compatibility fixture should I use?",
            idempotency_key="pickup-relay",
        )
        old_wake = relay.wake_id
        self.deliver_wake_with_turn(old_wake, terminal=True)
        before = self.con.execute(
            "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
            (self.unit_id,),
        ).fetchone()[0]
        self.lifecycle.pause(
            self.sprint_id,
            sprint_domain.LifecycleActor("participant", 1),
            reason="recover a participant question",
        )

        receipt = self.coordinator().resume(
            self.sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
            reason="restore message pickup",
        )

        replacement = receipt.requeued_wake_ids[0]
        captured: list[tuple[int, str]] = []
        delivery = sprint_message_delivery.SprintWakeDeliveryService(self.con)
        outcome = None
        while outcome is None or outcome.wake_id != replacement:
            actual_prompt: list[str] = []
            outcome = delivery.deliver_once(
                "relay-recovery",
                lambda _conversation, prompt, _key, sink=actual_prompt: (
                    sink.append(prompt) or "native"
                ),
            )
            self.assertIsNotNone(outcome)
            captured.append((outcome.wake_id, actual_prompt[0]))
        self.assertEqual(replacement, outcome.wake_id)
        self.assertEqual(
            sprint_message_delivery.wake_prompt(self.sprint_id, "developer")
            + f"\n\n## wake_message #{relay.message_id} "
            "(declared Re-Enter)\n\nWhich compatibility fixture should I use?",
            captured[-1][1],
        )
        self.assertEqual(
            before,
            self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                (self.unit_id,),
            ).fetchone()[0],
        )

    def test_resume_does_not_replace_an_adequate_turn_or_deliverable_wake(self):
        queued_turn = self.send("pickup-queued-turn")
        _, queued_key = self.deliver_wake_with_turn(
            int(queued_turn.wake_id),
            terminal=False,
        )
        self.lifecycle.pause(
            self.sprint_id,
            sprint_domain.LifecycleActor("participant", 1),
            reason="test adequate queued turn",
        )
        first = self.coordinator().resume(
            self.sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
        )
        self.assertEqual((), first.requeued_wake_ids)

        self.messages.mark_read(queued_turn.message_id, 1)
        self.con.execute(
            "UPDATE conversation_messages SET state='running' WHERE idempotency_key=?",
            (queued_key,),
        )
        self.con.execute(
            "UPDATE conversation_messages SET state='completed',"
            "completed_at=datetime('now') WHERE idempotency_key=?",
            (queued_key,),
        )
        for state in ("running", "waiting"):
            self.con.execute(
                "UPDATE conversations SET state=? WHERE conversation_id=?",
                (state, self.developer_conversation_id),
            )
        self.con.commit()
        terminal = self.send("pickup-existing-wake")
        self.deliver_wake_with_turn(int(terminal.wake_id), terminal=True)
        deliverable = self.send("pickup-already-pending")
        wake_count = self.con.execute(
            "SELECT COUNT(*) FROM sprint_wake_outbox WHERE participant_id=?",
            (self.developer_id,),
        ).fetchone()[0]
        self.lifecycle.pause(
            self.sprint_id,
            sprint_domain.LifecycleActor("participant", 1),
            reason="test adequate pending wake",
        )
        second = self.coordinator().resume(
            self.sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
        )
        self.assertIn(deliverable.wake_id, second.requeued_wake_ids)
        self.assertEqual(
            wake_count,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_wake_outbox WHERE participant_id=?",
                (self.developer_id,),
            ).fetchone()[0],
            "resume reuses existing wakes instead of adding a pickup fallback",
        )
        self.assertEqual(
            [(deliverable.wake_id,), (deliverable.wake_id,)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT wake_id FROM sprint_wake_messages "
                    "WHERE message_id IN (?,?) ORDER BY message_id",
                    (terminal.message_id, deliverable.message_id),
                )
            ],
        )

    def test_error_conversation_with_no_run_pauses_as_invalid_evidence(self):
        message = self.send("pickup-error-conversation")
        old_wake = int(message.wake_id)
        self.deliver_wake_with_turn(old_wake, terminal=False)
        self.con.execute(
            "UPDATE conversations SET state='running' WHERE conversation_id=?",
            (self.developer_conversation_id,),
        )
        self.con.execute(
            "UPDATE conversations SET state='error' WHERE conversation_id=?",
            (self.developer_conversation_id,),
        )
        self.con.commit()
        self.lifecycle.pause(
            self.sprint_id,
            sprint_domain.LifecycleActor("participant", 1),
            reason="queued turn cannot run from an error conversation",
        )

        receipt = self.coordinator().resume(
            self.sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
        )

        self.assertEqual((), receipt.requeued_wake_ids)
        self.assertEqual(
            ("paused", old_wake, "wake_pickup_evidence_invalid"),
            tuple(
                self.con.execute(
                    "SELECT s.lifecycle,wm.wake_id,"
                    "json_extract(r.body,'$.reason') "
                    "FROM sprints s JOIN sprint_reports r USING (sprint_id) "
                    "JOIN sprint_wake_messages wm ON wm.message_id=? "
                    "WHERE s.sprint_id=? ORDER BY r.report_id DESC LIMIT 1",
                    (message.message_id, self.sprint_id),
                ).fetchone()
            ),
        )

    def _activate_unregistered_chat(self, key: str, shell_id: int = 1) -> str:
        """Give the shell an active chat the Sprint never registered.

        This is the Originating Planner's normal shape: its chat predates the
        Sprint, so re-enter wakes resolve into a conversation with no
        sprint_participant_conversations row.
        """
        self.con.execute(
            "UPDATE conversations SET state='closed',closed_at=datetime('now') "
            "WHERE shell_id=? AND state<>'closed'",
            (shell_id,),
        )
        conversation_id = str(
            self.con.execute(
                "INSERT INTO conversations "
                "(shell_id,owner_user_id,harness,worktree,state,"
                "creation_idempotency_key,creation_request_hash) "
                "SELECT shell_id,owner_user_id,harness,worktree,'idle',?,? "
                "FROM conversations WHERE shell_id=? LIMIT 1 "
                "RETURNING conversation_id",
                (key, key, shell_id),
            ).fetchone()[0]
        )
        self.con.execute(
            "INSERT INTO active_shell_chats (shell_id,chat_id) VALUES (?,?) "
            "ON CONFLICT(shell_id) DO UPDATE SET chat_id=excluded.chat_id",
            (shell_id, conversation_id),
        )
        self.con.commit()
        return conversation_id

    def test_unregistered_completed_message_without_run_pauses_as_invalid(self):
        target = self._activate_unregistered_chat("gui-chat-queued")
        message = self.send("pickup-unregistered-queued")
        old_wake = int(message.wake_id)
        _, wake_key = self.deliver_wake_with_turn(old_wake, terminal=False)
        self.assertEqual(
            target,
            self.con.execute(
                "SELECT target_conversation_id FROM sprint_wake_attempts "
                "WHERE wake_id=? ORDER BY attempt_number DESC LIMIT 1",
                (old_wake,),
            ).fetchone()[0],
        )

        self.assertEqual(
            (),
            self.lifecycle.reconcile_unread_pickup(
                self.sprint_id,
                trigger="unregistered-queued-turn",
            ),
        )

        self.con.execute(
            "UPDATE conversation_messages SET state='running' WHERE idempotency_key=?",
            (wake_key,),
        )
        self.con.execute(
            "UPDATE conversation_messages SET state='completed',"
            "completed_at=datetime('now') WHERE idempotency_key=?",
            (wake_key,),
        )
        self.con.commit()

        self.assertEqual(
            (),
            self.lifecycle.reconcile_unread_pickup(
                self.sprint_id,
                trigger="unregistered-turn-finished-unread",
            ),
        )
        self.assertEqual(
            ("paused", old_wake, "wake_pickup_evidence_invalid"),
            tuple(
                self.con.execute(
                    "SELECT s.lifecycle,wm.wake_id,"
                    "json_extract(r.body,'$.reason') "
                    "FROM sprints s JOIN sprint_reports r USING (sprint_id) "
                    "JOIN sprint_wake_messages wm ON wm.message_id=? "
                    "WHERE s.sprint_id=?",
                    (message.message_id, self.sprint_id),
                ).fetchone()
            ),
        )

    def test_unregistered_conversation_running_turn_suppresses_recovery(self):
        target = self._activate_unregistered_chat("gui-chat-running")
        message = self.send("pickup-unregistered-running")
        old_wake = int(message.wake_id)
        _, wake_key = self.deliver_wake_with_turn(old_wake, terminal=False)
        trigger_message_id = int(
            self.con.execute(
                "SELECT message_id FROM conversation_messages "
                "WHERE idempotency_key=?",
                (wake_key,),
            ).fetchone()[0]
        )
        self.con.execute(
            "UPDATE conversation_messages SET state='running' "
            "WHERE message_id=?",
            (trigger_message_id,),
        )
        run_id = int(
            self.con.execute(
                "INSERT INTO conversation_runs "
                "(conversation_id,shell_id,trigger_message_id,state,lease_owner,"
                "lease_expires_at,started_at,heartbeat_at) "
                "SELECT ?,shell_id,?,'running','test-broker',"
                "'2999-01-01 00:00:00','2026-08-01 00:00:00',"
                "'2026-08-01 00:00:00' FROM conversations "
                "WHERE conversation_id=?",
                (target, trigger_message_id, target),
            ).lastrowid
        )
        self.con.execute(
            "UPDATE conversations SET state='running' WHERE conversation_id=?",
            (target,),
        )
        self.con.commit()

        self.assertEqual(
            (),
            self.lifecycle.reconcile_unread_pickup(
                self.sprint_id,
                trigger="unregistered-running-turn",
            ),
        )

        self.con.execute(
            "UPDATE conversation_runs SET state='succeeded',"
            "ended_at=datetime('now'),exit_code=0 WHERE run_id=?",
            (run_id,),
        )
        self.con.execute(
            "UPDATE conversation_messages SET state='completed',"
            "completed_at=datetime('now') WHERE message_id=?",
            (trigger_message_id,),
        )
        self.con.execute(
            "UPDATE conversations SET state='waiting' WHERE conversation_id=?",
            (target,),
        )
        self.con.commit()

        replacements = self.lifecycle.reconcile_unread_pickup(
            self.sprint_id,
            trigger="unregistered-running-finished-unread",
        )
        self.assertEqual(1, len(replacements))

    def test_paused_decline_keeps_one_planner_wake_across_resume(self):
        assignment = self.send(
            "paused-decline-recovery",
            kind="work_assignment",
            actionable=True,
        )
        self.lifecycle.pause(
            self.sprint_id,
            sprint_domain.LifecycleActor("participant", 1),
            reason="decline while paused",
        )
        result_id = self.messages.decline(
            assignment.message_id,
            1,
            "cannot safely continue",
        )
        self.con.execute(
            "UPDATE conversation_messages SET state='running' "
            "WHERE conversation_id IN (SELECT conversation_id "
            "FROM sprint_participant_conversations "
            "WHERE sprint_participant_id=?) AND state='queued'",
            (self.planner_id,),
        )
        self.con.execute(
            "UPDATE conversation_messages SET state='completed',"
            "completed_at=datetime('now') WHERE conversation_id IN ("
            "SELECT conversation_id FROM sprint_participant_conversations "
            "WHERE sprint_participant_id=?) AND state='running'",
            (self.planner_id,),
        )
        self.con.commit()
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_wake_messages WHERE message_id=?",
                (result_id,),
            ).fetchone()[0],
        )

        first = self.coordinator().resume(
            self.sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
        )
        self.assertEqual((), first.requeued_wake_ids)
        wake_id = int(
            self.con.execute(
                "SELECT wake_id FROM sprint_wake_messages WHERE message_id=?",
                (result_id,),
            ).fetchone()[0]
        )
        self.assertEqual(
            [(wake_id, result_id, "pending")],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT wm.wake_id,wm.message_id,w.state "
                    "FROM sprint_wake_messages wm JOIN sprint_wake_outbox w "
                    "USING (wake_id) WHERE wm.message_id=?",
                    (result_id,),
                )
            ],
        )
        self.assertEqual(
            (),
            self.lifecycle.reconcile_unread_pickup(
                self.sprint_id,
                trigger="monitor",
            ),
        )

    def test_startup_reconciliation_repairs_once_across_repeated_passes(self):
        message = self.send("pickup-startup")
        old_wake = int(message.wake_id)
        self.deliver_wake_with_turn(old_wake, terminal=True)
        coordinator = self.coordinator()

        self.assertEqual("armed", coordinator.recover_on_startup(self.sprint_id))
        replacement = int(
            self.con.execute(
                "SELECT wake_id FROM sprint_wake_messages WHERE message_id=?",
                (message.message_id,),
            ).fetchone()[0]
        )
        self.assertNotEqual(old_wake, replacement)
        count = self.con.execute("SELECT COUNT(*) FROM sprint_wake_outbox").fetchone()[
            0
        ]

        self.assertEqual("armed", coordinator.recover_on_startup(self.sprint_id))
        self.assertEqual(
            count,
            self.con.execute("SELECT COUNT(*) FROM sprint_wake_outbox").fetchone()[0],
        )
        self.assertEqual(
            (replacement,),
            tuple(
                self.con.execute(
                    "SELECT wake_id FROM sprint_wake_messages WHERE message_id=?",
                    (message.message_id,),
                ).fetchone()
            ),
        )

    def test_startup_converts_legacy_generic_recovery_stall_to_atomic_pause(self):
        message = self.send("legacy-generic-recovery-stall")
        original = int(message.wake_id)
        self.fail_wake_turn(original, error_code="HARNESS_ROUTE_MISMATCH")
        generic_recovery = self.lifecycle.reconcile_unread_pickup(
            self.sprint_id,
            trigger="legacy-current-floor-first-failure",
        )[0]
        self.assertEqual(
            f"sprint-recovery:{self.sprint_id}:delivered-unread:{original}",
            self.con.execute(
                "SELECT idempotency_key FROM sprint_wake_outbox WHERE wake_id=?",
                (generic_recovery,),
            ).fetchone()[0],
        )
        self.con.execute(
            "UPDATE sprint_wake_outbox SET available_at="
            "datetime('now','-1 second') WHERE wake_id=?",
            (generic_recovery,),
        )
        self.con.commit()
        self.fail_wake_turn(
            generic_recovery,
            error_code="HARNESS_SESSION_DISCOVERY_FAILED",
        )
        self.assertEqual(
            "armed",
            self.con.execute(
                "SELECT lifecycle FROM sprints WHERE sprint_id=?",
                (self.sprint_id,),
            ).fetchone()[0],
        )

        coordinator = self.coordinator()
        self.assertEqual("armed", coordinator.recover_on_startup(self.sprint_id))

        self.assertEqual(
            (
                "paused",
                None,
                generic_recovery,
                "wake_pickup_failed",
                "HARNESS_SESSION_DISCOVERY_FAILED",
                1,
            ),
            tuple(
                self.con.execute(
                    "SELECT s.lifecycle,m.read_at,wm.wake_id,"
                    "json_extract(r.body,'$.reason'),"
                    "json_extract(r.body,'$.detail.error_code'),"
                    "(SELECT COUNT(*) FROM sprint_events e "
                    "WHERE e.sprint_id=s.sprint_id "
                    "AND e.event_type='wake.pickup_exhausted') "
                    "FROM sprints s JOIN wake_message m "
                    "ON m.message_id=? JOIN sprint_wake_messages wm "
                    "USING (message_id) JOIN sprint_reports r "
                    "ON r.sprint_id=s.sprint_id "
                    "WHERE s.sprint_id=? AND r.report_kind='pause' "
                    "ORDER BY r.report_id DESC LIMIT 1",
                    (message.message_id, self.sprint_id),
                ).fetchone()
            ),
        )
        before_repeat = self.con.total_changes
        self.assertEqual("paused", coordinator.recover_on_startup(self.sprint_id))
        self.assertEqual(before_repeat, self.con.total_changes)

    def test_resume_projects_observed_merge_before_releasing_downstream(self):
        registered = self.register()
        self.con.execute(
            "UPDATE sprint_work_units SET disposition='merge_ready' "
            "WHERE work_unit_id=?",
            (self.unit_id,),
        )
        downstream = int(
            self.con.execute(
                "INSERT INTO sprint_work_units "
                "(sprint_id,assigned_shell_id,reviewer_shell_id,title,"
                "expected_output,planned_wave) "
                "VALUES (?,1,2,'Downstream','Continue the lane',2)",
                (self.sprint_id,),
            ).lastrowid
        )
        self.con.execute(
            "INSERT INTO sprint_work_unit_dependencies "
            "(sprint_id,work_unit_id,depends_on_work_unit_id) VALUES (?,?,?)",
            (self.sprint_id, downstream, self.unit_id),
        )
        self.con.commit()
        coordinator = self.coordinator()
        coordinator.pause(
            self.sprint_id,
            sprint_domain.LifecycleActor("participant", 1),
            reason="merge observation race",
        )
        self.con.execute(
            "INSERT INTO sprint_pr_transitions "
            "(registered_pr_id,normalized_state,transition_key,observed_head_sha) "
            "VALUES (?,'merged','paused-merge',?)",
            (registered.registered_pr_id, "b" * 40),
        )
        self.con.commit()
        self.reader.current = pull_request(
            state="MERGED",
            checks="SUCCESS",
            checks_failed=False,
            head_sha="b" * 40,
        )

        receipt = coordinator.resume(
            self.sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
            reason="evidence reviewed",
        )

        # The engine-wide watcher projects the merge during the resume
        # reconciliation callback; the lifecycle projection then sees the unit
        # already complete instead of reporting it a second time.
        self.assertEqual((), receipt.projected_work_unit_ids)
        self.assertEqual(1, len(receipt.dispatched_wake_ids))
        self.assertEqual(
            [(self.unit_id, "completed"), (downstream, "ready")],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT work_unit_id,disposition FROM sprint_work_units "
                    "WHERE work_unit_id IN (?,?) ORDER BY work_unit_id",
                    (self.unit_id, downstream),
                )
            ],
        )
        self.assertEqual(
            [(downstream, "work_assignment", "pending")],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT work_unit_id,message_kind,disposition "
                    "FROM wake_message WHERE work_unit_id=?",
                    (downstream,),
                )
            ],
        )

    def test_resume_records_drift_and_github_failure_without_blocking(self):
        self.register()
        coordinator = self.coordinator()
        coordinator.pause(
            self.sprint_id,
            sprint_domain.LifecycleActor("participant", 1),
            reason="inspect drift",
        )
        document_id = int(
            self.con.execute(
                "SELECT document_id FROM sprint_specs WHERE sprint_id=?",
                (self.sprint_id,),
            ).fetchone()[0]
        )
        self.con.execute(
            "UPDATE documents SET body='edited while paused' WHERE document_id=?",
            (document_id,),
        )
        self.con.commit()
        self.reader.current = GitHubReadError("github unavailable")

        receipt = coordinator.resume(
            self.sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
        )

        self.assertTrue(receipt.changed)
        self.assertEqual((document_id,), receipt.spec_drift_document_ids)
        self.assertEqual(
            ("acme/repo#42 reconciliation failed: github unavailable",),
            receipt.anomalies,
        )
        self.assertEqual(
            "armed",
            self.con.execute(
                "SELECT lifecycle FROM sprints WHERE sprint_id=?",
                (self.sprint_id,),
            ).fetchone()[0],
        )
        payload = json.loads(
            self.con.execute(
                "SELECT payload FROM sprint_events WHERE sprint_id=? "
                "AND event_type='lifecycle.reconciled'",
                (self.sprint_id,),
            ).fetchone()[0]
        )
        self.assertEqual([str(document_id)], sorted(payload["spec_drift"]))
        self.assertEqual(list(receipt.anomalies), payload["anomalies"])
        self.assertEqual(
            [(None, 3, 0)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT to_participant_id,receiver_shell_id,actionable "
                    "FROM wake_message "
                    "WHERE idempotency_key LIKE 'sprint-resume:%'"
                )
            ],
        )

    def test_resume_surfaces_native_and_capacity_anomalies_without_blocking(self):
        run_id = self.add_live_run()
        self.con.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (5,'Replacement Reviewer','REV5','reviewer','prompt',1)"
        )
        self.con.execute(
            "INSERT INTO sprint_participants "
            "(sprint_id,shell_id,role,harness,model,effort) "
            "VALUES (?,5,'reviewer','codex','model','high')",
            (self.sprint_id,),
        )
        self.con.commit()
        coordinator = self.coordinator()
        coordinator.pause(
            self.sprint_id,
            sprint_domain.LifecycleActor("participant", 1),
            reason="participant capacity check",
        )
        self.con.execute("UPDATE shells SET is_deleted=1 WHERE shell_id=2")
        self.con.commit()

        receipt = coordinator.resume(
            self.sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
            reason="replace unavailable closeout owner",
            conformance_reviewer_shell_id=5,
        )

        self.assertEqual(
            (
                (
                    "native-run reconciliation deferred: conversation broker "
                    f"unavailable for run(s) {run_id}"
                ),
                f"participant {self.reviewer_id} (reviewer) has no usable capacity",
            ),
            receipt.anomalies,
        )
        self.assertEqual(
            "armed",
            self.con.execute(
                "SELECT lifecycle FROM sprints WHERE sprint_id=?",
                (self.sprint_id,),
            ).fetchone()[0],
        )
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM wake_message "
                "WHERE idempotency_key LIKE 'sprint-resume:%'",
            ).fetchone()[0],
        )


class ClosedReviewRecoveryTest(SprintReviewLoopCase):
    def test_closed_without_merge_resolves_every_review_expectation(self):
        handoff = self.request_review()
        self.accept_review(handoff.message_id)
        accepted = self.con.execute(
            "SELECT sprint_id,to_participant_id,read_at FROM wake_message "
            "WHERE message_id=?",
            (handoff.message_id,),
        ).fetchone()
        self.con.execute(
            "INSERT INTO sprint_liveness_expectations "
            "(message_id,sprint_id,participant_id,accepted_at,last_strong_at,"
            "last_strong_key,next_evaluation_at) VALUES (?,?,?,?,?,?,?)",
            (
                handoff.message_id,
                accepted["sprint_id"],
                accepted["to_participant_id"],
                accepted["read_at"],
                accepted["read_at"],
                f"message.accepted:{handoff.message_id}",
                "2999-01-01 00:00:00",
            ),
        )
        self.con.commit()
        lifecycle = sprint_domain.SprintLifecycleStore(
            self.con,
            interrupt_run=lambda _run_id: True,
            notify_commit=lambda: True,
        )
        lifecycle.pause(
            self.sprint_id,
            sprint_domain.LifecycleActor("participant", 1),
            reason="PR ownership changed",
        )
        self.reader.current = pull_request(
            state="CLOSED",
            checks=None,
            checks_failed=False,
        )
        coordinator = sprint_recovery.SprintRecoveryCoordinator(
            self.con,
            repo_root=ROOT,
            reader_factory=lambda _repository: self.reader,
            interrupt_run=lambda _run_id: True,
            notify_commit=lambda: True,
        )

        receipt = coordinator.resume(
            self.sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
        )

        self.assertEqual((handoff.message_id,), receipt.resolved_review_message_ids)
        self.assertEqual(
            ("registered_pr.closed_without_merge", None),
            tuple(
                self.con.execute(
                    "SELECT resolution,next_evaluation_at "
                    "FROM sprint_liveness_expectations WHERE message_id=?",
                    (handoff.message_id,),
                ).fetchone()
            ),
        )
        self.assertEqual(
            "in_review",
            self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                (self.unit_id,),
            ).fetchone()[0],
            "closed PR resolution must not invent a review outcome",
        )


class LifecycleExitAndRestartTest(SprintDomainCase):
    def setUp(self) -> None:
        quiet_env = mock.patch.dict(
            "os.environ", {"SC_SPRINT_FORCE_NEW_QUIET_SECONDS": "0"}
        )
        quiet_env.start()
        self.addCleanup(quiet_env.stop)
        super().setUp()

    def terminalize(self, sprint_id: int) -> None:
        self.con.execute(
            "UPDATE sprint_work_units SET disposition='completed',"
            "completed_at=datetime('now') WHERE sprint_id=?",
            (sprint_id,),
        )
        self.con.commit()

    def coordinator(self) -> sprint_recovery.SprintRecoveryCoordinator:
        def no_reader(_repository: str):
            raise AssertionError("non-armed restart performed GitHub egress")

        return sprint_recovery.SprintRecoveryCoordinator(
            self.con,
            repo_root=ROOT,
            reader_factory=no_reader,
            interrupt_run=lambda _run_id: True,
            notify_commit=lambda: True,
        )

    def ensure_wake_chat(self, sprint_id: int, shell_id: int) -> str:
        conversation = self.con.execute(
            "SELECT pc.conversation_id "
            "FROM sprint_participant_conversations pc "
            "JOIN sprint_participants participant "
            "ON participant.participant_id=pc.sprint_participant_id "
            "WHERE participant.sprint_id=? AND participant.shell_id=?",
            (sprint_id, shell_id),
        ).fetchone()
        if conversation is None:
            participant_id = int(
                self.con.execute(
                    "SELECT participant_id FROM sprint_participants "
                    "WHERE sprint_id=? AND shell_id=?",
                    (sprint_id, shell_id),
                ).fetchone()[0]
            )
            receipt = sprint_message_delivery.SprintMessageStore(self.con).send(
                sprint_id,
                to_participant_id=participant_id,
                message_kind="notification",
                body="Create the fixture wake chat.",
                declared_type="re-enter",
                idempotency_key=f"fixture-live-run:{sprint_id}:{shell_id}",
            )
            service = sprint_message_delivery.SprintWakeDeliveryService(self.con)
            while True:
                outcome = service.deliver_once(
                    "fixture-live-run",
                    lambda target, _prompt, _key: target,
                )
                self.assertIsNotNone(outcome)
                if outcome.wake_id == receipt.wake_id:
                    break
            conversation = self.con.execute(
                "SELECT active.chat_id FROM sprint_participants participant "
                "JOIN active_shell_chats active "
                "ON active.shell_id=participant.shell_id "
                "WHERE participant.participant_id=?",
                (participant_id,),
            ).fetchone()
        return str(conversation[0])

    def add_live_run(self, sprint_id: int, shell_id: int) -> tuple[str, int]:
        conversation_id = self.ensure_wake_chat(sprint_id, shell_id)
        token = int(
            self.con.execute("SELECT COUNT(*)+1 FROM conversation_runs").fetchone()[0]
        )
        message_id = int(
            self.con.execute(
                "INSERT INTO conversation_messages "
                "(conversation_id,sender_kind,sender_ref,message_kind,body,"
                "idempotency_key,request_hash,state) "
                "VALUES (?,'engine','test','prompt','active Sprint turn',?,?,"
                "'running')",
                (
                    conversation_id,
                    f"terminal-active:{token}",
                    f"terminal-active:{token}",
                ),
            ).lastrowid
        )
        run_id = int(
            self.con.execute(
                "INSERT INTO conversation_runs "
                "(conversation_id,shell_id,trigger_message_id,state,lease_owner,"
                "lease_expires_at,started_at,heartbeat_at) "
                "VALUES (?,?,?,'running','test-broker','2999-01-01 00:00:00',"
                "'2026-08-02 00:00:00','2026-08-02 00:00:00')",
                (conversation_id, shell_id, message_id),
            ).lastrowid
        )
        self.con.execute(
            "UPDATE conversations SET state='queued' WHERE conversation_id=?",
            (conversation_id,),
        )
        self.con.execute(
            "UPDATE conversations SET state='running' WHERE conversation_id=?",
            (conversation_id,),
        )
        self.con.commit()
        return conversation_id, run_id

    def test_prepared_abort_records_stub_report_and_preserves_plan(self):
        sprint_id, unit_id = self.create_sprint()
        coordinator = self.coordinator()

        receipt = coordinator.abort(
            sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
            reason="Sprint will not arm",
            terminal_outcome="discarded before arming",
        )

        self.assertTrue(receipt.changed)
        self.assertEqual(
            ("aborted", "discarded before arming"),
            tuple(
                self.con.execute(
                    "SELECT lifecycle,terminal_outcome FROM sprints WHERE sprint_id=?",
                    (sprint_id,),
                ).fetchone()
            ),
        )
        report = json.loads(
            self.con.execute(
                "SELECT body FROM sprint_reports WHERE report_id=?",
                (receipt.report_id,),
            ).fetchone()[0]
        )
        self.assertEqual([], report["completed_work"])
        self.assertEqual(
            [(unit_id, "planned")],
            [
                (row["work_unit_id"], row["disposition"])
                for row in report["outstanding_work"]
            ],
        )
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_work_units WHERE work_unit_id=?",
                (unit_id,),
            ).fetchone()[0],
        )

    def test_completion_closes_linked_live_chat_without_interrupt_and_notifies(self):
        sprint_id, _ = self.create_sprint()
        self.store.arm(sprint_id, 3)
        planner_conversation, planner_run = self.add_live_run(sprint_id, 3)
        developer_conversation, developer_run = self.add_live_run(sprint_id, 1)
        reviewer_conversation = self.ensure_wake_chat(sprint_id, 2)
        process = sprint_domain.active_chat_registry.process_details(str(os.getpid()))
        self.assertIsNotNone(process)
        self.con.execute(
            "UPDATE active_shell_chats SET process_pid=?,process_start_ticks=? "
            "WHERE shell_id=1",
            (process.pid, process.start_ticks),
        )
        self.con.commit()
        interrupts: list[int] = []
        lifecycle = sprint_domain.SprintLifecycleStore(
            self.con,
            interrupt_run=lambda run_id: interrupts.append(run_id) or True,
            notify_commit=lambda: True,
        )
        notified: list[str] = []
        self.terminalize(sprint_id)

        def notify_after_commit(conversation_id: str) -> int:
            self.assertFalse(self.con.in_transaction)
            notified.append(conversation_id)
            return 1

        with mock.patch.object(
            sprint_domain.conversation_events,
            "notify",
            side_effect=notify_after_commit,
        ):
            self.assertTrue(
                lifecycle.transition(
                    sprint_id,
                    "completed",
                    sprint_domain.LifecycleActor("planner", 3),
                    reason="finish with successful chat cleanup",
                    terminal_outcome="accepted",
                )
        )

        self.assertEqual([], interrupts)
        self.assertEqual([developer_conversation, reviewer_conversation], notified)
        self.assertEqual(
            [(planner_run, "running"), (developer_run, "running")],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT run_id,state FROM conversation_runs "
                    "WHERE run_id IN (?,?) ORDER BY run_id",
                    (planner_run, developer_run),
                )
            ],
        )
        self.assertEqual(
            [(3, planner_conversation)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT shell_id,chat_id FROM active_shell_chats "
                    "WHERE shell_id IN (1,3) ORDER BY shell_id"
                )
            ],
        )
        self.assertEqual(
            ("closed", 1),
            tuple(
                self.con.execute(
                    "SELECT state,closed_at IS NOT NULL FROM conversations "
                    "WHERE conversation_id=?",
                    (developer_conversation,),
                ).fetchone()
            ),
        )
        self.assertEqual(
            ("closed", 1),
            tuple(
                self.con.execute(
                    "SELECT state,closed_at IS NOT NULL FROM conversations "
                    "WHERE conversation_id=?",
                    (reviewer_conversation,),
                ).fetchone()
            ),
        )
        self.assertEqual(
            {
                "reason": "sprint_completed",
                "retained_shell_ids": [3],
                "sprint_id": sprint_id,
                "state": "closed",
            },
            json.loads(
                self.con.execute(
                    "SELECT payload FROM conversation_events "
                    "WHERE conversation_id=? AND event_type='conversation.closed' "
                    "AND json_extract(payload,'$.reason')='sprint_completed'",
                    (developer_conversation,),
                ).fetchone()[0]
            ),
        )
        self.assertEqual(
            [developer_conversation, reviewer_conversation],
            json.loads(
                self.con.execute(
                    "SELECT payload FROM sprint_events WHERE sprint_id=? "
                    "AND event_type='lifecycle.completed'",
                    (sprint_id,),
                ).fetchone()[0]
            )["closed_conversation_ids"],
        )

        later_chat = str(
            self.con.execute(
                "INSERT INTO conversations "
                "(shell_id,owner_user_id,harness,worktree,state,"
                "creation_idempotency_key,creation_request_hash) "
                "VALUES (1,1,'codex','/tmp/work','idle','later-normal','later-normal') "
                "RETURNING conversation_id"
            ).fetchone()[0]
        )
        self.con.execute(
            "INSERT INTO active_shell_chats (shell_id,chat_id) VALUES (1,?)",
            (later_chat,),
        )
        self.con.commit()
        with mock.patch.object(sprint_domain.conversation_events, "notify") as notify:
            self.assertFalse(
                lifecycle.transition(
                    sprint_id,
                    "completed",
                    sprint_domain.LifecycleActor("planner", 3),
                    reason="idempotent replay",
                    terminal_outcome="accepted",
                )
            )
        notify.assert_not_called()
        self.assertEqual(
            ("idle", later_chat),
            tuple(
                self.con.execute(
                    "SELECT conversation.state,active.chat_id "
                    "FROM conversations conversation JOIN active_shell_chats active "
                    "ON active.chat_id=conversation.conversation_id "
                    "WHERE conversation.conversation_id=?",
                    (later_chat,),
                ).fetchone()
            ),
        )

    def test_abort_interrupts_owning_planner_and_other_active_participants(self):
        sprint_id, _ = self.create_sprint()
        self.store.arm(sprint_id, 3)
        _, planner_run = self.add_live_run(sprint_id, 3)
        _, developer_run = self.add_live_run(sprint_id, 1)
        interrupts: list[int] = []
        lifecycle = sprint_domain.SprintLifecycleStore(
            self.con,
            interrupt_run=lambda run_id: interrupts.append(run_id) or True,
            notify_commit=lambda: True,
        )

        receipt = lifecycle.abort(
            sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
            reason="stop all work",
            terminal_outcome="aborted",
        )

        self.assertEqual((planner_run, developer_run), receipt.interrupt_run_ids)
        self.assertEqual([planner_run, developer_run], interrupts)
        self.assertEqual(
            [(planner_run,), (developer_run,)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT run_id FROM conversation_events "
                    "WHERE event_type='run.interrupt.requested' ORDER BY run_id"
                )
            ],
        )
        self.assertEqual(
            2,
            self.con.execute(
                "SELECT COUNT(*) FROM active_shell_chats WHERE shell_id IN (1,3)"
            ).fetchone()[0],
        )
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM conversation_events "
                "WHERE event_type='conversation.close.requested'"
            ).fetchone()[0],
        )
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM conversation_events "
                "WHERE event_type='conversation.closed' "
                "AND json_extract(payload,'$.reason')='sprint_completed'"
            ).fetchone()[0],
        )

    def test_fallback_retains_unique_reviewer_final_report_author(self):
        sprint_id, _ = self.create_sprint()
        self.store.arm(sprint_id, 3)
        chats = {
            shell_id: self.ensure_wake_chat(sprint_id, shell_id)
            for shell_id in (1, 2, 3)
        }
        self.con.execute(
            "INSERT INTO sprint_reports "
            "(sprint_id,report_kind,author_shell_id,body,idempotency_key) "
            "VALUES (?,'final',2,'Reviewer final report','fallback-final')",
            (sprint_id,),
        )
        self.con.commit()
        self.terminalize(sprint_id)

        self.store.transition(
            sprint_id,
            "completed",
            sprint_domain.LifecycleActor("planner", 3),
            reason="finish with unique Reviewer author",
            terminal_outcome="completed",
        )

        self.assertEqual(
            [(1, chats[1], "closed"), (2, chats[2], "idle"), (3, chats[3], "idle")],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT shell_id,conversation_id,state FROM conversations "
                    "WHERE conversation_id IN (?,?,?) ORDER BY shell_id",
                    (chats[1], chats[2], chats[3]),
                )
            ],
        )
        self.assertEqual(
            [(2, chats[2]), (3, chats[3])],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT shell_id,chat_id FROM active_shell_chats "
                    "WHERE shell_id IN (1,2,3) ORDER BY shell_id"
                )
            ],
        )
        self.assertEqual(
            [2, 3],
            json.loads(
                self.con.execute(
                    "SELECT payload FROM conversation_events "
                    "WHERE conversation_id=? AND event_type='conversation.closed'",
                    (chats[1],),
                ).fetchone()[0]
            )["retained_shell_ids"],
        )

    def test_fallback_ambiguous_authors_close_linked_reviewers_not_unrelated_chat(
        self,
    ):
        self.con.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (5,'Reviewer 5','REV5','reviewer','prompt',1)"
        )
        sprint_id, _ = self.create_sprint()
        self.con.execute(
            "INSERT INTO sprint_participants "
            "(sprint_id,shell_id,role,harness,model,effort) "
            "VALUES (?,5,'reviewer','codex','model','high')",
            (sprint_id,),
        )
        self.con.commit()
        self.store.arm(
            sprint_id, 3, conformance_reviewer_shell_id=2
        )
        chats = {
            shell_id: self.ensure_wake_chat(sprint_id, shell_id)
            for shell_id in (1, 2, 3, 5)
        }
        self.con.execute(
            "UPDATE conversations SET state='closed',closed_at=datetime('now') "
            "WHERE conversation_id=?",
            (chats[2],),
        )
        unrelated_reviewer_chat = str(
            self.con.execute(
                "INSERT INTO conversations "
                "(shell_id,owner_user_id,harness,worktree,state,"
                "creation_idempotency_key,creation_request_hash) "
                "VALUES (2,1,'kimi','/tmp/work','idle','reviewer-normal',"
                "'reviewer-normal') RETURNING conversation_id"
            ).fetchone()[0]
        )
        self.con.execute(
            "INSERT INTO active_shell_chats (shell_id,chat_id) VALUES (2,?)",
            (unrelated_reviewer_chat,),
        )
        self.con.executemany(
            "INSERT INTO sprint_reports "
            "(sprint_id,report_kind,author_shell_id,body,idempotency_key) "
            "VALUES (?,'final',?,?,?)",
            (
                (sprint_id, 2, "Reviewer 2 final", "ambiguous-final-2"),
                (sprint_id, 5, "Reviewer 5 final", "ambiguous-final-5"),
            ),
        )
        self.con.commit()
        self.terminalize(sprint_id)

        self.store.transition(
            sprint_id,
            "completed",
            sprint_domain.LifecycleActor("planner", 3),
            reason="ambiguous final-report author evidence",
            terminal_outcome="completed",
        )

        self.assertEqual(
            [(2, unrelated_reviewer_chat), (3, chats[3])],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT shell_id,chat_id FROM active_shell_chats "
                    "WHERE shell_id IN (1,2,3,5) ORDER BY shell_id"
                )
            ],
        )
        self.assertEqual(
            [(1, chats[1], "closed"), (5, chats[5], "closed")],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT shell_id,conversation_id,state FROM conversations "
                    "WHERE conversation_id IN (?,?) ORDER BY shell_id",
                    (chats[1], chats[5]),
                )
            ],
        )
        self.assertEqual(
            ("idle", 0),
            tuple(
                self.con.execute(
                    "SELECT state,closed_at IS NOT NULL FROM conversations "
                    "WHERE conversation_id=?",
                    (unrelated_reviewer_chat,),
                ).fetchone()
            ),
        )
        retained_sets = [
            json.loads(row[0])["retained_shell_ids"]
            for row in self.con.execute(
                "SELECT payload FROM conversation_events "
                "WHERE event_type='conversation.closed' "
                "AND json_extract(payload,'$.reason')='sprint_completed' "
                "ORDER BY event_id"
            )
        ]
        self.assertEqual([[3], [3]], retained_sets)

    def test_post_commit_notification_failure_preserves_completed_cleanup(self):
        sprint_id, _ = self.create_sprint()
        self.store.arm(sprint_id, 3)
        developer_chat = self.ensure_wake_chat(sprint_id, 1)
        reviewer_chat = self.ensure_wake_chat(sprint_id, 2)
        self.terminalize(sprint_id)
        with (
            mock.patch.object(
                sprint_domain.conversation_events,
                "notify",
                side_effect=(RuntimeError("notifier unavailable"), 1),
            ) as notify,
            self.assertRaisesRegex(RuntimeError, "notifier unavailable"),
        ):
            self.store.transition(
                sprint_id,
                "completed",
                sprint_domain.LifecycleActor("planner", 3),
                reason="commit before notifier",
                terminal_outcome="completed",
            )

        self.assertEqual(
            [mock.call(developer_chat), mock.call(reviewer_chat)],
            notify.call_args_list,
        )
        self.assertEqual(
            ("completed", "closed", 1, "closed", 1, 0),
            tuple(
                self.con.execute(
                    "SELECT sprint.lifecycle,conversation.state,"
                    "conversation.closed_at IS NOT NULL,"
                    "reviewer.state,reviewer.closed_at IS NOT NULL,"
                    "(SELECT COUNT(*) FROM active_shell_chats WHERE shell_id=1) "
                    "FROM sprints sprint JOIN conversations conversation "
                    "ON conversation.conversation_id=? JOIN conversations reviewer "
                    "ON reviewer.conversation_id=? WHERE sprint.sprint_id=?",
                    (developer_chat, reviewer_chat, sprint_id),
                ).fetchone()
            ),
        )
    def test_restart_recovers_prepared_armed_and_paused_without_state_drift(self):
        sprint_id, _ = self.create_sprint()
        coordinator = self.coordinator()

        self.assertEqual("prepared", coordinator.recover_on_startup(sprint_id))
        self.assertEqual(
            "prepared",
            self.con.execute(
                "SELECT lifecycle FROM sprints WHERE sprint_id=?", (sprint_id,)
            ).fetchone()[0],
        )
        self.store.arm(sprint_id, 3)
        armed = sprint_recovery.SprintRecoveryCoordinator(
            self.con,
            repo_root=ROOT,
            reader_factory=lambda _repository: None,
            interrupt_run=lambda _run_id: True,
            notify_commit=lambda: True,
        )
        self.assertEqual("armed", armed.recover_on_startup(sprint_id))
        self.assertEqual(
            "armed",
            self.con.execute(
                "SELECT lifecycle FROM sprints WHERE sprint_id=?", (sprint_id,)
            ).fetchone()[0],
        )
        self.store.pause(
            sprint_id,
            sprint_domain.LifecycleActor("participant", 1),
            reason="restart while paused",
        )
        self.assertEqual("paused", coordinator.recover_on_startup(sprint_id))
        self.assertEqual(
            "paused",
            self.con.execute(
                "SELECT lifecycle FROM sprints WHERE sprint_id=?", (sprint_id,)
            ).fetchone()[0],
        )

    def test_resume_cannot_bypass_prepared_arming_transaction(self):
        sprint_id, _ = self.create_sprint()
        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError,
            "prepared Sprints must use arm",
        ):
            self.store.resume(
                sprint_id,
                sprint_domain.LifecycleActor("planner", 3),
            )
        self.assertEqual(
            ("prepared", 0, 0),
            tuple(
                self.con.execute(
                    "SELECT s.lifecycle,"
                    "(SELECT COUNT(*) FROM wake_message WHERE sprint_id=s.sprint_id),"
                    "(SELECT COUNT(*) FROM sprint_events WHERE sprint_id=s.sprint_id) "
                    "FROM sprints s WHERE s.sprint_id=?",
                    (sprint_id,),
                ).fetchone()
            ),
        )


if __name__ == "__main__":
    import unittest

    unittest.main(verbosity=2)
