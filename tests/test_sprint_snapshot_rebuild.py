#!/usr/bin/env python3
"""Lossless Sprints v2 snapshot/rebuild regressions (#918)."""
from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
SCHEMA = ENGINE / "schema.sql"
MIGRATIONS = ENGINE / "migrations"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))
import db_driver  # noqa: E402
import rebuild  # noqa: E402
import route_bindings  # noqa: E402
import snapshot  # noqa: E402


def apply_engine_schema(path: Path, *, through: str | None = None) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript(SCHEMA.read_text())
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            if through is not None and migration.name > through:
                break
            con.executescript(migration.read_text())
        con.execute("PRAGMA foreign_keys=ON")
        con.commit()
    finally:
        con.close()


def rows_by_table(path: Path, tables: list[str]) -> dict[str, list[tuple]]:
    con = sqlite3.connect(path)
    try:
        return {
            table: [
                tuple(row)
                for row in con.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]
            for table in tables
        }
    finally:
        con.close()


def seed_prepared(
    con: sqlite3.Connection,
    *,
    reviewed: bool = True,
    immutable_body: bool = True,
) -> int:
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("INSERT INTO users (user_id,username) VALUES (1,'operator')")
    con.executemany(
        "INSERT INTO shells "
        "(shell_id,display_name,shortname,flavor,system_prompt,user_id) "
        "VALUES (?,?,?,?,?,1)",
        (
            (1, "Developer", "DEV1", "dev", "dev prompt"),
            (2, "Reviewer", "REV1", "reviewer", "review prompt"),
            (3, "Planner", "PLN1", "planner", "plan prompt"),
        ),
    )
    feature_id = con.execute(
        "INSERT INTO roadmap (feature_id,title,roadmap_status) "
        "VALUES (19,'Tiny Sprint feature','in_progress')"
    ).lastrowid
    body = "# Exact Sprint governing spec\n\nOne bounded unit."
    document_id = con.execute(
        "INSERT INTO documents (document_id,feature_id,kind,seq,title,body) "
        "VALUES (56,?,'spec',1,'Sprint spec',?)",
        (feature_id, body),
    ).lastrowid
    revision = hashlib.sha256(body.encode()).hexdigest()
    approval_id = None
    if reviewed:
        approval_id = con.execute(
            "INSERT INTO sprint_spec_approvals "
            "(approval_id,document_id,revision_sha256,reviewer_shell_id,verdict) "
            "VALUES (2,?,?,2,'pass')",
            (document_id, revision),
        ).lastrowid
    sprint_id = con.execute(
        "INSERT INTO sprints "
        "(sprint_id,feature_id,originating_planner_shell_id,merge_grant_enabled) "
        "VALUES (1,?,3,1)",
        (feature_id,),
    ).lastrowid
    if immutable_body:
        con.execute(
            "INSERT INTO sprint_specs "
            "(sprint_id,document_id,bound_revision_sha256,approval_id,"
            "bound_revision_body,bound_revision_legacy) VALUES (?,?,?,?,?,0)",
            (sprint_id, document_id, revision, approval_id, body),
        )
    else:
        con.execute(
            "INSERT INTO sprint_specs "
            "(sprint_id,document_id,bound_revision_sha256,approval_id) "
            "VALUES (?,?,?,?)",
            (sprint_id, document_id, revision, approval_id),
        )
    con.executemany(
        "INSERT INTO sprint_participants "
        "(participant_id,sprint_id,shell_id,role,harness,model,effort,route) "
        "VALUES (?,1,?,?,?,?,?,?)",
        (
            (1, 3, "planner", "codex", "gpt-5.6-sol", "high", "sol"),
            (2, 1, "developer", "kimi", "kimi-code/k3", "high", "kimi"),
            (3, 2, "reviewer", "claude", "opus", "high", "opus-5"),
        ),
    )
    con.executemany(
        "INSERT INTO spec_tasks "
        "(task_id,feature_id,document_id,seq,title,status,shell_id) "
        "VALUES (?,?,?,?,?,'pending',?)",
        (
            (30, feature_id, document_id, 1, "Implement tiny change", 1),
            (31, feature_id, document_id, 2, "Verify tiny change", 1),
        ),
    )
    con.executemany(
        "INSERT INTO sprint_work_units "
        "(work_unit_id,sprint_id,assigned_shell_id,reviewer_shell_id,title,"
        "expected_output,planned_wave,output_kind) "
        "VALUES (?,1,1,2,?,?,?,?)",
        (
            (1, "Implement", "One focused PR", 0, "code"),
            (2, "Verify", "A verification report", 1, "report_only"),
        ),
    )
    con.executemany(
        "INSERT INTO sprint_work_unit_tasks (sprint_id,work_unit_id,task_id) "
        "VALUES (1,?,?)",
        ((1, 30), (2, 31)),
    )
    con.execute(
        "INSERT INTO sprint_work_unit_dependencies "
        "(sprint_id,work_unit_id,depends_on_work_unit_id) VALUES (1,2,1)"
    )
    con.execute(
        "INSERT INTO sprint_events "
        "(sprint_id,event_type,actor_kind,actor_shell_id,payload) "
        "VALUES (1,'sprint.declared','planner',3,'{\"feature_id\":19}')"
    )
    con.commit()
    return int(sprint_id)


def arm_with_representative_state(con: sqlite3.Connection) -> None:
    binding = {
        "contract_version": 2,
        "control_state": "controlled",
        "harness": "kimi",
        "requested_model": "kimi-code/k3",
        "provider_model": "k3",
        "requested_effort": "high",
        "effective_effort": "high",
        "native_variant_id": None,
        "transport": "kimi-effort-environment",
        "catalogue_generation": "a" * 32,
        "evidence_digest": "b" * 64,
        "selector_binding": {"kind": "configured-alias",
                             "selector": "kimi-code/k3"},
        "adapter_metadata": {},
    }
    binding_digest = route_bindings.digest_json(binding)
    binding_id = con.execute(
        "INSERT INTO sprint_participant_route_bindings ("
        "participant_id,route_revision,contract_version,control_state,harness,"
        "requested_model,provider_model,requested_effort,effective_effort,"
        "native_variant_id,transport,catalogue_generation,evidence_digest,"
        "selector_binding,adapter_metadata,binding_json,binding_digest) "
        "VALUES (2,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            binding["contract_version"], binding["control_state"],
            binding["harness"], binding["requested_model"],
            binding["provider_model"], binding["requested_effort"],
            binding["effective_effort"], binding["native_variant_id"],
            binding["transport"], binding["catalogue_generation"],
            binding["evidence_digest"],
            route_bindings.canonical_json(binding["selector_binding"]),
            route_bindings.canonical_json(binding["adapter_metadata"]),
            route_bindings.canonical_json(binding), binding_digest,
        ),
    ).lastrowid
    con.execute(
        "UPDATE sprint_participants SET active_route_binding_id=? "
        "WHERE participant_id=2", (binding_id,)
    )
    conversations = (
        ("cv_plan", 3, "planner-work"),
        ("cv_dev", 1, "developer-work"),
        ("cv_rev", 2, "reviewer-work"),
    )
    con.executemany(
        "INSERT INTO conversations "
        "(conversation_id,shell_id,owner_user_id,harness,provider,model,effort,"
        "worktree,state,title,creation_idempotency_key,creation_request_hash,"
        "conversation_scope) "
        "VALUES (?,?,1,'codex','test','model','high','/worktree','idle',"
        "'Sprint work',?,'request-hash','sprint')",
        conversations,
    )
    con.execute(
        "UPDATE conversations SET state='queued' WHERE conversation_id='cv_dev'"
    )
    con.execute(
        "INSERT INTO conversation_messages "
        "(message_id,conversation_id,sender_kind,sender_ref,message_kind,body,"
        "idempotency_key,request_hash,state) VALUES "
        "(1,'cv_dev','engine','sprint-runtime','prompt','Sprint wake prompt',"
        "'native-wake-1','native-request-1','queued')"
    )
    con.execute(
        "INSERT INTO conversation_events "
        "(event_id,conversation_id,sequence,event_type,payload,message_id) "
        "VALUES (1,'cv_dev',1,'message.queued','{\"sprint_id\":1}',1)"
    )
    con.execute(
        "INSERT INTO conversation_outbox "
        "(outbox_id,conversation_id,message_id,state) "
        "VALUES (1,'cv_dev',1,'pending')"
    )
    con.executemany(
        "INSERT INTO sprint_participant_conversations "
        "(sprint_participant_id,conversation_id) VALUES (?,?)",
        ((1, "cv_plan"), (2, "cv_dev"), (3, "cv_rev")),
    )
    con.executemany(
        "UPDATE sprint_participants SET disposition=?,updated_at=? "
        "WHERE participant_id=?",
        (
            ("active", "2026-08-01 21:00:00", 1),
            ("active", "2026-08-01 21:00:01", 2),
            ("idle", "2026-08-01 21:00:02", 3),
        ),
    )
    con.executemany(
        "INSERT INTO active_shell_chats (shell_id,chat_id) VALUES (?,?)",
        ((3, "cv_plan"), (1, "cv_dev"), (2, "cv_rev")),
    )
    con.execute(
        "UPDATE sprints SET conformance_reviewer_shell_id=2,"
        "conformance_owner_generation=1,lifecycle='armed',"
        "armed_at='2026-08-01 21:00:00',updated_at='2026-08-01 21:00:00',"
        "version=2 WHERE sprint_id=1"
    )
    con.execute(
        "UPDATE sprint_work_units SET disposition='active',"
        "updated_at='2026-08-01 21:01:00' WHERE work_unit_id=1"
    )
    con.execute(
        "INSERT INTO wake_message "
        "(message_id,sprint_id,sender_shell_id,receiver_shell_id,"
        "from_participant_id,to_participant_id,work_unit_id,message_kind,body,"
        "declared_type,actionable,disposition,read_at,delivered_at,idempotency_key) "
        "VALUES (1,1,3,1,1,2,1,'work_assignment','Implement now','new',1,"
        "'accepted','2026-08-01 21:01:00','2026-08-01 21:01:01','assignment-1')"
    )
    con.execute(
        "INSERT INTO sprint_wake_outbox "
        "(wake_id,sprint_id,participant_id,receiver_shell_id,state,attempt_count,"
        "idempotency_key,delivered_at) VALUES (1,1,2,1,'delivered',1,'wake-1',"
        "'2026-08-01 21:01:01')"
    )
    con.execute(
        "INSERT INTO sprint_wake_messages (sprint_id,wake_id,message_id) "
        "VALUES (1,1,1)"
    )
    con.execute(
        "INSERT INTO sprint_wake_attempts "
        "(attempt_id,wake_id,attempt_number,target_conversation_id,native_run_ref,"
        "outcome,attempted_at) VALUES (1,1,1,'cv_dev','run-1','delivered',"
        "'2026-08-01 21:01:01')"
    )
    con.execute(
        "INSERT INTO sprint_liveness_expectations "
        "(message_id,sprint_id,participant_id,accepted_at,last_strong_at,"
        "last_strong_key,next_evaluation_at) VALUES (1,1,2,"
        "'2026-08-01 21:01:00','2026-08-01 21:01:00','accepted-1',"
        "'2026-08-01 21:06:00')"
    )
    con.execute(
        "INSERT INTO sprint_registered_prs "
        "(registered_pr_id,sprint_id,owner_participant_id,repository,pr_number) "
        "VALUES (1,1,2,'jedbjorn/dos-app',54)"
    )
    con.execute(
        "INSERT INTO pr_subscriptions "
        "(subscription_id,owner_shell_id,repository,pr_number,"
        "sprint_registered_pr_id) VALUES (1,1,'jedbjorn/dos-app',54,1)"
    )
    con.execute(
        "INSERT INTO sprint_pr_work_units "
        "(sprint_id,registered_pr_id,work_unit_id) VALUES (1,1,1)"
    )
    con.execute(
        "INSERT INTO sprint_pr_transitions "
        "(transition_id,registered_pr_id,normalized_state,transition_key,"
        "observed_head_sha,evidence,observed_at) VALUES "
        "(1,1,'green','pr-54-green','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',"
        "'{\"checks\":\"green\"}','2026-08-01 21:05:00')"
    )
    con.execute(
        "INSERT INTO pr_subscription_transitions "
        "(transition_id,subscription_id,normalized_state,transition_key,"
        "observed_head_sha,evidence,observed_at) VALUES "
        "(1,1,'green','subscription-54-green',"
        "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',"
        "'{\"checks\":\"green\"}','2026-08-01 21:05:00')"
    )
    con.execute(
        "INSERT INTO pr_subscription_poll_failures "
        "(failure_id,subscription_id,failure_count,backoff_seconds,trigger,"
        "error_detail,failed_at) VALUES "
        "(1,1,1,10.0,'pulse','fixture failure','2026-08-01 21:04:00')"
    )
    con.execute(
        "INSERT INTO sprint_judgments "
        "(judgment_id,sprint_id,participant_id,work_unit_id,kind,body) "
        "VALUES (1,1,2,1,'decision','Ready for review')"
    )
    con.execute(
        "INSERT INTO sprint_reports "
        "(report_id,sprint_id,report_kind,author_shell_id,body,idempotency_key) "
        "VALUES (1,1,'conformance',2,'Representative finding','report-1')"
    )
    con.execute(
        "INSERT INTO sprint_followups "
        "(followup_id,sprint_id,source_report_id,severity,title,body,"
        "spec_document_id,work_unit_id,idempotency_key) "
        "VALUES (1,1,1,'Medium','Follow-up','Inspect after close',56,1,'followup-1')"
    )
    recovery_event_id = int(
        con.execute(
            "INSERT INTO sprint_events "
            "(sprint_id,event_type,actor_kind,actor_shell_id,payload) "
            "VALUES (1,'wake.requeued','system',NULL,'{\"replacement_wake_id\":1}')"
        ).lastrowid
    )
    con.execute(
        "INSERT INTO sprint_wake_recovery_messages "
        "(recovery_event_id,sprint_id,prior_wake_id,replacement_wake_id,message_id) "
        "VALUES (?,1,1,1,1)",
        (recovery_event_id,),
    )
    con.execute(
        "INSERT INTO sprints "
        "(sprint_id,feature_id,originating_planner_shell_id,merge_grant_enabled) "
        "SELECT 2,feature_id,originating_planner_shell_id,1 FROM sprints "
        "WHERE sprint_id=1"
    )
    con.execute(
        "INSERT INTO sprint_participants "
        "(participant_id,sprint_id,shell_id,role,harness) "
        "VALUES (4,2,2,'reviewer','claude')"
    )
    con.execute(
        "UPDATE sprints SET lifecycle='paused',paused_at='2026-08-01 21:59:00' "
        "WHERE sprint_id=1"
    )
    con.execute(
        "UPDATE sprints SET conformance_reviewer_shell_id=2,"
        "conformance_owner_generation=1,lifecycle='armed',"
        "armed_at='2026-08-01 22:00:00' WHERE sprint_id=2"
    )
    con.execute(
        "UPDATE sprints SET lifecycle='completed',terminal_outcome='accepted',"
        "completed_at='2026-08-01 22:01:00' WHERE sprint_id=2"
    )
    con.execute(
        "INSERT INTO sprint_cleanup_targets "
        "(sprint_id,shell_id,target_kind,canonical_path,repository_root,"
        "git_common_dir,expected_base_branch) VALUES "
        "(2,1,'worktree','/repo/.sc-worktrees/dev1','/repo','/repo/.git',"
        "'shell/dev1')"
    )
    con.execute(
        "INSERT INTO sprint_cleanup_requests "
        "(cleanup_request_id,sprint_id,caller_shell_id,request_kind,"
        "idempotency_key,request_hash,response_json) VALUES "
        "(1,2,3,'adopted_legacy','snapshot-cleanup-request',"
        "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',"
        "'{\"action\":\"adopted_legacy\",\"projection\":{},"
        "\"sprint_id\":2,\"target_ids\":[1]}')"
    )
    con.execute(
        "UPDATE sprints SET lifecycle='armed',updated_at='2026-08-01 22:02:00' "
        "WHERE sprint_id=1"
    )
    con.commit()


class SprintSnapshotRebuildTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "shell_db.db"
        self.content = self.root / ".sc-state" / "local" / "content.sql"
        self.content.parent.mkdir(parents=True)
        apply_engine_schema(self.db)

    def snapshot_and_rebuild(self) -> None:
        with mock.patch.multiple(
            snapshot,
            DB_PATH=self.db,
            OUT_PATH=self.content,
            LEGACY_PATH=self.root / "missing-legacy.sql",
            REPO_ROOT=self.root,
        ), mock.patch.object(
            snapshot, "require_admin"
        ), mock.patch.object(
            snapshot.artifact_policy, "prepare_local_state", return_value=[]
        ), mock.patch.object(snapshot, "snapshot_map"):
            self.assertEqual(snapshot.main(), 0)

        with mock.patch.multiple(
            rebuild,
            ENGINE=self.root / ".super-coder",
            REPO_ROOT=self.root,
            DB_PATH=self.db,
            SNAPSHOT=self.content,
            SNAPSHOT_LEGACY=self.root / "missing-legacy.sql",
        ), mock.patch.object(
            rebuild.artifact_policy, "prepare_local_state", return_value=[]
        ), mock.patch.object(rebuild.map_repo, "main"):
            self.assertEqual(rebuild.main(["--no-backup"]), 0)

    def assert_roundtrip(self, *, armed: bool) -> None:
        con = sqlite3.connect(self.db)
        try:
            seed_prepared(con)
            if armed:
                arm_with_representative_state(con)
        finally:
            con.close()
        compared = [
            *snapshot.SPRINT_INSTANCE_TABLES,
            "conversations",
            "conversation_git_targets",
            "conversation_messages",
            "conversation_runs",
            "conversation_events",
            "conversation_outbox",
        ]
        before = rows_by_table(self.db, compared)
        if armed:
            self.assertTrue(
                all(before[table] for table in snapshot.SPRINT_INSTANCE_TABLES),
                "armed fixture must exercise every durable Sprint table",
            )

        self.snapshot_and_rebuild()

        after = rows_by_table(self.db, compared)
        self.assertEqual(before, after)
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual([], con.execute("PRAGMA foreign_key_check").fetchall())
        finally:
            con.close()

    def test_prepared_sprint_roundtrips_without_plan_or_generation_drift(self) -> None:
        self.assert_roundtrip(armed=False)

    def test_unreviewed_spec_binding_roundtrips_with_null_evidence(self) -> None:
        con = sqlite3.connect(self.db)
        try:
            seed_prepared(con, reviewed=False)
        finally:
            con.close()
        before = rows_by_table(self.db, ["sprint_specs", "sprint_spec_approvals"])
        self.assertEqual([], before["sprint_spec_approvals"])
        self.assertIsNone(before["sprint_specs"][0][3])

        self.snapshot_and_rebuild()

        after = rows_by_table(self.db, ["sprint_specs", "sprint_spec_approvals"])
        self.assertEqual(before, after)
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual([], con.execute("PRAGMA foreign_key_check").fetchall())
        finally:
            con.close()

    def test_pre_revision_snapshot_hash_matches_and_recovers_exact_body(self) -> None:
        self.db.unlink()
        apply_engine_schema(
            self.db, through="0203_sprint_cleanup_recovery.sql"
        )
        con = sqlite3.connect(self.db)
        try:
            seed_prepared(con, immutable_body=False)
        finally:
            con.close()

        self.snapshot_and_rebuild()

        con = sqlite3.connect(self.db)
        try:
            row = con.execute(
                "SELECT bound_revision_body,bound_revision_legacy "
                "FROM sprint_specs"
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(("# Exact Sprint governing spec\n\nOne bounded unit.", 1), row)

    def test_armed_in_flight_sprint_roundtrips_every_v2_table_exactly(self) -> None:
        self.assert_roundtrip(armed=True)

    def test_causal_message_parent_with_higher_id_roundtrips_exactly(self) -> None:
        con = sqlite3.connect(self.db)
        try:
            seed_prepared(con)
            con.execute(
                "INSERT INTO conversations "
                "(conversation_id,shell_id,owner_user_id,harness,provider,model,"
                "effort,worktree,title,creation_idempotency_key,"
                "creation_request_hash) VALUES "
                "('cv_causal',1,1,'codex','test','model','high','/worktree',"
                "'Causal order','causal-conversation','causal-request')"
            )
            con.execute(
                "INSERT INTO conversation_messages "
                "(message_id,conversation_id,sender_kind,sender_ref,message_kind,"
                "body,idempotency_key,request_hash) VALUES "
                "(100,'cv_causal','user','operator','prompt','parent',"
                "'causal-parent','parent-hash')"
            )
            con.execute(
                "INSERT INTO conversation_messages "
                "(message_id,conversation_id,sender_kind,sender_ref,message_kind,"
                "body,idempotency_key,request_hash,caused_by_message_id) VALUES "
                "(1,'cv_causal','engine','runtime','result','child',"
                "'causal-child','child-hash',100)"
            )
            con.commit()
        finally:
            con.close()
        compared = ["conversations", "conversation_messages"]
        before = rows_by_table(self.db, compared)

        self.snapshot_and_rebuild()

        self.assertEqual(before, rows_by_table(self.db, compared))

    def test_flat_participant_links_restore_out_of_id_order(self) -> None:
        con = sqlite3.connect(self.db)
        try:
            seed_prepared(con)
            con.executemany(
                "INSERT INTO conversations "
                "(conversation_id,shell_id,owner_user_id,harness,provider,model,"
                "effort,worktree,title,creation_idempotency_key,"
                "creation_request_hash,conversation_scope,state,closed_at) VALUES "
                "(?,1,1,'codex','test','model','high','/worktree','Sprint link',"
                "?,?,'sprint',?,?)",
                (
                    (
                        "cv_parent",
                        "link-parent",
                        "link-parent-hash",
                        "closed",
                        "2026-08-01 20:00:00",
                    ),
                    ("cv_child", "link-child", "link-child-hash", "idle", None),
                ),
            )
            con.execute(
                "INSERT INTO sprint_participant_conversations "
                "(participant_conversation_id,sprint_participant_id,"
                "conversation_id) VALUES (100,2,'cv_parent')"
            )
            con.execute(
                "INSERT INTO sprint_participant_conversations "
                "(participant_conversation_id,sprint_participant_id,"
                "conversation_id) VALUES (1,2,'cv_child')"
            )
            con.execute(
                "INSERT INTO active_shell_chats (shell_id,chat_id) "
                "VALUES (1,'cv_child')"
            )
            con.commit()
        finally:
            con.close()
        compared = [
            "conversations",
            "sprint_participants",
            "sprint_participant_conversations",
        ]
        before = rows_by_table(self.db, compared)

        self.snapshot_and_rebuild()

        self.assertEqual(before, rows_by_table(self.db, compared))

    def test_paused_and_terminal_lifecycle_rows_replay_exactly(self) -> None:
        con = sqlite3.connect(self.db)
        try:
            seed_prepared(con)
            con.executemany(
                "INSERT INTO sprints "
                "(sprint_id,feature_id,originating_planner_shell_id,"
                "merge_grant_enabled,created_at,updated_at) "
                "VALUES (?,19,3,?, ?, ?)",
                (
                    (2, 1, "2026-08-01 20:00:00", "2026-08-01 20:03:00"),
                    (3, 1, "2026-08-01 19:00:00", "2026-08-01 19:05:00"),
                    (4, 0, "2026-08-01 18:00:00", "2026-08-01 18:01:00"),
                ),
            )
            con.executemany(
                "INSERT INTO sprint_participants "
                "(sprint_id,shell_id,role,harness) VALUES (?,2,'reviewer','claude')",
                ((2,), (3,)),
            )
            con.execute(
                "UPDATE sprints SET conformance_reviewer_shell_id=2,"
                "conformance_owner_generation=1,lifecycle='armed',"
                "armed_at='2026-08-01 20:01:00' WHERE sprint_id=2"
            )
            con.execute(
                "UPDATE sprints SET lifecycle='paused',paused_at='2026-08-01 20:03:00' "
                "WHERE sprint_id=2"
            )
            con.execute(
                "UPDATE sprints SET conformance_reviewer_shell_id=2,"
                "conformance_owner_generation=1,lifecycle='armed',"
                "armed_at='2026-08-01 19:01:00' WHERE sprint_id=3"
            )
            con.execute(
                "UPDATE sprints SET lifecycle='completed',"
                "terminal_outcome='shipped',completed_at='2026-08-01 19:05:00' "
                "WHERE sprint_id=3"
            )
            con.execute(
                "UPDATE sprints SET lifecycle='aborted',terminal_outcome='cancelled',"
                "aborted_at='2026-08-01 18:01:00' WHERE sprint_id=4"
            )
            con.commit()
        finally:
            con.close()
        before = rows_by_table(self.db, ["sprints"])

        self.snapshot_and_rebuild()

        self.assertEqual(before, rows_by_table(self.db, ["sprints"]))

    def test_older_resumed_sprint_replays_after_newer_paused_row(self) -> None:
        con = sqlite3.connect(self.db)
        try:
            seed_prepared(con)
            con.executemany(
                "INSERT INTO sprints "
                "(sprint_id,feature_id,originating_planner_shell_id,"
                "merge_grant_enabled,created_at,updated_at) "
                "VALUES (?,19,3,1,?,?)",
                (
                    (2, "2026-08-01 19:00:00", "2026-08-01 20:04:00"),
                    (3, "2026-08-01 20:00:00", "2026-08-01 20:03:00"),
                ),
            )
            con.executemany(
                "INSERT INTO sprint_participants "
                "(sprint_id,shell_id,role,harness) VALUES (?,2,'reviewer','claude')",
                ((2,), (3,)),
            )
            con.execute(
                "UPDATE sprints SET conformance_reviewer_shell_id=2,"
                "conformance_owner_generation=1,lifecycle='armed' WHERE sprint_id=2"
            )
            con.execute("UPDATE sprints SET lifecycle='paused' WHERE sprint_id=2")
            con.execute(
                "UPDATE sprints SET conformance_reviewer_shell_id=2,"
                "conformance_owner_generation=1,lifecycle='armed' WHERE sprint_id=3"
            )
            con.execute("UPDATE sprints SET lifecycle='paused' WHERE sprint_id=3")
            con.execute("UPDATE sprints SET lifecycle='armed' WHERE sprint_id=2")
            con.commit()
        finally:
            con.close()
        before = rows_by_table(self.db, ["sprints"])

        self.snapshot_and_rebuild()

        self.assertEqual(before, rows_by_table(self.db, ["sprints"]))

    def test_snapshot_authority_names_every_persistent_sprint_table(self) -> None:
        con = sqlite3.connect(self.db)
        try:
            actual = {
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND (name IN ('sprints','wake_message','pr_subscriptions') "
                    "OR name LIKE 'sprint_%' OR name LIKE 'pr_subscription_%')"
                )
            }
        finally:
            con.close()
        self.assertEqual(actual, set(snapshot.SPRINT_INSTANCE_TABLES))
        self.assertEqual(
            len(snapshot.SPRINT_INSTANCE_TABLES),
            len(set(snapshot.SPRINT_INSTANCE_TABLES)),
        )
        conversation_index = snapshot.PER_INSTANCE_TABLES.index("conversations")
        link_index = snapshot.PER_INSTANCE_TABLES.index(
            "sprint_participant_conversations"
        )
        self.assertLess(conversation_index, link_index)

    def test_snapshot_uses_one_read_view_across_sprint_tables(self) -> None:
        con = sqlite3.connect(self.db)
        try:
            seed_prepared(con)
        finally:
            con.close()
        reader = db_driver.connect(self.db)
        writer = sqlite3.connect(self.db)
        writer.execute("PRAGMA journal_mode=WAL").fetchone()
        original = snapshot.dump_table
        mutated = False

        def mutate_between_tables(connection, table: str):
            nonlocal mutated
            if table == "sprints" and not mutated:
                writer.execute(
                    "INSERT INTO sprint_events "
                    "(sprint_id,event_type,actor_kind,payload) "
                    "VALUES (1,'committed.after.snapshot','system','{}')"
                )
                writer.commit()
                mutated = True
            return original(connection, table)

        try:
            with mock.patch.object(snapshot, "dump_table", side_effect=mutate_between_tables):
                content = snapshot.serialize_instance(reader)
        finally:
            writer.close()
            reader.close()
        self.assertTrue(mutated)
        self.assertIn("sprint.declared", content)
        self.assertNotIn("committed.after.snapshot", content)
        self.assertEqual(
            "committed.after.snapshot",
            rows_by_table(self.db, ["sprint_events"])["sprint_events"][-1][2],
        )


if __name__ == "__main__":
    unittest.main()
