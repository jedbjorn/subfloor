---
name: rev_sprint
description: Execute one ephemeral Conductor reviewer slot. Review an assigned unit at an exact head and emit `findings` or `review-clean`, or run the folded-in close-time conformance pass when launched without `--unit`. Load only from `./sc run <reviewer> --slot rev --sprint <id> [--unit U]`.
category: craft
common: false
---

# rev_sprint

Use the base adversarial review discipline inside the mandatory slot scope.
Emit one reviewer directive for the turn, then exit. Never merge, write the
board, boot another shell, poll, or wait.

## Select the mode

Read the slot context, relayed prompt, board, and governing document:

```sh
./sc sprint board --sprint <doc-id>
./sc directives list --status pending --sprint <doc-id>
./sc mem get documents --doc <doc-id>
```

- Focused numeric unit in the slot -> unit review.
- No unit focus + conformance prompt -> close-time conformance over the
  integrated sprint.
- Any other ambiguity -> emit `ask-planner`.

Use the boot section's numeric unit ID for `--unit`; `U1` is display sequence.

**Pass:** the mode, exact code target, governing requirements, and completion
verdict are unambiguous.

## Ask before reviewing the wrong target

For a missing/superseded head, unclear scope, missing ratified deviations, or
overlap requiring a rebase, emit:

```sh
./sc directives emit ask-planner \
  --target conductor \
  --sprint <doc-id> \
  --unit <numeric-unit-id> \
  --payload '{"question":"<decision or evidence required>","alternatives":["<A>","<B>"],"evidence":["<fact>"]}'
```

Omit `--unit` for conformance. Do not continue on an assumed target.

## Review a unit

Pin the PR and exact head from the prompt. Confirm required checks belong to
that head. Trace correctness, error handling, boundary/empty/concurrent/partial
failure states, authorization, scope, and test strength.

For a high-value property, mutate the implementation or condition, prove the
relevant test fails, restore it, and prove the test passes.

Emit blocking and nonblocking findings together:

```sh
./sc directives emit findings \
  --target conductor \
  --sprint <doc-id> \
  --unit <numeric-unit-id> \
  --payload '{"head":"<sha>","findings":[{"severity":"Major","location":"path:line","consequence":"<observable failure>","required":"<behavior>"}],"mutation":"<property/result>"}'
```

When no finding remains, emit:

```sh
./sc directives emit review-clean \
  --target conductor \
  --sprint <doc-id> \
  --unit <numeric-unit-id> \
  --payload '{"head":"<sha>","findings":[],"mutation":"<property/result>"}'
```

A clean verdict applies only to the named head and a later head whose
contribution is proven unchanged across disjoint interference.

**Pass:** exactly one `findings` or `review-clean` directive names the reviewed
head and mutation evidence.

## Run conformance

Use this mode only when the prompt supplies the integrated main SHA, complete
requirement scope, and ratified-deviation list. Judge shipped code, not PR
narratives.

Give every requirement exactly one verdict:

- `as-specced`
- `deviated-intentionally`
- `deviated-silently`
- `unimplemented`

Attach location, observable consequence, and Major/Medium/Low severity to every
silent deviation or unimplemented requirement.

Emit gaps:

```sh
./sc directives emit findings \
  --target conductor \
  --sprint <doc-id> \
  --payload '{"mode":"conformance","main_sha":"<sha>","verdicts":[{"requirement":"<id>","verdict":"unimplemented","severity":"Major","evidence":"path:line"}]}'
```

Emit clean conformance:

```sh
./sc directives emit review-clean \
  --target conductor \
  --sprint <doc-id> \
  --payload '{"mode":"conformance","main_sha":"<sha>","verdicts":[{"requirement":"<id>","verdict":"as-specced"}],"findings":[]}'
```

**Conformance pass:** every requirement has one verdict and the directive names
the integrated SHA.

## Exit gate

Read the emitted ID with `./sc directives inspect <id>`.

**Reviewer completion:** one valid reviewer directive addressed to `conductor`
contains the exact target, evidence, and verdict; the worktree contains no
unrestored mutation.
