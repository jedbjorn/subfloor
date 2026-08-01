---
name: sprint_dev
description: Execute a Sprints v2 Developer lane — accept one assignment, implement and verify it, own the PR through green and review, merge only after live authorization, and record judgment without overlapping edits.
category: workflow
common: false
---

# sprint_dev — own one editing lane

Use for an actionable work-unit assignment in an armed Sprint. Marking that
Sprint message read is acceptance and starts work immediately. If you cannot
accept, decline with a concrete reason; never leave it unread and waking.

## Orient and bound the lane

Read the assignment, expected output, bound spec revision, dependencies,
assigned Reviewer, repository/worktree, merge grant, and prior judgments. One
Developer shell may own one active work unit in the Sprint. Do not start a
second editing lane or edit another shell's worktree.

If the requirement is ambiguous, choose the shippable reading within your
unit's scope, record the choice and rationale, and continue. Escalate changes to
the unit boundary, interfaces another unit consumes, deliverable cuts, or scope
growth to the Planner.

## Build and verify

Sync the assigned repository, work on a feature branch, match the surrounding
code, and implement the smallest complete change. Keep external calls outside
database transactions. Preserve durable identities and append-only evidence.

Verification must exercise the unit's independent stage gate and realistic
failure paths. A local exploratory number is not merge evidence. Record real CI
failures, anomalous infrastructure failures, retries, review friction, and
known departures for the final report.

Register the PR through the authoritative Sprint surface and retain ownership
until it is green. The watcher supplies red/green facts; do not write PR state
yourself. On red, diagnose and fix the PR. On green, judge readiness rather
than forwarding mechanically.

```text
sc sprint register-pr --sprint <id> --repository <owner/name> \
  --pr <number> --work-unit <id>
```

## Review handoff

Put the readiness claim in a file, then use one stable retry key:

```text
sc sprint request-review \
  --sprint <id> --registered-pr <registered-id> \
  --readiness-file <path> --key <stable-key>
```

The assigned Reviewer receives an actionable request. Stop until its durable
outcome arrives. A changes-requested verdict opens a fresh linked fix
conversation and makes it current. Apply every blocking finding, re-establish
green, and hand back with a new stable review key. Record disagreements as
judgment; the Planner resolves scope/severity disputes.

## Merge boundary

Approval alone is stale evidence. Immediately before merge, ask the engine to
re-read live GitHub state and revalidate the armed grant, ownership, work-unit
state, approved head, and checks:

```text
sc sprint authorize-merge \
  --sprint <id> --registered-pr <registered-id>
```

Merge only the exact repository, PR number, and head SHA returned. If the
command refuses, do not work around it; wait for the watcher or return to the
appropriate loop. After merge, clean the worktree, submit the unit result and
judgments, and let automatic merge observation advance dependencies.

## Pause and stop

Pause immediately when integrity is threatened: broken base, destructive
ambiguity, unavailable GitHub, untrustworthy runners, provider exhaustion, or
an unrecoverable environment. State the short reason first; detailed judgment
can follow after pause is durable.

```text
sc sprint pause --sprint <id> --reason <integrity-threat>
```

Stop when the unit is merged and reported, declined, paused awaiting recovery,
or returned to review. Ask the Planner for later work only after the current
editing lane is terminal.
