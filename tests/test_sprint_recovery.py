"""Stage 8 gates for Sprint pause, resume, abort, and restart recovery."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
sys.path[:0] = [str(SCRIPTS), str(ROOT / "tests")]

import sprint_domain
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
            "SELECT current_conversation_id FROM sprint_participants "
            "WHERE sprint_id=? AND shell_id=?",
            (self.sprint_id, shell_id),
        ).fetchone()
        conversation_id = str(participant[0])
        token = self.con.execute(
            "SELECT COUNT(*)+1 FROM conversation_runs"
        ).fetchone()[0]
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
        self.assertEqual(
            {"kind": "participant", "shell_id": 1}, report["actor"]
        )
        self.assertEqual([run_id], [row["run_id"] for row in report["deterministic"]["active_turns"]])
        self.assertEqual(
            [self.unit_id],
            [row["work_unit_id"] for row in report["deterministic"]["work_units"]],
        )
        self.assertEqual(
            ["red"],
            [row["normalized_state"] for row in report["deterministic"]["registered_prs"]],
        )
        self.assertEqual("", report["integrity_threat"])
        self.assertEqual("", report["judgment"])
        self.assertEqual("", report["recommendation"])
        self.assertEqual(
            [(self.planner_id, 0, "Sprint 1 paused: GitHub state cannot be trusted")],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT to_participant_id,actionable,body FROM sprint_messages "
                    "WHERE idempotency_key LIKE 'sprint-pause:%'"
                )
            ],
        )
        self.assertEqual(
            [("engine", "sprint-recovery", "queued")],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT sender_kind,sender_ref,state FROM conversation_messages "
                    "WHERE idempotency_key LIKE 'sprint-pause:%:planner-conversation'"
                )
            ],
        )

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
                    "(SELECT COUNT(*) FROM sprint_messages "
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
        self.assertEqual([run_id], [row["run_id"] for row in report["deterministic"]["active_turns"]])

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
        self.reader.current = pull_request(state="MERGED", checks="SUCCESS", checks_failed=False)

        receipt = coordinator.resume(
            self.sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
            reason="evidence reviewed",
        )

        self.assertEqual((self.unit_id,), receipt.projected_work_unit_ids)
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
                    "FROM sprint_messages WHERE work_unit_id=?",
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
            [(self.planner_id, 0)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT to_participant_id,actionable FROM sprint_messages "
                    "WHERE idempotency_key LIKE 'sprint-resume:%'"
                )
            ],
        )

    def test_resume_surfaces_native_and_capacity_anomalies_without_blocking(self):
        run_id = self.add_live_run()
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
                "SELECT COUNT(*) FROM sprint_messages "
                "WHERE idempotency_key LIKE 'sprint-resume:%'",
            ).fetchone()[0],
        )


class ClosedReviewRecoveryTest(SprintReviewLoopCase):
    def test_closed_without_merge_resolves_every_review_expectation(self):
        handoff = self.request_review()
        self.accept_review(handoff.message_id)
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
                    "SELECT lifecycle,terminal_outcome FROM sprints "
                    "WHERE sprint_id=?",
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
                    "(SELECT COUNT(*) FROM sprint_messages WHERE sprint_id=s.sprint_id),"
                    "(SELECT COUNT(*) FROM sprint_events WHERE sprint_id=s.sprint_id) "
                    "FROM sprints s WHERE s.sprint_id=?",
                    (sprint_id,),
                ).fetchone()
            ),
        )


if __name__ == "__main__":
    import unittest

    unittest.main(verbosity=2)
