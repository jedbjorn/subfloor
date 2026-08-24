-- 0233 — keep Developer PR notifications useful outside an active Sprint.

BEGIN;

UPDATE skills
SET content=replace(
  content,
  'After `register-pr` succeeds, retain ownership. Expect red/green/closed
Re-enter wakes outside an armed Sprint. While the PR remains attached to an
aborted Sprint, expect observation without a wake; reconciliation restores
wakes. Fix red; judge green. Planner/Reviewer get none.',
  'After `register-pr` succeeds, retain ownership. Red/green/closed Re-enter wakes
continue after the Sprint ends. Follow context: armed -> fix red + judge/pass
green; paused -> fix red now + judge green, review after resume;
no active Sprint -> fix red if needed + no action on green. Planner/Reviewer get
none.'
)
WHERE name='sprint_dev';

COMMIT;
