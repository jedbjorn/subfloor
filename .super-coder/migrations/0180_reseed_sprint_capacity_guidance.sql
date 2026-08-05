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

Match participant count to the actual independent work, not the available
roster. Use a moderate share of eligible shells by default and leave capacity
unassigned; expand only when the change size and dependency graph expose more
genuinely parallel editing and review lanes. A small change should normally use
one Developer and one Reviewer, not every available shell.

For every participant, record role, route, model, and effective effort.'
)
WHERE name = 'sprint_prep'
  AND instr(content, 'Match participant count to the actual independent work') = 0;

COMMIT;
