-- 0029 — db_map skill: remove "grants via snapshot" from skills/shell_skills write rule
--
-- The skills row was left with "catalogue via migration; grants via snapshot" in the
-- Write rule column after 0028, leaking admin-only snapshot awareness to shells.
-- Replace with "managed by engine" — opaque to non-admin shells.

BEGIN;

UPDATE skills SET content = replace(
    content,
    'catalogue via migration; grants via snapshot',
    'managed by engine'
) WHERE name = 'db_map' AND is_deleted = 0;

COMMIT;
