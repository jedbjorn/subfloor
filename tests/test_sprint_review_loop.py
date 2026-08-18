"""Stage 6 gates for the Sprints v2 Developer and Reviewer loop."""
from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path[:0] = [str(SCRIPTS), str(ROOT / "tests")]

import sprint_domain
import sprint_message_delivery
import sprint_pr_watcher
import sprint_review_loop
from test_sprint_pr_watcher import SprintPRWatcherCase, pull_request


class SprintReviewLoopCase(SprintPRWatcherCase):
    def setUp(self) -> None:
        super().setUp()
        self.reader.current = pull_request(
            checks="SUCCESS", checks_failed=False, head_sha="a" * 40
        )
        self.registered_pr_id = self.register().registered_pr_id
        initial_green = self.con.execute(
            "SELECT message_id FROM wake_message "
            "WHERE receiver_shell_id=? "
            "AND idempotency_key LIKE 'pr-transition:%' "
            "ORDER BY message_id DESC LIMIT 1",
            (1,),
        ).fetchone()
        self.messages.mark_read(int(initial_green["message_id"]), 1)
        self.loop = sprint_review_loop.SprintReviewLoopStore(
            self.con,
            repo_root=ROOT,
            reader_factory=lambda _repository: self.reader,
        )

    def request_review(self, key: str = "review-1"):
        return self.loop.request_review(
            self.sprint_id,
            self.registered_pr_id,
            1,
            readiness="Focused and full gates are green; ready for review.",
            idempotency_key=key,
        )

    def deliver_message(self, message_id: int) -> None:
        """Stamp one message delivered, mimicking wake delivery finalize."""
        self.con.execute(
            "UPDATE sprint_wake_outbox SET state='delivered',"
            "delivered_at=datetime('now') WHERE state='pending' AND wake_id IN "
            "(SELECT wake_id FROM sprint_wake_messages WHERE message_id=?)",
            (message_id,),
        )
        self.con.execute(
            "UPDATE wake_message SET delivered_at=datetime('now') "
            "WHERE message_id=? AND delivered_at IS NULL",
            (message_id,),
        )
        self.con.commit()

    def accept_review(self, message_id: int) -> None:
        self.deliver_message(message_id)
        self.assertEqual("accepted", self.messages.mark_read(message_id, 2))

    def seed_historical_expectation(self, message_id: int) -> None:
        message = self.con.execute(
            "SELECT sprint_id,to_participant_id,read_at FROM wake_message "
            "WHERE message_id=?",
            (message_id,),
        ).fetchone()
        self.assertIsNotNone(message["read_at"])
        self.con.execute(
            "INSERT INTO sprint_liveness_expectations "
            "(message_id,sprint_id,participant_id,accepted_at,last_strong_at,"
            "last_strong_key,next_evaluation_at) VALUES (?,?,?,?,?,?,?)",
            (
                message_id,
                message["sprint_id"],
                message["to_participant_id"],
                message["read_at"],
                message["read_at"],
                f"message.accepted:{message_id}",
                "2999-01-01 00:00:00",
            ),
        )
        self.con.commit()

    def approve(self, key: str = "approved-1"):
        handoff = self.request_review(f"{key}:request")
        self.accept_review(handoff.message_id)
        return self.loop.record_review(
            self.sprint_id,
            self.registered_pr_id,
            2,
            verdict="approved",
            body="No Medium-or-higher findings remain.",
            idempotency_key=key,
        )


class ReviewHandoffTest(SprintReviewLoopCase):
    def test_review_payloads_accept_8000_and_reject_8001_without_state_change(self):
        before_messages = self.con.execute(
            "SELECT COUNT(*) FROM wake_message"
        ).fetchone()[0]
        with self.assertRaisesRegex(
            ValueError,
            "readiness judgment is 8001 characters; maximum is 8000",
        ):
            self.loop.request_review(
                self.sprint_id,
                self.registered_pr_id,
                1,
                readiness="x" * 8001,
                idempotency_key="oversize-readiness",
            )
        self.assertEqual(
            ("active", before_messages),
            tuple(
                self.con.execute(
                    "SELECT u.disposition,(SELECT COUNT(*) FROM wake_message) "
                    "FROM sprint_work_units u WHERE u.work_unit_id=?",
                    (self.unit_id,),
                ).fetchone()
            ),
        )

        handoff = self.loop.request_review(
            self.sprint_id,
            self.registered_pr_id,
            1,
            readiness="x" * 8000,
            idempotency_key="bounded-readiness",
        )
        self.accept_review(handoff.message_id)
        before_judgments = self.con.execute(
            "SELECT COUNT(*) FROM sprint_judgments WHERE sprint_id=?",
            (self.sprint_id,),
        ).fetchone()[0]
        with self.assertRaisesRegex(
            ValueError,
            "review body is 8001 characters; maximum is 8000",
        ):
            self.loop.record_review(
                self.sprint_id,
                self.registered_pr_id,
                2,
                verdict="approved",
                body="x" * 8001,
                idempotency_key="oversize-review",
            )
        self.assertEqual(
            before_judgments,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_judgments WHERE sprint_id=?",
                (self.sprint_id,),
            ).fetchone()[0],
        )

        outcome = self.loop.record_review(
            self.sprint_id,
            self.registered_pr_id,
            2,
            verdict="approved",
            body="x" * 8000,
            idempotency_key="bounded-review",
        )
        self.assertTrue(outcome.created)

    def test_green_legacy_readiness_is_replaced_by_engine_locator(self):
        assignment_message_id = int(
            self.con.execute(
                "SELECT message_id FROM wake_message WHERE work_unit_id=? "
                "AND message_kind='work_assignment'",
                (self.unit_id,),
            ).fetchone()[0]
        )
        handoff = self.request_review()

        self.assertTrue(handoff.created)
        self.assertEqual(self.unit_id, handoff.work_unit_id)
        unit = self.con.execute(
            "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
            (self.unit_id,),
        ).fetchone()
        self.assertEqual("in_review", unit["disposition"])
        message = self.con.execute(
            "SELECT from_participant_id,to_participant_id,message_kind,body,"
            "declared_type,actionable,disposition FROM wake_message "
            "WHERE message_id=?",
            (handoff.message_id,),
        ).fetchone()
        locator = (
            "Submitting PR for review: https://github.com/acme/repo/pull/42; "
            f"registered Sprint PR {self.registered_pr_id}; exact head "
            f"{'a' * 40}; work unit {self.unit_id}."
        )
        self.assertEqual(
            (
                self.developer_id,
                self.reviewer_id,
                "review_request",
                locator,
                "force-new",
                1,
                "pending",
            ),
            tuple(message),
        )
        judgment = self.con.execute(
            "SELECT participant_id,kind,body FROM sprint_judgments "
            "WHERE work_unit_id=?",
            (self.unit_id,),
        ).fetchone()
        self.assertEqual(
            (
                self.developer_id,
                "decision",
                locator,
            ),
            tuple(judgment),
        )
        assignment_expectation = self.con.execute(
            "SELECT resolved_at,resolution,next_evaluation_at "
            "FROM sprint_liveness_expectations WHERE message_id=?",
            (assignment_message_id,),
        ).fetchone()
        self.assertIsNone(assignment_expectation)
        observed: list[tuple[int, str]] = []
        service = sprint_message_delivery.SprintWakeDeliveryService(self.con)
        while True:
            outcome = service.deliver_once(
                "stage6-review",
                lambda conversation, _prompt, _key: conversation,
            )
            self.assertIsNotNone(outcome)
            attempt = self.con.execute(
                "SELECT target_conversation_id FROM sprint_wake_attempts "
                "WHERE wake_id=? AND outcome='delivered'",
                (outcome.wake_id,),
            ).fetchone()
            observed.append((outcome.wake_id, str(attempt[0])))
            if outcome.wake_id == handoff.wake_id:
                break
        reviewer_chat = self.con.execute(
            "SELECT active.chat_id FROM sprint_participants participant "
            "JOIN active_shell_chats active ON active.shell_id=participant.shell_id "
            "WHERE participant.participant_id=?",
            (self.reviewer_id,),
        ).fetchone()[0]
        self.assertEqual((handoff.wake_id, reviewer_chat), observed[-1])
        self.accept_review(handoff.message_id)
        review_expectation = self.con.execute(
            "SELECT resolved_at,resolution,next_evaluation_at "
            "FROM sprint_liveness_expectations WHERE message_id=?",
            (handoff.message_id,),
        ).fetchone()
        self.assertIsNone(review_expectation)
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM wake_message "
                "WHERE message_id=? AND to_participant_id=?",
                (handoff.message_id, self.planner_id),
            ).fetchone()[0],
            "the watcher never bypasses Developer judgment to request review",
        )

    def test_resubmit_intent_generates_exact_engine_locator(self):
        handoff = self.loop.request_review(
            self.sprint_id,
            self.registered_pr_id,
            1,
            intent="resubmit",
            idempotency_key="review-resubmit",
        )

        body = self.con.execute(
            "SELECT body FROM wake_message WHERE message_id=?",
            (handoff.message_id,),
        ).fetchone()[0]
        self.assertEqual(
            "Re-submitting PR for review: "
            "https://github.com/acme/repo/pull/42; registered Sprint PR "
            f"{self.registered_pr_id}; exact head {'a' * 40}; work unit "
            f"{self.unit_id}.",
            body,
        )

    def test_review_intent_rejects_latest_and_ambiguous_legacy_input(self):
        with self.assertRaisesRegex(
            ValueError, "review intent must be submit or resubmit"
        ):
            self.loop.request_review(
                self.sprint_id,
                self.registered_pr_id,
                1,
                intent="latest",
                idempotency_key="invalid-intent",
            )
        with self.assertRaisesRegex(
            ValueError, "provide review intent or legacy readiness, not both"
        ):
            self.loop.request_review(
                self.sprint_id,
                self.registered_pr_id,
                1,
                intent="submit",
                readiness="Submitting old locator",
                idempotency_key="ambiguous-intent",
            )

    def test_unchanged_registration_replay_hands_off_without_second_wake(self):
        transitions_before = self.con.execute(
            "SELECT COUNT(*) FROM sprint_pr_transitions"
        ).fetchone()[0]
        wakes_before = self.con.execute(
            "SELECT COUNT(*) FROM wake_message"
        ).fetchone()[0]

        replay = self.register()

        self.assertFalse(replay.created)
        self.assertEqual(
            transitions_before,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_pr_transitions"
            ).fetchone()[0],
        )
        self.assertEqual(
            wakes_before,
            self.con.execute("SELECT COUNT(*) FROM wake_message").fetchone()[0],
        )
        handoff = self.loop.request_review(
            self.sprint_id,
            self.registered_pr_id,
            1,
            intent="submit",
            idempotency_key="unchanged-replay-review",
        )
        self.assertTrue(handoff.created)

    def test_non_green_and_wrong_developer_leave_no_review_evidence(self):
        self.reader.current = pull_request(
            checks="PENDING", checks_failed=False, head_sha="a" * 40
        )
        self.watcher.poll_once()
        before = self.con.execute(
            "SELECT COUNT(*) FROM wake_message"
        ).fetchone()[0]

        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError, "observed green"
        ):
            self.request_review()
        with self.assertRaisesRegex(
            sprint_domain.SprintAuthorityError, "owning Developer"
        ):
            self.loop.request_review(
                self.sprint_id,
                self.registered_pr_id,
                99,
                readiness="wrong owner",
                idempotency_key="wrong-owner",
            )

        self.assertEqual(
            before,
            self.con.execute("SELECT COUNT(*) FROM wake_message").fetchone()[0],
        )
        self.assertEqual(
            "active",
            self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                (self.unit_id,),
            ).fetchone()[0],
        )

    def test_pending_rejection_reports_latest_observation_and_live_watcher(self):
        self.con.execute(
            "INSERT INTO sprint_pr_transitions "
            "(registered_pr_id,normalized_state,transition_key,"
            "observed_head_sha,evidence,observed_at) "
            "VALUES (?,'pending','test-pending-observation',?,?,"
            "datetime('now','-3 hours'))",
            (
                self.registered_pr_id,
                "b" * 40,
                json.dumps({"checks": "PENDING"}),
            ),
        )
        self.con.execute(
            "INSERT INTO daemon_heartbeats (name,beat_at,interval_s) "
            "VALUES ('sprint-pr-watcher',datetime('now'),5) "
            "ON CONFLICT(name) DO UPDATE SET beat_at=excluded.beat_at,"
            "interval_s=excluded.interval_s"
        )
        self.con.commit()
        reads_before = list(self.reader.get_calls)
        messages_before = self.con.execute(
            "SELECT COUNT(*) FROM wake_message"
        ).fetchone()[0]

        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError,
            r"review handoff requires observed green checks "
            r"\(latest observation: pending @ bbbbbbb, 3h ago; "
            r"watcher beat \d+s ago\)",
        ):
            self.request_review()

        self.assertEqual(reads_before, self.reader.get_calls)
        self.assertEqual(
            ("active", messages_before),
            tuple(
                self.con.execute(
                    "SELECT disposition,(SELECT COUNT(*) FROM wake_message) "
                    "FROM sprint_work_units WHERE work_unit_id=?",
                    (self.unit_id,),
                ).fetchone()
            ),
        )

    def test_absent_observation_rejection_reports_stale_watcher(self):
        unobserved = self.watcher.registration.register(
            self.sprint_id,
            owner_shell_id=1,
            repository="Acme/Repo",
            pr_number=43,
            work_unit_ids=(self.unit_id,),
            notify_service=False,
        )
        self.con.execute(
            "INSERT INTO daemon_heartbeats (name,beat_at,interval_s) "
            "VALUES ('sprint-pr-watcher',datetime('now','-2 days'),5) "
            "ON CONFLICT(name) DO UPDATE SET beat_at=excluded.beat_at,"
            "interval_s=excluded.interval_s"
        )
        self.con.commit()
        reads_before = list(self.reader.get_calls)

        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError,
            "review handoff requires observed green checks "
            r"\(no observation recorded for this PR; "
            r"watcher last beat 2d ago — stale\)",
        ):
            self.loop.request_review(
                self.sprint_id,
                unobserved.registered_pr_id,
                1,
                readiness="ready",
                idempotency_key="unobserved-review",
            )

        self.assertEqual(reads_before, self.reader.get_calls)
        self.assertEqual(
            "active",
            self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                (self.unit_id,),
            ).fetchone()[0],
        )

    def test_created_rejection_reports_persistent_no_checks_and_absent_watcher(self):
        self.reader.current = pull_request(checks=None, checks_failed=False)
        self.watcher.poll_once()
        self.con.execute(
            "DELETE FROM daemon_heartbeats WHERE name='sprint-pr-watcher'"
        )
        self.con.commit()
        reads_before = list(self.reader.get_calls)

        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError,
            "review handoff requires observed green checks "
            r"\(latest observation: created @ aaaaaaa — "
            r"no checks reported on this repository; watcher never started\)",
        ):
            self.request_review()

        self.assertEqual(reads_before, self.reader.get_calls)
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM wake_message "
                "WHERE idempotency_key='review-1'"
            ).fetchone()[0],
        )

    def test_handoff_and_outcome_replay_without_duplicate_durable_facts(self):
        first = self.request_review("idempotent-request")
        replay = self.request_review("idempotent-request")
        self.assertEqual(
            (first.work_unit_id, first.message_id, first.wake_id),
            (replay.work_unit_id, replay.message_id, replay.wake_id),
        )
        self.accept_review(first.message_id)
        approved = self.loop.record_review(
            self.sprint_id,
            self.registered_pr_id,
            2,
            verdict="approved",
            body="Clean on the reviewed head.",
            idempotency_key="idempotent-approval",
        )
        approval_replay = self.loop.record_review(
            self.sprint_id,
            self.registered_pr_id,
            2,
            verdict="approved",
            body="Clean on the reviewed head.",
            idempotency_key="idempotent-approval",
        )

        self.assertFalse(replay.created)
        self.assertFalse(approval_replay.created)
        self.assertEqual(approved.message_id, approval_replay.message_id)
        self.assertEqual(approved.conversation_id, approval_replay.conversation_id)
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_liveness_expectations e "
                "JOIN wake_message m USING(message_id) WHERE m.work_unit_id=?",
                (self.unit_id,),
            ).fetchone()[0],
        )
        self.assertEqual(
            (2, 2, 0),
            tuple(
                self.con.execute(
                    "SELECT COUNT(*),"
                    "(SELECT COUNT(*) FROM sprint_judgments WHERE work_unit_id=?),"
                    "(SELECT COUNT(*) FROM pragma_table_info("
                    "'sprint_participant_conversations') "
                    "WHERE name IN ('purpose','parent_conversation_id')) "
                    "FROM wake_message WHERE idempotency_key IN (?,?)",
                    (
                        self.unit_id,
                        "idempotent-request",
                        "idempotent-approval",
                    ),
                ).fetchone()
            ),
        )


class ReviewOutcomeTest(SprintReviewLoopCase):
    def test_verdict_and_liveness_resolution_survive_post_commit_abort(self):
        handoff = self.request_review("atomic-review-request")
        self.accept_review(handoff.message_id)
        self.seed_historical_expectation(handoff.message_id)
        real_write_transaction = sprint_review_loop.db_driver.write_transaction

        @contextmanager
        def abort_after_commit(con, operation):
            with real_write_transaction(con, operation):
                yield
            raise SystemExit("simulated abort after verdict commit")

        with mock.patch.object(
            sprint_review_loop.db_driver,
            "write_transaction",
            abort_after_commit,
        ), self.assertRaisesRegex(SystemExit, "after verdict commit"):
            self.loop.record_review(
                self.sprint_id,
                self.registered_pr_id,
                2,
                verdict="approved",
                body="Atomic verdict is clean.",
                idempotency_key="atomic-review-verdict",
            )

        expectation = self.con.execute(
            "SELECT resolved_at,resolution,next_evaluation_at "
            "FROM sprint_liveness_expectations WHERE message_id=?",
            (handoff.message_id,),
        ).fetchone()
        self.assertIsNotNone(expectation["resolved_at"])
        self.assertEqual(
            ("review submitted: approved", None),
            (expectation["resolution"], expectation["next_evaluation_at"]),
        )
        self.assertEqual(
            ("merge_ready", "decision", "Atomic verdict is clean."),
            tuple(
                self.con.execute(
                    "SELECT u.disposition,j.kind,j.body "
                    "FROM sprint_work_units u JOIN sprint_judgments j "
                    "ON j.work_unit_id=u.work_unit_id "
                    "WHERE u.work_unit_id=? ORDER BY j.judgment_id DESC LIMIT 1",
                    (self.unit_id,),
                ).fetchone()
            ),
        )
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_liveness_expectations e "
                "JOIN wake_message m USING(message_id) "
                "WHERE m.work_unit_id=? AND m.message_kind='review_request' "
                "AND e.resolved_at IS NULL",
                (self.unit_id,),
            ).fetchone()[0],
        )

    def test_changes_and_approval_route_only_when_the_wake_is_delivered(self):
        first = self.request_review()
        service = sprint_message_delivery.SprintWakeDeliveryService(self.con)
        first_delivery = service.deliver_once(
            "stage6-first-review",
            lambda conversation, _prompt, _key: conversation,
        )
        self.assertEqual(first.wake_id, first_delivery.wake_id)
        self.accept_review(first.message_id)
        changed = self.loop.record_review(
            self.sprint_id,
            self.registered_pr_id,
            2,
            verdict="changes_requested",
            body="Medium: preserve the exact reviewed head.",
            idempotency_key="changes-1",
        )
        self.assertEqual(
            "re-enter",
            self.con.execute(
                "SELECT declared_type FROM wake_message WHERE message_id=?",
                (changed.message_id,),
            ).fetchone()[0],
        )
        self.assertEqual("fixing", changed.disposition)
        self.assertIsNone(changed.conversation_id)
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM pragma_table_info("
                "'sprint_participant_conversations') "
                "WHERE name IN ('purpose','parent_conversation_id')"
            ).fetchone()[0],
        )
        self.assertEqual("accepted", self.messages.mark_read(changed.message_id, 1))
        changed_expectation = self.con.execute(
            "SELECT resolved_at,resolution,next_evaluation_at "
            "FROM sprint_liveness_expectations WHERE message_id=?",
            (changed.message_id,),
        ).fetchone()
        self.assertIsNone(changed_expectation)
        assignment_expectation = self.con.execute(
            "SELECT resolution FROM sprint_liveness_expectations expectation "
            "JOIN wake_message message USING(message_id) "
            "WHERE message.work_unit_id=? AND message.message_kind='work_assignment'",
            (self.unit_id,),
        ).fetchone()
        self.assertIsNone(assignment_expectation)
        self.assertIsNone(
            self.con.execute(
                "SELECT 1 FROM sprint_liveness_expectations WHERE message_id=?",
                (first.message_id,),
            ).fetchone()
        )
        while service.deliver_once(
            "stage6-before-red",
            lambda conversation, _prompt, _key: conversation,
        ) is not None:
            pass

        self.reader.current = pull_request(
            checks="FAILURE", checks_failed=True, head_sha="b" * 40
        )
        self.watcher.poll_once()
        red_delivery: list[str] = []
        red_outcome = service.deliver_once(
            "stage6-fix-red",
            lambda conversation, _prompt, _key: (
                red_delivery.append(conversation) or "red-run"
            ),
        )
        self.assertEqual(self.developer_conversation_id, red_delivery[0])
        red_messages = self.con.execute(
            "SELECT m.message_id FROM wake_message m "
            "JOIN sprint_wake_messages wm USING(message_id) "
            "WHERE wm.wake_id=?",
            (red_outcome.wake_id,),
        ).fetchall()
        for message in red_messages:
            self.messages.mark_read(int(message["message_id"]), 1)

        self.reader.current = pull_request(
            checks="SUCCESS", checks_failed=False, head_sha="b" * 40
        )
        self.watcher.poll_once()
        first_reviewer_chat = self.con.execute(
            "SELECT active.chat_id FROM sprint_participants participant "
            "JOIN active_shell_chats active ON active.shell_id=participant.shell_id "
            "WHERE participant.participant_id=?",
            (self.reviewer_id,),
        ).fetchone()[0]
        second = self.request_review("review-2")
        changed_expectation = self.con.execute(
            "SELECT resolution,next_evaluation_at "
            "FROM sprint_liveness_expectations WHERE message_id=?",
            (changed.message_id,),
        ).fetchone()
        self.assertIsNone(changed_expectation)
        second_delivery: list[str] = []
        while True:
            outcome = service.deliver_once(
                "stage6-second-review",
                lambda conversation, _prompt, _key: (
                    second_delivery.append(conversation) or "second-review-run"
                ),
            )
            self.assertIsNotNone(outcome)
            if outcome.wake_id == second.wake_id:
                break
        self.assertNotEqual(first_reviewer_chat, second_delivery[-1])
        self.accept_review(second.message_id)
        approved = self.loop.record_review(
            self.sprint_id,
            self.registered_pr_id,
            2,
            verdict="approved",
            body="The fix closes the Medium finding.",
            idempotency_key="approved-2",
        )

        self.assertEqual("merge_ready", approved.disposition)
        self.assertIsNone(approved.conversation_id)
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM pragma_table_info("
                "'sprint_participant_conversations') "
                "WHERE name IN ('purpose','parent_conversation_id')"
            ).fetchone()[0],
        )
        self.assertEqual(
            self.developer_conversation_id,
            self.con.execute(
                "SELECT active.chat_id FROM sprint_participants participant "
                "JOIN active_shell_chats active "
                "ON active.shell_id=participant.shell_id "
                "WHERE participant.participant_id=?",
                (self.developer_id,),
            ).fetchone()[0],
        )
        self.assertIsNone(
            self.con.execute(
                "SELECT 1 FROM sprint_liveness_expectations WHERE message_id=?",
                (second.message_id,),
            ).fetchone()
        )
        self.assertEqual(
            [("issue", "Medium: preserve the exact reviewed head."),
             ("decision", "The fix closes the Medium finding.")],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT kind,body FROM sprint_judgments "
                    "WHERE participant_id=? ORDER BY judgment_id",
                    (self.reviewer_id,),
                )
            ],
        )

    def test_in_review_same_head_rejects_a_second_handoff_key(self):
        first = self.request_review("first-review-key")

        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError,
            "review handoff already targets the current PR head",
        ):
            self.request_review("different-review-key")

        self.assertEqual(
            [(first.message_id, "first-review-key")],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT message_id,idempotency_key FROM wake_message "
                    "WHERE work_unit_id=? AND message_kind='review_request'",
                    (self.unit_id,),
                )
            ],
        )
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_events "
                "WHERE event_type='review.requested'"
            ).fetchone()[0],
        )

    def test_request_review_repairs_a_pre_update_stale_in_review_lane(self):
        stale = self.request_review("stale-review-key")
        self.accept_review(stale.message_id)
        self.seed_historical_expectation(stale.message_id)
        self.con.execute(
            "INSERT INTO sprint_pr_transitions "
            "(registered_pr_id,normalized_state,transition_key,"
            "observed_head_sha,evidence) VALUES (?,?,?,?,?)",
            (
                self.registered_pr_id,
                "green",
                "historical-head-change",
                "b" * 40,
                json.dumps(
                    {
                        "base_sha": "c" * 40,
                        "checks": "SUCCESS",
                        "checks_failed": False,
                    },
                    sort_keys=True,
                ),
            ),
        )
        self.con.commit()

        refreshed = self.request_review("replacement-review-key")

        self.assertTrue(refreshed.created)
        self.assertEqual(
            (
                "in_review",
                (
                    "Submitting PR for review: "
                    "https://github.com/acme/repo/pull/42; registered Sprint PR "
                    f"{self.registered_pr_id}; exact head {'b' * 40}; work unit "
                    f"{self.unit_id}."
                ),
            ),
            tuple(
                self.con.execute(
                    "SELECT unit.disposition,message.body "
                    "FROM sprint_work_units unit JOIN wake_message message "
                    "ON message.work_unit_id=unit.work_unit_id "
                    "WHERE unit.work_unit_id=? AND message.message_id=?",
                    (self.unit_id, refreshed.message_id),
                ).fetchone()
            ),
        )
        stale_expectation = self.con.execute(
            "SELECT resolution,next_evaluation_at "
            "FROM sprint_liveness_expectations WHERE message_id=?",
            (stale.message_id,),
        ).fetchone()
        self.assertEqual(
            ("review request invalidated by PR head change", None),
            tuple(stale_expectation),
        )
        invalidated = json.loads(
            self.con.execute(
                "SELECT payload FROM sprint_events "
                "WHERE event_type='review.request_invalidated'"
            ).fetchone()[0]
        )
        self.assertEqual(
            (stale.message_id, "a" * 40, "b" * 40),
            (
                invalidated["invalidated_message_id"],
                invalidated["previous_head_sha"],
                invalidated["head_sha"],
            ),
        )
        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError, "accepted request"
        ):
            self.loop.record_review(
                self.sprint_id,
                self.registered_pr_id,
                2,
                verdict="approved",
                body="The obsolete acceptance must not authorize this head.",
                idempotency_key="premature-refreshed-approval",
            )
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM wake_message "
                "WHERE idempotency_key='premature-refreshed-approval'"
            ).fetchone()[0],
        )

    def test_outcome_requires_assigned_reviewer_and_accepted_request(self):
        handoff = self.request_review()
        before = self.con.execute(
            "SELECT COUNT(*) FROM sprint_participant_conversations"
        ).fetchone()[0]
        with self.assertRaisesRegex(
            sprint_domain.SprintAuthorityError, "assigned Reviewer"
        ):
            self.loop.record_review(
                self.sprint_id,
                self.registered_pr_id,
                99,
                verdict="approved",
                body="wrong reviewer",
                idempotency_key="wrong-reviewer",
            )
        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError, "accepted request"
        ):
            self.loop.record_review(
                self.sprint_id,
                self.registered_pr_id,
                2,
                verdict="approved",
                body="not accepted",
                idempotency_key="unaccepted",
            )
        self.assertEqual(
            before,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_participant_conversations"
            ).fetchone()[0],
        )
        self.assertEqual("pending", self.con.execute(
            "SELECT disposition FROM wake_message WHERE message_id=?",
            (handoff.message_id,),
        ).fetchone()[0])

    def test_old_acceptance_does_not_authorize_a_new_pending_review_cycle(self):
        first = self.request_review()
        self.accept_review(first.message_id)
        changed = self.loop.record_review(
            self.sprint_id,
            self.registered_pr_id,
            2,
            verdict="changes_requested",
            body="Medium: update the implementation.",
            idempotency_key="changes-before-new-request",
        )
        self.messages.mark_read(changed.message_id, 1)
        self.reader.current = pull_request(
            checks="FAILURE", checks_failed=True, head_sha="b" * 40
        )
        self.watcher.poll_once()
        red = self.con.execute(
            "SELECT message_id FROM wake_message WHERE receiver_shell_id=? "
            "AND idempotency_key LIKE 'pr-transition:%' "
            "ORDER BY message_id DESC LIMIT 1",
            (1,),
        ).fetchone()
        self.messages.mark_read(int(red["message_id"]), 1)
        self.reader.current = pull_request(
            checks="SUCCESS", checks_failed=False, head_sha="b" * 40
        )
        self.watcher.poll_once()
        pending = self.request_review("review-pending-second-cycle")

        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError, "accepted request"
        ):
            self.loop.record_review(
                self.sprint_id,
                self.registered_pr_id,
                2,
                verdict="approved",
                body="must not skip second acceptance",
                idempotency_key="premature-second-approval",
            )

        self.assertEqual(
            "pending",
            self.con.execute(
                "SELECT disposition FROM wake_message WHERE message_id=?",
                (pending.message_id,),
            ).fetchone()[0],
        )
        self.assertEqual(
            "in_review",
            self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                (self.unit_id,),
            ).fetchone()[0],
        )

    def test_approval_rejects_a_head_that_changed_after_review_acceptance(self):
        handoff = self.request_review()
        self.accept_review(handoff.message_id)
        self.reader.current = pull_request(
            checks="PENDING", checks_failed=False, head_sha="b" * 40
        )
        self.watcher.poll_once()

        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError,
            "review verdict requires an accepted request",
        ):
            self.loop.record_review(
                self.sprint_id,
                self.registered_pr_id,
                2,
                verdict="approved",
                body="must re-request on the changed head",
                idempotency_key="stale-review-head",
            )

        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM wake_message "
                "WHERE idempotency_key='stale-review-head'"
            ).fetchone()[0],
        )
        self.assertEqual(
            ("declined", "superseded by PR head change"),
            tuple(
                self.con.execute(
                    "SELECT disposition,decline_reason FROM wake_message "
                    "WHERE message_id=?",
                    (handoff.message_id,),
                ).fetchone()
            ),
        )
        self.assertEqual(
            "fixing",
            self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                (self.unit_id,),
            ).fetchone()[0],
        )
        self.assertEqual(
            self.developer_conversation_id,
            self.con.execute(
                "SELECT active.chat_id FROM sprint_participants participant "
                "JOIN active_shell_chats active "
                "ON active.shell_id=participant.shell_id "
                "WHERE participant.participant_id=?",
                (self.developer_id,),
            ).fetchone()[0],
        )


class MergeGateAndAdvanceTest(SprintReviewLoopCase):
    def test_merge_gate_rechecks_live_green_and_exact_approved_head(self):
        approved = self.approve()
        authorization = self.loop.authorize_merge(
            self.sprint_id, self.registered_pr_id, 1
        )
        self.assertEqual((42, "a" * 40), (authorization.pr_number, authorization.head_sha))
        authorized = json.loads(
            self.con.execute(
                "SELECT payload FROM sprint_events "
                "WHERE event_type='merge.authorized'"
            ).fetchone()[0]
        )
        self.assertEqual("c" * 40, authorized["base_sha"])

        self.reader.current = pull_request(
            checks="PENDING", checks_failed=False, head_sha="a" * 40
        )
        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError, "green at merge time"
        ):
            self.loop.authorize_merge(self.sprint_id, self.registered_pr_id, 1)

        self.reader.current = pull_request(
            checks="SUCCESS", checks_failed=False, head_sha="b" * 40
        )
        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError, "approval is stale"
        ):
            self.loop.authorize_merge(self.sprint_id, self.registered_pr_id, 1)
        self.assertEqual(
            "merge_ready",
            self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                (approved.work_unit_id,),
            ).fetchone()[0],
        )

    def test_moved_base_refuses_authorization_and_wakes_developer_once(self):
        approved = self.approve()
        self.reader.current = pull_request(
            checks="SUCCESS",
            checks_failed=False,
            head_sha="a" * 40,
            base_sha="d" * 40,
        )

        for _ in range(2):
            with self.assertRaisesRegex(
                sprint_domain.SprintInvariantError,
                "approved base .* differs from live base",
            ):
                self.loop.authorize_merge(self.sprint_id, self.registered_pr_id, 1)

        wake = self.con.execute(
            "SELECT sprint_id,to_participant_id,work_unit_id,message_kind,body,"
            "actionable,disposition,declared_type FROM wake_message "
            "WHERE idempotency_key LIKE 'merge-base-stale:%'"
        ).fetchall()
        self.assertEqual(1, len(wake))
        self.assertEqual(
            (
                self.sprint_id,
                self.developer_id,
                self.unit_id,
                "notification",
                "Merge authorization refused for PR #42: approved base "
                + "c" * 40
                + " differs from live base "
                + "d" * 40
                + "; sync with base and return through green review.",
                1,
                "pending",
                "re-enter",
            ),
            tuple(wake[0]),
        )
        self.assertEqual(
            ("merge_ready", 0),
            tuple(
                self.con.execute(
                    "SELECT disposition,(SELECT COUNT(*) FROM sprint_events "
                    "WHERE event_type='merge.authorized') "
                    "FROM sprint_work_units WHERE work_unit_id=?",
                    (approved.work_unit_id,),
                ).fetchone()
            ),
        )

    def test_legacy_missing_base_evidence_refuses_then_converges_after_rebase(self):
        self.reader.current = pull_request(
            checks="FAILURE", checks_failed=True, base_sha=None
        )
        self.assertTrue(self.watcher.poll_once())
        self.reader.current = pull_request(
            checks="SUCCESS", checks_failed=False, base_sha=None
        )
        self.assertTrue(self.watcher.poll_once())
        self.approve()
        self.reader.current = pull_request(checks="SUCCESS", checks_failed=False)

        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError, "approval predates base-tracking"
        ):
            self.loop.authorize_merge(self.sprint_id, self.registered_pr_id, 1)

        legacy_wake = self.con.execute(
            "SELECT message_id,body FROM wake_message "
            "WHERE idempotency_key LIKE 'merge-base-stale:%:legacy:%'"
        ).fetchone()
        self.assertEqual(
            "Merge authorization refused for PR #42: approval predates "
            "base-tracking; sync with base to mint current evidence.",
            legacy_wake["body"],
        )
        self.assertEqual("accepted", self.messages.mark_read(legacy_wake["message_id"], 1))

        self.reader.current = pull_request(
            checks="SUCCESS",
            checks_failed=False,
            head_sha="b" * 40,
            base_sha="d" * 40,
        )
        self.assertTrue(self.watcher.poll_once())
        self.assertEqual(
            "fixing",
            self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                (self.unit_id,),
            ).fetchone()[0],
        )
        invalidation = json.loads(
            self.con.execute(
                "SELECT payload FROM sprint_events "
                "WHERE event_type='review.approval_invalidated'"
            ).fetchone()[0]
        )
        self.assertEqual(("a" * 40, "b" * 40), (
            invalidation["previous_head_sha"], invalidation["head_sha"]
        ))

        handoff = self.request_review("legacy-base-review")
        self.accept_review(handoff.message_id)
        outcome = self.loop.record_review(
            self.sprint_id,
            self.registered_pr_id,
            2,
            verdict="approved",
            body="Rebased head and current base evidence are clean.",
            idempotency_key="legacy-base-approved",
        )
        self.assertEqual("merge_ready", outcome.disposition)
        authorization = self.loop.authorize_merge(
            self.sprint_id, self.registered_pr_id, 1
        )
        self.assertEqual("b" * 40, authorization.head_sha)
        evidence = json.loads(
            self.con.execute(
                "SELECT evidence FROM sprint_pr_transitions "
                "WHERE registered_pr_id=? ORDER BY transition_id DESC LIMIT 1",
                (self.registered_pr_id,),
            ).fetchone()[0]
        )
        self.assertEqual("d" * 40, evidence["base_sha"])

    def test_same_state_head_move_returns_readiness_judgment_to_developer(self):
        approved = self.approve()
        self.reader.current = pull_request(
            checks="SUCCESS", checks_failed=False, head_sha="b" * 40
        )

        self.assertTrue(self.watcher.poll_once())

        self.assertEqual(
            [("green", "a" * 40), ("green", "b" * 40)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT normalized_state,observed_head_sha "
                    "FROM sprint_pr_transitions ORDER BY transition_id"
                )
            ],
        )
        self.assertEqual(
            "fixing",
            self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                (self.unit_id,),
            ).fetchone()[0],
        )
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM wake_message "
                "WHERE idempotency_key LIKE 'pr-head-change:%:delta-review'"
            ).fetchone()[0],
        )
        owner_event = self.con.execute(
            "SELECT receiver_shell_id,body FROM wake_message "
            "WHERE idempotency_key LIKE 'pr-transition:%' "
            "ORDER BY message_id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(1, owner_event["receiver_shell_id"])
        self.assertIn("event=green", owner_event["body"])
        self.assertIn("judge readiness", owner_event["body"])
        invalidated = json.loads(
            self.con.execute(
                "SELECT payload FROM sprint_events "
                "WHERE event_type='review.approval_invalidated'"
            ).fetchone()[0]
        )
        self.assertEqual("a" * 40, invalidated["previous_head_sha"])
        self.assertEqual("b" * 40, invalidated["head_sha"])
        self.assertEqual(approved.message_id, invalidated["invalidated_message_id"])
        self.assertIsNotNone(
            self.con.execute(
                "SELECT read_at FROM wake_message WHERE message_id=?",
                (approved.message_id,),
            ).fetchone()[0]
        )

        handoff = self.request_review("review-after-head-move")
        self.accept_review(handoff.message_id)
        outcome = self.loop.record_review(
            self.sprint_id,
            self.registered_pr_id,
            2,
            verdict="approved",
            body="Delta review is clean on the replacement head.",
            idempotency_key="approved-after-head-move",
        )
        self.assertEqual("merge_ready", outcome.disposition)
        self.assertEqual(
            "b" * 40,
            self.loop.authorize_merge(
                self.sprint_id, self.registered_pr_id, 1
            ).head_sha,
        )

    def test_head_move_invalidates_an_active_review_request(self):
        stale = self.request_review("review-before-head-move")
        self.reader.current = pull_request(
            checks="SUCCESS", checks_failed=False, head_sha="b" * 40
        )

        self.assertTrue(self.watcher.poll_once())

        self.assertEqual(
            "fixing",
            self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                (self.unit_id,),
            ).fetchone()[0],
        )
        self.assertEqual(
            (1, "declined", "superseded by PR head change", "cancelled"),
            tuple(
                self.con.execute(
                    "SELECT message.read_at IS NOT NULL,message.disposition,"
                    "message.decline_reason,outbox.state "
                    "FROM wake_message message "
                    "JOIN sprint_wake_messages link USING(message_id) "
                    "JOIN sprint_wake_outbox outbox USING(wake_id) "
                    "WHERE message.message_id=?",
                    (stale.message_id,),
                ).fetchone()
            ),
        )
        invalidated = json.loads(
            self.con.execute(
                "SELECT payload FROM sprint_events "
                "WHERE event_type='review.request_invalidated'"
            ).fetchone()[0]
        )
        self.assertEqual(
            (
                stale.message_id,
                self.registered_pr_id,
                self.unit_id,
                "a" * 40,
                "b" * 40,
            ),
            (
                invalidated["invalidated_message_id"],
                invalidated["registered_pr_id"],
                invalidated["work_unit_id"],
                invalidated["previous_head_sha"],
                invalidated["head_sha"],
            ),
        )
        refreshed = self.request_review("review-after-active-head-move")
        self.assertEqual(
            (
                "in_review",
                (
                    "Submitting PR for review: "
                    "https://github.com/acme/repo/pull/42; registered Sprint PR "
                    f"{self.registered_pr_id}; exact head {'b' * 40}; work unit "
                    f"{self.unit_id}."
                ),
            ),
            tuple(
                self.con.execute(
                    "SELECT unit.disposition,message.body "
                    "FROM sprint_work_units unit JOIN wake_message message "
                    "ON message.work_unit_id=unit.work_unit_id "
                    "WHERE unit.work_unit_id=? AND message.message_id=?",
                    (self.unit_id, refreshed.message_id),
                ).fetchone()
            ),
        )
        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError, "accepted request"
        ):
            self.loop.record_review(
                self.sprint_id,
                self.registered_pr_id,
                2,
                verdict="approved",
                body="The stale review acceptance cannot cross the head change.",
                idempotency_key="stale-acceptance-after-head-move",
            )
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM wake_message "
                "WHERE idempotency_key='stale-acceptance-after-head-move'"
            ).fetchone()[0],
        )

    def test_merged_head_move_completes_without_requesting_dead_delta_review(self):
        self.approve()
        self.reader.current = pull_request(
            state="MERGED",
            checks="SUCCESS",
            checks_failed=False,
            head_sha="b" * 40,
        )

        self.assertTrue(self.watcher.poll_once())

        self.assertEqual(
            "completed",
            self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                (self.unit_id,),
            ).fetchone()[0],
        )
        self.assertEqual(
            (0, 0, 0),
            tuple(
                self.con.execute(
                    "SELECT "
                    "(SELECT COUNT(*) FROM sprint_events "
                    "WHERE event_type='review.approval_invalidated'),"
                    "(SELECT COUNT(*) FROM wake_message "
                    "WHERE idempotency_key LIKE 'pr-head-change:%:delta-review'),"
                    "(SELECT COUNT(*) FROM sprint_events "
                    "WHERE event_type='merge.grant_bypassed')"
                ).fetchone()
            ),
        )

    def test_closed_head_move_does_not_create_orphaned_delta_review(self):
        self.approve()
        self.reader.current = pull_request(
            state="CLOSED",
            checks=None,
            checks_failed=False,
            head_sha="b" * 40,
        )

        self.assertTrue(self.watcher.poll_once())

        self.assertEqual(
            "merge_ready",
            self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                (self.unit_id,),
            ).fetchone()[0],
        )
        self.assertEqual(
            (0, 0, 0),
            tuple(
                self.con.execute(
                    "SELECT "
                    "(SELECT COUNT(*) FROM sprint_events "
                    "WHERE event_type='review.approval_invalidated'),"
                    "(SELECT COUNT(*) FROM wake_message "
                    "WHERE idempotency_key LIKE 'pr-head-change:%:delta-review'),"
                    "(SELECT COUNT(*) FROM sprint_liveness_expectations e "
                    "JOIN wake_message m USING (message_id) "
                    "WHERE m.message_kind='review_request' "
                    "AND e.resolved_at IS NULL)"
                ).fetchone()
            ),
        )

    def test_grant_bypassed_merge_notifies_planner_without_completing_unit(self):
        self.reader.current = pull_request(
            state="MERGED", checks="SUCCESS", checks_failed=False
        )

        self.assertTrue(self.watcher.poll_once())

        self.assertEqual(
            "active",
            self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                (self.unit_id,),
            ).fetchone()[0],
        )
        bypass = self.con.execute(
            "SELECT payload FROM sprint_events "
            "WHERE event_type='merge.grant_bypassed'"
        ).fetchone()
        self.assertEqual("active", json.loads(bypass["payload"])["before"])
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_events "
                "WHERE event_type='work_unit.completed'"
            ).fetchone()[0],
        )
        self.assertEqual(
            (0, 0),
            tuple(
                self.con.execute(
                    "SELECT "
                    "(SELECT COUNT(*) FROM sprint_events "
                    "WHERE event_type='sprint.delivery_terminal'),"
                    "(SELECT COUNT(*) FROM wake_message "
                    "WHERE idempotency_key LIKE 'sprint:%:delivery-terminal:%')"
                ).fetchone()
            ),
        )
        notice = self.con.execute(
            "SELECT m.to_participant_id,m.work_unit_id,m.body,w.state "
            "FROM wake_message m JOIN sprint_wake_messages wm USING (message_id) "
            "JOIN sprint_wake_outbox w USING (wake_id) "
            "WHERE m.idempotency_key LIKE 'merge-grant-bypassed:%'"
        ).fetchone()
        self.assertEqual(
            (self.planner_id, self.unit_id, "pending"),
            (int(notice[0]), int(notice[1]), notice[3]),
        )
        self.assertIn("remains incomplete", notice["body"])

        lifecycle = sprint_domain.SprintLifecycleStore(self.con)
        lifecycle.pause(
            self.sprint_id,
            sprint_domain.LifecycleActor("participant", 1),
            reason="inspect the bypass",
        )
        lifecycle.resume(
            self.sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
        )
        self.assertEqual(
            (1, 1),
            tuple(
                self.con.execute(
                    "SELECT "
                    "(SELECT COUNT(*) FROM sprint_events "
                    "WHERE event_type='merge.grant_bypassed'),"
                    "(SELECT COUNT(*) FROM wake_message "
                    "WHERE idempotency_key LIKE 'merge-grant-bypassed:%')"
                ).fetchone()
            ),
        )

    def test_grant_bypassed_resume_after_disposition_change_does_not_conflict(self):
        handoff = self.request_review()
        self.accept_review(handoff.message_id)
        self.reader.current = pull_request(
            state="MERGED", checks="SUCCESS", checks_failed=False
        )
        self.assertTrue(self.watcher.poll_once())
        self.assertEqual(
            "in_review",
            self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                (self.unit_id,),
            ).fetchone()[0],
        )
        notice = self.con.execute(
            "SELECT body FROM wake_message "
            "WHERE idempotency_key LIKE 'merge-grant-bypassed:%'"
        ).fetchone()
        self.assertIn("from in_review", notice["body"])

        lifecycle = sprint_domain.SprintLifecycleStore(self.con)
        lifecycle.pause(
            self.sprint_id,
            sprint_domain.LifecycleActor("participant", 1),
            reason="reconcile the bypass",
        )
        # The Planner dispositions the bypassed unit for fixing before
        # resuming; resume re-observes the same merged transition.
        self.con.execute(
            "UPDATE sprint_work_units SET disposition='fixing' "
            "WHERE work_unit_id=?",
            (self.unit_id,),
        )
        self.con.commit()

        receipt = lifecycle.resume(
            self.sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
        )

        self.assertTrue(receipt.changed)
        self.assertEqual(
            (1, 1),
            tuple(
                self.con.execute(
                    "SELECT "
                    "(SELECT COUNT(*) FROM sprint_events "
                    "WHERE event_type='merge.grant_bypassed'),"
                    "(SELECT COUNT(*) FROM wake_message "
                    "WHERE idempotency_key LIKE 'merge-grant-bypassed:%')"
                ).fetchone()
            ),
        )
        self.assertEqual(
            "from in_review",
            self.con.execute(
                "SELECT substr(body,instr(body,'from '),14) FROM wake_message "
                "WHERE idempotency_key LIKE 'merge-grant-bypassed:%'"
            ).fetchone()[0],
        )

    def test_grant_bypassed_merge_resolves_accepted_review_expectation(self):
        handoff = self.request_review()
        self.accept_review(handoff.message_id)
        self.seed_historical_expectation(handoff.message_id)
        self.reader.current = pull_request(
            state="MERGED", checks="SUCCESS", checks_failed=False
        )

        self.assertTrue(self.watcher.poll_once())

        expectation = self.con.execute(
            "SELECT resolved_at,resolution,next_evaluation_at "
            "FROM sprint_liveness_expectations WHERE message_id=?",
            (handoff.message_id,),
        ).fetchone()
        self.assertIsNotNone(expectation["resolved_at"])
        self.assertEqual(
            ("registered_pr.merged_grant_bypassed", None),
            (expectation["resolution"], expectation["next_evaluation_at"]),
        )
        self.assertEqual(
            "in_review",
            self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                (self.unit_id,),
            ).fetchone()[0],
        )
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_events "
                "WHERE event_type='merge.grant_bypassed'"
            ).fetchone()[0],
        )

    def test_observed_merge_completes_board_and_assigns_next_in_fresh_active_lane(self):
        approved = self.approve()
        self.messages.mark_read(approved.message_id, 1)
        document = self.con.execute(
            "SELECT document_id FROM sprint_specs WHERE sprint_id=?",
            (self.sprint_id,),
        ).fetchone()[0]
        feature = self.con.execute(
            "SELECT feature_id FROM sprints WHERE sprint_id=?",
            (self.sprint_id,),
        ).fetchone()[0]
        task_id = self.con.execute(
            "INSERT INTO spec_tasks (feature_id,document_id,seq,title) "
            "VALUES (?,?,99,'Next unit')",
            (feature, document),
        ).lastrowid
        self.con.commit()
        next_unit = sprint_domain.SprintWorkUnitStore(self.con).create(
            self.sprint_id,
            3,
            assigned_shell_id=1,
            reviewer_shell_id=2,
            title="Next unit",
            expected_output="Continue in the persistent lane",
            task_ids=(task_id,),
            dependency_ids=(self.unit_id,),
        )

        self.reader.current = pull_request(
            state="MERGED", checks="SUCCESS", checks_failed=False, head_sha="a" * 40
        )
        self.watcher.poll_once()

        self.assertEqual(
            [(self.unit_id, "completed"), (next_unit, "ready")],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT work_unit_id,disposition FROM sprint_work_units "
                    "WHERE work_unit_id IN (?,?) ORDER BY work_unit_id",
                    (self.unit_id, next_unit),
                )
            ],
        )
        current = self.con.execute(
            "SELECT active.chat_id FROM sprint_participants participant "
            "JOIN active_shell_chats active ON active.shell_id=participant.shell_id "
            "WHERE participant.participant_id=?",
            (self.developer_id,),
        ).fetchone()
        self.assertEqual(self.developer_conversation_id, current[0])
        assignment = self.con.execute(
            "SELECT message_id FROM wake_message WHERE work_unit_id=? "
            "AND message_kind='work_assignment'",
            (next_unit,),
        ).fetchone()
        self.assertIsNotNone(assignment)
        assignment_wake_id = int(
            self.con.execute(
                "SELECT wake_id FROM sprint_wake_messages WHERE message_id=?",
                (assignment["message_id"],),
            ).fetchone()[0]
        )
        delivered: list[str] = []
        service = sprint_message_delivery.SprintWakeDeliveryService(self.con)
        while True:
            outcome = service.deliver_once(
                "stage6-next",
                lambda conversation, _prompt, _key: (
                    delivered.append(conversation) or "next-run"
                ),
            )
            self.assertIsNotNone(outcome)
            if outcome.wake_id == assignment_wake_id:
                break
        self.assertNotEqual(
            self.developer_conversation_id,
            delivered[-1],
        )
        active = self.con.execute(
            "SELECT chat_id,process_pid,process_start_ticks "
            "FROM active_shell_chats WHERE shell_id=1"
        ).fetchone()
        self.assertEqual(
            tuple(active),
            (delivered[-1], None, None),
        )
        self.assertEqual(
            ["work_unit.completed", "work_unit.ready", "pr.transition"],
            [
                row[0]
                for row in self.con.execute(
                    "SELECT event_type FROM sprint_events "
                    "ORDER BY event_id DESC LIMIT 3"
                )
            ][::-1],
        )


class CarriedLowClosureTest(SprintReviewLoopCase):
    def test_registration_notifies_live_watcher_once_after_commit(self):
        store = sprint_pr_watcher.SprintPRRegistrationStore(self.con)
        with mock.patch.object(
            sprint_pr_watcher, "notify_commit", return_value=True
        ) as notify:
            created = store.register(
                self.sprint_id,
                owner_shell_id=1,
                repository="acme/other",
                pr_number=43,
                work_unit_ids=(self.unit_id,),
            )
            replay = store.register(
                self.sprint_id,
                owner_shell_id=1,
                repository="acme/other",
                pr_number=43,
                work_unit_ids=(self.unit_id,),
            )

        self.assertTrue(created.created)
        self.assertFalse(replay.created)
        notify.assert_called_once_with()
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_registered_prs "
                "WHERE repository='acme/other' AND pr_number=43"
            ).fetchone()[0],
        )

    def test_paused_sprint_blocks_work_but_delivers_pause_notice(self):
        sent = self.messages.send(
            self.sprint_id,
            to_participant_id=self.developer_id,
            from_participant_id=self.planner_id,
            work_unit_id=self.unit_id,
            message_kind="notification",
            body="active before pause",
            idempotency_key="pause-race",
            declared_type="re-enter",
        )
        self.lifecycle.transition(
            self.sprint_id,
            "paused",
            sprint_domain.LifecycleActor("participant", 1),
            reason="integrity check",
        )

        prompts: list[str] = []
        service = sprint_message_delivery.SprintWakeDeliveryService(self.con)
        outcome = service.deliver_once(
            "paused-delivery",
            lambda conversation, prompt, _key: (
                prompts.append(prompt) or conversation
            ),
        )

        self.assertIsNotNone(outcome)
        self.assertNotEqual(sent.wake_id, outcome.wake_id)
        self.assertIn("Sprint 1 paused: integrity check", prompts[0])
        self.assertEqual(
            (None, "pending"),
            tuple(
                self.con.execute(
                    "SELECT m.delivered_at,w.state FROM wake_message m "
                    "JOIN sprint_wake_messages joined USING(message_id) "
                    "JOIN sprint_wake_outbox w USING(wake_id) "
                    "WHERE m.message_id=?",
                    (sent.message_id,),
                ).fetchone()
            ),
        )
        self.assertIsNone(service.claim_next("paused-work-stays-gated"))
        wake = self.con.execute(
            "SELECT state,attempt_count,claim_owner FROM sprint_wake_outbox "
            "WHERE wake_id=?",
            (sent.wake_id,),
        ).fetchone()
        self.assertEqual(("pending", 0, None), tuple(wake))


if __name__ == "__main__":
    import unittest

    unittest.main(verbosity=2)
