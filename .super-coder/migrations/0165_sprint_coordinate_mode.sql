-- 0165 — tracked Sprint coordinate mode.
--
-- Coordinate mode is an operator choice, not a property derived from whether
-- the Planner currently has an open chat.  It therefore lives on the Sprint
-- and survives automatic pause/resume cycles until an FnB lifecycle action
-- explicitly clears it.

BEGIN;

ALTER TABLE sprints
  ADD COLUMN coordinate_mode INTEGER NOT NULL DEFAULT 0
  CHECK (coordinate_mode IN (0,1));

COMMIT;
