---
name: sprint_pln
description: Run an armed Sprints v2 collaboration loop as Planner — dispatch ready lanes, respond to durable evidence and escalations, re-plan honestly, and coordinate pause/resume without becoming a transition bottleneck.
category: workflow
common: false
---

# sprint_pln — govern the armed Sprint

Use as the originating Planner after `sprint_prep` arms the Sprint. The system
captures deterministic facts; you decide scope, sequencing, and recovery.
On every wake or re-entry, load `sprint_pln`, run the exact inbox command below,
and inspect the durable message before deciding what to do.

## Start from durable state

Read the Sprint inbox, lifecycle, bound spec revisions, work-unit graph,
participant routes, active conversations, registered PRs, unresolved
expectations, and recent anomalies. Viewing a participant conversation is
observation, not activity; never manufacture progress from browser presence.

```text
sc sprint inbox --sprint <id>
sc sprint accept --sprint <id> --message <message-id>
sc sprint decline --sprint <id> --message <message-id> --reason <reason>
```

Release every dependency-ready lane through the production surface:

```text
sc sprint dispatch --sprint <id>
```

The returned ids are wake identities. Work-unit disposition and messages are
the authoritative release facts. Dispatch is safe to repeat: occupied Developer
lanes and stable assignment generations prevent double booking.

Accept or decline only when the inbox item is actionable. Informational
questions, answers, blockers, and evidence remain unread until you inspect them;
reading them does not invent a work-unit transition.

## Running loop

- Keep dependencies as the only hard sequence. Re-plan assignments, waves, or
  dependencies when reality changes, but never rewrite completed history.
- Let Developers own their PRs through green, review, correction, and merge.
  Let Reviewers own verdicts. Do not proxy routine handoffs.
- Consume passive system facts without waking yourself into every transition.
  Act on decisions, escalations, re-plans, pauses, and terminal synthesis.
- Record scope calls, spec edits, ratified deviations, and their rationale as
  judgment evidence while context is live.
- A mid-Sprint spec edit is allowed only by the owning Planner or FnB. Record
  the prior and new exact revision hashes. The running Sprint remains bound to
  its approved revision unless an explicit recorded judgment says otherwise.

The armed runtime evaluates liveness on its five-second pulse. A one-shot
diagnostic/evaluation is available when evidence requires it:

```text
sc sprint monitor --sprint <id>
```

Do not poll this command on a schedule. It evaluates only due accepted
expectations and its nudge/escalation identities are durable.

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

Revise only a still-planned lane; name its complete new projection so the
before/after event is reviewable. Cancel only an unreleased lane, with the
reason retained as its terminal result:

```text
sc sprint replan-unit --sprint <id> --work-unit <id> \
  --developer-shell <id> --reviewer-shell <id> --wave <n> \
  [--depends-on <work-unit-id>] [--output-kind code|report-only|no-code]
sc sprint cancel-unit --sprint <id> --work-unit <id> --reason <reason>
```

## Escalation judgment

Read the evidence packet before acting: last strong evidence, supporting
signals, unreadable signals, failure identity, nudge history, route/quota state,
and current work facts. A proven failure may justify re-plan, route recovery, or
pause. Ambiguous silence does not justify corrupting a work-unit disposition.

If a Developer or Reviewer declines, preserve the reason, return the lane or
review request to the eligible pool, and issue a fresh assignment identity.
Never edit the declined message into a different instruction.

## Pause and resume

Any participant may pause immediately for an integrity threat. Effective pause
comes first: transition durably, stop external Sprint services, persist
interrupt intent, preserve every partial artifact, and notify Planner/FnB. Add
judgment to the generated pause report after the boundary is safe.

Only Planner or FnB resumes. Review reconciliation for native runs, unread
messages, pending wakes, work units, registered PRs, capacity, and spec drift.
Drift informs; it never silently blocks resume. Record the exact revision facts
and choose continue, re-plan, or abort.

```text
sc sprint pause --sprint <id> --reason <integrity-threat>
sc sprint resume --sprint <id> [--reason <reconciliation-judgment>]
```

Abort only when continuing would be dishonest or unsafe. It is terminal and
deletes nothing.

## Handoffs and stop

Assign ready work in the Developer's persistent Sprint conversation. Review
outcomes move the Developer to fresh fix/merge conversations automatically;
the next work assignment returns it to the persistent lane.

When all planned delivery work is terminal and merged or explicitly no-code,
re-run `sc sprint inbox --sprint <id>`, act on any newly arrived message, confirm
the final typed transition succeeded, stop dispatching, and invoke
`sprint_close`. Do not fix close-out conformance findings inside this Sprint.
