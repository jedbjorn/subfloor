-- 0143 — remove the Sprint runtime roles and their current skill packs.
--
-- Task #169 removes executable Sprint/Conductor code while task #170 owns the
-- later schema and historical-migration excision. This data-only cutover keeps
-- both fresh rebuilds and installed forks from exposing a role whose runtime no
-- longer exists. The rows stay as deleted catalogue history until #170 removes
-- the retired schema inputs.

BEGIN;

DELETE FROM shell_skills
WHERE skill_id IN (
    SELECT skill_id
    FROM skills
    WHERE name IN (
        'sprint_cond',
        'sprint_dev',
        'sprint_onboarding',
        'sprint_pln',
        'sprint_rev'
    )
);

DELETE FROM flavor_skills
WHERE flavor = 'conductor'
   OR skill_id IN (
       SELECT skill_id
       FROM skills
       WHERE name IN (
           'sprint_cond',
           'sprint_dev',
           'sprint_onboarding',
           'sprint_pln',
           'sprint_rev'
       )
   );

UPDATE skills
SET is_deleted = 1
WHERE name IN (
    'sprint_cond',
    'sprint_dev',
    'sprint_onboarding',
    'sprint_pln',
    'sprint_rev'
);

DELETE FROM flavor_defaults
WHERE flavor = 'conductor';

UPDATE shells
SET is_deleted = 1
WHERE flavor = 'conductor'
  AND is_deleted = 0;

COMMIT;
