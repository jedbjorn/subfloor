"""Stage 9 gates for conformance follow-ups and report compilation."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
MIGRATIONS = ENGINE / "migrations"
MIGRATION = MIGRATIONS / "0150_sprint_close_reports.sql"
SURFACE_MIGRATION = MIGRATIONS / "0152_sprint_surface_completion.sql"
FINAL_REPORT = "Reviewer final report: integrated Sprint scope conforms."
COMPLETION_REASON = "Reviewer approved integrated conformance"
TERMINAL_OUTCOME = "accepted"


def rendered_notification(
    sprint_id: int,
    report_id: int,
    final_report_id: int,
    followup_ids: tuple[int, ...],
) -> str:
    followups = ",".join(str(value) for value in followup_ids) or "none"
    return (
        f"Sprint {sprint_id} completed by Reviewer conformance. "
        f"conformance_report_id={report_id}; final_report_id={final_report_id}; "
        f"followup_ids={followups}; outcome={TERMINAL_OUTCOME}; "
        "cleanup_state=pending. Managed participant worktrees are not reusable "
        "until the engine-authored cleanup receipt reports succeeded.\n\n"
        f"Reason: {COMPLETION_REASON}"
    )

sys.path[:0] = [str(ENGINE / "scripts"), str(ROOT / "tests")]
import sprint_close  # noqa: E402
import sprint_domain  # noqa: E402
import sprint_message_delivery  # noqa: E402
from test_sprint_v2_domain import SprintDomainCase, apply_schema  # noqa: E402


class SprintCloseCase(SprintDomainCase):
    def setUp(self) -> None:
        super().setUp()
        self.sprint_id, self.unit_id = self.create_sprint()
        self.store.arm(self.sprint_id, 3)
        self.close = sprint_close.SprintCloseStore(self.con)
        self.document_id = int(
            self.con.execute(
                "SELECT document_id FROM sprint_specs WHERE sprint_id=?",
                (self.sprint_id,),
            ).fetchone()[0]
        )

    def finding(self, **overrides):
        finding = {
            "severity": "Major",
            "title": "Integrated seam diverges",
            "body": "The delivered seam does not preserve the bound contract.",
            "spec_document_id": self.document_id,
            "work_unit_id": self.unit_id,
        }
        finding.update(overrides)
        return finding

    def record_conformance(self, *args, **kwargs):
        kwargs.setdefault("reason", COMPLETION_REASON)
        kwargs.setdefault("terminal_outcome", TERMINAL_OUTCOME)
        return self.close.record_conformance(*args, **kwargs)

    def add_participant(self, shell_id: int, role: str) -> int:
        flavor = "dev" if role == "developer" else role
        prefix = "DEV" if role == "developer" else "REV"
        self.con.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (?,?,?,?,?,1)",
            (shell_id, f"{role} {shell_id}", f"{prefix}{shell_id}", flavor, "prompt"),
        )
        participant_id = int(
            self.con.execute(
                "INSERT INTO sprint_participants "
                "(sprint_id,shell_id,role,harness,model,effort) "
                "VALUES (?,?,?,'codex','model','high')",
                (self.sprint_id, shell_id, role),
            ).lastrowid
        )
        self.con.commit()
        return participant_id

    def activate_chat(
        self,
        shell_id: int,
        key: str,
        *,
        linked: bool = True,
    ) -> str:
        conversation_id = str(
            self.con.execute(
                "INSERT INTO conversations "
                "(shell_id,owner_user_id,harness,worktree,state,conversation_scope,"
                "creation_idempotency_key,creation_request_hash) "
                "VALUES (?,1,'codex','/tmp/work','idle',?,?,?) "
                "RETURNING conversation_id",
                (shell_id, "sprint" if linked else "normal", key, key),
            ).fetchone()[0]
        )
        if linked:
            participant_id = int(
                self.con.execute(
                    "SELECT participant_id FROM sprint_participants "
                    "WHERE sprint_id=? AND shell_id=?",
                    (self.sprint_id, shell_id),
                ).fetchone()[0]
            )
            self.con.execute(
                "INSERT INTO sprint_participant_conversations "
                "(sprint_participant_id,conversation_id) VALUES (?,?)",
                (participant_id, conversation_id),
            )
        self.con.execute(
            "INSERT INTO active_shell_chats (shell_id,chat_id) VALUES (?,?)",
            (shell_id, conversation_id),
        )
        self.con.commit()
        return conversation_id


class SprintCloseMigrationTest(unittest.TestCase):
    def test_forward_migration_adds_followups_without_rewriting_reports(self):
        with closing(sqlite3.connect(":memory:")) as con:
            apply_schema(con, through="0149_sprint_liveness_monitor.sql")
            con.execute(
                "INSERT INTO users (user_id,username) VALUES (1,'operator')"
            )
            con.execute(
                "INSERT INTO shells "
                "(shell_id,display_name,shortname,system_prompt,user_id) "
                "VALUES (1,'Planner','PLN1','prompt',1)"
            )
            feature_id = int(
                con.execute(
                    "INSERT INTO roadmap (title) VALUES ('Feature')"
                ).lastrowid
            )
            sprint_id = int(
                con.execute(
                    "INSERT INTO sprints "
                    "(feature_id,originating_planner_shell_id) VALUES (?,1)",
                    (feature_id,),
                ).lastrowid
            )
            report_id = int(
                con.execute(
                    "INSERT INTO sprint_reports (sprint_id,report_kind,body) "
                    "VALUES (?,'pause','existing')",
                    (sprint_id,),
                ).lastrowid
            )

            con.executescript(MIGRATION.read_text())

            self.assertEqual(
                (report_id, "existing", None),
                tuple(
                    con.execute(
                        "SELECT report_id,body,idempotency_key FROM sprint_reports"
                    ).fetchone()
                ),
            )
            self.assertIsNotNone(
                con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='sprint_followups'"
                ).fetchone()
            )
            self.assertEqual([], con.execute("PRAGMA foreign_key_check").fetchall())

    def test_surface_migration_preserves_code_units_and_accepts_non_code_result(self):
        with closing(sqlite3.connect(":memory:")) as con:
            apply_schema(con, through="0151_seed_sprint_v2_skills.sql")
            con.execute("INSERT INTO users (user_id,username) VALUES (1,'operator')")
            con.executemany(
                "INSERT INTO shells "
                "(shell_id,display_name,shortname,system_prompt,user_id) "
                "VALUES (?,?,?,?,1)",
                (
                    (1, "Developer", "DEV1", "prompt"),
                    (2, "Reviewer", "REV1", "prompt"),
                    (3, "Planner", "PLN1", "prompt"),
                ),
            )
            feature_id = int(
                con.execute("INSERT INTO roadmap (title) VALUES ('Feature')").lastrowid
            )
            sprint_id = int(
                con.execute(
                    "INSERT INTO sprints "
                    "(feature_id,originating_planner_shell_id) VALUES (?,3)",
                    (feature_id,),
                ).lastrowid
            )
            unit_id = int(
                con.execute(
                    "INSERT INTO sprint_work_units "
                    "(sprint_id,assigned_shell_id,reviewer_shell_id,title,"
                    "expected_output) VALUES (?,1,2,'Existing','Code PR')",
                    (sprint_id,),
                ).lastrowid
            )

            con.executescript(SURFACE_MIGRATION.read_text())

            self.assertEqual(
                ("code", None),
                tuple(
                    con.execute(
                        "SELECT output_kind,completion_result "
                        "FROM sprint_work_units WHERE work_unit_id=?",
                        (unit_id,),
                    ).fetchone()
                ),
            )
            con.execute(
                "UPDATE sprint_work_units SET output_kind='report_only',"
                "completion_result='Report #77' WHERE work_unit_id=?",
                (unit_id,),
            )
            self.assertEqual(
                ("report_only", "Report #77"),
                tuple(
                    con.execute(
                        "SELECT output_kind,completion_result "
                        "FROM sprint_work_units WHERE work_unit_id=?",
                        (unit_id,),
                    ).fetchone()
                ),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "UPDATE sprint_work_units SET output_kind='spreadsheet' "
                    "WHERE work_unit_id=?",
                    (unit_id,),
                )


class ConformanceFollowupTest(SprintCloseCase):
    def test_close_payloads_accept_8000_and_reject_8001_without_partial_writes(self):
        with self.assertRaisesRegex(
            ValueError,
            "conformance body is 8001 characters; maximum is 8000",
        ):
            self.record_conformance(
                self.sprint_id,
                2,
                body="x" * 8001,
                findings=[],
                final_report=FINAL_REPORT,
                idempotency_key="oversize-conformance",
            )
        with self.assertRaisesRegex(
            ValueError,
            "finding body is 8001 characters; maximum is 8000",
        ):
            self.record_conformance(
                self.sprint_id,
                2,
                body="bounded",
                findings=[self.finding(body="x" * 8001)],
                final_report=FINAL_REPORT,
                idempotency_key="oversize-finding",
            )
        with self.assertRaisesRegex(ValueError, "final report body is required"):
            self.record_conformance(
                self.sprint_id,
                2,
                body="bounded",
                findings=[],
                final_report=" ",
                idempotency_key="empty-final-report",
            )
        with self.assertRaisesRegex(
            ValueError,
            "final report body is 8001 characters; maximum is 8000",
        ):
            self.record_conformance(
                self.sprint_id,
                2,
                body="bounded",
                findings=[],
                final_report="x" * 8001,
                idempotency_key="oversize-final-report",
            )
        for field, error in (
            ("reason", "completion reason is required"),
            ("terminal_outcome", "terminal outcome is required"),
        ):
            kwargs = {
                "body": "bounded",
                "findings": [],
                "final_report": FINAL_REPORT,
                "reason": COMPLETION_REASON,
                "terminal_outcome": TERMINAL_OUTCOME,
                "idempotency_key": f"empty-{field}",
                field: " ",
            }
            with self.assertRaisesRegex(ValueError, error):
                self.close.record_conformance(self.sprint_id, 2, **kwargs)
        self.assertEqual(
            (0, 0),
            tuple(
                self.con.execute(
                    "SELECT (SELECT COUNT(*) FROM sprint_reports WHERE sprint_id=?),"
                    "(SELECT COUNT(*) FROM sprint_followups WHERE sprint_id=?)",
                    (self.sprint_id, self.sprint_id),
                ).fetchone()
            ),
        )

        conformance = self.record_conformance(
            self.sprint_id,
            2,
            body="x" * 8000,
            findings=[self.finding(body="x" * 8000)],
                final_report=FINAL_REPORT,
            idempotency_key="bounded-conformance",
        )
        self.assertTrue(conformance.created)
        self.con.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (5,'FnB','FNB','admin','prompt',1)"
        )
        self.con.commit()
        with self.assertRaisesRegex(
            ValueError,
            "follow-up resolution is 8001 characters; maximum is 8000",
        ):
            self.close.disposition_followup(
                self.sprint_id,
                conformance.followup_ids[0],
                5,
                disposition="resolved",
                resolution="x" * 8001,
            )
        self.assertEqual(
            ("pending", None),
            tuple(
                self.con.execute(
                    "SELECT disposition,resolution FROM sprint_followups "
                    "WHERE followup_id=?",
                    (conformance.followup_ids[0],),
                ).fetchone()
            ),
        )
        self.assertTrue(
            self.close.disposition_followup(
                self.sprint_id,
                conformance.followup_ids[0],
                5,
                disposition="resolved",
                resolution="x" * 8000,
            )
        )

        self.assertEqual(
            (1, FINAL_REPORT),
            tuple(
                self.con.execute(
                    "SELECT COUNT(*),body FROM sprint_reports WHERE sprint_id=? "
                    "AND report_kind='final'",
                    (self.sprint_id,),
                ).fetchone()
            ),
        )

    def test_database_rejects_cross_sprint_report_and_spec_links(self):
        other_sprint_id, _ = self.create_sprint()
        other_report_id = int(
            self.con.execute(
                "INSERT INTO sprint_reports (sprint_id,report_kind,body) "
                "VALUES (?,'pause','other Sprint')",
                (other_sprint_id,),
            ).lastrowid
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "another Sprint"):
            self.con.execute(
                "INSERT INTO sprint_followups "
                "(sprint_id,source_report_id,severity,title,body,idempotency_key) "
                "VALUES (?,?,'Low','Cross report','Bad link','cross-report')",
                (self.sprint_id, other_report_id),
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "not bound"):
            self.con.execute(
                "INSERT INTO sprint_followups "
                "(sprint_id,source_report_id,severity,title,body,"
                "spec_document_id,idempotency_key) "
                "VALUES (?,?,'Low','Cross spec','Bad link',?,'cross-spec')",
                (other_sprint_id, other_report_id, self.document_id),
            )

    def test_findings_become_followups_without_creating_fix_work(self):
        before_units = [
            tuple(row)
            for row in self.con.execute(
                "SELECT work_unit_id,disposition FROM sprint_work_units "
                "WHERE sprint_id=? ORDER BY work_unit_id",
                (self.sprint_id,),
            )
        ]

        receipt = self.record_conformance(
            self.sprint_id,
            2,
            body="Conformance found one integrated departure.",
            findings=[self.finding()],
            final_report=FINAL_REPORT,
            idempotency_key="conformance-pass-1",
        )

        self.assertTrue(receipt.created)
        self.assertEqual(1, len(receipt.followup_ids))
        followup = self.con.execute(
            "SELECT sprint_id,source_report_id,severity,title,body,"
            "spec_document_id,work_unit_id,disposition "
            "FROM sprint_followups WHERE followup_id=?",
            (receipt.followup_ids[0],),
        ).fetchone()
        self.assertEqual(
            (
                self.sprint_id,
                receipt.report_id,
                "Major",
                "Integrated seam diverges",
                "The delivered seam does not preserve the bound contract.",
                self.document_id,
                self.unit_id,
                "pending",
            ),
            tuple(followup),
        )
        self.assertEqual(
            before_units,
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT work_unit_id,disposition FROM sprint_work_units "
                    "WHERE sprint_id=? ORDER BY work_unit_id",
                    (self.sprint_id,),
                )
            ],
            "conformance never opens an in-Sprint fix lane",
        )
        event = self.con.execute(
            "SELECT payload FROM sprint_events WHERE sprint_id=? "
            "AND event_type='conformance.recorded'",
            (self.sprint_id,),
        ).fetchone()
        self.assertEqual(
            [receipt.followup_ids[0]], json.loads(event["payload"])["followup_ids"]
        )
        payload = json.loads(event["payload"])
        self.assertEqual(receipt.planner_message_id, payload["planner_message_id"])
        self.assertEqual(receipt.planner_wake_id, payload["planner_wake_id"])
        final_report = self.con.execute(
            "SELECT report_kind,author_shell_id,body,idempotency_key "
            "FROM sprint_reports WHERE report_id=?",
            (receipt.final_report_id,),
        ).fetchone()
        self.assertEqual(
            (
                "final",
                2,
                FINAL_REPORT,
                "conformance-pass-1:final-report",
            ),
            tuple(final_report),
        )
        notification = self.con.execute(
            "SELECT message.sender_shell_id,message.receiver_shell_id,"
            "message.sprint_id,message.from_participant_id,"
            "message.to_participant_id,message.work_unit_id,"
            "message.message_kind,message.body,message.declared_type,"
            "message.actionable,message.idempotency_key "
            "FROM wake_message message "
            "WHERE message.message_id=?",
            (receipt.planner_message_id,),
        ).fetchone()
        self.assertEqual(
            (
                2,
                3,
                None,
                None,
                None,
                None,
                "notification",
                rendered_notification(
                    self.sprint_id,
                    receipt.report_id,
                    receipt.final_report_id,
                    receipt.followup_ids,
                ),
                "re-enter",
                0,
                "conformance-pass-1:planner-completed",
            ),
            tuple(notification),
        )
        lifecycle = self.con.execute(
            "SELECT lifecycle,terminal_outcome,completed_at FROM sprints "
            "WHERE sprint_id=?",
            (self.sprint_id,),
        ).fetchone()
        self.assertEqual(("completed", TERMINAL_OUTCOME), tuple(lifecycle)[:2])
        self.assertIsNotNone(lifecycle["completed_at"])
        lifecycle_event = self.con.execute(
            "SELECT actor_kind,actor_shell_id,payload FROM sprint_events "
            "WHERE sprint_id=? AND event_type='lifecycle.completed'",
            (self.sprint_id,),
        ).fetchone()
        self.assertEqual(("participant", 2), tuple(lifecycle_event)[:2])
        self.assertEqual(
            {
                "closed_conversation_ids": [],
                "from": "armed",
                "reason": COMPLETION_REASON,
                "via": "conformance",
                "idempotency_key": "conformance-pass-1",
            },
            json.loads(lifecycle_event["payload"]),
        )
        self.assertEqual(
            [(receipt.planner_wake_id, receipt.planner_message_id)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT wake_id,message_id FROM sprint_wake_messages "
                    "WHERE message_id=?",
                    (receipt.planner_message_id,),
                )
            ],
        )

    def test_conformance_closes_only_linked_nonretained_chats_and_replay_is_idle(
        self,
    ):
        self.add_participant(5, "reviewer")
        self.add_participant(6, "developer")
        developer_chat = self.activate_chat(1, "linked-developer")
        author_chat = self.activate_chat(2, "linked-author")
        planner_chat = self.activate_chat(3, "linked-planner")
        other_reviewer_chat = self.activate_chat(5, "linked-other-reviewer")
        former_linked_chat = self.activate_chat(6, "linked-former-developer")
        self.con.execute(
            "UPDATE conversations SET state='closed',closed_at=datetime('now') "
            "WHERE conversation_id=?",
            (former_linked_chat,),
        )
        unrelated_chat = self.activate_chat(6, "unrelated-normal", linked=False)
        notified: list[str] = []

        def notify_after_commit(conversation_id: str) -> int:
            self.assertFalse(self.con.in_transaction)
            state = self.con.execute(
                "SELECT state FROM conversations WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()[0]
            self.assertEqual("closed", state)
            notified.append(conversation_id)
            return 1

        with mock.patch.object(
            sprint_close.conversation_events,
            "notify",
            side_effect=notify_after_commit,
        ):
            first = self.record_conformance(
                self.sprint_id,
                2,
                body="Close only this Sprint's eligible chats.",
                findings=[],
                final_report=FINAL_REPORT,
                idempotency_key="chat-cleanup",
            )

        self.assertTrue(first.created)
        self.assertEqual([developer_chat, other_reviewer_chat], notified)
        self.assertEqual(
            [
                (developer_chat, "closed", 1),
                (author_chat, "idle", 0),
                (planner_chat, "idle", 0),
                (other_reviewer_chat, "closed", 1),
                (unrelated_chat, "idle", 0),
            ],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT conversation_id,state,closed_at IS NOT NULL "
                    "FROM conversations WHERE conversation_id IN (?,?,?,?,?) "
                    "ORDER BY CASE conversation_id "
                    "WHEN ? THEN 1 WHEN ? THEN 2 WHEN ? THEN 3 WHEN ? THEN 4 ELSE 5 END",
                    (
                        developer_chat,
                        author_chat,
                        planner_chat,
                        other_reviewer_chat,
                        unrelated_chat,
                        developer_chat,
                        author_chat,
                        planner_chat,
                        other_reviewer_chat,
                    ),
                )
            ],
        )
        self.assertEqual(
            [(2, author_chat), (3, planner_chat), (6, unrelated_chat)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT shell_id,chat_id FROM active_shell_chats ORDER BY shell_id"
                )
            ],
        )
        close_events = [
            (row["conversation_id"], json.loads(row["payload"]))
            for row in self.con.execute(
                "SELECT conversation_id,payload FROM conversation_events "
                "WHERE event_type='conversation.closed' "
                "AND json_extract(payload,'$.reason')='sprint_completed' "
                "ORDER BY event_id"
            )
        ]
        self.assertEqual(
            [
                (
                    developer_chat,
                    {
                        "reason": "sprint_completed",
                        "retained_shell_ids": [2, 3],
                        "sprint_id": self.sprint_id,
                        "state": "closed",
                    },
                ),
                (
                    other_reviewer_chat,
                    {
                        "reason": "sprint_completed",
                        "retained_shell_ids": [2, 3],
                        "sprint_id": self.sprint_id,
                        "state": "closed",
                    },
                ),
            ],
            close_events,
        )
        lifecycle_payload = json.loads(
            self.con.execute(
                "SELECT payload FROM sprint_events WHERE sprint_id=? "
                "AND event_type='lifecycle.completed'",
                (self.sprint_id,),
            ).fetchone()[0]
        )
        self.assertEqual(
            [developer_chat, other_reviewer_chat],
            lifecycle_payload["closed_conversation_ids"],
        )

        later_chat = self.activate_chat(1, "post-completion-normal", linked=False)
        with mock.patch.object(sprint_close.conversation_events, "notify") as notify:
            replay = self.record_conformance(
                self.sprint_id,
                2,
                body="Close only this Sprint's eligible chats.",
                findings=[],
                final_report=FINAL_REPORT,
                idempotency_key="chat-cleanup",
            )
        self.assertFalse(replay.created)
        notify.assert_not_called()
        self.assertEqual(
            ("idle", 0, later_chat),
            tuple(
                self.con.execute(
                    "SELECT conversation.state,conversation.closed_at IS NOT NULL,"
                    "active.chat_id FROM conversations conversation "
                    "JOIN active_shell_chats active "
                    "ON active.chat_id=conversation.conversation_id "
                    "WHERE conversation.conversation_id=?",
                    (later_chat,),
                ).fetchone()
            ),
        )
        with (
            mock.patch.object(sprint_close.conversation_events, "notify") as notify,
            self.assertRaisesRegex(
                sprint_domain.SprintInvariantError,
                "different input",
            ),
        ):
            self.record_conformance(
                self.sprint_id,
                2,
                body="Divergent replay body.",
                findings=[],
                final_report=FINAL_REPORT,
                idempotency_key="chat-cleanup",
            )
        notify.assert_not_called()
        self.assertEqual(
            ("idle", 0, later_chat),
            tuple(
                self.con.execute(
                    "SELECT conversation.state,conversation.closed_at IS NOT NULL,"
                    "active.chat_id FROM conversations conversation "
                    "JOIN active_shell_chats active "
                    "ON active.chat_id=conversation.conversation_id "
                    "WHERE conversation.conversation_id=?",
                    (later_chat,),
                ).fetchone()
            ),
        )
        self.assertEqual(
            2,
            self.con.execute(
                "SELECT COUNT(*) FROM conversation_events "
                "WHERE event_type='conversation.closed' "
                "AND json_extract(payload,'$.reason')='sprint_completed'"
            ).fetchone()[0],
        )

    def test_conformance_close_failure_rolls_back_reports_lifecycle_and_chats(self):
        self.add_participant(5, "reviewer")
        developer_chat = self.activate_chat(1, "rollback-developer")
        other_reviewer_chat = self.activate_chat(5, "rollback-reviewer")
        original_close = sprint_domain.active_chat_registry.close_for_displacement
        close_calls = 0

        def fail_second_close(con, shell_id, *, allow_live_process):
            nonlocal close_calls
            close_calls += 1
            if close_calls == 2:
                raise sprint_domain.active_chat_registry.ActiveChatError(
                    "injected second close failure"
                )
            return original_close(
                con,
                shell_id,
                allow_live_process=allow_live_process,
            )

        with (
            mock.patch.object(
                sprint_domain.active_chat_registry,
                "close_for_displacement",
                side_effect=fail_second_close,
            ),
            mock.patch.object(sprint_close.conversation_events, "notify") as notify,
            self.assertRaisesRegex(
                sprint_domain.active_chat_registry.ActiveChatError,
                "injected second close failure",
            ),
        ):
            self.record_conformance(
                self.sprint_id,
                2,
                body="This transaction must roll back.",
                findings=[self.finding()],
                final_report=FINAL_REPORT,
                idempotency_key="chat-cleanup-rollback",
            )

        notify.assert_not_called()
        self.assertEqual(2, close_calls)
        self.assertEqual(
            ("armed", None, 0, 0, 0, 0, 0),
            tuple(
                self.con.execute(
                    "SELECT sprint.lifecycle,sprint.terminal_outcome,"
                    "(SELECT COUNT(*) FROM sprint_reports WHERE sprint_id=?),"
                    "(SELECT COUNT(*) FROM sprint_followups WHERE sprint_id=?),"
                    "(SELECT COUNT(*) FROM wake_message "
                    " WHERE idempotency_key='chat-cleanup-rollback:planner-completed'),"
                    "(SELECT COUNT(*) FROM sprint_events WHERE sprint_id=? "
                    " AND event_type='lifecycle.completed'),"
                    "(SELECT COUNT(*) FROM conversation_events "
                    " WHERE event_type='conversation.closed' "
                    " AND json_extract(payload,'$.reason')='sprint_completed') "
                    "FROM sprints sprint WHERE sprint.sprint_id=?",
                    (self.sprint_id,) * 4,
                ).fetchone()
            ),
        )
        self.assertEqual(
            [(1, developer_chat), (5, other_reviewer_chat)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT shell_id,chat_id FROM active_shell_chats "
                    "WHERE shell_id IN (1,5) ORDER BY shell_id"
                )
            ],
        )
        self.assertEqual(
            sorted(
                [
                    (developer_chat, "idle", 0),
                    (other_reviewer_chat, "idle", 0),
                ]
            ),
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT conversation_id,state,closed_at IS NOT NULL "
                    "FROM conversations WHERE conversation_id IN (?,?) "
                    "ORDER BY conversation_id",
                    tuple(sorted((developer_chat, other_reviewer_chat))),
                )
            ],
        )

    def test_paused_conformance_precondition_preserves_linked_chat(self):
        developer_chat = self.activate_chat(1, "paused-developer")
        self.store.pause(
            self.sprint_id,
            sprint_domain.LifecycleActor("participant", 1),
            reason="pause before conformance",
        )

        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError,
            "conformance requires an armed Sprint, not paused",
        ):
            self.record_conformance(
                self.sprint_id,
                2,
                body="Rejected while paused.",
                findings=[],
                final_report=FINAL_REPORT,
                idempotency_key="paused-conformance",
            )

        self.assertEqual(
            ("paused", developer_chat, "idle", 0, 0, 0),
            tuple(
                self.con.execute(
                    "SELECT sprint.lifecycle,active.chat_id,conversation.state,"
                    "conversation.closed_at IS NOT NULL,"
                    "(SELECT COUNT(*) FROM sprint_reports WHERE sprint_id=? "
                    " AND report_kind IN ('conformance','final')),"
                    "(SELECT COUNT(*) FROM conversation_events "
                    " WHERE event_type='conversation.closed' "
                    " AND json_extract(payload,'$.reason')='sprint_completed') "
                    "FROM sprints sprint JOIN active_shell_chats active "
                    "ON active.shell_id=1 JOIN conversations conversation "
                    "ON conversation.conversation_id=active.chat_id "
                    "WHERE sprint.sprint_id=?",
                    (self.sprint_id, self.sprint_id),
                ).fetchone()
            ),
        )

    def test_planner_receipt_is_informational_after_automatic_completion(self):
        receipt = self.record_conformance(
            self.sprint_id,
            2,
            body="Integrated conformance is complete.",
            findings=[],
            final_report=FINAL_REPORT,
            idempotency_key="liveness-pass",
        )
        self.assertTrue(receipt.completed)
        self.assertEqual(
            ("completed", TERMINAL_OUTCOME),
            tuple(
                self.con.execute(
                    "SELECT lifecycle,terminal_outcome FROM sprints "
                    "WHERE sprint_id=?",
                    (self.sprint_id,),
                ).fetchone()
            ),
        )
        self.assertIsNone(
            sprint_message_delivery.SprintMessageStore(self.con).mark_read(
                receipt.planner_message_id,
                3,
            )
        )
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_liveness_expectations "
                "WHERE message_id=?",
                (receipt.planner_message_id,),
            ).fetchone()[0],
        )
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_liveness_expectations "
                "WHERE sprint_id=? AND resolved_at IS NULL",
                (self.sprint_id,),
            ).fetchone()[0],
        )

    def test_planner_notification_failure_rolls_back_every_closeout_write(self):
        self.con.execute(
            "CREATE TRIGGER reject_planner_notification BEFORE INSERT ON wake_message "
            "WHEN NEW.idempotency_key='rollback-pass:planner-completed' "
            "BEGIN SELECT RAISE(ABORT,'reject Planner notification'); END"
        )
        self.con.commit()

        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "reject Planner notification"
        ):
            self.record_conformance(
                self.sprint_id,
                2,
                body="This report must roll back.",
                findings=[self.finding()],
                final_report=FINAL_REPORT,
                idempotency_key="rollback-pass",
            )

        self.assertEqual(
            (0, 0, 0, 0, "armed", None),
            tuple(
                self.con.execute(
                    "SELECT "
                    "(SELECT COUNT(*) FROM sprint_reports WHERE sprint_id=?),"
                    "(SELECT COUNT(*) FROM sprint_followups WHERE sprint_id=?),"
                    "(SELECT COUNT(*) FROM sprint_events WHERE sprint_id=? "
                    " AND event_type='conformance.recorded'),"
                    "(SELECT COUNT(*) FROM wake_message "
                    " WHERE idempotency_key='rollback-pass:planner-completed'),"
                    "lifecycle,terminal_outcome FROM sprints WHERE sprint_id=?",
                    (self.sprint_id,) * 4,
                ).fetchone()
            ),
        )

    def test_retry_replays_exactly_and_conflicting_input_is_rejected(self):
        first = self.record_conformance(
            self.sprint_id,
            2,
            body="Review body",
            findings=[self.finding(severity="Low")],
            final_report=FINAL_REPORT,
            idempotency_key="same-pass",
        )
        replay = self.record_conformance(
            self.sprint_id,
            2,
            body="Review body",
            findings=[self.finding(severity="Low")],
            final_report=FINAL_REPORT,
            idempotency_key="same-pass",
        )
        self.assertFalse(replay.created)
        self.assertEqual(first.report_id, replay.report_id)
        self.assertEqual(first.followup_ids, replay.followup_ids)
        self.assertEqual(first.final_report_id, replay.final_report_id)
        self.assertEqual(first.planner_message_id, replay.planner_message_id)
        self.assertEqual(first.planner_wake_id, replay.planner_wake_id)
        self.assertTrue(replay.completed)

        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError, "different findings"
        ):
            self.record_conformance(
                self.sprint_id,
                2,
                body="Review body",
                findings=[self.finding(severity="Critical")],
                final_report=FINAL_REPORT,
                idempotency_key="same-pass",
            )
        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError,
            "different final report",
        ):
            self.record_conformance(
                self.sprint_id,
                2,
                body="Review body",
                findings=[self.finding(severity="Low")],
                final_report="Changed final report.",
                idempotency_key="same-pass",
            )
        for field, value in (
            ("reason", "Changed completion reason"),
            ("terminal_outcome", "changed-outcome"),
        ):
            kwargs = {
                "body": "Review body",
                "findings": [self.finding(severity="Low")],
                "final_report": FINAL_REPORT,
                "idempotency_key": "same-pass",
                field: value,
            }
            with self.assertRaisesRegex(
                sprint_domain.SprintInvariantError, "different completion"
            ):
                self.record_conformance(self.sprint_id, 2, **kwargs)
        self.assertEqual(
            (2, 1, 1, 1),
            tuple(
                self.con.execute(
                    "SELECT "
                    "(SELECT COUNT(*) FROM sprint_reports WHERE sprint_id=?),"
                    "(SELECT COUNT(*) FROM sprint_followups WHERE sprint_id=?),"
                    "(SELECT COUNT(*) FROM wake_message "
                    " WHERE idempotency_key='same-pass:planner-completed'),"
                    "(SELECT COUNT(*) FROM sprint_events WHERE sprint_id=? "
                    " AND event_type='lifecycle.completed')",
                    (self.sprint_id,) * 3,
                ).fetchone()
            ),
        )

    def test_non_object_finding_is_rejected_before_any_report_write(self):
        with self.assertRaisesRegex(TypeError, "must be an object"):
            self.record_conformance(
                self.sprint_id,
                2,
                body="Malformed findings",
                findings=["not an object"],
                final_report=FINAL_REPORT,
                idempotency_key="malformed-findings",
            )
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_reports WHERE sprint_id=? "
                "AND report_kind='conformance'",
                (self.sprint_id,),
            ).fetchone()[0],
        )

    def test_wrong_role_and_cross_sprint_links_leave_no_report(self):
        before = self.con.execute(
            "SELECT COUNT(*) FROM sprint_reports WHERE sprint_id=?",
            (self.sprint_id,),
        ).fetchone()[0]
        with self.assertRaisesRegex(
            sprint_domain.SprintAuthorityError, "participating Reviewer"
        ):
            self.record_conformance(
                self.sprint_id,
                1,
                body="Not a review",
                findings=[],
                final_report=FINAL_REPORT,
                idempotency_key="wrong-role",
            )
        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError, "not bound"
        ):
            self.record_conformance(
                self.sprint_id,
                2,
                body="Bad link",
                findings=[self.finding(spec_document_id=999)],
                final_report=FINAL_REPORT,
                idempotency_key="bad-link",
            )
        self.assertEqual(
            before,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_reports WHERE sprint_id=?",
                (self.sprint_id,),
            ).fetchone()[0],
        )

    def test_final_report_is_idempotent_and_replays_after_completion(self):
        first = self.close.record_final_report(
            self.sprint_id,
            3,
            body="Delivered scope, conformance, judgments, and follow-ups.",
            idempotency_key="final-synthesis",
        )
        self.assertTrue(first.created)
        sprint_domain.SprintLifecycleStore(self.con).transition(
            self.sprint_id,
            "completed",
            sprint_domain.LifecycleActor("planner", 3),
            reason="close",
            terminal_outcome="accepted",
        )
        replay = self.close.record_final_report(
            self.sprint_id,
            3,
            body="Delivered scope, conformance, judgments, and follow-ups.",
            idempotency_key="final-synthesis",
        )
        self.assertFalse(replay.created)
        self.assertEqual(first.report_id, replay.report_id)
        self.assertEqual(
            (
                "final",
                3,
                "Delivered scope, conformance, judgments, and follow-ups.",
            ),
            tuple(
                self.con.execute(
                    "SELECT report_kind,author_shell_id,body FROM sprint_reports "
                    "WHERE report_id=?",
                    (first.report_id,),
                ).fetchone()
            ),
        )
        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError, "different final report"
        ):
            self.close.record_final_report(
                self.sprint_id,
                3,
                body="Conflicting report",
                idempotency_key="final-synthesis",
            )
        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError, "different final report"
        ):
            self.close.record_final_report(
                self.sprint_id,
                3,
                body="Delivered scope, conformance, judgments, and follow-ups.",
                idempotency_key="second-final-key",
            )
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_reports WHERE sprint_id=? "
                "AND report_kind='final'",
                (self.sprint_id,),
            ).fetchone()[0],
        )

    def test_participating_reviewer_cannot_record_final_report(self):
        with self.assertRaisesRegex(
            sprint_domain.SprintAuthorityError, "owning Planner or FnB"
        ):
            self.close.record_final_report(
                self.sprint_id,
                2,
                body="Reviewer must not author the final synthesis.",
                idempotency_key="reviewer-final-report",
            )

        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_reports WHERE sprint_id=? "
                "AND report_kind='final'",
                (self.sprint_id,),
            ).fetchone()[0],
        )
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_events WHERE sprint_id=? "
                "AND event_type='final_report.recorded'",
                (self.sprint_id,),
            ).fetchone()[0],
        )

    def test_only_fnb_dispositions_followup_and_only_pending_is_unresolved(self):
        self.con.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (5,'FnB','FNB','admin','prompt',1)"
        )
        self.con.commit()
        receipt = self.record_conformance(
            self.sprint_id,
            2,
            body="Two follow-ups",
            findings=[
                self.finding(title="Accepted"),
                self.finding(title="Resolved"),
            ],
            final_report=FINAL_REPORT,
            idempotency_key="disposition-pass",
        )
        with self.assertRaisesRegex(
            sprint_domain.SprintAuthorityError, "only FnB"
        ):
            self.close.disposition_followup(
                self.sprint_id,
                receipt.followup_ids[0],
                3,
                disposition="accepted",
            )
        self.assertEqual(
            "pending",
            self.con.execute(
                "SELECT disposition FROM sprint_followups WHERE followup_id=?",
                (receipt.followup_ids[0],),
            ).fetchone()[0],
        )

        self.assertTrue(
            self.close.disposition_followup(
                self.sprint_id,
                receipt.followup_ids[0],
                5,
                disposition="accepted",
            )
        )
        self.assertTrue(
            self.close.disposition_followup(
                self.sprint_id,
                receipt.followup_ids[1],
                5,
                disposition="resolved",
                resolution="Fixed by PR #900",
            )
        )
        self.assertFalse(
            self.close.disposition_followup(
                self.sprint_id,
                receipt.followup_ids[1],
                5,
                disposition="resolved",
                resolution="Fixed by PR #900",
            )
        )
        self.assertEqual(
            [
                ("accepted", None, None),
                ("resolved", "Fixed by PR #900", 1),
            ],
            [
                (
                    row["disposition"],
                    row["resolution"],
                    int(row["resolved_at"] is not None) if row["resolved_at"] else None,
                )
                for row in self.con.execute(
                    "SELECT disposition,resolution,resolved_at "
                    "FROM sprint_followups WHERE source_report_id=? "
                    "ORDER BY followup_id",
                    (receipt.report_id,),
                )
            ],
        )
        packet = self.close.compile_evidence_packet(self.sprint_id, 3)
        self.assertEqual(0, packet["unresolved_work"]["followups"]["total"])
        self.assertEqual([], packet["unresolved_work"]["followups"]["items"])


class EvidenceCompilerTest(SprintCloseCase):
    def setUp(self) -> None:
        super().setUp()
        participant = self.con.execute(
            "SELECT participant_id FROM sprint_participants "
            "WHERE sprint_id=? AND shell_id=1",
            (self.sprint_id,),
        ).fetchone()[0]
        self.con.execute(
            "INSERT INTO sprint_judgments "
            "(sprint_id,participant_id,work_unit_id,kind,body) "
            "VALUES (?,?,?,'deviation','Intentional seam choice')",
            (self.sprint_id, participant, self.unit_id),
        )
        self.con.execute(
            "INSERT INTO sprint_events "
            "(sprint_id,event_type,actor_kind,actor_shell_id,payload) "
            "VALUES (?,'spec.edited','planner',3,?)",
            (
                self.sprint_id,
                json.dumps(
                    {
                        "document_id": self.document_id,
                        "from_revision_sha256": "a" * 64,
                        "to_revision_sha256": "b" * 64,
                    }
                ),
            ),
        )
        self.con.executemany(
            "INSERT INTO sprint_events "
            "(sprint_id,event_type,actor_kind,payload) "
            "VALUES (?,'monitor.error','system',?)",
            (
                (self.sprint_id, json.dumps({"error": "first"})),
                (self.sprint_id, json.dumps({"error": "second"})),
            ),
        )
        registered_pr_id = int(
            self.con.execute(
                "INSERT INTO sprint_registered_prs "
                "(sprint_id,owner_participant_id,repository,pr_number) "
                "VALUES (?,?,'acme/repo',42)",
                (self.sprint_id, participant),
            ).lastrowid
        )
        self.con.execute(
            "INSERT INTO sprint_pr_work_units "
            "(sprint_id,registered_pr_id,work_unit_id) VALUES (?,?,?)",
            (self.sprint_id, registered_pr_id, self.unit_id),
        )
        self.con.execute(
            "INSERT INTO sprint_pr_transitions "
            "(registered_pr_id,normalized_state,transition_key,observed_head_sha) "
            "VALUES (?,'merged','merged-42',?)",
            (registered_pr_id, "c" * 40),
        )
        self.con.commit()
        self.record_conformance(
            self.sprint_id,
            2,
            body="Integrated review",
            findings=[self.finding(severity="Low")],
            final_report=FINAL_REPORT,
            idempotency_key="compiler-review",
        )

    def test_packet_is_bounded_but_carries_every_required_section(self):
        packet = self.close.compile_evidence_packet(
            self.sprint_id, 3, section_limit=1
        )

        self.assertEqual(
            {
                "packet_version",
                "scope",
                "spec_revisions",
                "planned_vs_actual",
                "pr_outcomes",
                "judgments_and_deviations",
                "pause_and_recovery",
                "wake_health",
                "anomalies",
                "conformance",
                "unresolved_work",
                "full_history_links",
            },
            set(packet),
        )
        spec = packet["spec_revisions"]["bound"][0]
        self.assertEqual(self.document_id, spec["document_id"])
        self.assertEqual(
            hashlib.sha256(b"governing spec revision 1").hexdigest(),
            spec["bound_revision_sha256"],
        )
        edit = packet["spec_revisions"]["mid_sprint_edits"]["items"][0]
        self.assertEqual("b" * 64, edit["payload"]["to_revision_sha256"])
        self.assertEqual(
            self.unit_id,
            packet["planned_vs_actual"]["items"][0]["work_unit_id"],
        )
        pr = packet["pr_outcomes"]["items"][0]
        self.assertEqual("merged", pr["normalized_state"])
        self.assertEqual("https://github.com/acme/repo/pull/42", pr["url"])
        self.assertEqual(
            "Intentional seam choice",
            packet["judgments_and_deviations"]["items"][0]["body"],
        )
        self.assertEqual(
            "Integrated review",
            packet["conformance"]["reports"]["items"][0]["body"],
        )
        self.assertEqual(
            "The delivered seam does not preserve the bound contract.",
            packet["conformance"]["followups"]["items"][0]["body"],
        )
        followup = packet["unresolved_work"]["followups"]["items"][0]
        self.assertEqual("Low", followup["severity"])
        self.assertEqual(
            f"/_sc/sprint/{self.sprint_id}/timeline",
            packet["full_history_links"]["timeline"],
        )
        self.assertEqual(
            [], packet["full_history_links"]["participant_conversations"]
        )
        self.assertGreater(packet["anomalies"]["events"]["truncated"], 0)

    def test_packet_includes_bound_spec_without_review_evidence(self):
        self.con.execute(
            "UPDATE sprint_specs SET approval_id=NULL WHERE sprint_id=?",
            (self.sprint_id,),
        )
        self.con.commit()

        packet = self.close.compile_evidence_packet(self.sprint_id, 3)

        self.assertEqual(1, len(packet["spec_revisions"]["bound"]))
        spec = packet["spec_revisions"]["bound"][0]
        self.assertEqual(self.document_id, spec["document_id"])
        self.assertEqual(
            hashlib.sha256(b"governing spec revision 1").hexdigest(),
            spec["bound_revision_sha256"],
        )
        for field in (
            "approval_id",
            "reviewer_shell_id",
            "verdict",
            "reviewed_revision_sha256",
            "findings_document_id",
            "reviewed_at",
        ):
            self.assertIsNone(spec[field])

    def test_planner_admin_and_participating_reviewer_compile_evidence(self):
        planner_packet = self.close.compile_evidence_packet(self.sprint_id, 3)
        reviewer_packet = self.close.compile_evidence_packet(self.sprint_id, 2)
        self.assertEqual(
            (self.sprint_id, self.sprint_id),
            (
                planner_packet["scope"]["sprint_id"],
                reviewer_packet["scope"]["sprint_id"],
            ),
        )
        self.con.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (5,'FnB','FNB','admin','prompt',1)"
        )
        self.con.commit()
        self.assertEqual(
            self.sprint_id,
            self.close.compile_evidence_packet(self.sprint_id, 5)["scope"][
                "sprint_id"
            ],
        )
        with self.assertRaisesRegex(
            sprint_domain.SprintAuthorityError, "participating Reviewer"
        ):
            self.close.compile_evidence_packet(self.sprint_id, 1)
        with self.assertRaisesRegex(
            sprint_domain.SprintAuthorityError, "participating Reviewer"
        ):
            self.close.compile_evidence_packet(self.sprint_id, 4)
        timeline = self.close.timeline(self.sprint_id, 2)
        self.assertEqual(self.sprint_id, timeline["sprint_id"])
        self.assertEqual(
            sorted(event["event_id"] for event in timeline["events"]),
            [event["event_id"] for event in timeline["events"]],
        )
        with self.assertRaisesRegex(
            sprint_domain.SprintAuthorityError, "participants"
        ):
            self.close.timeline(self.sprint_id, 4)
