-- Rebuild route-bearing tables into the current live-native schema.
-- Existing rows copy byte-for-byte.

-- migrate: foreign-keys-off
PRAGMA foreign_keys=OFF;

BEGIN;

DROP TRIGGER IF EXISTS sprint_participant_active_binding_owner_insert;
DROP TRIGGER IF EXISTS sprint_participant_active_binding_owner_update;

CREATE TABLE _sprint_participant_route_bindings_live_native_v3 (
    binding_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    participant_id       INTEGER NOT NULL
                         REFERENCES sprint_participants(participant_id),
    route_revision       INTEGER NOT NULL CHECK (route_revision > 0),
    contract_version     INTEGER NOT NULL CHECK (contract_version IN (2,3)),
    control_state        TEXT NOT NULL
                         CHECK (control_state IN
                           ('controlled','harness-default','native-uncontrolled')),
    harness              TEXT NOT NULL CHECK (
                           trim(harness)<>'' AND harness=lower(trim(harness))),
    requested_model      TEXT,
    provider_model       TEXT,
    requested_effort     TEXT,
    effective_effort     TEXT,
    native_variant_id    TEXT,
    native_option_id     TEXT CHECK (
                           native_option_id IS NULL OR (
                             trim(native_option_id)<>''
                             AND native_option_id=trim(native_option_id)
                           )
                         ),
    transport            TEXT NOT NULL CHECK (trim(transport)<>''),
    catalogue_generation TEXT,
    evidence_digest      TEXT,
    selector_binding     TEXT CHECK (
                           selector_binding IS NULL OR
                           (json_valid(selector_binding)
                            AND json_type(selector_binding)='object')),
    adapter_metadata     TEXT NOT NULL CHECK (
                           json_valid(adapter_metadata)
                           AND json_type(adapter_metadata)='object'),
    binding_json         TEXT NOT NULL CHECK (
                           json_valid(binding_json)
                           AND json_type(binding_json)='object'),
    binding_digest       TEXT NOT NULL
                         CHECK (length(binding_digest)=64
                           AND binding_digest NOT GLOB '*[^0-9a-f]*'),
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    source_fingerprint   TEXT CHECK (
                           source_fingerprint IS NULL OR (
                             length(source_fingerprint)=64
                             AND source_fingerprint NOT GLOB '*[^0-9a-f]*'
                           )
                         ),
    harness_version      TEXT CHECK (
                           harness_version IS NULL OR (
                             trim(harness_version)<>''
                             AND harness_version=trim(harness_version)
                           )
                         ),
    harness_evidence_format TEXT NOT NULL DEFAULT 'legacy-semver' CHECK (
                           harness_evidence_format IN
                             ('legacy-semver','raw-observed-v1','harness-live-v1')
                         ),
    harness_support_state TEXT CHECK (
                           harness_support_state IS NULL OR
                           harness_support_state IN ('tested','best-effort')
                         ),
    UNIQUE (participant_id, route_revision),
    CHECK (
      (contract_version=2 AND (
        (control_state='controlled'
         AND harness IN ('claude','codex','kimi','opencode')
         AND requested_model IS NOT NULL
         AND trim(requested_model)<>''
         AND requested_model=trim(requested_model)
         AND provider_model IS NOT NULL
         AND trim(provider_model)<>''
         AND provider_model=trim(provider_model)
         AND requested_effort IS NOT NULL
         AND trim(requested_effort)<>''
         AND requested_effort=lower(trim(requested_effort))
         AND effective_effort IS NOT NULL
         AND effective_effort=requested_effort
         AND native_option_id IS NULL
         AND transport=CASE harness
           WHEN 'claude' THEN 'claude-effort-argument'
           WHEN 'codex' THEN 'codex-reasoning-config'
           WHEN 'kimi' THEN 'kimi-effort-environment'
           WHEN 'opencode' THEN 'opencode-route-agent'
         END
         AND catalogue_generation IS NOT NULL
         AND length(catalogue_generation)=32
         AND catalogue_generation NOT GLOB '*[^0-9a-f]*'
         AND ((requested_effort='default' AND evidence_digest IS NULL)
           OR (requested_effort<>'default'
             AND evidence_digest IS NOT NULL
             AND length(evidence_digest)=64
             AND evidence_digest NOT GLOB '*[^0-9a-f]*'))
         AND selector_binding IS NOT NULL
         AND json(selector_binding)<>'{}'
         AND ((harness='opencode'
            AND ((requested_effort='default' AND native_variant_id IS NULL)
              OR (requested_effort<>'default'
                AND native_variant_id IS NOT NULL
                AND native_variant_id=requested_effort)))
           OR (harness<>'opencode' AND native_variant_id IS NULL)))
        OR
        (control_state='harness-default'
         AND harness IN ('claude','codex','kimi','opencode','vibe')
         AND requested_model IS NULL
         AND provider_model IS NULL
         AND requested_effort IS NULL
         AND effective_effort IS NULL
         AND native_variant_id IS NULL
         AND native_option_id IS NULL
         AND catalogue_generation IS NULL
         AND evidence_digest IS NULL
         AND selector_binding IS NULL
         AND json(adapter_metadata)='{}'
         AND transport='native-default')
        OR
        (control_state='native-uncontrolled'
         AND harness='vibe'
         AND requested_model IS NOT NULL
         AND trim(requested_model)<>''
         AND requested_model=trim(requested_model)
         AND provider_model IS NULL
         AND requested_effort IS NULL
         AND effective_effort IS NULL
         AND native_variant_id IS NULL
         AND native_option_id IS NULL
         AND catalogue_generation IS NULL
         AND evidence_digest IS NULL
         AND selector_binding IS NULL
         AND json(adapter_metadata)='{}'
         AND transport='native-default')
      ))
      OR
      (contract_version=3
       AND control_state='controlled'
       AND harness='opencode'
       AND requested_model IS NOT NULL
       AND trim(requested_model)<>''
       AND requested_model=trim(requested_model)
       AND provider_model IS NOT NULL
       AND trim(provider_model)<>''
       AND provider_model=trim(provider_model)
       AND requested_effort IS native_option_id
       AND effective_effort IS native_option_id
       AND native_variant_id IS NULL
       AND transport='opencode-route-agent'
       AND catalogue_generation IS NULL
       AND evidence_digest IS NULL
       AND selector_binding IS NOT NULL
       AND json(selector_binding)<>'{}'
       AND json(adapter_metadata)='{}')
    )
);

INSERT INTO _sprint_participant_route_bindings_live_native_v3 (
    binding_id,
    participant_id,
    route_revision,
    contract_version,
    control_state,
    harness,
    requested_model,
    provider_model,
    requested_effort,
    effective_effort,
    native_variant_id,
    native_option_id,
    transport,
    catalogue_generation,
    evidence_digest,
    selector_binding,
    adapter_metadata,
    binding_json,
    binding_digest,
    created_at,
    source_fingerprint,
    harness_version,
    harness_evidence_format,
    harness_support_state
)
SELECT binding_id,
       participant_id,
       route_revision,
       contract_version,
       control_state,
       harness,
       requested_model,
       provider_model,
       requested_effort,
       effective_effort,
       native_variant_id,
       CASE WHEN contract_version=3 THEN requested_effort ELSE NULL END,
       transport,
       catalogue_generation,
       evidence_digest,
       selector_binding,
       adapter_metadata,
       binding_json,
       binding_digest,
       created_at,
       source_fingerprint,
       harness_version,
       harness_evidence_format,
       harness_support_state
FROM sprint_participant_route_bindings;

DROP TABLE sprint_participant_route_bindings;
ALTER TABLE _sprint_participant_route_bindings_live_native_v3
RENAME TO sprint_participant_route_bindings;

CREATE INDEX idx_sprint_participant_route_bindings_participant
    ON sprint_participant_route_bindings(participant_id, route_revision DESC);

CREATE TRIGGER sprint_participant_route_bindings_immutable_update
BEFORE UPDATE ON sprint_participant_route_bindings
BEGIN
  SELECT RAISE(ABORT, 'participant route bindings are immutable');
END;

CREATE TRIGGER sprint_participant_route_bindings_immutable_delete
BEFORE DELETE ON sprint_participant_route_bindings
BEGIN
  SELECT RAISE(ABORT, 'participant route bindings are immutable');
END;

CREATE TRIGGER sprint_participant_binding_provenance_insert
BEFORE INSERT ON sprint_participant_route_bindings
WHEN (NEW.contract_version=2 AND (
        NEW.harness_version IS NULL
        OR (NEW.control_state='controlled' AND NEW.source_fingerprint IS NULL)
        OR (NEW.control_state<>'controlled' AND NEW.source_fingerprint IS NOT NULL)
      ))
  OR (NEW.contract_version=3 AND (
        NEW.source_fingerprint IS NOT NULL
        OR NEW.harness_version IS NOT NULL
      ))
BEGIN
  SELECT RAISE(ABORT, 'participant route binding provenance is invalid');
END;

CREATE TRIGGER sprint_participant_binding_raw_evidence_insert
BEFORE INSERT ON sprint_participant_route_bindings
WHEN (NEW.contract_version=2 AND (
        NEW.harness_evidence_format <> 'raw-observed-v1'
        OR NEW.harness_support_state NOT IN ('tested','best-effort')
      ))
  OR (NEW.contract_version=3 AND (
        NEW.harness_evidence_format <> 'harness-live-v1'
        OR NEW.harness_support_state IS NOT NULL
      ))
BEGIN
  SELECT RAISE(ABORT, 'participant route binding support provenance is invalid');
END;

CREATE TRIGGER sprint_participant_active_binding_owner_insert
BEFORE INSERT ON sprint_participants
WHEN NEW.active_route_binding_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM sprint_participant_route_bindings binding
    WHERE binding.binding_id=NEW.active_route_binding_id
      AND binding.participant_id=NEW.participant_id
  )
BEGIN
  SELECT RAISE(ABORT, 'active route binding belongs to another participant');
END;

CREATE TRIGGER sprint_participant_active_binding_owner_update
BEFORE UPDATE OF active_route_binding_id ON sprint_participants
WHEN NEW.active_route_binding_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM sprint_participant_route_bindings binding
    WHERE binding.binding_id=NEW.active_route_binding_id
      AND binding.participant_id=NEW.participant_id
  )
BEGIN
  SELECT RAISE(ABORT, 'active route binding belongs to another participant');
END;

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
          harness='opencode'
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
