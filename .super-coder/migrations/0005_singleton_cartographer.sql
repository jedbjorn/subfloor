-- 0005 — Cartographer singleton guard (trg_singleton_cartographer).
--
-- One additive, convergent change — safe both on an existing fork (the trigger
-- is absent → created) and on a fresh rebuild (schema.sql already has it →
-- CREATE … IF NOT EXISTS converges). Matches the 0002/0003/0004 precedent.
--
-- A fork has exactly one cartographer: it owns the repo map and no other shell
-- maps, so a second is incoherent. The trigger blocks INSERT of a 2nd non-deleted
-- cartographer (is_deleted=0 so a deleted one frees the slot). shell_factory
-- pre-checks for a friendly error; this trigger is the DB backstop that also
-- catches direct writes and the GUI `POST /api/shells` path.
--
-- Existing forks already carry exactly one cartographer (init_fork seeds it), so
-- applying this never fires on the incumbent — it only guards the next INSERT.

BEGIN;

CREATE TRIGGER IF NOT EXISTS trg_singleton_cartographer
BEFORE INSERT ON shells
WHEN NEW.flavor = 'cartographer' AND (
  SELECT COUNT(*) FROM shells
  WHERE flavor = 'cartographer' AND is_deleted = 0
) >= 1
BEGIN
  SELECT RAISE(ABORT, 'cartographer is a singleton — this fork already has one');
END;

COMMIT;
