-- Calibrate Sprint participation to the amount of genuinely parallel work.
BEGIN;

UPDATE skills
SET content = replace(
  content,
  'Prefer the smallest dependency graph that preserves correctness. Record the
expected output in outcome language. Do not encode a shell''s implementation
steps into the durable plan when its role skill and judgment can decide them.

For every participant, record role, route, model, and effective effort.',
  'Prefer the smallest dependency graph that preserves correctness. Record the
expected output in outcome language. Do not encode a shell''s implementation
steps into the durable plan when its role skill and judgment can decide them.

### Balance capacity and parallelism

Optimize for the smallest participant set that keeps justified critical-path
development and review moving without avoidable queues. Neither minimum
headcount nor maximum shell occupancy is a goal.

Before choosing participants, analyze the task ledger and dependency graph for
coherent non-overlapping editing lanes, expected readiness, critical-path work,
and likely review demand. Put dependency-free Developer lanes in the same wave
and plan Reviewer capacity so ready reviews can run alongside ongoing
independent development. Do not serialize work merely because it appears in
task order, split coherent work, or start a review before its unit is ready just
to create concurrency.

- For one coherent small lane, normally use one Developer and one Reviewer.
- Add a Developer only when another independent lane can start or make useful
  progress without conflicting ownership and has enough review capacity.
- Add Reviewer capacity when expected concurrent review demand would otherwise
  queue critical-path work. Reuse a Reviewer across units when their review
  readiness is unlikely to overlap.
- Leave eligible capacity unassigned when the roster allows, preserving room
  for correction, re-plan, or urgent work. Use every eligible shell only when
  the work graph and review demand justify simultaneous work and coordination
  cost does not erase the expected time-to-completion gain.

Record the capacity rationale: chosen participants, parallel lanes, expected
review overlap, retained reserve, and why another shell would or would not
shorten the critical path.

For every participant, record role, route, model, and effective effort.'
)
WHERE name = 'sprint_prep'
  AND instr(content, '### Balance capacity and parallelism') = 0;

UPDATE skills
SET content = replace(
  content,
  '- enough local/GitHub capacity to execute the plan.',
  '- a capacity plan sized to justified parallel work and review demand, with the
  local/GitHub capacity to execute it.'
)
WHERE name = 'sprint_prep';

UPDATE skills
SET content = replace(
  content,
  'planned waves, merge-grant state, and known accepted risks.',
  'planned waves, capacity rationale and reserve, merge-grant state, and known
accepted risks.'
)
WHERE name = 'sprint_prep';

COMMIT;
