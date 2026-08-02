-- 0162 — active chat registry.
--
-- The registry, rather than a scan of conversations or Sprint participant
-- pointers, is the authority for the one active chat a shell may own.  Existing
-- installs can contain one normal chat plus one or more Sprint chats for the
-- same shell, so the migration preserves the most recently active row and
-- closes every older open row before installing the registry entry.

BEGIN;

CREATE TABLE IF NOT EXISTS active_shell_chats (
    shell_id             INTEGER PRIMARY KEY REFERENCES shells(shell_id)
                         ON DELETE CASCADE,
    chat_id              TEXT NOT NULL UNIQUE REFERENCES conversations(conversation_id)
                         ON DELETE CASCADE,
    process_pid          INTEGER CHECK (process_pid IS NULL OR process_pid > 0),
    process_start_ticks  INTEGER
                         CHECK (
                           process_start_ticks IS NULL
                           OR process_start_ticks >= 0
                         ),
    updated_at           TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (
      (process_pid IS NULL AND process_start_ticks IS NULL)
      OR
      (process_pid IS NOT NULL AND process_start_ticks IS NOT NULL)
    )
);

CREATE TRIGGER IF NOT EXISTS trg_active_shell_chats_insert
BEFORE INSERT ON active_shell_chats
WHEN NOT EXISTS (
    SELECT 1 FROM conversations c
    WHERE c.conversation_id=NEW.chat_id
      AND c.shell_id=NEW.shell_id
      AND c.state<>'closed'
)
BEGIN
  SELECT RAISE(ABORT, 'active chat must be open and belong to its shell');
END;

CREATE TRIGGER IF NOT EXISTS trg_active_shell_chats_update
BEFORE UPDATE OF shell_id,chat_id ON active_shell_chats
WHEN NOT EXISTS (
    SELECT 1 FROM conversations c
    WHERE c.conversation_id=NEW.chat_id
      AND c.shell_id=NEW.shell_id
      AND c.state<>'closed'
)
BEGIN
  SELECT RAISE(ABORT, 'active chat must be open and belong to its shell');
END;

CREATE TRIGGER IF NOT EXISTS trg_conversations_clear_active_chat
AFTER UPDATE OF state ON conversations
WHEN NEW.state='closed' AND OLD.state<>'closed'
BEGIN
  DELETE FROM active_shell_chats WHERE chat_id=NEW.conversation_id;
END;

CREATE TEMP TABLE IF NOT EXISTS _active_shell_chat_seed (
    shell_id INTEGER PRIMARY KEY,
    chat_id  TEXT NOT NULL UNIQUE
);
DELETE FROM _active_shell_chat_seed;

-- Preserve an existing registry choice on an idempotent replay, otherwise
-- choose the newest open chat for each shell.
INSERT OR IGNORE INTO _active_shell_chat_seed (shell_id,chat_id)
SELECT shell_id,chat_id FROM active_shell_chats;
INSERT OR IGNORE INTO _active_shell_chat_seed (shell_id,chat_id)
SELECT c.shell_id,c.conversation_id
FROM conversations c
WHERE c.state<>'closed'
  AND c.conversation_id=(
    SELECT newest.conversation_id
    FROM conversations newest
    WHERE newest.shell_id=c.shell_id AND newest.state<>'closed'
    ORDER BY newest.last_activity_at DESC,newest.rowid DESC
    LIMIT 1
  );

-- Closing a chat makes its queued work permanently unclaimable.  Mirror the
-- runtime close path by cancelling pending/claimed delivery intent first.
UPDATE conversation_messages
SET state='cancelled',completed_at=datetime('now')
WHERE state IN ('accepted','queued','running')
  AND message_id IN (
    SELECT outbox.message_id
    FROM conversation_outbox outbox
    WHERE outbox.state IN ('pending','claimed')
      AND NOT EXISTS (
        SELECT 1 FROM _active_shell_chat_seed seed
        WHERE seed.chat_id=outbox.conversation_id
      )
  );
UPDATE conversation_outbox
SET state='cancelled',claim_owner=NULL,claimed_at=NULL,lease_expires_at=NULL
WHERE state IN ('pending','claimed')
  AND NOT EXISTS (
    SELECT 1 FROM _active_shell_chat_seed seed
    WHERE seed.chat_id=conversation_outbox.conversation_id
  );

-- Walk legal state edges before closing legacy extras.  This keeps the
-- migration compatible with the conversation transition trigger.
UPDATE conversations
SET state='idle'
WHERE state='queued'
  AND NOT EXISTS (
    SELECT 1 FROM _active_shell_chat_seed seed
    WHERE seed.chat_id=conversations.conversation_id
  );
UPDATE conversations
SET state='error'
WHERE state='running'
  AND NOT EXISTS (
    SELECT 1 FROM _active_shell_chat_seed seed
    WHERE seed.chat_id=conversations.conversation_id
  );
UPDATE conversations
SET state='closed',closed_at=COALESCE(closed_at,datetime('now')),
    last_activity_at=datetime('now'),version=version+1
WHERE state<>'closed'
  AND NOT EXISTS (
    SELECT 1 FROM _active_shell_chat_seed seed
    WHERE seed.chat_id=conversations.conversation_id
  );

INSERT OR IGNORE INTO active_shell_chats (shell_id,chat_id)
SELECT shell_id,chat_id FROM _active_shell_chat_seed;
DROP TABLE _active_shell_chat_seed;

COMMIT;
