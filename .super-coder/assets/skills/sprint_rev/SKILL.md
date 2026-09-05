---
name: sprint_rev
description: Review Sprints v2 work and whole-Sprint conformance — own review, re-enter, abort, and conclude judgments, author the conformance and Sprint reports, and direct safety actions through durable messages.
category: workflow
common: false
---

# sprint_rev — independent review and conformance

Use for pre-declaration QAQC, one work-unit review, or whole-Sprint
conformance. The Reviewer decides review/conformance; the Planner owns
operational plan structure + control execution. FnB retains the board-level
override from decision #46.

Use the simplest path supported by current durable state. Treat independence,
authority, lifecycle preconditions, durable writes, and typed handoffs as hard
boundaries. Repeat a read only when later activity could have changed it or the
next command requires live revalidation.

## Route the entry

Classify the entry before reading an inbox:

| Entry | Route |
|---|---|
| Explicit pre-declaration request | Read/sign the exact current spec directly; there is no Sprint id or Sprint inbox to inspect yet. |
| Work-unit review / `sprint.delivery_terminal` | Inspect the Sprint inbox once; accept the actionable request. |
| Live FnB instruction | Preserve board-level authority; read only durable state needed for independent judgment. |

QAQC precedes all Sprint inbox commands:

```text
sc sprint record-qaqc --document <spec-document-id> \
  --verdict pass [--findings-document <document-id>]
```

For an armed Sprint, load `sprint_rev` on every entry:

```text
sc sprint inbox --sprint <id>
sc sprint accept --sprint <id> --message <message-id>
sc sprint decline --sprint <id> --message <message-id> --reason <reason>
```

Decline actionable work only with a concrete reason. After handling an
informational message, run `accept`; it marks the message read and does not
change Sprint or work-unit state.

An unusable success receipt from idempotent bookkeeping does not stall the
Sprint. Retry the exact command once, then use its normal read surface once to
prove the exact postcondition. For informational `accept`, prior inbox presence
+ absence of that exact message id proves the read landed. Continue under that
proof + name the receipt defect in the next normal handoff. NEVER use this
recovery to infer assignment ownership, review outcome, merge authorization,
lifecycle/work-unit transition, governing revision, PR head/green state, or
cleanup authority. An unproved postcondition stops.

Review requests and verdicts use Force-new delivery. Planner decisions use
Re-enter. Delivery waits for a natural boundary; the runtime owns bundling,
rotation, and recovery. Stop after a successful typed handoff. Reviewers never
receive PR-event wakes.

## Relay contract and authority

Ask the Developer for unit evidence with a unit-scoped question/blocker:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --intent question --requires-reply --work-unit <work-unit-id> \
  --key <stable-key>
```

Use `--intent blocker` when the unit cannot advance. Cross-unit, closeout,
re-enter, abort, and safety rulings are Sprint-level decisions:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --intent decision --requires-reply --sprint-level --key <stable-key>
```

Reply through the original message; the server inherits its scope, so never
add `--work-unit` or `--sprint-level` to a reply:

```text
sc sprint send --sprint <id> --to <shortname> --body-file <path> \
  --intent information --reply-to <message-id> --key <stable-key>
```

Confirm a reply, then `accept` its incoming message. Missing facts stop review
at the decision boundary; unread recovery re-wakes, so send no duplicate.
A stable key identifies recipient + exact body + intent + reply + scope. Reuse
it only for the same failed/ambiguous write; when any of those fields changes,
use a new key.

Keep bodies near 6,000 characters and below the 8,000 hard maximum; run
`wc -m < <path>`. A handoff completes only when the command exits successfully
and confirms the durable write + wake. If a command is rejected or transport fails,
the verdict/handoff is incomplete. Correct and retry safely. If the relay itself fails,
give FnB the attempted command, evidence, impact, and recommendation; invent no
alternate protocol.

## Sprint artifact paths

Keep review notes, diffs, evidence, reports, and scratch proof in gitignored
`shared/sprints/sprint-<n>/`; never commit, branch, or PR them. Durable records
belong in `record-review`, `sprint_reports`, and the relay.

## Conformance decisions and Planner controls

Reviewer owns review, re-enter, abort, and conclude judgments. Planner
independently owns operational plan structure: safe-edit pauses, recalling
unreleased work, lane changes/repeats, assignment/routing, unreleased-scope
cancellation, and validated resume. Reviewer never runs standalone pause,
replan, recall, reroute, cancel, resume, complete, or abort actions; clean
`record-conformance` alone performs its narrow atomic close.

Base judgment on durable Sprint state, bound revisions, current work/PR facts,
progress-carrier evidence, and ratified judgments. Every Reviewer→Planner route
is Re-enter. A decision body names:

- `decision`: `re-enter`, `abort`, or the exact safety-critical recommendation;
- Reviewer-owned evidence + rationale;
- exact Sprint/unit ids, reason, outcome, and complete action arguments;
- immediate safety impact for FnB.

Planner verifies Reviewer identity and executes the transition without
surrendering plan authority. A rejected action requires a revised judgment
supported by returned durable state, never an improvised bypass. A live FnB
instruction supersedes as distinct FnB board-level override authority under
decision #46.

## Severity rubric

- **Critical** — active security/authority violation, destructive corruption,
  or unsafe continued operation.
- **Major** — wrong behavior, data loss, broken invariant, material spec
  violation, or silently wedged delivery/recovery.
- **Medium** — concrete normal-use correctness/recovery gap, missing negative
  enforcement, or unreliable handoff.
- **Low** — bounded cleanup, clarity, test-depth, or resilience improvement;
  delivered behavior remains correct.

Critical/Major/Medium block unit approval; Low is a report note. At closeout,
severity does not decide timing: Reviewer judges whether each finding requires in-Sprint patching
or acceptable post-Sprint follow-up.

## Work-unit review

Accept the request and retain that exact message id. Its body is a bare locator:
intent, PR URL, registered PR id, exact head, work-unit id. Scope narrative,
verification, rationale, or focus steering is a protocol defect. PR comments
and annotations are forbidden; PR body contains only unit id + spec reference.

Bind inspection/verdict to the accepted request's message id, registered PR,
and work unit. Review the live PR head; a rebase since the locator's head is
not a defect. Read the exact spec revision + full diff, then checks, tests,
relevant runtime facts,
and ratified judgments. Each round is clean: no prior Developer evidence or
prose; prior findings clear only when the new head proves it. Trace code paths,
failure cases, and spec behavior rather than names or PR prose.

### Red-check doctrine

Accepted-red is not a legal review outcome. A departure that leaves checks
failing is never acceptable; the handoff remains green-only, without exception
or waiver: do not note the failure and approve anyway.

- In-scope failure -> record `changes_requested` so the Developer fixes them and restores green.
- Out-of-scope failure -> keep the lane unapproved and send the Planner a `replan`
  decision naming the failures; Planner widens the lane or cuts follow-up work.

Read cited and feature-scoped resolved flag evidence through memory, never SQL:

```text
sc mem get flags <flag-id>
sc mem get flags --feature <feature-id> --resolved
```

Each finding pins severity/title, violated invariant, exact location/evidence,
reproducible consequence, and fix boundary without unnecessary architecture.

Complete a unit verdict in this exact order:

1. Finish every inspection, finding, and verdict body.
2. Re-run `sc sprint inbox --sprint <id>` once; handle + `accept` new items.
3. Run `wc -m < <path>`; require near 6,000 and below 8,000 characters.
4. As the literal final action, run:

```text
sc sprint record-review \
  --sprint <id> --registered-pr <registered-id> \
  --verdict changes_requested --body-file <path> --key <stable-key>
```

5. Require durable judgment evidence + Developer Force-new wake. Run no trailing command;
   stop.

Use `approved` only with no Critical/Major/Medium finding. Engine validation
requires the accepted request. Do not message around the
surface; an unrecorded verdict cannot unlock merge.

## Delivery-terminal closeout

Retain the exact notification message id + delivered wake as this closeout
episode's identity. Proceed only when the notification names this shell as the
selected conformance owner for its current ownership generation. A different
Reviewer accepts the informational notification if received and records no
conformance. Inspect inbox, lifecycle, and units first:

- Already completed/aborted -> `accept` notification and stop.
- If any non-terminal unit is visible, the wake is stale -> `accept`, stop, and
  await a fresh delivery-terminal episode.
- Only an armed Sprint whose units are all terminal enters conformance.

Compile the bounded evidence packet first, yourself:

```text
sc sprint compile-report --sprint <id> --limit 50 \
  > shared/sprints/sprint-<n>/evidence.json
```

Increase only when truncation omitted needed evidence; maximum 200. Judge
integrated `main` against every bound/current revision + ratified judgment. All
units cancelled and nothing shipped -> `abort`, not `conclude`.

Choose one branch:

- **In-Sprint patching required.** Do not run `record-conformance`. Send Planner
  a durable `re-enter` decision with every blocking finding; each spec task's
  title and description; grouping, waves, dependencies, routing, and capacity
  rationale. State independent lanes, expected review overlap, useful reserve,
  and critical-path effect. After three re-entry episodes, escalate
  non-convergence to FnB.
- **Clean or post-Sprint-only findings.** Prepare conformance report, findings,
  final report, reason, and outcome; submit the atomic close below. Send no
  conclude message.

## Whole-Sprint conformance

Review the integrated system, not unit diffs. Classify every requirement
`as-specced`, `deviated-intentionally` with ratified judgment,
`deviated-silently`, or `unimplemented`; the last two are findings. Include
spec document + work-unit ids when known.

For the clean branch, write a conformance report and JSON findings array with
`severity`, `title`, `body`, `spec_document_id`, and `work_unit_id`. Keep the
report and each body near 6,000 and below 8,000 characters; run
`wc -m < <report>` and validate each body.

Before recording conformance, author the final Sprint report. Name Reviewer as
author and cover governing scope/revisions, shipped units/PRs, judgments +
ratified deviations, failures/retries/recovery/anomalies, conclusion,
follow-ups, and evidence location. Keep it near 6,000 and below 8,000; preserve
discrepancies.

Record one atomic final write:

```text
sc sprint record-conformance \
  --sprint <id> --body-file <report> --findings-file <json> \
  --final-report-file <final-report> --reason <reason> --outcome <outcome> \
  --key <stable-pass-key>
```

Require receipt: conformance report id, final report id, follow-up ids,
completed state, Planner message id, and Planner wake id. The transaction adds
append-only evidence, follow-ups, terminal state, and one informational engine-wide Planner Re-enter;
send no conclude message. Require cleanup projection `pending`; cleanup runs
after participant turns exit. Do not reset a worktree, poll cleanup, or wait
before stopping. The Planner receives the later engine-authored receipt.
Successful conformance also closes other Sprint-linked
chats while the originating Planner + report-authoring Reviewer stay open. Do
not manually close peer chats. Pause, abort, re-entry, failed conformance, and
rejected fallback retain no-cleanup behavior. Never reopen editing after recording; a re-enter defers
reports until new scope is terminal and a fresh delivery-terminal wake arrives.

## Stop

Unit review ends with the ordered `record-review` write as the literal final
action.

For closeout, first re-run `sc sprint inbox --sprint <id>`, handle + `accept`
new messages, then confirm every artifact/body is final and below 8,000.

- Clean conclude -> run the atomic `record-conformance` command above as the
  literal final action. When it confirms completed state, pending cleanup, and
  all receipt identities, stop immediately; Planner is notified.
- Re-enter/abort -> as literal final action send:

```text
sc sprint send --sprint <id> --to <planner-shortname> --body-file <path> \
  --intent decision --requires-reply --sprint-level \
  --key <stable-decision-handoff-key>
```

Require durable write + Planner wake, then stop immediately. Run no trailing
command until another native wake.
