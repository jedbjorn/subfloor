-- 0168 — retire Sprint conversation pointers and topology.
--
-- The active-chat registry is the only current-chat authority.  Sprint links
-- remain as immutable history, but no longer classify work/fix/merge/fallback
-- lanes or carry parent/context topology.  The one-open-chat invariant is now
-- universal instead of exempting conversation_scope='sprint'.

-- migrate: foreign-keys-off
PRAGMA foreign_keys=OFF;

BEGIN;

DROP TRIGGER IF EXISTS trg_sprint_participant_conversation_pointers_insert;
DROP TRIGGER IF EXISTS trg_sprint_participant_conversation_pointers_update;
DROP TRIGGER IF EXISTS trg_sprint_participant_conversation_insert;
DROP TRIGGER IF EXISTS trg_sprint_participant_conversations_immutable_update;
DROP TRIGGER IF EXISTS trg_sprint_participant_conversations_immutable_delete;
DROP INDEX IF EXISTS idx_sprint_participant_persistent_work;
DROP INDEX IF EXISTS idx_sprint_participant_conversation_history;

ALTER TABLE sprint_participants DROP COLUMN persistent_conversation_id;
ALTER TABLE sprint_participants DROP COLUMN current_conversation_id;

ALTER TABLE sprint_participant_conversations
  RENAME TO _sprint_participant_conversations_topology;

CREATE TABLE sprint_participant_conversations (
    participant_conversation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sprint_participant_id       INTEGER NOT NULL
                                REFERENCES sprint_participants(participant_id),
    conversation_id             TEXT NOT NULL UNIQUE
                                REFERENCES conversations(conversation_id),
    created_at                  TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO sprint_participant_conversations (
    participant_conversation_id,
    sprint_participant_id,
    conversation_id,
    created_at
)
SELECT participant_conversation_id,
       sprint_participant_id,
       conversation_id,
       created_at
FROM _sprint_participant_conversations_topology;

DROP TABLE _sprint_participant_conversations_topology;

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

DROP INDEX IF EXISTS idx_conversations_live_normal_shell;
CREATE UNIQUE INDEX idx_conversations_one_open_shell
    ON conversations(shell_id)
    WHERE state<>'closed';

COMMIT;

PRAGMA foreign_keys=ON;
