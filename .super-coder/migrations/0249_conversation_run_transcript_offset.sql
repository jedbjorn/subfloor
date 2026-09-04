-- 0249 — durable native transcript adoption evidence.
--
-- The native session file is the channel the CLI writes regardless of the
-- stdout pipe.  Recording its path and the byte offset at spawn lets a broker
-- that lost the pipe tail exactly this turn's output instead of reconciling
-- the whole session as unknown.
--
-- The reaper now also sweeps terminal runs that still name a process, so the
-- one-time backfill drops process identity from runs whose outcome is already
-- proven.  ``unknown`` rows keep theirs: their process may still be alive and
-- the reaper is the authority that ends it.

BEGIN;

ALTER TABLE conversation_runs
ADD COLUMN transcript_path TEXT;

ALTER TABLE conversation_runs
ADD COLUMN transcript_offset INTEGER
    CHECK (transcript_offset IS NULL OR transcript_offset >= 0);

UPDATE conversation_runs
SET process_pid=NULL,
    process_start_ticks=NULL,
    process_group_id=NULL
WHERE state IN ('succeeded','failed','cancelled')
  AND process_pid IS NOT NULL;

COMMIT;
