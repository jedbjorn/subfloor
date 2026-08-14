-- System-managed runtime advisories share the Flags projection without gaining
-- blocker semantics.  Existing human-authored rows retain their original
-- defaults and behavior; lifecycle rows carry exact keyed generations and a
-- structured evidence payload.

ALTER TABLE flags ADD COLUMN source_kind TEXT;
ALTER TABLE flags ADD COLUMN source_key TEXT;
ALTER TABLE flags ADD COLUMN source_generation INTEGER;
ALTER TABLE flags ADD COLUMN evidence_digest TEXT;
ALTER TABLE flags ADD COLUMN management_state TEXT NOT NULL DEFAULT 'human'
    CHECK (management_state IN ('human','system'));
ALTER TABLE flags ADD COLUMN severity TEXT NOT NULL DEFAULT 'tracker'
    CHECK (severity IN ('tracker','advisory'));
ALTER TABLE flags ADD COLUMN blocking_scope TEXT NOT NULL DEFAULT 'feature'
    CHECK (blocking_scope IN ('feature','none'));
ALTER TABLE flags ADD COLUMN blocks_runtime INTEGER NOT NULL DEFAULT 1
    CHECK (blocks_runtime IN (0,1));
ALTER TABLE flags ADD COLUMN source_payload TEXT;

CREATE UNIQUE INDEX idx_flags_open_source_key
    ON flags(source_key)
    WHERE source_key IS NOT NULL AND resolved=0 AND COALESCE(is_deleted,0)=0;
CREATE INDEX idx_flags_source_generation
    ON flags(source_key, source_generation DESC);

CREATE TRIGGER flags_system_advisory_insert
BEFORE INSERT ON flags
WHEN NEW.management_state='system' AND (
    NEW.source_kind IS NULL OR NEW.source_key IS NULL OR
    NEW.source_generation IS NULL OR NEW.source_generation < 1 OR
    NEW.evidence_digest IS NULL OR NEW.source_payload IS NULL OR
    NEW.severity != 'advisory' OR NEW.blocking_scope != 'none' OR
    NEW.blocks_runtime != 0 OR NEW.feature_id IS NOT NULL
)
BEGIN
    SELECT RAISE(ABORT, 'system-managed flags must be non-blocking keyed advisories');
END;

CREATE TRIGGER flags_system_advisory_update
BEFORE UPDATE ON flags
WHEN NEW.management_state='system' AND (
    NEW.source_kind IS NULL OR NEW.source_key IS NULL OR
    NEW.source_generation IS NULL OR NEW.source_generation < 1 OR
    NEW.evidence_digest IS NULL OR NEW.source_payload IS NULL OR
    NEW.severity != 'advisory' OR NEW.blocking_scope != 'none' OR
    NEW.blocks_runtime != 0 OR NEW.feature_id IS NOT NULL
)
BEGIN
    SELECT RAISE(ABORT, 'system-managed flags must remain non-blocking keyed advisories');
END;
