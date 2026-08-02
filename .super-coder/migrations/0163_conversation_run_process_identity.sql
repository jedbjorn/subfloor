-- 0163 — durable conversation-run process identity and reaper ladder.
--
-- Process identity belongs on the run row as recovery evidence.  The active
-- chat registry remains the protection authority; these columns let the
-- reaper identify and escalate an unlinked turn after its broker disappears.

BEGIN;

ALTER TABLE conversation_runs
ADD COLUMN process_pid INTEGER
    CHECK (process_pid IS NULL OR process_pid > 0);

ALTER TABLE conversation_runs
ADD COLUMN process_start_ticks INTEGER
    CHECK (process_start_ticks IS NULL OR process_start_ticks >= 0);

ALTER TABLE conversation_runs
ADD COLUMN process_group_id INTEGER
    CHECK (process_group_id IS NULL OR process_group_id > 0);

ALTER TABLE conversation_runs
ADD COLUMN reaper_last_signal TEXT
    CHECK (
      reaper_last_signal IS NULL
      OR reaper_last_signal IN ('interrupt','SIGTERM','SIGKILL')
    );

ALTER TABLE conversation_runs
ADD COLUMN reaper_signaled_at TEXT;

CREATE TRIGGER trg_conversation_runs_process_identity_insert
BEFORE INSERT ON conversation_runs
WHEN NOT (
  (NEW.process_pid IS NULL
   AND NEW.process_start_ticks IS NULL
   AND NEW.process_group_id IS NULL)
  OR
  (NEW.process_pid IS NOT NULL
   AND NEW.process_start_ticks IS NOT NULL
   AND NEW.process_group_id IS NOT NULL)
)
BEGIN
  SELECT RAISE(ABORT, 'conversation run process identity must be complete');
END;

CREATE TRIGGER trg_conversation_runs_process_identity_update
BEFORE UPDATE OF process_pid,process_start_ticks,process_group_id
ON conversation_runs
WHEN NOT (
  (NEW.process_pid IS NULL
   AND NEW.process_start_ticks IS NULL
   AND NEW.process_group_id IS NULL)
  OR
  (NEW.process_pid IS NOT NULL
   AND NEW.process_start_ticks IS NOT NULL
   AND NEW.process_group_id IS NOT NULL)
)
BEGIN
  SELECT RAISE(ABORT, 'conversation run process identity must be complete');
END;

CREATE TRIGGER trg_conversation_runs_reaper_signal_insert
BEFORE INSERT ON conversation_runs
WHEN (NEW.reaper_last_signal IS NULL) <> (NEW.reaper_signaled_at IS NULL)
BEGIN
  SELECT RAISE(ABORT, 'conversation run reaper signal must have a timestamp');
END;

CREATE TRIGGER trg_conversation_runs_reaper_signal_update
BEFORE UPDATE OF reaper_last_signal,reaper_signaled_at ON conversation_runs
WHEN (NEW.reaper_last_signal IS NULL) <> (NEW.reaper_signaled_at IS NULL)
BEGIN
  SELECT RAISE(ABORT, 'conversation run reaper signal must have a timestamp');
END;

CREATE INDEX idx_conversation_runs_reaper
    ON conversation_runs(state,process_pid,run_id)
    WHERE process_pid IS NOT NULL;

COMMIT;
