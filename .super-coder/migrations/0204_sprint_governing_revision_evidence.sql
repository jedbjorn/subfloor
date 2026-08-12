-- Preserve exact governing Sprint bodies while keeping legacy drift explicit.
-- The migration runner performs the SHA-256-verified legacy backfill inside
-- this migration's transaction before it records the ledger stamp.

ALTER TABLE sprint_specs ADD COLUMN bound_revision_body TEXT;
-- Legacy snapshots do not name either new column.  Default them into the
-- one-time backfill lane; current declaration code explicitly writes 0.
ALTER TABLE sprint_specs ADD COLUMN bound_revision_legacy INTEGER NOT NULL DEFAULT 1
    CHECK (bound_revision_legacy IN (0,1));

CREATE TABLE governing_revision_backfill_permits (
    sprint_id INTEGER NOT NULL,
    document_id INTEGER NOT NULL,
    PRIMARY KEY (sprint_id, document_id),
    FOREIGN KEY (sprint_id, document_id)
        REFERENCES sprint_specs(sprint_id, document_id) ON DELETE CASCADE
);

CREATE TRIGGER trg_sprint_specs_bound_revision_required
BEFORE INSERT ON sprint_specs
WHEN NEW.bound_revision_body IS NULL
 AND NEW.bound_revision_legacy=0
 AND EXISTS (
   SELECT 1 FROM schema_migrations
   WHERE filename='0204_sprint_governing_revision_evidence.sql'
 )
BEGIN
  SELECT RAISE(ABORT, 'new Sprint bindings require an immutable governing body');
END;

CREATE TRIGGER trg_sprint_specs_bound_revision_immutable
BEFORE UPDATE OF
  bound_revision_sha256, bound_revision_body, bound_revision_legacy
ON sprint_specs
WHEN EXISTS (
   SELECT 1 FROM schema_migrations
   WHERE filename='0204_sprint_governing_revision_evidence.sql'
 )
 AND NOT (
   OLD.bound_revision_legacy=1
   AND OLD.bound_revision_body IS NULL
   AND NEW.bound_revision_legacy=1
   AND NEW.bound_revision_sha256 IS OLD.bound_revision_sha256
   AND NEW.bound_revision_body IS NOT NULL
   AND EXISTS (
     SELECT 1 FROM governing_revision_backfill_permits p
     WHERE p.sprint_id=OLD.sprint_id AND p.document_id=OLD.document_id
   )
 )
 AND (
   NEW.bound_revision_sha256 IS NOT OLD.bound_revision_sha256
   OR NEW.bound_revision_body IS NOT OLD.bound_revision_body
   OR NEW.bound_revision_legacy IS NOT OLD.bound_revision_legacy
 )
BEGIN
  SELECT RAISE(ABORT, 'Sprint governing revisions are immutable');
END;
