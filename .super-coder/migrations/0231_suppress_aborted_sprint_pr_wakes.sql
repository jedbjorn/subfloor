-- 0231 — keep aborted-Sprint PR observation silent until ownership recovery.

BEGIN;

UPDATE skills
SET content=replace(
  content,
  'After `register-pr` succeeds, retain ownership through green. The native
registered-PR watcher creates your engine-wide subscription and sends
self-describing red/green/externally-closed PR-event wakes as Re-enter, even
outside an armed Sprint. Fix red; judge green. Planner and Reviewer receive no
PR-event wakes.',
  'After `register-pr` succeeds, retain ownership. Expect red/green/closed
Re-enter wakes outside an armed Sprint. While the PR remains attached to an
aborted Sprint, expect observation without a wake; reconciliation restores
wakes. Fix red; judge green. Planner/Reviewer get none.'
)
WHERE name='sprint_dev';

COMMIT;
