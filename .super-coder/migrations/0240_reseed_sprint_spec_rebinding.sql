-- Teach Planners the supported paused-Sprint governing-spec rebind control.

BEGIN;

UPDATE skills
SET content=replace(
  replace(
    content,
    '- Mid-Sprint spec edits require owning Planner/FnB + durable Reviewer decision.
  Record old/new revision hashes; binding changes only when the decision says so.',
    '- Reviewer-approved Planner/FnB spec rebind:
  pause -> `sc mem doc edit` -> `sc sprint rebind-spec --sprint <id>
  --document <id> --expected-revision <old-sha256> --reason <decision>` ->
  replan -> resume. Pass = old/new hashes + changed boolean; conflict -> reread.'
  ),
  '- Use native wakes. Start no recurring loop, scheduled job, manual participant
  boot, or external watcher. One stalled-gate inspection is allowed:

```text
sc sprint watcher-state --sprint <id>
```

Do not repeat this read as a polling loop. Act on its bounded watcher evidence,
then return to native delivery.',
  '- Use native wakes. Start no recurring loop, scheduled job, manual participant
  boot, or external watcher. Do not repeat a stalled-gate inspection:

```text
sc sprint watcher-state --sprint <id>
```'
)
WHERE name='sprint_pln';

COMMIT;
