---
name: sprint_dev
description: Execute one Conductor-assigned sprint unit, ask the originating Planner for decisions, deliver an exact reviewed head, report the merge, and exit.
category: craft
common: false
---

# sprint_dev

Own only the unit in the mandatory slot context. Conductor owns the board,
worker boots, relays, and dependency release. The originating Planner owns
decisions. You build, prove, report, and exit.

## Establish the boundary

```sh
sc sprint board --sprint <id>
sc directives list --status pending --sprint <id>
sc mem get documents --doc <governing-spec-id>
```

Confirm assignment, dependencies, branch, overlap, and observable completion
gate. Never widen scope from neighboring units.

When meaning, scope, authority, or evidence is ambiguous, stop and emit:

```sh
sc directives emit ask-planner \
  --target conductor --sprint <id> --unit <unit-id> \
  --payload '{"question":"<decision>","alternatives":["A","B"],"evidence":["<fact>"],"downstream":["<effect>"]}'
```

Do not guess, message Planner directly, or boot another shell.

## Build and review gate

Use the recorded branch, preserve unrelated work, implement the smallest
complete change, and run the unit's focused and full gates. When green:

```sh
sc directives emit ready-for-review \
  --target conductor --sprint <id> --unit <unit-id> \
  --payload '{"pr_number":123,"head":"<sha>","branch":"feat/example","checks":"green","verification":["<gate>"]}'
```

Review approval is exact-head-bound. Any relevant change after `review-clean`
requires a new review. Merge only when the board still assigns this unit, all
required checks are green, dependencies are satisfied, and `review-clean`
names the exact head.

After merge emit both records:

```sh
sc directives emit merged \
  --target conductor --sprint <id> --unit <unit-id> \
  --payload '{"pr_number":123,"head":"<approved-head>","merge_sha":"<sha>"}'
sc directives emit unit-report \
  --target conductor --sprint <id> --unit <unit-id> \
  --payload '{"shipped":"<observable behavior>","judgements":[],"issues":[],"deviations":[],"follow_ups":[]}'
```

## Forbidden

Never write the sprint board, boot or kill shells, schedule polling, issue
Planner directives, approve your own head, merge without exact-head approval,
or continue after an unresolved decision request.

## Stop

Exit after the inspected transition directive for the turn. Completion means
the approved head is merged, both merge/report directives exist, and the
worktree is clean.
