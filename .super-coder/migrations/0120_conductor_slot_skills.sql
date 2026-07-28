-- 0120 — Conductor Step 7 slot skills.
--
-- Installed forks already hold the legacy role skills and their flavor grants.
-- Rename where possible to preserve skill_id/grant identity; a fresh rebuild
-- already has the new rows from 0001, so it takes the conflict-safe retire +
-- grant path instead. `sc update` syncs the regenerated 0001 immediately after
-- migrations, replacing any renamed legacy body with the authored slot body.

BEGIN;

UPDATE skills
SET name='dev_sprint'
WHERE name='sprint_dev'
  AND NOT EXISTS (SELECT 1 FROM skills WHERE name='dev_sprint');

UPDATE skills
SET name='plan_sprint'
WHERE name='sprint_orchestration'
  AND NOT EXISTS (SELECT 1 FROM skills WHERE name='plan_sprint');

UPDATE skills
SET name='rev_sprint'
WHERE name='sprint_review'
  AND NOT EXISTS (SELECT 1 FROM skills WHERE name='rev_sprint');

-- Fresh rebuilds replay historical skill migrations after 0001, so the old
-- names exist alongside the new catalogue until this ordered retirement.
UPDATE skills
SET is_deleted=1
WHERE name IN (
    'sprint_dev',
    'sprint_review',
    'sprint_orchestration',
    'sprint_orchestration_recover',
    'sprint_orchestration_close'
);

DELETE FROM flavor_skills
WHERE skill_id IN (
    SELECT skill_id FROM skills
    WHERE name IN (
        'sprint_dev',
        'sprint_review',
        'sprint_orchestration',
        'sprint_orchestration_recover',
        'sprint_orchestration_close'
    )
);

WITH slot_grants(flavor, skill_name) AS (
    VALUES
        ('planner', 'plan_sprint'),
        ('dev', 'dev_sprint'),
        ('reviewer', 'rev_sprint')
)
INSERT OR IGNORE INTO flavor_skills (flavor, skill_id)
SELECT g.flavor, s.skill_id
FROM slot_grants g
JOIN skills s ON s.name=g.skill_name
WHERE s.is_deleted=0;

COMMIT;
