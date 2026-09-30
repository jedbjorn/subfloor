-- 0272 — conversation hot query indexes.
-- Release-fixture EXPLAIN QUERY PLAN evidence: close/reopen probes need an
-- event-type seek, message pages need message-id order, stream secrets need a
-- conversation run seek, and accepted queue receipts need sequence order.
-- Minimal set: the message-page index also serves the transcript prompt window;
-- drop candidate conversation_messages(conversation_id, message_kind, message_id).
-- Replace candidate conversation_events(message_id, event_type) with the
-- sequence suffix: the two-column candidate still needs a temporary sort.
-- Transcript windows share descending order and only the bounded result is
-- sorted in Python; stream secrets use UNION ALL and are deduplicated in Python.
-- Drop idx_conversation_events_replay: UNIQUE(conversation_id, sequence) already
-- supplies its replay and watermark plans, with no INDEXED BY references.

BEGIN;

CREATE INDEX IF NOT EXISTS idx_conversation_events_conversation_type_sequence
    ON conversation_events(conversation_id, event_type, sequence);

CREATE INDEX IF NOT EXISTS idx_conversation_messages_conversation_message
    ON conversation_messages(conversation_id, message_id);

CREATE INDEX IF NOT EXISTS idx_conversation_runs_conversation_run
    ON conversation_runs(conversation_id, run_id);

CREATE INDEX IF NOT EXISTS idx_conversation_events_message_type_sequence
    ON conversation_events(message_id, event_type, sequence);

DROP INDEX IF EXISTS idx_conversation_events_replay;

COMMIT;
