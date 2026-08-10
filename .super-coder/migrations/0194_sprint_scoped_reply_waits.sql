-- 0194 — structured Sprint relay intent, scope, and reply linkage.

ALTER TABLE wake_message
  ADD COLUMN intent TEXT NOT NULL DEFAULT 'information'
  CHECK (intent IN ('information','handoff','question','blocker','decision'));

ALTER TABLE wake_message
  ADD COLUMN requires_reply INTEGER NOT NULL DEFAULT 0
  CHECK (
    requires_reply IN (0,1)
    AND (
      requires_reply=0
      OR intent IN ('question','blocker','decision')
    )
  );

ALTER TABLE wake_message
  ADD COLUMN reply_to_message_id INTEGER REFERENCES wake_message(message_id);

CREATE INDEX idx_wake_message_replies
  ON wake_message(sprint_id, reply_to_message_id, message_id);
