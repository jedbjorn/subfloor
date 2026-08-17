#!/usr/bin/env python3
"""Stage 1 gates for the Sprints v2 domain and lifecycle switch."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"
FOUNDATION = MIGRATIONS / "0146_sprint_v2_domain.sql"
GENERATION_MIGRATION = MIGRATIONS / "0155_sprint_conversation_generations.sql"
OPTIONAL_QAQC_MIGRATION = MIGRATIONS / "0185_optional_sprint_qaqc.sql"
LIVE_REPLAN_MIGRATION = MIGRATIONS / "0199_sprint_live_replanning.sql"
CONFORMANCE_OWNER_MIGRATION = (
    MIGRATIONS / "0205_sprint_conformance_ownership.sql"
)

sys.path.insert(0, str(ENGINE / "scripts"))
import db_driver  # noqa: E402
import migrate  # noqa: E402
import sprint_domain  # noqa: E402
import sprint_message_delivery  # noqa: E402
from sprint_route_binding_support import candidate as route_candidate  # noqa: E402


def apply_schema(con: sqlite3.Connection, *, through: str | None = None) -> None:
    con.executescript(SCHEMA.read_text())
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        if through is not None and migration.name > through:
            break
        con.executescript(migration.read_text())
    con.execute("PRAGMA foreign_keys=ON")


class SprintDomainCase(unittest.TestCase):
    def setUp(self) -> None:
        route_patch = mock.patch.object(
            sprint_domain, "_participant_binding_candidate", side_effect=route_candidate
        )
        route_patch.start()
        self.addCleanup(route_patch.stop)
        evidence_patch = mock.patch.object(
            sprint_domain.route_bindings,
            "verify_stored_v2_before_first_turn",
        )
        evidence_patch.start()
        self.addCleanup(evidence_patch.stop)
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
                (4, "Other planner", "PLN2", "planner", "prompt"),
            ),
        )
        self.con.commit()
        self.store = sprint_domain.SprintLifecycleStore(
            self.con, probe_harness=lambda _harness: None
        )
        self.serial = 0

    def create_sprint(
        self,
        *,
        merge_grant: bool = True,
        approval_verdict: str = "pass",
    ) -> tuple[int, int]:
        self.serial += 1
        serial = self.serial
        feature_id = self.con.execute(
            "INSERT INTO roadmap (title,roadmap_status) VALUES (?,'in_progress')",
            (f"Feature {serial}",),
        ).lastrowid
        body = f"governing spec revision {serial}"
        document_id = self.con.execute(
            "INSERT INTO documents (feature_id,kind,seq,title,body) "
            "VALUES (?,'spec',1,?,?)",
            (feature_id, f"Spec {serial}", body),
        ).lastrowid
        revision = hashlib.sha256(body.encode()).hexdigest()
        approval_id = self.con.execute(
            "INSERT INTO sprint_spec_approvals "
            "(document_id,revision_sha256,reviewer_shell_id,verdict) "
            "VALUES (?,?,2,?)",
            (document_id, revision, approval_verdict),
        ).lastrowid
        sprint_id = self.con.execute(
            "INSERT INTO sprints "
            "(feature_id,originating_planner_shell_id,merge_grant_enabled) "
            "VALUES (?,3,?)",
            (feature_id, 1 if merge_grant else 0),
        ).lastrowid
        self.con.execute(
            "INSERT INTO sprint_specs "
            "(sprint_id,document_id,bound_revision_sha256,approval_id,"
            "bound_revision_body) VALUES (?,?,?,?,?)",
            (sprint_id, document_id, revision, approval_id, body),
        )
        self.con.executemany(
            "INSERT INTO sprint_participants "
            "(sprint_id,shell_id,role,harness,model,effort) "
            "VALUES (?,?,?,?,?,?)",
            (
                (sprint_id, 3, "planner", "codex", "planner-model", "high"),
                (sprint_id, 1, "developer", "codex", "dev-model", "high"),
                (sprint_id, 2, "reviewer", "kimi", "review-model", "high"),
            ),
        )
        work_unit_id = self.con.execute(
            "INSERT INTO sprint_work_units "
            "(sprint_id,assigned_shell_id,reviewer_shell_id,title,expected_output) "
            "VALUES (?,1,2,'Foundation','Ship the durable foundation')",
            (sprint_id,),
        ).lastrowid
        self.con.commit()
        return int(sprint_id), int(work_unit_id)


class MigrationAndShapeTest(SprintDomainCase):
    @staticmethod
    def _pre_owner_terminal_db(*, reviewer_shell_ids: tuple[int, ...]):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        apply_schema(con, through="0204_sprint_governing_revision_evidence.sql")
        con.execute("INSERT INTO users (user_id,username) VALUES (1,'operator')")
        con.executemany(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (?,?,?,?,?,1)",
            (
                (1, "Developer", "DEV1", "dev", "prompt"),
                (2, "Reviewer one", "REV1", "reviewer", "prompt"),
                (3, "Planner", "PLN1", "planner", "prompt"),
                (5, "Reviewer two", "REV2", "reviewer", "prompt"),
            ),
        )
        feature_id = int(
            con.execute("INSERT INTO roadmap (title) VALUES ('Feature')").lastrowid
        )
        sprint_id = int(
            con.execute(
                "INSERT INTO sprints "
                "(feature_id,originating_planner_shell_id,merge_grant_enabled) "
                "VALUES (?,3,1)",
                (feature_id,),
            ).lastrowid
        )
        participants = [(sprint_id, 3, "planner"), (sprint_id, 1, "developer")]
        participants.extend(
            (sprint_id, reviewer_shell_id, "reviewer")
            for reviewer_shell_id in reviewer_shell_ids
        )
        con.executemany(
            "INSERT INTO sprint_participants "
            "(sprint_id,shell_id,role,harness) VALUES (?,?,?,'codex')",
            participants,
        )
        reviewer_shell_id = reviewer_shell_ids[0]
        con.execute(
            "INSERT INTO sprint_work_units "
            "(sprint_id,assigned_shell_id,reviewer_shell_id,title,expected_output,"
            "disposition,completed_at) "
            "VALUES (?,1,?,'Historical lane','Historical output','completed',"
            "'2026-08-01 00:00:00')",
            (sprint_id, reviewer_shell_id),
        )
        participant_id = int(
            con.execute(
                "SELECT participant_id FROM sprint_participants "
                "WHERE sprint_id=? AND shell_id=?",
                (sprint_id, reviewer_shell_id),
            ).fetchone()[0]
        )
        con.execute(
            "INSERT INTO wake_message "
            "(sprint_id,receiver_shell_id,to_participant_id,message_kind,body,"
            "declared_type,actionable,read_at,delivered_at,idempotency_key) "
            "VALUES (?,?,?,'notification','Old broadcast closeout','new',0,"
            "'2026-08-01 00:00:00','2026-08-01 00:00:00',?)",
            (
                sprint_id,
                reviewer_shell_id,
                participant_id,
                f"historical:{sprint_id}:delivery-terminal",
            ),
        )
        con.execute(
            "INSERT INTO sprint_events "
            "(sprint_id,event_type,actor_kind,payload) "
            "VALUES (?,'sprint.delivery_terminal','system',?)",
            (
                sprint_id,
                json.dumps(
                    {
                        "terminal_count": 1,
                        "completed_count": 1,
                        "cancelled_count": 0,
                    }
                ),
            ),
        )
        con.execute(
            "UPDATE sprints SET lifecycle='armed' WHERE sprint_id=?",
            (sprint_id,),
        )
        con.commit()
        return con, sprint_id

    @staticmethod
    def _seed_prechange_binding(con: sqlite3.Connection) -> tuple[int, int, str, int]:
        con.execute("INSERT INTO users (user_id,username) VALUES (1,'operator')")
        con.executemany(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (?,?,?,?,?,1)",
            (
                (2, "Reviewer", "REV1", "reviewer", "prompt"),
                (3, "Planner", "PLN1", "planner", "prompt"),
            ),
        )
        feature_id = int(
            con.execute("INSERT INTO roadmap (title) VALUES ('Feature')").lastrowid
        )
        body = "reviewed governing spec"
        document_id = int(
            con.execute(
                "INSERT INTO documents (feature_id,kind,seq,title,body) "
                "VALUES (?,'spec',1,'Reviewed',?)",
                (feature_id, body),
            ).lastrowid
        )
        revision = hashlib.sha256(body.encode()).hexdigest()
        approval_id = int(
            con.execute(
                "INSERT INTO sprint_spec_approvals "
                "(document_id,revision_sha256,reviewer_shell_id,verdict) "
                "VALUES (?,?,2,'fail')",
                (document_id, revision),
            ).lastrowid
        )
        sprint_id = int(
            con.execute(
                "INSERT INTO sprints "
                "(feature_id,originating_planner_shell_id) VALUES (?,3)",
                (feature_id,),
            ).lastrowid
        )
        con.execute(
            "INSERT INTO sprint_specs "
            "(sprint_id,document_id,bound_revision_sha256,approval_id,included_at) "
            "VALUES (?,?,?,?,?)",
            (sprint_id, document_id, revision, approval_id, "2026-08-05 12:34:56"),
        )
        con.commit()
        return sprint_id, document_id, revision, approval_id

    def test_optional_qaqc_migration_preserves_reviewed_binding_and_allows_null(self) -> None:
        with closing(sqlite3.connect(":memory:")) as con:
            con.row_factory = sqlite3.Row
            apply_schema(con, through="0184_reseed_sprint_skill_polish.sql")
            reviewed = self._seed_prechange_binding(con)

            con.executescript(OPTIONAL_QAQC_MIGRATION.read_text())

            self.assertEqual(
                (*reviewed, "2026-08-05 12:34:56"),
                tuple(con.execute("SELECT * FROM sprint_specs").fetchone()),
            )
            feature_id = int(
                con.execute("SELECT feature_id FROM sprints").fetchone()[0]
            )
            direct_body = "direct governing spec"
            direct_document_id = int(
                con.execute(
                    "INSERT INTO documents (feature_id,kind,seq,title,body) "
                    "VALUES (?,'spec',2,'Direct',?)",
                    (feature_id, direct_body),
                ).lastrowid
            )
            direct_sprint_id = int(
                con.execute(
                    "INSERT INTO sprints "
                    "(feature_id,originating_planner_shell_id) VALUES (?,3)",
                    (feature_id,),
                ).lastrowid
            )
            direct_revision = hashlib.sha256(direct_body.encode()).hexdigest()
            con.execute(
                "INSERT INTO sprint_specs "
                "(sprint_id,document_id,bound_revision_sha256,approval_id) "
                "VALUES (?,?,?,NULL)",
                (direct_sprint_id, direct_document_id, direct_revision),
            )
            self.assertEqual(
                (direct_document_id, direct_revision, None),
                tuple(
                    con.execute(
                        "SELECT document_id,bound_revision_sha256,approval_id "
                        "FROM sprint_specs WHERE sprint_id=?",
                        (direct_sprint_id,),
                    ).fetchone()
                ),
            )
            self.assertEqual(2, con.execute("SELECT COUNT(*) FROM sprint_specs").fetchone()[0])
            self.assertEqual([], con.execute("PRAGMA foreign_key_check").fetchall())

    def test_conformance_owner_migration_backfills_only_unambiguous_history(self) -> None:
        with closing(sqlite3.connect(":memory:")) as con:
            con.row_factory = sqlite3.Row
            apply_schema(con, through="0204_sprint_governing_revision_evidence.sql")
            con.execute("INSERT INTO users (user_id,username) VALUES (1,'operator')")
            con.executemany(
                "INSERT INTO shells "
                "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
                "VALUES (?,?,?,?,?,1)",
                (
                    (2, "Reviewer one", "REV1", "reviewer", "prompt"),
                    (3, "Planner", "PLN1", "planner", "prompt"),
                    (5, "Reviewer two", "REV2", "reviewer", "prompt"),
                ),
            )
            feature_id = int(
                con.execute("INSERT INTO roadmap (title) VALUES ('Feature')").lastrowid
            )
            unambiguous = int(
                con.execute(
                    "INSERT INTO sprints "
                    "(feature_id,originating_planner_shell_id) VALUES (?,3)",
                    (feature_id,),
                ).lastrowid
            )
            ambiguous = int(
                con.execute(
                    "INSERT INTO sprints "
                    "(feature_id,originating_planner_shell_id) VALUES (?,3)",
                    (feature_id,),
                ).lastrowid
            )
            con.executemany(
                "INSERT INTO sprint_participants "
                "(sprint_id,shell_id,role,harness) VALUES (?,?,?,'codex')",
                (
                    (unambiguous, 3, "planner"),
                    (unambiguous, 2, "reviewer"),
                    (ambiguous, 3, "planner"),
                    (ambiguous, 2, "reviewer"),
                    (ambiguous, 5, "reviewer"),
                ),
            )
            con.commit()

            con.executescript(CONFORMANCE_OWNER_MIGRATION.read_text())

            self.assertEqual(
                [(unambiguous, 2, 1), (ambiguous, None, 0)],
                [
                    tuple(row)
                    for row in con.execute(
                        "SELECT sprint_id,conformance_reviewer_shell_id,"
                        "conformance_owner_generation FROM sprints "
                        "ORDER BY sprint_id"
                    )
                ],
            )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "active Reviewer participant",
            ):
                con.execute(
                    "UPDATE sprints SET conformance_reviewer_shell_id=3,"
                    "conformance_owner_generation=2 WHERE sprint_id=?",
                    (unambiguous,),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "generation is invalid"):
                con.execute(
                    "UPDATE sprints SET conformance_owner_generation=2 "
                    "WHERE sprint_id=?",
                    (unambiguous,),
                )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "arming requires a Sprint conformance owner",
            ):
                con.execute(
                    "UPDATE sprints SET lifecycle='armed' WHERE sprint_id=?",
                    (ambiguous,),
                )
            con.execute(
                "UPDATE sprints SET conformance_reviewer_shell_id=5,"
                "conformance_owner_generation=1,merge_grant_enabled=1 "
                "WHERE sprint_id=?",
                (ambiguous,),
            )
            con.execute(
                "UPDATE sprints SET lifecycle='armed' WHERE sprint_id=?",
                (ambiguous,),
            )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "reassigned only while paused",
            ):
                con.execute(
                    "UPDATE sprints SET conformance_reviewer_shell_id=2,"
                    "conformance_owner_generation=2 WHERE sprint_id=?",
                    (ambiguous,),
                )
            con.execute(
                "UPDATE sprints SET lifecycle='paused' WHERE sprint_id=?",
                (ambiguous,),
            )
            con.execute(
                "UPDATE sprints SET conformance_reviewer_shell_id=2,"
                "conformance_owner_generation=2 WHERE sprint_id=?",
                (ambiguous,),
            )
            self.assertEqual(
                (2, 2, "paused"),
                tuple(
                    con.execute(
                        "SELECT conformance_reviewer_shell_id,"
                        "conformance_owner_generation,lifecycle FROM sprints "
                        "WHERE sprint_id=?",
                        (ambiguous,),
                    ).fetchone()
                ),
            )

    def test_conformance_owner_upgrade_reconciles_terminal_history_once(self) -> None:
        for reviewers, expected_owner in (((2,), 2), ((2, 5), None)):
            with self.subTest(reviewers=reviewers), closing(
                self._pre_owner_terminal_db(reviewer_shell_ids=reviewers)[0]
            ) as con:
                sprint_id = int(con.execute("SELECT sprint_id FROM sprints").fetchone()[0])
                con.executescript(CONFORMANCE_OWNER_MIGRATION.read_text())
                notifications: list[bool] = []
                store = sprint_domain.SprintLifecycleStore(
                    con,
                    probe_harness=lambda _harness: None,
                    notify_commit=lambda: notifications.append(con.in_transaction)
                    or True,
                )

                self.assertEqual(((sprint_id, "armed"),), store.recover_on_startup())
                before_retry = (
                    con.total_changes,
                    con.execute(
                        "SELECT COUNT(*) FROM wake_message"
                    ).fetchone()[0],
                    con.execute(
                        "SELECT COUNT(*) FROM sprint_events"
                    ).fetchone()[0],
                    con.execute(
                        "SELECT COUNT(*) FROM sprint_reports"
                    ).fetchone()[0],
                )
                store.recover_on_startup()
                after_retry = (
                    con.total_changes,
                    con.execute(
                        "SELECT COUNT(*) FROM wake_message"
                    ).fetchone()[0],
                    con.execute(
                        "SELECT COUNT(*) FROM sprint_events"
                    ).fetchone()[0],
                    con.execute(
                        "SELECT COUNT(*) FROM sprint_reports"
                    ).fetchone()[0],
                )

                self.assertEqual(
                    1,
                    con.execute(
                        "SELECT COUNT(*) FROM wake_message "
                        "WHERE body='Old broadcast closeout' AND read_at IS NOT NULL "
                        "AND delivered_at IS NOT NULL"
                    ).fetchone()[0],
                )
                if expected_owner is not None:
                    self.assertEqual(
                        ("armed", expected_owner, 1),
                        tuple(
                            con.execute(
                                "SELECT lifecycle,conformance_reviewer_shell_id,"
                                "conformance_owner_generation FROM sprints "
                                "WHERE sprint_id=?",
                                (sprint_id,),
                            ).fetchone()
                        ),
                    )
                    self.assertEqual(
                        1,
                        con.execute(
                            "SELECT COUNT(*) FROM wake_message "
                            "WHERE sprint_id=? AND idempotency_key=?",
                            (
                                sprint_id,
                                f"sprint:{sprint_id}:delivery-terminal:1:"
                                "owner:2:generation:1",
                            ),
                        ).fetchone()[0],
                    )
                    self.assertEqual(
                        1,
                        con.execute(
                            "SELECT COUNT(*) FROM sprint_events WHERE sprint_id=? "
                            "AND event_type='sprint.delivery_terminal' "
                            "AND json_extract(payload,'$.conformance_reviewer_shell_id')=2 "
                            "AND json_extract(payload,'$.conformance_owner_generation')=1",
                            (sprint_id,),
                        ).fetchone()[0],
                    )
                    self.assertEqual([], notifications)
                else:
                    self.assertEqual(
                        ("paused", None, 0),
                        tuple(
                            con.execute(
                                "SELECT lifecycle,conformance_reviewer_shell_id,"
                                "conformance_owner_generation FROM sprints "
                                "WHERE sprint_id=?",
                                (sprint_id,),
                            ).fetchone()
                        ),
                    )
                    self.assertEqual(
                        (1, 1, 1),
                        tuple(
                            con.execute(
                                "SELECT "
                                "(SELECT COUNT(*) FROM sprint_reports "
                                " WHERE sprint_id=? AND report_kind='pause'),"
                                "(SELECT COUNT(*) FROM sprint_events "
                                " WHERE sprint_id=? "
                                " AND event_type='conformance_owner.required'),"
                                "(SELECT COUNT(*) FROM wake_message "
                                " WHERE sprint_id IS NULL AND receiver_shell_id=3 "
                                " AND body LIKE ?)",
                                (
                                    sprint_id,
                                    sprint_id,
                                    f"Sprint {sprint_id} reached delivery terminal%",
                                ),
                            ).fetchone()
                        ),
                    )
                    self.assertEqual([False], notifications)
                self.assertEqual(before_retry[1:], after_retry[1:])

    def test_live_replanning_migration_preserves_and_repeats_task_binding(self) -> None:
        with closing(sqlite3.connect(":memory:")) as con:
            con.row_factory = sqlite3.Row
            apply_schema(con, through="0198_reseed_engine_authored_review_handoff.sql")
            sprint_id, document_id, _, _ = self._seed_prechange_binding(con)
            con.execute(
                "INSERT INTO shells "
                "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
                "VALUES (1,'Developer','DEV1','dev','prompt',1)"
            )
            feature_id = int(
                con.execute(
                    "SELECT feature_id FROM sprints WHERE sprint_id=?", (sprint_id,)
                ).fetchone()[0]
            )
            task_id = int(
                con.execute(
                    "INSERT INTO spec_tasks (feature_id,document_id,seq,title) "
                    "VALUES (?,?,1,'Repeat governing task')",
                    (feature_id, document_id),
                ).lastrowid
            )
            unit_ids = [
                int(
                    con.execute(
                        "INSERT INTO sprint_work_units "
                        "(sprint_id,assigned_shell_id,reviewer_shell_id,title,"
                        "expected_output) VALUES (?,1,2,?,?)",
                        (sprint_id, f"Lane {number}", f"Output {number}"),
                    ).lastrowid
                )
                for number in (1, 2)
            ]
            con.execute(
                "INSERT INTO sprint_work_unit_tasks "
                "(sprint_id,work_unit_id,task_id) VALUES (?,?,?)",
                (sprint_id, unit_ids[0], task_id),
            )

            con.executescript(LIVE_REPLAN_MIGRATION.read_text())
            con.execute(
                "INSERT INTO sprint_work_unit_tasks "
                "(sprint_id,work_unit_id,task_id) VALUES (?,?,?)",
                (sprint_id, unit_ids[1], task_id),
            )

            self.assertEqual(
                [(unit_ids[0], task_id), (unit_ids[1], task_id)],
                [
                    tuple(row)
                    for row in con.execute(
                        "SELECT work_unit_id,task_id FROM sprint_work_unit_tasks "
                        "ORDER BY work_unit_id"
                    )
                ],
            )
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "INSERT INTO sprint_work_unit_tasks "
                    "(sprint_id,work_unit_id,task_id) VALUES (?,?,?)",
                    (sprint_id, unit_ids[1], task_id),
                )
            self.assertEqual([], con.execute("PRAGMA foreign_key_check").fetchall())

    def test_optional_qaqc_migration_failure_rolls_back_original_table(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / OPTIONAL_QAQC_MIGRATION.name
            path.write_text(
                OPTIONAL_QAQC_MIGRATION.read_text()
                + "\nSELECT missing_migration_function();\n"
            )
            with closing(sqlite3.connect(":memory:")) as con:
                con.row_factory = sqlite3.Row
                apply_schema(con, through="0184_reseed_sprint_skill_polish.sql")
                original = self._seed_prechange_binding(con)

                with self.assertRaisesRegex(
                    sqlite3.OperationalError, "missing_migration_function"
                ):
                    migrate.apply(con, path)

                self.assertEqual(
                    original,
                    tuple(
                        con.execute(
                            "SELECT sprint_id,document_id,bound_revision_sha256,"
                            "approval_id FROM sprint_specs"
                        ).fetchone()
                    ),
                )
                approval_column = next(
                    row
                    for row in con.execute("PRAGMA table_info(sprint_specs)")
                    if row[1] == "approval_id"
                )
                self.assertEqual(1, approval_column[3])
                self.assertEqual([], con.execute("PRAGMA foreign_key_check").fetchall())

    def test_optional_qaqc_normal_migration_discovery_does_not_reapply(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            migration_dir = Path(temp_dir)
            path = migration_dir / OPTIONAL_QAQC_MIGRATION.name
            path.write_text(OPTIONAL_QAQC_MIGRATION.read_text())
            with closing(sqlite3.connect(":memory:")) as con:
                con.row_factory = sqlite3.Row
                apply_schema(con, through="0184_reseed_sprint_skill_polish.sql")
                original = self._seed_prechange_binding(con)

                migrate.apply(con, path)
                with mock.patch.object(migrate, "MIGRATIONS_DIR", migration_dir):
                    self.assertEqual([], migrate.pending(con))
                    for pending_path in migrate.pending(con):
                        migrate.apply(con, pending_path)

                self.assertEqual(
                    original,
                    tuple(
                        con.execute(
                            "SELECT sprint_id,document_id,bound_revision_sha256,"
                            "approval_id FROM sprint_specs"
                        ).fetchone()
                    ),
                )
                self.assertEqual(
                    [(OPTIONAL_QAQC_MIGRATION.name,)],
                    [
                        tuple(row)
                        for row in con.execute(
                            "SELECT filename FROM schema_migrations WHERE filename=?",
                            (OPTIONAL_QAQC_MIGRATION.name,),
                        )
                    ],
                )

    def test_conversation_generation_backfills_prepared_plan_without_drift(self) -> None:
        with closing(sqlite3.connect(":memory:")) as con:
            con.row_factory = sqlite3.Row
            apply_schema(con, through="0154_remove_tombstoned_skills.sql")
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
            feature_id = con.execute(
                "INSERT INTO roadmap (title,roadmap_status) "
                "VALUES ('Prepared feature','in_progress')"
            ).lastrowid
            body = "exact prepared governing revision"
            document_id = con.execute(
                "INSERT INTO documents (feature_id,kind,seq,title,body) "
                "VALUES (?,'spec',1,'Prepared spec',?)",
                (feature_id, body),
            ).lastrowid
            revision = hashlib.sha256(body.encode()).hexdigest()
            approval_id = con.execute(
                "INSERT INTO sprint_spec_approvals "
                "(document_id,revision_sha256,reviewer_shell_id,verdict) "
                "VALUES (?,?,2,'pass')",
                (document_id, revision),
            ).lastrowid
            sprint_id = con.execute(
                "INSERT INTO sprints "
                "(feature_id,originating_planner_shell_id,merge_grant_enabled) "
                "VALUES (?,3,1)",
                (feature_id,),
            ).lastrowid
            con.execute(
                "INSERT INTO sprint_specs "
                "(sprint_id,document_id,bound_revision_sha256,approval_id) "
                "VALUES (?,?,?,?)",
                (sprint_id, document_id, revision, approval_id),
            )
            con.executemany(
                "INSERT INTO sprint_participants "
                "(sprint_id,shell_id,role,harness,model,effort) "
                "VALUES (?,?,?,?,?,?)",
                (
                    (sprint_id, 3, "planner", "codex", "planner-model", "high"),
                    (sprint_id, 1, "developer", "kimi", "dev-model", "high"),
                    (sprint_id, 2, "reviewer", "claude", "review-model", "high"),
                ),
            )
            con.execute(
                "INSERT INTO sprint_work_units "
                "(sprint_id,assigned_shell_id,reviewer_shell_id,title,expected_output) "
                "VALUES (?,1,2,'Tiny unit','One small verified change')",
                (sprint_id,),
            )
            before = {
                "sprint": tuple(
                    con.execute(
                        "SELECT sprint_id,feature_id,originating_planner_shell_id,"
                        "lifecycle,merge_grant_enabled FROM sprints"
                    ).fetchone()
                ),
                "spec": tuple(con.execute("SELECT * FROM sprint_specs").fetchone()),
                "participants": [
                    tuple(row)
                    for row in con.execute(
                        "SELECT participant_id,sprint_id,shell_id,role,harness,"
                        "model,effort,persistent_conversation_id,current_conversation_id "
                        "FROM sprint_participants ORDER BY participant_id"
                    )
                ],
                "unit": tuple(
                    con.execute(
                        "SELECT work_unit_id,sprint_id,assigned_shell_id,"
                        "reviewer_shell_id,title,expected_output,disposition "
                        "FROM sprint_work_units"
                    ).fetchone()
                ),
            }

            con.executescript(GENERATION_MIGRATION.read_text())

            generation = con.execute(
                "SELECT conversation_generation FROM sprints WHERE sprint_id=?",
                (sprint_id,),
            ).fetchone()[0]
            self.assertRegex(generation, r"^[0-9a-f]{32}$")
            after = {
                "sprint": tuple(
                    con.execute(
                        "SELECT sprint_id,feature_id,originating_planner_shell_id,"
                        "lifecycle,merge_grant_enabled FROM sprints"
                    ).fetchone()
                ),
                "spec": tuple(con.execute("SELECT * FROM sprint_specs").fetchone()),
                "participants": [
                    tuple(row)
                    for row in con.execute(
                        "SELECT participant_id,sprint_id,shell_id,role,harness,"
                        "model,effort,persistent_conversation_id,current_conversation_id "
                        "FROM sprint_participants ORDER BY participant_id"
                    )
                ],
                "unit": tuple(
                    con.execute(
                        "SELECT work_unit_id,sprint_id,assigned_shell_id,"
                        "reviewer_shell_id,title,expected_output,disposition "
                        "FROM sprint_work_units"
                    ).fetchone()
                ),
            }
            self.assertEqual(before, after)
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "conversation generation is immutable"
            ):
                con.execute(
                    "UPDATE sprints SET conversation_generation=? WHERE sprint_id=?",
                    ("f" * 32, sprint_id),
                )

    def test_upgrade_from_zero_sprint_baseline_creates_v2_domain(self) -> None:
        with closing(sqlite3.connect(":memory:")) as con:
            apply_schema(con, through="0145_reseed_generic_guidance.sql")
            self.assertEqual(
                [],
                con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name LIKE 'sprint%'"
                ).fetchall(),
            )
            con.executescript(FOUNDATION.read_text())
            tables = {
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertTrue(
                {
                    "sprints",
                    "sprint_specs",
                    "sprint_participants",
                    "sprint_work_units",
                    "sprint_messages",
                    "sprint_wake_outbox",
                    "sprint_wake_attempts",
                    "sprint_registered_prs",
                    "sprint_pr_transitions",
                    "sprint_judgments",
                    "sprint_reports",
                    "sprint_events",
                }.issubset(tables)
            )
            self.assertEqual([], con.execute("PRAGMA foreign_key_check").fetchall())

    def test_append_only_evidence_rejects_mutation_and_preserves_row(self) -> None:
        sprint_id, _ = self.create_sprint()
        self.store.arm(sprint_id, 3)
        event = self.con.execute(
            "SELECT event_id,event_type FROM sprint_events WHERE sprint_id=? "
            "AND event_type='lifecycle.armed'",
            (sprint_id,),
        ).fetchone()
        self.assertEqual("lifecycle.armed", event["event_type"])
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.con.execute(
                "UPDATE sprint_events SET event_type='rewritten' WHERE event_id=?",
                (event["event_id"],),
            )
        self.assertEqual(
            "lifecycle.armed",
            self.con.execute(
                "SELECT event_type FROM sprint_events WHERE event_id=?",
                (event["event_id"],),
            ).fetchone()[0],
        )


class SpecApprovalTest(SprintDomainCase):
    def test_review_shell_records_exact_revision_and_retry_is_idempotent(self) -> None:
        feature_id = int(
            self.con.execute(
                "INSERT INTO roadmap (title) VALUES ('QAQC feature')"
            ).lastrowid
        )
        document_id = int(
            self.con.execute(
                "INSERT INTO documents (feature_id,kind,seq,title,body) "
                "VALUES (?,'spec',1,'QAQC spec','exact body')",
                (feature_id,),
            ).lastrowid
        )
        findings_id = int(
            self.con.execute(
                "INSERT INTO documents (feature_id,kind,seq,title,body) "
                "VALUES (?,'doc',1,'Findings','none')",
                (feature_id,),
            ).lastrowid
        )
        self.con.commit()
        approvals = sprint_domain.SprintSpecApprovalStore(self.con)

        first = approvals.record(
            document_id,
            2,
            verdict="pass",
            findings_document_id=findings_id,
        )
        replay = approvals.record(
            document_id,
            2,
            verdict="pass",
            findings_document_id=findings_id,
        )

        self.assertTrue(first.created)
        self.assertFalse(replay.created)
        self.assertEqual(first.approval_id, replay.approval_id)
        self.assertEqual(hashlib.sha256(b"exact body").hexdigest(), first.revision_sha256)
        self.assertEqual(
            (
                document_id,
                2,
                "pass",
                findings_id,
            ),
            tuple(
                self.con.execute(
                    "SELECT document_id,reviewer_shell_id,verdict,"
                    "findings_document_id FROM sprint_spec_approvals "
                    "WHERE approval_id=?",
                    (first.approval_id,),
                ).fetchone()
            ),
        )
        listed = approvals.for_document(document_id)
        self.assertEqual(1, len(listed))
        self.assertEqual("REV1", listed[0]["reviewer_shortname"])

    def test_non_reviewer_cannot_record_but_hand_seeded_evidence_does_not_gate_arm(self) -> None:
        feature_id = int(
            self.con.execute(
                "INSERT INTO roadmap (title) VALUES ('Bad signer feature')"
            ).lastrowid
        )
        document_id = int(
            self.con.execute(
                "INSERT INTO documents (feature_id,kind,seq,title,body) "
                "VALUES (?,'spec',1,'Bad signer','body')",
                (feature_id,),
            ).lastrowid
        )
        self.con.commit()
        approvals = sprint_domain.SprintSpecApprovalStore(self.con)
        with self.assertRaisesRegex(
            sprint_domain.SprintAuthorityError, "Review shell"
        ):
            approvals.record(document_id, 1, verdict="pass")
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_spec_approvals WHERE document_id=?",
                (document_id,),
            ).fetchone()[0],
        )

        sprint_id, _ = self.create_sprint()
        self.con.execute(
            "UPDATE sprint_spec_approvals SET reviewer_shell_id=1 "
            "WHERE approval_id=(SELECT approval_id FROM sprint_specs "
            "WHERE sprint_id=?)",
            (sprint_id,),
        )
        self.con.commit()
        self.store.arm(sprint_id, 3)
        self.assertEqual(
            "armed",
            self.con.execute(
                "SELECT lifecycle FROM sprints WHERE sprint_id=?", (sprint_id,)
            ).fetchone()[0],
        )


class LiveReplanningTest(SprintDomainCase):
    def test_paused_recall_reassign_and_reroute_dispatches_fresh_generation(self) -> None:
        self.con.execute(
            "INSERT INTO shells "
            "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
            "VALUES (5,'Developer 2','DEV2','dev','prompt',1)"
        )
        sprint_id, work_unit_id = self.create_sprint()
        document_id = int(
            self.con.execute(
                "SELECT document_id FROM sprint_specs WHERE sprint_id=?", (sprint_id,)
            ).fetchone()[0]
        )
        feature_id = int(
            self.con.execute(
                "SELECT feature_id FROM sprints WHERE sprint_id=?", (sprint_id,)
            ).fetchone()[0]
        )
        task_id = int(
            self.con.execute(
                "INSERT INTO spec_tasks (feature_id,document_id,seq,title) "
                "VALUES (?,?,1,'Governing task')",
                (feature_id, document_id),
            ).lastrowid
        )
        self.con.execute(
            "INSERT INTO sprint_work_unit_tasks "
            "(sprint_id,work_unit_id,task_id) VALUES (?,?,?)",
            (sprint_id, work_unit_id, task_id),
        )
        self.con.execute(
            "INSERT INTO sprint_participants "
            "(sprint_id,shell_id,role,harness,model,effort) "
            "VALUES (?,5,'developer','codex','old-model','high')",
            (sprint_id,),
        )
        self.con.commit()

        assignment_wake = self.store.arm(sprint_id, 3)[0]
        assignment_message = int(
            self.con.execute(
                "SELECT message_id FROM sprint_wake_messages WHERE wake_id=?",
                (assignment_wake,),
            ).fetchone()[0]
        )
        self.con.execute(
            "UPDATE wake_message SET delivered_at=datetime('now') WHERE message_id=?",
            (assignment_message,),
        )
        self.con.execute(
            "UPDATE sprint_wake_outbox SET state='delivered',"
            "delivered_at=datetime('now') WHERE wake_id=?",
            (assignment_wake,),
        )
        self.con.commit()
        sprint_message_delivery.SprintMessageStore(self.con).mark_read(
            assignment_message, 1, sprint_id=sprint_id
        )
        self.store.pause(
            sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
            reason="Planner is restructuring assignments",
        )

        units = sprint_domain.SprintWorkUnitStore(self.con)
        self.assertTrue(
            units.recall(
                sprint_id,
                work_unit_id,
                3,
                reason="move the lane to available capacity",
            )
        )
        self.assertTrue(
            units.replan(
                sprint_id,
                work_unit_id,
                3,
                assigned_shell_id=5,
                title="Reassigned foundation",
                expected_output="Ship through the replacement route",
                task_ids=(task_id,),
                planned_wave=4,
            )
        )
        participant_id = int(
            self.con.execute(
                "SELECT participant_id FROM sprint_participants "
                "WHERE sprint_id=? AND shell_id=5",
                (sprint_id,),
            ).fetchone()[0]
        )
        prepared = sprint_domain.sprint_participant_chats.PreparedParticipantRoute(
            participant_id=participant_id,
            shell_id=5,
            role="developer",
            shortname="DEV2",
            harness="codex",
            provider="openai",
            model="replacement-model",
            effort="medium",
            worktree="/tmp/dev2",
        )
        with mock.patch.object(
            sprint_domain.sprint_participant_chats,
            "prepare_participant_route",
            return_value=prepared,
        ):
            changed = sprint_domain.SprintParticipantStore(
                self.con, probe_harness=lambda _harness: None
            ).reroute(
                sprint_id,
                3,
                participant_shell_id=5,
                harness="codex",
                model="replacement-model",
                effort="medium",
                route="codex/replacement-model",
            )
        self.assertTrue(changed)

        receipt = self.store.resume(
            sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
            reason="replan is complete",
        )

        self.assertEqual(1, len(receipt.dispatched_wake_ids))
        recall_event = json.loads(
            self.con.execute(
                "SELECT payload FROM sprint_events WHERE sprint_id=? "
                "AND event_type='work_unit.recalled'",
                (sprint_id,),
            ).fetchone()[0]
        )
        self.assertEqual("planned", recall_event["after"])
        self.assertEqual(
            "accepted",
            self.con.execute(
                "SELECT disposition FROM wake_message WHERE message_id=?",
                (assignment_message,),
            ).fetchone()[0],
        )
        self.assertEqual(
            (5, "Reassigned foundation", 4),
            tuple(
                self.con.execute(
                    "SELECT assigned_shell_id,title,planned_wave "
                    "FROM sprint_work_units WHERE work_unit_id=?",
                    (work_unit_id,),
                ).fetchone()
            ),
        )
        self.assertEqual(
            ("replacement-model", "medium", "codex/replacement-model"),
            tuple(
                self.con.execute(
                    "SELECT model,effort,route FROM sprint_participants "
                    "WHERE participant_id=?",
                    (participant_id,),
                ).fetchone()
            ),
        )
        fresh = self.con.execute(
            "SELECT receiver_shell_id,idempotency_key FROM wake_message "
            "WHERE work_unit_id=? AND message_kind='work_assignment' "
            "ORDER BY message_id DESC LIMIT 1",
            (work_unit_id,),
        ).fetchone()
        self.assertEqual(5, fresh["receiver_shell_id"])
        self.assertTrue(str(fresh["idempotency_key"]).endswith(":assignment:2"))

    def test_recall_rejects_armed_and_registered_pr_lanes(self) -> None:
        sprint_id, work_unit_id = self.create_sprint()
        self.store.arm(sprint_id, 3)
        units = sprint_domain.SprintWorkUnitStore(self.con)
        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError, "only while the Sprint is paused"
        ):
            units.recall(sprint_id, work_unit_id, 3, reason="too early")

        self.store.pause(
            sprint_id,
            sprint_domain.LifecycleActor("planner", 3),
            reason="inspect lane",
        )
        owner = int(
            self.con.execute(
                "SELECT participant_id FROM sprint_participants "
                "WHERE sprint_id=? AND shell_id=1",
                (sprint_id,),
            ).fetchone()[0]
        )
        registered_pr_id = int(
            self.con.execute(
                "INSERT INTO sprint_registered_prs "
                "(sprint_id,owner_participant_id,repository,pr_number) "
                "VALUES (?,?,'acme/repo',99)",
                (sprint_id, owner),
            ).lastrowid
        )
        self.con.execute(
            "INSERT INTO sprint_pr_work_units "
            "(sprint_id,registered_pr_id,work_unit_id) VALUES (?,?,?)",
            (sprint_id, registered_pr_id, work_unit_id),
        )
        self.con.commit()
        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError, "preserve that lane"
        ):
            units.recall(sprint_id, work_unit_id, 3, reason="unsafe rewind")


class LifecycleTest(SprintDomainCase):
    def test_sprint_insert_must_start_prepared(self) -> None:
        sprint_id, _ = self.create_sprint()
        feature_id = self.con.execute(
            "SELECT feature_id FROM sprints WHERE sprint_id=?", (sprint_id,)
        ).fetchone()[0]

        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "Sprint inserts must start prepared"
        ):
            self.con.execute(
                "INSERT INTO sprints "
                "(feature_id,originating_planner_shell_id,lifecycle,"
                "merge_grant_enabled) VALUES (?,3,'armed',1)",
                (feature_id,),
            )

        self.assertEqual(
            [(sprint_id, "prepared")],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT sprint_id,lifecycle FROM sprints ORDER BY sprint_id"
                )
            ],
        )

    def test_arm_defers_participant_conversations_until_delivery(self) -> None:
        sprint_id, _ = self.create_sprint()

        self.store.arm(sprint_id, 3)

        participants = self.con.execute(
            "SELECT participant_id FROM sprint_participants "
            "WHERE sprint_id=? ORDER BY participant_id",
            (sprint_id,),
        ).fetchall()
        self.assertEqual(3, len(participants))
        self.assertEqual(
            set(),
            {"persistent_conversation_id", "current_conversation_id"}
            & {
                row[1]
                for row in self.con.execute(
                    "PRAGMA table_info(sprint_participants)"
                )
            },
        )
        self.assertEqual(
            0,
            self.con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
        )
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_participant_conversations"
            ).fetchone()[0],
        )
        self.assertEqual(
            0,
            self.con.execute("SELECT COUNT(*) FROM conversation_outbox").fetchone()[0],
            "creating placeholders must not launch or wake a harness",
        )
        event = self.con.execute(
            "SELECT payload FROM sprint_events WHERE sprint_id=? "
            "AND event_type='lifecycle.armed'",
            (sprint_id,),
        ).fetchone()
        payload = json.loads(event["payload"])
        self.assertEqual(2, len(payload["initial_wake_ids"]))
        self.assertEqual(1, len(payload["work_wake_ids"]))
        self.assertIn(payload["planner_wake_id"], payload["initial_wake_ids"])

        self.con.execute(
            "INSERT INTO conversations "
            "(shell_id,owner_user_id,harness,worktree,title,"
            "creation_idempotency_key,creation_request_hash) "
            "VALUES (1,1,'codex','/normal','Normal chat','normal-1','hash')"
        )
        self.assertEqual(
            [("normal", 1)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT conversation_scope,COUNT(*) FROM conversations "
                    "GROUP BY conversation_scope ORDER BY conversation_scope"
                )
            ],
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO conversations "
                "(shell_id,owner_user_id,harness,worktree,title,"
                "creation_idempotency_key,creation_request_hash) "
                "VALUES (1,1,'codex','/normal-2','Other normal chat',"
                "'normal-2','hash')"
            )

    def test_arm_rolls_back_when_initial_release_fails(self) -> None:
        sprint_id, _ = self.create_sprint()
        self.con.execute(
            "CREATE TRIGGER reject_initial_message BEFORE INSERT ON wake_message "
            "BEGIN SELECT RAISE(ABORT,'release fault'); END"
        )
        self.con.commit()

        with self.assertRaisesRegex(sqlite3.IntegrityError, "release fault"):
            self.store.arm(sprint_id, 3)

        self.assertEqual(
            "prepared",
            self.con.execute(
                "SELECT lifecycle FROM sprints WHERE sprint_id=?", (sprint_id,)
            ).fetchone()[0],
        )
        self.assertEqual(
            0,
            self.con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
        )
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_participant_conversations"
            ).fetchone()[0],
        )
        self.assertEqual(
            3,
            self.con.execute(
                "SELECT COUNT(*) FROM sprint_participants WHERE sprint_id=?",
                (sprint_id,),
            ).fetchone()[0],
        )

    def test_arm_ignores_orphan_conversation_keys_from_reused_numeric_ids(self) -> None:
        historical = (
            ("cv_old_planner", 3, "sprint:1:participant:1:work"),
            ("cv_old_developer", 1, "sprint:1:participant:2:work"),
            ("cv_old_reviewer", 2, "sprint:1:participant:3:work"),
        )
        self.con.executemany(
            "INSERT INTO conversations "
            "(conversation_id,shell_id,owner_user_id,harness,worktree,state,"
            "closed_at,title,creation_idempotency_key,creation_request_hash,"
            "conversation_scope) "
            "VALUES (?,?,1,'codex','/historical','closed',datetime('now'),"
            "'Closed historical Sprint',?,'historical-request','sprint')",
            historical,
        )
        self.con.commit()

        sprint_id, _ = self.create_sprint()
        participants = self.con.execute(
            "SELECT participant_id FROM sprint_participants "
            "WHERE sprint_id=? ORDER BY participant_id",
            (sprint_id,),
        ).fetchall()
        self.assertEqual(1, sprint_id)
        self.assertEqual([1, 2, 3], [row[0] for row in participants])

        wake_ids = self.store.arm(sprint_id, 3)

        self.assertEqual(2, len(wake_ids))
        self.assertEqual(
            "armed",
            self.con.execute(
                "SELECT lifecycle FROM sprints WHERE sprint_id=?", (sprint_id,)
            ).fetchone()[0],
        )
        linked = self.con.execute(
            "SELECT c.conversation_id,c.creation_idempotency_key "
            "FROM sprint_participant_conversations link "
            "JOIN conversations c ON c.conversation_id=link.conversation_id "
            "ORDER BY link.sprint_participant_id"
        ).fetchall()
        self.assertEqual(0, len(linked))
        self.assertEqual(
            3,
            self.con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
        )
        self.assertEqual(
            0,
            self.con.execute("SELECT COUNT(*) FROM active_shell_chats").fetchone()[0],
        )

    def test_armed_sprint_merge_grant_is_immutable(self) -> None:
        sprint_id, _ = self.create_sprint()
        self.store.arm(sprint_id, 3)

        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "Sprint merge grant is immutable after arming",
        ):
            self.con.execute(
                "UPDATE sprints SET merge_grant_enabled=0 WHERE sprint_id=?",
                (sprint_id,),
            )

        self.assertEqual(
            ("armed", 1),
            tuple(
                self.con.execute(
                    "SELECT lifecycle,merge_grant_enabled FROM sprints "
                    "WHERE sprint_id=?",
                    (sprint_id,),
                ).fetchone()
            ),
        )

    def test_arm_atomically_releases_only_dependency_free_work(self) -> None:
        sprint_id, first_unit = self.create_sprint()
        blocked_unit = self.con.execute(
            "INSERT INTO sprint_work_units "
            "(sprint_id,assigned_shell_id,reviewer_shell_id,title,expected_output,"
            "planned_wave) VALUES (?,1,2,'Blocked','Wait for foundation',1)",
            (sprint_id,),
        ).lastrowid
        self.con.execute(
            "INSERT INTO sprint_work_unit_dependencies "
            "(sprint_id,work_unit_id,depends_on_work_unit_id) VALUES (?,?,?)",
            (sprint_id, blocked_unit, first_unit),
        )
        self.con.commit()

        wake_ids = self.store.arm(sprint_id, 3)

        self.assertEqual(2, len(wake_ids))
        self.assertEqual(
            ("armed", 2),
            tuple(
                self.con.execute(
                    "SELECT lifecycle,version FROM sprints WHERE sprint_id=?",
                    (sprint_id,),
                ).fetchone()
            ),
        )
        self.assertEqual(
            [(first_unit, "ready"), (blocked_unit, "planned")],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT work_unit_id,disposition FROM sprint_work_units "
                    "WHERE sprint_id=? ORDER BY work_unit_id",
                    (sprint_id,),
                )
            ],
        )
        message = self.con.execute(
            "SELECT work_unit_id,message_kind,actionable,disposition,body "
            "FROM wake_message WHERE sprint_id=? "
            "AND message_kind='work_assignment'",
            (sprint_id,),
        ).fetchone()
        self.assertEqual(first_unit, message["work_unit_id"])
        self.assertEqual("work_assignment", message["message_kind"])
        self.assertEqual(1, message["actionable"])
        self.assertEqual("pending", message["disposition"])
        self.assertEqual(
            "Foundation\n\nShip the durable foundation", message["body"]
        )
        self.assertEqual(
            [(wake_ids[0], message["work_unit_id"])],
            [
                (row["wake_id"], first_unit)
                for row in self.con.execute(
                    "SELECT w.wake_id FROM sprint_wake_outbox w "
                    "JOIN sprint_wake_messages wm USING (wake_id) "
                    "JOIN wake_message m USING (message_id) "
                    "WHERE w.sprint_id=? AND m.work_unit_id=?",
                    (sprint_id, first_unit),
                )
            ],
        )

    def test_single_armed_invariant_rolls_back_second_release(self) -> None:
        first, _ = self.create_sprint()
        second, second_unit = self.create_sprint()
        self.store.arm(first, 3)

        with self.assertRaises(sqlite3.IntegrityError):
            self.store.arm(second, 3)

        self.assertEqual(
            "prepared",
            self.con.execute(
                "SELECT lifecycle FROM sprints WHERE sprint_id=?", (second,)
            ).fetchone()[0],
        )
        self.assertEqual(
            "planned",
            self.con.execute(
                "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                (second_unit,),
            ).fetchone()[0],
        )
        self.assertEqual(
            0,
            self.con.execute(
                "SELECT COUNT(*) FROM wake_message WHERE sprint_id=?", (second,)
            ).fetchone()[0],
        )

    def test_arm_releases_only_one_ready_unit_per_participant(self) -> None:
        sprint_id, first_unit = self.create_sprint()
        second_unit = self.con.execute(
            "INSERT INTO sprint_work_units "
            "(sprint_id,assigned_shell_id,reviewer_shell_id,title,expected_output) "
            "VALUES (?,1,2,'Second','Ship second')",
            (sprint_id,),
        ).lastrowid
        self.con.commit()

        wake_ids = self.store.arm(sprint_id, 3)

        self.assertEqual(2, len(wake_ids))
        self.assertEqual(
            [(first_unit,)],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT m.work_unit_id FROM sprint_wake_messages wm "
                    "JOIN wake_message m USING (message_id) "
                    "WHERE wm.wake_id=? ORDER BY m.message_id",
                    (wake_ids[0],),
                )
            ],
        )
        self.assertEqual(
            [(first_unit, "ready"), (second_unit, "planned")],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT work_unit_id,disposition FROM sprint_work_units "
                    "WHERE sprint_id=? ORDER BY work_unit_id",
                    (sprint_id,),
                )
            ],
        )

    def test_arm_rejects_wrong_planner_but_accepts_failing_review_evidence(self) -> None:
        sprint_id, unit_id = self.create_sprint(approval_verdict="fail")
        with self.assertRaises(sprint_domain.SprintAuthorityError):
            self.store.arm(sprint_id, 4)
        wake_ids = self.store.arm(sprint_id, 3)
        self.assertEqual(
            ("armed", "ready", 2),
            (
                self.con.execute(
                    "SELECT lifecycle FROM sprints WHERE sprint_id=?", (sprint_id,)
                ).fetchone()[0],
                self.con.execute(
                    "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                    (unit_id,),
                ).fetchone()[0],
                self.con.execute(
                    "SELECT COUNT(*) FROM sprint_wake_outbox WHERE sprint_id=?",
                    (sprint_id,),
                ).fetchone()[0],
            ),
        )
        self.assertEqual(2, len(wake_ids))

    def test_arm_accepts_no_qaqc_evidence(self) -> None:
        sprint_id, _ = self.create_sprint()
        approval_id = self.con.execute(
            "SELECT approval_id FROM sprint_specs WHERE sprint_id=?", (sprint_id,)
        ).fetchone()[0]
        self.con.execute(
            "UPDATE sprint_specs SET approval_id=NULL WHERE sprint_id=?", (sprint_id,)
        )
        self.con.execute(
            "DELETE FROM sprint_spec_approvals WHERE approval_id=?", (approval_id,)
        )
        self.con.commit()

        self.store.arm(sprint_id, 3)

        self.assertEqual(
            ("armed", None, 0),
            tuple(
                self.con.execute(
                    "SELECT s.lifecycle,ss.approval_id,"
                    "(SELECT COUNT(*) FROM sprint_spec_approvals) "
                    "FROM sprints s JOIN sprint_specs ss USING (sprint_id) "
                    "WHERE s.sprint_id=?",
                    (sprint_id,),
                ).fetchone()
            ),
        )

    def test_arm_rejects_missing_bound_spec_without_effect(self) -> None:
        sprint_id, unit_id = self.create_sprint()
        self.con.execute("DELETE FROM sprint_specs WHERE sprint_id=?", (sprint_id,))
        self.con.commit()

        with self.assertRaisesRegex(
            sprint_domain.SprintInvariantError, "exact current governing spec"
        ):
            self.store.arm(sprint_id, 3)

        self.assertEqual(
            ("prepared", "planned", 0),
            (
                self.con.execute(
                    "SELECT lifecycle FROM sprints WHERE sprint_id=?", (sprint_id,)
                ).fetchone()[0],
                self.con.execute(
                    "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                    (unit_id,),
                ).fetchone()[0],
                self.con.execute(
                    "SELECT COUNT(*) FROM sprint_wake_outbox WHERE sprint_id=?",
                    (sprint_id,),
                ).fetchone()[0],
            ),
        )

    def test_arm_ignores_stale_failed_findings_and_deleted_signer_evidence(self) -> None:
        sprint_id, _ = self.create_sprint()
        findings_id = int(
            self.con.execute(
                "INSERT INTO documents (kind,seq,title,body) "
                "VALUES ('doc',1,'Unresolved findings','blocking history')"
            ).lastrowid
        )
        self.con.execute("UPDATE shells SET is_deleted=1 WHERE shell_id=4")
        self.con.execute(
            "UPDATE sprint_spec_approvals "
            "SET verdict='fail',revision_sha256=?,reviewer_shell_id=4,"
            "findings_document_id=? "
            "WHERE approval_id=(SELECT approval_id FROM sprint_specs "
            "WHERE sprint_id=?)",
            ("0" * 64, findings_id, sprint_id),
        )
        self.con.commit()

        self.store.arm(sprint_id, 3)

        self.assertEqual(
            ("armed", "fail", "0" * 64, 4, findings_id),
            tuple(
                self.con.execute(
                    "SELECT s.lifecycle,a.verdict,a.revision_sha256,"
                    "a.reviewer_shell_id,a.findings_document_id "
                    "FROM sprints s JOIN sprint_specs ss USING (sprint_id) "
                    "JOIN sprint_spec_approvals a USING (approval_id) "
                    "WHERE s.sprint_id=?",
                    (sprint_id,),
                ).fetchone()
            ),
        )

    def test_arm_uses_immutable_bound_spec_after_current_document_edit(self) -> None:
        sprint_id, unit_id = self.create_sprint()
        self.con.execute(
            "UPDATE documents SET body='edited after QAQC' "
            "WHERE document_id=(SELECT document_id FROM sprint_specs "
            "WHERE sprint_id=?)",
            (sprint_id,),
        )
        self.con.commit()

        self.store.arm(sprint_id, 3)

        self.assertEqual(
            ("armed", "ready", 2),
            (
                self.con.execute(
                    "SELECT lifecycle FROM sprints WHERE sprint_id=?", (sprint_id,)
                ).fetchone()[0],
                self.con.execute(
                    "SELECT disposition FROM sprint_work_units WHERE work_unit_id=?",
                    (unit_id,),
                ).fetchone()[0],
                self.con.execute(
                    "SELECT COUNT(*) FROM wake_message WHERE sprint_id=?",
                    (sprint_id,),
                ).fetchone()[0],
            ),
        )
        document_id = int(
            self.con.execute(
                "SELECT document_id FROM sprint_specs WHERE sprint_id=?", (sprint_id,)
            ).fetchone()[0]
        )
        evidence = sprint_domain.SprintSpecRevisionStore(self.con).read(
            sprint_id, document_id, caller_shell_id=1
        )
        self.assertEqual("governing spec revision 1", evidence["body"])

    def test_transition_authority_and_database_backstop(self) -> None:
        sprint_id, _ = self.create_sprint()
        self.store.arm(sprint_id, 3)
        with self.assertRaises(sprint_domain.SprintAuthorityError):
            self.store.transition(
                sprint_id,
                "paused",
                sprint_domain.LifecycleActor("participant", 4),
                reason="not assigned",
            )
        self.assertTrue(
            self.store.transition(
                sprint_id,
                "paused",
                sprint_domain.LifecycleActor("participant", 1),
                reason="integrity threat",
            )
        )
        with self.assertRaises(sprint_domain.SprintAuthorityError):
            self.store.transition(
                sprint_id,
                "armed",
                sprint_domain.LifecycleActor("participant", 1),
            )
        self.assertTrue(
            self.store.transition(
                sprint_id,
                "armed",
                sprint_domain.LifecycleActor("fnb"),
            )
        )
        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "illegal Sprint lifecycle transition"
        ):
            self.con.execute(
                "UPDATE sprints SET lifecycle='prepared' WHERE sprint_id=?",
                (sprint_id,),
            )
        self.assertEqual(
            "armed",
            self.con.execute(
                "SELECT lifecycle FROM sprints WHERE sprint_id=?", (sprint_id,)
            ).fetchone()[0],
        )

    def test_terminal_wake_failure_auto_pauses_with_evidence(self) -> None:
        sprint_id, _ = self.create_sprint()
        wake_id = self.store.arm(sprint_id, 3)[0]

        self.assertEqual(1, self.store.record_wake_failure(wake_id, "fault one"))
        self.assertEqual(2, self.store.record_wake_failure(wake_id, "fault two"))
        self.assertEqual(
            "armed",
            self.con.execute(
                "SELECT lifecycle FROM sprints WHERE sprint_id=?", (sprint_id,)
            ).fetchone()[0],
        )
        self.assertEqual(3, self.store.record_wake_failure(wake_id, "fault three"))

        self.assertEqual(
            ("paused", "failed", 3),
            tuple(
                self.con.execute(
                    "SELECT s.lifecycle,w.state,w.attempt_count FROM sprints s "
                    "JOIN sprint_wake_outbox w USING (sprint_id) "
                    "WHERE w.wake_id=?",
                    (wake_id,),
                ).fetchone()
            ),
        )
        self.assertEqual(
            [(1, "fault one"), (2, "fault two"), (3, "fault three")],
            [
                tuple(row)
                for row in self.con.execute(
                    "SELECT attempt_number,error_detail FROM sprint_wake_attempts "
                    "WHERE wake_id=? ORDER BY attempt_number",
                    (wake_id,),
                )
            ],
        )
        report = self.con.execute(
            "SELECT report_kind,body FROM sprint_reports WHERE sprint_id=?",
            (sprint_id,),
        ).fetchone()
        self.assertEqual("pause", report["report_kind"])
        self.assertIn('"reason": "wake_delivery_exhausted"', report["body"])
        self.assertEqual(
            "lifecycle.paused",
            self.con.execute(
                "SELECT event_type FROM sprint_events WHERE sprint_id=? "
                "ORDER BY event_id DESC LIMIT 1",
                (sprint_id,),
            ).fetchone()[0],
        )


class ArmedServiceSwitchTest(SprintDomainCase):
    def test_non_armed_states_invoke_zero_poll_or_dispatch_callbacks(self) -> None:
        sprint_id, _ = self.create_sprint()
        calls: list[tuple[int, str]] = []
        switch = sprint_domain.ArmedServiceSwitch(
            self.store,
            (lambda active, trigger: calls.append((active, trigger)),),
        )

        self.assertFalse(switch.recover_on_startup())
        self.assertFalse(switch.tick())
        self.assertEqual([], calls)
        self.store.arm(sprint_id, 3)
        self.assertTrue(switch.tick())
        self.assertEqual([(sprint_id, "pulse")], calls)
        self.store.transition(
            sprint_id,
            "paused",
            sprint_domain.LifecycleActor("participant", 1),
            reason="hold",
        )
        self.assertFalse(switch.tick())
        self.assertEqual([(sprint_id, "pulse")], calls)

    def test_restart_recovers_armed_sprint_from_durable_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sprint.db"
            with closing(db_driver.connect(path)) as seed:
                apply_schema(seed)
                seed.execute(
                    "INSERT INTO users (user_id,username) VALUES (1,'operator')"
                )
                seed.executemany(
                    "INSERT INTO shells "
                    "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
                    "VALUES (?,?,?,?,?,1)",
                    (
                        (1, "Developer", "DEV1", "dev", "prompt"),
                        (2, "Reviewer", "REV1", "reviewer", "prompt"),
                        (3, "Planner", "PLN1", "planner", "prompt"),
                    ),
                )
                seed.execute(
                    "INSERT INTO roadmap (feature_id,title,roadmap_status) "
                    "VALUES (1,'Feature','in_progress')"
                )
                body = "spec"
                revision = hashlib.sha256(body.encode()).hexdigest()
                seed.execute(
                    "INSERT INTO documents "
                    "(document_id,feature_id,kind,seq,title,body) "
                    "VALUES (1,1,'spec',1,'Spec',?)",
                    (body,),
                )
                seed.execute(
                    "INSERT INTO sprint_spec_approvals "
                    "(approval_id,document_id,revision_sha256,reviewer_shell_id,verdict) "
                    "VALUES (1,1,?,2,'pass')",
                    (revision,),
                )
                seed.execute(
                    "INSERT INTO sprints "
                    "(sprint_id,feature_id,originating_planner_shell_id,"
                    "merge_grant_enabled) VALUES (1,1,3,1)"
                )
                seed.execute(
                    "INSERT INTO sprint_specs "
                    "(sprint_id,document_id,bound_revision_sha256,approval_id,"
                    "included_at,bound_revision_body) "
                    "VALUES (1,1,?,1,datetime('now'),?)",
                    (revision, body),
                )
                seed.executemany(
                    "INSERT INTO sprint_participants "
                    "(sprint_id,shell_id,role,harness) VALUES (1,?,?,?)",
                    ((3, "planner", "codex"), (1, "developer", "codex"),
                     (2, "reviewer", "kimi")),
                )
                seed.execute(
                    "INSERT INTO sprint_work_units "
                    "(sprint_id,assigned_shell_id,reviewer_shell_id,title,expected_output) "
                    "VALUES (1,1,2,'Unit','Output')"
                )
                seed.commit()
                sprint_domain.SprintLifecycleStore(
                    seed, probe_harness=lambda _harness: None
                ).arm(1, 3)

            calls: list[tuple[int, str]] = []
            with closing(db_driver.connect(path)) as restarted:
                switch = sprint_domain.ArmedServiceSwitch(
                    sprint_domain.SprintLifecycleStore(restarted),
                    (lambda active, trigger: calls.append((active, trigger)),),
                )
                self.assertTrue(switch.recover_on_startup())
            self.assertEqual([(1, "startup")], calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
