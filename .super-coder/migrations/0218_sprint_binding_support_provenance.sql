-- Feature #54 / spec #149 — preserve legacy binding version encoding while
-- recording raw runtime identity and advisory support for every new revision.
--
-- Existing immutable JSON, digests, wake keys, and binding rows are untouched.
-- Rows created before this migration used a parsed semantic version; new rows
-- must retain the exact observed executable output.

BEGIN;

ALTER TABLE sprint_participant_route_bindings
ADD COLUMN harness_evidence_format TEXT NOT NULL DEFAULT 'legacy-semver' CHECK (
  harness_evidence_format IN ('legacy-semver','raw-observed-v1')
);

ALTER TABLE sprint_participant_route_bindings
ADD COLUMN harness_support_state TEXT CHECK (
  harness_support_state IS NULL OR
  harness_support_state IN ('tested','best-effort')
);

CREATE TRIGGER sprint_participant_binding_raw_evidence_insert
BEFORE INSERT ON sprint_participant_route_bindings
WHEN NEW.harness_evidence_format <> 'raw-observed-v1'
  OR NEW.harness_support_state NOT IN ('tested','best-effort')
BEGIN
  SELECT RAISE(ABORT, 'participant route binding support provenance is invalid');
END;

COMMIT;
