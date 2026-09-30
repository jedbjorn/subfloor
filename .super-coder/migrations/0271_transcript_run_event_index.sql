-- 0271 — transcript run event index.
-- Transcript segmentation probes count evidence and find the last boundary
-- for each displayed run. Without this index, every probe scans all chats'
-- events. Keep the sequence in the index to cover the snapshot watermark.

BEGIN;

CREATE INDEX IF NOT EXISTS idx_conversation_events_run_type_sequence
    ON conversation_events(run_id, event_type, sequence);

COMMIT;
