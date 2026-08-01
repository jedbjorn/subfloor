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

-- Keep generic communication a relay: informational acceptance only marks the
-- handled message read, while pause judgment remains Planner-owned.

UPDATE skills SET content=replace(content,
'sc sprint decline --sprint <id> --message <message-id> --reason <reason>
```

## Orient and bound the lane',
'sc sprint decline --sprint <id> --message <message-id> --reason <reason>
```

Use `accept` or `decline` for actionable work. After acting on an informational
question, answer, blocker, or context message, run `accept` for that message.
For informational messages it only marks the message read; it does not change
Sprint or work-unit state.

## Orient and bound the lane')
WHERE name='sprint_dev';

UPDATE skills SET content=replace(content,
'Ask the Planner about scope, priority, or cross-unit decisions; ask the assigned
Reviewer about review evidence. Answer an incoming question through `send` so it
wakes the asker. For a blocker, send evidence, impact, and the exact action
needed to the Planner and any directly affected participant. Continue safe
independent work, but stop at a decision boundary when the answer is required.
No immediate response is not a reason to send duplicates: the durable message
and recovery reconciler own re-waking.',
'Ask the Planner about scope, priority, or cross-unit decisions; ask the assigned
Reviewer about review evidence. Answer an incoming question through `send` so it
wakes the asker, confirm that write, then mark the handled question read with
`accept`. For a blocker or integrity concern, send the Planner concise evidence,
impact, the exact action needed, and your recommendation. Continue safe
independent work, but stop at a decision boundary when the answer is required.
No immediate response is not a reason to send duplicates: the durable message
and recovery reconciler own re-waking.')
WHERE name='sprint_dev';

UPDATE skills SET content=replace(content,
'If a Sprint command is rejected or transport fails, the write or handoff is
incomplete. Correct and retry when safe; if it still cannot complete, relay the
problem to the Planner. For an integrity threat, pause first, confirm the pause,
then send the evidence needed to resolve it:

```text
sc sprint pause --sprint <id> --reason <integrity-threat>
```

After the pause succeeds, use the `send` command above for the evidence.',
'If a Sprint command is rejected or transport fails, the write or handoff is
incomplete. Correct and retry when safe. If the relay itself fails, surface the
attempted command, evidence, impact, and recommendation to FnB; do not invent an
alternate delivery protocol. A Developer does not pause the Sprint. The Planner
decides whether the reported condition warrants continuing, re-planning, or
pausing.')
WHERE name='sprint_dev';

UPDATE skills SET content=replace(content,
'## Pause and stop

Pause immediately when integrity is threatened: broken base, destructive
ambiguity, unavailable GitHub, untrustworthy runners, provider exhaustion, or
an unrecoverable environment. State the short reason first; detailed judgment
can follow after pause is durable.

```text
sc sprint pause --sprint <id> --reason <integrity-threat>
```

Stop when the unit is merged and reported, declined, paused awaiting recovery,
or returned to review. Before stopping, re-run `sc sprint inbox --sprint <id>`,
act on any newly arrived message, and confirm the final typed handoff succeeded.
Ask the Planner for later work only after the current editing lane is terminal.',
'## Report and stop

Report broken bases, destructive ambiguity, unavailable GitHub, untrustworthy
runners, provider exhaustion, or an unrecoverable environment to the Planner
with evidence, impact, and a recommendation. Stop at the unsafe boundary while
the Planner decides whether to continue, re-plan, or pause.

Stop when the unit is merged and reported, declined, awaiting Planner/FnB
recovery, or returned to review. Before stopping, re-run `sc sprint inbox
--sprint <id>`, act on newly arrived messages, mark every handled informational
message read with `accept`, and confirm the final typed handoff succeeded. Ask
the Planner for later work only after the current editing lane is terminal.')
WHERE name='sprint_dev';

UPDATE skills SET content=replace(content,
'sc sprint decline --sprint <id> --message <message-id> --reason <reason>
```

## Questions, answers, blockers, and failures',
'sc sprint decline --sprint <id> --message <message-id> --reason <reason>
```

Use `accept` or `decline` for actionable work. After acting on an informational
question, answer, blocker, or context message, run `accept` for that message.
For informational messages it only marks the message read; it does not change
Sprint or work-unit state.

## Questions, answers, blockers, and failures')
WHERE name='sprint_rev';

UPDATE skills SET content=replace(content,
'Answer incoming questions through `send` so the answer is durable and wakes the
asker. A blocker names evidence, impact, and the exact action needed, and goes to
the Planner plus any directly affected Developer. Continue independent safe
review, but stop at a decision boundary when the answer is required. Do not send
duplicate reminders; unread recovery owns re-waking.',
'Answer incoming questions through `send` so the answer is durable and wakes the
asker, confirm that write, then mark the handled question read with `accept`. A
blocker or integrity concern goes to the Planner with concise evidence, impact,
the exact action needed, and your recommendation. Continue independent safe
review, but stop at a decision boundary when the answer is required. Do not send
duplicate reminders; unread recovery owns re-waking.')
WHERE name='sprint_rev';

UPDATE skills SET content=replace(content,
'If a Sprint command is rejected or transport fails, the verdict or handoff is
incomplete. Correct and retry when safe; if it cannot complete, relay the exact
failure to the Planner. For an integrity threat, pause first, confirm it, and
then relay the evidence:

```text
sc sprint pause --sprint <id> --reason <integrity-threat>
```

After the pause succeeds, use the `send` command above for the evidence.',
'If a Sprint command is rejected or transport fails, the verdict or handoff is
incomplete. Correct and retry when safe. If the relay itself fails, surface the
attempted command, evidence, impact, and recommendation to FnB; do not invent an
alternate delivery protocol. A Reviewer does not pause the Sprint. The Planner
decides whether the reported condition warrants continuing, re-planning, or
pausing.')
WHERE name='sprint_rev';

UPDATE skills SET content=replace(content,
'For unit review, stop after the durable verdict is recorded. For conformance,
re-run `sc sprint inbox --sprint <id>`, act on newly arrived messages, and stop
after the report and all findings replay idempotently and give the Planner their
report/follow-up ids.',
'For unit review, stop after the durable verdict is recorded. For conformance,
re-run `sc sprint inbox --sprint <id>`, act on newly arrived messages, and stop
after marking every handled informational message read with `accept` and after
the report and all findings replay idempotently and give the Planner their
report/follow-up ids.')
WHERE name='sprint_rev';

UPDATE skills SET content=replace(content,
'Accept or decline only when the inbox item is actionable. Informational
questions, answers, blockers, and evidence remain unread until you inspect them;
reading them does not invent a work-unit transition.',
'Accept or decline only when the inbox item is actionable. After acting on an
informational question, answer, blocker, or evidence message, run `accept` for
that message. For informational messages it only marks the message read; it does
not change Sprint or work-unit state.')
WHERE name='sprint_pln';

UPDATE skills SET content=replace(content,
'Answer incoming questions through `send` so the answer is durable and wakes the
asker. For a cross-unit blocker, send evidence, impact, and the exact action
needed to every directly affected participant. Continue safe independent
governance, but stop at a decision boundary when an answer is required. Do not
spam duplicates when no response is immediate; unread recovery owns re-waking.',
'Answer incoming questions through `send` so the answer is durable and wakes the
asker, confirm that write, then mark the handled question read with `accept`.
For a cross-unit blocker, send evidence, impact, and the exact action needed to
every directly affected participant. Continue safe independent governance, but
stop at a decision boundary when an answer is required. Do not spam duplicates
when no response is immediate; unread recovery owns re-waking.')
WHERE name='sprint_pln';

UPDATE skills SET content=replace(content,
'If a Sprint command is rejected or transport fails, the write or handoff is
incomplete. Correct and retry when safe; if it cannot complete, send the exact
failure to an eligible participant or surface it to FnB. For an integrity
threat, pause first, confirm the pause, then relay the evidence:

```text
sc sprint pause --sprint <id> --reason <integrity-threat>
```

After the pause succeeds, use the `send` command above for the evidence.',
'If a Sprint command is rejected or transport fails, the write or handoff is
incomplete. Correct and retry when safe. If the relay itself fails, surface the
attempted command and durable evidence to FnB; do not invent an alternate
delivery protocol. When a Developer or Reviewer reports an integrity concern,
evaluate its evidence, impact, and recommendation. Decide whether to continue,
re-plan, or pause. Send any needed participant context before pausing; an active
relay is not available after the lifecycle becomes paused.

```text
sc sprint pause --sprint <id> --reason <integrity-threat>
```')
WHERE name='sprint_pln';

UPDATE skills SET content=replace(content,
'Any participant may pause immediately for an integrity threat. Effective pause
comes first: transition durably, stop external Sprint services, persist
interrupt intent, preserve every partial artifact, and notify Planner/FnB. Add
judgment to the generated pause report after the boundary is safe.

Only Planner or FnB resumes. Review reconciliation for native runs, unread
messages, pending wakes, work units, registered PRs, capacity, and spec drift.
Drift informs; it never silently blocks resume.',
'Developer and Reviewer participants report integrity concerns; the Planner or
FnB decides whether to pause. When pause is warranted, transition durably, stop
external Sprint services, persist interrupt intent, preserve every partial
artifact, and retain the judgment and evidence for FnB recovery.

Only Planner or FnB resumes. Review reconciliation for native runs, unread
messages, pending wakes, work units, registered PRs, capacity, and spec drift.
An exhausted recovery wake is one bounded fallback, not a retry loop. Preserve
the unread message and failed wake as evidence, involve FnB for manual recovery,
and do not create recursive fallbacks.
Drift informs; it never silently blocks resume.')
WHERE name='sprint_pln';

UPDATE skills SET content=replace(content,
're-run `sc sprint inbox --sprint <id>`, act on any newly arrived message, confirm
the final typed transition succeeded, stop dispatching, and invoke
`sprint_close`. Do not fix close-out conformance findings inside this Sprint.',
're-run `sc sprint inbox --sprint <id>`, act on any newly arrived message, confirm
every handled informational message is marked read with `accept`, confirm the
final typed transition succeeded, stop dispatching, and invoke `sprint_close`.
Do not fix close-out conformance findings inside this Sprint.')
WHERE name='sprint_pln';

UPDATE skills SET content=replace(content,
'sc sprint decline --sprint <id> --message <message-id> --reason <reason>
```

## Questions, answers, blockers, and failures',
'sc sprint decline --sprint <id> --message <message-id> --reason <reason>
```

Use `accept` or `decline` for actionable work. After acting on an informational
question, answer, blocker, or context message, run `accept` for that message.
For informational messages it only marks the message read; it does not change
Sprint or work-unit state.

## Questions, answers, blockers, and failures')
WHERE name='sprint_close';

UPDATE skills SET content=replace(content,
'Answer incoming questions through `send`. For a blocker, send the evidence,
impact, and exact action needed to the originating Planner/FnB and every directly
affected participant. Continue safe synthesis, but stop at a decision boundary
when the answer is required. Do not send duplicate reminders; unread recovery
owns re-waking.',
'Answer incoming questions through `send`, confirm that write, then mark the
handled question read with `accept`. For a blocker, send the evidence, impact,
and exact action needed to FnB and every directly affected participant. Continue
safe synthesis, but stop at a decision boundary when the answer is required. Do
not send duplicate reminders; unread recovery owns re-waking.')
WHERE name='sprint_close';

UPDATE skills SET content=replace(content,
'If a Sprint command is rejected or transport fails, the write or handoff is
incomplete. Correct and retry when safe; relay the exact failure if it cannot
complete. For an integrity threat, pause first, confirm it, then send evidence:

```text
sc sprint pause --sprint <id> --reason <integrity-threat>
```

After the pause succeeds, use the `send` command above for the evidence.',
'If a Sprint command is rejected or transport fails, the write or handoff is
incomplete. Correct and retry when safe. If the relay itself fails, surface the
attempted command and durable evidence to FnB; do not invent an alternate
delivery protocol. This skill is Planner-owned: the Planner or FnB decides
whether an integrity threat warrants pause. Send any needed participant context
before pausing; an active relay is not available after the lifecycle becomes
paused.

Treat an exhausted recovery wake as bounded manual-recovery evidence for FnB;
preserve the unread message and failed wake, and do not create recursive
fallbacks.

```text
sc sprint pause --sprint <id> --reason <integrity-threat>
```')
WHERE name='sprint_close';

UPDATE skills SET content=replace(content,
'links. Re-run `sc sprint inbox --sprint <id>`, act on any newly arrived message,
and stop after the terminal transition; Sprint-scoped authority is over.',
'links. Re-run `sc sprint inbox --sprint <id>`, act on any newly arrived message,
mark every handled informational message read with `accept`, and stop after the
terminal transition; Sprint-scoped authority is over.')
WHERE name='sprint_close';

COMMIT;
