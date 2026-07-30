-- 0135 — Sprint conversation identity and assignment concurrency floor.
--
-- Migration 0132 reserved one live Sprint conversation per Sprint. Browser
-- orchestration needs a narrower fence: one persistent Conductor conversation
-- plus fresh one-shot Planner, Developer, Reviewer, and conformance
-- conversations. Keep the hot conversations table intact and bind Sprint
-- identity in this additive child table.
--
-- Existing reserved Sprint conversations are preserved but not guessed into a
-- role. The activation/assignment API owns explicit binding in a short write
-- transaction; an inferred Conductor or worker would be unsafe.

BEGIN;

DROP INDEX idx_conversations_live_sprint;

CREATE TABLE sprint_conversation_bindings (
    binding_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id        TEXT NOT NULL UNIQUE
                           REFERENCES conversations(conversation_id),
    sprint_doc_id          INTEGER NOT NULL
                           REFERENCES sprints(sprint_doc_id),
    role                   TEXT NOT NULL
                           CHECK (role IN (
                             'conductor','planner','developer','reviewer',
                             'conformance'
                           )),
    lifecycle              TEXT NOT NULL
                           CHECK (lifecycle IN ('persistent','one_shot')),
    slot                   TEXT NOT NULL
                           CHECK (length(trim(slot)) BETWEEN 1 AND 64),
    unit_id                INTEGER REFERENCES sprint_units(unit_id),
    source_directive_id    INTEGER REFERENCES directives(directive_id),
    source_message_id      INTEGER REFERENCES shell_messages(message_id),
    required_result_kind   TEXT
                           CHECK (
                             required_result_kind IS NULL
                             OR length(trim(required_result_kind))
                                BETWEEN 1 AND 64
                           ),
    state                  TEXT NOT NULL DEFAULT 'pending'
                           CHECK (state IN ('pending','active','terminal')),
    outcome                TEXT
                           CHECK (outcome IN (
                             'succeeded','failed','cancelled','unknown','closed'
                           )),
    result_message_id      INTEGER REFERENCES shell_messages(message_id),
    created_at             TEXT NOT NULL DEFAULT (datetime('now')),
    started_at             TEXT,
    completed_at           TEXT,
    CHECK (
      (role='conductor' AND lifecycle='persistent')
      OR
      (role<>'conductor' AND lifecycle='one_shot')
    ),
    CHECK (
      (role='conductor'
       AND unit_id IS NULL
       AND source_directive_id IS NULL
       AND source_message_id IS NULL
       AND required_result_kind IS NULL)
      OR
      (role<>'conductor'
       AND source_directive_id IS NOT NULL
       AND trim(COALESCE(required_result_kind,'')) <> '')
    ),
    CHECK (
      (role IN ('developer','reviewer') AND unit_id IS NOT NULL)
      OR
      (role='conformance' AND unit_id IS NULL)
      OR
      role IN ('conductor','planner')
    ),
    CHECK (
      (state='pending'
       AND outcome IS NULL
       AND started_at IS NULL
       AND completed_at IS NULL
       AND result_message_id IS NULL)
      OR
      (state='active'
       AND outcome IS NULL
       AND started_at IS NOT NULL
       AND completed_at IS NULL
       AND result_message_id IS NULL)
      OR
      (state='terminal'
       AND outcome IS NOT NULL
       AND completed_at IS NOT NULL
       AND (
         (outcome='succeeded' AND result_message_id IS NOT NULL)
         OR
         (outcome<>'succeeded' AND result_message_id IS NULL)
       ))
    ),
    CHECK (
      (role='conductor' AND (outcome IS NULL OR outcome='closed'))
      OR
      (role<>'conductor' AND (outcome IS NULL OR outcome<>'closed'))
    )
);

-- A Sprint can retain closed Conductor history, but only one non-terminal
-- Conductor may own it. Worker histories do not collide with this fence.
CREATE UNIQUE INDEX idx_sprint_conversation_live_conductor
    ON sprint_conversation_bindings(sprint_doc_id)
    WHERE role='conductor' AND state<>'terminal';

-- A committed directive names one assignment identity. A retry may recover the
-- existing row; it may not create another native session for the same role and
-- slot, even after the first assignment becomes terminal.
CREATE UNIQUE INDEX idx_sprint_conversation_assignment_source
    ON sprint_conversation_bindings(source_directive_id, role, slot)
    WHERE lifecycle='one_shot';

CREATE INDEX idx_sprint_conversation_bindings_state
    ON sprint_conversation_bindings(sprint_doc_id, state, binding_id);
CREATE INDEX idx_sprint_conversation_bindings_unit
    ON sprint_conversation_bindings(sprint_doc_id, unit_id, role, binding_id);

CREATE TRIGGER trg_sprint_conversation_binding_links_insert
BEFORE INSERT ON sprint_conversation_bindings
WHEN NOT EXISTS (
  SELECT 1
  FROM conversations c
  JOIN shells s ON s.shell_id=c.shell_id
  WHERE c.conversation_id=NEW.conversation_id
    AND c.mode='sprint'
    AND c.sprint_doc_id=NEW.sprint_doc_id
    AND COALESCE(s.is_deleted,0)=0
    AND s.shortname=NEW.slot
    AND (
      s.flavor=NEW.role
      OR (NEW.role='developer' AND s.flavor='dev')
      OR (NEW.role='conformance' AND s.flavor='reviewer')
    )
)
BEGIN
  SELECT RAISE(
    ABORT,
    'Sprint binding conversation, Sprint, role, or shell does not match'
  );
END;

CREATE TRIGGER trg_sprint_conversation_binding_unit_insert
BEFORE INSERT ON sprint_conversation_bindings
WHEN NEW.unit_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM sprint_units u
  WHERE u.unit_id=NEW.unit_id
    AND u.sprint_doc_id=NEW.sprint_doc_id
)
BEGIN
  SELECT RAISE(ABORT, 'Sprint binding unit does not belong to Sprint');
END;

CREATE TRIGGER trg_sprint_conversation_binding_source_insert
BEFORE INSERT ON sprint_conversation_bindings
WHEN NEW.source_directive_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM directives d
  WHERE d.directive_id=NEW.source_directive_id
    AND d.sprint_doc_id=NEW.sprint_doc_id
    AND d.unit_id IS NEW.unit_id
)
BEGIN
  SELECT RAISE(ABORT, 'Sprint binding source directive does not match');
END;

CREATE TRIGGER trg_sprint_conversation_binding_message_insert
BEFORE INSERT ON sprint_conversation_bindings
WHEN NEW.source_message_id IS NOT NULL AND NOT EXISTS (
  SELECT 1
  FROM shell_messages m
  JOIN conversations c ON c.conversation_id=NEW.conversation_id
  WHERE m.message_id=NEW.source_message_id
    AND m.sprint_doc_id=NEW.sprint_doc_id
    AND m.to_shell_id=c.shell_id
)
BEGIN
  SELECT RAISE(ABORT, 'Sprint binding source message does not match');
END;

CREATE TRIGGER trg_sprint_conversation_binding_result_insert
BEFORE INSERT ON sprint_conversation_bindings
WHEN NEW.result_message_id IS NOT NULL AND NOT EXISTS (
  SELECT 1
  FROM shell_messages m
  JOIN conversations c ON c.conversation_id=NEW.conversation_id
  WHERE m.message_id=NEW.result_message_id
    AND m.sprint_doc_id=NEW.sprint_doc_id
    AND m.kind='result'
    AND m.from_shell_id=c.shell_id
)
BEGIN
  SELECT RAISE(ABORT, 'Sprint binding result message does not match');
END;

CREATE TRIGGER trg_sprint_conversation_binding_identity_immutable
BEFORE UPDATE OF
    binding_id, conversation_id, sprint_doc_id, role, lifecycle, slot, unit_id,
    source_directive_id, source_message_id, required_result_kind, created_at
ON sprint_conversation_bindings
BEGIN
  SELECT RAISE(ABORT, 'Sprint conversation binding identity is immutable');
END;

CREATE TRIGGER trg_sprint_conversation_binding_state
BEFORE UPDATE OF state ON sprint_conversation_bindings
WHEN NEW.state<>OLD.state AND NOT (
  (OLD.state='pending' AND NEW.state IN ('active','terminal'))
  OR
  (OLD.state='active' AND NEW.state='terminal')
)
BEGIN
  SELECT RAISE(ABORT, 'illegal Sprint conversation binding transition');
END;

CREATE TRIGGER trg_sprint_conversation_binding_terminal_insert
BEFORE INSERT ON sprint_conversation_bindings
WHEN NEW.state='terminal' AND NOT EXISTS (
  SELECT 1 FROM conversations c
  WHERE c.conversation_id=NEW.conversation_id
    AND c.state='closed'
)
BEGIN
  SELECT RAISE(ABORT, 'terminal Sprint binding requires closed conversation');
END;

CREATE TRIGGER trg_sprint_conversation_binding_terminal_update
BEFORE UPDATE OF state ON sprint_conversation_bindings
WHEN NEW.state='terminal' AND OLD.state<>'terminal' AND NOT EXISTS (
  SELECT 1 FROM conversations c
  WHERE c.conversation_id=NEW.conversation_id
    AND c.state='closed'
)
BEGIN
  SELECT RAISE(ABORT, 'terminal Sprint binding requires closed conversation');
END;

CREATE TRIGGER trg_sprint_conversation_binding_terminal_immutable
BEFORE UPDATE OF outcome, result_message_id, started_at, completed_at
ON sprint_conversation_bindings
WHEN OLD.state='terminal' AND (
  NEW.outcome IS NOT OLD.outcome
  OR NEW.result_message_id IS NOT OLD.result_message_id
  OR NEW.started_at IS NOT OLD.started_at
  OR NEW.completed_at IS NOT OLD.completed_at
)
BEGIN
  SELECT RAISE(ABORT, 'terminal Sprint conversation binding is immutable');
END;

CREATE TRIGGER trg_sprint_conversation_binding_delete
BEFORE DELETE ON sprint_conversation_bindings
BEGIN
  SELECT RAISE(ABORT, 'Sprint conversation bindings are durable history');
END;

CREATE TRIGGER trg_sprint_conversation_binding_result_update
BEFORE UPDATE OF result_message_id ON sprint_conversation_bindings
WHEN NEW.result_message_id IS NOT NULL AND NOT EXISTS (
  SELECT 1
  FROM shell_messages m
  JOIN conversations c ON c.conversation_id=NEW.conversation_id
  WHERE m.message_id=NEW.result_message_id
    AND m.sprint_doc_id=NEW.sprint_doc_id
    AND m.kind='result'
    AND m.from_shell_id=c.shell_id
)
BEGIN
  SELECT RAISE(ABORT, 'Sprint binding result message does not match');
END;

COMMIT;
