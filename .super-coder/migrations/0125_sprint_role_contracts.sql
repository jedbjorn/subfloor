-- 0125 — final sprint role names and Conductor skill grant.
--
-- Preserve existing skill ids where possible; seed-skills replaces the
-- renamed bodies from the authored assets immediately after migration.

BEGIN;

UPDATE skills SET name='sprint_pln'
WHERE name='plan_sprint'
  AND NOT EXISTS (SELECT 1 FROM skills WHERE name='sprint_pln');

UPDATE skills SET name='sprint_dev'
WHERE name='dev_sprint'
  AND NOT EXISTS (SELECT 1 FROM skills WHERE name='sprint_dev');

UPDATE skills SET name='sprint_rev'
WHERE name='rev_sprint'
  AND NOT EXISTS (SELECT 1 FROM skills WHERE name='sprint_rev');

UPDATE skills
SET name='sprint_cond', is_deleted=0
WHERE name='sprint_orchestration_close'
  AND NOT EXISTS (SELECT 1 FROM skills WHERE name='sprint_cond');

UPDATE skills SET is_deleted=1
WHERE name IN (
  'plan_sprint','dev_sprint','rev_sprint',
  'sprint_orchestration','sprint_orchestration_recover'
);

-- Fresh rebuilds seed the final names before historical migrations run.
-- Re-activate those final rows after 0120 has retired the former names that
-- overlap them.
UPDATE skills SET is_deleted=0
WHERE name IN ('sprint_pln','sprint_dev','sprint_rev','sprint_cond');

-- Historical migrations after 0001 used the same sprint_dev name. Reassert
-- the final authored contract so a fresh rebuild is byte-for-byte current
-- before the runtime freshness healer ever runs.
UPDATE skills
SET description='Execute one Conductor-assigned sprint unit, ask the originating Planner for decisions, deliver an exact reviewed head, report the merge, and exit.',
    category='craft',
    command=NULL,
    common=0,
    content='# sprint_dev

Own only the unit in the mandatory slot context. Conductor owns the board,
worker boots, relays, and dependency release. The originating Planner owns
decisions. You build, prove, report, and exit.

## Establish the boundary

```sh
sc sprint board --sprint <id>
sc directives list --status pending --sprint <id>
sc mem get documents --doc <governing-spec-id>
```

Confirm assignment, dependencies, branch, overlap, and observable completion
gate. Never widen scope from neighboring units.

When meaning, scope, authority, or evidence is ambiguous, stop and emit:

```sh
sc directives emit ask-planner \
  --target conductor --sprint <id> --unit <unit-id> \
  --payload ''{"question":"<decision>","alternatives":["A","B"],"evidence":["<fact>"],"downstream":["<effect>"]}''
```

Do not guess, message Planner directly, or boot another shell.

## Build and review gate

Use the recorded branch, preserve unrelated work, implement the smallest
complete change, and run the unit''s focused and full gates. When green:

```sh
sc directives emit ready-for-review \
  --target conductor --sprint <id> --unit <unit-id> \
  --payload ''{"pr_number":123,"head":"<sha>","branch":"feat/example","checks":"green","verification":["<gate>"]}''
```

Review approval is exact-head-bound. Any relevant change after `review-clean`
requires a new review. Merge only when the board still assigns this unit, all
required checks are green, dependencies are satisfied, and `review-clean`
names the exact head.

After merge emit both records:

```sh
sc directives emit merged \
  --target conductor --sprint <id> --unit <unit-id> \
  --payload ''{"pr_number":123,"head":"<approved-head>","merge_sha":"<sha>"}''
sc directives emit unit-report \
  --target conductor --sprint <id> --unit <unit-id> \
  --payload ''{"shipped":"<observable behavior>","judgements":[],"issues":[],"deviations":[],"follow_ups":[]}''
```

## Forbidden

Never write the sprint board, boot or kill shells, schedule polling, issue
Planner directives, approve your own head, merge without exact-head approval,
or continue after an unresolved decision request.

## Stop

Exit after the inspected transition directive for the turn. Completion means
the approved head is merged, both merge/report directives exist, and the
worktree is clean.'
WHERE name='sprint_dev';

DELETE FROM flavor_skills
WHERE skill_id IN (
  SELECT skill_id FROM skills
  WHERE name IN (
    'plan_sprint','dev_sprint','rev_sprint',
    'sprint_orchestration','sprint_orchestration_recover'
  )
);

WITH grants(flavor, skill_name) AS (
  VALUES
    ('planner','sprint_pln'),
    ('dev','sprint_dev'),
    ('reviewer','sprint_rev'),
    ('conductor','sprint_cond')
)
INSERT OR IGNORE INTO flavor_skills(flavor,skill_id)
SELECT g.flavor,s.skill_id
FROM grants g JOIN skills s ON s.name=g.skill_name
WHERE s.is_deleted=0;

COMMIT;
