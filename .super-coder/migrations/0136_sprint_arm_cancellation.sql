-- 0136 — Planner arm and operator cancellation contract.
--
-- A Sprint is armed by its recorded Planner. The browser has no activation
-- authority; its one lifecycle mutation is a durable cancellation request.
-- Cancellation is operational immediately, while the originating Planner
-- retains sole authority to finish the abort report and terminal transition.

BEGIN;

CREATE TABLE sprint_cancellations (
    cancellation_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sprint_doc_id            INTEGER NOT NULL UNIQUE
                             REFERENCES sprints(sprint_doc_id),
    requested_by_user_id     INTEGER NOT NULL REFERENCES users(user_id),
    reason                   TEXT NOT NULL
                             CHECK (length(trim(reason)) BETWEEN 1 AND 2000),
    source_directive_id      INTEGER NOT NULL UNIQUE
                             REFERENCES directives(directive_id),
    planner_conversation_id  TEXT NOT NULL UNIQUE
                             REFERENCES conversations(conversation_id),
    state                    TEXT NOT NULL DEFAULT 'requested'
                             CHECK (state IN ('requested','completed')),
    abort_report             TEXT
                             CHECK (
                               abort_report IS NULL
                               OR length(trim(abort_report))
                                  BETWEEN 1 AND 1048576
                             ),
    completed_by_shell_id    INTEGER REFERENCES shells(shell_id),
    requested_at             TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at             TEXT,
    CHECK (
      (state='requested'
       AND abort_report IS NULL
       AND completed_by_shell_id IS NULL
       AND completed_at IS NULL)
      OR
      (state='completed'
       AND trim(COALESCE(abort_report,'')) <> ''
       AND completed_by_shell_id IS NOT NULL
       AND completed_at IS NOT NULL)
    )
);

CREATE INDEX idx_sprint_cancellations_state
    ON sprint_cancellations(state, requested_at, cancellation_id);

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
    AND sp.state IN ('declared','active')
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

CREATE TRIGGER trg_sprint_cancellation_identity_immutable
BEFORE UPDATE OF
    cancellation_id, sprint_doc_id, requested_by_user_id, reason,
    source_directive_id, planner_conversation_id, requested_at
ON sprint_cancellations
BEGIN
  SELECT RAISE(ABORT, 'Sprint cancellation identity is immutable');
END;

CREATE TRIGGER trg_sprint_cancellation_state
BEFORE UPDATE OF state ON sprint_cancellations
WHEN NEW.state<>OLD.state AND NOT (
  OLD.state='requested' AND NEW.state='completed'
)
BEGIN
  SELECT RAISE(ABORT, 'illegal Sprint cancellation transition');
END;

CREATE TRIGGER trg_sprint_cancellation_completion
BEFORE UPDATE OF state ON sprint_cancellations
WHEN NEW.state='completed' AND OLD.state='requested' AND NOT EXISTS (
  SELECT 1
  FROM sprints sp
  JOIN shells planner ON planner.shell_id=NEW.completed_by_shell_id
  WHERE sp.sprint_doc_id=NEW.sprint_doc_id
    AND sp.planner_shell_id=NEW.completed_by_shell_id
    AND planner.flavor='planner'
    AND COALESCE(planner.is_deleted,0)=0
)
BEGIN
  SELECT RAISE(
    ABORT,
    'Sprint cancellation must be completed by its originating Planner'
  );
END;

CREATE TRIGGER trg_sprint_cancellation_terminal_immutable
BEFORE UPDATE OF
    state, abort_report, completed_by_shell_id, completed_at
ON sprint_cancellations
WHEN OLD.state='completed'
BEGIN
  SELECT RAISE(ABORT, 'completed Sprint cancellation is immutable');
END;

CREATE TRIGGER trg_sprint_cancellation_delete
BEFORE DELETE ON sprint_cancellations
BEGIN
  SELECT RAISE(ABORT, 'Sprint cancellations are durable history');
END;

INSERT OR IGNORE INTO directive_kinds (issuer_flavor,kind)
VALUES ('system','cancel');

UPDATE directives
SET status='refused',
    refusal_reason='Planner handoff retired; the originating Planner arms with sc sprint arm',
    executed_at=datetime('now')
WHERE issuer_flavor='planner'
  AND kind='handoff'
  AND status='pending';

-- Cancelling a queued conversation after cancelling its outbox intent must be
-- able to release the conversation back to idle. No native run crossed the
-- dispatch boundary in this edge.
DROP TRIGGER trg_conversations_state;
CREATE TRIGGER trg_conversations_state
BEFORE UPDATE OF state ON conversations
WHEN NEW.state <> OLD.state AND NOT (
    (OLD.state='idle'    AND NEW.state IN ('queued','closed')) OR
    (OLD.state='queued'  AND NEW.state IN ('idle','running')) OR
    (OLD.state='running' AND NEW.state IN
        ('idle','queued','waiting','error')) OR
    (OLD.state='waiting' AND NEW.state IN ('queued','closed')) OR
    (OLD.state='error'   AND NEW.state IN ('queued','closed'))
    -- closed is terminal.
)
BEGIN
  SELECT RAISE(ABORT, 'illegal conversation transition');
END;

COMMIT;
