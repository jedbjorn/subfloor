-- Remove authority owned by the permanent upstream skill tombstone registry.
-- Runtime reconciliation repeats this cleanup after loading an older snapshot,
-- because the migration ledger is stamped before per-instance content loads.
BEGIN;

DELETE FROM shell_skills
WHERE skill_id IN (
  SELECT skill_id FROM skills WHERE name IN (
    'dev_sprint',
    'plan_sprint',
    'rev_sprint',
    'sprint',
    'sprint_cond',
    'sprint_onboarding',
    'sprint_orchestration',
    'sprint_orchestration_close',
    'sprint_orchestration_recover',
    'sprint_review',
    'engine_surgery',
    'test_authoring_pg',
    'test_authoring_sqlite'
  )
);

DELETE FROM flavor_skills
WHERE skill_id IN (
  SELECT skill_id FROM skills WHERE name IN (
    'dev_sprint',
    'plan_sprint',
    'rev_sprint',
    'sprint',
    'sprint_cond',
    'sprint_onboarding',
    'sprint_orchestration',
    'sprint_orchestration_close',
    'sprint_orchestration_recover',
    'sprint_review',
    'engine_surgery',
    'test_authoring_pg',
    'test_authoring_sqlite'
  )
);

DELETE FROM skills WHERE name IN (
  'dev_sprint',
  'plan_sprint',
  'rev_sprint',
  'sprint',
  'sprint_cond',
  'sprint_onboarding',
  'sprint_orchestration',
  'sprint_orchestration_close',
  'sprint_orchestration_recover',
  'sprint_review',
  'engine_surgery',
  'test_authoring_pg',
  'test_authoring_sqlite'
);

COMMIT;
