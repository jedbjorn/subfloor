-- 0131 — teach sprint workers the explicit report-only review contract.
--
-- Runtime support alone is insufficient: existing forks rebuild these worker
-- instructions from the DB migration chain before the asset freshness healer
-- runs. Re-seed both affected role skills so the same update that installs the
-- runtime also teaches workers how to invoke it.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_dev',
  'Execute one Conductor-assigned sprint unit, ask the originating Planner for decisions, deliver an exact reviewed head, report the merge, and exit.',
  'craft',
  NULL,
  0,
  '# sprint_dev

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
  --payload ''{"question":"<decision>","alternatives":["A","B"],"evidence":["<fact>"],"downstream":["<effect>"]}''
```

Do not guess, message Planner directly, or boot another shell.

## Build and review gate

Use the recorded branch, preserve unrelated work, implement the smallest
complete change, and run the unit''s focused and full gates. When green:

```sh
sc directives emit ready-for-review \
  --target conductor --sprint <id> --unit <unit-id> \
  --payload ''{"pr_number":123,"head":"<sha>","branch":"feat/example","checks":"green","verification":["<gate>"]}''
```

Review approval is exact-head-bound. Any relevant change after `review-clean`
requires a new review. Merge only when the board still assigns this unit, all
required checks are green, dependencies are satisfied, and `review-clean`
names the exact head.

When the bounded investigation proves the requested state is already true and
the originating Planner explicitly rules that no change should be fabricated,
send the current integrated main head through the same independent review:

```sh
sc directives emit ready-for-review \
  --target conductor --sprint <id> --unit <unit-id> \
  --payload ''{"report_only":true,"pr_number":null,"head":"<main-sha>","branch":null,"checks":"report-only","verification":["<re-executed gate>"]}''
```

This is not a shortcut around implementation or review. Use it only for a
Planner-ratified no-diff outcome with concrete verification. Conductor moves a
clean report-only unit terminal after exact-head review. When that
`review-clean` returns, do not emit `merged`; emit only the `unit-report` below.

After merge emit both records:

```sh
sc directives emit merged \
  --target conductor --sprint <id> --unit <unit-id> \
  --payload ''{"pr_number":123,"head":"<approved-head>","merge_sha":"<sha>"}''
sc directives emit unit-report \
  --target conductor --sprint <id> --unit <unit-id> \
  --payload ''{"shipped":"<observable behavior>","judgements":[],"issues":[],"deviations":[],"follow_ups":[]}''
```

## Forbidden

Never write the sprint board, boot or kill shells, schedule polling, issue
Planner directives, approve your own head, merge without exact-head approval,
or continue after an unresolved decision request.

## Stop

Exit after the inspected transition directive for the turn. Completion means
the approved head is merged, both merge/report directives exist, and the
worktree is clean.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'sprint_rev',
  'Independently review one sprint unit at an exact head or run close-time conformance, route decisions to the originating Planner, emit one evidence-bound verdict, and exit.',
  'craft',
  NULL,
  0,
  '# sprint_rev

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
  --payload ''{"question":"<decision or evidence>","alternatives":["A","B"],"evidence":["<fact>"]}''
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
  --payload ''{"head":"<sha>","findings":[{"severity":"Major","location":"path:line","consequence":"<failure>","required":"<behavior>"}],"mutation":"<proof>"}''
```

Or exact-head approval:

```sh
sc directives emit review-clean \
  --target conductor --sprint <id> --unit <unit-id> \
  --payload ''{"head":"<sha>","findings":[],"mutation":"<proof>"}''
```

## Conformance

Require the integrated main SHA, full requirement scope, and complete
ratified-deviation list. Give every requirement exactly one verdict:
`as-specced`, `deviated-intentionally`, `deviated-silently`, or
`unimplemented`.

```sh
sc directives emit findings \
  --target conductor --sprint <id> \
  --payload ''{"mode":"conformance","main_sha":"<sha>","findings":[{"severity":"Major","requirement":"R1","evidence":"path:line"}]}''
sc directives emit review-clean \
  --target conductor --sprint <id> \
  --payload ''{"mode":"conformance","main_sha":"<sha>","verdicts":[{"requirement":"R1","verdict":"as-specced"}],"findings":[]}''
```

## Stop

Inspect the one emitted verdict and exit with no unrestored mutation. A clean
verdict never floats to another head.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
