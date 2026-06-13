-- 0014 — grant the `spec` skill to dev shells
--
-- `spec` (the spec_tasks lifecycle: read the ask → break it into a
-- Preparation → steps → Verification task list → execute + track across
-- sessions) is now a dev-flavor skill (dev.json lists it). It was previously
-- granted to no flavor. Grant it to existing dev shells so installed forks pick
-- it up on ./sc update; new shells get it from the template at creation.

BEGIN;

INSERT OR IGNORE INTO shell_skills (shell_id, skill_id)
SELECT s.shell_id, k.skill_id
FROM shells s
JOIN skills k ON k.name = 'spec' AND k.is_deleted = 0
WHERE s.flavor = 'dev' AND s.is_deleted = 0;

COMMIT;
