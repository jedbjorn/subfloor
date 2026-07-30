-- A completed cancellation is serialized after its Sprint has already made
-- the terminal `aborted` transition.  The original insert guard admitted only
-- live Sprint states, so a valid snapshot could not be reconstructed.
DROP TRIGGER IF EXISTS trg_sprint_cancellation_links_insert;
CREATE TRIGGER trg_sprint_cancellation_links_insert
BEFORE INSERT ON sprint_cancellations
WHEN NOT EXISTS (
  SELECT 1
  FROM sprints sp
  JOIN directives d
    ON d.directive_id=NEW.source_directive_id
  JOIN conversations c
    ON c.conversation_id=NEW.planner_conversation_id
  JOIN sprint_conversation_bindings b
    ON b.conversation_id=c.conversation_id
  WHERE sp.sprint_doc_id=NEW.sprint_doc_id
    AND (
      (NEW.state='requested' AND sp.state IN ('declared','active'))
      OR
      (NEW.state='completed' AND sp.state='aborted')
    )
    AND d.sprint_doc_id=NEW.sprint_doc_id
    AND d.issuer_flavor='system'
    AND d.kind='cancel'
    AND d.target='planner'
    AND d.status='executed'
    AND c.mode='sprint'
    AND c.sprint_doc_id=NEW.sprint_doc_id
    AND b.sprint_doc_id=NEW.sprint_doc_id
    AND b.role='planner'
    AND b.lifecycle='one_shot'
    AND b.source_directive_id=NEW.source_directive_id
)
BEGIN
  SELECT RAISE(
    ABORT,
    'Sprint cancellation links must name its executed cancel directive and Planner conversation'
  );
END;
