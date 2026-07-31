-- 0144 — destructively remove the disposable Sprint v1 database architecture.
--
-- Existing runtime data has no preservation value. Retained generic state is
-- copied through explicit common-column projections so this file is safe both
-- on a legacy database and on the already-clean schema used by fresh rebuilds.
-- Placeholder legacy tables make partially absent installations a no-op, and
-- the migration runner commits this body and its ledger stamp atomically.

-- migrate: foreign-keys-off
PRAGMA foreign_keys=OFF;

BEGIN;

-- Minimal placeholders make every legacy read and DROP below defensive. They
-- are dropped before commit and never become part of the retained schema.
CREATE TABLE IF NOT EXISTS sprints (
    sprint_doc_id INTEGER PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS sprint_conversation_bindings (
    binding_id       INTEGER PRIMARY KEY,
    conversation_id  TEXT,
    result_message_id INTEGER
);
CREATE TABLE IF NOT EXISTS sprint_assignment_results (
    result_id  INTEGER PRIMARY KEY,
    message_id INTEGER
);
CREATE TABLE IF NOT EXISTS planner_wake_items (
    item_id    INTEGER PRIMARY KEY,
    message_id INTEGER
);
CREATE TABLE IF NOT EXISTS planner_action_receipts (
    receipt_id INTEGER PRIMARY KEY,
    message_id INTEGER
);

CREATE TEMP TABLE _sprint_v1_documents (
    document_id INTEGER PRIMARY KEY
);
CREATE TEMP TABLE _sprint_v1_conversations (
    conversation_id TEXT PRIMARY KEY
);
CREATE TEMP TABLE _sprint_v1_messages (
    message_id INTEGER PRIMARY KEY
);

INSERT OR IGNORE INTO _sprint_v1_documents(document_id)
SELECT sprint_doc_id
FROM sprints
WHERE sprint_doc_id IS NOT NULL;

INSERT OR IGNORE INTO _sprint_v1_conversations(conversation_id)
SELECT conversation_id
FROM sprint_conversation_bindings
WHERE conversation_id IS NOT NULL;
INSERT OR IGNORE INTO _sprint_v1_conversations(conversation_id)
SELECT conversation_id
FROM conversations
WHERE owner_user_id IS NULL;

INSERT OR IGNORE INTO _sprint_v1_messages(message_id)
SELECT message_id
FROM sprint_assignment_results
WHERE message_id IS NOT NULL;
INSERT OR IGNORE INTO _sprint_v1_messages(message_id)
SELECT message_id
FROM planner_wake_items
WHERE message_id IS NOT NULL;
INSERT OR IGNORE INTO _sprint_v1_messages(message_id)
SELECT message_id
FROM planner_action_receipts
WHERE message_id IS NOT NULL;
INSERT OR IGNORE INTO _sprint_v1_messages(message_id)
SELECT result_message_id
FROM sprint_conversation_bindings
WHERE result_message_id IS NOT NULL;
INSERT OR IGNORE INTO _sprint_v1_messages(message_id)
SELECT message_id
FROM shell_messages
WHERE kind NOT IN ('shell', 'task', 'result');

-- Remove disposable conversation children before rebuilding their parent.
DELETE FROM conversation_git_targets
WHERE conversation_id IN (
    SELECT conversation_id FROM _sprint_v1_conversations
);
DELETE FROM conversation_outbox
WHERE conversation_id IN (
    SELECT conversation_id FROM _sprint_v1_conversations
);
DELETE FROM conversation_events
WHERE conversation_id IN (
    SELECT conversation_id FROM _sprint_v1_conversations
);
DELETE FROM conversation_runs
WHERE conversation_id IN (
    SELECT conversation_id FROM _sprint_v1_conversations
);
DELETE FROM conversation_messages
WHERE conversation_id IN (
    SELECT conversation_id FROM _sprint_v1_conversations
);

-- Only these two Sprint triggers were attached to retained tables.
DROP TRIGGER IF EXISTS trg_conductor_skill_pack_insert;
DROP TRIGGER IF EXISTS trg_singleton_conductor;

-- Child-first teardown; indexes and table-owned triggers disappear with their
-- tables. IF EXISTS covers installations where an earlier cleanup removed only
-- part of the subsystem.
DROP TABLE IF EXISTS sprint_assignment_results;
DROP TABLE IF EXISTS sprint_cancellations;
DROP TABLE IF EXISTS sprint_conversation_bindings;
DROP TABLE IF EXISTS sentinel_events;
DROP TABLE IF EXISTS unit_expectations;
DROP TABLE IF EXISTS directives;
DROP TABLE IF EXISTS directive_kinds;
DROP TABLE IF EXISTS planner_alerts;
DROP TABLE IF EXISTS wake_machine_retirements;
DROP TABLE IF EXISTS pr_poll_observations;
DROP TABLE IF EXISTS pr_poll_runs;
DROP TABLE IF EXISTS watched_prs;
DROP TABLE IF EXISTS planner_action_receipts;
DROP TABLE IF EXISTS planner_wake_items;
DROP TABLE IF EXISTS planner_wake_batches;
DROP TABLE IF EXISTS sprint_planner_bindings;
DROP TABLE IF EXISTS interface_writer_leases;
DROP TABLE IF EXISTS interface_input_state;
DROP TABLE IF EXISTS interface_idempotency_keys;
DROP TABLE IF EXISTS interface_recovery_observations;
DROP TABLE IF EXISTS interface_sessions;
DROP TABLE IF EXISTS interface_generations;
DROP TABLE IF EXISTS sprint_units;
DROP TABLE IF EXISTS sprints;
DROP TABLE IF EXISTS spec_qaqc_reviews;

-- Preserve historical planning records, but remove the runtime board documents
-- themselves and detach surviving decisions from those deleted rows.
DELETE FROM spec_tasks
WHERE document_id IN (SELECT document_id FROM _sprint_v1_documents);
UPDATE shell_decisions
SET document_id=NULL
WHERE document_id IN (SELECT document_id FROM _sprint_v1_documents);
DELETE FROM documents
WHERE document_id IN (SELECT document_id FROM _sprint_v1_documents);

-- Remove all generations of the retired role skills and their grants.
DELETE FROM shell_skills
WHERE skill_id IN (
    SELECT skill_id FROM skills
    WHERE name IN (
        'dev_sprint',
        'plan_sprint',
        'rev_sprint',
        'sprint',
        'sprint_cond',
        'sprint_dev',
        'sprint_onboarding',
        'sprint_orchestration',
        'sprint_orchestration_close',
        'sprint_orchestration_recover',
        'sprint_pln',
        'sprint_rev',
        'sprint_review'
    )
);
DELETE FROM flavor_skills
WHERE flavor='conductor'
   OR skill_id IN (
       SELECT skill_id FROM skills
       WHERE name IN (
           'dev_sprint',
           'plan_sprint',
           'rev_sprint',
           'sprint',
           'sprint_cond',
           'sprint_dev',
           'sprint_onboarding',
           'sprint_orchestration',
           'sprint_orchestration_close',
           'sprint_orchestration_recover',
           'sprint_pln',
           'sprint_rev',
           'sprint_review'
       )
   );
DELETE FROM skills
WHERE name IN (
    'dev_sprint',
    'plan_sprint',
    'rev_sprint',
    'sprint',
    'sprint_cond',
    'sprint_dev',
    'sprint_onboarding',
    'sprint_orchestration',
    'sprint_orchestration_close',
    'sprint_orchestration_recover',
    'sprint_pln',
    'sprint_rev',
    'sprint_review'
);
DELETE FROM flavor_defaults WHERE flavor='conductor';
DELETE FROM shell_launch_records
WHERE shell_id IN (
    SELECT shell_id FROM shells WHERE flavor='conductor'
);
UPDATE shells
SET is_deleted=1
WHERE flavor='conductor' AND is_deleted=0;

DELETE FROM daemon_heartbeats
WHERE name IN ('watch', 'pr-poller', 'sentinel', 'reconciler', 'conductor');

-- Retain generic message identity, dedupe, and read state while narrowing the
-- kind vocabulary. Association rows captured above identify scoped task/result
-- traffic without requiring the retired scope column to exist on a clean DB.
DROP TABLE IF EXISTS _shell_messages_retained;
CREATE TABLE _shell_messages_retained (
    message_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    from_shell_id INTEGER NOT NULL REFERENCES shells(shell_id),
    to_shell_id   INTEGER NOT NULL REFERENCES shells(shell_id),
    body          TEXT NOT NULL CHECK (length(body) > 0),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    read_at       TEXT,
    kind          TEXT NOT NULL DEFAULT 'shell'
                  CHECK (kind IN ('shell','task','result')),
    dedupe_key    TEXT
);
INSERT INTO _shell_messages_retained (
    message_id, from_shell_id, to_shell_id, body, created_at, read_at, kind,
    dedupe_key
)
SELECT
    message_id, from_shell_id, to_shell_id, body, created_at, read_at, kind,
    dedupe_key
FROM shell_messages
WHERE kind IN ('shell','task','result')
  AND message_id NOT IN (SELECT message_id FROM _sprint_v1_messages);
DROP TABLE shell_messages;
ALTER TABLE _shell_messages_retained RENAME TO shell_messages;
CREATE INDEX idx_shell_messages_to_unread
    ON shell_messages(to_shell_id, read_at);
CREATE UNIQUE INDEX idx_shell_messages_dedupe
    ON shell_messages(dedupe_key) WHERE dedupe_key IS NOT NULL;

-- Preserve every archive and analytics field except the retired scope marker.
DROP TABLE IF EXISTS _shell_memory_archives_retained;
CREATE TABLE _shell_memory_archives_retained (
    archive_id     INTEGER PRIMARY KEY,
    shell_id       INTEGER NOT NULL REFERENCES shells(shell_id),
    session_id     TEXT,
    date           DATE NOT NULL,
    full_narrative TEXT,
    started_at     TEXT,
    ended_at       TEXT,
    harness        TEXT,
    provider       TEXT,
    model          TEXT
);
INSERT INTO _shell_memory_archives_retained (
    archive_id, shell_id, session_id, date, full_narrative, started_at,
    ended_at, harness, provider, model
)
SELECT
    archive_id, shell_id, session_id, date, full_narrative, started_at,
    ended_at, harness, provider, model
FROM shell_memory_archives;
DROP TABLE shell_memory_archives;
ALTER TABLE _shell_memory_archives_retained RENAME TO shell_memory_archives;

-- Rebuild the parent conversation table with direct user ownership. The
-- retained projection is common to both the legacy and clean table shapes.
DROP TABLE IF EXISTS _conversations_retained;
CREATE TABLE _conversations_retained (
    conversation_id          TEXT PRIMARY KEY
                             DEFAULT ('cv_' || lower(hex(randomblob(16)))),
    shell_id                 INTEGER NOT NULL REFERENCES shells(shell_id),
    owner_user_id            INTEGER NOT NULL REFERENCES users(user_id),
    harness                  TEXT NOT NULL CHECK (trim(harness) <> ''),
    provider                 TEXT,
    model                    TEXT,
    effort                   TEXT,
    worktree                 TEXT NOT NULL CHECK (trim(worktree) <> ''),
    harness_session_ref      TEXT,
    state                    TEXT NOT NULL DEFAULT 'idle'
                             CHECK (state IN
                                 ('idle','queued','running','waiting',
                                  'error','closed')),
    title                    TEXT CHECK (title IS NULL OR length(title) <= 200),
    creation_idempotency_key TEXT NOT NULL
                             CHECK (
                               length(creation_idempotency_key)
                               BETWEEN 1 AND 255
                             ),
    creation_request_hash    TEXT NOT NULL
                             CHECK (
                               length(creation_request_hash) BETWEEN 1 AND 256
                             ),
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    last_activity_at         TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at                TEXT,
    version                  INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    starred                  INTEGER NOT NULL DEFAULT 0
                             CHECK (starred IN (0, 1)),
    CHECK (
      (state='closed' AND closed_at IS NOT NULL)
      OR
      (state<>'closed' AND closed_at IS NULL)
    )
);
INSERT INTO _conversations_retained (
    conversation_id, shell_id, owner_user_id, harness, provider, model, effort,
    worktree, harness_session_ref, state, title, creation_idempotency_key,
    creation_request_hash, created_at, last_activity_at, closed_at, version,
    starred
)
SELECT
    conversation_id, shell_id, owner_user_id, harness, provider, model, effort,
    worktree, harness_session_ref, state, title, creation_idempotency_key,
    creation_request_hash, created_at, last_activity_at, closed_at, version,
    starred
FROM conversations
WHERE owner_user_id IS NOT NULL
  AND conversation_id NOT IN (
      SELECT conversation_id FROM _sprint_v1_conversations
  );
DROP TRIGGER IF EXISTS trg_conversation_messages_immutable;
DROP TRIGGER IF EXISTS trg_conversation_messages_cause_insert;
DROP TRIGGER IF EXISTS trg_conversation_messages_state;
DROP TRIGGER IF EXISTS trg_conversation_runs_links_insert;
DROP TRIGGER IF EXISTS trg_conversation_runs_links_update;
DROP TRIGGER IF EXISTS trg_conversation_runs_state;
DROP TRIGGER IF EXISTS trg_conversation_events_links_insert;
DROP TRIGGER IF EXISTS trg_conversation_events_append_only_update;
DROP TRIGGER IF EXISTS trg_conversation_events_append_only_delete;
DROP TRIGGER IF EXISTS trg_conversation_outbox_links_insert;
DROP TRIGGER IF EXISTS trg_conversation_outbox_identity_immutable;
DROP TRIGGER IF EXISTS trg_conversation_outbox_run_update;
DROP TRIGGER IF EXISTS trg_conversation_outbox_state;
DROP TABLE conversations;
ALTER TABLE _conversations_retained RENAME TO conversations;

CREATE UNIQUE INDEX idx_conversations_idempotency
    ON conversations(owner_user_id, creation_idempotency_key);
CREATE UNIQUE INDEX idx_conversations_live_shell
    ON conversations(shell_id) WHERE state<>'closed';
CREATE INDEX idx_conversations_shell_activity
    ON conversations(shell_id, last_activity_at, conversation_id);

CREATE TRIGGER trg_conversations_identity_immutable
BEFORE UPDATE OF
    conversation_id, shell_id, owner_user_id, harness, provider, model, effort,
    worktree, creation_idempotency_key, creation_request_hash, created_at
ON conversations
BEGIN
  SELECT RAISE(ABORT, 'conversation identity and route are immutable');
END;

CREATE TRIGGER trg_conversations_state
BEFORE UPDATE OF state ON conversations
WHEN NEW.state <> OLD.state AND NOT (
    (OLD.state='idle'    AND NEW.state IN ('queued','closed')) OR
    (OLD.state='queued'  AND NEW.state IN ('idle','running')) OR
    (OLD.state='running' AND NEW.state IN
        ('idle','queued','waiting','error')) OR
    (OLD.state='waiting' AND NEW.state IN ('queued','closed')) OR
    (OLD.state='error'   AND NEW.state IN ('queued','closed'))
)
BEGIN
  SELECT RAISE(ABORT, 'illegal conversation transition');
END;

CREATE TRIGGER trg_conversation_messages_immutable
BEFORE UPDATE OF
    message_id, conversation_id, sender_kind, sender_ref, message_kind,
    body, idempotency_key, request_hash, caused_by_message_id, created_at
ON conversation_messages
BEGIN
  SELECT RAISE(ABORT, 'conversation message identity and content are immutable');
END;

CREATE TRIGGER trg_conversation_messages_cause_insert
BEFORE INSERT ON conversation_messages
WHEN NEW.caused_by_message_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM conversation_messages cause
    WHERE cause.message_id=NEW.caused_by_message_id
      AND cause.conversation_id=NEW.conversation_id
)
BEGIN
  SELECT RAISE(ABORT, 'causal message does not belong to conversation');
END;

CREATE TRIGGER trg_conversation_messages_state
BEFORE UPDATE OF state ON conversation_messages
WHEN NEW.state <> OLD.state AND NOT (
    (OLD.state='accepted' AND NEW.state IN
        ('queued','running','completed','failed','cancelled')) OR
    (OLD.state='queued'   AND NEW.state IN
        ('running','failed','cancelled')) OR
    (OLD.state='running'  AND NEW.state IN
        ('completed','failed','cancelled'))
)
BEGIN
  SELECT RAISE(ABORT, 'illegal conversation message transition');
END;

CREATE TRIGGER trg_conversation_runs_links_insert
BEFORE INSERT ON conversation_runs
WHEN NOT EXISTS (
    SELECT 1
    FROM conversations c
    JOIN conversation_messages m
      ON m.conversation_id=c.conversation_id
    WHERE c.conversation_id=NEW.conversation_id
      AND c.shell_id=NEW.shell_id
      AND m.message_id=NEW.trigger_message_id
)
BEGIN
  SELECT RAISE(ABORT, 'conversation run links do not share a conversation');
END;

CREATE TRIGGER trg_conversation_runs_links_update
BEFORE UPDATE OF conversation_id, shell_id, trigger_message_id
ON conversation_runs
BEGIN
  SELECT RAISE(ABORT, 'conversation run identity is immutable');
END;

CREATE TRIGGER trg_conversation_runs_state
BEFORE UPDATE OF state ON conversation_runs
WHEN NEW.state <> OLD.state AND NOT (
    (OLD.state='leased' AND NEW.state IN
        ('starting','failed','cancelled','unknown')) OR
    (OLD.state='starting' AND NEW.state IN
        ('running','failed','cancelled','unknown')) OR
    (OLD.state='running' AND NEW.state IN
        ('succeeded','failed','cancelled','unknown'))
)
BEGIN
  SELECT RAISE(ABORT, 'illegal conversation run transition');
END;

CREATE TRIGGER trg_conversation_events_links_insert
BEFORE INSERT ON conversation_events
WHEN (
    NEW.message_id IS NOT NULL
    AND NOT EXISTS (
      SELECT 1 FROM conversation_messages m
      WHERE m.message_id=NEW.message_id
        AND m.conversation_id=NEW.conversation_id
    )
) OR (
    NEW.run_id IS NOT NULL
    AND NOT EXISTS (
      SELECT 1 FROM conversation_runs r
      WHERE r.run_id=NEW.run_id
        AND r.conversation_id=NEW.conversation_id
    )
)
BEGIN
  SELECT RAISE(ABORT, 'conversation event links do not share a conversation');
END;

CREATE TRIGGER trg_conversation_events_append_only_update
BEFORE UPDATE ON conversation_events
BEGIN
  SELECT RAISE(ABORT, 'conversation events are append-only');
END;

CREATE TRIGGER trg_conversation_events_append_only_delete
BEFORE DELETE ON conversation_events
BEGIN
  SELECT RAISE(ABORT, 'conversation events are append-only');
END;

CREATE TRIGGER trg_conversation_outbox_links_insert
BEFORE INSERT ON conversation_outbox
WHEN (
  NOT EXISTS (
    SELECT 1 FROM conversation_messages m
    WHERE m.message_id=NEW.message_id
      AND m.conversation_id=NEW.conversation_id
  )
) OR (
  NEW.run_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM conversation_runs r
    WHERE r.run_id=NEW.run_id
      AND r.conversation_id=NEW.conversation_id
      AND r.trigger_message_id=NEW.message_id
  )
)
BEGIN
  SELECT RAISE(ABORT, 'conversation outbox links do not share a message');
END;

CREATE TRIGGER trg_conversation_outbox_identity_immutable
BEFORE UPDATE OF outbox_id, conversation_id, message_id, created_at
ON conversation_outbox
BEGIN
  SELECT RAISE(ABORT, 'conversation outbox identity is immutable');
END;

CREATE TRIGGER trg_conversation_outbox_run_update
BEFORE UPDATE OF run_id ON conversation_outbox
WHEN NEW.run_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM conversation_runs r
    WHERE r.run_id=NEW.run_id
      AND r.conversation_id=NEW.conversation_id
      AND r.trigger_message_id=NEW.message_id
)
BEGIN
  SELECT RAISE(ABORT, 'conversation outbox run does not match message');
END;

CREATE TRIGGER trg_conversation_outbox_state
BEFORE UPDATE OF state ON conversation_outbox
WHEN NEW.state <> OLD.state AND NOT (
    (OLD.state='pending' AND NEW.state IN ('claimed','cancelled')) OR
    (OLD.state='claimed' AND NEW.state IN
        ('pending','dispatched','cancelled'))
)
BEGIN
  SELECT RAISE(ABORT, 'illegal conversation outbox transition');
END;

DROP TABLE _sprint_v1_messages;
DROP TABLE _sprint_v1_conversations;
DROP TABLE _sprint_v1_documents;

-- With enforcement disabled for SQLite's parent-table swaps, turn the final
-- integrity check into a statement-level guard inside this same transaction.
CREATE TEMP TABLE _sprint_v1_fk_guard (
    valid INTEGER NOT NULL CHECK (valid=1)
);
INSERT INTO _sprint_v1_fk_guard(valid)
SELECT CASE
    WHEN EXISTS (SELECT 1 FROM pragma_foreign_key_check) THEN 0
    ELSE 1
END;
DROP TABLE _sprint_v1_fk_guard;

COMMIT;

PRAGMA foreign_keys=ON;
