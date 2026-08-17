-- Feature #54 / spec #149 — immutable first-turn Sprint route provenance.
--
-- Existing bindings remain readable.  Controlled historical rows without
-- captured provenance fail closed before their first native turn; new arm and
-- reroute writes always populate both values.

BEGIN;

ALTER TABLE sprint_participant_route_bindings
ADD COLUMN source_fingerprint TEXT CHECK (
  source_fingerprint IS NULL OR (
    length(source_fingerprint)=64
    AND source_fingerprint NOT GLOB '*[^0-9a-f]*'
  )
);

ALTER TABLE sprint_participant_route_bindings
ADD COLUMN harness_version TEXT CHECK (
  harness_version IS NULL OR (
    trim(harness_version)<>''
    AND harness_version=trim(harness_version)
  )
);

CREATE TRIGGER sprint_participant_binding_provenance_insert
BEFORE INSERT ON sprint_participant_route_bindings
WHEN NEW.harness_version IS NULL
  OR (NEW.control_state='controlled' AND NEW.source_fingerprint IS NULL)
  OR (NEW.control_state<>'controlled' AND NEW.source_fingerprint IS NOT NULL)
BEGIN
  SELECT RAISE(ABORT, 'participant route binding provenance is invalid');
END;

COMMIT;
