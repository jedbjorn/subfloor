-- 0011 — local_skill_management + migration_management to admin-only
--
-- Both skills shipped in 0010 without common: false in their frontmatter,
-- defaulting to common=1. They should be admin-only (same as self_update).
-- Revoke from non-admin shells; the UPSERT in the skills seed corrects the
-- common flag in the live DB on ./sc update.

BEGIN;

DELETE FROM shell_skills
WHERE skill_id IN (
    SELECT skill_id FROM skills
    WHERE name IN ('local_skill_management', 'migration_management')
      AND is_deleted = 0
  )
  AND shell_id IN (
    SELECT shell_id FROM shells WHERE flavor != 'admin' AND is_deleted = 0
  );

COMMIT;
