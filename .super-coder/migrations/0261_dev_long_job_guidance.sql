-- 0261 — route long-running Developer verification through the durable job
-- supervisor. Harness/GUI background watchers can die at a turn boundary or
-- report their own completion as the test's completion; sc job records the
-- command's terminal state and exit code independently. No schema change.
-- Idempotent.

BEGIN;

UPDATE shells
SET system_prompt = replace(
  system_prompt,
  'Run every available smallest affected test target that proves the changed behavior and realistic failure paths.',
  'Run every available smallest affected test target that proves the changed behavior and realistic failure paths. Use the engine''s `./sc job` tools to watch checks that may outlive the current tool call or session. Do not set up independent watchers.'
)
WHERE flavor = 'dev'
  AND instr(system_prompt, '## TESTING POSTURE') > 0
  AND instr(system_prompt, 'Do not set up independent watchers.') = 0;

COMMIT;
