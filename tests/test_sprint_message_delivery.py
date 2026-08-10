#!/usr/bin/env python3
"""Stage 3 gates for dedicated Sprint messages and wake delivery."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
MIGRATIONS = ENGINE / "migrations"

sys.path.insert(0, str(ENGINE / "scripts"))
import active_chat_registry  # noqa: E402
import sprint_domain  # noqa: E402
import sprint_message_delivery as delivery  # noqa: E402


def apply_schema(con: sqlite3.Connection, *, through: str | None = None) -> None:
    con.executescript((ENGINE / "schema.sql").read_text())
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        if through is not None and migration.name > through:
            break
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")


class ForceNewMigrationTest(unittest.TestCase):
    def test_upgrade_preserves_wake_rows_shape_guards_and_identity(self) -> None:
        with closing(sqlite3.connect(":memory:")) as con:
            con.row_factory = sqlite3.Row
            apply_schema(con, through="0172_sanctioned_pause_liveness.sql")
            con.execute(
                "INSERT INTO users (user_id,username) VALUES (1,'operator')"
            )
            con.executemany(
                "INSERT INTO shells "
                "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
                "VALUES (?,?,?,?,?,1)",
                tuple(
                    (shell_id, f"Shell {shell_id}", f"SH{shell_id}", "dev", "prompt")
                    for shell_id in range(1, 7)
                ),
            )
            states = (
                (21, 1, "pending", 0, None, None, None, None, "2099-07-31 12:00:01"),
                (
                    22,
                    2,
                    "delivering",
                    1,
                    "worker-a",
                    "2099-07-31 12:00:02",
                    None,
                    None,
                    "2099-07-31 12:00:02",
                ),
                (
                    23,
                    3,
                    "delivered",
                    1,
                    None,
                    None,
                    "2099-07-31 12:00:03",
                    None,
                    "2099-07-31 12:00:03",
                ),
                (
                    24,
                    4,
                    "failed",
                    3,
                    None,
                    None,
                    None,
                    "2099-07-31 12:00:04",
                    "2099-07-31 12:00:04",
                ),
                (
                    25,
                    5,
                    "cancelled",
                    0,
                    None,
                    None,
                    None,
                    None,
                    "2099-07-31 12:00:05",
                ),
            )
            for (
                wake_id,
                shell_id,
                state,
                attempts,
                owner,
                claimed,
                delivered,
                failed,
                available,
            ) in states:
                message_id = wake_id - 10
                con.execute(
                    "INSERT INTO wake_message "
                    "(message_id,receiver_shell_id,message_kind,body,declared_type,"
                    "idempotency_key,delivered_at) "
                    "VALUES (?,?,'notification',?,?,?,?)",
                    (
                        message_id,
                        shell_id,
                        f"body-{state}",
                        "new" if wake_id % 2 else "re-enter",
                        f"message-{state}",
                        delivered,
                    ),
                )
                con.execute(
                    "INSERT INTO sprint_wake_outbox "
                    "(wake_id,receiver_shell_id,state,attempt_count,idempotency_key,"
                    "available_at,delivered_at,failed_at,claim_owner,claimed_at,"
                    "lease_expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        wake_id,
                        shell_id,
                        state,
                        attempts,
                        f"wake-{state}",
                        available,
                        delivered,
                        failed,
                        owner,
                        claimed,
                        "2099-07-31 12:01:02" if owner else None,
                    ),
                )
                con.execute(
                    "INSERT INTO sprint_wake_messages (wake_id,message_id) VALUES (?,?)",
                    (wake_id, message_id),
                )
            con.commit()

            con.executescript(
                (MIGRATIONS / "0173_force_new_wake_delivery.sql").read_text()
            )

            self.assertEqual([], con.execute("PRAGMA foreign_key_check").fetchall())
            self.assertEqual(
                [
                    "message_id",
                    "sprint_id",
                    "sender_shell_id",
                    "receiver_shell_id",
                    "from_participant_id",
                    "to_participant_id",
                    "work_unit_id",
                    "message_kind",
                    "body",
                    "declared_type",
                    "actionable",
                    "disposition",
                    "read_at",
                    "delivered_at",
                    "decline_reason",
                    "idempotency_key",
                    "created_at",
                ],
                [row[1] for row in con.execute("PRAGMA table_info(wake_message)")],
            )
            self.assertEqual(
                "quiet_since",
                con.execute("PRAGMA table_info(sprint_wake_outbox)").fetchall()[-1][1],
            )
            self.assertEqual(
                [
                    "idx_wake_message_delivery",
                    "idx_wake_message_inbox",
                    "sqlite_autoindex_wake_message_1",
                    "sqlite_autoindex_wake_message_2",
                ],
                sorted(row[1] for row in con.execute("PRAGMA index_list(wake_message)")),
            )
            self.assertEqual(
                [
                    "trg_sprint_liveness_acceptance",
                    "trg_wake_message_acceptance_insert",
                    "trg_wake_message_acceptance_update",
                ],
                sorted(
                    row[0]
                    for row in con.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='trigger' AND tbl_name='wake_message'"
                    )
                ),
            )
            self.assertEqual(
                tuple((row[2], row[3], row[4], row[8]) for row in states),
                tuple(
                    tuple(row)
                    for row in con.execute(
                        "SELECT state,attempt_count,claim_owner,available_at "
                        "FROM sprint_wake_outbox ORDER BY wake_id"
                    )
                ),
            )
            self.assertEqual(
                {"wake_message": 15, "sprint_wake_outbox": 25},
                dict(
                    con.execute(
                        "SELECT name,seq FROM sqlite_sequence "
                        "WHERE name IN ('wake_message','sprint_wake_outbox')"
                    )
                ),
            )
            con.execute(
                "INSERT INTO wake_message "
                "(receiver_shell_id,message_kind,body,declared_type,idempotency_key) "
                "VALUES (6,'notification','force body','force-new','message-force')"
            )
            self.assertEqual(
                16,
                con.execute(
                    "SELECT message_id FROM wake_message "
                    "WHERE idempotency_key='message-force'"
                ).fetchone()[0],
            )
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "INSERT INTO wake_message "
                    "(receiver_shell_id,message_kind,body,declared_type,idempotency_key) "
                    "VALUES (6,'notification','bad body','other','message-bad')"
                )
            self.assertEqual(
                0,
                con.execute(
                    "SELECT COUNT(*) FROM wake_message "
                    "WHERE idempotency_key='message-bad'"
                ).fetchone()[0],
            )


class ScopedReplyMigrationTest(unittest.TestCase):
    def test_upgrade_preserves_legacy_relays_and_enforces_reply_intent(self) -> None:
        with closing(sqlite3.connect(":memory:")) as con:
            con.row_factory = sqlite3.Row
            apply_schema(con, through="0192_reseed_sprint_pr_recovery.sql")
            con.execute("INSERT INTO users (user_id,username) VALUES (1,'operator')")
            con.execute(
                "INSERT INTO shells "
                "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
                "VALUES (1,'Developer','DEV1','dev','prompt',1)"
            )
            legacy_id = int(
                con.execute(
                    "INSERT INTO wake_message "
                    "(receiver_shell_id,message_kind,body,declared_type,idempotency_key) "
                    "VALUES (1,'notification','legacy','re-enter','legacy-relay')"
                ).lastrowid
            )
            con.commit()

            con.executescript(
                (MIGRATIONS / "0194_sprint_scoped_reply_waits.sql").read_text()
            )

            self.assertEqual([], con.execute("PRAGMA foreign_key_check").fetchall())
            self.assertEqual(
                ("information", 0, None),
                tuple(
                    con.execute(
                        "SELECT intent,requires_reply,reply_to_message_id "
                        "FROM wake_message WHERE message_id=?",
                        (legacy_id,),
                    ).fetchone()
                ),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "INSERT INTO wake_message "
                    "(receiver_shell_id,message_kind,body,declared_type,idempotency_key,"
                    "intent,requires_reply) "
                    "VALUES (1,'notification','invalid','re-enter','invalid-wait',"
                    "'information',1)"
                )
            self.assertEqual(
                0,
                con.execute(
                    "SELECT COUNT(*) FROM wake_message "
                    "WHERE idempotency_key='invalid-wait'"
                ).fetchone()[0],
            )
            valid_id = int(
                con.execute(
                    "INSERT INTO wake_message "
                    "(receiver_shell_id,message_kind,body,declared_type,idempotency_key,"
                    "intent,requires_reply) "
                    "VALUES (1,'notification','valid','re-enter','valid-wait',"
                    "'question',1)"
                ).lastrowid
            )
            con.execute(
                "INSERT INTO wake_message "
                "(receiver_shell_id,message_kind,body,declared_type,idempotency_key,"
                "reply_to_message_id) VALUES "
                "(1,'notification','reply','re-enter','valid-reply',?)",
                (valid_id,),
            )
            self.assertEqual([], con.execute("PRAGMA foreign_key_check").fetchall())


class SprintMessageCase(unittest.TestCase):
    def setUp(self) -> None:
        quiet_env = mock.patch.dict(
            os.environ, {"SC_SPRINT_FORCE_NEW_QUIET_SECONDS": "0"}
        )
        quiet_env.start()
        self.addCleanup(quiet_env.stop)
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
            "(sprint_id,shell_id,role,harness) VALUES (?,?,?,?)",
            (
                (self.sprint_id, 3, "planner", "codex"),
                (self.sprint_id, 1, "developer", "codex"),
                (self.sprint_id, 2, "reviewer", "codex"),
            ),
        )
        participants = {
            row["role"]: int(row["participant_id"])
            for row in self.con.execute(
                "SELECT role,participant_id FROM sprint_participants WHERE sprint_id=?",
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
        self.lifecycle = sprint_domain.SprintLifecycleStore(
            self.con, probe_harness=lambda _harness: None
        )
        initial_wake = self.lifecycle.arm(self.sprint_id, 3)[0]
        setup_delivery = delivery.SprintWakeDeliveryService(self.con)
        planner_delivery: list[str] = []
        setup_delivery.deliver_once(
            "setup-planner-worker",
            lambda conversation, _prompt, _key: (
                planner_delivery.append(conversation) or "setup-planner-run"
            ),
        )
        self.assertEqual(1, len(planner_delivery))
        self.messages = delivery.SprintMessageStore(self.con)
        arming_message_id = int(
            self.con.execute(
                "SELECT message_id FROM wake_message WHERE idempotency_key=?",
                (f"sprint:{self.sprint_id}:arming-model-selections",),
            ).fetchone()[0]
        )
        self.assertIsNone(self.messages.mark_read(arming_message_id, 3))
        initial_delivery: list[str] = []
        delivered = setup_delivery.deliver_once(
            "setup-worker",
            lambda conversation, _prompt, _key: (
                initial_delivery.append(conversation) or "setup-native-run"
            ),
        )
        self.assertEqual(initial_wake, delivered.wake_id)
        self.developer_conversation_id = initial_delivery[0]
        initial_message = self.con.execute(
            "SELECT message_id FROM sprint_wake_messages WHERE wake_id=?",
            (initial_wake,),
        ).fetchone()[0]
        self.assertEqual("accepted", self.messages.mark_read(initial_message, 1))

    def send(
        self,
        key: str,
        *,
        kind: str = "notification",
        actionable: bool = False,
        to_participant_id: int | None = None,
        declared_type: str = "re-enter",
    ) -> delivery.MessageReceipt:
        return self.messages.send(
            self.sprint_id,
            to_participant_id=to_participant_id or self.developer_id,
            from_participant_id=self.planner_id,
            work_unit_id=self.unit_id,
            message_kind=kind,
            body=f"body for {key}",
            actionable=actionable,
            declared_type=declared_type,
            idempotency_key=key,
        )


class MessageTransactionTest(SprintMessageCase):
    def test_message_and_active_wake_commit_or_roll_back_together(self) -> None:
        before = tuple(
            self.con.execute(
                "SELECT (SELECT COUNT(*) FROM wake_message),"
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
                    "SELECT (SELECT COUNT(*) FROM wake_message),"
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
                declared_type="re-enter",
            )
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM wake_message WHERE idempotency_key='same-key'"
            ).fetchone()[0],
        )

    def test_only_participant_handoffs_are_actionable_and_every_message_wakes(
        self,
    ) -> None:
        before = self.con.execute("SELECT COUNT(*) FROM wake_message").fetchone()[0]
        with self.assertRaises(sprint_domain.SprintInvariantError) as direct:
            self.send("bad-action", kind="system", actionable=True)
        self.con.execute("BEGIN")
        try:
            with self.assertRaises(sprint_domain.SprintInvariantError) as nested:
                self.messages.send_in_transaction(
                    self.sprint_id,
                    to_participant_id=self.developer_id,
                    message_kind="system",
                    body="bad nested action",
                    idempotency_key="bad-nested-action",
                    actionable=True,
                )
        finally:
            self.con.rollback()
        self.assertEqual(str(direct.exception), delivery.ACTIONABLE_KIND_ERROR)
        self.assertEqual(str(nested.exception), delivery.ACTIONABLE_KIND_ERROR)
        self.assertEqual(
            before,
            self.con.execute("SELECT COUNT(*) FROM wake_message").fetchone()[0],
        )

        passive = self.send("formerly-passive")
        self.assertIsNotNone(passive.wake_id)
        self.assertEqual(
            [(passive.wake_id, passive.message_id)],
            [
                tuple(row)
                for row in self.con.execute(
                "SELECT wake_id,message_id FROM sprint_wake_messages "
                "WHERE message_id=?",
                (passive.message_id,),
                )
            ],
        )


class AcceptanceAndDeclineTest(SprintMessageCase):
    def test_actionable_read_accepts_but_informational_read_does_not(self) -> None:
        task = self.send("accept-task", kind="review_request", actionable=True)
        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "invalid wake message acceptance state"
        ):
            self.con.execute(
                "UPDATE wake_message SET read_at=datetime('now') WHERE message_id=?",
                (task.message_id,),
            )
        self.con.rollback()
        self.assertEqual("accepted", self.messages.mark_read(task.message_id, 1))
        task_row = self.con.execute(
            "SELECT disposition,read_at,decline_reason FROM wake_message "
            "WHERE message_id=?",
            (task.message_id,),
        ).fetchone()
        self.assertEqual("accepted", task_row["disposition"])
        self.assertIsNotNone(task_row["read_at"])
        self.assertIsNone(task_row["decline_reason"])

        info = self.send("read-info")
        self.assertIsNone(self.messages.mark_read(info.message_id, 1))
        info_row = self.con.execute(
            "SELECT disposition,read_at FROM wake_message WHERE message_id=?",
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
                "FROM wake_message WHERE message_id IN (?,?) ORDER BY message_id",
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
            "active",
            self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                (self.unit_id,),
            ).fetchone()[0],
            "declining a duplicate assignment must not release an active lane",
        )
        planner_results = [
            tuple(row)
            for row in self.con.execute(
                "SELECT message_id,to_participant_id,body,declared_type "
                "FROM wake_message "
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
            ["re-enter", "re-enter"],
            [row[3] for row in planner_results],
        )
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

    def test_decline_while_paused_still_records_planner_wake(self) -> None:
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
            "SELECT to_participant_id,body FROM wake_message WHERE message_id=?",
            (result_id,),
        ).fetchone()
        self.assertEqual(self.planner_id, result["to_participant_id"])
        self.assertIn("cannot safely continue", result["body"])
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_wake_messages WHERE message_id=?",
                (result_id,),
            ).fetchone()[0],
        )


class ForceNewInboxDeliveryGateTest(SprintMessageCase):
    def test_undelivered_force_new_stays_unavailable_until_rotation(self) -> None:
        # The Developer's previous turn is still live and its lane is done;
        # the Planner queues the next lane as a Force-new assignment.
        pid, start_ticks = active_chat_registry.process_identity(str(os.getpid()))
        self.con.execute(
            "UPDATE active_shell_chats SET process_pid=?,process_start_ticks=? "
            "WHERE shell_id=1",
            (pid, start_ticks),
        )
        self.con.execute(
            "UPDATE sprint_work_units SET disposition='completed',"
            "completed_at=datetime('now'),updated_at=datetime('now') "
            "WHERE work_unit_id=?",
            (self.unit_id,),
        )
        next_unit_id = int(
            self.con.execute(
                "INSERT INTO sprint_work_units "
                "(sprint_id,assigned_shell_id,reviewer_shell_id,title,"
                "expected_output,disposition) "
                "VALUES (?,1,2,'Next unit','Ship it','ready')",
                (self.sprint_id,),
            ).lastrowid
        )
        self.con.commit()
        assignment = self.messages.send(
            self.sprint_id,
            to_participant_id=self.developer_id,
            from_participant_id=self.planner_id,
            work_unit_id=next_unit_id,
            message_kind="work_assignment",
            body="next lane",
            actionable=True,
            declared_type="force-new",
            idempotency_key="force-next-lane",
        )

        # The old live turn polls its inbox: the undelivered force-new stays
        # invisible, and acting on it by id is refused outright.
        self.assertEqual([], list(self.messages.inbox(self.sprint_id, 1)))
        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError, "not been delivered"
        ):
            self.messages.mark_read(assignment.message_id, 1)
        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError, "not been delivered"
        ):
            self.messages.decline(assignment.message_id, 1, "wrong lane")
        self.assertEqual(
            "ready",
            self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                (next_unit_id,),
            ).fetchone()[0],
        )
        self.assertEqual(
            "pending",
            self.con.execute(
                "SELECT state FROM sprint_wake_outbox WHERE wake_id=?",
                (assignment.wake_id,),
            ).fetchone()[0],
        )

        # Live process defers the wake; the old turn ending starts the quiet
        # gate, and the boundary rotates delivery into a fresh chat.
        clock = [datetime(2099, 7, 31, 12, 0, tzinfo=timezone.utc)]
        service = delivery.SprintWakeDeliveryService(
            self.con,
            now=lambda: clock[0],
            force_new_quiet_seconds=5,
        )
        self.assertIsNone(service.claim_next("live-turn-pulse"))
        self.con.execute(
            "UPDATE active_shell_chats SET process_pid=NULL,"
            "process_start_ticks=NULL WHERE shell_id=1"
        )
        self.con.commit()
        self.assertIsNone(service.claim_next("first-quiet-pulse"))
        clock[0] += timedelta(seconds=5)
        conversations: list[str] = []
        outcome = service.deliver_once(
            "rotation-worker",
            lambda conversation, _prompt, _key: (
                conversations.append(conversation) or "rotation-run"
            ),
        )
        self.assertEqual(assignment.wake_id, outcome.wake_id)
        self.assertEqual("delivered", outcome.state)
        self.assertEqual(1, len(conversations))
        self.assertNotEqual(self.developer_conversation_id, conversations[0])

        # Only the new run sees and accepts the assignment.
        self.assertEqual(
            [assignment.message_id],
            [
                row["message_id"]
                for row in self.messages.inbox(self.sprint_id, 1)
            ],
        )
        self.assertEqual(
            "accepted", self.messages.mark_read(assignment.message_id, 1)
        )
        self.assertEqual(
            "active",
            self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                (next_unit_id,),
            ).fetchone()[0],
        )


class WakeAvailabilityTest(SprintMessageCase):
    def test_plain_new_uses_normal_immediate_availability(self) -> None:
        sent = self.send("immediate-new", declared_type="new")
        row = self.con.execute(
            "SELECT created_at,available_at FROM sprint_wake_outbox WHERE wake_id=?",
            (sent.wake_id,),
        ).fetchone()
        self.assertEqual(row["created_at"], row["available_at"])

    def test_plain_new_coalescing_does_not_rewrite_available_at(self) -> None:
        first = self.send("pending-reenter", declared_type="re-enter")
        available_before = self.con.execute(
            "SELECT available_at FROM sprint_wake_outbox WHERE wake_id=?",
            (first.wake_id,),
        ).fetchone()[0]
        second = self.send("coalesced-new", declared_type="new")

        self.assertEqual(first.wake_id, second.wake_id)
        self.assertEqual(
            available_before,
            self.con.execute(
                "SELECT available_at FROM sprint_wake_outbox WHERE wake_id=?",
                (first.wake_id,),
            ).fetchone()[0],
        )
        self.assertEqual(
            [first.message_id, second.message_id],
            [
                int(row[0])
                for row in self.con.execute(
                    "SELECT message_id FROM sprint_wake_messages "
                    "WHERE wake_id=? ORDER BY message_id",
                    (first.wake_id,),
                )
            ],
        )

    def test_shell_scoped_plain_new_is_immediately_available(self) -> None:
        sent = self.messages.send_to_shell(
            2,
            message_kind="system",
            body="immediate shell-scoped wake",
            idempotency_key="shell-scoped-immediate-new",
            declared_type="new",
        )

        row = self.con.execute(
            "SELECT sprint_id,participant_id,receiver_shell_id,"
            "created_at,available_at "
            "FROM sprint_wake_outbox WHERE wake_id=?",
            (sent.wake_id,),
        ).fetchone()
        self.assertEqual((None, None, 2), tuple(row[:3]))
        self.assertEqual(row["created_at"], row["available_at"])


class ForceNewDeliveryTest(SprintMessageCase):
    def test_setting_accepts_zero_and_rejects_invalid_values_by_name(self) -> None:
        service = delivery.SprintWakeDeliveryService(
            self.con, force_new_quiet_seconds=0
        )
        self.assertEqual(0, service.force_new_quiet_seconds)

        for invalid in ("1.5", "not-a-number", "-1"):
            with (
                self.subTest(invalid=invalid),
                mock.patch.dict(
                    os.environ,
                    {"SC_SPRINT_FORCE_NEW_QUIET_SECONDS": invalid},
                ),
                self.assertRaisesRegex(
                    ValueError, "SC_SPRINT_FORCE_NEW_QUIET_SECONDS"
                ),
            ):
                delivery.SprintWakeDeliveryService(self.con)

    def test_python_boundaries_accept_force_new_and_reject_other_types(self) -> None:
        sprint = self.send("force-sprint", declared_type="force-new")
        shell = self.messages.send_to_shell(
            2,
            message_kind="system",
            body="force shell",
            idempotency_key="force-shell",
            declared_type="force-new",
        )
        self.assertEqual(
            [(sprint.message_id, "force-new"), (shell.message_id, "force-new")],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT message_id,declared_type FROM wake_message "
                    "WHERE message_id IN (?,?) ORDER BY message_id",
                    (sprint.message_id, shell.message_id),
                )
            ],
        )

        with self.assertRaisesRegex(ValueError, delivery.DECLARED_TYPE_ERROR):
            self.send("invalid-sprint", declared_type="other")
        with self.assertRaisesRegex(ValueError, delivery.DECLARED_TYPE_ERROR):
            self.messages.send_to_shell(
                2,
                message_kind="system",
                body="invalid shell",
                idempotency_key="invalid-shell",
                declared_type="other",
            )
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM wake_message "
                "WHERE idempotency_key IN ('invalid-sprint','invalid-shell')"
            ).fetchone()[0],
        )

    def test_live_process_clears_quiet_then_boundary_claims_without_sliding(
        self,
    ) -> None:
        pid, start_ticks = active_chat_registry.process_identity(str(os.getpid()))
        self.con.execute(
            "UPDATE active_shell_chats SET process_pid=?,process_start_ticks=? "
            "WHERE shell_id=1",
            (pid, start_ticks),
        )
        self.con.commit()
        sent = self.send("force-quiet-boundary", declared_type="force-new")
        clock = [datetime(2099, 7, 31, 12, 0, tzinfo=timezone.utc)]
        service = delivery.SprintWakeDeliveryService(
            self.con,
            now=lambda: clock[0],
            force_new_quiet_seconds=10,
        )

        self.assertIsNone(service.claim_next("live-pulse-one"))
        clock[0] += timedelta(seconds=5)
        self.assertIsNone(service.claim_next("live-pulse-two"))
        self.assertEqual(
            (None, 0),
            tuple(
                self.con.execute(
                    "SELECT quiet_since,attempt_count FROM sprint_wake_outbox "
                    "WHERE wake_id=?",
                    (sent.wake_id,),
                ).fetchone()
            ),
        )

        self.con.execute(
            "UPDATE active_shell_chats SET process_pid=NULL,"
            "process_start_ticks=NULL WHERE shell_id=1"
        )
        self.con.commit()
        self.assertIsNone(service.claim_next("first-quiet-pulse"))
        self.assertEqual(
            "2099-07-31 12:00:05",
            self.con.execute(
                "SELECT quiet_since FROM sprint_wake_outbox WHERE wake_id=?",
                (sent.wake_id,),
            ).fetchone()[0],
        )
        clock[0] += timedelta(seconds=9)
        self.assertIsNone(service.claim_next("before-boundary"))
        self.assertEqual(
            "2099-07-31 12:00:05",
            self.con.execute(
                "SELECT quiet_since FROM sprint_wake_outbox WHERE wake_id=?",
                (sent.wake_id,),
            ).fetchone()[0],
        )
        clock[0] += timedelta(seconds=1)
        lease = service.claim_next("at-boundary")
        self.assertEqual(sent.wake_id, lease.wake_id)
        self.assertEqual(("force-new",), lease.declared_types)
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_wake_attempts WHERE wake_id=?",
                (sent.wake_id,),
            ).fetchone()[0],
        )

    def test_zero_second_gate_claims_no_chat_on_first_quiet_observation(self) -> None:
        self.con.execute("DELETE FROM active_shell_chats WHERE shell_id=1")
        self.con.commit()
        sent = self.send("force-zero", declared_type="force-new")
        clock = datetime(2099, 7, 31, 12, 0, tzinfo=timezone.utc)
        lease = delivery.SprintWakeDeliveryService(
            self.con,
            now=lambda: clock,
            force_new_quiet_seconds=0,
        ).claim_next("zero-gate")

        self.assertEqual(sent.wake_id, lease.wake_id)
        self.assertEqual(
            ("delivering", "2099-07-31 12:00:00", 0),
            tuple(
                self.con.execute(
                    "SELECT state,quiet_since,attempt_count "
                    "FROM sprint_wake_outbox WHERE wake_id=?",
                    (sent.wake_id,),
                ).fetchone()
            ),
        )

    def test_stale_pid_and_service_restart_preserve_quiet_observation(self) -> None:
        self.con.execute(
            "UPDATE active_shell_chats SET process_pid=2147483647,"
            "process_start_ticks=1 WHERE shell_id=1"
        )
        self.con.commit()
        sent = self.send("force-stale-restart", declared_type="force-new")
        clock = [datetime(2099, 7, 31, 12, 0, tzinfo=timezone.utc)]
        first = delivery.SprintWakeDeliveryService(
            self.con,
            now=lambda: clock[0],
            force_new_quiet_seconds=5,
        )
        self.assertIsNone(first.claim_next("stale-first-pulse"))
        clock[0] += timedelta(seconds=4)
        restarted = delivery.SprintWakeDeliveryService(
            self.con,
            now=lambda: clock[0],
            force_new_quiet_seconds=5,
        )
        self.assertIsNone(restarted.claim_next("stale-after-restart"))
        clock[0] += timedelta(seconds=1)
        lease = restarted.claim_next("stale-boundary")
        self.assertEqual(sent.wake_id, lease.wake_id)
        self.assertEqual(
            "2099-07-31 12:00:00",
            self.con.execute(
                "SELECT quiet_since FROM sprint_wake_outbox WHERE wake_id=?",
                (sent.wake_id,),
            ).fetchone()[0],
        )

    def test_force_precedence_preserves_available_at_and_quiet_stamp(self) -> None:
        first = self.send("plain-before-force", declared_type="new")
        available_at = self.con.execute(
            "SELECT available_at FROM sprint_wake_outbox WHERE wake_id=?",
            (first.wake_id,),
        ).fetchone()[0]
        force = self.send("force-after-plain", declared_type="force-new")
        self.assertEqual(first.wake_id, force.wake_id)
        service = delivery.SprintWakeDeliveryService(
            self.con,
            now=lambda: datetime(2099, 7, 31, 12, 0, tzinfo=timezone.utc),
            force_new_quiet_seconds=10,
        )
        self.assertIsNone(service.claim_next("stamp-force-bundle"))
        quiet_since = self.con.execute(
            "SELECT quiet_since FROM sprint_wake_outbox WHERE wake_id=?",
            (first.wake_id,),
        ).fetchone()[0]
        plain = self.send("plain-joins-force", declared_type="re-enter")
        self.assertEqual(first.wake_id, plain.wake_id)
        self.assertEqual(
            (available_at, quiet_since),
            tuple(
                self.con.execute(
                    "SELECT available_at,quiet_since FROM sprint_wake_outbox "
                    "WHERE wake_id=?",
                    (first.wake_id,),
                ).fetchone()
            ),
        )

        reverse_force = self.messages.send(
            self.sprint_id,
            to_participant_id=self.reviewer_id,
            from_participant_id=self.planner_id,
            message_kind="notification",
            body="force first",
            idempotency_key="force-before-plain",
            declared_type="force-new",
        )
        reverse_plain = self.messages.send(
            self.sprint_id,
            to_participant_id=self.reviewer_id,
            from_participant_id=self.planner_id,
            message_kind="notification",
            body="plain second",
            idempotency_key="plain-after-force",
            declared_type="new",
        )
        self.assertEqual(reverse_force.wake_id, reverse_plain.wake_id)

    def test_deferred_low_id_force_does_not_starve_ready_receiver(self) -> None:
        pid, start_ticks = active_chat_registry.process_identity(str(os.getpid()))
        self.con.execute(
            "UPDATE active_shell_chats SET process_pid=?,process_start_ticks=? "
            "WHERE shell_id=1",
            (pid, start_ticks),
        )
        self.con.commit()
        blocked = self.send("blocked-force", declared_type="force-new")
        ready = self.messages.send(
            self.sprint_id,
            to_participant_id=self.reviewer_id,
            from_participant_id=self.planner_id,
            message_kind="notification",
            body="ready reviewer wake",
            idempotency_key="ready-reviewer",
            declared_type="re-enter",
        )

        lease = delivery.SprintWakeDeliveryService(
            self.con, force_new_quiet_seconds=0
        ).claim_next("starvation-scan")

        self.assertEqual(ready.wake_id, lease.wake_id)
        self.assertEqual(2, lease.receiver_shell_id)
        self.assertEqual(
            ("pending", None, 0),
            tuple(
                self.con.execute(
                    "SELECT state,quiet_since,attempt_count "
                    "FROM sprint_wake_outbox WHERE wake_id=?",
                    (blocked.wake_id,),
                ).fetchone()
            ),
        )

    def test_coordinate_mode_promotes_only_to_new(self) -> None:
        self.con.execute(
            "UPDATE sprints SET coordinate_mode=1 WHERE sprint_id=?",
            (self.sprint_id,),
        )
        self.con.commit()
        sent = self.messages.send(
            self.sprint_id,
            to_participant_id=self.planner_id,
            from_participant_id=self.developer_id,
            message_kind="notification",
            body="coordinate wake",
            idempotency_key="coordinate-reenter",
            declared_type="re-enter",
        )
        lease = delivery.SprintWakeDeliveryService(
            self.con, force_new_quiet_seconds=0
        ).claim_next("coordinate-worker")
        self.assertEqual(sent.wake_id, lease.wake_id)
        self.assertEqual(("new",), lease.declared_types)

    def test_rotation_closes_exact_chat_and_delivers_full_snapshot_once(self) -> None:
        force = self.send("rotation-force", declared_type="force-new")
        plain = self.send("rotation-plain", declared_type="re-enter")
        observed: list[tuple[str, str]] = []

        outcome = delivery.SprintWakeDeliveryService(
            self.con, force_new_quiet_seconds=0
        ).deliver_once(
            "rotation-worker",
            lambda conversation, prompt, _key: (
                observed.append((conversation, prompt)) or "rotation-run"
            ),
        )

        self.assertEqual(force.wake_id, outcome.wake_id)
        self.assertNotEqual(self.developer_conversation_id, observed[0][0])
        self.assertIn(f"wake_message #{force.message_id}", observed[0][1])
        self.assertIn(f"wake_message #{plain.message_id}", observed[0][1])
        self.assertEqual(
            ("closed", observed[0][0]),
            (
                self.con.execute(
                    "SELECT state FROM conversations WHERE conversation_id=?",
                    (self.developer_conversation_id,),
                ).fetchone()[0],
                self.con.execute(
                    "SELECT chat_id FROM active_shell_chats WHERE shell_id=1"
                ).fetchone()[0],
            ),
        )
        closed = self.con.execute(
            "SELECT payload FROM conversation_events "
            "WHERE conversation_id=? AND event_type='conversation.closed'",
            (self.developer_conversation_id,),
        ).fetchone()
        self.assertEqual(
            {
                "reason": "force-new wake delivery",
                "state": "closed",
                "wake_id": force.wake_id,
            },
            json.loads(closed[0]),
        )
        self.assertEqual(
            [(force.message_id, 1), (plain.message_id, 1)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT message_id,delivered_at IS NOT NULL FROM wake_message "
                    "WHERE message_id IN (?,?) ORDER BY message_id",
                    (force.message_id, plain.message_id),
                )
            ],
        )
        self.assertIsNone(
            self.con.execute(
                "SELECT quiet_since FROM sprint_wake_outbox WHERE wake_id=?",
                (force.wake_id,),
            ).fetchone()[0]
        )

    def test_sprint_route_preflight_failure_preserves_active_chat(self) -> None:
        self.con.execute("UPDATE shells SET user_id=NULL WHERE shell_id=3")
        self.con.commit()
        sent = self.send("force-invalid-route", declared_type="force-new")

        outcome = delivery.SprintWakeDeliveryService(
            self.con, force_new_quiet_seconds=0
        ).deliver_once(
            "route-preflight-worker",
            lambda *_args: self.fail("invalid route must not enqueue"),
        )

        self.assertEqual(
            (sent.wake_id, "pending", 1),
            (outcome.wake_id, outcome.state, outcome.attempt_number),
        )
        self.assertEqual(
            (self.developer_conversation_id, "idle"),
            tuple(
                self.con.execute(
                    "SELECT active.chat_id,c.state FROM active_shell_chats active "
                    "JOIN conversations c ON c.conversation_id=active.chat_id "
                    "WHERE active.shell_id=1"
                ).fetchone()
            ),
        )

    def test_live_race_after_claim_defers_without_attempt_or_enqueue(self) -> None:
        sent = self.send("force-live-race", declared_type="force-new")
        native = mock.Mock(return_value="must-not-run")
        service = delivery.SprintWakeDeliveryService(
            self.con, force_new_quiet_seconds=0
        )
        with mock.patch.object(
            active_chat_registry,
            "has_live_process",
            side_effect=(False, True),
        ):
            outcome = service.deliver_once("live-race-worker", native)

        self.assertEqual(
            (sent.wake_id, "pending", 0),
            (outcome.wake_id, outcome.state, outcome.attempt_number),
        )
        native.assert_not_called()
        self.assertEqual(
            ("pending", 0, None, None),
            tuple(
                self.con.execute(
                    "SELECT state,attempt_count,claim_owner,quiet_since "
                    "FROM sprint_wake_outbox WHERE wake_id=?",
                    (sent.wake_id,),
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

    def test_close_race_defers_instead_of_absorbing_winner(self) -> None:
        sent = self.send("force-close-race", declared_type="force-new")
        service = delivery.SprintWakeDeliveryService(
            self.con, force_new_quiet_seconds=0
        )
        with mock.patch.object(
            active_chat_registry,
            "close_for_wake",
            side_effect=active_chat_registry.ActiveChatBusy("replacement won"),
        ):
            outcome = service.deliver_once(
                "close-race-worker",
                lambda *_args: self.fail("raced force-new must not enqueue"),
            )
        self.assertEqual(
            (sent.wake_id, "pending", 0),
            (outcome.wake_id, outcome.state, outcome.attempt_number),
        )
        self.assertEqual(
            [],
            self.con.execute(
                "SELECT attempt_number FROM sprint_wake_attempts WHERE wake_id=?",
                (sent.wake_id,),
            ).fetchall(),
        )

    def test_creation_deferral_merges_followup_into_claimed_survivor(self) -> None:
        self.con.execute("DELETE FROM active_shell_chats WHERE shell_id=1")
        self.con.commit()
        first = self.send("force-create-race", declared_type="force-new")
        service = delivery.SprintWakeDeliveryService(
            self.con, force_new_quiet_seconds=0
        )
        lease = service.claim_next("create-race-worker")
        followup = self.send("followup-during-claim", declared_type="new")
        self.assertNotEqual(first.wake_id, followup.wake_id)

        with (
            mock.patch.object(
                delivery.sprint_participant_chats,
                "create_prepared_wake_conversation",
                side_effect=delivery.sprint_participant_chats.WakeConversationBusy(
                    "winner created"
                ),
            ),
            self.assertRaises(delivery.ForceNewDeferred),
        ):
            service._resolve_conversation(lease)
        service._defer_force_new(lease)

        self.assertEqual(
            [(first.wake_id, "pending", None, None), (followup.wake_id, "cancelled", None, None)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT wake_id,state,claim_owner,quiet_since "
                    "FROM sprint_wake_outbox WHERE wake_id IN (?,?) ORDER BY wake_id",
                    (first.wake_id, followup.wake_id),
                )
            ],
        )
        self.assertEqual(
            [(first.wake_id, first.message_id), (first.wake_id, followup.message_id)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT wake_id,message_id FROM sprint_wake_messages "
                    "WHERE message_id IN (?,?) ORDER BY message_id",
                    (first.message_id, followup.message_id),
                )
            ],
        )
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_wake_attempts WHERE wake_id=?",
                (first.wake_id,),
            ).fetchone()[0],
        )

    def test_crash_after_native_enqueue_reuses_wake_chat_and_logical_turn(self) -> None:
        sent = self.send("force-crash-retry", declared_type="force-new")
        clock = [datetime(2099, 7, 31, 12, 0, tzinfo=timezone.utc)]
        service = delivery.SprintWakeDeliveryService(
            self.con,
            now=lambda: clock[0],
            force_new_quiet_seconds=0,
        )
        lease = service.claim_next("crashing-worker", lease_seconds=60)
        first_target = service._resolve_conversation(lease)
        invocations: list[tuple[str, str]] = []

        def native(conversation: str, prompt: str, key: str) -> str:
            invocations.append((conversation, key))
            existing = self.con.execute(
                "SELECT message_id FROM conversation_messages "
                "WHERE conversation_id=? AND idempotency_key=?",
                (conversation, key),
            ).fetchone()
            if existing is not None:
                run_id = self.con.execute(
                    "SELECT run_id FROM conversation_runs "
                    "WHERE trigger_message_id=?",
                    (existing["message_id"],),
                ).fetchone()[0]
                return f"conversation-run:{run_id}"
            message_id = int(
                self.con.execute(
                    "INSERT INTO conversation_messages "
                    "(conversation_id,sender_kind,sender_ref,message_kind,body,"
                    "idempotency_key,request_hash,state,completed_at) "
                    "VALUES (?,'engine','test','prompt',?,?,?,'completed',"
                    "datetime('now'))",
                    (conversation, prompt, key, key),
                ).lastrowid
            )
            run_id = int(
                self.con.execute(
                    "INSERT INTO conversation_runs "
                    "(conversation_id,shell_id,trigger_message_id,state,"
                    "lease_owner,lease_expires_at,started_at,ended_at,exit_code) "
                    "VALUES (?,1,?,'succeeded','test','2999-01-01 00:00:00',"
                    "datetime('now'),datetime('now'),0)",
                    (conversation, message_id),
                ).lastrowid
            )
            self.con.commit()
            return f"conversation-run:{run_id}"

        first_run = native(first_target, lease.prompt, lease.idempotency_key)
        pid, start_ticks = active_chat_registry.process_identity(str(os.getpid()))
        self.con.execute(
            "UPDATE active_shell_chats SET process_pid=?,process_start_ticks=? "
            "WHERE shell_id=1 AND chat_id=?",
            (pid, start_ticks, first_target),
        )
        self.con.commit()
        clock[0] += timedelta(seconds=61)

        outcome = service.deliver_once("recovery-worker", native)

        self.assertEqual("delivered", outcome.state)
        self.assertEqual([(first_target, lease.idempotency_key)] * 2, invocations)
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM conversation_messages "
                "WHERE conversation_id=? AND idempotency_key=?",
                (first_target, lease.idempotency_key),
            ).fetchone()[0],
        )
        self.assertEqual(
            [(first_run, 1, first_target)],
            [
                (
                    f"conversation-run:{row['run_id']}",
                    row["attempt_number"],
                    row["target_conversation_id"],
                )
                for row in self.con.execute(
                    "SELECT r.run_id,a.attempt_number,a.target_conversation_id "
                    "FROM conversation_runs r JOIN sprint_wake_attempts a "
                    "ON a.native_run_ref='conversation-run:' || r.run_id "
                    "WHERE a.wake_id=?",
                    (sent.wake_id,),
                )
            ],
        )


class WakeDeliveryTest(SprintMessageCase):
    def test_live_verified_turn_forces_declared_new_to_reenter(self) -> None:
        pid, start_ticks = active_chat_registry.process_identity(str(os.getpid()))
        self.assertEqual(os.getpid(), pid)
        self.con.execute(
            "UPDATE active_shell_chats SET process_pid=?,process_start_ticks=? "
            "WHERE shell_id=1",
            (pid, start_ticks),
        )
        self.con.commit()
        before = self.con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        sent = self.send("busy-new", declared_type="new")
        observed: list[str] = []

        outcome = delivery.SprintWakeDeliveryService(self.con).deliver_once(
            "busy-worker",
            lambda conversation, _prompt, _key: (
                observed.append(conversation) or "busy-run"
            ),
        )

        self.assertEqual(sent.wake_id, outcome.wake_id)
        self.assertEqual([self.developer_conversation_id], observed)
        self.assertEqual(
            before,
            self.con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
        )
        self.assertEqual(
            "idle",
            self.con.execute(
                "SELECT state FROM conversations WHERE conversation_id=?",
                (self.developer_conversation_id,),
            ).fetchone()[0],
        )

    def test_idle_mixed_types_rotate_once_and_drain_every_body(self) -> None:
        first = self.send("mixed-reenter", declared_type="re-enter")
        second = self.send("mixed-new", declared_type="new")
        self.assertEqual(first.wake_id, second.wake_id)
        observed: list[tuple[str, str]] = []

        outcome = delivery.SprintWakeDeliveryService(self.con).deliver_once(
            "mixed-worker",
            lambda conversation, prompt, _key: (
                observed.append((conversation, prompt)) or "mixed-run"
            ),
        )

        conversation_id, prompt = observed[0]
        self.assertEqual(first.wake_id, outcome.wake_id)
        self.assertNotEqual(self.developer_conversation_id, conversation_id)
        self.assertEqual(
            "closed",
            self.con.execute(
                "SELECT state FROM conversations WHERE conversation_id=?",
                (self.developer_conversation_id,),
            ).fetchone()[0],
        )
        self.assertLess(
            prompt.index("body for mixed-reenter"), prompt.index("body for mixed-new")
        )
        self.assertEqual(
            [(first.message_id, 1), (second.message_id, 1)],
            [
                (int(row["message_id"]), row["delivered_at"] is not None)
                for row in self.con.execute(
                    "SELECT message_id,delivered_at FROM wake_message "
                    "WHERE message_id IN (?,?) ORDER BY message_id",
                    (first.message_id, second.message_id),
                )
            ],
        )

    def test_stale_registry_pid_counts_as_idle_for_new(self) -> None:
        self.con.execute(
            "UPDATE active_shell_chats SET process_pid=2147483647,"
            "process_start_ticks=1 WHERE shell_id=1"
        )
        self.con.commit()
        sent = self.send("stale-new", declared_type="new")
        observed: list[str] = []

        outcome = delivery.SprintWakeDeliveryService(self.con).deliver_once(
            "stale-worker",
            lambda conversation, _prompt, _key: (
                observed.append(conversation) or "stale-run"
            ),
        )

        self.assertEqual(sent.wake_id, outcome.wake_id)
        self.assertNotEqual(self.developer_conversation_id, observed[0])

    def test_engine_wake_message_has_optional_sprint_scope(self) -> None:
        sent = self.messages.send_to_shell(
            1,
            message_kind="system",
            body="Engine-authored receiver-shell wake.",
            idempotency_key="engine-wide-wake",
        )
        observed: list[tuple[str, str]] = []

        outcome = delivery.SprintWakeDeliveryService(self.con).deliver_once(
            "engine-worker",
            lambda conversation, prompt, _key: (
                observed.append((conversation, prompt)) or "engine-run"
            ),
        )

        self.assertEqual(sent.wake_id, outcome.wake_id)
        self.assertEqual(self.developer_conversation_id, observed[0][0])
        self.assertIn("Engine-authored receiver-shell wake.", observed[0][1])
        row = self.con.execute(
            "SELECT sprint_id,sender_shell_id,receiver_shell_id,"
            "from_participant_id,to_participant_id,declared_type,delivered_at "
            "FROM wake_message WHERE message_id=?",
            (sent.message_id,),
        ).fetchone()
        self.assertEqual((None, None, 1, None, None, "re-enter"), tuple(row)[:6])
        self.assertIsNotNone(row["delivered_at"])

    def test_armed_message_claims_foreign_paused_sprint_wake(self) -> None:
        paused = self.messages.send(
            self.sprint_id,
            to_participant_id=self.planner_id,
            message_kind="notification",
            body="paused sprint backlog",
            idempotency_key="paused-sprint-backlog",
        )
        self.con.execute(
            "UPDATE sprints SET lifecycle='paused' WHERE sprint_id=?",
            (self.sprint_id,),
        )
        feature_id = int(
            self.con.execute(
                "SELECT feature_id FROM sprints WHERE sprint_id=?",
                (self.sprint_id,),
            ).fetchone()[0]
        )
        armed_sprint_id = int(
            self.con.execute(
                "INSERT INTO sprints "
                "(feature_id,originating_planner_shell_id,merge_grant_enabled) "
                "VALUES (?,3,1)",
                (feature_id,),
            ).lastrowid
        )
        self.con.execute(
            "UPDATE sprints SET lifecycle='armed' WHERE sprint_id=?",
            (armed_sprint_id,),
        )
        armed_planner_id = int(
            self.con.execute(
                "INSERT INTO sprint_participants "
                "(sprint_id,shell_id,role,harness) VALUES (?,3,'planner','codex')",
                (armed_sprint_id,),
            ).lastrowid
        )
        self.con.commit()

        armed = self.messages.send(
            armed_sprint_id,
            to_participant_id=armed_planner_id,
            message_kind="notification",
            body="armed sprint work",
            idempotency_key="armed-sprint-work",
        )
        self.assertEqual(paused.wake_id, armed.wake_id)

        service = delivery.SprintWakeDeliveryService(self.con)
        lease = service.claim_next("cross-sprint-worker")

        self.assertIsNotNone(lease)
        self.assertEqual(paused.wake_id, lease.wake_id)
        self.assertEqual(armed_sprint_id, lease.sprint_id)
        self.assertEqual(armed_planner_id, lease.participant_id)
        self.assertEqual("planner", lease.participant_role)
        self.assertEqual((paused.message_id, armed.message_id), lease.message_ids)
        self.assertTrue(
            lease.prompt.startswith(delivery.wake_prompt(armed_sprint_id, "planner"))
        )
        self.assertIn("paused sprint backlog", lease.prompt)
        self.assertIn("armed sprint work", lease.prompt)
        conversation_id = service._resolve_conversation(lease)
        self.assertEqual(
            self.planner_id,
            self.con.execute(
                "SELECT sprint_participant_id "
                "FROM sprint_participant_conversations WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()[0],
        )

    def test_mixed_scope_terminal_failure_pauses_armed_message_sprint(self) -> None:
        engine = self.messages.send_to_shell(
            3,
            message_kind="system",
            body="engine notice before armed work",
            idempotency_key="mixed-failure-engine",
        )
        sprint = self.messages.send(
            self.sprint_id,
            to_participant_id=self.planner_id,
            message_kind="notification",
            body="armed sprint work on engine wake",
            idempotency_key="mixed-failure-sprint",
        )
        self.assertEqual(engine.wake_id, sprint.wake_id)

        def fail(_conversation: str, _prompt: str, _key: str) -> str:
            raise RuntimeError("broker unavailable")

        service = delivery.SprintWakeDeliveryService(self.con)
        for attempt in range(1, 4):
            outcome = service.deliver_once(
                f"mixed-failure-{attempt}",
                fail,
            )
            self.assertIsNotNone(outcome)
            self.assertEqual(attempt, outcome.attempt_number)
            self.con.execute(
                "UPDATE sprint_wake_outbox SET available_at=datetime('now') "
                "WHERE wake_id=? AND state='pending'",
                (engine.wake_id,),
            )
            self.con.commit()

        self.assertEqual(
            ("paused", "failed", 3),
            tuple(
                self.con.execute(
                    "SELECT s.lifecycle,w.state,w.attempt_count FROM sprints s "
                    "JOIN wake_message m ON m.sprint_id=s.sprint_id "
                    "JOIN sprint_wake_messages wm USING (message_id) "
                    "JOIN sprint_wake_outbox w USING (wake_id) "
                    "WHERE s.sprint_id=? AND m.message_id=?",
                    (self.sprint_id, sprint.message_id),
                ).fetchone()
            ),
        )
        pending = self.con.execute(
            "SELECT w.wake_id,m.body FROM sprint_wake_outbox w "
            "JOIN sprint_wake_messages wm USING (wake_id) "
            "JOIN wake_message m USING (message_id) "
            "WHERE w.receiver_shell_id=3 AND w.state='pending'"
        ).fetchall()
        self.assertEqual(1, len(pending))
        self.assertIn("wake_delivery_exhausted", pending[0]["body"])

    def test_resume_does_not_redeliver_paused_ride_along_message(self) -> None:
        paused = self.messages.send(
            self.sprint_id,
            to_participant_id=self.planner_id,
            message_kind="notification",
            body="paused ride-along body",
            idempotency_key="paused-ride-along",
        )
        self.con.execute(
            "UPDATE sprints SET lifecycle='paused' WHERE sprint_id=?",
            (self.sprint_id,),
        )
        feature_id = int(
            self.con.execute(
                "SELECT feature_id FROM sprints WHERE sprint_id=?",
                (self.sprint_id,),
            ).fetchone()[0]
        )
        armed_sprint_id = int(
            self.con.execute(
                "INSERT INTO sprints "
                "(feature_id,originating_planner_shell_id,merge_grant_enabled) "
                "VALUES (?,3,1)",
                (feature_id,),
            ).lastrowid
        )
        armed_planner_id = int(
            self.con.execute(
                "INSERT INTO sprint_participants "
                "(sprint_id,shell_id,role,harness) VALUES (?,3,'planner','codex')",
                (armed_sprint_id,),
            ).lastrowid
        )
        self.con.execute(
            "UPDATE sprints SET lifecycle='armed' WHERE sprint_id=?",
            (armed_sprint_id,),
        )
        self.con.commit()
        armed = self.messages.send(
            armed_sprint_id,
            to_participant_id=armed_planner_id,
            message_kind="notification",
            body="armed delivery trigger",
            idempotency_key="ride-along-trigger",
        )
        self.assertEqual(paused.wake_id, armed.wake_id)

        def succeed(conversation: str, prompt: str, key: str) -> str:
            message_id = int(
                self.con.execute(
                    "INSERT INTO conversation_messages "
                    "(conversation_id,sender_kind,sender_ref,message_kind,body,"
                    "idempotency_key,request_hash,state,completed_at) "
                    "VALUES (?,'engine','test','prompt',?,?,?,'completed',"
                    "datetime('now'))",
                    (conversation, prompt, key, key),
                ).lastrowid
            )
            run_id = int(
                self.con.execute(
                    "INSERT INTO conversation_runs "
                    "(conversation_id,shell_id,trigger_message_id,state,"
                    "lease_owner,lease_expires_at,started_at,ended_at,exit_code) "
                    "VALUES (?,3,?,'succeeded','test','2999-01-01 00:00:00',"
                    "datetime('now'),datetime('now'),0)",
                    (conversation, message_id),
                ).lastrowid
            )
            self.con.commit()
            return f"conversation-run:{run_id}"

        outcome = delivery.SprintWakeDeliveryService(self.con).deliver_once(
            "ride-along-worker",
            succeed,
        )
        self.assertEqual("delivered", outcome.state)
        delivered_at = self.con.execute(
            "SELECT delivered_at FROM wake_message WHERE message_id=?",
            (paused.message_id,),
        ).fetchone()[0]
        self.assertIsNotNone(delivered_at)
        self.lifecycle.pause(
            armed_sprint_id,
            sprint_domain.LifecycleActor("fnb"),
            reason="resume the ride-along sprint",
        )

        receipt = self.lifecycle.resume(
            self.sprint_id,
            sprint_domain.LifecycleActor("fnb"),
            reason="body already delivered",
        )

        self.assertEqual((), receipt.requeued_wake_ids)
        self.assertEqual(
            (delivered_at, paused.wake_id, "delivered"),
            tuple(
                self.con.execute(
                    "SELECT m.delivered_at,wm.wake_id,w.state "
                    "FROM wake_message m JOIN sprint_wake_messages wm "
                    "USING (message_id) JOIN sprint_wake_outbox w USING (wake_id) "
                    "WHERE m.message_id=?",
                    (paused.message_id,),
                ).fetchone()
            ),
        )

    def test_engine_new_rotates_idle_chat_and_delivers(self) -> None:
        sent = self.messages.send_to_shell(
            1,
            message_kind="system",
            body="rotate engine chat",
            idempotency_key="engine-new-idle",
            declared_type="new",
        )
        observed: list[str] = []

        outcome = delivery.SprintWakeDeliveryService(self.con).deliver_once(
            "engine-new-worker",
            lambda conversation, _prompt, _key: (
                observed.append(conversation) or "engine-new-run"
            ),
        )

        self.assertEqual(sent.wake_id, outcome.wake_id)
        self.assertNotEqual(self.developer_conversation_id, observed[0])
        self.assertEqual(
            "closed",
            self.con.execute(
                "SELECT state FROM conversations WHERE conversation_id=?",
                (self.developer_conversation_id,),
            ).fetchone()[0],
        )
        self.assertEqual(
            observed[0],
            self.con.execute(
                "SELECT chat_id FROM active_shell_chats WHERE shell_id=1"
            ).fetchone()[0],
        )

    def test_engine_new_without_registry_creates_chat(self) -> None:
        self.con.execute(
            "UPDATE conversations SET state='closed',closed_at=datetime('now') "
            "WHERE conversation_id=?",
            (self.developer_conversation_id,),
        )
        self.con.commit()
        sent = self.messages.send_to_shell(
            1,
            message_kind="system",
            body="create engine chat",
            idempotency_key="engine-new-no-registry",
            declared_type="new",
        )
        observed: list[str] = []

        outcome = delivery.SprintWakeDeliveryService(self.con).deliver_once(
            "engine-create-worker",
            lambda conversation, _prompt, _key: (
                observed.append(conversation) or "engine-create-run"
            ),
        )

        self.assertEqual(sent.wake_id, outcome.wake_id)
        self.assertNotEqual(self.developer_conversation_id, observed[0])
        created = self.con.execute(
            "SELECT state,conversation_scope FROM conversations "
            "WHERE conversation_id=?",
            (observed[0],),
        ).fetchone()
        self.assertEqual(("idle", "normal"), tuple(created))

    def test_engine_new_preflight_failure_preserves_active_chat(self) -> None:
        self.con.execute("UPDATE shells SET user_id=NULL WHERE shell_id=1")
        self.con.commit()
        sent = self.messages.send_to_shell(
            1,
            message_kind="system",
            body="invalid engine route",
            idempotency_key="engine-new-invalid-route",
            declared_type="new",
        )

        outcome = delivery.SprintWakeDeliveryService(self.con).deliver_once(
            "engine-invalid-worker",
            lambda *_args: self.fail("invalid route must not enqueue"),
        )

        self.assertEqual(
            (sent.wake_id, "pending", 1),
            (outcome.wake_id, outcome.state, outcome.attempt_number),
        )
        self.assertEqual(
            self.developer_conversation_id,
            self.con.execute(
                "SELECT chat_id FROM active_shell_chats WHERE shell_id=1"
            ).fetchone()[0],
        )
        self.assertEqual(
            "idle",
            self.con.execute(
                "SELECT state FROM conversations WHERE conversation_id=?",
                (self.developer_conversation_id,),
            ).fetchone()[0],
        )

    def test_engine_delivery_failures_use_three_attempt_budget(self) -> None:
        sent = self.messages.send_to_shell(
            1,
            message_kind="system",
            body="bounded engine wake",
            idempotency_key="engine-bounded-failure",
        )
        service = delivery.SprintWakeDeliveryService(self.con)

        outcomes = []
        for attempt in range(1, 4):
            outcomes.append(
                service.deliver_once(
                    f"engine-failure-{attempt}",
                    lambda *_args: (_ for _ in ()).throw(RuntimeError("offline")),
                )
            )
            self.con.execute(
                "UPDATE sprint_wake_outbox SET available_at=datetime('now') "
                "WHERE wake_id=? AND state='pending'",
                (sent.wake_id,),
            )
            self.con.commit()

        self.assertEqual(
            [
                (sent.wake_id, "pending", 1),
                (sent.wake_id, "pending", 2),
                (sent.wake_id, "failed", 3),
            ],
            [
                (outcome.wake_id, outcome.state, outcome.attempt_number)
                for outcome in outcomes
            ],
        )
        self.assertEqual(
            [
                (1, "failed", "offline"),
                (2, "failed", "offline"),
                (3, "failed", "offline"),
            ],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT attempt_number,outcome,error_detail "
                    "FROM sprint_wake_attempts WHERE wake_id=? "
                    "ORDER BY attempt_number",
                    (sent.wake_id,),
                )
            ],
        )

    def test_busy_close_race_reenters_without_failed_attempt(self) -> None:
        sent = self.send("busy-close-race", declared_type="new")
        service = delivery.SprintWakeDeliveryService(self.con)

        with mock.patch.object(
            active_chat_registry,
            "close_for_wake",
            side_effect=active_chat_registry.ActiveChatBusy("turn started"),
        ):
            outcome = service.deliver_once(
                "busy-close-worker",
                lambda conversation, _prompt, _key: conversation,
            )

        self.assertEqual(
            (sent.wake_id, "delivered", 1),
            (outcome.wake_id, outcome.state, outcome.attempt_number),
        )
        self.assertEqual(
            [(1, "delivered")],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT attempt_number,outcome FROM sprint_wake_attempts "
                    "WHERE wake_id=?",
                    (sent.wake_id,),
                )
            ],
        )

    def test_creation_race_reenters_winning_chat_without_failed_attempt(self) -> None:
        self.con.execute("DELETE FROM active_shell_chats WHERE shell_id=1")
        self.con.commit()
        sent = self.messages.send_to_shell(
            1,
            message_kind="system",
            body="creation race",
            idempotency_key="engine-creation-race",
            declared_type="new",
        )

        winner = active_chat_registry.ActiveChat(
            1, self.developer_conversation_id, "idle", None, None
        )
        with (
            mock.patch.object(
                delivery.sprint_participant_chats,
                "create_shell_wake_conversation",
                side_effect=delivery.sprint_participant_chats.WakeConversationBusy(
                    "another chat became active"
                ),
            ),
            mock.patch.object(
                active_chat_registry,
                "get",
                side_effect=(None, winner),
            ),
        ):
            outcome = delivery.SprintWakeDeliveryService(self.con).deliver_once(
                "creation-race-worker",
                lambda conversation, _prompt, _key: conversation,
            )

        self.assertEqual(
            (sent.wake_id, "delivered", 1),
            (outcome.wake_id, outcome.state, outcome.attempt_number),
        )
        self.assertEqual(
            self.developer_conversation_id,
            self.con.execute(
                "SELECT target_conversation_id FROM sprint_wake_attempts "
                "WHERE wake_id=?",
                (sent.wake_id,),
            ).fetchone()[0],
        )

    def test_claim_respects_available_at_backoff_boundary(self) -> None:
        sent = self.send("backoff-boundary")
        clock = [datetime(2099, 7, 31, 12, 0, tzinfo=timezone.utc)]
        self.con.execute(
            "UPDATE sprint_wake_outbox SET available_at='2099-07-31 12:00:15' "
            "WHERE wake_id=?",
            (sent.wake_id,),
        )
        self.con.commit()
        service = delivery.SprintWakeDeliveryService(
            self.con,
            now=lambda: clock[0],
        )

        self.assertIsNone(service.claim_next("before-backoff"))
        self.assertEqual(
            "pending",
            self.con.execute(
                "SELECT state FROM sprint_wake_outbox WHERE wake_id=?",
                (sent.wake_id,),
            ).fetchone()[0],
        )
        clock[0] += timedelta(seconds=15)
        lease = service.claim_next("at-backoff")
        self.assertIsNotNone(lease)
        self.assertEqual(sent.wake_id, lease.wake_id)
        self.assertEqual(
            "delivering",
            self.con.execute(
                "SELECT state FROM sprint_wake_outbox WHERE wake_id=?",
                (sent.wake_id,),
            ).fetchone()[0],
        )

    def test_role_aware_prompt_and_target_are_durable_delivery_evidence(self) -> None:
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
            [
                (
                    self.developer_conversation_id,
                    (
                        f"Sprint {self.sprint_id} handoff for your Developer role. "
                        "Load `sprint_dev`. Run `sc sprint inbox --sprint "
                        f"{self.sprint_id}` now and act on the Sprint message(s) using "
                        "`sprint_dev`. Confirm every Sprint write succeeds before "
                        "stopping. If a Sprint command failed or did not confirm its "
                        "durable write, retry that command. Do not re-check the inbox "
                        "otherwise — new messages arrive as their own wakes.\n\n"
                        f"## wake_message #{sent.message_id} "
                        "(declared Re-Enter)\n\nbody for deliver"
                    ),
                    self._wake_key(sent.wake_id),
                )
            ],
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
        self.assertEqual(
            (1, self.developer_conversation_id, "native-run-7", "delivered"),
            tuple(attempt),
        )

    def test_every_participant_role_receives_the_exact_general_template(self) -> None:
        expected = {
            "developer": (
                self.developer_id,
                "Developer",
                "sprint_dev",
            ),
            "reviewer": (self.reviewer_id, "Reviewer", "sprint_rev"),
            "planner": (self.planner_id, "Originating Planner", "sprint_pln"),
        }
        observed: dict[str, str] = {}
        service = delivery.SprintWakeDeliveryService(self.con)
        for role, (participant_id, label, skill) in expected.items():
            sent = self.messages.send(
                self.sprint_id,
                to_participant_id=participant_id,
                from_participant_id=(
                    self.developer_id if role != "developer" else self.planner_id
                ),
                message_kind="notification",
                body="PR #321, message 987, work unit 654, sha deadbeef",
                idempotency_key=f"role-prompt:{role}",
            )
            outcome = service.deliver_once(
                "role-worker",
                lambda _conversation, prompt, _key, role=role: (
                    observed.__setitem__(role, prompt) or f"run-{role}"
                ),
            )
            self.assertEqual("delivered", outcome.state)
            self.assertEqual(
                observed[role],
                f"Sprint {self.sprint_id} handoff for your {label} role. "
                f"Load `{skill}`. Run `sc sprint inbox --sprint {self.sprint_id}` "
                f"now and act on the Sprint message(s) using `{skill}`. Confirm "
                "every Sprint write succeeds before stopping. If a Sprint command "
                "failed or did not confirm its durable write, retry that command. "
                "Do not re-check the inbox otherwise — new messages arrive as their "
                "own wakes.\n\n"
                f"## wake_message #{sent.message_id} (declared Re-Enter)\n\n"
                "PR #321, message 987, work unit 654, sha deadbeef",
            )
            self.assertIn("PR #321", observed[role])
            self.assertIn("message 987", observed[role])
            self.assertIn("work unit 654", observed[role])
            self.assertIn("deadbeef", observed[role])

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

    def test_expired_claim_retries_same_identity_without_logical_duplicate(
        self,
    ) -> None:
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
        service.claim_next("worker-a", lease_seconds=5)
        second = self.send("pending-second")
        clock[0] += timedelta(seconds=6)

        self.assertEqual(0, service.requeue_expired())
        reclaimed = service.claim_next("worker-b", lease_seconds=5)

        self.assertEqual(first.wake_id, reclaimed.wake_id)
        self.assertEqual(self._wake_key(first.wake_id), reclaimed.idempotency_key)
        self.assertEqual(
            ("delivering", "cancelled"),
            tuple(
                self.con.execute(
                    "SELECT "
                    "(SELECT state FROM sprint_wake_outbox WHERE wake_id=?),"
                    "(SELECT state FROM sprint_wake_outbox WHERE wake_id=?)",
                    (first.wake_id, second.wake_id),
                ).fetchone()
            ),
        )
        self.assertEqual(
            [(first.wake_id, first.message_id), (first.wake_id, second.message_id)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT wake_id,message_id FROM sprint_wake_messages "
                    "WHERE message_id IN (?,?) ORDER BY message_id",
                    (first.message_id, second.message_id),
                )
            ],
        )

    def test_third_delivery_failure_auto_pauses_and_stops_claiming(self) -> None:
        sent = self.send("always-fails")
        service = delivery.SprintWakeDeliveryService(self.con)

        outcomes = []
        for _ in range(3):
            outcomes.append(
                service.deliver_once(
                    "worker-a",
                    lambda _conversation, _prompt, _key: (_ for _ in ()).throw(
                        RuntimeError("provider unavailable")
                    ),
                )
            )
            self.con.execute(
                "UPDATE sprint_wake_outbox SET available_at=datetime('now') "
                "WHERE wake_id=? AND state='pending'",
                (sent.wake_id,),
            )
            self.con.commit()

        self.assertEqual(
            [(1, "pending"), (2, "pending"), (3, "failed")],
            [(item.attempt_number, item.state) for item in outcomes],
        )
        pause_notice = service.claim_next("worker-a")
        self.assertIsNotNone(pause_notice)
        self.assertIsNone(pause_notice.sprint_id)
        self.assertIn("wake_delivery_exhausted", pause_notice.prompt)
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


class ParticipantRelayTest(SprintMessageCase):
    def test_relay_is_freeform_communication_without_workflow_mutation(self) -> None:
        before = tuple(
            self.con.execute(
                "SELECT s.lifecycle,u.disposition FROM sprints s "
                "JOIN sprint_work_units u USING (sprint_id) "
                "WHERE s.sprint_id=? AND u.work_unit_id=?",
                (self.sprint_id, self.unit_id),
            ).fetchone()
        )

        receipt = self.messages.relay(
            self.sprint_id,
            from_shell_id=1,
            to_shortname="pln1",
            body="Which acceptance criterion owns the empty-input case?",
            idempotency_key="participant-send:question-1",
        )

        message = self.con.execute(
            "SELECT m.from_participant_id,m.to_participant_id,m.work_unit_id,"
            "m.message_kind,m.body,m.actionable,m.disposition,m.read_at "
            "FROM wake_message m WHERE m.message_id=?",
            (receipt.message_id,),
        ).fetchone()
        self.assertEqual(
            (
                self.developer_id,
                self.planner_id,
                None,
                "notification",
                "Which acceptance criterion owns the empty-input case?",
                0,
                None,
                None,
            ),
            tuple(message),
        )
        self.assertEqual("pending", receipt.wake_state)
        self.assertTrue(receipt.message_created)
        self.assertEqual(
            [(receipt.wake_id, receipt.message_id)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT wake_id,message_id FROM sprint_wake_messages "
                    "WHERE message_id=?",
                    (receipt.message_id,),
                )
            ],
        )
        after = tuple(
            self.con.execute(
                "SELECT s.lifecycle,u.disposition FROM sprints s "
                "JOIN sprint_work_units u USING (sprint_id) "
                "WHERE s.sprint_id=? AND u.work_unit_id=?",
                (self.sprint_id, self.unit_id),
            ).fetchone()
        )
        self.assertEqual(before, after)

    def test_unit_and_sprint_reply_waits_persist_typed_scope(self) -> None:
        unit_wait = self.messages.relay(
            self.sprint_id,
            from_shell_id=1,
            to_shortname="PLN1",
            body="Which unit rule applies?",
            idempotency_key="participant-send:unit-wait",
            intent="question",
            requires_reply=True,
            work_unit_id=self.unit_id,
        )
        sprint_wait = self.messages.relay(
            self.sprint_id,
            from_shell_id=2,
            to_shortname="PLN1",
            body="Choose the cross-unit order.",
            idempotency_key="participant-send:sprint-wait",
            intent="decision",
            requires_reply=True,
            sprint_level=True,
        )

        rows = self.con.execute(
            "SELECT message_id,intent,requires_reply,work_unit_id,"
            "reply_to_message_id,actionable FROM wake_message "
            "WHERE message_id IN (?,?) ORDER BY message_id",
            (unit_wait.message_id, sprint_wait.message_id),
        ).fetchall()
        self.assertEqual(
            [
                (unit_wait.message_id, "question", 1, self.unit_id, None, 0),
                (sprint_wait.message_id, "decision", 1, None, None, 0),
            ],
            [tuple(row) for row in rows],
        )

    def test_reply_reverses_endpoints_and_inherits_original_scope(self) -> None:
        original = self.messages.relay(
            self.sprint_id,
            from_shell_id=1,
            to_shortname="PLN1",
            body="Need a unit ruling.",
            idempotency_key="participant-send:reply-original",
            intent="blocker",
            requires_reply=True,
            work_unit_id=self.unit_id,
        )
        reply = self.messages.relay(
            self.sprint_id,
            from_shell_id=3,
            to_shortname="DEV1",
            body="Use the bounded rule.",
            idempotency_key="participant-send:reply-answer",
            intent="information",
            reply_to_message_id=original.message_id,
        )

        self.assertEqual(
            (
                self.planner_id,
                self.developer_id,
                self.unit_id,
                "information",
                0,
                original.message_id,
            ),
            tuple(
                self.con.execute(
                    "SELECT from_participant_id,to_participant_id,work_unit_id,"
                    "intent,requires_reply,reply_to_message_id FROM wake_message "
                    "WHERE message_id=?",
                    (reply.message_id,),
                ).fetchone()
            ),
        )

    def test_reply_keeps_stored_endpoints_after_legal_replan(self) -> None:
        self.con.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (4,'Replacement Developer','DEV2','dev','prompt',1)"
        )
        replacement_id = int(
            self.con.execute(
                "INSERT INTO sprint_participants "
                "(sprint_id,shell_id,role,harness) "
                "VALUES (?,4,'developer','codex')",
                (self.sprint_id,),
            ).lastrowid
        )
        planned_unit = int(
            self.con.execute(
                "INSERT INTO sprint_work_units "
                "(sprint_id,assigned_shell_id,reviewer_shell_id,title,expected_output) "
                "VALUES (?,1,2,'Still planned','Answer then implement')",
                (self.sprint_id,),
            ).lastrowid
        )
        self.con.commit()
        original = self.messages.relay(
            self.sprint_id,
            from_shell_id=3,
            to_shortname="DEV1",
            body="Which path should this unit take?",
            idempotency_key="participant-send:replan-original",
            intent="question",
            requires_reply=True,
            work_unit_id=planned_unit,
        )

        self.assertTrue(
            sprint_domain.SprintWorkUnitStore(self.con).replan(
                self.sprint_id,
                planned_unit,
                3,
                assigned_shell_id=4,
                reviewer_shell_id=2,
                planned_wave=0,
                dependency_ids=(),
            )
        )
        reply = self.messages.relay(
            self.sprint_id,
            from_shell_id=1,
            to_shortname="PLN1",
            body="Take the bounded path.",
            idempotency_key="participant-send:replan-answer",
            reply_to_message_id=original.message_id,
        )

        before_hijack = self.con.execute(
            "SELECT COUNT(*) FROM wake_message WHERE reply_to_message_id=?",
            (original.message_id,),
        ).fetchone()[0]
        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError,
            "must reverse the original message endpoints",
        ):
            self.messages.relay(
                self.sprint_id,
                from_shell_id=4,
                to_shortname="PLN1",
                body="Replacement answer.",
                idempotency_key="participant-send:replan-hijack",
                reply_to_message_id=original.message_id,
            )
        self.assertEqual(
            before_hijack,
            self.con.execute(
                "SELECT COUNT(*) FROM wake_message WHERE reply_to_message_id=?",
                (original.message_id,),
            ).fetchone()[0],
        )
        self.assertEqual(
            [
                (
                    reply.message_id,
                    self.developer_id,
                    self.planner_id,
                    planned_unit,
                    original.message_id,
                )
            ],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT message_id,from_participant_id,to_participant_id,"
                    "work_unit_id,reply_to_message_id FROM wake_message "
                    "WHERE reply_to_message_id=?",
                    (original.message_id,),
                )
            ],
        )
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM wake_message "
                "WHERE reply_to_message_id=? AND from_participant_id=?",
                (original.message_id, replacement_id),
            ).fetchone()[0],
        )

    def test_reply_wait_validation_rejects_invalid_combinations_without_writes(self) -> None:
        before = self.con.execute("SELECT COUNT(*) FROM wake_message").fetchone()[0]
        cases = (
            (
                {"intent": "question", "requires_reply": True},
                "exactly one work-unit or Sprint-level scope",
            ),
            (
                {
                    "intent": "question",
                    "requires_reply": True,
                    "work_unit_id": self.unit_id,
                    "sprint_level": True,
                },
                "exactly one work-unit or Sprint-level scope",
            ),
            (
                {
                    "intent": "information",
                    "requires_reply": True,
                    "work_unit_id": self.unit_id,
                },
                "must use question, blocker, or decision",
            ),
            (
                {
                    "intent": "question",
                    "requires_reply": True,
                    "work_unit_id": self.unit_id + 999,
                },
                "work unit does not belong to this Sprint",
            ),
        )
        for index, (kwargs, error) in enumerate(cases):
            with self.subTest(error=error), self.assertRaisesRegex(
                sprint_domain.SprintInvariantError, error
            ):
                self.messages.relay(
                    self.sprint_id,
                    from_shell_id=1,
                    to_shortname="PLN1",
                    body="invalid wait",
                    idempotency_key=f"participant-send:invalid-wait:{index}",
                    **kwargs,
                )
        self.assertEqual(
            before,
            self.con.execute("SELECT COUNT(*) FROM wake_message").fetchone()[0],
        )

    def test_unit_scope_rejects_unrelated_participant_endpoint(self) -> None:
        self.con.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (4,'Other Developer','DEV2','dev','prompt',1)"
        )
        self.con.execute(
            "INSERT INTO sprint_participants (sprint_id,shell_id,role,harness) "
            "VALUES (?,4,'developer','codex')",
            (self.sprint_id,),
        )
        self.con.commit()
        before = self.con.execute("SELECT COUNT(*) FROM wake_message").fetchone()[0]

        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError,
            "unit-scoped message endpoint does not own this work unit",
        ):
            self.messages.relay(
                self.sprint_id,
                from_shell_id=4,
                to_shortname="PLN1",
                body="unrelated unit wait",
                idempotency_key="participant-send:unrelated-unit",
                intent="question",
                requires_reply=True,
                work_unit_id=self.unit_id,
            )
        self.assertEqual(
            before,
            self.con.execute("SELECT COUNT(*) FROM wake_message").fetchone()[0],
        )

    def test_reply_rejects_wrong_recipient_and_caller_supplied_scope(self) -> None:
        original = self.messages.relay(
            self.sprint_id,
            from_shell_id=1,
            to_shortname="PLN1",
            body="Need a Sprint ruling.",
            idempotency_key="participant-send:reply-validation-original",
            intent="decision",
            requires_reply=True,
            sprint_level=True,
        )
        before = self.con.execute("SELECT COUNT(*) FROM wake_message").fetchone()[0]
        cases = (
            ({"to_shortname": "REV1"}, "must reverse the original message endpoints"),
            (
                {"to_shortname": "DEV1", "work_unit_id": self.unit_id},
                "replies inherit scope",
            ),
            (
                {"to_shortname": "DEV1", "sprint_level": True},
                "replies inherit scope",
            ),
        )
        for index, (overrides, error) in enumerate(cases):
            with self.subTest(error=error), self.assertRaisesRegex(
                sprint_domain.SprintInvariantError, error
            ):
                self.messages.relay(
                    self.sprint_id,
                    from_shell_id=3,
                    to_shortname=overrides.pop("to_shortname"),
                    body="invalid reply",
                    idempotency_key=f"participant-send:invalid-reply:{index}",
                    reply_to_message_id=original.message_id,
                    **overrides,
                )
        self.assertEqual(
            before,
            self.con.execute("SELECT COUNT(*) FROM wake_message").fetchone()[0],
        )

    def test_relay_idempotency_includes_reply_semantics_and_scope(self) -> None:
        first = self.messages.relay(
            self.sprint_id,
            from_shell_id=1,
            to_shortname="PLN1",
            body="Stable question.",
            idempotency_key="participant-send:semantic-replay",
            intent="question",
            requires_reply=True,
            work_unit_id=self.unit_id,
        )
        replay = self.messages.relay(
            self.sprint_id,
            from_shell_id=1,
            to_shortname="PLN1",
            body="Stable question.",
            idempotency_key="participant-send:semantic-replay",
            intent="question",
            requires_reply=True,
            work_unit_id=self.unit_id,
        )
        self.assertEqual(first.message_id, replay.message_id)
        self.assertFalse(replay.message_created)
        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError, "different input"
        ):
            self.messages.relay(
                self.sprint_id,
                from_shell_id=1,
                to_shortname="PLN1",
                body="Stable question.",
                idempotency_key="participant-send:semantic-replay",
                intent="blocker",
                requires_reply=True,
                work_unit_id=self.unit_id,
            )
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM wake_message "
                "WHERE idempotency_key='participant-send:semantic-replay'"
            ).fetchone()[0],
        )

    def test_relay_reuses_a_usable_current_conversation(self) -> None:
        first = self.messages.relay(
            self.sprint_id,
            from_shell_id=1,
            to_shortname="PLN1",
            body="Create the Planner wake chat.",
            idempotency_key="participant-send:create-chat",
        )
        observed: list[str] = []
        service = delivery.SprintWakeDeliveryService(self.con)
        service.deliver_once(
            "create-route",
            lambda conversation, _prompt, _key: (
                observed.append(conversation) or "create-run"
            ),
        )
        current = observed[-1]
        before = int(
            self.con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        )

        receipt = self.messages.relay(
            self.sprint_id,
            from_shell_id=1,
            to_shortname="PLN1",
            body="A durable answer is needed.",
            idempotency_key="participant-send:reuse-chat",
        )
        outcome = service.deliver_once(
            "reuse-route",
            lambda conversation, _prompt, _key: (
                observed.append(conversation) or "reuse-run"
            ),
        )

        self.assertEqual(current, receipt.conversation_id)
        self.assertEqual(receipt.wake_id, outcome.wake_id)
        self.assertEqual(current, observed[-1])
        self.assertNotEqual(first.message_id, receipt.message_id)
        self.assertEqual(
            before,
            int(self.con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]),
        )

    def test_relay_reuses_the_planner_chat_opened_by_arming(self) -> None:
        before = int(
            self.con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        )
        active_before = self.con.execute(
            "SELECT chat_id FROM active_shell_chats WHERE shell_id=3"
        ).fetchone()[0]

        receipt = self.messages.relay(
            self.sprint_id,
            from_shell_id=1,
            to_shortname="PLN1",
            body="Please answer from a fresh route.",
            idempotency_key="participant-send:new-chat",
        )
        self.assertEqual(active_before, receipt.conversation_id)
        self.assertEqual(
            before,
            self.con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
        )
        observed: list[str] = []
        outcome = delivery.SprintWakeDeliveryService(self.con).deliver_once(
            "fresh-route",
            lambda conversation, _prompt, _key: (
                observed.append(conversation) or "fresh-run"
            ),
        )
        self.assertEqual(receipt.wake_id, outcome.wake_id)
        conversation_id = observed[0]

        route = self.con.execute(
            "SELECT c.shell_id,c.state,c.conversation_scope,"
            "pc.sprint_participant_id "
            "FROM conversations c JOIN sprint_participant_conversations pc "
            "USING (conversation_id) WHERE c.conversation_id=?",
            (conversation_id,),
        ).fetchone()
        self.assertEqual(
            before,
            self.con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
        )
        self.assertEqual(active_before, conversation_id)
        self.assertEqual(
            (3, "idle", "sprint", self.planner_id),
            tuple(route),
        )
        self.assertEqual(
            conversation_id,
            self.con.execute(
                "SELECT active.chat_id FROM sprint_participants participant "
                "JOIN active_shell_chats active "
                "ON active.shell_id=participant.shell_id "
                "WHERE participant.participant_id=?",
                (self.planner_id,),
            ).fetchone()[0],
        )
        self.assertEqual(
            "generation:"
            + str(
                self.con.execute(
                    "SELECT conversation_generation FROM sprints WHERE sprint_id=?",
                    (self.sprint_id,),
                ).fetchone()[0]
            )
            + ":wake:"
            + str(
                self.con.execute(
                    "SELECT wm.wake_id FROM sprint_wake_messages wm "
                    "JOIN wake_message m USING (message_id) "
                    "WHERE m.idempotency_key=?",
                    (f"sprint:{self.sprint_id}:arming-model-selections",),
                ).fetchone()[0]
            ),
            self.con.execute(
                "SELECT creation_idempotency_key FROM conversations "
                "WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()[0],
        )

    def test_closed_planner_reenter_uses_the_open_chat_canonical_route(self) -> None:
        original = self.con.execute(
            "SELECT c.conversation_id,c.harness,c.provider,c.model,c.effort,"
            "c.worktree FROM active_shell_chats active "
            "JOIN conversations c ON c.conversation_id=active.chat_id "
            "WHERE active.shell_id=3"
        ).fetchone()
        self.assertEqual("codex", original["harness"])
        self.assertEqual("openai", original["provider"])
        self.assertIsInstance(original["model"], str)
        self.assertNotEqual("", original["model"])
        self.assertEqual("high", original["effort"])

        with self.con:
            closed = active_chat_registry.close_for_wake(self.con, 3)
        self.assertEqual(original["conversation_id"], closed.chat_id)

        receipt = self.messages.relay(
            self.sprint_id,
            from_shell_id=1,
            to_shortname="PLN1",
            body="Planner re-entry after the originating chat closed.",
            idempotency_key="participant-send:closed-planner-reenter",
        )
        observed: list[str] = []
        outcome = delivery.SprintWakeDeliveryService(self.con).deliver_once(
            "closed-planner-reenter",
            lambda conversation, _prompt, _key: (
                observed.append(conversation) or "closed-planner-run"
            ),
        )

        self.assertEqual(receipt.wake_id, outcome.wake_id)
        self.assertEqual(1, len(observed))
        self.assertNotEqual(original["conversation_id"], observed[0])
        replacement = self.con.execute(
            "SELECT harness,provider,model,effort,worktree "
            "FROM conversations WHERE conversation_id=?",
            (observed[0],),
        ).fetchone()
        self.assertEqual(
            tuple(original[field] for field in (
                "harness", "provider", "model", "effort", "worktree"
            )),
            tuple(replacement),
        )

    def test_delivery_reroutes_when_the_created_wake_chat_closes(self) -> None:
        receipt = self.messages.relay(
            self.sprint_id,
            from_shell_id=1,
            to_shortname="PLN1",
            body="Route this wake after a closed fallback.",
            idempotency_key="participant-send:closed-fallback-route",
        )
        observed: list[str] = []
        service = delivery.SprintWakeDeliveryService(self.con)
        first = service.deliver_once(
            "closed-route-first",
            lambda conversation, _prompt, _key: (
                observed.append(conversation) or "first-run"
            ),
        )
        self.assertEqual(receipt.wake_id, first.wake_id)
        first_route = observed[-1]
        self.con.execute(
            "UPDATE conversations SET state='closed',closed_at=datetime('now') "
            "WHERE conversation_id=?",
            (first_route,),
        )
        self.con.commit()
        second_receipt = self.messages.relay(
            self.sprint_id,
            from_shell_id=1,
            to_shortname="PLN1",
            body="Route the next wake after the first chat closed.",
            idempotency_key="participant-send:closed-fallback-route:second",
        )
        second = service.deliver_once(
            "closed-route-second",
            lambda conversation, _prompt, _key: (
                observed.append(conversation) or "second-run"
            ),
        )
        self.assertEqual(second_receipt.wake_id, second.wake_id)
        self.assertNotEqual(first_route, observed[-1])

    def test_relay_replay_returns_one_message_and_one_wake(self) -> None:
        first = self.messages.relay(
            self.sprint_id,
            from_shell_id=1,
            to_shortname="PLN1",
            body="Idempotent question",
            idempotency_key="participant-send:replay",
        )
        replay = self.messages.relay(
            self.sprint_id,
            from_shell_id=1,
            to_shortname="PLN1",
            body="Idempotent question",
            idempotency_key="participant-send:replay",
        )

        self.assertEqual(first.message_id, replay.message_id)
        self.assertEqual(first.wake_id, replay.wake_id)
        self.assertFalse(replay.message_created)
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM wake_message WHERE idempotency_key=?",
                ("participant-send:replay",),
            ).fetchone()[0],
        )
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_wake_messages WHERE message_id=?",
                (first.message_id,),
            ).fetchone()[0],
        )

    def test_relay_rejects_nonparticipant_sender_without_any_write(self) -> None:
        self.con.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (4,'Outside','OUT1','dev','prompt',1)"
        )
        self.con.commit()
        before = tuple(
            self.con.execute(
                "SELECT (SELECT COUNT(*) FROM wake_message),"
                "(SELECT COUNT(*) FROM sprint_wake_outbox),"
                "(SELECT COUNT(*) FROM conversations)"
            ).fetchone()
        )

        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError,
            "sender is not a Sprint participant",
        ):
            self.messages.relay(
                self.sprint_id,
                from_shell_id=4,
                to_shortname="PLN1",
                body="should not land",
                idempotency_key="participant-send:outsider",
            )

        self.assertEqual(
            before,
            tuple(
                self.con.execute(
                    "SELECT (SELECT COUNT(*) FROM wake_message),"
                    "(SELECT COUNT(*) FROM sprint_wake_outbox),"
                    "(SELECT COUNT(*) FROM conversations)"
                ).fetchone()
            ),
        )

    def test_relay_rejects_nonparticipant_recipient_without_any_write(self) -> None:
        self.con.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (4,'Outside','OUT1','dev','prompt',1)"
        )
        self.con.commit()
        before = self.con.execute("SELECT COUNT(*) FROM wake_message").fetchone()[0]

        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError,
            "recipient is not a Sprint participant",
        ):
            self.messages.relay(
                self.sprint_id,
                from_shell_id=1,
                to_shortname="OUT1",
                body="should not land",
                idempotency_key="participant-send:bad-recipient",
            )

        self.assertEqual(
            before,
            self.con.execute("SELECT COUNT(*) FROM wake_message").fetchone()[0],
        )

    def test_relay_rejects_oversize_body_with_actual_and_maximum_counts(self) -> None:
        before = self.con.execute("SELECT COUNT(*) FROM wake_message").fetchone()[0]
        with self.assertRaisesRegex(
            ValueError,
            "Sprint message body is 8001 characters; maximum is 8000",
        ):
            self.messages.relay(
                self.sprint_id,
                from_shell_id=1,
                to_shortname="PLN1",
                body="x" * 8001,
                idempotency_key="participant-send:oversize",
            )
        self.assertEqual(
            before,
            self.con.execute("SELECT COUNT(*) FROM wake_message").fetchone()[0],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
