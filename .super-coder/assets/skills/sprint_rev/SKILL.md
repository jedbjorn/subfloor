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

Use the simplest path supported by current durable state. Treat independence,
authority, lifecycle preconditions, durable writes, and typed handoffs as hard
boundaries; use judgment for investigation and review within them. Repeat a
read only when later activity could have changed it or the next command
requires live revalidation.

## Route the entry

Classify the entry before reading an inbox:

- Pre-declaration QAQC begins from an explicit Planner or FnB request through
  the ordinary shell-to-shell channel. Read and sign the exact current spec
  body directly; there is no Sprint id or Sprint inbox to inspect yet.
- A work-unit review request or delivery-terminal notification is Sprint-scoped.
  Inspect the Sprint inbox once and accept the actionable request before work.
- For a live FnB instruction, preserve its board-level authority distinctly and
  inspect only the durable state needed for an independent decision.

Record pre-declaration QAQC through the authenticated surface:

```text
sc sprint record-qaqc --document <spec-document-id> \
  --verdict pass [--findings-document <document-id>]
```

Once a Sprint is armed, review and conformance entries arrive through durable
wakes. Load `sprint_rev` on every entry, then use the Sprint inbox for the
Sprint-scoped cases above:

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

Review requests use Force-new delivery. Reviewer verdicts to Developers and
decisions to Planners use Re-enter. Neither displaces a live turn; delivery
waits for its natural boundary, and the runtime owns bundling, rotation, and
recovery. Stop after a successful typed handoff. Reviewers never receive
PR-event subscription wakes.

## Questions, answers, blockers, and failures

Put one concrete question, answer, blocker, or useful context item in a short
body file and send it to the participant who owns the next fact or action. Ask
the Developer for missing PR evidence and the Planner for durable state or
action-feasibility facts. Do not delegate Reviewer judgment to the Planner:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --key <stable-key>
```

Answer an incoming question through `send`, confirm the write, then mark the
handled message read with `accept`. A blocker or integrity concern is evidence
for your decision. If action is needed, send the Planner the decision, impact,
exact action, and recommendation. Continue safe independent review, but stop at
the decision boundary when missing facts prevent an honest decision. Unread
recovery owns re-waking; do not send duplicate reminders.

Choose one stable key for the intended recipient and exact body. Reuse it only
when retrying that same write; use a new key when the recipient or body changes.

Keep the body near 6,000 characters and below the 8,000 hard maximum; run
`wc -m < <path>`. A handoff is complete only when the Sprint command exits
successfully and confirms the durable write and wake.

If a command is rejected or transport fails, the verdict or handoff is
incomplete. Correct and retry when safe. If the relay itself fails, surface the
attempted command, evidence, impact, and recommendation to FnB; do not invent an
alternate protocol. A Reviewer decides whether the condition warrants
continuing, re-planning, pausing, cancellation, or conclusion; the Planner
executes the durable decision. Record a live FnB override as FnB authority, not
Reviewer judgment.

## Sprint artifact paths

Sprint working artifacts (per-unit review notes, raw diffs, evidence packets,
report drafts, and Dev scratch proof) go to the gitignored
`shared/sprints/sprint-<n>/` directory. They are never committed, branched, or
PR'd in the work repo; a review-notes commit is a finding.

DB rows stay the durable record: judgments via `record-review`, report bodies in
`sprint_reports`, and decisions in the durable relay. Files in the Sprint
artifact directory are working material only.

## Control and conclude decisions

The Reviewer owns all pause, cancel, and conclude decisions and recommendations.
The Planner owns control actions; clean conformance approval atomically performs
its own close. Base a decision on durable Sprint state,
the exact bound revisions, current work/PR facts, liveness evidence, and any
ratified judgment; ambiguous silence is not enough to corrupt a disposition.

Send pause, resume, replan, re-enter, cancel, and abort decisions through the
durable `send` surface above. A clean conclude instead runs the atomic
`record-conformance` close below. Every Reviewer → Planner route is Re-enter.
The Reviewer-authored body must name:

- `decision`: `pause`, `resume`, `replan`, `re-enter`, `cancel`, or `abort`;
- the evidence and rationale owned by the Reviewer;
- exact Sprint/work-unit ids, reason, outcome, and complete action arguments;
- any immediate safety impact that the FnB must see.

The Planner marks the message handled, verifies the assigned Reviewer, and
executes exactly that control transition. The clean completion receipt is
informational because the Sprint is already terminal. The Reviewer never runs
the standalone pause, replan, cancel, resume, complete, or abort action; its
clean `record-conformance` command owns the narrow automatic close. If a control
action is rejected, inspect the returned durable state and issue
a new decision only when the evidence supports one; never ask the Planner to
improvise around a precondition.

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
report note. During close-out conformance, severity does not decide timing: the
Reviewer judges whether each finding requires in-Sprint patching or is an
acceptable post-Sprint follow-up.

## Work-unit review

Accept the actionable review request. Its readiness body must be only the bare
locator: submitting or resubmitting intent, PR URL, registered Sprint PR id,
exact head SHA, and work-unit id. Treat scope narrative, verification evidence,
judgment rationale, or review-focus steering in that body as a protocol defect;
do not use it to frame the review. Neither party writes PR comments or
annotations, and the PR body contains only the work-unit id and spec reference.

Review the exact bound spec revision and the full diff at the request's exact
head, then inspect checks, tests, relevant runtime evidence, and ratified
judgments. Each round is clean: no prior Developer evidence or prose is input,
and prior findings are cleared only when the code at the new head proves they
are cleared. Review code quality, edge cases/failure paths, and spec
conformance. Trace the real path; do not trust names or PR prose.

### Red-check doctrine

Accepted-red is not a legal review outcome. A departure that leaves checks
failing is never acceptable: do not note the failure and approve anyway. The
review handoff remains green-only, without exception or waiver.

`Note it and pass anyway` is the acceptance-shaped anti-pattern. In the
dos-arch incident, a Reviewer accepted known-failing tests as a scoped
departure and created a deadlock: the green-only handoff gate could never pass.
Decision #93 records why this no-waiver rule exists.

When failing checks are within the lane's ratified scope, record
`changes_requested` so the Developer fixes them and re-establishes green. When
the failures are outside that scope, name the blocking failures in the finding
and send the Planner a `replan` decision through the control protocol. The
Planner must either widen the lane explicitly or cut a follow-up work unit; the
current lane remains unapproved until the resulting work is green.

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

Complete a unit verdict in this exact order:

1. Finish the review, findings, and verdict body; no inspection remains after
   this step.
2. Re-run `sc sprint inbox --sprint <id>`, act on newly arrived messages, and
   mark every handled informational message read with `accept`.
3. Run `wc -m < <path>`; keep the verdict near 6,000 characters and below the
   8,000-character hard maximum.
4. As the literal final action of the turn, record the typed verdict through
   the authenticated surface:

```text
sc sprint record-review \
  --sprint <id> --registered-pr <registered-id> \
  --verdict changes_requested --body-file <path> --key <stable-key>
```

5. When the command confirms the durable write and Developer wake, stop
   immediately. Run no trailing command.

Use `approved` only when no Critical/Major/Medium finding remains. The engine
checks that the request was accepted and still binds to the reviewed head,
records judgment evidence, sends a Re-enter wake to the Developer, and
resolves the review liveness expectation. Do not message around this surface;
an unrecorded verdict cannot unlock merge.

## Delivery-terminal closeout

The `sprint.delivery_terminal` notification is the entry signal for
whole-Sprint conformance. On that wake, inspect the inbox and current work-unit
state first. If any non-terminal unit is visible, the wake is stale: mark the
informational notification handled with `accept`, exit, and await the next
episode's delivery-terminal wake.

Compile the bounded evidence packet first and do so yourself, then judge
integrated `main` against every governing bound revision, exact recorded
mid-Sprint revision fact, and ratified judgment:

```text
sc sprint compile-report --sprint <id> --limit 50 \
  > shared/sprints/sprint-<n>/evidence.json
```

Increase the bound only when truncation counters show the default omitted
needed evidence; 200 is the maximum. The packet supplies facts, not judgment.
If every work unit was cancelled and nothing shipped, the honest decision is
`abort`, not `conclude`.

After classifying the requirements, choose exactly one branch:

- **In-Sprint patching required.** Do not run `record-conformance`. Send the
  Planner a durable `re-enter` decision naming every blocking finding; each
  spec task to cut against the governing spec document, with title and
  description; and the suggested unit grouping, waves, dependencies,
  Developer/Reviewer routing, and capacity rationale. Identify independent
  lanes, expected review overlap, useful reserve, and why additional capacity
  would or would not shorten the critical path. The durable decision is the
  failed-pass record.
  After three re-entry episodes in one Sprint, escalate the non-convergence to
  FnB instead of starting another patch round.
- **Clean or post-Sprint-only findings.** Prepare the conformance report,
  findings, final Sprint report, reason, and outcome. Submit them through the
  atomic `record-conformance` protocol below: the engine commits the evidence,
  completed lifecycle, informational Planner receipt, and wake together. Do not
  send a separate conclude message.

## Whole-Sprint conformance

Review the integrated system, not unit diffs. Classify each requirement as:

- `as-specced`;
- `deviated-intentionally` with its ratified judgment;
- `deviated-silently`; or
- `unimplemented`.

The last two are findings. Include spec document id and work-unit id when known.
For the clean or post-Sprint-only branch, write the conformance report and a
JSON findings array:

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

Keep the conformance report and each finding body at about 6,000 characters or
fewer; 8,000 is the hard maximum for each. Run `wc -m < <report>` and length-check
each finding body before submission.

Before recording conformance, author the final Sprint report. Name the Reviewer
as its author and answer:

1. Which exact scope and revisions governed?
2. What shipped, through which work units and PRs?
3. Which Reviewer judgments and ratified deviations shaped the result?
4. What failed, retried, paused, recovered, or remained anomalous?
5. What did conformance conclude?
6. Which follow-ups or unresolved items require FnB disposition?
7. Where is the complete evidence?

Keep the final report at about 6,000 characters or fewer and below the 8,000
hard maximum; run `wc -m < <report>`. Do not smooth discrepancies into a
success narrative. Keep the final report below 8,000 characters, then choose
the exact completion reason, terminal outcome, and stable completion key. The
engine stores the final report unchanged and generates the Planner receipt from
the committed report and follow-up identities.

Record the clean branch as one atomic final write:

```text
sc sprint record-conformance \
  --sprint <id> --body-file <report> --findings-file <json> \
  --final-report-file <final-report> --reason <reason> --outcome <outcome> \
  --key <stable-pass-key>
```

The receipt must name the conformance report id, final report id, follow-up ids,
completed state, Planner message id, and Planner wake id. This creates
append-only evidence, pending follow-ups, terminal lifecycle, and one
informational engine-wide Planner Re-enter in the same transaction. Never
record conformance first and then close around it; send no conclude message.
On that successful commit, the engine also closes every other active chat
immutably linked to the Sprint. The originating Planner and this
report-authoring Reviewer remain open. Do not manually close peer chats as an
extra closeout step. Pause, abort, re-entry, failed conformance, and rejected
fallback completion keep their existing no-cleanup behavior.
Never reopen an editing
lane after recording; the re-enter branch defers the report until added scope
reaches terminal disposition and a fresh delivery-terminal wake starts the next
episode. Surface any immediate safety risk to FnB.

## Stop

For unit review, follow the ordered verdict procedure above: inbox handling and
all evidence work precede `record-review`; the durable verdict is the literal
last action, then the Reviewer stops.

For the clean or post-Sprint-only branch, require both reports, findings,
reason, and outcome to replay idempotently. For the re-enter or abort branch,
confirm that the decision body carries the complete evidence and exact
requested action. Then complete this final handoff order:

1. Re-run `sc sprint inbox --sprint <id>`, act on newly arrived messages, and
   mark every handled informational message read with `accept`.
2. Confirm every Reviewer-authored artifact and decision body is final and
   below its 8,000-character hard maximum.
3. For a clean conclude, run the atomic `record-conformance` command above as
   the literal final action. When it confirms completed state and all receipt
   identities, stop immediately; the Planner is already notified.
4. For re-enter or abort, deliver the decision to the Planner as the literal
   final action:

```text
sc sprint send --sprint <id> --to <planner-shortname> --body-file <path> \
  --key <stable-decision-handoff-key>
```

5. When the command confirms the durable write and Planner wake, stop
   immediately. Run no trailing command until another native wake arrives.
