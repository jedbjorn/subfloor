-- Feature #54 / spec #177 — live-native route binding contract v3.
--
-- V3 pins exact OpenCode/DeepSeek model and optional native-option identity
-- without catalogue generation, evidence digest, captured harness version, or
-- source fingerprint. Existing v2 binding JSON, digests, provenance, revisions,
-- and active pointers copy verbatim; immutable history is never rewritten.

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
         AND harness IN ('claude','codex','deepseek','kimi','opencode')
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
         AND (
           (harness='deepseek' AND transport IN
             ('deepseek-provider-options-v1','deepseek-stock-host-v1'))
           OR
           (harness<>'deepseek' AND transport=CASE harness
             WHEN 'claude' THEN 'claude-effort-argument'
             WHEN 'codex' THEN 'codex-reasoning-config'
             WHEN 'kimi' THEN 'kimi-effort-environment'
             WHEN 'opencode' THEN 'opencode-route-agent'
           END)
         )
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
       AND harness IN ('deepseek','opencode')
       AND requested_model IS NOT NULL
       AND trim(requested_model)<>''
       AND requested_model=trim(requested_model)
       AND provider_model IS NOT NULL
       AND trim(provider_model)<>''
       AND provider_model=trim(provider_model)
       AND requested_effort IS native_option_id
       AND effective_effort IS native_option_id
       AND native_variant_id IS NULL
       AND transport=CASE harness
         WHEN 'deepseek' THEN 'deepseek-stock-host-v1'
         WHEN 'opencode' THEN 'opencode-route-agent'
       END
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
       NULL,
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

COMMIT;
