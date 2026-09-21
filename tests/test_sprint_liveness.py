"""Gates for the surviving Sprint liveness expectation rows and resolutions."""
from __future__ import annotations

import hashlib
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
MIGRATIONS = ENGINE / "migrations"
RETIREMENT_MIGRATION = MIGRATIONS / "0193_retire_sprint_liveness_acceptance.sql"
REVISION_MIGRATION = MIGRATIONS / "0204_sprint_governing_revision_evidence.sql"
CONFORMANCE_OWNER_MIGRATION = (
    MIGRATIONS / "0205_sprint_conformance_ownership.sql"
)

sys.path.insert(0, str(ENGINE / "scripts"))
import sprint_domain
import sprint_liveness
import sprint_message_delivery


def apply_schema(con: sqlite3.Connection) -> None:
    con.executescript((ENGINE / "schema.sql").read_text())
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        if migration == RETIREMENT_MIGRATION:
            break
        con.executescript(migration.read_text())
    con.executescript(
        (MIGRATIONS / "0194_sprint_scoped_reply_waits.sql").read_text()
    )
    con.executescript(REVISION_MIGRATION.read_text())
    con.executescript(CONFORMANCE_OWNER_MIGRATION.read_text())
    con.execute("PRAGMA foreign_keys=ON")


class SprintLivenessCase(unittest.TestCase):
    def setUp(self) -> None:
        quiet_env = mock.patch.dict(
            "os.environ", {"SC_SPRINT_FORCE_NEW_QUIET_SECONDS": "0"}
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
        feature_id = int(
            self.con.execute(
                "INSERT INTO roadmap (title,roadmap_status) "
                "VALUES ('Feature','in_progress')"
            ).lastrowid
        )
        body = "governing spec"
        document_id = int(
            self.con.execute(
                "INSERT INTO documents (feature_id,kind,seq,title,body) "
                "VALUES (?,'spec',1,'Spec',?)",
                (feature_id, body),
            ).lastrowid
        )
        revision = hashlib.sha256(body.encode()).hexdigest()
        approval_id = int(
            self.con.execute(
                "INSERT INTO sprint_spec_approvals "
                "(document_id,revision_sha256,reviewer_shell_id,verdict) "
                "VALUES (?,?,2,'pass')",
                (document_id, revision),
            ).lastrowid
        )
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
            "(sprint_id,document_id,bound_revision_sha256,approval_id,"
            "bound_revision_body,bound_revision_legacy) VALUES (?,?,?,?,?,0)",
            (self.sprint_id, document_id, revision, approval_id, body),
        )
        self.con.executemany(
            "INSERT INTO sprint_participants "
            "(sprint_id,shell_id,role,harness,model,effort) VALUES (?,?,?,?,?,?)",
            (
                (self.sprint_id, 3, "planner", "codex", "gpt", "high"),
                (self.sprint_id, 1, "developer", "codex", "gpt", "high"),
                (self.sprint_id, 2, "reviewer", "kimi", "kimi", "high"),
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
        task_id = int(
            self.con.execute(
                "INSERT INTO spec_tasks (feature_id,document_id,seq,title) "
                "VALUES (?,?,1,'Task')",
                (feature_id, document_id),
            ).lastrowid
        )
        self.unit_id = int(
            self.con.execute(
                "INSERT INTO sprint_work_units "
                "(sprint_id,assigned_shell_id,reviewer_shell_id,title,"
                "expected_output,output_kind) "
                "VALUES (?,1,2,'Unit','Ship it','no_code')",
                (self.sprint_id,),
            ).lastrowid
        )
        self.con.execute(
            "INSERT INTO sprint_work_unit_tasks (sprint_id,work_unit_id,task_id) "
            "VALUES (?,?,?)",
            (self.sprint_id, self.unit_id, task_id),
        )
        self.con.commit()
        wake_id = sprint_domain.SprintLifecycleStore(
            self.con, probe_harness=lambda _harness: None
        ).arm(self.sprint_id, 3, conformance_reviewer_shell_id=2)[0]
        self.assignment_message_id = int(
            self.con.execute(
                "SELECT message_id FROM sprint_wake_messages WHERE wake_id=?",
                (wake_id,),
            ).fetchone()[0]
        )
        self.messages = sprint_message_delivery.SprintMessageStore(self.con)
        delivery_service = sprint_message_delivery.SprintWakeDeliveryService(
            self.con
        )
        planner_delivery = delivery_service.deliver_once(
            "liveness-planner-setup",
            lambda _conversation, _prompt, _key: "liveness-planner-run",
        )
        self.assertNotEqual(wake_id, planner_delivery.wake_id)
        arming_message_id = int(
            self.con.execute(
                "SELECT message_id FROM wake_message WHERE idempotency_key=?",
                (f"sprint:{self.sprint_id}:arming-model-selections",),
            ).fetchone()[0]
        )
        self.assertIsNone(self.messages.mark_read(arming_message_id, 3))
        delivered = delivery_service.deliver_once(
            "liveness-setup",
            lambda _conversation, _prompt, _key: "liveness-setup-run",
        )
        self.assertEqual(wake_id, delivered.wake_id)
        self.assertEqual(
            "accepted", self.messages.mark_read(self.assignment_message_id, 1)
        )
        expectation = self.expectation(self.assignment_message_id)
        self.assertIsNotNone(expectation["accepted_at"])

    def expectation(self, message_id: int | None = None) -> sqlite3.Row:
        row = self.con.execute(
            "SELECT * FROM sprint_liveness_expectations WHERE message_id=?",
            (message_id or self.assignment_message_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        return row

    def test_retired_evaluation_policy_has_no_module_surface(self) -> None:
        """Decisions #126/#127/#130 retired nudges, escalation and the backstop."""
        for retired in (
            "evaluate",
            "_apply",
            "_escalate",
            "_send_ci_stalled_backstops",
            "_send_ci_stalled_backstop",
            "_record_strong_evidence",
            "_update_observation",
            "_receiver_has_pending_force_new",
            "_fresh_strong",
            "_armed",
            "_event",
        ):
            with self.subTest(attribute=retired):
                self.assertFalse(
                    hasattr(sprint_liveness.SprintLivenessMonitor, retired)
                )
        for retired in (
            "SprintEvidenceCollector",
            "PlannerEscalationRouter",
            "Evidence",
            "EvidenceSnapshot",
            "EvaluationOutcome",
            "QuotaState",
            "GRACE_WINDOW",
            "ESCALATION_WINDOW",
            "EVALUATION_INTERVAL",
            "CI_STALLED_BACKSTOP",
            "_NATIVE_EVIDENCE_EVENTS",
            "_PROVIDER_ALIASES",
            "_parse",
            "_json",
            "_provider",
        ):
            with self.subTest(attribute=retired):
                self.assertFalse(hasattr(sprint_liveness, retired))

    def test_retirement_preserves_history_and_stops_new_expectations(self) -> None:
        historical = dict(self.expectation())

        self.con.executescript(RETIREMENT_MIGRATION.read_text())
        self.con.executescript(RETIREMENT_MIGRATION.read_text())

        next_message_id = int(
            self.con.execute(
                "INSERT INTO wake_message "
                "(sprint_id,sender_shell_id,receiver_shell_id,from_participant_id,"
                "to_participant_id,work_unit_id,message_kind,body,declared_type,"
                "actionable,disposition,idempotency_key) "
                "VALUES (?,3,2,?,?,?,?,'Review this head',"
                "'force-new',1,'pending','retirement-review-request')",
                (
                    self.sprint_id,
                    self.planner_id,
                    self.reviewer_id,
                    self.unit_id,
                    "review_request",
                ),
            ).lastrowid
        )
        self.con.execute(
            "UPDATE wake_message SET disposition='accepted',"
            "read_at='2026-08-10 12:00:00' WHERE message_id=?",
            (next_message_id,),
        )

        self.assertEqual(historical, dict(self.expectation()))
        self.assertIsNone(
            self.con.execute(
                "SELECT 1 FROM sprint_liveness_expectations WHERE message_id=?",
                (next_message_id,),
            ).fetchone()
        )
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_liveness_expectations"
            ).fetchone()[0],
        )
        self.assertIsNone(
            self.con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' "
                "AND name='trg_sprint_liveness_acceptance'"
            ).fetchone()
        )
        self.assertEqual([], self.con.execute("PRAGMA foreign_key_check").fetchall())


class DeliveryAndActivationTest(SprintLivenessCase):
    def test_terminal_work_unit_resolves_assignment_expectation(self) -> None:
        sprint_domain.SprintWorkUnitStore(self.con).complete(
            self.sprint_id,
            self.unit_id,
            1,
            result="Liveness fixture completed without code",
        )
        expectation = self.expectation()
        self.assertIsNotNone(expectation["resolved_at"])
        self.assertEqual("work_unit.completed", expectation["resolution"])
        self.assertIsNone(expectation["next_evaluation_at"])

    def test_cancelled_work_unit_resolves_assignment_expectation(self) -> None:
        self.con.execute(
            "UPDATE sprint_work_units SET disposition='cancelled',"
            "updated_at=datetime('now') WHERE work_unit_id=?",
            (self.unit_id,),
        )
        self.con.commit()

        expectation = self.expectation()
        self.assertIsNotNone(expectation["resolved_at"])
        self.assertEqual("work_unit.cancelled", expectation["resolution"])
        self.assertIsNone(expectation["next_evaluation_at"])

    def test_reassignment_does_not_resolve_former_developer_notification(self) -> None:
        notification = self.messages.send(
            self.sprint_id,
            to_participant_id=self.developer_id,
            from_participant_id=self.reviewer_id,
            work_unit_id=self.unit_id,
            message_kind="notification",
            body="Review changes requested",
            actionable=True,
            declared_type="re-enter",
            idempotency_key="changes-requested:former-developer",
        )
        self.assertEqual("accepted", self.messages.mark_read(notification.message_id, 1))
        self.con.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (4,'Replacement Developer','DEV2','dev','prompt',1)"
        )
        self.con.execute(
            "UPDATE sprint_work_units SET assigned_shell_id=4,"
            "disposition='in_review',updated_at=datetime('now') "
            "WHERE work_unit_id=?",
            (self.unit_id,),
        )
        self.con.commit()

        self.assertIsNotNone(self.expectation()["resolved_at"])
        former = self.expectation(notification.message_id)
        self.assertIsNone(former["resolved_at"])
        self.assertIsNone(former["resolution"])
        self.assertIsNotNone(former["next_evaluation_at"])

    def test_in_review_resolves_assignment_expectation(self) -> None:
        self.con.execute(
            "UPDATE sprint_work_units SET disposition='in_review',"
            "updated_at=datetime('now') WHERE work_unit_id=?",
            (self.unit_id,),
        )
        self.con.commit()

        expectation = self.expectation()
        self.assertIsNotNone(expectation["resolved_at"])
        self.assertEqual("work_unit.in_review", expectation["resolution"])
        self.assertIsNone(expectation["next_evaluation_at"])


class MigrationGateTest(unittest.TestCase):
    def test_in_review_upgrade_backfills_dirty_assignment_once(self) -> None:
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.row_factory = sqlite3.Row
        con.executescript((ENGINE / "schema.sql").read_text())
        target = "0158_sprint_terminal_liveness_hardening.sql"
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            if migration.name >= target:
                break
            con.executescript(migration.read_text())
        con.execute("INSERT INTO users (user_id,username) VALUES (1,'operator')")
        con.executemany(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (?,?,?,?,?,1)",
            (
                (1, "Developer", "DEV1", "dev", "prompt"),
                (2, "Reviewer", "REV1", "reviewer", "prompt"),
                (3, "Planner", "PLN1", "planner", "prompt"),
            ),
        )
        feature_id = int(
            con.execute(
                "INSERT INTO roadmap (title,roadmap_status) "
                "VALUES ('Feature','in_progress')"
            ).lastrowid
        )
        body = "governing spec"
        document_id = int(
            con.execute(
                "INSERT INTO documents (feature_id,kind,seq,title,body) "
                "VALUES (?,'spec',1,'Spec',?)",
                (feature_id, body),
            ).lastrowid
        )
        revision = hashlib.sha256(body.encode()).hexdigest()
        approval_id = int(
            con.execute(
                "INSERT INTO sprint_spec_approvals "
                "(document_id,revision_sha256,reviewer_shell_id,verdict) "
                "VALUES (?,?,2,'pass')",
                (document_id, revision),
            ).lastrowid
        )
        sprint_id = int(
            con.execute(
                "INSERT INTO sprints "
                "(feature_id,originating_planner_shell_id,merge_grant_enabled) "
                "VALUES (?,3,1)",
                (feature_id,),
            ).lastrowid
        )
        con.execute(
            "INSERT INTO sprint_specs "
            "(sprint_id,document_id,bound_revision_sha256,approval_id) "
            "VALUES (?,?,?,?)",
            (sprint_id, document_id, revision, approval_id),
        )
        con.executemany(
            "INSERT INTO sprint_participants "
            "(sprint_id,shell_id,role,harness) VALUES (?,?,?,?)",
            (
                (sprint_id, 3, "planner", "codex"),
                (sprint_id, 1, "developer", "codex"),
                (sprint_id, 2, "reviewer", "kimi"),
            ),
        )
        task_id = int(
            con.execute(
                "INSERT INTO spec_tasks (feature_id,document_id,seq,title) "
                "VALUES (?,?,1,'Task')",
                (feature_id, document_id),
            ).lastrowid
        )
        unit_id = int(
            con.execute(
                "INSERT INTO sprint_work_units "
                "(sprint_id,assigned_shell_id,reviewer_shell_id,title,"
                "expected_output,output_kind) "
                "VALUES (?,1,2,'Unit','Ship it','no_code')",
                (sprint_id,),
            ).lastrowid
        )
        con.execute(
            "INSERT INTO sprint_work_unit_tasks (sprint_id,work_unit_id,task_id) "
            "VALUES (?,?,?)",
            (sprint_id, unit_id, task_id),
        )
        con.commit()
        assignment_message_id = int(
            con.execute(
                "INSERT INTO sprint_messages "
                "(sprint_id,from_participant_id,to_participant_id,work_unit_id,"
                "message_kind,body,actionable,disposition,idempotency_key) "
                "VALUES (?,(SELECT participant_id FROM sprint_participants "
                "WHERE sprint_id=? AND role='planner'),"
                "(SELECT participant_id FROM sprint_participants "
                "WHERE sprint_id=? AND role='developer'),?,"
                "'work_assignment','Ship it',1,'pending','legacy-assignment')",
                (sprint_id, sprint_id, sprint_id, unit_id),
            ).lastrowid
        )
        con.execute("UPDATE sprints SET lifecycle='armed' WHERE sprint_id=?", (sprint_id,))
        con.execute(
            "UPDATE sprint_messages SET disposition='accepted',"
            "read_at='2026-08-02 00:00:00' WHERE message_id=?",
            (assignment_message_id,),
        )
        con.execute(
            "UPDATE sprint_work_units SET disposition='in_review',"
            "updated_at='2026-08-02 00:00:00' WHERE work_unit_id=?",
            (unit_id,),
        )
        con.commit()
        self.assertIsNone(
            con.execute(
                "SELECT resolved_at FROM sprint_liveness_expectations "
                "WHERE message_id=?",
                (assignment_message_id,),
            ).fetchone()[0]
        )

        migration_sql = (MIGRATIONS / target).read_text()
        con.executescript(migration_sql)
        first = tuple(
            con.execute(
                "SELECT resolved_at,resolution,next_evaluation_at "
                "FROM sprint_liveness_expectations WHERE message_id=?",
                (assignment_message_id,),
            ).fetchone()
        )
        self.assertIsNotNone(first[0])
        self.assertEqual(("work_unit.in_review", None), first[1:])

        con.executescript(migration_sql)
        self.assertEqual(
            first,
            tuple(
                con.execute(
                    "SELECT resolved_at,resolution,next_evaluation_at "
                    "FROM sprint_liveness_expectations WHERE message_id=?",
                    (assignment_message_id,),
                ).fetchone()
            ),
        )

    def test_upgrade_backfills_only_armed_nonterminal_expectations(self) -> None:
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.row_factory = sqlite3.Row
        con.executescript((ENGINE / "schema.sql").read_text())
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            if migration.name >= "0149_sprint_liveness_monitor.sql":
                break
            con.executescript(migration.read_text())
        con.execute("INSERT INTO users (user_id,username) VALUES (1,'operator')")
        con.executemany(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (?,?,?,?,?,1)",
            (
                (1, "Developer", "DEV1", "dev", "prompt"),
                (2, "Reviewer", "REV1", "reviewer", "prompt"),
                (3, "Planner", "PLN1", "planner", "prompt"),
            ),
        )
        feature_id = int(
            con.execute(
                "INSERT INTO roadmap (title,roadmap_status) "
                "VALUES ('Feature','in_progress')"
            ).lastrowid
        )
        sprint_id = int(
            con.execute(
                "INSERT INTO sprints "
                "(feature_id,originating_planner_shell_id,merge_grant_enabled) "
                "VALUES (?,3,1)",
                (feature_id,),
            ).lastrowid
        )
        participant_id = int(
            con.execute(
                "INSERT INTO sprint_participants "
                "(sprint_id,shell_id,role,harness) VALUES (?,1,'developer','codex')",
                (sprint_id,),
            ).lastrowid
        )
        active_unit_id = int(
            con.execute(
                "INSERT INTO sprint_work_units "
                "(sprint_id,assigned_shell_id,reviewer_shell_id,title,expected_output) "
                "VALUES (?,1,2,'Active','Ship it')",
                (sprint_id,),
            ).lastrowid
        )
        terminal_unit_id = int(
            con.execute(
                "INSERT INTO sprint_work_units "
                "(sprint_id,assigned_shell_id,reviewer_shell_id,title,"
                "expected_output,disposition,completed_at) "
                "VALUES (?,1,2,'Done','Already shipped','completed',?)",
                (sprint_id, "2026-07-31 11:00:00"),
            ).lastrowid
        )
        accepted_at = "2026-07-31 12:00:00"
        active_message_id = int(
            con.execute(
                "INSERT INTO sprint_messages "
                "(sprint_id,to_participant_id,work_unit_id,message_kind,body,"
                "actionable,"
                "disposition,read_at,idempotency_key) "
                "VALUES (?,?,?,'work_assignment','Build',1,'accepted',?,'active')",
                (sprint_id, participant_id, active_unit_id, accepted_at),
            ).lastrowid
        )
        terminal_message_id = int(
            con.execute(
                "INSERT INTO sprint_messages "
                "(sprint_id,to_participant_id,work_unit_id,message_kind,body,"
                "actionable,"
                "disposition,read_at,idempotency_key) "
                "VALUES (?,?,?,'work_assignment','Done',1,'accepted',?,'terminal')",
                (sprint_id, participant_id, terminal_unit_id, accepted_at),
            ).lastrowid
        )
        review_message_id = int(
            con.execute(
                "INSERT INTO sprint_messages "
                "(sprint_id,to_participant_id,message_kind,body,actionable,"
                "disposition,read_at,idempotency_key) "
                "VALUES (?,?,'review_request','Review',1,'accepted',?,'review')",
                (sprint_id, participant_id, accepted_at),
            ).lastrowid
        )
        con.execute(
            "UPDATE sprints SET lifecycle='armed',armed_at=? WHERE sprint_id=?",
            (accepted_at, sprint_id),
        )
        con.commit()

        con.executescript(
            (MIGRATIONS / "0149_sprint_liveness_monitor.sql").read_text()
        )

        expectations = con.execute(
            "SELECT message_id,accepted_at,last_strong_key,next_evaluation_at "
            "FROM sprint_liveness_expectations ORDER BY message_id"
        ).fetchall()
        self.assertEqual(
            [
                (
                    active_message_id,
                    accepted_at,
                    f"message.accepted:{active_message_id}",
                    "2026-07-31 12:05:00",
                ),
                (
                    review_message_id,
                    accepted_at,
                    f"message.accepted:{review_message_id}",
                    "2026-07-31 12:05:00",
                ),
            ],
            [tuple(row) for row in expectations],
        )
        self.assertNotIn(
            terminal_message_id, [row["message_id"] for row in expectations]
        )
        self.assertEqual([], con.execute("PRAGMA foreign_key_check").fetchall())

if __name__ == "__main__":
    unittest.main()
