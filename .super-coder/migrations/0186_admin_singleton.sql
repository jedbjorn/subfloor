-- 0186 — converge every installation on one active Admin identity.
--
-- Historical Admin rows stay in place so archives, messages, and other foreign
-- keys keep their identity. The earliest active Admin is canonical; later
-- duplicates are soft-deleted before the partial unique index makes that state
-- durable across factory, restore, and direct SQL paths.

BEGIN;

UPDATE shells
SET is_deleted = 1
WHERE flavor = 'admin'
  AND COALESCE(is_deleted, 0) = 0
  AND shell_id <> (
    SELECT MIN(shell_id)
    FROM shells
    WHERE flavor = 'admin' AND COALESCE(is_deleted, 0) = 0
  );

CREATE UNIQUE INDEX IF NOT EXISTS idx_shells_one_active_admin
ON shells(flavor)
WHERE flavor = 'admin' AND is_deleted = 0;

COMMIT;
