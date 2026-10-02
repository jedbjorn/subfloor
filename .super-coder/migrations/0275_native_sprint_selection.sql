-- 0275 — native sprint selection.
-- Existing participants keep legacy execution. Native mode is explicit intent,
-- independent from the canonical provider/model/effort route binding.

BEGIN;

ALTER TABLE sprint_participants ADD COLUMN runtime_mode TEXT NOT NULL DEFAULT 'ephemeral'
    CHECK(runtime_mode IN ('ephemeral','native_experiment'));

CREATE TRIGGER sprint_participant_runtime_mode_prepared_only
BEFORE UPDATE OF runtime_mode ON sprint_participants
WHEN NEW.runtime_mode<>OLD.runtime_mode
 AND (SELECT lifecycle FROM sprints WHERE sprint_id=OLD.sprint_id)<>'prepared'
BEGIN
    SELECT RAISE(ABORT,'Sprint runtime mode may change only while prepared');
END;

COMMIT;
