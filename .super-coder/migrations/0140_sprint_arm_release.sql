-- An armed Sprint must give its persistent Conductor committed work to
-- consume.  The initial release is a system transition so board activation
-- remains atomic and the Conductor uses the same assignment/outbox path as
-- every later dependency release.
INSERT OR IGNORE INTO directive_kinds (issuer_flavor,kind)
VALUES ('system','sprint-armed');

-- Existing forks have already applied the generated 0001 baseline. Carry the
-- corresponding authoritative sprint_cond asset edit forward without
-- duplicating it when a fresh rebuild applies both 0001 and this delta.
UPDATE skills
SET content=replace(
  content,
  '| system | pr-green/pr-red/pr-merged | apply recorded PR transition and assigned-role wake | no discretionary interpretation |',
  '| system | sprint-armed | release every dependency-ready unit through its assigned developer route | every initially ready developer starts |
| system | pr-green/pr-red/pr-merged | apply recorded PR transition and assigned-role wake | no discretionary interpretation |'
)
WHERE name='sprint_cond'
  AND content NOT LIKE '%| system | sprint-armed |%';
