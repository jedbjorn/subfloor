-- 0153 — publish Sprint handoff contingencies and bounded-write guidance.
--
-- The baseline seed is regenerated from the assets for fresh installs. These
-- targeted replacements advance existing databases whose 0001 migration has
-- already been recorded, without disturbing fork-local skill catalogue rows.

BEGIN;

UPDATE skills SET content=replace(content,
'been chosen. Close-out supplies meaning; the compiler supplies facts.

## Delivery-complete gate',
'been chosen. Close-out supplies meaning; the compiler supplies facts.
On entry or any wake, load `sprint_close`, run `sc sprint inbox --sprint <id>`,
inspect the durable message, and accept or decline it only when actionable.

```text
sc sprint accept --sprint <id> --message <message-id>
sc sprint decline --sprint <id> --message <message-id> --reason <reason>
```

## Questions, answers, blockers, and failures

Put a concrete question, answer, blocker, or context request in a short body
file, then send it to the conformance Reviewer or participant who owns the fact:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path>
```

Answer incoming questions through `send`. For a blocker, send the evidence,
impact, and exact action needed to the originating Planner/FnB and every directly
affected participant. Continue safe synthesis, but stop at a decision boundary
when the answer is required. Do not send duplicate reminders; unread recovery
owns re-waking.

Keep this Sprint message or result at about 6,000 characters or fewer; 8,000
characters is the hard maximum. Before submitting, run `wc -m < <path>` and
condense if needed. The handoff is complete only when the Sprint command exits
successfully and confirms the durable write and wake where applicable.

If a Sprint command is rejected or transport fails, the write or handoff is
incomplete. Correct and retry when safe; relay the exact failure if it cannot
complete. For an integrity threat, pause first, confirm it, then send evidence:

```text
sc sprint pause --sprint <id> --reason <integrity-threat>
```

After the pause succeeds, use the `send` command above for the evidence.

## Delivery-complete gate')
WHERE name='sprint_close';

UPDATE skills SET content=replace(content,
'ship-as-is; `resolved` and `dismissed` require a resolution file.

```text',
'ship-as-is; `resolved` and `dismissed` require a resolution file.

Keep a resolution at about 6,000 characters or fewer; 8,000 is the hard
maximum. Run `wc -m < <path>` and require a successful durable disposition.

```text')
WHERE name='sprint_close';

UPDATE skills SET content=replace(content,
'evidence, reports, and follow-ups.

```text',
'evidence, reports, and follow-ups.

Keep the final report at about 6,000 characters or fewer; 8,000 is the hard
maximum. Run `wc -m < <path>` before the typed terminal handoff, then require
the successful report receipt and lifecycle transition.

```text')
WHERE name='sprint_close';

UPDATE skills SET content=replace(content,
'links. Stop after the terminal transition; Sprint-scoped authority is over.',
'links. Re-run `sc sprint inbox --sprint <id>`, act on any newly arrived message,
and stop after the terminal transition; Sprint-scoped authority is over.')
WHERE name='sprint_close';

UPDATE skills SET content=replace(content,
'accept, decline with a concrete reason; never leave it unread and waking.

```text',
'accept, decline with a concrete reason; never leave it unread and waking.
On every wake or re-entry, load `sprint_dev`, run the exact inbox command below,
and inspect the durable message before deciding what to do.

```text')
WHERE name='sprint_dev';

UPDATE skills SET content=replace(content,
'growth to the Planner.

## Build and verify',
'growth to the Planner.

## Questions, answers, blockers, and failures

Write a concrete question, answer, blocker, or useful context to a short body
file, then send it durably to the participant who can act:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path>
```

Ask the Planner about scope, priority, or cross-unit decisions; ask the assigned
Reviewer about review evidence. Answer an incoming question through `send` so it
wakes the asker. For a blocker, send evidence, impact, and the exact action
needed to the Planner and any directly affected participant. Continue safe
independent work, but stop at a decision boundary when the answer is required.
No immediate response is not a reason to send duplicates: the durable message
and recovery reconciler own re-waking.

Keep this Sprint message or result at about 6,000 characters or fewer; 8,000
characters is the hard maximum. Before submitting, run `wc -m < <path>` and
condense if needed. The handoff is complete only when the Sprint command exits
successfully and confirms the durable write and wake where applicable.

If a Sprint command is rejected or transport fails, the write or handoff is
incomplete. Correct and retry when safe; if it still cannot complete, relay the
problem to the Planner. For an integrity threat, pause first, confirm the pause,
then send the evidence needed to resolve it:

```text
sc sprint pause --sprint <id> --reason <integrity-threat>
```

After the pause succeeds, use the `send` command above for the evidence.

## Build and verify')
WHERE name='sprint_dev';

UPDATE skills SET content=replace(content,
'after merge authorization and observation.

```text
sc sprint complete-unit',
'after merge authorization and observation.

Keep the result at about 6,000 characters or fewer; 8,000 is the hard maximum.
Run `wc -m < <path>` before submitting, then require a successful command and
durable completion receipt.

```text
sc sprint complete-unit')
WHERE name='sprint_dev';

UPDATE skills SET content=replace(content,
'Put the readiness claim in a file, then use one stable retry key:

```text',
'Put the readiness claim in a file, then use one stable retry key:

Keep the readiness claim at about 6,000 characters or fewer; 8,000 is the hard
maximum. Run `wc -m < <path>` and condense before the typed handoff. The handoff
exists only after the command succeeds and confirms its durable write and wake.

```text')
WHERE name='sprint_dev';

UPDATE skills SET content=replace(content,
'or returned to review. Ask the Planner for later work only after the current
editing lane is terminal.',
'or returned to review. Before stopping, re-run `sc sprint inbox --sprint <id>`,
act on any newly arrived message, and confirm the final typed handoff succeeded.
Ask the Planner for later work only after the current editing lane is terminal.')
WHERE name='sprint_dev';

UPDATE skills SET content=replace(content,
'captures deterministic facts; you decide scope, sequencing, and recovery.

## Start from durable state',
'captures deterministic facts; you decide scope, sequencing, and recovery.
On every wake or re-entry, load `sprint_pln`, run the exact inbox command below,
and inspect the durable message before deciding what to do.

## Start from durable state')
WHERE name='sprint_pln';

UPDATE skills SET content=replace(content,
'sc sprint accept --sprint <id> --message <message-id>
```',
'sc sprint accept --sprint <id> --message <message-id>
sc sprint decline --sprint <id> --message <message-id> --reason <reason>
```')
WHERE name='sprint_pln';

UPDATE skills SET content=replace(content,
'lanes and stable assignment generations prevent double booking.

## Running loop',
'lanes and stable assignment generations prevent double booking.

Accept or decline only when the inbox item is actionable. Informational
questions, answers, blockers, and evidence remain unread until you inspect them;
reading them does not invent a work-unit transition.

## Running loop')
WHERE name='sprint_pln';

UPDATE skills SET content=replace(content,
'expectations and its nudge/escalation identities are durable.

Revise only a still-planned lane',
'expectations and its nudge/escalation identities are durable.

## Questions, answers, blockers, and failures

Put a concrete question, answer, decision, blocker, or useful context in a short
body file and address the participant who owns the needed fact or action:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path>
```

Answer incoming questions through `send` so the answer is durable and wakes the
asker. For a cross-unit blocker, send evidence, impact, and the exact action
needed to every directly affected participant. Continue safe independent
governance, but stop at a decision boundary when an answer is required. Do not
spam duplicates when no response is immediate; unread recovery owns re-waking.

Keep this Sprint message or result at about 6,000 characters or fewer; 8,000
characters is the hard maximum. Before submitting, run `wc -m < <path>` and
condense if needed. The handoff is complete only when the Sprint command exits
successfully and confirms the durable write and wake where applicable.

If a Sprint command is rejected or transport fails, the write or handoff is
incomplete. Correct and retry when safe; if it cannot complete, send the exact
failure to an eligible participant or surface it to FnB. For an integrity
threat, pause first, confirm the pause, then relay the evidence:

```text
sc sprint pause --sprint <id> --reason <integrity-threat>
```

After the pause succeeds, use the `send` command above for the evidence.

Revise only a still-planned lane')
WHERE name='sprint_pln';

UPDATE skills SET content=replace(content,
'stop dispatching and invoke `sprint_close`. Do not fix close-out conformance
findings inside this Sprint.',
're-run `sc sprint inbox --sprint <id>`, act on any newly arrived message, confirm
the final typed transition succeeded, stop dispatching, and invoke
`sprint_close`. Do not fix close-out conformance findings inside this Sprint.')
WHERE name='sprint_pln';

UPDATE skills SET content=replace(content,
'whole-Sprint conformance pass. The evidence differs; independence does not.

Read and accept',
'whole-Sprint conformance pass. The evidence differs; independence does not.
On every wake or re-entry, load `sprint_rev`, run the exact inbox command below,
and inspect the durable message before deciding what to do.

Read and accept')
WHERE name='sprint_rev';

UPDATE skills SET content=replace(content,
'  --verdict pass [--findings-document <document-id>]
```

## Severity rubric',
'  --verdict pass [--findings-document <document-id>]
```

Decline an actionable request you cannot take, with a concrete reason:

```text
sc sprint decline --sprint <id> --message <message-id> --reason <reason>
```

## Questions, answers, blockers, and failures

Put a concrete question, answer, blocker, or useful context in a short body file
and send it to the participant who can act. Ask the Developer for missing PR
evidence and the Planner for scope or severity decisions:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path>
```

Answer incoming questions through `send` so the answer is durable and wakes the
asker. A blocker names evidence, impact, and the exact action needed, and goes to
the Planner plus any directly affected Developer. Continue independent safe
review, but stop at a decision boundary when the answer is required. Do not send
duplicate reminders; unread recovery owns re-waking.

Keep this Sprint message or result at about 6,000 characters or fewer; 8,000
characters is the hard maximum. Before submitting, run `wc -m < <path>` and
condense if needed. The handoff is complete only when the Sprint command exits
successfully and confirms the durable write and wake where applicable.

If a Sprint command is rejected or transport fails, the verdict or handoff is
incomplete. Correct and retry when safe; if it cannot complete, relay the exact
failure to the Planner. For an integrity threat, pause first, confirm it, and
then relay the evidence:

```text
sc sprint pause --sprint <id> --reason <integrity-threat>
```

After the pause succeeds, use the `send` command above for the evidence.

## Severity rubric')
WHERE name='sprint_rev';

UPDATE skills SET content=replace(content,
'Put the verdict body in a file and record it through the authenticated surface:

```text',
'Put the verdict body in a file and record it through the authenticated surface:

Keep the verdict at about 6,000 characters or fewer; 8,000 is the hard maximum.
Run `wc -m < <path>` before submission. The typed review handoff exists only
after the command succeeds and confirms its durable write and Developer wake.

```text')
WHERE name='sprint_rev';

UPDATE skills SET content=replace(content,
'Then record both atomically:

```text',
'Then record both atomically:

Keep the conformance report and each finding body at about 6,000 characters or
fewer; 8,000 is the hard maximum for each. Run `wc -m < <report>` and length-check
each finding body before submission. Require the successful report and
follow-up receipt before stopping.

```text')
WHERE name='sprint_rev';

UPDATE skills SET content=replace(content,
'For unit review, stop after the durable verdict is recorded. For conformance,
stop after the report and all findings replay idempotently and give the Planner
their report/follow-up ids.',
'For unit review, stop after the durable verdict is recorded. For conformance,
re-run `sc sprint inbox --sprint <id>`, act on newly arrived messages, and stop
after the report and all findings replay idempotently and give the Planner their
report/follow-up ids.')
WHERE name='sprint_rev';

COMMIT;
