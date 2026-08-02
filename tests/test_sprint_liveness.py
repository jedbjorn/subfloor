"""Stage 7 fake-clock gates for Sprint liveness and quota escalation."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
MIGRATIONS = ENGINE / "migrations"

sys.path.insert(0, str(ENGINE / "scripts"))
import sprint_domain  # noqa: E402
import sprint_liveness  # noqa: E402
import sprint_message_delivery  # noqa: E402


def apply_schema(con: sqlite3.Connection) -> None:
    con.executescript((ENGINE / "schema.sql").read_text())
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")


def stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


@dataclass
class FakeClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value

    def at(self, value: datetime) -> None:
        self.value = value


class SprintLivenessCase(unittest.TestCase):
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
            "(sprint_id,document_id,bound_revision_sha256,approval_id) "
            "VALUES (?,?,?,?)",
            (self.sprint_id, document_id, revision, approval_id),
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
        wake_id = sprint_domain.SprintLifecycleStore(self.con).arm(
            self.sprint_id, 3
        )[0]
        self.assignment_message_id = int(
            self.con.execute(
                "SELECT message_id FROM sprint_wake_messages WHERE wake_id=?",
                (wake_id,),
            ).fetchone()[0]
        )
        self.messages = sprint_message_delivery.SprintMessageStore(self.con)
        self.assertEqual(
            "accepted", self.messages.mark_read(self.assignment_message_id, 1)
        )
        expectation = self.expectation(self.assignment_message_id)
        self.started_at = parse(expectation["accepted_at"])
        self.clock = FakeClock(self.started_at)

    def expectation(self, message_id: int | None = None) -> sqlite3.Row:
        row = self.con.execute(
            "SELECT * FROM sprint_liveness_expectations WHERE message_id=?",
            (message_id or self.assignment_message_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        return row

    def monitor(self, process_probe=None) -> sprint_liveness.SprintLivenessMonitor:
        if process_probe is None:
            process_probe = lambda _participant, _accepted, _now: (None, None, None)
        collector = sprint_liveness.SprintEvidenceCollector(
            self.con, process_probe=process_probe
        )
        return sprint_liveness.SprintLivenessMonitor(
            self.con, now=self.clock, collector=collector
        )

    def advance(self, minutes: int) -> None:
        self.clock.at(self.started_at + timedelta(minutes=minutes))

    def message_rows(self, kind: str) -> list[sqlite3.Row]:
        return self.con.execute(
            "SELECT * FROM sprint_messages WHERE message_kind=? ORDER BY message_id",
            (kind,),
        ).fetchall()

    def add_native_event(self, event_type: str, minutes: int) -> int:
        conversation_id = self.con.execute(
            "SELECT current_conversation_id FROM sprint_participants "
            "WHERE participant_id=?",
            (self.developer_id,),
        ).fetchone()[0]
        sequence = int(
            self.con.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM conversation_events "
                "WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()[0]
        )
        event_id = int(
            self.con.execute(
                "INSERT INTO conversation_events "
                "(conversation_id,sequence,event_type,payload,created_at) "
                "VALUES (?,?,?,'{}',?)",
                (
                    conversation_id,
                    sequence,
                    event_type,
                    stamp(self.started_at + timedelta(minutes=minutes)),
                ),
            ).lastrowid
        )
        self.con.commit()
        return event_id

    def add_terminal_run(
        self, state: str, minutes: int, error_code: str | None = None
    ) -> int:
        conversation_id = self.con.execute(
            "SELECT current_conversation_id FROM sprint_participants "
            "WHERE participant_id=?",
            (self.developer_id,),
        ).fetchone()[0]
        token = self.con.execute(
            "SELECT COUNT(*)+1 FROM conversation_runs"
        ).fetchone()[0]
        ended_at = stamp(self.started_at + timedelta(minutes=minutes))
        message_id = int(
            self.con.execute(
                "INSERT INTO conversation_messages "
                "(conversation_id,sender_kind,sender_ref,message_kind,body,"
                "idempotency_key,request_hash,state,completed_at) "
                "VALUES (?,'engine','test','notice','run fixture',?,?,"
                "'completed',?)",
                (conversation_id, f"test-run:{token}", f"test-run:{token}", ended_at),
            ).lastrowid
        )
        run_id = int(
            self.con.execute(
                "INSERT INTO conversation_runs "
                "(conversation_id,shell_id,trigger_message_id,state,lease_owner,"
                "lease_expires_at,ended_at,error_code) "
                "VALUES (?,1,?,?,'test','2999-01-01 00:00:00',?,?)",
                (conversation_id, message_id, state, ended_at, error_code),
            ).lastrowid
        )
        self.con.commit()
        return run_id


class FakeClockPolicyTest(SprintLivenessCase):
    def test_grace_nudge_escalation_and_restart_dedup(self) -> None:
        monitor = self.monitor()
        self.advance(5)
        # Acceptance also emits work_unit.accepted in the same SQLite second;
        # its different key is the initial strong-evidence observation here.
        self.assertEqual("strong-evidence", monitor.evaluate(self.sprint_id)[0].action)
        self.assertEqual([], self.message_rows("nudge"))

        self.advance(10)
        self.assertEqual("nudged", monitor.evaluate(self.sprint_id)[0].action)
        nudges = self.message_rows("nudge")
        self.assertEqual(1, len(nudges))
        self.assertEqual(self.developer_id, nudges[0]["to_participant_id"])
        self.assertIn(
            f"accepted Sprint message #{self.assignment_message_id}",
            nudges[0]["body"],
        )

        self.advance(15)
        self.assertEqual("observed", monitor.evaluate(self.sprint_id)[0].action)
        self.assertEqual([], self.message_rows("escalation"))

        self.advance(20)
        self.assertEqual("escalated", monitor.evaluate(self.sprint_id)[0].action)
        escalations = self.message_rows("escalation")
        self.assertEqual(1, len(escalations))
        self.assertEqual(self.planner_id, escalations[0]["to_participant_id"])
        payload = json.loads(escalations[0]["body"])
        self.assertEqual(self.assignment_message_id, payload["expectation_message_id"])
        self.assertEqual("continued silence after one nudge", payload["reason"])

        restarted = self.monitor()
        for minute in (25, 30, 60):
            self.advance(minute)
            restarted.evaluate(self.sprint_id)
        self.assertEqual(1, len(self.message_rows("nudge")))
        self.assertEqual(1, len(self.message_rows("escalation")))
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_events "
                "WHERE event_type='liveness.escalated'"
            ).fetchone()[0],
        )

    def test_fresh_strong_evidence_extends_grace_and_starts_new_episode(self) -> None:
        monitor = self.monitor()
        self.advance(5)
        monitor.evaluate(self.sprint_id)
        event_id = self.add_native_event("tool.completed", 9)

        self.advance(10)
        outcome = monitor.evaluate(self.sprint_id)[0]
        self.assertEqual("strong-evidence", outcome.action)
        self.assertEqual([], self.message_rows("nudge"))
        expectation = self.expectation()
        self.assertEqual(f"conversation.event:{event_id}", expectation["last_strong_key"])
        self.assertEqual(
            stamp(self.started_at + timedelta(minutes=9)),
            expectation["last_strong_at"],
        )

        self.advance(15)
        monitor.evaluate(self.sprint_id)
        self.assertEqual([], self.message_rows("nudge"))
        self.advance(20)
        self.assertEqual("nudged", monitor.evaluate(self.sprint_id)[0].action)
        self.assertEqual(1, len(self.message_rows("nudge")))

    def test_supporting_process_health_allows_one_nudge_but_defers_escalation(self) -> None:
        def healthy(_participant, _accepted, _now):
            return (
                sprint_liveness.Evidence(
                    "process:123:1",
                    "process.present",
                    self.clock(),
                    "supporting",
                    "healthy long-running tool",
                ),
                None,
                None,
            )

        monitor = self.monitor(healthy)
        self.advance(5)
        monitor.evaluate(self.sprint_id)
        self.advance(10)
        self.assertEqual("nudged", monitor.evaluate(self.sprint_id)[0].action)
        self.advance(20)
        self.assertEqual(
            "supporting-evidence", monitor.evaluate(self.sprint_id)[0].action
        )
        self.advance(40)
        self.assertEqual(
            "supporting-evidence", monitor.evaluate(self.sprint_id)[0].action
        )
        self.assertEqual(1, len(self.message_rows("nudge")))
        self.assertEqual([], self.message_rows("escalation"))

    def test_proven_failure_escalates_immediately_without_nudging_worker(self) -> None:
        failure_at = self.started_at + timedelta(minutes=1)
        unknown_run_id = self.add_terminal_run(
            "unknown", 1, "HARNESS_OUTCOME_UNKNOWN"
        )
        self.add_native_event("run.unknown", 1)

        def failed(_participant, _accepted, _now):
            return (
                None,
                sprint_liveness.Evidence(
                    "run:failed:7",
                    "run.failed",
                    failure_at,
                    "failure",
                    "proven failed native run",
                ),
                None,
            )

        self.advance(5)
        outcome = self.monitor(failed).evaluate(self.sprint_id)[0]
        self.assertEqual("escalated", outcome.action)
        self.assertEqual([], self.message_rows("nudge"))
        payload = json.loads(self.message_rows("escalation")[0]["body"])
        self.assertEqual("proven failed native run", payload["reason"])
        self.assertEqual("run:failed:7", payload["failure"]["key"])
        self.assertTrue(
            any(
                f"native run {unknown_run_id} outcome is unknown" in signal
                for signal in payload["unreadable_signals"]
            )
        )

        self.advance(10)
        self.assertEqual(
            "observed", self.monitor(failed).evaluate(self.sprint_id)[0].action
        )
        self.assertEqual([], self.message_rows("nudge"))
        self.assertEqual(1, len(self.message_rows("escalation")))

    def test_unknown_run_is_ambiguous_silence_not_terminal_failure(self) -> None:
        unknown_run_id = self.add_terminal_run(
            "unknown", 1, "HARNESS_OUTCOME_UNKNOWN"
        )
        self.add_native_event("run.unknown", 1)
        monitor = self.monitor()

        self.advance(5)
        self.assertEqual("strong-evidence", monitor.evaluate(self.sprint_id)[0].action)
        self.assertEqual([], self.message_rows("escalation"))
        self.advance(10)
        self.assertEqual("nudged", monitor.evaluate(self.sprint_id)[0].action)
        self.advance(20)
        self.assertEqual("escalated", monitor.evaluate(self.sprint_id)[0].action)

        payload = json.loads(self.message_rows("escalation")[0]["body"])
        self.assertIsNone(payload["failure"])
        self.assertTrue(
            any(
                f"native run {unknown_run_id} outcome is unknown" in signal
                for signal in payload["unreadable_signals"]
            )
        )

    def test_unchanged_git_observation_does_not_mask_failed_run(self) -> None:
        run_id = self.add_terminal_run("failed", 1, "BROKER_RUN_ERROR")
        self.add_native_event("run.failed", 2)
        conversation_id = self.con.execute(
            "SELECT current_conversation_id FROM sprint_participants "
            "WHERE participant_id=?",
            (self.developer_id,),
        ).fetchone()[0]
        head = "a" * 40
        self.con.execute(
            "INSERT INTO conversation_git_targets "
            "(conversation_id,branch_name,base_ref,first_head_sha,latest_head_sha,"
            "first_seen_at,last_seen_at) VALUES (?,?,?,?,?,?,?)",
            (
                conversation_id,
                "feat/test",
                "main",
                head,
                head,
                stamp(self.started_at + timedelta(minutes=2)),
                stamp(self.started_at + timedelta(minutes=4)),
            ),
        )
        self.con.commit()

        self.advance(5)
        outcome = self.monitor().evaluate(self.sprint_id)[0]
        self.assertEqual("escalated", outcome.action)
        self.assertEqual([], self.message_rows("nudge"))
        payload = json.loads(self.message_rows("escalation")[0]["body"])
        self.assertEqual(f"run.failure:{run_id}:failed", payload["failure"]["key"])
        self.assertEqual("run.failed", payload["failure"]["kind"])

    def test_new_failure_key_re_escalates_without_worker_nudge(self) -> None:
        failure_key = ["run:failed:7"]
        failure_at = [self.started_at + timedelta(minutes=1)]

        def failed(_participant, _accepted, _now):
            return (
                None,
                sprint_liveness.Evidence(
                    failure_key[0],
                    "run.failed",
                    failure_at[0],
                    "failure",
                    f"failure {failure_key[0]}",
                ),
                None,
            )

        monitor = self.monitor(failed)
        self.advance(5)
        self.assertEqual("escalated", monitor.evaluate(self.sprint_id)[0].action)
        self.advance(10)
        self.assertEqual("observed", monitor.evaluate(self.sprint_id)[0].action)
        self.assertEqual(1, len(self.message_rows("escalation")))

        failure_key[0] = "run:failed:8"
        failure_at[0] = self.started_at + timedelta(minutes=14)
        self.advance(15)
        self.assertEqual("escalated", monitor.evaluate(self.sprint_id)[0].action)
        escalations = self.message_rows("escalation")
        self.assertEqual(2, len(escalations))
        self.assertEqual(
            ["run:failed:7", "run:failed:8"],
            [json.loads(row["body"])["failure"]["key"] for row in escalations],
        )
        self.assertEqual([], self.message_rows("nudge"))
        self.assertEqual("run:failed:8", self.expectation()["last_failure_key"])

    def test_fresh_exhausted_worker_quota_escalates_on_first_evaluation(self) -> None:
        self.con.execute("DELETE FROM flavor_defaults WHERE flavor='planner'")
        self.con.executemany(
            "INSERT INTO flavor_defaults (flavor,harness,model,is_default) "
            "VALUES ('planner',?,?,?)",
            (("codex", "gpt", 1), ("claude", "sonnet", 0)),
        )
        account = int(
            self.con.execute(
                "INSERT INTO harness_quota_account (provider,account_ref) "
                "VALUES ('openai','worker')"
            ).lastrowid
        )
        self.con.execute(
            "INSERT INTO harness_quota_window "
            "(account_pk,window_kind,used_percent,captured_at,status) "
            "VALUES (?,'five_hour',100,?,'ok')",
            (account, stamp(self.started_at + timedelta(minutes=4))),
        )
        self.con.commit()

        self.advance(5)
        outcome = self.monitor().evaluate(self.sprint_id)[0]
        self.assertEqual("escalated", outcome.action)
        self.assertEqual([], self.message_rows("nudge"))
        payload = json.loads(self.message_rows("escalation")[0]["body"])
        self.assertEqual("quota.exhausted", payload["failure"]["kind"])
        self.assertIn("openai", payload["reason"])

    def test_review_request_is_an_expectation_even_without_an_editing_lane(self) -> None:
        self.monitor().resolve(self.assignment_message_id, "handoff to review")
        review = self.messages.send(
            self.sprint_id,
            to_participant_id=self.reviewer_id,
            from_participant_id=self.developer_id,
            work_unit_id=self.unit_id,
            message_kind="review_request",
            body="Please review",
            actionable=True,
            active=True,
            idempotency_key="review-request:1",
        )
        self.assertEqual("accepted", self.messages.mark_read(review.message_id, 2))
        review_expectation = self.expectation(review.message_id)
        self.assertIsNone(review_expectation["resolved_at"])
        reviewer_units = self.con.execute(
            "SELECT COUNT(*) FROM sprint_work_units WHERE assigned_shell_id=2 "
            "AND disposition IN ('active','in_review','fixing','merge_ready')"
        ).fetchone()[0]
        self.assertEqual(0, reviewer_units)

        review_start = parse(review_expectation["accepted_at"])
        self.clock.at(review_start + timedelta(minutes=5))
        self.monitor().evaluate(self.sprint_id)
        self.clock.at(review_start + timedelta(minutes=10))
        self.monitor().evaluate(self.sprint_id)
        nudges = self.message_rows("nudge")
        self.assertEqual(1, len(nudges))
        self.assertEqual(self.reviewer_id, nudges[0]["to_participant_id"])


class DeliveryAndActivationTest(SprintLivenessCase):
    def test_planner_quota_exhaustion_routes_escalation_to_fallback_only(self) -> None:
        self.con.execute("DELETE FROM flavor_defaults WHERE flavor='planner'")
        self.con.executemany(
            "INSERT INTO flavor_defaults (flavor,harness,model,is_default) "
            "VALUES ('planner',?,?,?)",
            (("codex", "gpt", 1), ("claude", "sonnet", 0)),
        )
        account = int(
            self.con.execute(
                "INSERT INTO harness_quota_account (provider,account_ref) "
                "VALUES ('openai','acct')"
            ).lastrowid
        )
        self.con.execute(
            "INSERT INTO harness_quota_window "
            "(account_pk,window_kind,used_percent,captured_at,status) "
            "VALUES (?,'session',100,?,'ok')",
            (account, stamp(self.started_at)),
        )
        self.con.commit()
        review = self.messages.send(
            self.sprint_id,
            to_participant_id=self.reviewer_id,
            from_participant_id=self.developer_id,
            work_unit_id=self.unit_id,
            message_kind="review_request",
            body="Please review",
            actionable=True,
            active=True,
            idempotency_key="review-request:fallback-dedup",
        )
        self.assertEqual("accepted", self.messages.mark_read(review.message_id, 2))

        failure_at = self.started_at + timedelta(minutes=1)

        def failed(_participant, _accepted, _now):
            return (
                None,
                sprint_liveness.Evidence(
                    "worker:missing",
                    "process.missing",
                    failure_at,
                    "failure",
                    "worker disappeared",
                ),
                None,
            )

        self.advance(5)
        outcomes = self.monitor(failed).evaluate(self.sprint_id)
        self.assertEqual(
            ["escalated", "escalated"], [outcome.action for outcome in outcomes]
        )
        planner = self.con.execute(
            "SELECT persistent_conversation_id,current_conversation_id "
            "FROM sprint_participants WHERE participant_id=?",
            (self.planner_id,),
        ).fetchone()
        self.assertNotEqual(
            planner["persistent_conversation_id"], planner["current_conversation_id"]
        )
        fallbacks = self.con.execute(
            "SELECT link.purpose,c.harness FROM sprint_participant_conversations link "
            "JOIN conversations c ON c.conversation_id=link.conversation_id "
            "WHERE link.sprint_participant_id=? AND link.purpose='fallback'",
            (self.planner_id,),
        ).fetchall()
        self.assertEqual([("fallback", "claude")], [tuple(row) for row in fallbacks])
        escalations = self.message_rows("escalation")
        self.assertEqual(2, len(escalations))
        self.assertEqual(
            ["fallback:claude", "fallback:claude"],
            [json.loads(row["body"])["planner_delivery_route"] for row in escalations],
        )
        wakes = self.con.execute(
            "SELECT w.participant_id,w.state FROM sprint_wake_outbox w "
            "JOIN sprint_wake_messages wm USING (sprint_id,wake_id) "
            "WHERE wm.message_id IN (?,?) ORDER BY wm.message_id",
            (escalations[0]["message_id"], escalations[1]["message_id"]),
        ).fetchall()
        self.assertEqual(
            [(self.planner_id, "pending"), (self.planner_id, "pending")],
            [tuple(row) for row in wakes],
        )
        self.assertEqual([], self.message_rows("nudge"))

    def test_paused_sprint_does_no_evaluation_or_delivery_work(self) -> None:
        self.advance(20)
        sprint_domain.SprintLifecycleStore(self.con).transition(
            self.sprint_id,
            "paused",
            sprint_domain.LifecycleActor("planner", 3),
            reason="hold",
        )
        before = self.con.execute(
            "SELECT COUNT(*) FROM sprint_messages"
        ).fetchone()[0]
        self.assertEqual((), self.monitor().evaluate(self.sprint_id))
        after = self.con.execute(
            "SELECT COUNT(*) FROM sprint_messages"
        ).fetchone()[0]
        self.assertEqual(before, after)
        self.assertIsNone(self.expectation()["last_evaluated_at"])

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

    def test_in_review_resolves_assignment_before_liveness_nudge(self) -> None:
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

        self.advance(20)
        self.assertEqual((), self.monitor().evaluate(self.sprint_id))
        self.assertEqual([], self.message_rows("nudge"))


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
        wake_id = sprint_domain.SprintLifecycleStore(con).arm(sprint_id, 3)[0]
        assignment_message_id = int(
            con.execute(
                "SELECT message_id FROM sprint_wake_messages WHERE wake_id=?",
                (wake_id,),
            ).fetchone()[0]
        )
        messages = sprint_message_delivery.SprintMessageStore(con)
        self.assertEqual("accepted", messages.mark_read(assignment_message_id, 1))
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
