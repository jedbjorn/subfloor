---
name: sprint_rev
description: Review Sprints v2 work and whole-Sprint conformance — own review, re-enter, abort, and conclude judgments, author the conformance and Sprint reports, and direct safety actions through durable messages.
category: workflow
common: false
---

# sprint_rev — independent review and conformance

Load `sprint_protocol` first; it holds the lifecycle, wake types, inbox
commands, relay contract, body limits, artifact paths, receipt recovery, and
authority boundary. This skill holds only the Reviewer's steps: pre-declaration
QA/QC, one work-unit review, or whole-Sprint conformance.

## Route the entry

| Entry | Route |
|---|---|
| Explicit pre-declaration QA/QC request | Read and sign the exact current spec directly; there is no Sprint id or Sprint inbox yet. |
| Work-unit review / `sprint.delivery_terminal` | Inspect the Sprint inbox once; accept the actionable request. |
| Live FnB instruction | Preserve board-level authority; read only durable state needed for independent judgment. |

QA/QC precedes all Sprint inbox commands and is one write:

```text
sc mem doc qaqc <spec-document-id> --verdict pass|fail [--findings-doc <document-id>]
```

Reviewers never receive PR-event wakes.

## Conformance decisions and Planner controls

You own review, re-enter, abort, and conclude judgments. The Planner
independently owns operational plan structure: safe-edit pauses, recalling
unreleased work, lane changes/repeats, assignment/routing, unreleased-scope
cancellation, and validated resume. Never run standalone pause, replan,
recall, reroute, cancel, resume, complete, or abort actions; clean
`record-conformance` alone performs its narrow atomic close.

Base judgment on durable Sprint state, bound revisions, current work/PR facts,
progress-carrier evidence, and ratified judgments. A decision body names:

- `decision`: `re-enter`, `abort`, or the exact safety-critical recommendation;
- Reviewer-owned evidence + rationale;
- exact Sprint/unit ids, reason, outcome, and complete action arguments;
- immediate safety impact for FnB.

The Planner verifies your identity and executes the transition without
surrendering plan authority. A rejected action requires a revised judgment
supported by returned durable state, never an improvised bypass. A live FnB
instruction is the FnB's board-level override.

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
severity does not decide timing: you judge whether each finding requires
in-Sprint patching or acceptable post-Sprint follow-up.

## Work-unit review

Accept the request and retain that exact message id. Its body is a bare
locator: intent, PR URL, registered PR id, exact head, work-unit id. Scope
narrative, verification, rationale, or focus steering is a protocol defect. PR
comments and annotations are forbidden; the PR body contains only unit id +
spec reference plus the Developer's rationale.

Bind inspection/verdict to the accepted request's message id, registered PR,
and work unit. Review the live PR head; a rebase since the locator's head is
not a defect. Read the exact spec revision + full diff, then checks, tests,
relevant runtime facts, and ratified judgments. Each round is clean: no prior
Developer evidence or prose; prior findings clear only when the new head
proves it. Trace code paths, failure cases, and spec behavior rather than
names or PR prose.

### Red-check doctrine

Accepted-red is not a legal review outcome. A departure that leaves checks
failing is never acceptable; the handoff remains green-only, without exception
or waiver: do not note the failure and approve anyway.

- In-scope failure -> record `changes_requested` so the Developer fixes them and restores green.
- Out-of-scope failure -> keep the lane unapproved and send the Planner a `replan`
  decision naming the failures; Planner widens the lane or cuts follow-up work.

Read cited and feature-scoped resolved flag evidence through memory
(`sc mem get flags <flag-id>`, `sc mem get flags --feature <id> --resolved`).

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
  --verdict changes_requested|approved --body-file <path> --key <stable-key>
```

5. Require durable judgment evidence + the Developer wake. Run no trailing
   command; stop.

Use `approved` only with no Critical/Major/Medium finding. Engine validation
requires the accepted request. Do not message around the surface; an
unrecorded verdict cannot unlock merge.

## Delivery-terminal closeout

Retain the exact notification message id + delivered wake as this closeout
episode's identity. Proceed only when the notification names this shell as the
selected conformance owner for its current ownership generation. A different
Reviewer accepts the informational notification if received and records no
conformance. Inspect inbox, lifecycle, and units first:

- Already completed/aborted -> `accept` notification and stop.
- Any non-terminal unit visible -> the wake is stale: `accept`, stop, and
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

- **In-Sprint patching required.** Do not run `record-conformance`. Send the
  Planner a durable `re-enter` decision with every blocking finding; each spec
  task's title and description; grouping, waves, dependencies, routing, and
  capacity rationale. State independent lanes, expected review overlap, useful
  reserve, and critical-path effect. After three re-entry episodes, escalate
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

Before recording conformance, author the final Sprint report. Name yourself as
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

Require the receipt: conformance report id, final report id, follow-up ids,
completed state, Planner message id, and Planner wake id. Closing ends the
Developer chats and schedules deletion of the Sprint artifact directory; your
chat persists and no worktree is reset. Do not poll cleanup, wait before
stopping, or manually close peer chats. Never reopen editing after recording;
a re-enter defers reports until new scope is terminal and a fresh
delivery-terminal wake arrives.

## Stop

Unit review ends with the ordered `record-review` write as the literal final
action.

For closeout, first re-run `sc sprint inbox --sprint <id>`, handle + `accept`
new messages, then confirm every artifact/body is final and below 8,000.

- Clean conclude -> run the atomic `record-conformance` command above as the
  literal final action. When it confirms completed state and all receipt
  identities, stop immediately; the Planner is notified.
- Re-enter/abort -> as literal final action send the Sprint-level `decision`
  to the Planner (relay form in `sprint_protocol`), require durable write +
  Planner wake, then stop immediately. Run no trailing command until another
  native wake.
