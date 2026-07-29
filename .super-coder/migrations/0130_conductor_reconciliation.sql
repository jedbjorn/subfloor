-- 0130 — same-invocation Conductor provisioning and exact skill boundary.
--
-- An installed fork runs its OLD update.py while materializing a new engine.
-- The new Python reconciliation hook therefore cannot execute until a second
-- command. Migrations are the exception: the old updater reads the newly
-- materialized migration directory in the same invocation. This bridge makes
-- that first update converge, then persistent guards stop the old updater's
-- blanket common-skill regrant from re-polluting the opt-out Conductor pack.

BEGIN;

-- Idempotent direct replays temporarily lower only this migration's own pack
-- guards, then restore them after the exact pack is reconciled.
DROP TRIGGER IF EXISTS trg_conductor_skill_pack_insert;

CREATE TEMP TABLE IF NOT EXISTS _conductor_reconcile_assert (
    value INTEGER NOT NULL CHECK (value = 0)
);

-- Refuse ambiguous or identity-bearing operational shells. Seed and lineage
-- are sovereign; migration must never delete them to force a role-only shape.
INSERT INTO _conductor_reconcile_assert(value)
SELECT CASE WHEN COUNT(*) > 1 THEN 1 ELSE 0 END
FROM shells
WHERE flavor = 'conductor' AND is_deleted = 0;

INSERT INTO _conductor_reconcile_assert(value)
SELECT CASE WHEN EXISTS (
    SELECT 1
    FROM shells sh
    WHERE sh.flavor = 'conductor'
      AND sh.is_deleted = 0
      AND (
          sh.has_identity <> 0
          OR sh.lineage_seed IS NOT NULL
          OR EXISTS (
              SELECT 1 FROM shell_identity_entries sie
              WHERE sie.shell_id = sh.shell_id
          )
      )
) THEN 1 ELSE 0 END;

INSERT INTO _conductor_reconcile_assert(value)
SELECT CASE WHEN EXISTS (
    SELECT 1 FROM shells
    WHERE shortname = 'CON1'
      AND is_deleted = 0
      AND flavor <> 'conductor'
) THEN 1 ELSE 0 END;

DELETE FROM _conductor_reconcile_assert;
DROP TABLE _conductor_reconcile_assert;

INSERT INTO shells (
    display_name,
    shortname,
    partner,
    role,
    mandate,
    system_prompt,
    current_state,
    connections,
    flavor,
    has_identity,
    bootstrapped,
    user_id,
    is_shared,
    api_key,
    api_key_rotated_at
)
SELECT
    'Conductor',
    'CON1',
    (SELECT username FROM users WHERE user_id = 1),
    'Conductor shell',
    'Run active sprints mechanically from durable directives; never decide.',
    '# Conductor — mechanical sprint relay',
    'Created (conductor). First session — run the bootstrap skill to orient.',
    'Single repo: this one. One shell, one cwd.',
    'conductor',
    0,
    0,
    1,
    0,
    lower(hex(randomblob(32))),
    datetime('now')
WHERE EXISTS (
    SELECT 1 FROM users WHERE user_id = 1
)
  AND NOT EXISTS (
    SELECT 1 FROM shells
    WHERE flavor = 'conductor' AND is_deleted = 0
);

UPDATE shells
SET shortname = 'CON1',
    api_key = COALESCE(api_key, lower(hex(randomblob(32)))),
    api_key_rotated_at = CASE
        WHEN api_key IS NULL THEN datetime('now')
        ELSE api_key_rotated_at
    END
WHERE flavor = 'conductor' AND is_deleted = 0;

INSERT INTO shell_memory_archives (
    shell_id, session_id, date, full_narrative
)
SELECT shell_id, '0001', CURRENT_DATE, ''
FROM shells
WHERE flavor = 'conductor'
  AND is_deleted = 0
  AND active_archive_id IS NULL;

UPDATE shells
SET active_archive_id = (
    SELECT MAX(a.archive_id)
    FROM shell_memory_archives a
    WHERE a.shell_id = shells.shell_id
)
WHERE flavor = 'conductor'
  AND is_deleted = 0
  AND active_archive_id IS NULL;

DELETE FROM shell_skills
WHERE shell_id IN (
    SELECT shell_id FROM shells
    WHERE flavor = 'conductor' AND is_deleted = 0
);

DELETE FROM flavor_skills WHERE flavor = 'conductor';

INSERT INTO flavor_skills (flavor, skill_id)
SELECT 'conductor', skill_id
FROM skills
WHERE name = 'sprint_cond' AND is_deleted = 0;

CREATE TRIGGER IF NOT EXISTS trg_singleton_conductor
BEFORE INSERT ON shells
WHEN NEW.flavor = 'conductor' AND NEW.is_deleted = 0 AND (
    SELECT COUNT(*) FROM shells
    WHERE flavor = 'conductor' AND is_deleted = 0
) >= 1
BEGIN
    SELECT RAISE(
        ABORT,
        'conductor is a singleton — this fork already has one'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_conductor_skill_pack_insert
BEFORE INSERT ON flavor_skills
WHEN NEW.flavor = 'conductor' AND NEW.skill_id NOT IN (
    SELECT skill_id FROM skills
    WHERE name = 'sprint_cond' AND is_deleted = 0
)
BEGIN
    SELECT RAISE(IGNORE);
END;

COMMIT;
