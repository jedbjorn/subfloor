-- Feature #54 / spec #177 — persist live-native conversation contract v3.
--
-- Existing conversation bytes and lifecycle state copy unchanged.  The only
-- expanded invariant is route_contract_version: exact OpenCode/DeepSeek v3
-- bindings are now admitted alongside legacy v1 and canonical v2 rows.

-- migrate: foreign-keys-off
PRAGMA foreign_keys=OFF;

BEGIN;

DROP TRIGGER IF EXISTS trg_active_shell_chats_insert;
DROP TRIGGER IF EXISTS trg_active_shell_chats_update;
DROP TRIGGER IF EXISTS trg_conversation_runs_links_insert;
DROP TRIGGER IF EXISTS trg_sprint_participant_conversation_insert;

CREATE TABLE _flavor_defaults_live_native (
    flavor     TEXT NOT NULL,
    harness    TEXT NOT NULL,
    model      TEXT,
    is_default INTEGER NOT NULL DEFAULT 0,
    effort     TEXT CHECK (
      effort IS NULL OR (
        trim(effort)<>''
        AND effort=trim(effort)
        AND (
          harness IN ('deepseek','opencode')
          OR effort=lower(effort)
        )
      )
    ),
    PRIMARY KEY (flavor, harness)
);

INSERT INTO _flavor_defaults_live_native (
    flavor,harness,model,is_default,effort
)
SELECT flavor,harness,model,is_default,effort
FROM flavor_defaults;

DROP TABLE flavor_defaults;
ALTER TABLE _flavor_defaults_live_native RENAME TO flavor_defaults;

CREATE TABLE _conversations_live_native_v3 (
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
    conversation_scope       TEXT NOT NULL DEFAULT 'normal'
                             CHECK (conversation_scope IN ('normal','sprint')),
    route_contract_version   INTEGER NOT NULL DEFAULT 1
                             CHECK (route_contract_version IN (1,2,3)),
    route_binding            TEXT CHECK (
                               route_binding IS NULL OR (
                                 json_valid(route_binding)
                                 AND json_type(route_binding)='object'
                               )
                             ),
    CHECK (
      (state='closed' AND closed_at IS NOT NULL)
      OR
      (state<>'closed' AND closed_at IS NULL)
    )
);

INSERT INTO _conversations_live_native_v3 (
    conversation_id,
    shell_id,
    owner_user_id,
    harness,
    provider,
    model,
    effort,
    worktree,
    harness_session_ref,
    state,
    title,
    creation_idempotency_key,
    creation_request_hash,
    created_at,
    last_activity_at,
    closed_at,
    version,
    starred,
    conversation_scope,
    route_contract_version,
    route_binding
)
SELECT conversation_id,
       shell_id,
       owner_user_id,
       harness,
       provider,
       model,
       effort,
       worktree,
       harness_session_ref,
       state,
       title,
       creation_idempotency_key,
       creation_request_hash,
       created_at,
       last_activity_at,
       closed_at,
       version,
       starred,
       conversation_scope,
       route_contract_version,
       route_binding
FROM conversations;

DROP TABLE conversations;
ALTER TABLE _conversations_live_native_v3 RENAME TO conversations;

CREATE UNIQUE INDEX idx_conversations_idempotency
    ON conversations(owner_user_id, creation_idempotency_key);
CREATE INDEX idx_conversations_shell_activity
    ON conversations(shell_id, last_activity_at, conversation_id);
CREATE UNIQUE INDEX idx_conversations_one_open_shell
    ON conversations(shell_id)
    WHERE state<>'closed';

CREATE TRIGGER trg_conversations_identity_immutable
BEFORE UPDATE OF
    conversation_id, shell_id, owner_user_id, harness, provider, model, effort,
    worktree, creation_idempotency_key, creation_request_hash, created_at
ON conversations
BEGIN
  SELECT RAISE(ABORT, 'conversation identity and route are immutable');
END;

CREATE TRIGGER trg_conversations_scope_immutable
BEFORE UPDATE OF conversation_scope ON conversations
BEGIN
  SELECT RAISE(ABORT, 'conversation scope is immutable');
END;

CREATE TRIGGER trg_conversations_state
BEFORE UPDATE OF state ON conversations
WHEN NEW.state <> OLD.state AND NOT (
    (OLD.state='idle'    AND NEW.state IN ('queued','closed')) OR
    (OLD.state='queued'  AND NEW.state IN ('idle','running')) OR
    (OLD.state='running' AND NEW.state IN
        ('idle','queued','waiting','error')) OR
    (OLD.state='waiting' AND NEW.state IN ('queued','closed')) OR
    (OLD.state='error'   AND NEW.state IN ('queued','closed')) OR
    (OLD.state='closed'  AND NEW.state='idle')
)
BEGIN
  SELECT RAISE(ABORT, 'illegal conversation transition');
END;

CREATE TRIGGER trg_conversations_clear_active_chat
AFTER UPDATE OF state ON conversations
WHEN NEW.state='closed' AND OLD.state<>'closed'
BEGIN
  DELETE FROM active_shell_chats WHERE chat_id=NEW.conversation_id;
END;

CREATE TRIGGER conversations_route_contract_insert
BEFORE INSERT ON conversations
WHEN (NEW.route_contract_version=1 AND NEW.route_binding IS NOT NULL)
  OR (NEW.route_contract_version IN (2,3) AND NEW.route_binding IS NULL)
BEGIN
  SELECT RAISE(ABORT, 'conversation route contract and binding disagree');
END;

CREATE TRIGGER conversations_route_contract_update
BEFORE UPDATE OF route_contract_version,route_binding ON conversations
WHEN (NEW.route_contract_version=1 AND NEW.route_binding IS NOT NULL)
  OR (NEW.route_contract_version IN (2,3) AND NEW.route_binding IS NULL)
BEGIN
  SELECT RAISE(ABORT, 'conversation route contract and binding disagree');
END;

CREATE TRIGGER conversations_route_identity_immutable
BEFORE UPDATE OF harness,provider,model,effort,
                 route_contract_version,route_binding ON conversations
WHEN NEW.harness IS NOT OLD.harness
  OR NEW.provider IS NOT OLD.provider
  OR NEW.model IS NOT OLD.model
  OR NEW.effort IS NOT OLD.effort
  OR NEW.route_contract_version IS NOT OLD.route_contract_version
  OR NEW.route_binding IS NOT OLD.route_binding
BEGIN
  SELECT RAISE(ABORT, 'conversation route identity is immutable');
END;

CREATE TRIGGER trg_active_shell_chats_insert
BEFORE INSERT ON active_shell_chats
WHEN NOT EXISTS (
    SELECT 1 FROM conversations c
    WHERE c.conversation_id=NEW.chat_id
      AND c.shell_id=NEW.shell_id
      AND c.state<>'closed'
)
BEGIN
  SELECT RAISE(ABORT, 'active chat must be open and belong to its shell');
END;

CREATE TRIGGER trg_active_shell_chats_update
BEFORE UPDATE OF shell_id,chat_id ON active_shell_chats
WHEN NOT EXISTS (
    SELECT 1 FROM conversations c
    WHERE c.conversation_id=NEW.chat_id
      AND c.shell_id=NEW.shell_id
      AND c.state<>'closed'
)
BEGIN
  SELECT RAISE(ABORT, 'active chat must be open and belong to its shell');
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

CREATE TRIGGER trg_sprint_participant_conversation_insert
BEFORE INSERT ON sprint_participant_conversations
WHEN NOT EXISTS (
    SELECT 1
    FROM sprint_participants p
    JOIN conversations c ON c.conversation_id=NEW.conversation_id
    WHERE p.participant_id=NEW.sprint_participant_id
      AND p.shell_id=c.shell_id
      AND c.conversation_scope='sprint'
)
BEGIN
  SELECT RAISE(ABORT, 'invalid Sprint participant conversation link');
END;

COMMIT;
