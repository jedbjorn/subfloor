#!/usr/bin/env python3
"""Sprint 29 WU130: transactional DSH-owned database graph purge."""
from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder/scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests"))

import migrate
import test_dsh_removal_preparation as preparation

MIGRATION = ROOT / ".super-coder/migrations/0237_purge_dsh_owned_data.sql"
RESTORED_TRIGGERS = (
    "sprint_participant_route_bindings_immutable_delete",
    "trg_conversation_events_append_only_delete",
    "trg_pr_subscription_poll_failures_append_only_delete",
    "trg_pr_subscription_transitions_append_only_delete",
    "trg_sprint_cleanup_no_delete",
    "trg_sprint_cleanup_requests_append_only_delete",
    "trg_sprint_events_append_only_delete",
    "trg_sprint_followups_no_delete",
    "trg_sprint_judgments_append_only_delete",
    "trg_sprint_liveness_no_delete",
    "trg_sprint_participant_conversations_immutable_delete",
    "trg_sprint_pr_transitions_append_only_delete",
    "trg_sprint_reports_append_only_delete",
    "trg_sprints_conformance_owner_generation",
    "trg_sprints_conformance_owner_reassignment_paused",
)


def rows(con: sqlite3.Connection, query: str, params: tuple = ()) -> list[tuple]:
    return [tuple(row) for row in con.execute(query, params)]


def typed_absence(con: sqlite3.Connection) -> tuple[int, ...]:
    return tuple(
        con.execute(query).fetchone()[0]
        for query in (
            "SELECT COUNT(*) FROM flavor_defaults WHERE harness='deepseek'",
            "SELECT COUNT(*) FROM model_routes WHERE harness='deepseek'",
            "SELECT COUNT(*) FROM analytics_parse_cache WHERE harness='deepseek'",
            "SELECT COUNT(*) FROM session_token_usage WHERE harness='deepseek'",
            "SELECT COUNT(*) FROM shell_memory_archives WHERE harness='deepseek'",
            "SELECT COUNT(*) FROM shell_launch_records WHERE harness='deepseek'",
            "SELECT COUNT(*) FROM conversations WHERE harness='deepseek'",
            "SELECT COUNT(*) FROM sprint_participants WHERE harness='deepseek'",
            (
                "SELECT COUNT(*) FROM sprint_participant_route_bindings "
                "WHERE harness='deepseek'"
            ),
        )
    )


def protected_trigger_sql(con: sqlite3.Connection) -> list[tuple]:
    placeholders = ",".join("?" for _ in RESTORED_TRIGGERS)
    return rows(
        con,
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
        f"AND name IN ({placeholders}) ORDER BY name",
        RESTORED_TRIGGERS,
    )


def fresh_historical_replay() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript((ROOT / ".super-coder/schema.sql").read_text())
    manifest = preparation.load(preparation.MANIFEST_PATH)
    for row in manifest["immutable_migration_ledger"]:
        path = preparation.FIXTURES.frozen_source_path(row["path"])
        if preparation.FIXTURES.sha256_file(path) != row["sha256"]:
            con.close()
            raise AssertionError(f"immutable migration changed: {row['filename']}")
        con.executescript(path.read_text())
    return con


def seed_extended_graph(con: sqlite3.Connection) -> None:
    boot = "DSH-owned boot transcript"
    con.execute(
        "INSERT INTO conversation_boot_snapshots "
        "(conversation_id,content,content_sha256,content_bytes,format_version,"
        "binding_origin) VALUES (?,?,?,?,1,'new_conversation')",
        (
            "cv_dsh_fixture",
            boot,
            hashlib.sha256(boot.encode()).hexdigest(),
            len(boot.encode()),
        ),
    )
    con.execute(
        "INSERT INTO conversation_events "
        "(event_id,conversation_id,sequence,event_type,payload,message_id,run_id) "
        "VALUES (9101,'cv_dsh_fixture',1,'output','{\"owner\":\"dsh\"}',9001,9001)"
    )
    con.execute(
        "INSERT INTO conversation_git_targets "
        "(target_id,conversation_id,branch_name,first_head_sha,latest_head_sha) "
        "VALUES (?,?,?,?,?)",
        ("gt_" + "1" * 32, "cv_dsh_fixture", "feat/dsh", "1" * 40, "2" * 40),
    )
    con.execute(
        "INSERT INTO conversations "
        "(conversation_id,shell_id,owner_user_id,harness,provider,model,worktree,"
        "state,closed_at,title,creation_idempotency_key,creation_request_hash,"
        "conversation_scope,route_contract_version,route_binding) "
        "SELECT 'cv_dsh_sprint',shell_id,owner_user_id,harness,provider,model,"
        "worktree,'closed','2026-08-28 00:00:00','DSH Sprint transcript',"
        "'dsh-sprint-fixture','s'||substr(creation_request_hash,2),"
        "'sprint',route_contract_version,route_binding "
        "FROM conversations WHERE conversation_id='cv_dsh_fixture'"
    )
    con.execute(
        "INSERT INTO sprint_participant_conversations "
        "(participant_conversation_id,sprint_participant_id,conversation_id) "
        "VALUES (9101,9001,'cv_dsh_sprint')"
    )
    con.execute(
        "INSERT INTO shell_memory_archives "
        "(archive_id,shell_id,date,full_narrative,harness,provider,model) "
        "VALUES (9101,9001,'2026-08-28','DSH transcript','deepseek',"
        "'deepseek-official','deepseek-chat')"
    )
    con.execute(
        "INSERT INTO session_token_usage "
        "(usage_id,archive_id,shell_id,harness,harness_session_ref,provider,model,"
        "input_tokens,output_tokens) VALUES "
        "(9101,9101,9001,'deepseek','sc-dsh-usage','deepseek-official',"
        "'deepseek-chat',10,20)"
    )

    con.execute(
        "INSERT INTO documents "
        "(document_id,feature_id,kind,seq,title,body) "
        "VALUES (9001,9001,'spec',1,'DSH mixed plan','Purge DSH rows')"
    )
    planning_body = "Purge DSH rows"
    con.execute(
        "INSERT INTO sprint_specs "
        "(sprint_id,document_id,bound_revision_sha256,bound_revision_body,"
        "bound_revision_legacy) VALUES (9001,9001,?,?,0)",
        (hashlib.sha256(planning_body.encode()).hexdigest(), planning_body),
    )
    con.execute(
        "INSERT INTO spec_tasks "
        "(task_id,feature_id,document_id,seq,title,description,shell_id) "
        "VALUES (9001,9001,9001,1,'Delete DSH delivery','Typed purge',9001)"
    )
    con.execute(
        "INSERT INTO shell_decisions "
        "(decision_id,shell_id,decision_date,decision,rationale,feature_id,document_id) "
        "VALUES (9001,9003,'2026-08-28','Use DSH fixture','Typed owner',9001,9001)"
    )
    con.execute(
        "INSERT INTO flags "
        "(flag_id,display_name,description,shell_id,feature_id) "
        "VALUES (9001,'DSH-fixture','DSH delivery pending',9003,9001)"
    )
    con.execute(
        "INSERT INTO sprint_work_units "
        "(work_unit_id,sprint_id,assigned_shell_id,reviewer_shell_id,title,"
        "expected_output,planned_wave,disposition) VALUES "
        "(9101,9001,9001,9002,'Purge DSH graph','Delete DSH rows',1,'planned')"
    )
    con.execute(
        "INSERT INTO sprint_work_units "
        "(work_unit_id,sprint_id,assigned_shell_id,reviewer_shell_id,title,"
        "expected_output,planned_wave,disposition) VALUES "
        "(9201,9001,9002,9002,'Document replaced DSH participant',"
        "'Retain the mixed Sprint record exactly',2,'planned')"
    )
    con.execute(
        "INSERT INTO sprint_work_unit_tasks (sprint_id,work_unit_id,task_id) "
        "VALUES (9001,9101,9001)"
    )
    con.execute(
        "INSERT INTO sprint_work_unit_tasks (sprint_id,work_unit_id,task_id) "
        "VALUES (9001,9201,9001)"
    )
    con.execute(
        "INSERT INTO wake_message "
        "(message_id,sprint_id,sender_shell_id,receiver_shell_id,"
        "from_participant_id,to_participant_id,work_unit_id,message_kind,body,"
        "declared_type,actionable,disposition,idempotency_key) VALUES "
        "(9101,9001,9001,9002,9001,9002,9101,'work_assignment',"
        "'DSH assignment','force-new',1,'pending','dsh-assignment')"
    )
    con.execute(
        "INSERT INTO wake_message "
        "(message_id,sprint_id,sender_shell_id,receiver_shell_id,"
        "from_participant_id,to_participant_id,message_kind,body,declared_type,"
        "actionable,idempotency_key,reply_to_message_id) VALUES "
        "(9102,9001,9002,9001,9002,9001,'notification','reply',"
        "'re-enter',0,'dsh-reply',9101)"
    )
    con.execute(
        "INSERT INTO sprint_wake_outbox "
        "(wake_id,sprint_id,participant_id,receiver_shell_id,state,attempt_count,"
        "idempotency_key,delivered_at) VALUES "
        "(9101,9001,9001,9001,'delivered',1,'dsh-wake','2026-08-28 00:00:00')"
    )
    con.execute(
        "INSERT INTO sprint_wake_messages (sprint_id,wake_id,message_id) "
        "VALUES (9001,9101,9101)"
    )
    con.execute(
        "INSERT INTO sprint_wake_attempts "
        "(attempt_id,wake_id,attempt_number,target_conversation_id,outcome) "
        "VALUES (9101,9101,1,'cv_dsh_sprint','delivered')"
    )
    con.execute(
        "INSERT INTO sprint_liveness_expectations "
        "(message_id,sprint_id,participant_id,accepted_at,last_strong_at,"
        "last_strong_key) VALUES "
        "(9101,9001,9001,'2026-08-28 00:00:00','2026-08-28 00:00:00','accept')"
    )
    con.execute(
        "INSERT INTO sprint_events "
        "(event_id,sprint_id,event_type,actor_kind,actor_shell_id,payload) "
        "VALUES (9101,9001,'assignment','participant',9001,"
        "'{\"participant_id\":9001,\"work_unit_id\":9101,\"harness\":\"dsh\"}')"
    )
    con.execute(
        "INSERT INTO sprint_judgments "
        "(judgment_id,sprint_id,participant_id,work_unit_id,kind,body) "
        "VALUES (9101,9001,9001,9101,'decision','DSH-owned judgment')"
    )
    con.execute(
        "INSERT INTO sprint_reports "
        "(report_id,sprint_id,report_kind,author_shell_id,body,idempotency_key) "
        "VALUES (9101,9001,'pause',9001,'DSH-owned report','dsh-report')"
    )
    con.execute(
        "INSERT INTO sprint_followups "
        "(followup_id,sprint_id,source_report_id,severity,title,body,"
        "spec_document_id,work_unit_id,idempotency_key) VALUES "
        "(9101,9001,9101,'High','DSH follow-up','Remove DSH',9001,9101,"
        "'dsh-followup')"
    )
    con.execute(
        "INSERT INTO sprint_registered_prs "
        "(registered_pr_id,sprint_id,owner_participant_id,repository,pr_number) "
        "VALUES (9101,9001,9001,'example/dsh',91)"
    )
    con.execute(
        "INSERT INTO sprint_pr_work_units "
        "(sprint_id,registered_pr_id,work_unit_id) VALUES (9001,9101,9101)"
    )
    con.execute(
        "INSERT INTO sprint_pr_transitions "
        "(transition_id,registered_pr_id,normalized_state,transition_key) "
        "VALUES (9101,9101,'created','dsh-pr-created')"
    )
    con.execute(
        "INSERT INTO pr_subscriptions "
        "(subscription_id,owner_shell_id,repository,pr_number,"
        "sprint_registered_pr_id) VALUES (9101,9001,'example/dsh',91,9101)"
    )
    con.execute(
        "INSERT INTO pr_subscription_transitions "
        "(transition_id,subscription_id,normalized_state,transition_key) "
        "VALUES (9101,9101,'created','dsh-sub-created')"
    )
    con.execute(
        "INSERT INTO pr_subscription_poll_failures "
        "(failure_id,subscription_id,failure_count,backoff_seconds,trigger,"
        "error_detail) VALUES (9101,9101,1,1.0,'dsh-test','failure')"
    )
    con.execute(
        "INSERT INTO sprints "
        "(sprint_id,feature_id,originating_planner_shell_id,merge_grant_enabled) "
        "VALUES (9002,9001,9003,1)"
    )
    con.execute(
        "INSERT INTO sprint_participants "
        "(participant_id,sprint_id,shell_id,role,harness,model,disposition) VALUES "
        "(9004,9002,9001,'developer','deepseek','deepseek-chat','assigned'),"
        "(9005,9002,9002,'reviewer','opencode','deepseek-v4-pro','assigned')"
    )
    con.execute(
        "UPDATE sprints SET conformance_reviewer_shell_id=9002,"
        "conformance_owner_generation=1 WHERE sprint_id=9002"
    )
    con.execute(
        "UPDATE sprints SET lifecycle='armed',armed_at='2026-08-28 00:00:00' "
        "WHERE sprint_id=9002"
    )
    con.execute(
        "UPDATE sprints SET lifecycle='completed',terminal_outcome='complete',"
        "completed_at='2026-08-28 00:00:01' WHERE sprint_id=9002"
    )
    con.execute(
        "INSERT INTO sprint_cleanup_targets "
        "(cleanup_target_id,sprint_id,shell_id,target_kind,canonical_path,"
        "repository_root,git_common_dir,expected_base_branch) VALUES "
        "(9101,9002,9001,'worktree','/fixture/dsh','/fixture','/fixture/.git',"
        "'shell/dshf')"
    )
    con.execute(
        "INSERT INTO sprint_cleanup_requests "
        "(cleanup_request_id,sprint_id,caller_shell_id,request_kind,"
        "idempotency_key,request_hash,response_json) VALUES "
        "(9101,9002,9001,'requeued','dsh-cleanup','" + "1" * 64 + "','{}')"
    )

    con.execute(
        "INSERT INTO wake_message "
        "(message_id,sprint_id,sender_shell_id,receiver_shell_id,"
        "from_participant_id,to_participant_id,message_kind,body,declared_type,"
        "actionable,idempotency_key) VALUES "
        "(9201,9001,9002,9002,9002,9002,'notification','retained OpenCode',"
        "'re-enter',0,'opencode-message')"
    )
    con.execute(
        "INSERT INTO sprint_wake_outbox "
        "(wake_id,sprint_id,participant_id,receiver_shell_id,state,attempt_count,"
        "idempotency_key,delivered_at) VALUES "
        "(9201,9001,9002,9002,'delivered',1,'opencode-wake',"
        "'2026-08-28 00:00:00')"
    )
    con.execute(
        "INSERT INTO sprint_wake_messages (sprint_id,wake_id,message_id) "
        "VALUES (9001,9201,9201)"
    )
    con.execute(
        "INSERT INTO sprint_wake_attempts "
        "(attempt_id,wake_id,attempt_number,target_conversation_id,outcome) "
        "VALUES (9201,9201,1,'cv_opencode_deepseek_fixture','delivered')"
    )
    con.execute(
        "INSERT INTO sprint_wake_recovery_messages "
        "(recovery_event_id,sprint_id,prior_wake_id,replacement_wake_id,message_id) "
        "VALUES (9101,9001,9101,9201,9101)"
    )
    con.execute(
        "INSERT INTO sprint_liveness_expectations "
        "(message_id,sprint_id,participant_id,accepted_at,last_strong_at,"
        "last_strong_key) VALUES "
        "(9201,9001,9002,'2026-08-28 00:00:00','2026-08-28 00:00:00','accept')"
    )
    con.execute(
        "INSERT INTO sprint_events "
        "(event_id,sprint_id,event_type,actor_kind,actor_shell_id,payload) "
        "VALUES (9201,9001,'retained','participant',9002,"
        "'{\"participant_id\":9002,\"harness\":\"opencode\","
        "\"note\":\"DSH participant was replaced\"}')"
    )
    con.execute(
        "INSERT INTO sprint_reports "
        "(report_id,sprint_id,report_kind,author_shell_id,body,idempotency_key) "
        "VALUES (9201,9001,'final',9002,"
        "'Retained report: the DSH participant was replaced',"
        "'opencode-report')"
    )
    con.execute(
        "INSERT INTO sprint_judgments "
        "(judgment_id,sprint_id,participant_id,work_unit_id,kind,body) "
        "VALUES (9201,9001,9002,9201,'decision',"
        "'Retain this DSH replacement note')"
    )
    con.execute(
        "INSERT INTO sprint_followups "
        "(followup_id,sprint_id,source_report_id,severity,title,body,"
        "spec_document_id,work_unit_id,idempotency_key) VALUES "
        "(9201,9001,9201,'Low','Retained DSH note',"
        "'Document the retained replacement',9001,9201,'opencode-followup')"
    )
    con.commit()


class DshDataPurgeTest(unittest.TestCase):
    def test_governing_digest_purges_full_sprint_with_retained_harness_route(self) -> None:
        fixture = preparation.load(preparation.COMPATIBILITY_PATH)
        with closing(preparation.replay_database(fixture)) as con:
            con.execute(
                "INSERT INTO roadmap (feature_id,title,roadmap_status,owning_shell) "
                "VALUES (9100,'Temporary removal control','in_progress',9003)"
            )
            con.execute(
                "INSERT INTO documents (document_id,feature_id,kind,seq,title,body) "
                "VALUES (178,9100,'spec',1,'Removal control','control')"
            )
            con.execute(
                "INSERT INTO sprints "
                "(sprint_id,feature_id,originating_planner_shell_id) "
                "VALUES (9100,9100,9003)"
            )
            con.execute(
                "INSERT INTO sprint_specs "
                "(sprint_id,document_id,bound_revision_sha256,"
                "bound_revision_body,bound_revision_legacy) VALUES "
                "(9100,178,'be111eb206b9e6ea09352a90346e8a94544ac1ef3bd932716e4bd90451d33c42',"
                "'control',0)"
            )
            con.execute(
                "INSERT INTO sprint_participants "
                "(participant_id,sprint_id,shell_id,role,harness,model,disposition) "
                "VALUES (9301,9100,9002,'reviewer','opencode',"
                "'ollama-cloud/deepseek-v4-pro','assigned')"
            )
            con.execute(
                "INSERT INTO sprint_participant_route_bindings "
                "SELECT 9301,9301,route_revision,contract_version,control_state,"
                "harness,requested_model,provider_model,requested_effort,"
                "effective_effort,native_variant_id,native_option_id,transport,"
                "catalogue_generation,evidence_digest,selector_binding,"
                "adapter_metadata,binding_json,binding_digest,created_at,"
                "source_fingerprint,harness_version,harness_evidence_format,"
                "NULL FROM sprint_participant_route_bindings "
                "WHERE binding_id=9002"
            )
            con.execute(
                "UPDATE sprint_participants SET active_route_binding_id=9301 "
                "WHERE participant_id=9301"
            )
            con.execute(
                "INSERT INTO conversations "
                "(conversation_id,shell_id,owner_user_id,harness,provider,model,"
                "worktree,state,closed_at,title,creation_idempotency_key,"
                "creation_request_hash,conversation_scope,route_contract_version,"
                "route_binding) SELECT 'cv_control_opencode',shell_id,owner_user_id,"
                "harness,provider,model,worktree,'closed','2026-08-28 00:00:00',"
                "'control','control-opencode','c'||substr(creation_request_hash,2),"
                "'sprint',route_contract_version,route_binding FROM conversations "
                "WHERE conversation_id='cv_opencode_deepseek_fixture'"
            )
            con.execute(
                "INSERT INTO sprint_participant_conversations "
                "(participant_conversation_id,sprint_participant_id,conversation_id) "
                "VALUES (9301,9301,'cv_control_opencode')"
            )
            con.commit()

            migrate.apply(con, MIGRATION, dsh_purge_authorized=True)

            for table, column, identity in (
                ("roadmap", "feature_id", 9100),
                ("documents", "document_id", 178),
                ("sprints", "sprint_id", 9100),
                ("sprint_participants", "participant_id", 9301),
                ("sprint_participant_route_bindings", "binding_id", 9301),
                ("sprint_participant_conversations", "participant_conversation_id", 9301),
                ("sprint_specs", "sprint_id", 9100),
            ):
                self.assertEqual(
                    0,
                    con.execute(
                        f'SELECT COUNT(*) FROM "{table}" WHERE "{column}"=?',
                        (identity,),
                    ).fetchone()[0],
                    table,
                )
            self.assertEqual(
                [(9002, "opencode")],
                rows(
                    con,
                    "SELECT participant_id,harness FROM sprint_participants "
                    "WHERE participant_id=9002",
                ),
            )
            self.assertEqual([], con.execute("PRAGMA foreign_key_check").fetchall())

    def test_installed_bridge_purges_full_graph_and_preserves_siblings(self) -> None:
        fixture = preparation.load(preparation.COMPATIBILITY_PATH)
        with closing(preparation.replay_database(fixture)) as con:
            seed_extended_graph(con)
            protected_triggers = protected_trigger_sql(con)
            retained = {
                "routes": rows(con, "SELECT * FROM model_routes WHERE harness='opencode'"),
                "cache": rows(
                    con, "SELECT * FROM analytics_parse_cache WHERE harness='opencode'"
                ),
                "conversation": rows(
                    con,
                    "SELECT * FROM conversations "
                    "WHERE conversation_id='cv_opencode_deepseek_fixture'",
                ),
                "message": rows(
                    con, "SELECT * FROM conversation_messages WHERE message_id=9002"
                ),
                "participant": rows(
                    con, "SELECT * FROM sprint_participants WHERE participant_id=9002"
                ),
                "binding": rows(
                    con,
                    "SELECT * FROM sprint_participant_route_bindings "
                    "WHERE binding_id=9002",
                ),
                "delivery_message": rows(
                    con, "SELECT * FROM wake_message WHERE message_id=9201"
                ),
                "delivery_wake": rows(
                    con, "SELECT * FROM sprint_wake_outbox WHERE wake_id=9201"
                ),
                "delivery_attempt": rows(
                    con, "SELECT * FROM sprint_wake_attempts WHERE attempt_id=9201"
                ),
                "event": rows(con, "SELECT * FROM sprint_events WHERE event_id=9201"),
                "report": rows(con, "SELECT * FROM sprint_reports WHERE report_id=9201"),
                "roadmap": rows(con, "SELECT * FROM roadmap WHERE feature_id=9001"),
                "document": rows(con, "SELECT * FROM documents WHERE document_id=9001"),
                "task": rows(con, "SELECT * FROM spec_tasks WHERE task_id=9001"),
                "decision": rows(
                    con, "SELECT * FROM shell_decisions WHERE decision_id=9001"
                ),
                "flag": rows(con, "SELECT * FROM flags WHERE flag_id=9001"),
                "work_unit": rows(
                    con, "SELECT * FROM sprint_work_units WHERE work_unit_id=9201"
                ),
                "work_unit_task": rows(
                    con,
                    "SELECT * FROM sprint_work_unit_tasks WHERE work_unit_id=9201",
                ),
                "sprint_spec": rows(
                    con,
                    "SELECT * FROM sprint_specs "
                    "WHERE sprint_id=9001 AND document_id=9001",
                ),
                "judgment": rows(
                    con, "SELECT * FROM sprint_judgments WHERE judgment_id=9201"
                ),
                "followup": rows(
                    con, "SELECT * FROM sprint_followups WHERE followup_id=9201"
                ),
            }

            migrate.apply(con, MIGRATION, dsh_purge_authorized=True)

            self.assertEqual((0,) * 9, typed_absence(con))
            for table, column, identity in (
                ("conversation_messages", "message_id", 9001),
                ("conversation_events", "event_id", 9101),
                ("conversation_git_targets", "target_id", "gt_" + "1" * 32),
                ("conversation_boot_snapshots", "conversation_id", "cv_dsh_fixture"),
                ("conversation_runs", "run_id", 9001),
                ("conversation_outbox", "outbox_id", 9001),
                ("active_shell_chats", "shell_id", 9001),
                ("sprint_participant_conversations", "participant_conversation_id", 9101),
                ("session_token_usage", "usage_id", 9101),
                ("sprint_work_units", "work_unit_id", 9101),
                ("wake_message", "message_id", 9101),
                ("wake_message", "message_id", 9102),
                ("sprint_wake_outbox", "wake_id", 9101),
                ("sprint_wake_messages", "message_id", 9101),
                ("sprint_wake_recovery_messages", "recovery_event_id", 9101),
                ("sprint_liveness_expectations", "message_id", 9101),
                ("sprint_events", "event_id", 9101),
                ("sprint_judgments", "judgment_id", 9101),
                ("sprint_reports", "report_id", 9101),
                ("sprint_followups", "followup_id", 9101),
                ("sprint_registered_prs", "registered_pr_id", 9101),
                ("sprint_pr_transitions", "transition_id", 9101),
                ("sprint_pr_work_units", "registered_pr_id", 9101),
                ("pr_subscriptions", "subscription_id", 9101),
                ("sprint_cleanup_targets", "cleanup_target_id", 9101),
                ("sprint_cleanup_requests", "cleanup_request_id", 9101),
                ("model_catalog_generations", "generation_id", "d" * 32),
            ):
                self.assertEqual(
                    0,
                    con.execute(
                        f'SELECT COUNT(*) FROM "{table}" WHERE "{column}"=?',
                        (identity,),
                    ).fetchone()[0],
                    table,
                )

            for name, before in retained.items():
                query = {
                    "routes": "SELECT * FROM model_routes WHERE harness='opencode'",
                    "cache": "SELECT * FROM analytics_parse_cache WHERE harness='opencode'",
                    "conversation": "SELECT * FROM conversations WHERE conversation_id='cv_opencode_deepseek_fixture'",
                    "message": "SELECT * FROM conversation_messages WHERE message_id=9002",
                    "participant": "SELECT * FROM sprint_participants WHERE participant_id=9002",
                    "binding": "SELECT * FROM sprint_participant_route_bindings WHERE binding_id=9002",
                    "delivery_message": "SELECT * FROM wake_message WHERE message_id=9201",
                    "delivery_wake": "SELECT * FROM sprint_wake_outbox WHERE wake_id=9201",
                    "delivery_attempt": "SELECT * FROM sprint_wake_attempts WHERE attempt_id=9201",
                    "event": "SELECT * FROM sprint_events WHERE event_id=9201",
                    "report": "SELECT * FROM sprint_reports WHERE report_id=9201",
                    "roadmap": "SELECT * FROM roadmap WHERE feature_id=9001",
                    "document": "SELECT * FROM documents WHERE document_id=9001",
                    "task": "SELECT * FROM spec_tasks WHERE task_id=9001",
                    "decision": "SELECT * FROM shell_decisions WHERE decision_id=9001",
                    "flag": "SELECT * FROM flags WHERE flag_id=9001",
                    "work_unit": "SELECT * FROM sprint_work_units WHERE work_unit_id=9201",
                    "work_unit_task": "SELECT * FROM sprint_work_unit_tasks WHERE work_unit_id=9201",
                    "sprint_spec": "SELECT * FROM sprint_specs WHERE sprint_id=9001 AND document_id=9001",
                    "judgment": "SELECT * FROM sprint_judgments WHERE judgment_id=9201",
                    "followup": "SELECT * FROM sprint_followups WHERE followup_id=9201",
                }[name]
                self.assertEqual(before, rows(con, query), name)
            self.assertEqual([], con.execute("PRAGMA foreign_key_check").fetchall())
            self.assertEqual(protected_triggers, protected_trigger_sql(con))
            self.assertIn(
                MIGRATION.name,
                {row[0] for row in con.execute("SELECT filename FROM schema_migrations")},
            )

    def test_dirty_fixture_is_idempotent(self) -> None:
        fixture = preparation.load(preparation.COMPATIBILITY_PATH)
        with closing(preparation.replay_database(fixture)) as con:
            seed_extended_graph(con)
            con.executescript(MIGRATION.read_text())
            after_once = {
                table: rows(con, f'SELECT * FROM "{table}" ORDER BY rowid')
                for table in (
                    "flavor_defaults",
                    "model_routes",
                    "conversations",
                    "sprint_participants",
                    "wake_message",
                    "sprint_wake_outbox",
                    "roadmap",
                )
            }
            con.executescript(MIGRATION.read_text())
            self.assertEqual(
                after_once,
                {
                    table: rows(con, f'SELECT * FROM "{table}" ORDER BY rowid')
                    for table in after_once
                },
            )
            self.assertEqual([], con.execute("PRAGMA foreign_key_check").fetchall())

    def test_fresh_replay_and_installed_bridge_converge_on_absence(self) -> None:
        fixture = preparation.load(preparation.COMPATIBILITY_PATH)
        with closing(fresh_historical_replay()) as fresh, closing(
            preparation.replay_database(fixture)
        ) as bridge:
            migrate.apply(fresh, MIGRATION, dsh_purge_authorized=True)
            migrate.apply(bridge, MIGRATION, dsh_purge_authorized=True)

            self.assertEqual((0,) * 9, typed_absence(fresh))
            self.assertEqual(typed_absence(fresh), typed_absence(bridge))
            self.assertEqual([], fresh.execute("PRAGMA foreign_key_check").fetchall())
            self.assertEqual([], bridge.execute("PRAGMA foreign_key_check").fetchall())
            self.assertEqual(
                rows(
                    bridge,
                    "SELECT harness,selector,provider,provider_model FROM model_routes "
                    "WHERE harness='opencode'",
                ),
                [
                    (
                        "opencode",
                        "ollama-cloud/deepseek-v4-pro",
                        "ollama-cloud",
                        "deepseek-v4-pro",
                    )
                ],
            )

    def test_injected_failure_rolls_back_without_ledger_stamp(self) -> None:
        fixture = preparation.load(preparation.COMPATIBILITY_PATH)
        with closing(preparation.replay_database(fixture)) as con, tempfile.TemporaryDirectory() as raw:
            seed_extended_graph(con)
            injected = Path(raw) / MIGRATION.name
            injected.write_text(
                MIGRATION.read_text().replace(
                    "-- dsh-purge-failure-injection",
                    "INSERT INTO __injected_dsh_purge_failure__ VALUES (1);",
                    1,
                )
            )

            with self.assertRaisesRegex(sqlite3.OperationalError, "no such table"):
                migrate.apply(con, injected, dsh_purge_authorized=True)

            self.assertEqual(
                [("cv_dsh_fixture",), ("cv_dsh_sprint",)],
                rows(
                    con,
                    "SELECT conversation_id FROM conversations "
                    "WHERE harness='deepseek' ORDER BY conversation_id",
                ),
            )
            self.assertEqual(
                [(9001,), (9004,)],
                rows(
                    con,
                    "SELECT participant_id FROM sprint_participants "
                    "WHERE harness='deepseek' ORDER BY participant_id",
                ),
            )
            self.assertNotIn(
                MIGRATION.name,
                {row[0] for row in con.execute("SELECT filename FROM schema_migrations")},
            )
            trigger = con.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND name='trg_conversation_events_append_only_delete'"
            ).fetchone()
            self.assertEqual(("trg_conversation_events_append_only_delete",), tuple(trigger))
            self.assertEqual([], con.execute("PRAGMA foreign_key_check").fetchall())


if __name__ == "__main__":
    unittest.main()
