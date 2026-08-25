"""Freeze the Sprint v1 cutover database used by removal migration tests.

This generator intentionally reads the pre-removal schema and migrations.  The
generated SQL is the durable fixture: later removal units may delete or rewrite
those inputs without changing the old database shape exercised at cutover.
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ENGINE = ROOT / ".super-coder"
OUTPUT = Path(__file__).with_name("pre_removal.sql")
MANIFEST_OUTPUT = Path(__file__).with_name("manifest.json")

SCAN_ROOTS = (
    "sc",
    ".super-coder",
    "tests",
    "docs",
    "reviews",
    "README.md",
    "Makefile",
    ".github",
)
ALLOWED_REFERENCE_FILES = (
    ".super-coder/adapters/claude/adapter.json",
    ".super-coder/adapters/codex/adapter.json",
    ".super-coder/adapters/deepseek/adapter.json",
    ".super-coder/assets/deepseek/dsh-shell-authority-contract.json",
    ".super-coder/adapters/kimi/adapter.json",
    ".super-coder/adapters/opencode/adapter.json",
    ".super-coder/adapters/vibe/adapter.json",
    ".super-coder/scripts/harness_surfaces.py",
    "tests/fixtures/sprint_removal/build_pre_removal_fixture.py",
    "tests/fixtures/sprint_removal/manifest.json",
    "tests/fixtures/sprint_removal/pre_removal.sql",
    "tests/test_dos_app_sprint_canary.py",
    "tests/test_deepseek_dsh_preparation.py",
    "tests/test_sprint_removal_manifest.py",
    "tests/test_harness_surfaces.py",
)
SOURCE_REFERENCE_PATTERN = (
    r"(?:sprint|conductor|SC_SPRINT_|"
    r"interface_(?:generations|sessions|writer_leases|input_state|"
    r"idempotency_keys|recovery_observations)|"
    r"planner_(?:wake_batches|wake_items|action_receipts|alerts)|"
    r"wake_machine_retirements|watched_prs|pr_poll_(?:runs|observations)|"
    r"spec_qaqc_reviews|directive_kinds|directives|sentinel_events|"
    r"unit_expectations|watch(?:\.py| daemon|er))"
)
REMOVED_TABLES = (
    "interface_generations",
    "interface_sessions",
    "interface_writer_leases",
    "interface_input_state",
    "interface_idempotency_keys",
    "interface_recovery_observations",
    "sprint_planner_bindings",
    "planner_wake_batches",
    "planner_wake_items",
    "planner_action_receipts",
    "planner_alerts",
    "wake_machine_retirements",
    "watched_prs",
    "pr_poll_runs",
    "pr_poll_observations",
    "sprint_units",
    "spec_qaqc_reviews",
    "sprints",
    "directive_kinds",
    "directives",
    "sentinel_events",
    "unit_expectations",
    "sprint_conversation_bindings",
    "sprint_assignment_results",
    "sprint_cancellations",
)


def build_database() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.executescript((ENGINE / "schema.sql").read_text())
    migrations = sorted((ENGINE / "migrations").glob("*.sql"))
    for migration in migrations:
        con.executescript(migration.read_text())

    trigger_sql = [
        (name, sql)
        for name, sql in con.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='trigger' ORDER BY name"
        )
    ]
    for name, _ in trigger_sql:
        con.execute(f'DROP TRIGGER "{name}"')

    con.execute("PRAGMA foreign_keys=OFF")
    tables = [
        row[0]
        for row in con.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    for table in tables:
        con.execute(f'DELETE FROM "{table}"')
    con.execute("DELETE FROM sqlite_sequence")
    con.executemany(
        "INSERT INTO schema_migrations (filename, applied_at) VALUES (?, ?)",
        (
            (migration.name, "2026-07-31 00:00:00")
            for migration in migrations
        ),
    )

    con.executescript(
        """
        INSERT INTO users (user_id, username, email, initials)
        VALUES (1, 'fixture-operator', 'fixture@example.test', 'FO');

        INSERT INTO projects
            (project_id, shortname, title, purpose, standing)
        VALUES
            (10, 'KEEP', 'Retained project', 'Cutover preservation marker',
             'Unrelated project data survives Sprint removal');

        INSERT INTO shells
            (shell_id, display_name, shortname, flavor, mandate, system_prompt,
             current_state, user_id, has_identity, bootstrapped)
        VALUES
            (10, 'Fixture Dev', 'DEVX', 'dev', 'Retained shell',
             'Retained prompt', 'normal work', 1, 1, 1),
            (11, 'Fixture Planner', 'PLNX', 'planner', 'Sprint planner',
             'Planner prompt', 'Sprint v1 work', 1, 1, 1),
            (12, 'Fixture Reviewer', 'REVX', 'reviewer', 'Sprint reviewer',
             'Reviewer prompt', 'Sprint v1 review', 1, 1, 1),
            (13, 'Fixture Conductor', 'CONX', 'conductor', 'Sprint conductor',
             'Conductor prompt', 'Sprint v1 orchestration', 1, 1, 1);

        INSERT INTO project_shells
            (project_shell_id, project_id, shell_id, role)
        VALUES (20, 10, 10, 'developer');

        INSERT INTO roadmap
            (feature_id, title, roadmap_status, owning_shell, summary, project_id)
        VALUES
            (20, 'Retained roadmap feature', 'in_progress', 10,
             'Unrelated roadmap data survives Sprint removal', 10);

        INSERT INTO documents
            (document_id, feature_id, kind, seq, title, body, render_path)
        VALUES
            (30, 20, 'spec', 1, 'Retained normal specification',
             '# Retained specification', 'specs_sc/retained.md'),
            (31, NULL, 'doc', 1, 'SPRINT: frozen markdown board',
             '# SPRINT: frozen markdown board\\n\\n| U1 | working |',
             'docs_sc/sprint-fixture.md'),
            (32, NULL, 'spec', 1, 'Sprint source specification',
             '# Sprint source specification', NULL);

        INSERT INTO shell_identity_entries
            (entry_id, shell_id, kind, entry_date, body)
        VALUES (40, 10, 'seed', '2026-07-31', 'Retained identity memory');

        INSERT INTO shell_decisions
            (decision_id, shell_id, decision_date, priority, decision, rationale,
             feature_id, document_id)
        VALUES
            (41, 10, '2026-07-31', 'M', 'Retained decision',
             'Independent of Sprint', 20, 30);

        INSERT INTO shell_memory_archives
            (archive_id, shell_id, session_id, date, full_narrative, harness,
             provider, model, sprint_ref)
        VALUES
            (42, 10, 'normal-session', '2026-07-31',
             'Retained normal archive', 'codex', 'openai', 'gpt-fixture', NULL),
            (43, 11, 'sprint-session', '2026-07-31',
             'Disposable Sprint archive marker', 'claude', 'anthropic',
             'conductor-fixture', 'SPRINT:31');

        INSERT INTO skills
            (skill_id, name, description, category, content, command, common)
        VALUES
            (50, 'retained_generic', 'Retained generic skill', 'engine',
             '# Generic skill', '--generic', 0),
            (51, 'sprint_dev', 'Disposable Sprint developer skill', 'sprint',
             '# Sprint developer', '--sprint-dev', 0);

        INSERT INTO shell_skills (shell_skill_id, shell_id, skill_id)
        VALUES (60, 10, 50), (61, 13, 51);

        INSERT INTO flavor_skills (flavor, skill_id)
        VALUES ('dev', 50), ('conductor', 51);

        INSERT INTO flavor_defaults
            (flavor, harness, model, is_default)
        VALUES
            ('dev', 'codex', 'gpt-fixture', 1),
            ('conductor', 'claude', 'conductor-fixture', 1);

        INSERT INTO model_routes
            (harness, selector, provider, provider_model, display_name, family,
             source, availability, headless_supported, high_effort_supported,
             default_effort, supported_efforts, cli_version, last_seen_at)
        VALUES
            ('codex', 'gpt-fixture', 'openai', 'gpt-fixture',
             'Retained model route', 'gpt', 'probe', 'available', 1, 1,
             'high', '["low","high"]', '1.0.0', '2026-07-31 00:00:00');

        INSERT INTO daemon_heartbeats (name, beat_at, interval_s)
        VALUES
            ('conversation-broker', '2026-07-31 00:00:00', 5),
            ('watch', '2026-07-31 00:00:00', 30);

        INSERT INTO shell_launch_records
            (shell_id, pid, start_ticks, worktree, harness)
        VALUES (10, 4242, 99, '/tmp/retained-worktree', 'codex');

        INSERT INTO shell_messages
            (message_id, from_shell_id, to_shell_id, body, kind, dedupe_key,
             sprint_doc_id)
        VALUES
            (100, 10, 11, 'Retained generic shell message', 'shell',
             'generic-shell', NULL),
            (101, 10, 11, 'Retained generic job result', 'result',
             'generic-job-result', NULL),
            (102, 11, 13, 'Disposable Sprint PR event', 'pr_event',
             'sprint-pr-event', 31),
            (103, 10, 11, 'Disposable Sprint assignment result', 'result',
             'sprint-assignment-result', 31);

        INSERT INTO conversations
            (conversation_id, shell_id, mode, owner_user_id, sprint_doc_id,
             harness, provider, model, effort, worktree, harness_session_ref,
             state, title, creation_idempotency_key, creation_request_hash)
        VALUES
            ('cv_normal', 10, 'normal', 1, NULL, 'codex', 'openai',
             'gpt-fixture', 'high', '/tmp/normal', 'session-normal', 'idle',
             'Retained normal conversation', 'create-normal', 'hash-normal'),
            ('cv_sprint_conductor', 13, 'sprint', NULL, 31, 'claude',
             'anthropic', 'conductor-fixture', 'high', '/tmp/conductor',
             'session-conductor', 'idle', 'Disposable Conductor conversation',
             'create-conductor', 'hash-conductor'),
            ('cv_sprint_dev', 10, 'sprint', NULL, 31, 'codex', 'openai',
             'gpt-fixture', 'high', '/tmp/sprint-dev', 'session-sprint-dev',
             'idle', 'Disposable Sprint developer conversation',
             'create-sprint-dev', 'hash-sprint-dev'),
            ('cv_sprint_planner', 11, 'sprint', NULL, 31, 'claude',
             'anthropic', 'planner-fixture', 'high', '/tmp/sprint-planner',
             'session-sprint-planner', 'idle',
             'Disposable Sprint planner conversation',
             'create-sprint-planner', 'hash-sprint-planner');

        INSERT INTO conversation_messages
            (message_id, conversation_id, sender_kind, sender_ref, message_kind,
             body, idempotency_key, request_hash, state, completed_at)
        VALUES
            (200, 'cv_normal', 'user', '1', 'prompt',
             'Retained normal prompt', 'normal-message', 'normal-message-hash',
             'completed', '2026-07-31 00:01:00'),
            (201, 'cv_sprint_dev', 'engine', 'sprint', 'notice',
             'Disposable Sprint assignment', 'sprint-message',
             'sprint-message-hash', 'accepted', NULL);

        INSERT INTO conversation_runs
            (run_id, conversation_id, shell_id, trigger_message_id, attempt,
             harness_session_before, harness_session_after, runner_ref, state,
             lease_owner, lease_expires_at, started_at, ended_at, exit_code,
             archive_id)
        VALUES
            (210, 'cv_normal', 10, 200, 1, 'before-normal', 'after-normal',
             'broker:fixture', 'succeeded', 'fixture-lease',
             '2026-07-31 00:02:00', '2026-07-31 00:00:10',
             '2026-07-31 00:00:20', 0, 42);

        INSERT INTO conversation_events
            (event_id, conversation_id, sequence, event_type, payload,
             message_id, run_id)
        VALUES
            (220, 'cv_normal', 1, 'assistant.delta',
             '{"text":"retained event"}', 200, 210),
            (221, 'cv_sprint_dev', 1, 'assignment.notice',
             '{"text":"disposable Sprint event"}', 201, NULL);

        INSERT INTO conversation_outbox
            (outbox_id, conversation_id, message_id, state, claim_owner,
             claimed_at, attempts, run_id, dispatched_at)
        VALUES
            (230, 'cv_normal', 200, 'dispatched', 'broker:fixture',
             '2026-07-31 00:00:05', 1, 210, '2026-07-31 00:00:06');

        INSERT INTO conversation_git_targets
            (target_id, conversation_id, branch_name, base_ref, first_head_sha,
             latest_head_sha, pr_number, pr_head_sha, pr_state, pr_url,
             pr_title, first_seen_at, last_seen_at, remote_refreshed_at)
        VALUES
            ('gt_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'cv_normal',
             'feat/retained-review', 'main',
             'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
             'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 900,
             'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'OPEN',
             'https://example.test/pull/900', 'Retained review target',
             '2026-07-31 00:00:00', '2026-07-31 00:01:00',
             '2026-07-31 00:01:00');

        INSERT INTO spec_qaqc_reviews
            (review_id, spec_doc_id, reviewer_shell_id, body_sha256, verdict)
        VALUES
            (300, 32, 12,
             'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
             'approved');

        INSERT INTO sprints
            (sprint_doc_id, spec_doc_id, planner_shell_id, qaqc_review_id,
             planner_route, dev_route, reviewer_route, state, legacy)
        VALUES
            (31, 32, 11, 300, 'claude:planner', 'codex:dev',
             'codex:reviewer', 'active', 0);

        INSERT INTO sprint_units
            (unit_id, sprint_doc_id, seq, unit_title, dev_shell_id,
             reviewer_shell_id, state, branch, pr_number, assigned_at,
             updated_by_shell_id)
        VALUES
            (310, 31, 'U1', 'Disposable Sprint unit', 10, 12, 'working',
             'feat/disposable-sprint', 901, '2026-07-31 00:00:00', 11);

        INSERT INTO directive_kinds (issuer_flavor, kind)
        VALUES ('planner', 'assign'), ('planner', 'abort');

        INSERT INTO directives
            (directive_id, issuer_shell_id, issuer_flavor, kind, payload,
             target, sprint_doc_id, unit_id, status)
        VALUES
            (320, 11, 'planner', 'assign', '{"slot":"dev1"}', 'conductor',
             31, 310, 'pending'),
            (321, 11, 'planner', 'abort', '{"reason":"fixture"}',
             'conductor', 31, NULL, 'pending');

        INSERT INTO sentinel_events
            (event_id, event_kind, shell_id, sprint_doc_id, unit_id,
             directive_id, evidence)
        VALUES
            (330, 'worker_stalled', 10, 31, 310, 320,
             '{"marker":"disposable sentinel"}');

        INSERT INTO unit_expectations
            (unit_state, expected_signals, max_dwell_seconds, enabled)
        VALUES ('working', '["result"]', 600, 1);

        INSERT INTO sprint_conversation_bindings
            (binding_id, conversation_id, sprint_doc_id, role, lifecycle, slot,
             unit_id, source_directive_id, required_result_kind, state, outcome,
             result_message_id, completed_at)
        VALUES
            (340, 'cv_sprint_conductor', 31, 'conductor', 'persistent',
             'conductor', NULL, NULL, NULL, 'pending', NULL, NULL, NULL),
            (341, 'cv_sprint_dev', 31, 'developer', 'one_shot', 'dev1',
             310, 320, 'unit-report', 'terminal', 'succeeded', 103,
             '2026-07-31 00:10:00'),
            (342, 'cv_sprint_planner', 31, 'planner', 'one_shot', 'planner',
             NULL, 321, 'abort-report', 'pending', NULL, NULL, NULL);

        INSERT INTO sprint_assignment_results
            (result_id, binding_id, message_id, result_kind, directive_id)
        VALUES (350, 341, 103, 'unit-report', 320);

        INSERT INTO sprint_cancellations
            (cancellation_id, sprint_doc_id, requested_by_user_id, reason,
             source_directive_id, planner_conversation_id, state)
        VALUES
            (360, 31, 1, 'Disposable cancellation request', 321,
             'cv_sprint_planner', 'requested');

        INSERT INTO interface_generations
            (shell_id, generation, hook_token_hash, last_hook_seq)
        VALUES (11, 1, 'disposable-hook-hash', 7);

        INSERT INTO interface_sessions
            (session_id, shell_id, generation, archive_id, harness, model_route,
             cli_version, worktree, tmux_socket, tmux_session, tmux_window,
             tmux_pane_id, pane_pid, pane_start_ticks, harness_pid,
             harness_start_ticks, occupancy, lifecycle, occupied_at,
             provider_ready_at, process_ready_at, title, launch_effort)
        VALUES
            (370, 11, 1, 43, 'claude', 'claude:planner', '1.0.0',
             '/tmp/interface', '/tmp/tmux.sock', 'planner-session', 'shell',
             '%1', 5001, 6001, 5002, 6002, 'occupied', 'idle',
             '2026-07-31 00:00:00', '2026-07-31 00:00:01',
             '2026-07-31 00:00:01', 'Disposable Interface session', 'high');

        INSERT INTO interface_writer_leases
            (lease_id, session_id, shell_id, generation, client_id, token_hash,
             next_input_seq, heartbeat_at)
        VALUES
            (371, 370, 11, 1, 'browser-fixture', 'disposable-token-hash', 2,
             '2026-07-31 00:00:02');

        INSERT INTO interface_input_state
            (session_id, shell_id, generation, composer, delivery, forwarded_seq,
             browser_composer)
        VALUES (370, 11, 1, 'clean', 'normal', 1, 'clean');

        INSERT INTO interface_idempotency_keys
            (actor_scope, operation, idem_key, request_hash, response_status,
             response_resource, expires_at)
        VALUES
            ('browser:370', 'submit', 'disposable-idem',
             'disposable-request-hash', 202, 'wake:380',
             '2026-08-01 00:00:00');

        INSERT INTO interface_recovery_observations
            (observation_id, shell_id, classification, legal_actions, evidence,
             fingerprint, expires_at)
        VALUES
            ('disposable-recovery-observation', 11, 'verified_live',
             '["inspect"]', '{"session_id":370}',
             'disposable-recovery-fingerprint', '2026-08-01 00:00:00');

        INSERT INTO sprint_planner_bindings
            (binding_id, sprint_doc_id, planner_shell_id, session_id, shell_id,
             generation)
        VALUES (380, 31, 11, 370, 11, 1);

        INSERT INTO planner_wake_batches
            (batch_id, binding_id, shell_id, generation, state)
        VALUES (381, 380, 11, 1, 'queued');

        INSERT INTO planner_wake_items
            (item_id, binding_id, message_id, batch_id, state)
        VALUES (382, 380, 102, 381, 'batched');

        INSERT INTO planner_action_receipts
            (receipt_id, message_id, operation, target, idem_key, state)
        VALUES
            (383, 102, 'wake', 'planner:11', 'disposable-receipt', 'intent');

        INSERT INTO watched_prs
            (watch_id, repo, pr_number, shell_id, last_seen, sprint_doc_id,
             unit_id)
        VALUES
            (390, 'example/disposable-sprint', 901, 10,
             '{"state":"OPEN"}', 31, 310);

        INSERT INTO pr_poll_runs
            (run_id, repo, source, watch_count, finished_at, status)
        VALUES
            (391, 'example/disposable-sprint', 'scheduler', 1,
             '2026-07-31 00:00:10', 'ok');

        INSERT INTO pr_poll_observations
            (observation_id, watch_id, run_id, head_sha, fingerprint,
             transition, blind_window)
        VALUES
            (392, 390, 391, 'cccccccccccccccccccccccccccccccccccccccc',
             '{"state":"OPEN"}', 'checks_green', 0);

        INSERT INTO planner_alerts
            (alert_id, session_id, binding_id, message_id, watch_id, severity,
             reason, dedupe_key, sprint_doc_id, unit_id, role, signal, shell_id,
             batch_id, detail)
        VALUES
            (393, 370, 380, 102, 390, 'warning', 'disposable_sprint_alert',
             'disposable-alert-key', 31, 310, 'dev', 'checks_green', 10, 381,
             'Disposable planner alert');

        INSERT INTO wake_machine_retirements
            (retirement_id, binding_id, sprint_doc_id, planner_shell_id,
             session_id, wake_batch_count, wake_item_count)
        VALUES (394, 380, 31, 11, 370, 1, 1);
        """
    )

    for table in tables:
        timestamp_columns = [
            row[1]
            for row in con.execute(f'PRAGMA table_info("{table}")')
            if row[1].endswith("_at")
        ]
        for column in timestamp_columns:
            con.execute(
                f'UPDATE "{table}" SET "{column}"=? '
                f'WHERE "{column}" IS NOT NULL',
                ("2026-07-31 00:00:00",),
            )

    for _, sql in trigger_sql:
        con.executescript(sql)
    con.commit()
    return con


def build_manifest() -> dict:
    deny = re.compile(SOURCE_REFERENCE_PATTERN, re.IGNORECASE)
    allowed = set(ALLOWED_REFERENCE_FILES)
    reference_files: set[str] = set()
    tracked = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--", *SCAN_ROOTS],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    for relative in tracked:
        if relative in allowed:
            continue
        if deny.search(relative):
            reference_files.add(relative)
            continue
        path = ROOT / relative
        if path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        if deny.search(text):
            reference_files.add(relative)
    migrations = [
        migration.name
        for migration in sorted((ENGINE / "migrations").glob("*.sql"))
    ]
    return {
        "source_baseline": "36fd86703112d12bcf91f2577421f8009331d07a",
        "decision": 33,
        "spec": 44,
        "task": 166,
        "scan_roots": list(SCAN_ROOTS),
        "allowed_reference_files": list(ALLOWED_REFERENCE_FILES),
        "source_reference_pattern": SOURCE_REFERENCE_PATTERN,
        "baseline_reference_files": sorted(reference_files),
        "baseline_migration_ledger": {
            "count": len(migrations),
            "first": migrations[0],
            "last": migrations[-1],
        },
        "removed_tables": list(REMOVED_TABLES),
        "shared_table_cutover": {
            "conversations": ["mode", "sprint_doc_id"],
            "shell_messages": ["sprint_doc_id", "pr_event"],
            "shell_memory_archives": ["sprint_ref"],
        },
        "generation_markers": {
            "markdown_board": "SPRINT: frozen markdown board",
            "db_board": "Disposable Sprint unit",
            "interface_tmux": "Disposable Interface session",
            "conductor_browser": "Disposable Conductor conversation",
        },
        "retained_owners": {
            "conversation-broker": "normal browser conversations",
            "conversation_git_targets": "Diff and pull-request review",
            "shell_messages": "generic shell/task/result and job delivery",
            "model_routes": "generic model discovery and launch resolution",
            "daemon_heartbeats": "conversation-broker supervision",
            "shell_launch_records": "generic headless shell liveness",
        },
        "historical_exclusions": [".sc-state/content.sql"],
    }


def main() -> None:
    con = build_database()
    try:
        dump = "\n".join(con.iterdump()) + "\n"
    finally:
        con.close()
    OUTPUT.write_text(
        "-- Frozen Sprint v1 pre-removal database fixture.\n"
        "-- Generated by build_pre_removal_fixture.py at source baseline "
        "36fd867.\n"
        "PRAGMA foreign_keys=OFF;\n"
        f"{dump}"
        "PRAGMA foreign_keys=ON;\n"
    )
    print(f"wrote {OUTPUT}")
    MANIFEST_OUTPUT.write_text(
        json.dumps(build_manifest(), indent=2, sort_keys=True) + "\n"
    )
    print(f"wrote {MANIFEST_OUTPUT}")


if __name__ == "__main__":
    main()
