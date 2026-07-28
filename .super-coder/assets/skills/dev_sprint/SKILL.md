---
name: dev_sprint
description: Execute one ephemeral Conductor developer slot. Build the launcher-assigned sprint unit, resolve ambiguity through `ask-planner`, send `ready-for-review`, merge only at an approved green head, and emit the structured unit report. Load only from `sc run <dev> --slot dev --sprint <id> [--unit U]`.
category: craft
common: false
---

# dev_sprint

Execute only the unit or units in the mandatory slot section. Emit one
transition directive for the turn, then exit. After a merge, also emit the
required `unit-report`.

## Establish the assignment

Read the slot context, relayed prompt, sprint board, and governing document:

```sh
sc sprint board --sprint <doc-id>
sc directives list --status pending --sprint <doc-id>
sc mem get documents --doc <doc-id>
```

Use the boot section's numeric unit ID for `--unit`; `U1` is display sequence.
Load `git` before branch work and `spec` when the governing feature tracks
tasks.

**Pass:** the assignment names one bounded code outcome, its dependencies,
branch/PR state, and its observable verification gate.

## Ask before guessing

Stop when the request has materially different readings, its premise is false,
a credential/human action is required, or the work crosses the recorded scope.
Emit:

```sh
sc directives emit ask-planner \
  --target conductor \
  --sprint <doc-id> \
  --unit <numeric-unit-id> \
  --payload '{"question":"<decision required>","alternatives":["<A>","<B>"],"evidence":["<fact>"],"downstream":["<effect>"]}'
```

Do not send a message, boot the planner, or continue on an assumed answer.

**Pass:** the question contains alternatives, evidence, and downstream effect,
and the returned directive ID inspects as pending.

## Build

Sync the recorded base, create or resume the recorded unit branch, and implement
the smallest complete change. Preserve unrelated work. Run focused checks, then
the unit's full gate. Use `sc job` for work that must outlive the harness
turn.

Do not write the sprint board or schedule polling. The Conductor applies
mechanical transitions from directives; the sentinel observes liveness.

When implementation and required checks are green, push/open the PR and emit:

```sh
sc directives emit ready-for-review \
  --target conductor \
  --sprint <doc-id> \
  --unit <numeric-unit-id> \
  --payload '{"pr_number":123,"head":"<sha>","branch":"feat/example","checks":"green","verification":["<gate>"]}'
```

**Pass:** the directive names the exact PR head and the checks that ran.

## Respond to review

Apply relayed Major/Medium findings, preserve Low findings as follow-ups, rerun
the affected gate, and emit a new `ready-for-review` at the new exact head.

If a finding or rebase changes the recorded scope, emit `ask-planner` instead.
Never treat a clean verdict for an old head as approval for a changed
contribution.

## Merge and report

Merge only when all conditions hold:

- the sprint document is active and unfrozen;
- the board still assigns the unit to this shell;
- required checks are green on the exact head;
- a `review-clean` directive names that head;
- the planner's order releases the unit.

After the merge, emit the transition:

```sh
sc directives emit merged \
  --target conductor \
  --sprint <doc-id> \
  --unit <numeric-unit-id> \
  --payload '{"pr_number":123,"head":"<approved-head>","merge_sha":"<sha>"}'
```

Then emit the report:

```sh
sc directives emit unit-report \
  --target conductor \
  --sprint <doc-id> \
  --unit <numeric-unit-id> \
  --payload '{"shipped":"<observable behavior>","judgements":[],"issues":[],"deviations":[],"follow_ups":[]}'
```

Read both IDs back with `sc directives inspect <id>`, then clean the local
branch according to `git`.

**Developer completion:** the approved green head is merged, `merged` and
`unit-report` are pending for the Conductor, and the worktree is clean.
