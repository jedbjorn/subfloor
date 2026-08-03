"""Stage 9 gates for conformance follow-ups and report compilation."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
MIGRATIONS = ENGINE / "migrations"
MIGRATION = MIGRATIONS / "0150_sprint_close_reports.sql"
SURFACE_MIGRATION = MIGRATIONS / "0152_sprint_surface_completion.sql"

sys.path[:0] = [str(ENGINE / "scripts"), str(ROOT / "tests")]
import sprint_close  # noqa: E402
import sprint_domain  # noqa: E402
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
            self.close.record_conformance(
                self.sprint_id,
                2,
                body="x" * 8001,
                findings=[],
                idempotency_key="oversize-conformance",
            )
        with self.assertRaisesRegex(
            ValueError,
            "finding body is 8001 characters; maximum is 8000",
        ):
            self.close.record_conformance(
                self.sprint_id,
                2,
                body="bounded",
                findings=[self.finding(body="x" * 8001)],
                idempotency_key="oversize-finding",
            )
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

        conformance = self.close.record_conformance(
            self.sprint_id,
            2,
            body="x" * 8000,
            findings=[self.finding(body="x" * 8000)],
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

        with self.assertRaisesRegex(
            ValueError,
            "final report body is 8001 characters; maximum is 8000",
        ):
            self.close.record_final_report(
                self.sprint_id,
                3,
                body="x" * 8001,
                idempotency_key="oversize-final",
            )
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_reports WHERE sprint_id=? "
                "AND report_kind='final'",
                (self.sprint_id,),
            ).fetchone()[0],
        )
        final = self.close.record_final_report(
            self.sprint_id,
            3,
            body="x" * 8000,
            idempotency_key="bounded-final",
        )
        self.assertTrue(final.created)

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

        receipt = self.close.record_conformance(
            self.sprint_id,
            2,
            body="Conformance found one integrated departure.",
            findings=[self.finding()],
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

    def test_retry_replays_exactly_and_conflicting_input_is_rejected(self):
        first = self.close.record_conformance(
            self.sprint_id,
            2,
            body="Review body",
            findings=[self.finding(severity="Low")],
            idempotency_key="same-pass",
        )
        replay = self.close.record_conformance(
            self.sprint_id,
            2,
            body="Review body",
            findings=[self.finding(severity="Low")],
            idempotency_key="same-pass",
        )
        self.assertFalse(replay.created)
        self.assertEqual(first.report_id, replay.report_id)
        self.assertEqual(first.followup_ids, replay.followup_ids)

        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError, "different findings"
        ):
            self.close.record_conformance(
                self.sprint_id,
                2,
                body="Review body",
                findings=[self.finding(severity="Critical")],
                idempotency_key="same-pass",
            )
        self.assertEqual(
            1,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_followups WHERE sprint_id=?",
                (self.sprint_id,),
            ).fetchone()[0],
        )

    def test_non_object_finding_is_rejected_before_any_report_write(self):
        with self.assertRaisesRegex(TypeError, "must be an object"):
            self.close.record_conformance(
                self.sprint_id,
                2,
                body="Malformed findings",
                findings=["not an object"],
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
            self.close.record_conformance(
                self.sprint_id,
                1,
                body="Not a review",
                findings=[],
                idempotency_key="wrong-role",
            )
        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError, "not bound"
        ):
            self.close.record_conformance(
                self.sprint_id,
                2,
                body="Bad link",
                findings=[self.finding(spec_document_id=999)],
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

    def test_only_fnb_dispositions_followup_and_only_pending_is_unresolved(self):
        self.con.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (5,'FnB','FNB','admin','prompt',1)"
        )
        self.con.commit()
        receipt = self.close.record_conformance(
            self.sprint_id,
            2,
            body="Two follow-ups",
            findings=[
                self.finding(title="Accepted"),
                self.finding(title="Resolved"),
            ],
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
        self.close.record_conformance(
            self.sprint_id,
            2,
            body="Integrated review",
            findings=[self.finding(severity="Low")],
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

    def test_only_planner_or_admin_compiles_and_participants_read_timeline(self):
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
