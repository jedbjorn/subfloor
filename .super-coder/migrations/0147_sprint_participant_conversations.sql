-- 0147 — Sprints v2 participant conversation topology.
--
-- Generic browser conversations remain the one durable harness-session owner.
-- Sprint linkage and current/persistent pointers stay in the Sprint domain.
-- The scope discriminator preserves the normal-chat one-open-row rule while
-- allowing one participant's persistent work lane and fresh outcome lanes to
-- coexist.  The existing conversation_runs live-shell index still permits
-- only one mutating native run on a shell at a time.

BEGIN;

ALTER TABLE conversations
ADD COLUMN conversation_scope TEXT NOT NULL DEFAULT 'normal'
    CHECK (conversation_scope IN ('normal','sprint'));

DROP INDEX idx_conversations_live_shell;
CREATE UNIQUE INDEX idx_conversations_live_normal_shell
    ON conversations(shell_id)
    WHERE state<>'closed' AND conversation_scope='normal';

CREATE TRIGGER trg_conversations_scope_immutable
BEFORE UPDATE OF conversation_scope ON conversations
BEGIN
  SELECT RAISE(ABORT, 'conversation scope is immutable');
END;

CREATE TABLE sprint_participant_conversations (
    participant_conversation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sprint_participant_id       INTEGER NOT NULL
                                REFERENCES sprint_participants(participant_id),
    conversation_id             TEXT NOT NULL UNIQUE
                                REFERENCES conversations(conversation_id),
    purpose                     TEXT NOT NULL
                                CHECK (purpose IN
                                  ('work','fix','merge','fallback')),
    parent_conversation_id      TEXT REFERENCES conversations(conversation_id),
    context_packet              TEXT
                                CHECK (
                                  context_packet IS NULL OR (
                                    json_valid(context_packet)
                                    AND json_type(context_packet)='object'
                                  )
                                ),
    created_at                  TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (
      (purpose='work' AND parent_conversation_id IS NULL)
      OR
      (purpose<>'work' AND parent_conversation_id IS NOT NULL)
    ),
    CHECK (
      (purpose='fallback' AND context_packet IS NOT NULL)
      OR
      (purpose<>'fallback' AND context_packet IS NULL)
    ),
    CHECK (
      parent_conversation_id IS NULL
      OR parent_conversation_id<>conversation_id
    )
);

CREATE UNIQUE INDEX idx_sprint_participant_persistent_work
    ON sprint_participant_conversations(sprint_participant_id)
    WHERE purpose='work';
CREATE INDEX idx_sprint_participant_conversation_history
    ON sprint_participant_conversations(
      sprint_participant_id, created_at, participant_conversation_id
    );

CREATE TRIGGER trg_sprint_participant_conversation_insert
BEFORE INSERT ON sprint_participant_conversations
WHEN NOT EXISTS (
    SELECT 1
    FROM sprint_participants p
    JOIN conversations c ON c.conversation_id=NEW.conversation_id
    WHERE p.participant_id=NEW.sprint_participant_id
      AND p.shell_id=c.shell_id
      AND c.conversation_scope='sprint'
) OR (
    NEW.parent_conversation_id IS NOT NULL
    AND NOT EXISTS (
      SELECT 1
      FROM sprint_participant_conversations parent
      WHERE parent.sprint_participant_id=NEW.sprint_participant_id
        AND parent.conversation_id=NEW.parent_conversation_id
    )
)
BEGIN
  SELECT RAISE(ABORT, 'invalid Sprint participant conversation link');
END;

CREATE TRIGGER trg_sprint_participant_conversations_immutable_update
BEFORE UPDATE ON sprint_participant_conversations
BEGIN
  SELECT RAISE(ABORT, 'Sprint participant conversation links are immutable');
END;
CREATE TRIGGER trg_sprint_participant_conversations_immutable_delete
BEFORE DELETE ON sprint_participant_conversations
BEGIN
  SELECT RAISE(ABORT, 'Sprint participant conversation links are immutable');
END;

CREATE TRIGGER trg_sprint_participant_conversation_pointers_insert
BEFORE INSERT ON sprint_participants
WHEN NEW.persistent_conversation_id IS NOT NULL
  OR NEW.current_conversation_id IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'Sprint conversation pointers are selected after linkage');
END;

CREATE TRIGGER trg_sprint_participant_conversation_pointers_update
BEFORE UPDATE OF persistent_conversation_id,current_conversation_id
ON sprint_participants
WHEN (
    NEW.persistent_conversation_id IS NOT NULL
    AND NOT EXISTS (
      SELECT 1 FROM sprint_participant_conversations persistent
      WHERE persistent.sprint_participant_id=NEW.participant_id
        AND persistent.conversation_id=NEW.persistent_conversation_id
        AND persistent.purpose='work'
    )
) OR (
    NEW.current_conversation_id IS NOT NULL
    AND NOT EXISTS (
      SELECT 1 FROM sprint_participant_conversations current_link
      WHERE current_link.sprint_participant_id=NEW.participant_id
        AND current_link.conversation_id=NEW.current_conversation_id
    )
)
BEGIN
  SELECT RAISE(ABORT, 'invalid Sprint participant conversation pointer');
END;

COMMIT;
