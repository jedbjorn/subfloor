-- Feature #60 / spec #178 / task #681 — purge every DSH-owned data graph.
--
-- Ownership starts from typed DeepSeek harness rows, their foreign-key
-- descendants, bounded references inside an already-owned mixed Sprint, and
-- the frozen governing-spec digest.  OpenCode-owned DeepSeek-family models are
-- retained byte-for-byte.  The migration runner wraps this body and its ledger
-- stamp in one transaction, so every deletion and trigger change rolls back
-- together on failure.

-- migrate: foreign-keys-off
PRAGMA foreign_keys=OFF;

BEGIN;

DROP TRIGGER IF EXISTS trg_conversation_events_append_only_delete;
DROP TRIGGER IF EXISTS trg_pr_subscription_poll_failures_append_only_delete;
DROP TRIGGER IF EXISTS trg_pr_subscription_transitions_append_only_delete;
DROP TRIGGER IF EXISTS trg_sprint_cleanup_requests_append_only_delete;
DROP TRIGGER IF EXISTS trg_sprint_cleanup_no_delete;
DROP TRIGGER IF EXISTS trg_sprint_events_append_only_delete;
DROP TRIGGER IF EXISTS trg_sprint_followups_no_delete;
DROP TRIGGER IF EXISTS trg_sprint_judgments_append_only_delete;
DROP TRIGGER IF EXISTS trg_sprint_liveness_no_delete;
DROP TRIGGER IF EXISTS trg_sprint_participant_conversations_immutable_delete;
DROP TRIGGER IF EXISTS sprint_participant_route_bindings_immutable_delete;
DROP TRIGGER IF EXISTS trg_sprint_pr_transitions_append_only_delete;
DROP TRIGGER IF EXISTS trg_sprint_reports_append_only_delete;
DROP TRIGGER IF EXISTS trg_sprints_conformance_owner_generation;
DROP TRIGGER IF EXISTS trg_sprints_conformance_owner_reassignment_paused;

CREATE TEMP TABLE _dsh_conversations (
    conversation_id TEXT PRIMARY KEY
) WITHOUT ROWID;

INSERT INTO _dsh_conversations (conversation_id)
SELECT conversation_id
FROM conversations
WHERE lower(trim(harness))='deepseek';

CREATE TEMP TABLE _dsh_messages (
    message_id INTEGER PRIMARY KEY
);

WITH RECURSIVE owned(message_id) AS (
    SELECT message.message_id
    FROM conversation_messages message
    JOIN _dsh_conversations owned_conversation
      USING (conversation_id)
    UNION
    SELECT child.message_id
    FROM conversation_messages child
    JOIN owned parent ON parent.message_id=child.caused_by_message_id
)
INSERT INTO _dsh_messages (message_id)
SELECT message_id FROM owned;

CREATE TEMP TABLE _dsh_runs (
    run_id INTEGER PRIMARY KEY
);

INSERT INTO _dsh_runs (run_id)
SELECT DISTINCT run.run_id
FROM conversation_runs run
LEFT JOIN _dsh_conversations owned_conversation
  USING (conversation_id)
LEFT JOIN _dsh_messages trigger_message
  ON trigger_message.message_id=run.trigger_message_id
WHERE owned_conversation.conversation_id IS NOT NULL
   OR trigger_message.message_id IS NOT NULL;

CREATE TEMP TABLE _dsh_participants (
    participant_id INTEGER PRIMARY KEY,
    sprint_id      INTEGER NOT NULL,
    shell_id       INTEGER NOT NULL
);

INSERT INTO _dsh_participants (participant_id,sprint_id,shell_id)
SELECT DISTINCT participant.participant_id,
       participant.sprint_id,
       participant.shell_id
FROM sprint_participants participant
LEFT JOIN sprint_participant_route_bindings active_binding
  ON active_binding.binding_id=participant.active_route_binding_id
WHERE lower(trim(participant.harness))='deepseek'
   OR lower(trim(COALESCE(active_binding.harness,'')))='deepseek';

CREATE TEMP TABLE _dsh_bindings (
    binding_id INTEGER PRIMARY KEY
);

INSERT INTO _dsh_bindings (binding_id)
SELECT binding.binding_id
FROM sprint_participant_route_bindings binding
LEFT JOIN _dsh_participants participant USING (participant_id)
WHERE lower(trim(binding.harness))='deepseek'
   OR participant.participant_id IS NOT NULL;

CREATE TEMP TABLE _dsh_features (
    feature_id INTEGER PRIMARY KEY
);

INSERT OR IGNORE INTO _dsh_features (feature_id)
SELECT sprint.feature_id
FROM sprint_specs binding
JOIN sprints sprint USING (sprint_id)
WHERE binding.bound_revision_sha256=
      'be111eb206b9e6ea09352a90346e8a94544ac1ef3bd932716e4bd90451d33c42'
  AND binding.bound_revision_legacy=0;

INSERT OR IGNORE INTO _dsh_features (feature_id)
SELECT document.feature_id
FROM documents document
JOIN roadmap feature USING (feature_id)
WHERE document.document_id=178
  AND document.title='Complete DSH Removal'
  AND feature.title='DeepSeek Harness removal';

CREATE TEMP TABLE _dsh_documents (
    document_id INTEGER PRIMARY KEY
);

INSERT INTO _dsh_documents (document_id)
SELECT document.document_id
FROM documents document
JOIN _dsh_features feature USING (feature_id);

CREATE TEMP TABLE _dsh_tasks (
    task_id INTEGER PRIMARY KEY
);

INSERT INTO _dsh_tasks (task_id)
SELECT task.task_id
FROM spec_tasks task
JOIN _dsh_features feature USING (feature_id);

CREATE TEMP TABLE _dsh_full_sprints (
    sprint_id INTEGER PRIMARY KEY
);

INSERT OR IGNORE INTO _dsh_full_sprints (sprint_id)
SELECT sprint.sprint_id
FROM sprints sprint
JOIN _dsh_features feature USING (feature_id);

INSERT OR IGNORE INTO _dsh_full_sprints (sprint_id)
SELECT binding.sprint_id
FROM sprint_specs binding
JOIN _dsh_documents document USING (document_id);

INSERT OR IGNORE INTO _dsh_bindings (binding_id)
SELECT binding.binding_id
FROM sprint_participant_route_bindings binding
JOIN sprint_participants participant USING (participant_id)
JOIN _dsh_full_sprints full_sprint USING (sprint_id);

CREATE TEMP TABLE _dsh_affected_sprints (
    sprint_id INTEGER PRIMARY KEY
);

INSERT OR IGNORE INTO _dsh_affected_sprints (sprint_id)
SELECT sprint_id FROM _dsh_full_sprints;

INSERT OR IGNORE INTO _dsh_affected_sprints (sprint_id)
SELECT sprint_id FROM _dsh_participants;

INSERT OR IGNORE INTO _dsh_documents (document_id)
SELECT document.document_id
FROM documents document
JOIN sprints sprint ON sprint.feature_id=document.feature_id
JOIN _dsh_affected_sprints affected USING (sprint_id)
WHERE instr(lower(COALESCE(document.title,'')),'deepseek')>0
   OR instr(lower(COALESCE(document.title,'')),'dsh')>0
   OR instr(lower(COALESCE(document.body,'')),'deepseek')>0
   OR instr(lower(COALESCE(document.body,'')),'dsh')>0;

INSERT OR IGNORE INTO _dsh_tasks (task_id)
SELECT task.task_id
FROM spec_tasks task
LEFT JOIN _dsh_documents document USING (document_id)
WHERE document.document_id IS NOT NULL
   OR (
     task.feature_id IN (
       SELECT sprint.feature_id
       FROM sprints sprint
       JOIN _dsh_affected_sprints affected USING (sprint_id)
     )
     AND (
       instr(lower(task.title),'deepseek')>0
       OR instr(lower(task.title),'dsh')>0
       OR instr(lower(COALESCE(task.description,'')),'deepseek')>0
       OR instr(lower(COALESCE(task.description,'')),'dsh')>0
     )
   );

CREATE TEMP TABLE _dsh_decisions (
    decision_id INTEGER PRIMARY KEY
);

INSERT INTO _dsh_decisions (decision_id)
SELECT decision.decision_id
FROM shell_decisions decision
LEFT JOIN _dsh_features feature USING (feature_id)
LEFT JOIN _dsh_documents document USING (document_id)
WHERE feature.feature_id IS NOT NULL
   OR document.document_id IS NOT NULL
   OR (
     decision.feature_id IN (
       SELECT sprint.feature_id
       FROM sprints sprint
       JOIN _dsh_affected_sprints affected USING (sprint_id)
     )
     AND (
       instr(lower(decision.decision),'deepseek')>0
       OR instr(lower(decision.decision),'dsh')>0
       OR instr(lower(COALESCE(decision.rationale,'')),'deepseek')>0
       OR instr(lower(COALESCE(decision.rationale,'')),'dsh')>0
     )
   );

CREATE TEMP TABLE _dsh_flags (
    flag_id INTEGER PRIMARY KEY
);

INSERT INTO _dsh_flags (flag_id)
SELECT flag.flag_id
FROM flags flag
LEFT JOIN _dsh_features feature USING (feature_id)
WHERE feature.feature_id IS NOT NULL
   OR (
     flag.feature_id IN (
       SELECT sprint.feature_id
       FROM sprints sprint
       JOIN _dsh_affected_sprints affected USING (sprint_id)
     )
     AND (
       instr(lower(COALESCE(flag.display_name,'')),'deepseek')>0
       OR instr(lower(COALESCE(flag.display_name,'')),'dsh')>0
       OR instr(lower(COALESCE(flag.description,'')),'deepseek')>0
       OR instr(lower(COALESCE(flag.description,'')),'dsh')>0
       OR instr(lower(COALESCE(flag.resolution_notes,'')),'deepseek')>0
       OR instr(lower(COALESCE(flag.resolution_notes,'')),'dsh')>0
     )
   );

CREATE TEMP TABLE _dsh_work_units (
    work_unit_id INTEGER PRIMARY KEY,
    sprint_id    INTEGER NOT NULL
);

INSERT OR IGNORE INTO _dsh_work_units (work_unit_id,sprint_id)
SELECT unit.work_unit_id,unit.sprint_id
FROM sprint_work_units unit
LEFT JOIN _dsh_full_sprints full_sprint USING (sprint_id)
LEFT JOIN _dsh_participants assigned
  ON assigned.sprint_id=unit.sprint_id
 AND assigned.shell_id=unit.assigned_shell_id
LEFT JOIN _dsh_participants reviewer
  ON reviewer.sprint_id=unit.sprint_id
 AND reviewer.shell_id=unit.reviewer_shell_id
LEFT JOIN _dsh_affected_sprints affected USING (sprint_id)
WHERE full_sprint.sprint_id IS NOT NULL
   OR assigned.participant_id IS NOT NULL
   OR reviewer.participant_id IS NOT NULL
   OR (
     affected.sprint_id IS NOT NULL
     AND (
       instr(lower(unit.title),'deepseek')>0
       OR instr(lower(unit.title),'dsh')>0
       OR instr(lower(unit.expected_output),'deepseek')>0
       OR instr(lower(unit.expected_output),'dsh')>0
     )
   );

CREATE TEMP TABLE _dsh_registered_prs (
    registered_pr_id INTEGER PRIMARY KEY
);

INSERT OR IGNORE INTO _dsh_registered_prs (registered_pr_id)
SELECT registered.registered_pr_id
FROM sprint_registered_prs registered
LEFT JOIN _dsh_full_sprints full_sprint USING (sprint_id)
LEFT JOIN _dsh_participants participant
  ON participant.participant_id=registered.owner_participant_id
LEFT JOIN sprint_pr_work_units link
  ON link.sprint_id=registered.sprint_id
 AND link.registered_pr_id=registered.registered_pr_id
LEFT JOIN _dsh_work_units unit
  ON unit.sprint_id=link.sprint_id
 AND unit.work_unit_id=link.work_unit_id
WHERE full_sprint.sprint_id IS NOT NULL
   OR participant.participant_id IS NOT NULL
   OR unit.work_unit_id IS NOT NULL;

CREATE TEMP TABLE _dsh_subscriptions (
    subscription_id INTEGER PRIMARY KEY
);

INSERT INTO _dsh_subscriptions (subscription_id)
SELECT subscription.subscription_id
FROM pr_subscriptions subscription
JOIN _dsh_registered_prs registered
  ON registered.registered_pr_id=subscription.sprint_registered_pr_id;

CREATE TEMP TABLE _dsh_delivery_nodes (
    kind TEXT NOT NULL CHECK (kind IN ('message','wake')),
    id   INTEGER NOT NULL,
    PRIMARY KEY (kind,id)
) WITHOUT ROWID;

WITH RECURSIVE owned(kind,id) AS (
    SELECT 'message',message.message_id
    FROM wake_message message
    LEFT JOIN _dsh_full_sprints full_sprint USING (sprint_id)
    LEFT JOIN _dsh_participants sender
      ON sender.participant_id=message.from_participant_id
    LEFT JOIN _dsh_participants receiver
      ON receiver.participant_id=message.to_participant_id
    LEFT JOIN _dsh_work_units unit
      ON unit.work_unit_id=message.work_unit_id
     AND unit.sprint_id=message.sprint_id
    LEFT JOIN _dsh_affected_sprints affected USING (sprint_id)
    WHERE full_sprint.sprint_id IS NOT NULL
       OR sender.participant_id IS NOT NULL
       OR receiver.participant_id IS NOT NULL
       OR unit.work_unit_id IS NOT NULL
       OR (
         affected.sprint_id IS NOT NULL
         AND (instr(lower(message.body),'deepseek')>0
              OR instr(lower(message.body),'dsh')>0)
       )
    UNION
    SELECT 'wake',wake.wake_id
    FROM sprint_wake_outbox wake
    LEFT JOIN _dsh_full_sprints full_sprint USING (sprint_id)
    LEFT JOIN _dsh_participants participant USING (participant_id)
    WHERE full_sprint.sprint_id IS NOT NULL
       OR participant.participant_id IS NOT NULL
    UNION
    SELECT 'message',child.message_id
    FROM owned parent
    JOIN wake_message child ON child.reply_to_message_id=parent.id
    WHERE parent.kind='message'
    UNION
    SELECT 'wake',link.wake_id
    FROM owned message
    JOIN sprint_wake_messages link ON link.message_id=message.id
    WHERE message.kind='message'
    UNION
    SELECT 'message',link.message_id
    FROM owned wake
    JOIN sprint_wake_messages link ON link.wake_id=wake.id
    WHERE wake.kind='wake'
)
INSERT INTO _dsh_delivery_nodes (kind,id)
SELECT kind,id FROM owned;

CREATE TEMP TABLE _dsh_events (
    event_id INTEGER PRIMARY KEY
);

INSERT INTO _dsh_events (event_id)
SELECT event.event_id
FROM sprint_events event
LEFT JOIN _dsh_full_sprints full_sprint USING (sprint_id)
LEFT JOIN _dsh_affected_sprints affected USING (sprint_id)
WHERE full_sprint.sprint_id IS NOT NULL
   OR (
     affected.sprint_id IS NOT NULL
     AND (
       EXISTS (
         SELECT 1
         FROM _dsh_participants participant
         WHERE participant.sprint_id=event.sprint_id
           AND participant.shell_id=event.actor_shell_id
       )
       OR instr(lower(event.payload),'deepseek')>0
       OR instr(lower(event.payload),'dsh')>0
       OR EXISTS (
         SELECT 1
         FROM json_tree(event.payload) value
         WHERE value.key IN (
           'participant_id','from_participant_id','to_participant_id'
         )
           AND CAST(value.atom AS INTEGER) IN (
             SELECT participant_id FROM _dsh_participants
           )
       )
       OR EXISTS (
         SELECT 1
         FROM json_tree(event.payload) value
         WHERE value.key='work_unit_id'
           AND CAST(value.atom AS INTEGER) IN (
             SELECT work_unit_id FROM _dsh_work_units
           )
       )
     )
   );

CREATE TEMP TABLE _dsh_reports (
    report_id INTEGER PRIMARY KEY
);

INSERT INTO _dsh_reports (report_id)
SELECT report.report_id
FROM sprint_reports report
LEFT JOIN _dsh_full_sprints full_sprint USING (sprint_id)
LEFT JOIN _dsh_affected_sprints affected USING (sprint_id)
WHERE full_sprint.sprint_id IS NOT NULL
   OR (
     affected.sprint_id IS NOT NULL
     AND (
       EXISTS (
         SELECT 1
         FROM _dsh_participants participant
         WHERE participant.sprint_id=report.sprint_id
           AND participant.shell_id=report.author_shell_id
       )
       OR instr(lower(report.body),'deepseek')>0
       OR instr(lower(report.body),'dsh')>0
     )
   );

CREATE TEMP TABLE _dsh_approvals (
    approval_id INTEGER PRIMARY KEY
);

INSERT INTO _dsh_approvals (approval_id)
SELECT approval.approval_id
FROM sprint_spec_approvals approval
LEFT JOIN _dsh_documents document
  ON document.document_id=approval.document_id
LEFT JOIN _dsh_documents findings
  ON findings.document_id=approval.findings_document_id
WHERE document.document_id IS NOT NULL
   OR findings.document_id IS NOT NULL;

CREATE TEMP TABLE _dsh_generations (
    generation_id TEXT PRIMARY KEY
) WITHOUT ROWID;

INSERT OR IGNORE INTO _dsh_generations (generation_id)
SELECT generation_id
FROM model_routes
WHERE lower(trim(harness))='deepseek'
  AND generation_id IS NOT NULL;

INSERT OR IGNORE INTO _dsh_generations (generation_id)
SELECT generation.generation_id
FROM model_catalog_generations generation
WHERE EXISTS (
        SELECT 1 FROM json_each(generation.source_summary)
        WHERE lower(key)='deepseek'
      )
   OR EXISTS (
        SELECT 1 FROM json_each(generation.harness_versions)
        WHERE lower(key)='deepseek'
      )
   OR EXISTS (
        SELECT 1 FROM json_each(generation.source_fingerprints)
        WHERE lower(key)='deepseek'
      )
   OR (
        generation.error_summary IS NOT NULL
        AND EXISTS (
          SELECT 1 FROM json_tree(generation.error_summary)
          WHERE lower(CAST(atom AS TEXT))='deepseek'
             OR instr(lower(CAST(atom AS TEXT)),'dsh')>0
        )
      );

-- Runtime, catalogue, transcript, and normalized usage roots.
DELETE FROM flavor_defaults WHERE lower(trim(harness))='deepseek';
DELETE FROM model_routes WHERE lower(trim(harness))='deepseek';
DELETE FROM model_catalog_generations
WHERE generation_id IN (SELECT generation_id FROM _dsh_generations);
DELETE FROM analytics_parse_cache WHERE lower(trim(harness))='deepseek';
DELETE FROM session_token_usage
WHERE lower(trim(harness))='deepseek'
   OR archive_id IN (
        SELECT archive_id FROM shell_memory_archives
        WHERE lower(trim(COALESCE(harness,'')))='deepseek'
      );
DELETE FROM shell_memory_archives
WHERE lower(trim(COALESCE(harness,'')))='deepseek';
DELETE FROM shell_launch_records
WHERE lower(trim(COALESCE(harness,'')))='deepseek';

-- Conversation graphs, including cross-message ancestry and Sprint delivery.
DELETE FROM sprint_wake_attempts
WHERE target_conversation_id IN (
        SELECT conversation_id FROM _dsh_conversations
      );
DELETE FROM sprint_participant_conversations
WHERE conversation_id IN (SELECT conversation_id FROM _dsh_conversations)
   OR sprint_participant_id IN (
        SELECT participant_id FROM _dsh_participants
      )
   OR sprint_participant_id IN (
        SELECT participant.participant_id
        FROM sprint_participants participant
        JOIN _dsh_full_sprints full_sprint USING (sprint_id)
      );
DELETE FROM active_shell_chats
WHERE chat_id IN (SELECT conversation_id FROM _dsh_conversations);
DELETE FROM conversation_boot_snapshots
WHERE conversation_id IN (SELECT conversation_id FROM _dsh_conversations);
DELETE FROM conversation_git_targets
WHERE conversation_id IN (SELECT conversation_id FROM _dsh_conversations);
DELETE FROM conversation_outbox
WHERE conversation_id IN (SELECT conversation_id FROM _dsh_conversations)
   OR message_id IN (SELECT message_id FROM _dsh_messages)
   OR run_id IN (SELECT run_id FROM _dsh_runs);
DELETE FROM conversation_events
WHERE conversation_id IN (SELECT conversation_id FROM _dsh_conversations)
   OR message_id IN (SELECT message_id FROM _dsh_messages)
   OR run_id IN (SELECT run_id FROM _dsh_runs);
DELETE FROM conversation_runs
WHERE run_id IN (SELECT run_id FROM _dsh_runs);
DELETE FROM conversation_messages
WHERE message_id IN (SELECT message_id FROM _dsh_messages);
DELETE FROM conversations
WHERE conversation_id IN (SELECT conversation_id FROM _dsh_conversations);

-- Sprint relay, liveness, wake, PR, report, and cleanup graphs.
DELETE FROM sprint_wake_recovery_messages
WHERE recovery_event_id IN (SELECT event_id FROM _dsh_events)
   OR prior_wake_id IN (
        SELECT id FROM _dsh_delivery_nodes WHERE kind='wake'
      )
   OR replacement_wake_id IN (
        SELECT id FROM _dsh_delivery_nodes WHERE kind='wake'
      )
   OR message_id IN (
        SELECT id FROM _dsh_delivery_nodes WHERE kind='message'
      );
DELETE FROM sprint_wake_attempts
WHERE wake_id IN (SELECT id FROM _dsh_delivery_nodes WHERE kind='wake');
DELETE FROM sprint_wake_messages
WHERE wake_id IN (SELECT id FROM _dsh_delivery_nodes WHERE kind='wake')
   OR message_id IN (
        SELECT id FROM _dsh_delivery_nodes WHERE kind='message'
      );
DELETE FROM sprint_liveness_expectations
WHERE participant_id IN (SELECT participant_id FROM _dsh_participants)
   OR message_id IN (
        SELECT id FROM _dsh_delivery_nodes WHERE kind='message'
      )
   OR sprint_id IN (SELECT sprint_id FROM _dsh_full_sprints);
DELETE FROM wake_message
WHERE message_id IN (
        SELECT id FROM _dsh_delivery_nodes WHERE kind='message'
      );
DELETE FROM sprint_wake_outbox
WHERE wake_id IN (SELECT id FROM _dsh_delivery_nodes WHERE kind='wake');

DELETE FROM pr_subscription_poll_failures
WHERE subscription_id IN (SELECT subscription_id FROM _dsh_subscriptions);
DELETE FROM pr_subscription_transitions
WHERE subscription_id IN (SELECT subscription_id FROM _dsh_subscriptions);
DELETE FROM pr_subscriptions
WHERE subscription_id IN (SELECT subscription_id FROM _dsh_subscriptions);
DELETE FROM sprint_pr_transitions
WHERE registered_pr_id IN (
        SELECT registered_pr_id FROM _dsh_registered_prs
      );
DELETE FROM sprint_pr_work_units
WHERE registered_pr_id IN (
        SELECT registered_pr_id FROM _dsh_registered_prs
      )
   OR work_unit_id IN (SELECT work_unit_id FROM _dsh_work_units);
DELETE FROM sprint_registered_prs
WHERE registered_pr_id IN (
        SELECT registered_pr_id FROM _dsh_registered_prs
      );

DELETE FROM sprint_followups
WHERE sprint_id IN (SELECT sprint_id FROM _dsh_full_sprints)
   OR source_report_id IN (SELECT report_id FROM _dsh_reports)
   OR work_unit_id IN (SELECT work_unit_id FROM _dsh_work_units)
   OR spec_document_id IN (SELECT document_id FROM _dsh_documents)
   OR (
     sprint_id IN (SELECT sprint_id FROM _dsh_affected_sprints)
     AND (
       instr(lower(title),'deepseek')>0 OR instr(lower(title),'dsh')>0
       OR instr(lower(body),'deepseek')>0 OR instr(lower(body),'dsh')>0
     )
   );
DELETE FROM sprint_judgments
WHERE sprint_id IN (SELECT sprint_id FROM _dsh_full_sprints)
   OR participant_id IN (SELECT participant_id FROM _dsh_participants)
   OR work_unit_id IN (SELECT work_unit_id FROM _dsh_work_units)
   OR (
     sprint_id IN (SELECT sprint_id FROM _dsh_affected_sprints)
     AND (instr(lower(body),'deepseek')>0 OR instr(lower(body),'dsh')>0)
   );
DELETE FROM sprint_reports WHERE report_id IN (SELECT report_id FROM _dsh_reports);
DELETE FROM sprint_events WHERE event_id IN (SELECT event_id FROM _dsh_events);
DELETE FROM sprint_cleanup_requests
WHERE sprint_id IN (SELECT sprint_id FROM _dsh_full_sprints)
   OR (
     sprint_id IN (SELECT sprint_id FROM _dsh_affected_sprints)
     AND caller_shell_id IN (SELECT shell_id FROM _dsh_participants)
   );
DELETE FROM sprint_cleanup_targets
WHERE sprint_id IN (SELECT sprint_id FROM _dsh_full_sprints)
   OR (
     sprint_id IN (SELECT sprint_id FROM _dsh_affected_sprints)
     AND shell_id IN (SELECT shell_id FROM _dsh_participants)
   );

DELETE FROM sprint_work_unit_dependencies
WHERE work_unit_id IN (SELECT work_unit_id FROM _dsh_work_units)
   OR depends_on_work_unit_id IN (SELECT work_unit_id FROM _dsh_work_units);
DELETE FROM sprint_work_unit_tasks
WHERE work_unit_id IN (SELECT work_unit_id FROM _dsh_work_units)
   OR task_id IN (SELECT task_id FROM _dsh_tasks);
DELETE FROM sprint_work_units
WHERE work_unit_id IN (SELECT work_unit_id FROM _dsh_work_units);

UPDATE sprint_participants
SET active_route_binding_id=NULL
WHERE active_route_binding_id IN (SELECT binding_id FROM _dsh_bindings);
UPDATE sprints
SET conformance_reviewer_shell_id=NULL,
    conformance_owner_generation=conformance_owner_generation+1
WHERE sprint_id IN (SELECT sprint_id FROM _dsh_affected_sprints)
  AND conformance_reviewer_shell_id IN (
        SELECT shell_id FROM _dsh_participants
      );
DELETE FROM sprint_participant_route_bindings
WHERE binding_id IN (SELECT binding_id FROM _dsh_bindings);
DELETE FROM sprint_participants
WHERE participant_id IN (SELECT participant_id FROM _dsh_participants)
   OR sprint_id IN (SELECT sprint_id FROM _dsh_full_sprints);

UPDATE sprint_specs
SET approval_id=NULL
WHERE approval_id IN (SELECT approval_id FROM _dsh_approvals);
DELETE FROM sprint_specs
WHERE sprint_id IN (SELECT sprint_id FROM _dsh_full_sprints)
   OR document_id IN (SELECT document_id FROM _dsh_documents);
DELETE FROM sprint_spec_approvals
WHERE approval_id IN (SELECT approval_id FROM _dsh_approvals);
DELETE FROM governing_revision_backfill_permits
WHERE sprint_id IN (SELECT sprint_id FROM _dsh_full_sprints)
   OR document_id IN (SELECT document_id FROM _dsh_documents);
DELETE FROM sprints WHERE sprint_id IN (SELECT sprint_id FROM _dsh_full_sprints);

-- A mixed Sprint parent remains only to retain unrelated participants.  Scrub
-- its bounded DSH label while leaving every retained child row unchanged.
UPDATE roadmap
SET title='Retained mixed Sprint',summary=NULL
WHERE feature_id IN (
        SELECT sprint.feature_id
        FROM sprints sprint
        JOIN _dsh_affected_sprints affected USING (sprint_id)
      )
  AND feature_id NOT IN (SELECT feature_id FROM _dsh_features)
  AND (
    instr(lower(title),'deepseek')>0 OR instr(lower(title),'dsh')>0
    OR instr(lower(COALESCE(summary,'')),'deepseek')>0
    OR instr(lower(COALESCE(summary,'')),'dsh')>0
  );

-- Exact governing-feature planning data is removed after its delivery graph.
UPDATE shell_decisions
SET parent_decision_id=NULL
WHERE parent_decision_id IN (SELECT decision_id FROM _dsh_decisions)
  AND decision_id NOT IN (SELECT decision_id FROM _dsh_decisions);
DELETE FROM shell_decisions
WHERE decision_id IN (SELECT decision_id FROM _dsh_decisions);

UPDATE flags
SET parent_flag_id=NULL
WHERE parent_flag_id IN (SELECT flag_id FROM _dsh_flags)
  AND flag_id NOT IN (SELECT flag_id FROM _dsh_flags);
DELETE FROM flags
WHERE flag_id IN (SELECT flag_id FROM _dsh_flags);
DELETE FROM spec_tasks WHERE task_id IN (SELECT task_id FROM _dsh_tasks);
DELETE FROM documents WHERE document_id IN (SELECT document_id FROM _dsh_documents);
DELETE FROM feature_blockers
WHERE feature_id IN (SELECT feature_id FROM _dsh_features)
   OR blocked_by IN (SELECT feature_id FROM _dsh_features);
DELETE FROM roadmap WHERE feature_id IN (SELECT feature_id FROM _dsh_features);

-- dsh-purge-failure-injection

CREATE TEMP TABLE _dsh_purge_guard (
    value INTEGER NOT NULL CHECK (value=0)
);

INSERT INTO _dsh_purge_guard (value)
SELECT COUNT(*) FROM flavor_defaults WHERE lower(trim(harness))='deepseek';
INSERT INTO _dsh_purge_guard (value)
SELECT COUNT(*) FROM model_routes WHERE lower(trim(harness))='deepseek';
INSERT INTO _dsh_purge_guard (value)
SELECT COUNT(*) FROM analytics_parse_cache WHERE lower(trim(harness))='deepseek';
INSERT INTO _dsh_purge_guard (value)
SELECT COUNT(*) FROM session_token_usage WHERE lower(trim(harness))='deepseek';
INSERT INTO _dsh_purge_guard (value)
SELECT COUNT(*) FROM shell_memory_archives
WHERE lower(trim(COALESCE(harness,'')))='deepseek';
INSERT INTO _dsh_purge_guard (value)
SELECT COUNT(*) FROM shell_launch_records
WHERE lower(trim(COALESCE(harness,'')))='deepseek';
INSERT INTO _dsh_purge_guard (value)
SELECT COUNT(*) FROM conversations WHERE lower(trim(harness))='deepseek';
INSERT INTO _dsh_purge_guard (value)
SELECT COUNT(*) FROM sprint_participants WHERE lower(trim(harness))='deepseek';
INSERT INTO _dsh_purge_guard (value)
SELECT COUNT(*) FROM sprint_participant_route_bindings
WHERE lower(trim(harness))='deepseek';
INSERT INTO _dsh_purge_guard (value)
SELECT COUNT(*) FROM pragma_foreign_key_check;

DROP TABLE _dsh_purge_guard;
DROP TABLE _dsh_generations;
DROP TABLE _dsh_approvals;
DROP TABLE _dsh_reports;
DROP TABLE _dsh_events;
DROP TABLE _dsh_delivery_nodes;
DROP TABLE _dsh_subscriptions;
DROP TABLE _dsh_registered_prs;
DROP TABLE _dsh_work_units;
DROP TABLE _dsh_affected_sprints;
DROP TABLE _dsh_full_sprints;
DROP TABLE _dsh_flags;
DROP TABLE _dsh_decisions;
DROP TABLE _dsh_tasks;
DROP TABLE _dsh_documents;
DROP TABLE _dsh_features;
DROP TABLE _dsh_bindings;
DROP TABLE _dsh_participants;
DROP TABLE _dsh_runs;
DROP TABLE _dsh_messages;
DROP TABLE _dsh_conversations;

CREATE TRIGGER sprint_participant_route_bindings_immutable_delete
BEFORE DELETE ON sprint_participant_route_bindings
BEGIN
  SELECT RAISE(ABORT, 'participant route bindings are immutable');
END;

CREATE TRIGGER trg_conversation_events_append_only_delete
BEFORE DELETE ON conversation_events
BEGIN
  SELECT RAISE(ABORT, 'conversation events are append-only');
END;

CREATE TRIGGER trg_pr_subscription_poll_failures_append_only_delete
BEFORE DELETE ON pr_subscription_poll_failures BEGIN
  SELECT RAISE(ABORT, 'PR subscription poll failures are append-only');
END;

CREATE TRIGGER trg_pr_subscription_transitions_append_only_delete
BEFORE DELETE ON pr_subscription_transitions BEGIN
  SELECT RAISE(ABORT, 'PR subscription transitions are append-only');
END;

CREATE TRIGGER trg_sprint_cleanup_no_delete
BEFORE DELETE ON sprint_cleanup_targets BEGIN
  SELECT RAISE(ABORT, 'Sprint cleanup targets are durable evidence');
END;

CREATE TRIGGER trg_sprint_cleanup_requests_append_only_delete
BEFORE DELETE ON sprint_cleanup_requests BEGIN
  SELECT RAISE(ABORT, 'Sprint cleanup requests are append-only');
END;

CREATE TRIGGER trg_sprint_events_append_only_delete
BEFORE DELETE ON sprint_events BEGIN
  SELECT RAISE(ABORT, 'Sprint events are append-only');
END;

CREATE TRIGGER trg_sprint_followups_no_delete
BEFORE DELETE ON sprint_followups
BEGIN
  SELECT RAISE(ABORT, 'Sprint follow-up history is durable');
END;

CREATE TRIGGER trg_sprint_judgments_append_only_delete
BEFORE DELETE ON sprint_judgments BEGIN
  SELECT RAISE(ABORT, 'Sprint judgments are append-only');
END;

CREATE TRIGGER trg_sprint_liveness_no_delete
BEFORE DELETE ON sprint_liveness_expectations
BEGIN
  SELECT RAISE(ABORT, 'Sprint liveness history is durable');
END;

CREATE TRIGGER trg_sprint_participant_conversations_immutable_delete
BEFORE DELETE ON sprint_participant_conversations
BEGIN
  SELECT RAISE(ABORT, 'Sprint participant conversation links are immutable');
END;

CREATE TRIGGER trg_sprint_pr_transitions_append_only_delete
BEFORE DELETE ON sprint_pr_transitions BEGIN
  SELECT RAISE(ABORT, 'Sprint PR transitions are append-only');
END;

CREATE TRIGGER trg_sprint_reports_append_only_delete
BEFORE DELETE ON sprint_reports BEGIN
  SELECT RAISE(ABORT, 'Sprint reports are append-only');
END;

CREATE TRIGGER trg_sprints_conformance_owner_generation
BEFORE UPDATE OF conformance_reviewer_shell_id,conformance_owner_generation
ON sprints
WHEN NEW.conformance_owner_generation<0
  OR (
    NEW.conformance_reviewer_shell_id IS OLD.conformance_reviewer_shell_id
    AND NEW.conformance_owner_generation<>OLD.conformance_owner_generation
  )
  OR (
    NEW.conformance_reviewer_shell_id IS NOT OLD.conformance_reviewer_shell_id
    AND NEW.conformance_owner_generation<>OLD.conformance_owner_generation+1
  )
BEGIN
  SELECT RAISE(ABORT, 'Sprint conformance owner generation is invalid');
END;

CREATE TRIGGER trg_sprints_conformance_owner_reassignment_paused
BEFORE UPDATE OF conformance_reviewer_shell_id ON sprints
WHEN NEW.conformance_reviewer_shell_id IS NOT OLD.conformance_reviewer_shell_id
 AND OLD.lifecycle NOT IN ('prepared','paused')
BEGIN
  SELECT RAISE(ABORT, 'Sprint conformance owner may be reassigned only while paused');
END;

COMMIT;
