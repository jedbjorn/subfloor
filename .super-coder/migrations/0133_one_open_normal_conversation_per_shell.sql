-- One browser conversation owns a shell until Close or same-shell replacement.
-- Older builds allowed several idle rows. Preserve the most recently active one
-- and close older rows before installing the durable per-shell uniqueness fence.

BEGIN;

UPDATE conversations AS older
SET state='closed',
    closed_at=COALESCE(closed_at, datetime('now')),
    last_activity_at=datetime('now'),
    version=version+1
WHERE mode='normal'
  AND state<>'closed'
  AND EXISTS (
      SELECT 1
      FROM conversations AS newer
      WHERE newer.mode='normal'
        AND newer.state<>'closed'
        AND newer.shell_id=older.shell_id
        AND (
            newer.last_activity_at > older.last_activity_at
            OR (
                newer.last_activity_at = older.last_activity_at
                AND newer.conversation_id > older.conversation_id
            )
        )
  );

CREATE UNIQUE INDEX idx_conversations_live_normal_shell
    ON conversations(shell_id)
    WHERE mode='normal' AND state<>'closed';

COMMIT;
