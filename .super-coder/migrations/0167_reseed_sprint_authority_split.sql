-- 0167 — reseed Sprint authority split role contracts.
-- Full-body UPSERTs converge existing installations to decision #67:
-- Reviewer decides and documents; Planner executes the durable action.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_pln',
  'Run an armed Sprints v2 collaboration loop as Planner — dispatch ready lanes and execute Reviewer decisions through durable re-plan, pause, cancel, resume, and close protocols.',
  'workflow',
  NULL,
  0,
  '# sprint_pln — govern the armed Sprint

Use as the originating Planner after `sprint_prep` arms the Sprint. The system
captures deterministic facts; the Reviewer decides and documents, and the
Planner acts. Execute Reviewer decisions without taking over their judgment or
report authorship. The FnB retains the board-level override established by
decision #46.
On every wake or re-entry, load `sprint_pln`, run the exact inbox command below,
and inspect the durable message before deciding what to do.

## Start from durable state

The armed runtime owns scheduled dispatch, unread wake recovery, liveness
evaluation, and registered-PR observation. React to its durable inbox and wake
facts; use the Planner turn for dispatch and exact execution of durable Reviewer
decisions. The Reviewer owns pause, cancel, and conclude decisions plus the
conformance and final Sprint reports. The Planner owns the corresponding state
transitions.

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

Accept or decline only when the inbox item is actionable. After acting on an
informational question, answer, blocker, or evidence message, run `accept` for
that message. For informational messages it only marks the message read; it does
not change Sprint or work-unit state.

## Running loop

- Keep dependencies as the only hard sequence. When reality requires a re-plan,
  give the Reviewer the current projection and evidence, then execute the exact
  decision it returns; never rewrite completed history.
- Let Developers own their PRs through green, review, correction, and merge.
  Let Reviewers own verdicts and Sprint decisions. Do not proxy routine
  handoffs or substitute Planner judgment for a Reviewer decision.
- Consume passive system facts without waking yourself into every transition.
  Route a decision boundary to the Reviewer; act when its Re-enter decision
  message arrives.
- Record the Reviewer decision identity, the exact action taken, and the action
  receipt. Do not rewrite its rationale as Planner-authored judgment evidence.
- A mid-Sprint spec edit is allowed only by the owning Planner or FnB. Record
  the prior and new exact revision hashes. When acting as Planner, require a
  durable Reviewer decision before the edit. The running Sprint remains bound
  to its approved revision unless that decision explicitly says otherwise.

The armed runtime evaluates liveness on its five-second pulse. A one-shot
diagnostic/evaluation is available when evidence requires it:

```text
sc sprint monitor --sprint <id>
```

Run `monitor` once for concrete evidence, then return control to native
delivery. It evaluates only due accepted expectations and its
nudge/escalation identities are durable. Use Sprint-native wakes for
coordination. Do not start a recurring shell loop, scheduled job, manual
participant boot, or external PR watcher to track Sprint state.

## Questions, answers, blockers, and failures

Put a concrete question, answer, decision, blocker, or useful context in a short
body file and address the participant who owns the needed fact or action:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --key <stable-key>
```

Answer incoming questions through `send` so the answer is durable and wakes the
asker, confirm that write, then mark the handled question read with `accept`.
For a cross-unit blocker, send evidence, impact, and the exact action needed to
every directly affected participant. Continue safe independent governance, but
stop at a decision boundary when an answer is required. Do not spam duplicates
when no response is immediate; unread recovery owns re-waking.

Choose one stable key for the intended recipient and exact body. Reuse it only
when retrying that same write; use a new key when the recipient or body changes.

Keep this Sprint message or result at about 6,000 characters or fewer; 8,000
characters is the hard maximum. Before submitting, run `wc -m < <path>` and
condense if needed. The handoff is complete only when the Sprint command exits
successfully and confirms the durable write and wake where applicable.

If a Sprint command is rejected or transport fails, the write or handoff is
incomplete. Correct and retry when safe. If the relay itself fails, surface the
attempted command and durable evidence to FnB; do not invent an alternate
delivery protocol. When a Developer reports an integrity concern, relay its
evidence, impact, and recommendation to the Reviewer. When the Reviewer returns
a decision, execute it through the protocol below. Send any needed participant
context before pausing; an active relay is not available after the lifecycle
becomes paused.

## Reviewer decision actions

Pause, cancel, and conclude are Reviewer decisions and Planner actions. A valid
decision arrives as a durable Reviewer → Planner Re-enter message and names the
decision, evidence, target ids, reason, and exact requested transition. Accept
the actionable message, verify it came from the assigned Reviewer, and execute
that transition without re-adjudicating the decision. Record the decision
message id and action receipt together.

The FnB board-level override from decision #46 is unaffected: a live FnB
instruction may direct or supersede any action. Name that override in the
evidence instead of attributing it to the Reviewer.

If a requested action fails a lifecycle, authority, or disposition precondition,
do not substitute a different action. Send the refusal and current durable state
back to the Reviewer (or surface it directly to FnB for an FnB override), then
stop at that decision boundary.

### Pause or resume

On a Reviewer pause decision, transition durably, stop external Sprint services,
persist interrupt intent, preserve every partial artifact, and retain the
Reviewer judgment and evidence for recovery:

```text
sc sprint pause --sprint <id> --reason <reviewer-decision-reason>
```

Resume only on a later Reviewer decision or an FnB override. Reconcile native
runs, unread messages, pending wakes, work units, registered PRs, capacity, and
spec drift, then act with the supplied reason:

```text
sc sprint resume --sprint <id> [--reason <reviewer-reconciliation-decision>]
```

An exhausted recovery wake is bounded manual-recovery evidence, not a retry
loop. Preserve the unread message and failed wake, involve FnB, and do not create
recursive fallbacks. Drift informs; it never silently blocks resume.

### Cancel or re-plan

A Reviewer cancel decision must name one unreleased work unit and its retained
terminal reason. Execute exactly that cancellation:

```text
sc sprint cancel-unit --sprint <id> --work-unit <id> --reason <reviewer-decision-reason>
```

For a Reviewer re-plan decision, apply its complete projection; never infer
omitted fields or alter a released or completed lane:

```text
sc sprint replan-unit --sprint <id> --work-unit <id> \
  --developer-shell <id> --reviewer-shell <id> --wave <n> \
  [--depends-on <work-unit-id>] [--output-kind code|report-only|no-code]
```

If a Developer or Reviewer declines, preserve the reason and ask the Reviewer
for the replacement routing decision before issuing a fresh assignment.

### Conclude or abort

The Reviewer decides when the Sprint is done, records conformance, authors the
final Sprint report, and sends a conclude decision containing the conformance
receipt, follow-up ids, exact reason/outcome, stable key, and full report body.
Write that Reviewer-authored body to `<reviewer-report>` unchanged, then perform
the close action:

```text
sc sprint complete --sprint <id> --reason <reviewer-decision-reason> \
  --outcome <reviewer-decision-outcome> --report-file <reviewer-report> \
  --key <reviewer-decision-key>
```

Do not run `compile-report`, synthesize the final report, or editorialize the
Reviewer body. Abort is likewise an action taken only on a Reviewer decision or
FnB override; it is terminal and deletes nothing.

## Handoffs and stop

Assign ready work in the Developer''s persistent Sprint conversation. Review
outcomes move the Developer to fresh fix/merge conversations automatically;
the next work assignment returns it to the persistent lane.

When all planned delivery work is terminal and merged or explicitly no-code,
send the Reviewer the bound revisions, integrated main SHA, ratified judgments,
and current close evidence, then stop and await its Re-enter conclude decision.
The Reviewer writes the conformance and final Sprint reports; conformance
findings become follow-ups rather than new editing lanes in this Sprint.

On receipt, re-run `sc sprint inbox --sprint <id>`, accept the conclude message,
execute its exact close action through the protocol above, and confirm the typed
transition succeeded. After `complete` succeeds, emit its bounded receipt and
run no further Sprint command. The Planner does not author a second report.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_rev',
  'Review Sprints v2 work and whole-Sprint conformance — own pause, cancel, and conclude decisions, author the conformance and Sprint reports, and direct Planner actions through durable messages.',
  'workflow',
  NULL,
  0,
  '# sprint_rev — independent review and conformance

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
wake arrives.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
