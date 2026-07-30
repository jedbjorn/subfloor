-- 0137 — allow one directive to release a different unit in the same Sprint.
--
-- A completed U1 can mechanically release dependency-ready U2.  Migration
-- 0135 correctly required both the source directive and assignment unit to
-- belong to the binding's Sprint, but incorrectly required them to be the
-- same unit.  Keep both Sprint fences while allowing that cross-unit handoff.

BEGIN;

DROP TRIGGER trg_sprint_conversation_binding_source_insert;

CREATE TRIGGER trg_sprint_conversation_binding_source_insert
BEFORE INSERT ON sprint_conversation_bindings
WHEN NEW.source_directive_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM directives d
  WHERE d.directive_id=NEW.source_directive_id
    AND d.sprint_doc_id=NEW.sprint_doc_id
)
BEGIN
  SELECT RAISE(ABORT, 'Sprint binding source directive does not match');
END;

COMMIT;
