-- 0132 — durable browser-native conversation foundation (feature #24).
--
-- Conversations outlive harness processes. The engine stores the exact native
-- session reference, ordered messages, one-turn runs, normalized replay events,
-- and transactional dispatch intent. Harness transcripts remain harness-owned.
--
-- No trigger automatically dispatches work on INSERT. The API/broker must
-- insert a message and its outbox row in one explicit transaction; snapshot
-- replay therefore restores durable state without accidentally launching it.

BEGIN;

CREATE TABLE conversations (
    conversation_id         TEXT PRIMARY KEY
                            DEFAULT ('cv_' || lower(hex(randomblob(16)))),
    shell_id                INTEGER NOT NULL REFERENCES shells(shell_id),
    owner_user_id           INTEGER NOT NULL REFERENCES users(user_id),
    harness                 TEXT NOT NULL CHECK (trim(harness) <> ''),
    provider                TEXT,
    model                   TEXT,
    effort                  TEXT,
    worktree                TEXT NOT NULL CHECK (trim(worktree) <> ''),
    harness_session_ref     TEXT,
    state                   TEXT NOT NULL DEFAULT 'idle'
                            CHECK (state IN
                                ('idle','queued','running','waiting',
                                 'error','closed')),
    title                   TEXT CHECK (title IS NULL OR length(title) <= 200),
    creation_idempotency_key TEXT NOT NULL
                            CHECK (
                              length(creation_idempotency_key) BETWEEN 1 AND 255
                            ),
    creation_request_hash   TEXT NOT NULL
                            CHECK (length(creation_request_hash) BETWEEN 1 AND 256),
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    last_activity_at        TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at               TEXT,
    version                 INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    CHECK (
      (state='closed' AND closed_at IS NOT NULL)
      OR
      (state<>'closed' AND closed_at IS NULL)
    )
);

CREATE TABLE conversation_messages (
    message_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id         TEXT NOT NULL
                            REFERENCES conversations(conversation_id),
    sender_kind             TEXT NOT NULL
                            CHECK (sender_kind IN ('user','engine','shell')),
    sender_ref              TEXT NOT NULL CHECK (trim(sender_ref) <> ''),
    message_kind            TEXT NOT NULL
                            CHECK (
                              message_kind IN
                                ('prompt','control','result','notice')
                            ),
    body                    TEXT NOT NULL
                            CHECK (length(body) BETWEEN 1 AND 1048576),
    idempotency_key         TEXT NOT NULL
                            CHECK (length(idempotency_key) BETWEEN 1 AND 255),
    request_hash            TEXT NOT NULL
                            CHECK (length(request_hash) BETWEEN 1 AND 256),
    caused_by_message_id    INTEGER
                            REFERENCES conversation_messages(message_id),
    state                   TEXT NOT NULL DEFAULT 'accepted'
                            CHECK (state IN
                                ('accepted','queued','running','completed',
                                 'failed','cancelled')),
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at            TEXT,
    UNIQUE (conversation_id, idempotency_key),
    CHECK (
      (state IN ('completed','failed','cancelled')
       AND completed_at IS NOT NULL)
      OR
      (state IN ('accepted','queued','running')
       AND completed_at IS NULL)
    ),
    CHECK (
      caused_by_message_id IS NULL
      OR caused_by_message_id <> message_id
    )
);

CREATE TABLE conversation_runs (
    run_id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id         TEXT NOT NULL
                            REFERENCES conversations(conversation_id),
    shell_id                INTEGER NOT NULL REFERENCES shells(shell_id),
    trigger_message_id      INTEGER NOT NULL
                            REFERENCES conversation_messages(message_id),
    attempt                 INTEGER NOT NULL DEFAULT 1 CHECK (attempt > 0),
    harness_session_before  TEXT,
    harness_session_after   TEXT,
    runner_ref              TEXT,
    state                   TEXT NOT NULL DEFAULT 'leased'
                            CHECK (state IN
                                ('leased','starting','running','succeeded',
                                 'failed','cancelled','unknown')),
    lease_owner             TEXT NOT NULL CHECK (trim(lease_owner) <> ''),
    lease_expires_at        TEXT NOT NULL,
    started_at              TEXT,
    heartbeat_at            TEXT,
    ended_at                TEXT,
    exit_code               INTEGER,
    error_code              TEXT,
    error_detail            TEXT
                            CHECK (
                              error_detail IS NULL
                              OR length(error_detail) <= 16384
                            ),
    archive_id              INTEGER
                            REFERENCES shell_memory_archives(archive_id),
    UNIQUE (trigger_message_id, attempt),
    CHECK (
      (state='leased' AND started_at IS NULL AND ended_at IS NULL)
      OR
      (state IN ('starting','running')
       AND started_at IS NOT NULL AND ended_at IS NULL)
      OR
      (state IN ('succeeded','failed','cancelled','unknown')
       AND ended_at IS NOT NULL)
    )
);

CREATE TABLE conversation_events (
    event_id                INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id         TEXT NOT NULL
                            REFERENCES conversations(conversation_id),
    sequence                INTEGER NOT NULL CHECK (sequence > 0),
    event_type              TEXT NOT NULL CHECK (trim(event_type) <> ''),
    payload_version         INTEGER NOT NULL DEFAULT 1
                            CHECK (payload_version > 0),
    payload                 TEXT NOT NULL DEFAULT '{}'
                            CHECK (
                              json_valid(payload)
                              AND json_type(payload)='object'
                              AND length(payload) <= 262144
                            ),
    message_id              INTEGER
                            REFERENCES conversation_messages(message_id),
    run_id                  INTEGER REFERENCES conversation_runs(run_id),
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (conversation_id, sequence)
);

CREATE TABLE conversation_outbox (
    outbox_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id         TEXT NOT NULL
                            REFERENCES conversations(conversation_id),
    message_id              INTEGER NOT NULL UNIQUE
                            REFERENCES conversation_messages(message_id),
    state                   TEXT NOT NULL DEFAULT 'pending'
                            CHECK (
                              state IN
                                ('pending','claimed','dispatched','cancelled')
                            ),
    claim_owner             TEXT,
    claimed_at              TEXT,
    lease_expires_at        TEXT,
    attempts                INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    run_id                  INTEGER UNIQUE REFERENCES conversation_runs(run_id),
    last_error              TEXT
                            CHECK (
                              last_error IS NULL
                              OR length(last_error) <= 16384
                            ),
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    dispatched_at           TEXT,
    CHECK (
      (state='pending'
       AND claim_owner IS NULL
       AND claimed_at IS NULL
       AND lease_expires_at IS NULL
       AND run_id IS NULL
       AND dispatched_at IS NULL)
      OR
      (state='claimed'
       AND trim(COALESCE(claim_owner,'')) <> ''
       AND claimed_at IS NOT NULL
       AND lease_expires_at IS NOT NULL
       AND run_id IS NULL
       AND dispatched_at IS NULL)
      OR
      (state='dispatched'
       AND trim(COALESCE(claim_owner,'')) <> ''
       AND claimed_at IS NOT NULL
       AND run_id IS NOT NULL
       AND dispatched_at IS NOT NULL)
      OR
      (state='cancelled'
       AND run_id IS NULL
       AND dispatched_at IS NULL)
    )
);

-- Optimistic idempotency: the API compares the stored request hash when a key
-- already exists. The unique key itself is the DB backstop against duplicates.
CREATE UNIQUE INDEX idx_conversations_idempotency
    ON conversations(owner_user_id, creation_idempotency_key);

CREATE INDEX idx_conversations_shell_activity
    ON conversations(shell_id, last_activity_at, conversation_id);
CREATE INDEX idx_conversation_messages_queue
    ON conversation_messages(conversation_id, state, message_id);

-- Exactly one mutating run per conversation and per shell work surface.
CREATE UNIQUE INDEX idx_conversation_runs_live_conversation
    ON conversation_runs(conversation_id)
    WHERE state IN ('leased','starting','running');
CREATE UNIQUE INDEX idx_conversation_runs_live_shell
    ON conversation_runs(shell_id)
    WHERE state IN ('leased','starting','running');
CREATE INDEX idx_conversation_runs_recovery
    ON conversation_runs(state, lease_expires_at, run_id);

CREATE INDEX idx_conversation_events_replay
    ON conversation_events(conversation_id, sequence);
CREATE INDEX idx_conversation_outbox_claim
    ON conversation_outbox(state, lease_expires_at, outbox_id);

-- Identity and routing fields are immutable. Rename/close/session capture use
-- the explicitly mutable title, state, timestamps, version, and session ref.
CREATE TRIGGER trg_conversations_identity_immutable
BEFORE UPDATE OF
    conversation_id, shell_id, owner_user_id,
    harness, provider, model, effort, worktree,
    creation_idempotency_key, creation_request_hash, created_at
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
    -- closed is terminal.
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
    -- completed, failed, and cancelled are terminal.
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
    -- terminal run states have no exits.
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
    -- dispatched and cancelled are terminal.
)
BEGIN
  SELECT RAISE(ABORT, 'illegal conversation outbox transition');
END;

COMMIT;
