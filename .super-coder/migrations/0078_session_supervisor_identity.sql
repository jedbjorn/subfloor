-- 0078 — distinguish live supervisor cleanup from an orphaned harness group.
--
-- The harness process remains the fenced conversation owner and process-group
-- leader.  These nullable identity fields record the engine supervisor that
-- is responsible for reaping that group after the leader exits, so the
-- dispatcher does not misclassify a healthy shutdown as a #439 orphan race.

BEGIN;

ALTER TABLE shell_session_bindings ADD COLUMN supervisor_pid INTEGER;
ALTER TABLE shell_session_bindings ADD COLUMN supervisor_start_ticks INTEGER;

COMMIT;
