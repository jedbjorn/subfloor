-- 0276 — native wake quiet.
-- Bind the existing force-new quiet interval to an exact native observation.
-- Existing legacy wake rows retain their quiet behavior and identity.

BEGIN;

ALTER TABLE sprint_wake_outbox ADD COLUMN native_quiet_signature TEXT;

COMMIT;
