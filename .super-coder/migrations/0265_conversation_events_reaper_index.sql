-- 0265 — conversation events reaper index.
--
-- The conversation reaper asks, per terminal run that still carries a process
-- identity, whether it already wrote its outcome event. conversation_events
-- had no run_id index, so each probe scanned every event row; on a long-lived
-- engine one sweep cost minutes of CPU. The partial index holds only the
-- reaper's own outcome rows, so it stays tiny and each probe is a seek.
-- Idempotent.

BEGIN;

CREATE INDEX IF NOT EXISTS idx_conversation_events_reaper
    ON conversation_events(run_id, event_type)
    WHERE event_type IN ('run.interrupted','run.reaped');

COMMIT;
