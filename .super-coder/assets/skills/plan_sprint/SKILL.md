---
name: plan_sprint
description: Decide and direct one ephemeral Conductor planner slot. Decompose a declared sprint, assign models and units, rule on questions or sentinel evidence, re-scope or re-task work, launch close-time conformance, and close only after the integrated result passes. Load only from `./sc run <planner> --slot plan --sprint <id>`.
category: craft
common: false
---

# plan_sprint

Make the sprint decision requested by the slot prompt, emit the corresponding
planner directive, then exit. The Conductor relays and writes mechanics; never
boot another shell, write the sprint board, poll, or wait.

## Establish the record

Read the mandatory slot section and the relayed prompt. Confirm the sprint,
unit IDs, dependencies, assignments, and current states:

```sh
./sc sprint board --sprint <doc-id>
./sc directives list --status pending --sprint <doc-id>
./sc mem get documents --doc <doc-id>
```

Treat the boot section's numeric unit ID as the `--unit` value. Treat `U1` as
the display sequence only.

**Pass:** one requested planner decision is grounded in the current board,
governing document, and supplied evidence.

## Select one procedure

| Prompt / state | Decide | Emit |
|---|---|---|
| New declaration | decomposition, order, assignments, model routes | one `kickoff` per released unit |
| Worker question | answer from spec + evidence | `answer` |
| False or unresolved sentinel verdict | whether work continues | `answer`, `hold`, `re-task`, or `re-scope` |
| Scope or dependency changed | new bounded assignment/order | `re-scope` or `re-task` |
| Units terminal, conformance absent | reviewer + integrated SHA/scope | `kickoff` for conformance |
| Conformance clean | final disposition | `close` |
| Required evidence missing | what must be supplied | `hold` |

Do not infer a human choice. A declaration prompt must carry the FnB-approved
planner/dev/reviewer model routes. Missing approval -> emit `hold` naming the
missing choice.

## Declare

Decompose into independently verifiable units. Name dependencies, overlap,
developer, reviewer, model route, scope, and completion evidence. Release only
units whose dependencies are satisfied.

Emit each released assignment to the Conductor:

```sh
./sc directives emit kickoff \
  --target conductor \
  --sprint <doc-id> \
  --unit <numeric-unit-id> \
  --payload '{"to":"DEV1","unit_seq":"U1","instruction":"<bounded task + gate>","model":"<approved route>","depends_on":[],"overlap":[]}'
```

**Pass:** every kickoff identifies one recipient, one numeric unit, one bounded
instruction, one approved model route, and observable completion evidence.

## Rule

Use the spec and supplied evidence; do not replace missing evidence with a
guess.

Answer a worker:

```sh
./sc directives emit answer \
  --target conductor \
  --sprint <doc-id> \
  --unit <numeric-unit-id> \
  --payload '{"to":"DEV1","question_directive_id":42,"answer":"<ruling>","evidence":["<fact>"]}'
```

Hold unsafe progress:

```sh
./sc directives emit hold \
  --target conductor \
  --sprint <doc-id> \
  --unit <numeric-unit-id> \
  --payload '{"reason":"<missing or conflicting evidence>","next":"<required proof>"}'
```

Re-task the same outcome or re-scope the outcome itself:

```sh
./sc directives emit re-task \
  --target conductor \
  --sprint <doc-id> \
  --unit <numeric-unit-id> \
  --payload '{"to":"DEV1","instruction":"<replacement execution path>","reason":"<evidence>"}'

./sc directives emit re-scope \
  --target conductor \
  --sprint <doc-id> \
  --unit <numeric-unit-id> \
  --payload '{"to":"DEV1","scope":"<new boundary>","reason":"<ruling>","downstream":["U2"]}'
```

**Pass:** the directive states the decision, evidence, recipient, and downstream
effect; it contains no mechanical board mutation.

## Close

When all units are terminal, emit a conformance kickoff to a reviewer. Name the
integrated SHA, full requirement scope, and complete ratified-deviation list.
Conformance uses `rev_sprint` without a unit focus.

```sh
./sc directives emit kickoff \
  --target conductor \
  --sprint <doc-id> \
  --payload '{"to":"REV1","mode":"conformance","main_sha":"<sha>","scope":"all requirements","ratified_deviations":[],"model":"<approved route>"}'
```

After a clean conformance directive, emit:

```sh
./sc directives emit close \
  --target conductor \
  --sprint <doc-id> \
  --payload '{"main_sha":"<sha>","conformance_directive_id":84,"summary":"<shipped outcome>"}'
```

Never close with a nonterminal unit, an unresolved Major/Medium finding, red
required checks, or missing conformance.

## Exit gate

Read back the emitted directive ID with `./sc directives inspect <id>`.

**Planner completion:** every requested decision is represented by a valid
planner directive addressed to `conductor`; no shell was booted, no board row
was written, and no scheduled poll was started.
