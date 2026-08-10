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
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
MIGRATIONS = ENGINE / "migrations"
RETIREMENT_MIGRATION = MIGRATIONS / "0193_retire_sprint_liveness_acceptance.sql"

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
        wake_id = sprint_domain.SprintLifecycleStore(
            self.con, probe_harness=lambda _harness: None
        ).arm(self.sprint_id, 3)[0]
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
            "SELECT * FROM wake_message WHERE message_kind=? ORDER BY message_id",
            (kind,),
        ).fetchall()

    def add_native_event(
        self,
        event_type: str,
        minutes: int,
        *,
        run_id: int | None = None,
    ) -> int:
        conversation_id = self.con.execute(
            "SELECT active.chat_id FROM sprint_participants participant "
            "JOIN active_shell_chats active ON active.shell_id=participant.shell_id "
            "WHERE participant.participant_id=?",
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
                "(conversation_id,sequence,event_type,payload,run_id,created_at) "
                "VALUES (?,?,?,'{}',?,?)",
                (
                    conversation_id,
                    sequence,
                    event_type,
                    run_id,
                    stamp(self.started_at + timedelta(minutes=minutes)),
                ),
            ).lastrowid
        )
        self.con.commit()
        return event_id

    def add_succeeded_run(self, started_minutes: int, ended_minutes: int) -> int:
        conversation_id = self.con.execute(
            "SELECT active.chat_id FROM sprint_participants participant "
            "JOIN active_shell_chats active ON active.shell_id=participant.shell_id "
            "WHERE participant.participant_id=?",
            (self.developer_id,),
        ).fetchone()[0]
        token = self.con.execute(
            "SELECT COUNT(*)+1 FROM conversation_runs"
        ).fetchone()[0]
        started_at = stamp(self.started_at + timedelta(minutes=started_minutes))
        ended_at = stamp(self.started_at + timedelta(minutes=ended_minutes))
        message_id = int(
            self.con.execute(
                "INSERT INTO conversation_messages "
                "(conversation_id,sender_kind,sender_ref,message_kind,body,"
                "idempotency_key,request_hash,state,created_at,completed_at) "
                "VALUES (?,'engine','test','notice','run fixture',?,?,"
                "'completed',?,?)",
                (
                    conversation_id,
                    f"test-success:{token}",
                    f"test-success:{token}",
                    started_at,
                    ended_at,
                ),
            ).lastrowid
        )
        run_id = int(
            self.con.execute(
                "INSERT INTO conversation_runs "
                "(conversation_id,shell_id,trigger_message_id,harness_session_after,"
                "runner_ref,state,lease_owner,lease_expires_at,started_at,"
                "heartbeat_at,ended_at) VALUES (?,1,?,'session','runner','succeeded',"
                "'test','2999-01-01 00:00:00',?,?,?)",
                (conversation_id, message_id, started_at, ended_at, ended_at),
            ).lastrowid
        )
        self.con.commit()
        return run_id

    def add_terminal_run(
        self, state: str, minutes: int, error_code: str | None = None
    ) -> int:
        conversation_id = self.con.execute(
            "SELECT active.chat_id FROM sprint_participants participant "
            "JOIN active_shell_chats active ON active.shell_id=participant.shell_id "
            "WHERE participant.participant_id=?",
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

    def add_pr_transition(
        self,
        state: str,
        minutes: int,
        *,
        registered_pr_id: int | None = None,
        token: str = "first",
    ) -> int:
        if registered_pr_id is None:
            registered_pr_id = int(
                self.con.execute(
                    "INSERT INTO sprint_registered_prs "
                    "(sprint_id,owner_participant_id,repository,pr_number) "
                    "VALUES (?,?,?,?)",
                    (self.sprint_id, self.developer_id, "acme/widget", 41),
                ).lastrowid
            )
        self.con.execute(
            "INSERT INTO sprint_pr_transitions "
            "(registered_pr_id,normalized_state,transition_key,observed_head_sha,"
            "observed_at) VALUES (?,?,?,?,?)",
            (
                registered_pr_id,
                state,
                f"transition-{token}",
                token[0] * 40,
                stamp(self.started_at + timedelta(minutes=minutes)),
            ),
        )
        self.con.commit()
        return registered_pr_id

    def add_outbound_handoff(self, minutes: int) -> int:
        receipt = self.messages.send(
            self.sprint_id,
            to_participant_id=self.planner_id,
            from_participant_id=self.developer_id,
            message_kind="notification",
            body="Question awaiting Planner reply",
            actionable=False,
            declared_type="re-enter",
            idempotency_key=f"developer-question:{minutes}",
        )
        self.con.execute(
            "UPDATE wake_message SET created_at=? WHERE message_id=?",
            (
                stamp(self.started_at + timedelta(minutes=minutes)),
                receipt.message_id,
            ),
        )
        self.con.commit()
        return receipt.message_id

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


class SuppressorCollectorTest(SprintLivenessCase):
    def test_pending_owned_pr_suppresses_until_green_transition(self) -> None:
        registered_pr_id = self.add_pr_transition("pending", 1)
        snapshot = self.monitor().collector.collect(
            self.expectation(), self.started_at + timedelta(minutes=10)
        )

        self.assertEqual(
            [("pr.awaiting_transition", "pr.transition:transition-first")],
            [(item.kind, item.key) for item in snapshot.suppressors],
        )
        self.assertEqual("acme/widget#41 is pending", snapshot.suppressors[0].detail)

        self.add_pr_transition(
            "green", 2, registered_pr_id=registered_pr_id, token="green"
        )
        released = self.monitor().collector.collect(
            self.expectation(), self.started_at + timedelta(minutes=10)
        )
        self.assertEqual((), released.suppressors)
        self.assertEqual("pr.green", released.strong.kind)

    def test_outbound_handoff_ignores_its_turn_then_releases_on_new_run(self) -> None:
        handoff_run_id = self.add_succeeded_run(5, 7)
        message_id = self.add_outbound_handoff(6)
        tool_event_id = self.add_native_event(
            "tool.completed", 6, run_id=handoff_run_id
        )
        completed_event_id = self.add_native_event(
            "run.completed", 7, run_id=handoff_run_id
        )
        snapshot = self.monitor().collector.collect(
            self.expectation(), self.started_at + timedelta(minutes=10)
        )

        self.assertEqual(
            [("outbound.handoff", f"sprint.message:{message_id}")],
            [(item.kind, item.key) for item in snapshot.suppressors],
        )
        self.assertIsNone(snapshot.strong)
        self.assertEqual(
            [("tool.completed", handoff_run_id), ("run.completed", handoff_run_id)],
            [
                (row["event_type"], row["run_id"])
                for row in self.con.execute(
                    "SELECT event_type,run_id FROM conversation_events "
                    "WHERE event_id IN (?,?) ORDER BY event_id",
                    (tool_event_id, completed_event_id),
                )
            ],
        )

        new_run_id = self.add_succeeded_run(11, 12)
        self.add_native_event("run.completed", 12, run_id=new_run_id)
        released = self.monitor().collector.collect(
            self.expectation(), self.started_at + timedelta(minutes=15)
        )
        self.assertEqual((), released.suppressors)
        self.assertIsNotNone(released.strong)
        self.assertEqual(
            f"run.heartbeat:{new_run_id}:"
            f"{stamp(self.started_at + timedelta(minutes=12))}",
            released.strong.key,
        )


class SanctionedQuietPolicyTest(SprintLivenessCase):
    def assert_outbound_handoff_suppresses_role(
        self,
        *,
        participant_id: int,
        shell_id: int,
        sender_id: int,
        reply_to_id: int,
        token: str,
    ) -> None:
        self.monitor().resolve(self.assignment_message_id, "role suppressor fixture")
        inbound = self.messages.send(
            self.sprint_id,
            to_participant_id=participant_id,
            from_participant_id=sender_id,
            message_kind="notification",
            body=f"Actionable {token} work",
            actionable=True,
            declared_type="re-enter",
            idempotency_key=f"{token}:inbound",
        )
        self.assertEqual(
            "accepted", self.messages.mark_read(inbound.message_id, shell_id)
        )
        expectation = self.expectation(inbound.message_id)
        role_started_at = parse(expectation["accepted_at"])
        self.clock.at(role_started_at + timedelta(minutes=5))
        first = self.monitor().evaluate(self.sprint_id)
        self.assertEqual("observed", first[0].action)

        outbound = self.messages.send(
            self.sprint_id,
            to_participant_id=reply_to_id,
            from_participant_id=participant_id,
            message_kind="notification",
            body=f"{token} handoff awaiting reply",
            actionable=False,
            declared_type="re-enter",
            idempotency_key=f"{token}:outbound",
        )
        self.con.execute(
            "UPDATE wake_message SET created_at=? WHERE message_id=?",
            (
                stamp(role_started_at + timedelta(minutes=6)),
                outbound.message_id,
            ),
        )
        self.con.commit()
        self.clock.at(role_started_at + timedelta(minutes=10))
        outcomes = self.monitor().evaluate(self.sprint_id)

        self.assertEqual(1, len(outcomes))
        self.assertEqual(inbound.message_id, outcomes[0].message_id)
        self.assertEqual("sanctioned-quiet", outcomes[0].action)
        self.assertEqual([], self.message_rows("nudge"))
        event = self.con.execute(
            "SELECT payload FROM sprint_events "
            "WHERE event_type='liveness.sanctioned_quiet'"
        ).fetchone()
        self.assertEqual(
            {
                "expectation_message_id": inbound.message_id,
                "silence_episode": 1,
                "suppressor_kind": "outbound.handoff",
                "evidence_key": f"sprint.message:{outbound.message_id}",
            },
            json.loads(event["payload"]),
        )
        self.clock.at(role_started_at + timedelta(minutes=100))
        repeated = self.monitor().evaluate(self.sprint_id)
        self.assertEqual("sanctioned-quiet", repeated[0].action)
        self.assertEqual([], self.message_rows("escalation"))
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_events "
                "WHERE event_type='liveness.sanctioned_quiet'"
            ).fetchone()[0],
        )

    def test_reviewer_outbound_handoff_suppresses_ambiguous_silence(self) -> None:
        self.assert_outbound_handoff_suppresses_role(
            participant_id=self.reviewer_id,
            shell_id=2,
            sender_id=self.developer_id,
            reply_to_id=self.planner_id,
            token="reviewer",
        )

    def test_planner_outbound_handoff_suppresses_ambiguous_silence(self) -> None:
        self.assert_outbound_handoff_suppresses_role(
            participant_id=self.planner_id,
            shell_id=3,
            sender_id=self.reviewer_id,
            reply_to_id=self.developer_id,
            token="planner",
        )

    def test_existing_liveness_timing_is_unchanged(self) -> None:
        self.assertEqual(timedelta(minutes=5), sprint_liveness.EVALUATION_INTERVAL)
        self.assertEqual(timedelta(minutes=10), sprint_liveness.GRACE_WINDOW)
        self.assertEqual(timedelta(minutes=10), sprint_liveness.ESCALATION_WINDOW)

    def test_pending_pr_suppresses_nudge_once_per_episode_then_green_releases(self) -> None:
        registered_pr_id = self.add_pr_transition("pending", 1)
        monitor = self.monitor()

        self.advance(5)
        self.assertEqual("strong-evidence", monitor.evaluate(self.sprint_id)[0].action)
        self.advance(10)
        self.assertEqual(
            "sanctioned-quiet", monitor.evaluate(self.sprint_id)[0].action
        )
        self.advance(15)
        self.assertEqual(
            "sanctioned-quiet", monitor.evaluate(self.sprint_id)[0].action
        )
        self.assertEqual([], self.message_rows("nudge"))
        self.assertEqual([], self.message_rows("escalation"))
        events = self.con.execute(
            "SELECT payload FROM sprint_events "
            "WHERE event_type='liveness.sanctioned_quiet'"
        ).fetchall()
        self.assertEqual(1, len(events))
        self.assertEqual(
            {
                "expectation_message_id": self.assignment_message_id,
                "silence_episode": 2,
                "suppressor_kind": "pr.awaiting_transition",
                "evidence_key": "pr.transition:transition-first",
            },
            json.loads(events[0]["payload"]),
        )
        self.assertEqual(
            stamp(self.started_at + timedelta(minutes=20)),
            self.expectation()["next_evaluation_at"],
        )

        self.add_pr_transition(
            "green", 16, registered_pr_id=registered_pr_id, token="green"
        )
        self.advance(20)
        self.assertEqual("strong-evidence", monitor.evaluate(self.sprint_id)[0].action)
        self.advance(30)
        self.assertEqual("nudged", monitor.evaluate(self.sprint_id)[0].action)
        self.assertEqual(1, len(self.message_rows("nudge")))

    def test_current_failure_escalates_before_pending_pr_suppression(self) -> None:
        self.add_pr_transition("pending", 1)
        failure_at = self.started_at + timedelta(minutes=2)

        def failed(_participant, _accepted, _now):
            return (
                None,
                sprint_liveness.Evidence(
                    "process:missing:1",
                    "process.missing",
                    failure_at,
                    "failure",
                    "participant process is missing",
                ),
                None,
            )

        self.advance(5)
        outcome = self.monitor(failed).evaluate(self.sprint_id)[0]
        self.assertEqual("escalated", outcome.action)
        self.assertEqual([], self.message_rows("nudge"))
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_events "
                "WHERE event_type='liveness.sanctioned_quiet'"
            ).fetchone()[0],
        )
        payload = json.loads(self.message_rows("escalation")[0]["body"])
        self.assertEqual("process:missing:1", payload["failure"]["key"])

    def test_ci_stalled_backstop_wakes_planner_once_per_transition(self) -> None:
        registered_pr_id = self.add_pr_transition("pending", 0)
        monitor = self.monitor()
        self.advance(5)
        self.assertEqual("strong-evidence", monitor.evaluate(self.sprint_id)[0].action)

        self.advance(90)
        self.assertEqual(
            "sanctioned-quiet", monitor.evaluate(self.sprint_id)[0].action
        )
        backstops = self.message_rows("escalation")
        self.assertEqual(1, len(backstops))
        self.assertEqual(self.planner_id, backstops[0]["to_participant_id"])
        self.assertNotEqual(self.developer_id, backstops[0]["to_participant_id"])
        payload = json.loads(backstops[0]["body"])
        self.assertEqual(
            {
                "kind": "ci_stalled",
                "registered_pr_id": registered_pr_id,
                "repository": "acme/widget",
                "pr_number": 41,
                "head_sha": "f" * 40,
                "normalized_state": "pending",
                "transition_key": "transition-first",
                "pending_since": stamp(self.started_at),
                "pending_minutes": 90,
                "mandate": (
                    "Assess the stalled CI transition and attempt repair by "
                    "re-triggering checks, closing/reopening, or re-pushing. "
                    "Pause the Sprint if the runner is genuinely down."
                ),
                "planner_delivery_route": "delivery-time",
                "pause_option": True,
            },
            payload,
        )
        self.assertEqual([], self.message_rows("nudge"))

        self.advance(95)
        self.assertEqual(
            "sanctioned-quiet", monitor.evaluate(self.sprint_id)[0].action
        )
        self.assertEqual(1, len(self.message_rows("escalation")))
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_events "
                "WHERE event_type='liveness.ci_stalled'"
            ).fetchone()[0],
        )

        self.add_pr_transition(
            "green", 96, registered_pr_id=registered_pr_id, token="green"
        )
        self.advance(100)
        self.assertEqual("strong-evidence", monitor.evaluate(self.sprint_id)[0].action)
        self.add_pr_transition(
            "pending", 105, registered_pr_id=registered_pr_id, token="second"
        )
        self.advance(105)
        self.assertEqual("strong-evidence", monitor.evaluate(self.sprint_id)[0].action)
        self.advance(195)
        self.assertEqual(
            "sanctioned-quiet", monitor.evaluate(self.sprint_id)[0].action
        )
        self.assertEqual(2, len(self.message_rows("escalation")))
        self.assertEqual(
            ["transition-first", "transition-second"],
            [
                json.loads(row["body"])["transition_key"]
                for row in self.message_rows("escalation")
            ],
        )
        self.assertEqual([], self.message_rows("nudge"))

    def test_ci_stalled_backstop_does_not_require_a_live_expectation(self) -> None:
        registered_pr_id = self.add_pr_transition("pending", 0)
        self.assertTrue(
            self.monitor().resolve(
                self.assignment_message_id,
                "owner has no remaining live expectation",
            )
        )
        self.advance(90)

        self.assertEqual((), self.monitor().evaluate(self.sprint_id))
        backstops = self.message_rows("escalation")
        self.assertEqual(1, len(backstops))
        self.assertEqual(self.planner_id, backstops[0]["to_participant_id"])
        self.assertEqual(
            registered_pr_id,
            json.loads(backstops[0]["body"])["registered_pr_id"],
        )
        event = self.con.execute(
            "SELECT payload FROM sprint_events "
            "WHERE event_type='liveness.ci_stalled'"
        ).fetchone()
        self.assertEqual(
            {
                "registered_pr_id": registered_pr_id,
                "transition_key": "transition-first",
                "backstop_message_id": backstops[0]["message_id"],
            },
            json.loads(event["payload"]),
        )


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
            "SELECT active.chat_id FROM sprint_participants participant "
            "JOIN active_shell_chats active ON active.shell_id=participant.shell_id "
            "WHERE participant.participant_id=?",
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
            declared_type="new",
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


class ForceNewLivenessGateTest(SprintLivenessCase):
    def queue_force_new(self, key: str = "liveness-force-new"):
        return self.messages.send(
            self.sprint_id,
            to_participant_id=self.developer_id,
            from_participant_id=self.planner_id,
            work_unit_id=self.unit_id,
            message_kind="notification",
            body="next assignment waits for a fresh chat",
            actionable=False,
            declared_type="force-new",
            idempotency_key=key,
        )

    def test_pending_force_new_suppresses_immediate_failure_then_releases(self) -> None:
        force = self.queue_force_new()
        failure = sprint_liveness.Evidence(
            "run:failed:during-handoff",
            "run.failed",
            self.started_at + timedelta(minutes=1),
            "failure",
            "prior turn failed while the next assignment is pending",
        )

        self.advance(5)
        monitor = self.monitor(
            lambda _participant, _accepted, _now: (None, failure, None)
        )
        outcome = monitor.evaluate(self.sprint_id)[0]

        self.assertEqual("force-new-pending", outcome.action)
        self.assertEqual([], self.message_rows("escalation"))
        observed = self.expectation()
        self.assertEqual(failure.key, observed["last_failure_key"])
        self.assertEqual(stamp(self.clock.value), observed["last_evaluated_at"])

        delivered = sprint_message_delivery.SprintWakeDeliveryService(
            self.con
        ).deliver_once(
            "liveness-force-release",
            lambda _conversation, _prompt, _key: "liveness-force-release-run",
        )
        self.assertEqual(force.wake_id, delivered.wake_id)
        self.assertIsNone(self.messages.mark_read(force.message_id, 1))
        self.advance(10)
        released = monitor.evaluate(self.sprint_id)[0]
        self.assertEqual("escalated", released.action)
        self.assertEqual(1, len(self.message_rows("escalation")))

    def test_delivering_force_new_suppresses_nudge_until_terminal(self) -> None:
        force = self.queue_force_new("liveness-force-delivering")
        lease = sprint_message_delivery.SprintWakeDeliveryService(
            self.con, force_new_quiet_seconds=0
        ).claim_next("liveness-force-worker")
        self.assertEqual(force.wake_id, lease.wake_id)

        monitor = self.monitor()
        self.advance(5)
        self.assertEqual("strong-evidence", monitor.evaluate(self.sprint_id)[0].action)
        self.advance(20)
        self.assertEqual(
            "force-new-pending", monitor.evaluate(self.sprint_id)[0].action
        )
        self.assertEqual([], self.message_rows("nudge"))
        self.assertEqual([], self.message_rows("escalation"))

        self.con.execute(
            "UPDATE sprint_wake_outbox SET state='cancelled',claim_owner=NULL,"
            "claimed_at=NULL,lease_expires_at=NULL,quiet_since=NULL "
            "WHERE wake_id=?",
            (force.wake_id,),
        )
        self.con.commit()
        self.advance(25)
        released = monitor.evaluate(self.sprint_id)[0]
        self.assertEqual("nudged", released.action)
        self.assertEqual(1, len(self.message_rows("nudge")))

    def test_receiver_gate_covers_all_expectations_without_blocking_peer(self) -> None:
        second = self.messages.send(
            self.sprint_id,
            to_participant_id=self.developer_id,
            from_participant_id=self.planner_id,
            work_unit_id=self.unit_id,
            message_kind="notification",
            body="second developer expectation",
            actionable=True,
            declared_type="re-enter",
            idempotency_key="developer-second-expectation",
        )
        reviewer = self.messages.send(
            self.sprint_id,
            to_participant_id=self.reviewer_id,
            from_participant_id=self.developer_id,
            work_unit_id=self.unit_id,
            message_kind="review_request",
            body="reviewer expectation",
            actionable=True,
            declared_type="re-enter",
            idempotency_key="reviewer-peer-expectation",
        )
        self.assertEqual("accepted", self.messages.mark_read(second.message_id, 1))
        self.assertEqual("accepted", self.messages.mark_read(reviewer.message_id, 2))
        self.queue_force_new("receiver-wide-force")

        self.advance(10)
        outcomes = self.monitor().evaluate(self.sprint_id)
        self.assertEqual(
            [
                (self.assignment_message_id, "force-new-pending"),
                (second.message_id, "force-new-pending"),
                (reviewer.message_id, "nudged"),
            ],
            [(outcome.message_id, outcome.action) for outcome in outcomes],
        )
        self.assertEqual(
            [self.reviewer_id],
            [int(row["to_participant_id"]) for row in self.message_rows("nudge")],
        )


class DeliveryAndActivationTest(SprintLivenessCase):
    def test_planner_escalations_defer_chat_routing_and_coalesce(self) -> None:
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
            declared_type="new",
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
            "SELECT active.chat_id FROM sprint_participants participant "
            "LEFT JOIN active_shell_chats active "
            "ON active.shell_id=participant.shell_id "
            "WHERE participant.participant_id=?",
            (self.planner_id,),
        ).fetchone()
        self.assertIsNotNone(planner[0])
        self.assertEqual(
            set(),
            {"purpose", "parent_conversation_id", "context_packet"}
            & {
                row[1]
                for row in self.con.execute(
                    "PRAGMA table_info(sprint_participant_conversations)"
                )
            },
        )
        escalations = self.message_rows("escalation")
        self.assertEqual(2, len(escalations))
        self.assertEqual(
            ["delivery-time", "delivery-time"],
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
        self.assertEqual(
            1,
            len(
                {
                    int(row["wake_id"])
                    for row in self.con.execute(
                        "SELECT wake_id FROM sprint_wake_messages "
                        "WHERE message_id IN (?,?)",
                        (escalations[0]["message_id"], escalations[1]["message_id"]),
                    )
                }
            ),
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
            "SELECT COUNT(*) FROM wake_message"
        ).fetchone()[0]
        self.assertEqual((), self.monitor().evaluate(self.sprint_id))
        after = self.con.execute(
            "SELECT COUNT(*) FROM wake_message"
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
