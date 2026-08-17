-- Feature #54 / spec #149 — versioned route-binding foundation.
--
-- Catalogue rows are installation-local runtime evidence and remain outside
-- snapshots.  Participant bindings are durable Sprint truth and therefore
-- are included in snapshot.py's Sprint table set.

BEGIN;

CREATE TABLE model_catalog_generations (
    generation_id       TEXT PRIMARY KEY
                        CHECK (length(generation_id)=32
                          AND generation_id NOT GLOB '*[^0-9a-f]*'),
    payload_version     INTEGER NOT NULL,
    contract_version    INTEGER NOT NULL DEFAULT 2 CHECK (contract_version=2),
    started_at          TEXT NOT NULL,
    completed_at        TEXT NOT NULL,
    state               TEXT NOT NULL CHECK (state IN ('successful','failed')),
    runtime             TEXT NOT NULL CHECK (runtime IN ('host','sandbox')),
    source_summary      TEXT NOT NULL CHECK (json_valid(source_summary)),
    harness_versions    TEXT NOT NULL CHECK (json_valid(harness_versions)),
    source_fingerprints TEXT NOT NULL CHECK (json_valid(source_fingerprints)),
    error_summary       TEXT CHECK (error_summary IS NULL OR json_valid(error_summary)),
    payload_digest      TEXT NOT NULL
                        CHECK (length(payload_digest)=64
                          AND payload_digest NOT GLOB '*[^0-9a-f]*')
);

CREATE INDEX idx_model_catalog_generations_latest
    ON model_catalog_generations(state, completed_at DESC, generation_id DESC);

ALTER TABLE model_routes ADD COLUMN generation_id TEXT;
ALTER TABLE model_routes ADD COLUMN evidence_kind TEXT;
ALTER TABLE model_routes ADD COLUMN evidence_digest TEXT;
ALTER TABLE model_routes ADD COLUMN source_fingerprint TEXT;
ALTER TABLE model_routes ADD COLUMN harness_version TEXT;
ALTER TABLE model_routes ADD COLUMN harness_compatibility TEXT CHECK (
    harness_compatibility IS NULL OR
    harness_compatibility IN ('verified','supported'));
ALTER TABLE model_routes ADD COLUMN selector_binding TEXT;
ALTER TABLE model_routes ADD COLUMN effort_metadata TEXT;
ALTER TABLE model_routes ADD COLUMN adapter_metadata TEXT;

CREATE INDEX idx_model_routes_generation
    ON model_routes(generation_id, harness, selector);

CREATE TABLE sprint_participant_route_bindings (
    binding_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    participant_id       INTEGER NOT NULL
                         REFERENCES sprint_participants(participant_id),
    route_revision       INTEGER NOT NULL CHECK (route_revision > 0),
    contract_version     INTEGER NOT NULL CHECK (contract_version=2),
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
    UNIQUE (participant_id, route_revision),
    CHECK (
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
       AND transport=CASE harness
         WHEN 'claude' THEN 'claude-effort-argument'
         WHEN 'codex' THEN 'codex-reasoning-config'
         WHEN 'kimi' THEN 'kimi-effort-environment'
         WHEN 'opencode' THEN 'opencode-route-agent'
       END
       AND catalogue_generation IS NOT NULL
       AND length(catalogue_generation)=32
       AND catalogue_generation NOT GLOB '*[^0-9a-f]*'
       AND evidence_digest IS NOT NULL
       AND length(evidence_digest)=64
       AND evidence_digest NOT GLOB '*[^0-9a-f]*'
       AND selector_binding IS NOT NULL
       AND json(selector_binding)<>'{}'
       AND ((harness='opencode' AND native_variant_id IS NOT NULL
             AND native_variant_id=requested_effort)
         OR (harness<>'opencode' AND native_variant_id IS NULL)))
      OR
      (control_state='harness-default'
       AND harness IN ('claude','codex','kimi','opencode','vibe')
       AND requested_model IS NULL
       AND provider_model IS NULL
       AND requested_effort IS NULL
       AND effective_effort IS NULL
       AND native_variant_id IS NULL
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
       AND catalogue_generation IS NULL
       AND evidence_digest IS NULL
       AND selector_binding IS NULL
       AND json(adapter_metadata)='{}'
       AND transport='native-default')
    )
);

CREATE INDEX idx_sprint_participant_route_bindings_participant
    ON sprint_participant_route_bindings(participant_id, route_revision DESC);

ALTER TABLE sprint_participants ADD COLUMN active_route_binding_id INTEGER
    REFERENCES sprint_participant_route_bindings(binding_id);

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
