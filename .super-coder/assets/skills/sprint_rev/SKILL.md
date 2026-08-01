---
name: sprint_rev
description: Review Sprints v2 work and whole-Sprint conformance — apply the Medium-and-above gate, record precise verdicts through the authenticated surface, and route conformance findings only to post-Sprint follow-ups.
category: workflow
common: false
---

# sprint_rev — independent review and conformance

Use in one of two modes: a work-unit PR review during the loop, or the final
whole-Sprint conformance pass. The evidence differs; independence does not.
On every wake or re-entry, load `sprint_rev`, run the exact inbox command below,
and inspect the durable message before deciding what to do.

Read and accept the actionable request before beginning. During preparation,
sign the exact current spec revision through the same authenticated surface:

```text
sc sprint inbox --sprint <id>
sc sprint accept --sprint <id> --message <message-id>
sc sprint record-qaqc --document <spec-document-id> \
  --verdict pass [--findings-document <document-id>]
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

- `as-specced`;
- `deviated-intentionally` with its ratified judgment;
- `deviated-silently`; or
- `unimplemented`.

The last two are findings. Include spec document id and work-unit id when known.
Write the narrative report and a JSON findings array:

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

## Stop

For unit review, stop after the durable verdict is recorded. For conformance,
re-run `sc sprint inbox --sprint <id>`, act on newly arrived messages, and stop
after the report and all findings replay idempotently and give the Planner their
report/follow-up ids.
