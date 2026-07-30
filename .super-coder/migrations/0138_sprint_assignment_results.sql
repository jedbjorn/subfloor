-- 0138 — correlate one-shot Sprint results with their durable assignment.
--
-- A shell-scoped result message is useful audit evidence, but shell + Sprint
-- identity alone cannot prove which fresh one-shot produced it.  Keep the
-- existing message bus intact and add a narrow, append-only correlation row
-- that names the assignment, typed result, and exact directive returned to
-- Conductor.  Abort-report assignments are the sole exception: their state
-- mutation is the Planner-authorized Sprint abort resource, not a directive.

BEGIN;

CREATE TABLE sprint_assignment_results (
    result_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    binding_id      INTEGER NOT NULL UNIQUE
                    REFERENCES sprint_conversation_bindings(binding_id),
    message_id      INTEGER NOT NULL UNIQUE
                    REFERENCES shell_messages(message_id),
    result_kind     TEXT NOT NULL
                    CHECK (length(trim(result_kind)) BETWEEN 1 AND 64),
    directive_id    INTEGER UNIQUE REFERENCES directives(directive_id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TRIGGER trg_sprint_assignment_result_links_insert
BEFORE INSERT ON sprint_assignment_results
WHEN NOT EXISTS (
  SELECT 1
  FROM sprint_conversation_bindings b
  JOIN conversations c ON c.conversation_id=b.conversation_id
  JOIN shell_messages m ON m.message_id=NEW.message_id
  LEFT JOIN directives d ON d.directive_id=NEW.directive_id
  WHERE b.binding_id=NEW.binding_id
    AND b.lifecycle='one_shot'
    AND b.state='active'
    AND b.required_result_kind=NEW.result_kind
    AND m.kind='result'
    AND m.sprint_doc_id=b.sprint_doc_id
    AND m.from_shell_id=c.shell_id
    AND (
      (
        NEW.result_kind='abort-report'
        AND NEW.directive_id IS NULL
        AND EXISTS (
          SELECT 1 FROM sprint_cancellations cancel
          WHERE cancel.sprint_doc_id=b.sprint_doc_id
            AND cancel.state='completed'
            AND cancel.completed_by_shell_id=c.shell_id
        )
      )
      OR
      (
        NEW.result_kind<>'abort-report'
        AND NEW.directive_id IS NOT NULL
        AND d.sprint_doc_id=b.sprint_doc_id
        AND d.unit_id IS b.unit_id
        AND d.issuer_shell_id=c.shell_id
        AND d.target='conductor'
        AND d.status='pending'
        AND (
          (b.role='planner' AND d.issuer_flavor='planner')
          OR (b.role='developer' AND d.issuer_flavor='dev')
          OR (
            b.role IN ('reviewer','conformance')
            AND d.issuer_flavor='reviewer'
          )
        )
      )
    )
)
BEGIN
  SELECT RAISE(
    ABORT,
    'Sprint assignment result does not match its binding, message, or directive'
  );
END;

CREATE TRIGGER trg_sprint_assignment_results_append_only_update
BEFORE UPDATE ON sprint_assignment_results
BEGIN
  SELECT RAISE(ABORT, 'Sprint assignment results are append-only');
END;

CREATE TRIGGER trg_sprint_assignment_results_append_only_delete
BEFORE DELETE ON sprint_assignment_results
BEGIN
  SELECT RAISE(ABORT, 'Sprint assignment results are append-only');
END;

CREATE INDEX idx_sprint_assignment_results_directive
    ON sprint_assignment_results(directive_id, result_id);

INSERT OR IGNORE INTO directive_kinds (issuer_flavor,kind)
VALUES ('system','worker-failed');

COMMIT;
