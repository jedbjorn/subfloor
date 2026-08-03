---
name: sprint_rev
description: Review Sprints v2 work and whole-Sprint conformance — own pause, cancel, and conclude decisions, author the conformance and Sprint reports, and direct Planner actions through durable messages.
category: workflow
common: false
---

# sprint_rev — independent review and conformance

Use in one of two modes: a work-unit PR review during the loop, or the final
whole-Sprint conformance pass. Pre-declaration QAQC is a third entry condition,
before a Sprint exists. The evidence differs; independence does not. The
Reviewer decides and documents; the Planner acts. The FnB retains the
board-level override established by decision #46.

## Entry and durable state

Pre-declaration QAQC begins from an explicit Planner or FnB request. Read the
exact current spec document and sign that body directly; there is no Sprint id
or Sprint inbox to inspect yet:

```text
sc sprint record-qaqc --document <spec-document-id> \
  --verdict pass [--findings-document <document-id>]
```

Once a Sprint is armed, every review or conformance entry arrives through its
durable wake/inbox. On every wake or re-entry, load `sprint_rev`, inspect the
message, and accept the actionable request before beginning:

```text
sc sprint inbox --sprint <id>
sc sprint accept --sprint <id> --message <message-id>
```

Decline an actionable request you cannot take, with a concrete reason:

```text
sc sprint decline --sprint <id> --message <message-id> --reason <reason>
```

Use `accept` or `decline` for actionable work. After acting on an informational
question, answer, blocker, or context message, run `accept` for that message.
For informational messages it only marks the message read; it does not change
Sprint or work-unit state.

## Questions, answers, blockers, and failures

Put a concrete question, answer, blocker, or useful context in a short body file
and send it to the participant who can act. Ask the Developer for missing PR
evidence and the Planner for durable state or action-feasibility facts. Do not
delegate Reviewer judgment to the Planner:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --key <stable-key>
```

Answer incoming questions through `send` so the answer is durable and wakes the
asker, confirm that write, then mark the handled question read with `accept`. A
blocker or integrity concern is evidence for your decision. If action is needed,
send the Planner the decision, impact, exact action, and recommendation through
the protocol below. Continue independent safe review, but stop when missing
facts prevent an honest decision at the decision boundary. Do not send duplicate
reminders; unread recovery owns re-waking.

Choose one stable key for the intended recipient and exact body. Reuse it only
when retrying that same write; use a new key when the recipient or body changes.

Keep this Sprint message or result at about 6,000 characters or fewer; 8,000
characters is the hard maximum. Before submitting, run `wc -m < <path>` and
condense if needed. The handoff is complete only when the Sprint command exits
successfully and confirms the durable write and wake where applicable.

If a Sprint command is rejected or transport fails, the verdict or handoff is
incomplete. Correct and retry when safe. If the relay itself fails, surface the
attempted command, evidence, impact, and recommendation to FnB; do not invent an
alternate delivery protocol. A Reviewer decides whether the condition warrants
continuing, re-planning, pausing, cancellation, or conclusion, but does not
invoke the lifecycle transition. Send the durable decision to the Planner, who
executes it. A live FnB board-level override may supersede that decision and
must be recorded as FnB authority, not Reviewer judgment.

## Control and conclude decisions

The Reviewer owns all pause, cancel, and conclude decisions and recommendations.
The Planner owns all resulting actions. Base a decision on durable Sprint state,
the exact bound revisions, current work/PR facts, liveness evidence, and any
ratified judgment; ambiguous silence is not enough to corrupt a disposition.

Send each decision to the Planner through the durable `send` surface above. The
Reviewer → Planner route is Re-enter. The body must name:

- `decision`: `pause`, `resume`, `replan`, `cancel`, `conclude`, or `abort`;
- the evidence and rationale owned by the Reviewer;
- exact Sprint/work-unit ids, reason, outcome, and complete action arguments;
- for conclude, the conformance report/follow-up ids, stable completion key,
  and full Reviewer-authored final Sprint report body; and
- any immediate safety impact that the FnB must see.

The Planner accepts the actionable message, verifies the assigned Reviewer,
and executes exactly that transition. The Reviewer never runs the pause,
cancel, resume, complete, or abort action. If the action is rejected, inspect
the returned durable state and issue a new decision only when the evidence
supports one; never ask the Planner to improvise around a precondition.

The FnB board-level override from decision #46 is unaffected. A live FnB
instruction can direct or supersede any decision; preserve it as a distinct
authority record.

## Severity rubric

This skill owns severity. The governing spec intentionally does not.

- **Critical** — active security/authority violation, destructive corruption,
  or a condition that makes continued operation unsafe.
- **Major** — wrong behavior, data loss, broken invariant, material spec
  violation, or a loop/recovery path that can silently wedge delivery.
- **Medium** — a concrete correctness or recovery gap likely to bite normal
  use soon, including missing negative enforcement or an unreliable handoff.
- **Low** — bounded cleanup, clarity, test depth, or resilience improvement that
  does not make the delivered behavior wrong now.

During a work-unit review, Critical/Major/Medium block approval; Low is a
report note. During close-out conformance, every severity becomes a follow-up
and none is fixed inside the Sprint.

## Work-unit review

Accept the actionable review request, then inspect the exact bound spec
revision, readiness claim, PR head, diff, checks, tests, relevant runtime
evidence, and prior judgment calls. Review code quality, edge cases/failure
paths, and spec conformance. Trace the real path; do not trust names or PR prose.

Read resolved closure evidence through the authenticated memory surface; no SQL
or mutation is needed. Use the exact form for a cited flag and the scoped form
to audit every resolved flag attached to the feature:

```text
sc mem get flags <flag-id>
sc mem get flags --feature <feature-id> --resolved
```

Findings must state:

- severity and concise title;
- violated behavior or invariant;
- exact code/evidence location;
- a reproducible consequence; and
- the fix boundary, without prescribing unnecessary architecture.

Put the verdict body in a file and record it through the authenticated surface:

Keep the verdict at about 6,000 characters or fewer; 8,000 is the hard maximum.
Run `wc -m < <path>` before submission. The typed review handoff exists only
after the command succeeds and confirms its durable write and Developer wake.

```text
sc sprint record-review \
  --sprint <id> --registered-pr <registered-id> \
  --verdict changes_requested --body-file <path> --key <stable-key>
```

Use `approved` only when no Critical/Major/Medium finding remains. The engine
checks that the request was accepted and still binds to the reviewed head,
records judgment evidence, opens the correct fresh Developer conversation, and
resolves the review liveness expectation. Do not message around this surface;
an unrecorded verdict cannot unlock merge.

## Whole-Sprint conformance

Judge integrated `main` against every governing bound revision, plus the exact
recorded mid-Sprint revision facts and ratified judgments. Review the integrated
system, not unit diffs. Classify each requirement as:

```text
sc sprint compile-report --sprint <id> --limit 50 > evidence.json
```

Increase the bound only when truncation counters show the default omitted
needed evidence; 200 is the maximum. The packet supplies facts, not judgment.

- `as-specced`;
- `deviated-intentionally` with its ratified judgment;
- `deviated-silently`; or
- `unimplemented`.

The last two are findings. Include spec document id and work-unit id when known.
Write the conformance report and a JSON findings array:

```json
[
  {
    "severity": "Major",
    "title": "Integrated seam diverges",
    "body": "Evidence and consequence.",
    "spec_document_id": 46,
    "work_unit_id": 9
  }
]
```

Then record both atomically:

Keep the conformance report and each finding body at about 6,000 characters or
fewer; 8,000 is the hard maximum for each. Run `wc -m < <report>` and length-check
each finding body before submission. Require the successful report and
follow-up receipt before stopping.

```text
sc sprint record-conformance \
  --sprint <id> --body-file <report> --findings-file <json> \
  --key <stable-pass-key>
```

This creates append-only conformance evidence and pending follow-ups for FnB
disposition. It must not create a fix lane, reopen a completed work unit, or
send findings to a Developer for in-Sprint repair — including Critical ones.
Surface immediate safety risk to the FnB, but preserve the close-out rule.

Then author the final Sprint report. Name the Reviewer as its author and answer:

1. Which exact scope and revisions governed?
2. What shipped, through which work units and PRs?
3. Which Reviewer judgments and ratified deviations shaped the result?
4. What failed, retried, paused, recovered, or remained anomalous?
5. What did conformance conclude?
6. Which follow-ups or unresolved items require FnB disposition?
7. Where is the complete evidence?

Keep the final report at about 6,000 characters or fewer and below the 8,000
hard maximum; run `wc -m < <report>`. Do not smooth discrepancies into a
success narrative. If the Sprint is done, send the Planner a `conclude`
decision containing the full report body and the exact completion arguments
defined in the control protocol. The Planner submits that body unchanged and
owns only the close action. If the Sprint is not done, send the appropriate
pause, re-plan, cancel, or abort decision instead.

## Stop

For either mode, re-run `sc sprint inbox --sprint <id>` and act on newly arrived
messages before stopping. For unit review, stop after the durable verdict is
recorded and every handled informational message is marked read with `accept`.
For conformance, also require the report and all findings to replay
idempotently, then require successful delivery of the Reviewer-authored Sprint
report and conclude decision to the Planner. The conformance receipt plus the
durable decision handoff completes Reviewer work; stop until another native
wake arrives.
