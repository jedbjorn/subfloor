-- 0087 — metadata-only browser composer state for the planner wake gate.
--
-- The existing composer column describes the harness/tmux composer. A draft
-- in the browser must block planner wake independently: clearing one surface
-- must never certify the other surface clean. No draft bytes are persisted.

ALTER TABLE interface_input_state
ADD COLUMN browser_composer TEXT NOT NULL DEFAULT 'clean'
CHECK (browser_composer IN ('clean', 'dirty'));
