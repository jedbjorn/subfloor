"""Stage 6 gates for the Sprints v2 Developer and Reviewer loop."""
from __future__ import annotations

import json
import sys
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
            "SELECT message_id FROM sprint_messages "
            "WHERE to_participant_id=? "
            "AND idempotency_key LIKE 'pr-transition:%' "
            "ORDER BY message_id DESC LIMIT 1",
            (self.developer_id,),
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

    def accept_review(self, message_id: int) -> None:
        self.assertEqual("accepted", self.messages.mark_read(message_id, 2))

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
            "SELECT COUNT(*) FROM sprint_messages"
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
                    "SELECT u.disposition,(SELECT COUNT(*) FROM sprint_messages) "
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

    def test_green_readiness_commits_judgment_and_active_reviewer_request(self):
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
            "actionable,disposition FROM sprint_messages WHERE message_id=?",
            (handoff.message_id,),
        ).fetchone()
        self.assertEqual(
            (
                self.developer_id,
                self.reviewer_id,
                "review_request",
                "Focused and full gates are green; ready for review.",
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
                "Focused and full gates are green; ready for review.",
            ),
            tuple(judgment),
        )
        lease = sprint_message_delivery.SprintWakeDeliveryService(
            self.con
        ).claim_next("stage6-review")
        reviewer_chat = self.con.execute(
            "SELECT current_conversation_id FROM sprint_participants "
            "WHERE participant_id=?",
            (self.reviewer_id,),
        ).fetchone()[0]
        self.assertEqual(self.reviewer_id, lease.participant_id)
        self.assertEqual(reviewer_chat, lease.target_conversation_id)
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_messages "
                "WHERE message_id=? AND to_participant_id=?",
                (handoff.message_id, self.planner_id),
            ).fetchone()[0],
            "the watcher never bypasses Developer judgment to request review",
        )

    def test_non_green_and_wrong_developer_leave_no_review_evidence(self):
        self.reader.current = pull_request(
            checks="PENDING", checks_failed=False, head_sha="a" * 40
        )
        self.watcher.poll_once()
        before = self.con.execute(
            "SELECT COUNT(*) FROM sprint_messages"
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
            self.con.execute("SELECT COUNT(*) FROM sprint_messages").fetchone()[0],
        )
        self.assertEqual(
            "active",
            self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                (self.unit_id,),
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
            (2, 2, 1),
            tuple(
                self.con.execute(
                    "SELECT COUNT(*),"
                    "(SELECT COUNT(*) FROM sprint_judgments WHERE work_unit_id=?),"
                    "(SELECT COUNT(*) FROM sprint_participant_conversations "
                    " WHERE purpose='merge') "
                    "FROM sprint_messages WHERE idempotency_key IN (?,?)",
                    (
                        self.unit_id,
                        "idempotent-request",
                        "idempotent-approval",
                    ),
                ).fetchone()
            ),
        )


class ReviewOutcomeTest(SprintReviewLoopCase):
    def test_changes_and_approval_select_fresh_linked_conversations(self):
        first = self.request_review()
        self.accept_review(first.message_id)
        changed = self.loop.record_review(
            self.sprint_id,
            self.registered_pr_id,
            2,
            verdict="changes_requested",
            body="Medium: preserve the exact reviewed head.",
            idempotency_key="changes-1",
        )
        self.assertEqual("fixing", changed.disposition)
        self.assertNotEqual(self.developer_conversation_id, changed.conversation_id)
        fix_link = self.con.execute(
            "SELECT purpose,parent_conversation_id FROM "
            "sprint_participant_conversations WHERE conversation_id=?",
            (changed.conversation_id,),
        ).fetchone()
        self.assertEqual(("fix", self.developer_conversation_id), tuple(fix_link))
        self.assertIsNone(self.messages.mark_read(changed.message_id, 1))
        self.assertEqual(
            ("review submitted: changes_requested", 0),
            tuple(
                self.con.execute(
                    "SELECT resolution,next_evaluation_at IS NOT NULL "
                    "FROM sprint_liveness_expectations WHERE message_id=?",
                    (first.message_id,),
                ).fetchone()
            ),
        )

        self.reader.current = pull_request(
            checks="FAILURE", checks_failed=True, head_sha="b" * 40
        )
        self.watcher.poll_once()
        red_lease = sprint_message_delivery.SprintWakeDeliveryService(
            self.con
        ).claim_next("stage6-fix-red")
        self.assertEqual(changed.conversation_id, red_lease.target_conversation_id)
        red_messages = self.con.execute(
            "SELECT m.message_id FROM sprint_messages m "
            "JOIN sprint_wake_messages wm USING(message_id) "
            "WHERE wm.wake_id=?",
            (red_lease.wake_id,),
        ).fetchall()
        for message in red_messages:
            self.messages.mark_read(int(message["message_id"]), 1)

        self.reader.current = pull_request(
            checks="SUCCESS", checks_failed=False, head_sha="b" * 40
        )
        self.watcher.poll_once()
        second = self.request_review("review-2")
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
        self.assertNotIn(
            approved.conversation_id,
            {self.developer_conversation_id, changed.conversation_id},
        )
        merge_link = self.con.execute(
            "SELECT purpose,parent_conversation_id FROM "
            "sprint_participant_conversations WHERE conversation_id=?",
            (approved.conversation_id,),
        ).fetchone()
        self.assertEqual(("merge", changed.conversation_id), tuple(merge_link))
        self.assertEqual(
            approved.conversation_id,
            self.con.execute(
                "SELECT current_conversation_id FROM sprint_participants "
                "WHERE participant_id=?",
                (self.developer_id,),
            ).fetchone()[0],
        )
        self.assertEqual(
            ("review submitted: approved", 0),
            tuple(
                self.con.execute(
                    "SELECT resolution,next_evaluation_at IS NOT NULL "
                    "FROM sprint_liveness_expectations WHERE message_id=?",
                    (second.message_id,),
                ).fetchone()
            ),
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
            "SELECT disposition FROM sprint_messages WHERE message_id=?",
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
            "SELECT message_id FROM sprint_messages WHERE to_participant_id=? "
            "AND idempotency_key LIKE 'pr-transition:%' "
            "ORDER BY message_id DESC LIMIT 1",
            (self.developer_id,),
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
                "SELECT disposition FROM sprint_messages WHERE message_id=?",
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
            sprint_domain.SprintInvariantError, "request is stale"
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
                "SELECT COUNT(*) FROM sprint_messages "
                "WHERE idempotency_key='stale-review-head'"
            ).fetchone()[0],
        )
        self.assertEqual(
            self.developer_conversation_id,
            self.con.execute(
                "SELECT current_conversation_id FROM sprint_participants "
                "WHERE participant_id=?",
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

    def test_same_state_head_move_invalidates_approval_and_requests_delta_review(self):
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
            "in_review",
            self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                (self.unit_id,),
            ).fetchone()[0],
        )
        delta = self.con.execute(
            "SELECT message_id,to_participant_id,actionable,disposition,body "
            "FROM sprint_messages WHERE idempotency_key LIKE "
            "'pr-head-change:%:delta-review'"
        ).fetchone()
        self.assertEqual(
            (self.reviewer_id, 1, "pending"),
            (int(delta["to_participant_id"]), delta["actionable"], delta["disposition"]),
        )
        self.assertIn("Perform a delta review", delta["body"])
        self.assertEqual(
            [(self.reviewer_id, "pending")],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT w.participant_id,w.state FROM sprint_wake_outbox w "
                    "JOIN sprint_wake_messages wm USING (wake_id) "
                    "WHERE wm.message_id=?",
                    (delta["message_id"],),
                )
            ],
        )
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
                "SELECT read_at FROM sprint_messages WHERE message_id=?",
                (approved.message_id,),
            ).fetchone()[0]
        )

        self.accept_review(int(delta["message_id"]))
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
                    "(SELECT COUNT(*) FROM sprint_messages "
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
                    "(SELECT COUNT(*) FROM sprint_messages "
                    "WHERE idempotency_key LIKE 'pr-head-change:%:delta-review'),"
                    "(SELECT COUNT(*) FROM sprint_liveness_expectations e "
                    "JOIN sprint_messages m USING (message_id) "
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
        notice = self.con.execute(
            "SELECT m.to_participant_id,m.work_unit_id,m.body,w.state "
            "FROM sprint_messages m JOIN sprint_wake_messages wm USING (message_id) "
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
                    "(SELECT COUNT(*) FROM sprint_messages "
                    "WHERE idempotency_key LIKE 'merge-grant-bypassed:%')"
                ).fetchone()
            ),
        )

    def test_grant_bypassed_merge_resolves_accepted_review_expectation(self):
        handoff = self.request_review()
        self.accept_review(handoff.message_id)
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

    def test_observed_merge_completes_board_and_assigns_next_in_persistent_lane(self):
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
            "SELECT persistent_conversation_id,current_conversation_id "
            "FROM sprint_participants WHERE participant_id=?",
            (self.developer_id,),
        ).fetchone()
        self.assertEqual(self.developer_conversation_id, current[0])
        self.assertEqual(current[0], current[1])
        assignment = self.con.execute(
            "SELECT message_id FROM sprint_messages WHERE work_unit_id=? "
            "AND message_kind='work_assignment'",
            (next_unit,),
        ).fetchone()
        self.assertIsNotNone(assignment)
        lease = sprint_message_delivery.SprintWakeDeliveryService(
            self.con
        ).claim_next("stage6-next")
        self.assertEqual(self.developer_conversation_id, lease.target_conversation_id)
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

    def test_paused_sprint_wake_cannot_be_claimed_for_delivery(self):
        sent = self.messages.send(
            self.sprint_id,
            to_participant_id=self.developer_id,
            from_participant_id=self.planner_id,
            work_unit_id=self.unit_id,
            message_kind="notification",
            body="active before pause",
            idempotency_key="pause-race",
            active=True,
        )
        self.lifecycle.transition(
            self.sprint_id,
            "paused",
            sprint_domain.LifecycleActor("participant", 1),
            reason="integrity check",
        )

        service = sprint_message_delivery.SprintWakeDeliveryService(self.con)
        self.assertIsNone(service.claim_next("paused-delivery"))
        wake = self.con.execute(
            "SELECT state,attempt_count,claim_owner FROM sprint_wake_outbox "
            "WHERE wake_id=?",
            (sent.wake_id,),
        ).fetchone()
        self.assertEqual(("pending", 0, None), tuple(wake))


if __name__ == "__main__":
    import unittest

    unittest.main(verbosity=2)
