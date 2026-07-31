-- 0059 — typed generic shell messages.
--
-- shell_messages.kind makes generic instruction and result traffic filterable:
--     'shell'    — ordinary shell-to-shell mail (default; every existing row
--                  and writer stays valid)
--     'task'     — planner → worker instruction
--     'result'   — worker → planner completion report
-- Migration-only ADD COLUMN follows the 0047 precedent: rebuild applies this
-- ordered delta after schema.sql, so the baseline carries only a pointer.

BEGIN;

ALTER TABLE shell_messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'shell'
  CHECK (kind IN ('shell','task','result'));

COMMIT;
