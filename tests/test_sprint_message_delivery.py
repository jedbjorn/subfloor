#!/usr/bin/env python3
"""Stage 3 gates for dedicated Sprint messages and wake delivery."""
from __future__ import annotations

import hashlib
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
MIGRATIONS = ENGINE / "migrations"

sys.path.insert(0, str(ENGINE / "scripts"))
import sprint_domain  # noqa: E402
import sprint_message_delivery as delivery  # noqa: E402


def apply_schema(con: sqlite3.Connection) -> None:
    con.executescript((ENGINE / "schema.sql").read_text())
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")


class SprintMessageCase(unittest.TestCase):
    def setUp(self) -> None:
        self.con = sqlite3.connect(":memory:")
        self.addCleanup(self.con.close)
        self.con.row_factory = sqlite3.Row
        apply_schema(self.con)
        self.con.execute("INSERT INTO users (user_id,username) VALUES (1,'operator')")
        self.con.executemany(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (?,?,?,?,?,1)",
            (
                (1, "Developer", "DEV1", "dev", "prompt"),
                (2, "Reviewer", "REV1", "reviewer", "prompt"),
                (3, "Planner", "PLN1", "planner", "prompt"),
            ),
        )
        self.con.executemany(
            "INSERT INTO conversations "
            "(conversation_id,shell_id,owner_user_id,harness,worktree,"
            "creation_idempotency_key,creation_request_hash) "
            "VALUES (?,?,1,'codex',?,?,?)",
            (
                ("cv_dev", 1, "/tmp/dev", "conversation-dev", "hash-dev"),
                ("cv_review", 2, "/tmp/review", "conversation-review", "hash-review"),
                ("cv_plan", 3, "/tmp/plan", "conversation-plan", "hash-plan"),
            ),
        )
        feature_id = self.con.execute(
            "INSERT INTO roadmap (title,roadmap_status) "
            "VALUES ('Feature','in_progress')"
        ).lastrowid
        body = "governing spec"
        document_id = self.con.execute(
            "INSERT INTO documents (feature_id,kind,seq,title,body) "
            "VALUES (?,'spec',1,'Spec',?)",
            (feature_id, body),
        ).lastrowid
        revision = hashlib.sha256(body.encode()).hexdigest()
        approval_id = self.con.execute(
            "INSERT INTO sprint_spec_approvals "
            "(document_id,revision_sha256,reviewer_shell_id,verdict) "
            "VALUES (?,?,2,'pass')",
            (document_id, revision),
        ).lastrowid
        self.sprint_id = int(
            self.con.execute(
                "INSERT INTO sprints "
                "(feature_id,originating_planner_shell_id,merge_grant_enabled) "
                "VALUES (?,3,1)",
                (feature_id,),
            ).lastrowid
        )
        self.con.execute(
            "INSERT INTO sprint_specs "
            "(sprint_id,document_id,bound_revision_sha256,approval_id) "
            "VALUES (?,?,?,?)",
            (self.sprint_id, document_id, revision, approval_id),
        )
        self.con.executemany(
            "INSERT INTO sprint_participants "
            "(sprint_id,shell_id,role,harness,current_conversation_id) "
            "VALUES (?,?,?,?,?)",
            (
                (self.sprint_id, 3, "planner", "codex", "cv_plan"),
                (self.sprint_id, 1, "developer", "codex", "cv_dev"),
                (self.sprint_id, 2, "reviewer", "codex", "cv_review"),
            ),
        )
        participants = {
            row["role"]: int(row["participant_id"])
            for row in self.con.execute(
                "SELECT role,participant_id FROM sprint_participants "
                "WHERE sprint_id=?",
                (self.sprint_id,),
            )
        }
        self.planner_id = participants["planner"]
        self.developer_id = participants["developer"]
        self.reviewer_id = participants["reviewer"]
        self.unit_id = int(
            self.con.execute(
                "INSERT INTO sprint_work_units "
                "(sprint_id,assigned_shell_id,reviewer_shell_id,title,expected_output) "
                "VALUES (?,1,2,'Unit','Ship it')",
                (self.sprint_id,),
            ).lastrowid
        )
        self.con.commit()
        self.lifecycle = sprint_domain.SprintLifecycleStore(self.con)
        initial_wake = self.lifecycle.arm(self.sprint_id, 3)[0]
        initial_message = self.con.execute(
            "SELECT message_id FROM sprint_wake_messages WHERE wake_id=?",
            (initial_wake,),
        ).fetchone()[0]
        self.messages = delivery.SprintMessageStore(self.con)
        self.assertEqual("accepted", self.messages.mark_read(initial_message, 1))

    def send(
        self,
        key: str,
        *,
        kind: str = "notification",
        actionable: bool = False,
        to_participant_id: int | None = None,
        active: bool = True,
    ) -> delivery.MessageReceipt:
        return self.messages.send(
            self.sprint_id,
            to_participant_id=to_participant_id or self.developer_id,
            from_participant_id=self.planner_id,
            work_unit_id=self.unit_id,
            message_kind=kind,
            body=f"body for {key}",
            actionable=actionable,
            active=active,
            idempotency_key=key,
        )


class MessageTransactionTest(SprintMessageCase):
    def test_message_and_active_wake_commit_or_roll_back_together(self) -> None:
        before = tuple(
            self.con.execute(
                "SELECT (SELECT COUNT(*) FROM sprint_messages),"
                "(SELECT COUNT(*) FROM sprint_wake_outbox)"
            ).fetchone()
        )
        self.con.execute(
            "CREATE TEMP TRIGGER fail_wake BEFORE INSERT ON sprint_wake_outbox "
            "BEGIN SELECT RAISE(ABORT,'simulated crash window'); END"
        )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "simulated crash"):
            self.send("atomic")

        self.assertEqual(
            before,
            tuple(
                self.con.execute(
                    "SELECT (SELECT COUNT(*) FROM sprint_messages),"
                    "(SELECT COUNT(*) FROM sprint_wake_outbox)"
                ).fetchone()
            ),
        )

    def test_idempotency_replays_exact_input_and_rejects_conflicts(self) -> None:
        first = self.send("same-key")
        replay = self.send("same-key")
        self.assertEqual(first.message_id, replay.message_id)
        self.assertEqual(first.wake_id, replay.wake_id)
        self.assertFalse(replay.created)

        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError, "different input"
        ):
            self.messages.send(
                self.sprint_id,
                to_participant_id=self.developer_id,
                message_kind="notification",
                body="different body",
                idempotency_key="same-key",
                active=True,
            )
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_messages WHERE idempotency_key='same-key'"
            ).fetchone()[0],
        )

    def test_only_task_messages_are_actionable_and_passive_has_no_wake(self) -> None:
        before = self.con.execute("SELECT COUNT(*) FROM sprint_messages").fetchone()[0]
        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError, "only work assignments"
        ):
            self.send("bad-action", actionable=True)
        self.assertEqual(
            before,
            self.con.execute("SELECT COUNT(*) FROM sprint_messages").fetchone()[0],
        )

        passive = self.send("passive", active=False)
        self.assertIsNone(passive.wake_id)
        self.assertEqual(
            [],
            self.con.execute(
                "SELECT wake_id FROM sprint_wake_messages WHERE message_id=?",
                (passive.message_id,),
            ).fetchall(),
        )


class AcceptanceAndDeclineTest(SprintMessageCase):
    def test_actionable_read_accepts_but_informational_read_does_not(self) -> None:
        task = self.send(
            "accept-task", kind="review_request", actionable=True
        )
        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "invalid Sprint message acceptance state"
        ):
            self.con.execute(
                "UPDATE sprint_messages SET read_at=datetime('now') "
                "WHERE message_id=?",
                (task.message_id,),
            )
        self.con.rollback()
        self.assertEqual("accepted", self.messages.mark_read(task.message_id, 1))
        task_row = self.con.execute(
            "SELECT disposition,read_at,decline_reason FROM sprint_messages "
            "WHERE message_id=?",
            (task.message_id,),
        ).fetchone()
        self.assertEqual("accepted", task_row["disposition"])
        self.assertIsNotNone(task_row["read_at"])
        self.assertIsNone(task_row["decline_reason"])

        info = self.send("read-info")
        self.assertIsNone(self.messages.mark_read(info.message_id, 1))
        info_row = self.con.execute(
            "SELECT disposition,read_at FROM sprint_messages WHERE message_id=?",
            (info.message_id,),
        ).fetchone()
        self.assertIsNone(info_row["disposition"])
        self.assertIsNotNone(info_row["read_at"])

    def test_declines_resolve_source_and_route_once_to_planner(self) -> None:
        assignment = self.send(
            "decline-assignment", kind="work_assignment", actionable=True
        )
        assignment_result = self.messages.decline(
            assignment.message_id, 1, "wrong editing lane"
        )
        review = self.send(
            "decline-review",
            kind="review_request",
            actionable=True,
            to_participant_id=self.reviewer_id,
        )
        review_result = self.messages.decline(
            review.message_id, 2, "review capacity unavailable"
        )
        self.assertEqual(
            assignment_result,
            self.messages.decline(assignment.message_id, 1, "wrong editing lane"),
        )

        declined = [
            tuple(row)
            for row in self.con.execute(
                "SELECT disposition,read_at IS NOT NULL,decline_reason "
                "FROM sprint_messages WHERE message_id IN (?,?) ORDER BY message_id",
                (assignment.message_id, review.message_id),
            )
        ]
        self.assertEqual(
            [
                ("declined", 1, "wrong editing lane"),
                ("declined", 1, "review capacity unavailable"),
            ],
            declined,
        )
        self.assertEqual(
            "planned",
            self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                (self.unit_id,),
            ).fetchone()[0],
        )
        planner_results = [
            tuple(row)
            for row in self.con.execute(
                "SELECT message_id,to_participant_id,body FROM sprint_messages "
                "WHERE message_id IN (?,?) ORDER BY message_id",
                (assignment_result, review_result),
            )
        ]
        self.assertEqual(2, len(planner_results))
        self.assertEqual(
            [self.planner_id, self.planner_id],
            [row[1] for row in planner_results],
        )
        self.assertIn("wrong editing lane", planner_results[0][2])
        self.assertIn("review capacity unavailable", planner_results[1][2])
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(DISTINCT wm.wake_id) "
                "FROM sprint_wake_messages wm "
                "WHERE wm.message_id IN (?,?)",
                (assignment_result, review_result),
            ).fetchone()[0],
        )
        self.assertEqual(
            ["cancelled", "cancelled"],
            [
                row[0]
                for row in self.con.execute(
                    "SELECT w.state FROM sprint_wake_outbox w "
                    "JOIN sprint_wake_messages wm USING (wake_id) "
                    "WHERE wm.message_id IN (?,?) ORDER BY wm.message_id",
                    (assignment.message_id, review.message_id),
                )
            ],
        )

    def test_decline_while_paused_records_passive_planner_result(self) -> None:
        assignment = self.send(
            "paused-decline", kind="work_assignment", actionable=True
        )
        self.lifecycle.transition(
            self.sprint_id,
            "paused",
            sprint_domain.LifecycleActor("participant", 1),
            reason="integrity hold",
        )

        result_id = self.messages.decline(
            assignment.message_id, 1, "cannot safely continue"
        )

        result = self.con.execute(
            "SELECT to_participant_id,body FROM sprint_messages WHERE message_id=?",
            (result_id,),
        ).fetchone()
        self.assertEqual(self.planner_id, result["to_participant_id"])
        self.assertIn("cannot safely continue", result["body"])
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_wake_messages WHERE message_id=?",
                (result_id,),
            ).fetchone()[0],
        )


class WakeDeliveryTest(SprintMessageCase):
    def test_fixed_prompt_and_target_are_durable_delivery_evidence(self) -> None:
        sent = self.send("deliver")
        observed: list[tuple[str, str, str]] = []
        service = delivery.SprintWakeDeliveryService(self.con)

        outcome = service.deliver_once(
            "worker-a",
            lambda conversation, prompt, key: (
                observed.append((conversation, prompt, key)) or "native-run-7"
            ),
        )

        self.assertEqual(
            [("cv_dev", delivery.FIXED_WAKE_PROMPT, self._wake_key(sent.wake_id))],
            observed,
        )
        self.assertEqual(
            (sent.wake_id, "delivered", 1),
            (outcome.wake_id, outcome.state, outcome.attempt_number),
        )
        attempt = self.con.execute(
            "SELECT attempt_number,target_conversation_id,native_run_ref,outcome "
            "FROM sprint_wake_attempts WHERE wake_id=?",
            (sent.wake_id,),
        ).fetchone()
        self.assertEqual((1, "cv_dev", "native-run-7", "delivered"), tuple(attempt))

    def test_messages_behind_delivering_wake_coalesce_once(self) -> None:
        first = self.send("first")
        service = delivery.SprintWakeDeliveryService(self.con)
        lease = service.claim_next("worker-a")
        self.assertEqual(first.wake_id, lease.wake_id)

        second = self.send("second")
        third = self.send("third")

        self.assertNotEqual(first.wake_id, second.wake_id)
        self.assertEqual(second.wake_id, third.wake_id)
        self.assertEqual(
            [(first.wake_id, "delivering", 1), (second.wake_id, "pending", 2)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT w.wake_id,w.state,COUNT(wm.message_id) "
                    "FROM sprint_wake_outbox w "
                    "JOIN sprint_wake_messages wm USING (wake_id) "
                    "WHERE w.wake_id IN (?,?) GROUP BY w.wake_id ORDER BY w.wake_id",
                    (first.wake_id, second.wake_id),
                )
            ],
        )

    def test_expired_claim_retries_same_identity_without_logical_duplicate(self) -> None:
        sent = self.send("crash-retry")
        clock = [datetime(2099, 7, 31, 12, 0, tzinfo=timezone.utc)]
        service = delivery.SprintWakeDeliveryService(self.con, now=lambda: clock[0])
        first = service.claim_next("worker-a", lease_seconds=5)
        invocations: list[str] = []

        # Remote enqueue succeeds, then the worker crashes before recording it.
        invocations.append(first.idempotency_key)
        clock[0] += timedelta(seconds=6)
        self.assertEqual(1, service.requeue_expired())
        outcome = service.deliver_once(
            "worker-b",
            lambda _conversation, _prompt, key: (
                invocations.append(key) or "same-native-run"
            ),
        )

        self.assertEqual([self._wake_key(sent.wake_id)] * 2, invocations)
        self.assertEqual(1, len(set(invocations)))
        self.assertEqual("delivered", outcome.state)
        self.assertEqual(
            [(1, "same-native-run", "delivered")],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT attempt_number,native_run_ref,outcome "
                    "FROM sprint_wake_attempts WHERE wake_id=?",
                    (sent.wake_id,),
                )
            ],
        )

    def test_expired_wake_is_reclaimed_before_its_pending_followup(self) -> None:
        first = self.send("expired-first")
        clock = [datetime(2099, 7, 31, 12, 0, tzinfo=timezone.utc)]
        service = delivery.SprintWakeDeliveryService(self.con, now=lambda: clock[0])
        first_lease = service.claim_next("worker-a", lease_seconds=5)
        second = self.send("pending-second")
        clock[0] += timedelta(seconds=6)

        self.assertEqual(0, service.requeue_expired())
        reclaimed = service.claim_next("worker-b", lease_seconds=5)

        self.assertEqual(first.wake_id, reclaimed.wake_id)
        self.assertEqual(self._wake_key(first.wake_id), reclaimed.idempotency_key)
        self.assertEqual(
            ("delivering", "pending"),
            tuple(
                self.con.execute(
                    "SELECT "
                    "(SELECT state FROM sprint_wake_outbox WHERE wake_id=?),"
                    "(SELECT state FROM sprint_wake_outbox WHERE wake_id=?)",
                    (first.wake_id, second.wake_id),
                ).fetchone()
            ),
        )

    def test_third_delivery_failure_auto_pauses_and_stops_claiming(self) -> None:
        sent = self.send("always-fails")
        service = delivery.SprintWakeDeliveryService(self.con)

        outcomes = [
            service.deliver_once(
                "worker-a",
                lambda _conversation, _prompt, _key: (_ for _ in ()).throw(
                    RuntimeError("provider unavailable")
                ),
            )
            for _ in range(3)
        ]

        self.assertEqual(
            [(1, "pending"), (2, "pending"), (3, "failed")],
            [(item.attempt_number, item.state) for item in outcomes],
        )
        self.assertIsNone(service.claim_next("worker-a"))
        self.assertEqual(
            ("paused", "failed", 3, None),
            tuple(
                self.con.execute(
                    "SELECT s.lifecycle,w.state,w.attempt_count,w.claim_owner "
                    "FROM sprints s JOIN sprint_wake_outbox w USING (sprint_id) "
                    "WHERE w.wake_id=?",
                    (sent.wake_id,),
                ).fetchone()
            ),
        )
        self.assertEqual(
            [(1, "failed"), (2, "failed"), (3, "failed")],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT attempt_number,outcome FROM sprint_wake_attempts "
                    "WHERE wake_id=? ORDER BY attempt_number",
                    (sent.wake_id,),
                )
            ],
        )

    def _wake_key(self, wake_id: int | None) -> str:
        return self.con.execute(
            "SELECT idempotency_key FROM sprint_wake_outbox WHERE wake_id=?",
            (wake_id,),
        ).fetchone()[0]


if __name__ == "__main__":
    unittest.main(verbosity=2)
