-- Closed browser chats become re-enterable.  Selecting a closed chat and
-- sending a message reopens it: the API walks closed -> idle (clearing
-- closed_at in the same UPDATE to satisfy the state/closed_at CHECK), appends
-- a conversation.reopened event, then queues the message normally so the next
-- turn resumes the preserved harness_session_ref.  Only the closed -> idle
-- edge is new; every other edge stays exactly as migration 0144 rebuilt it.
-- conversation_state.py mirrors this map in lockstep, and every reader of
-- conversation.close.requested events scopes to sequences after the latest
-- conversation.reopened event so a pre-reopen close request cannot strand or
-- re-close a reopened chat.
BEGIN;

DROP TRIGGER trg_conversations_state;
CREATE TRIGGER trg_conversations_state
BEFORE UPDATE OF state ON conversations
WHEN NEW.state <> OLD.state AND NOT (
    (OLD.state='idle'    AND NEW.state IN ('queued','closed')) OR
    (OLD.state='queued'  AND NEW.state IN ('idle','running')) OR
    (OLD.state='running' AND NEW.state IN
        ('idle','queued','waiting','error')) OR
    (OLD.state='waiting' AND NEW.state IN ('queued','closed')) OR
    (OLD.state='error'   AND NEW.state IN ('queued','closed')) OR
    (OLD.state='closed'  AND NEW.state='idle')
)
BEGIN
  SELECT RAISE(ABORT, 'illegal conversation transition');
END;

COMMIT;
