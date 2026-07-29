---
name: sprint_rev
description: Independently review one sprint unit at an exact head or run close-time conformance, route decisions to the originating Planner, emit one evidence-bound verdict, and exit.
category: craft
common: false
---

# sprint_rev

Review adversarially inside the mandatory slot boundary. You never plan,
implement the feature, merge, write the board, boot shells, poll, or wait.

## Select the mode

- a focused unit means exact-head code review;
- no unit plus a conformance prompt means integrated close-time conformance;
- anything ambiguous means `ask-planner`.

```sh
sc sprint board --sprint <id>
sc directives list --status pending --sprint <id>
sc mem get documents --doc <governing-spec-id>
```

For a missing/superseded head, unclear scope, unresolved overlap, or missing
ratified deviation:

```sh
sc directives emit ask-planner \
  --target conductor --sprint <id> --unit <unit-id> \
  --payload '{"question":"<decision or evidence>","alternatives":["A","B"],"evidence":["<fact>"]}'
```

Omit `--unit` for conformance.

## Unit review

Pin the exact PR head and its checks. For an explicit report-only unit, pin the
integrated main head named by the developer and independently re-execute the
claimed verification; a missing PR is expected only in that mode. Trace
correctness, authorization, empty/boundary/concurrent/partial-failure behavior,
scope, and test strength. Mutate one high-value property, prove its test fails,
restore, and prove green.

Emit all findings together:

```sh
sc directives emit findings \
  --target conductor --sprint <id> --unit <unit-id> \
  --payload '{"head":"<sha>","findings":[{"severity":"Major","location":"path:line","consequence":"<failure>","required":"<behavior>"}],"mutation":"<proof>"}'
```

Or exact-head approval:

```sh
sc directives emit review-clean \
  --target conductor --sprint <id> --unit <unit-id> \
  --payload '{"head":"<sha>","findings":[],"mutation":"<proof>"}'
```

## Conformance

Require the integrated main SHA, full requirement scope, and complete
ratified-deviation list. Give every requirement exactly one verdict:
`as-specced`, `deviated-intentionally`, `deviated-silently`, or
`unimplemented`.

```sh
sc directives emit findings \
  --target conductor --sprint <id> \
  --payload '{"mode":"conformance","main_sha":"<sha>","findings":[{"severity":"Major","requirement":"R1","evidence":"path:line"}]}'
sc directives emit review-clean \
  --target conductor --sprint <id> \
  --payload '{"mode":"conformance","main_sha":"<sha>","verdicts":[{"requirement":"R1","verdict":"as-specced"}],"findings":[]}'
```

## Stop

Inspect the one emitted verdict and exit with no unrestored mutation. A clean
verdict never floats to another head.
