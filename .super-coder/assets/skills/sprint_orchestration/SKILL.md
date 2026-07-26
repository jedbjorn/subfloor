---
name: sprint_orchestration
description: Run the steady-state planner loop for a multi-shell sprint. Declare the sprint and structured unit board, assign developers and reviewers, arm event-driven wake, dispatch scoped tasks, advance units from durable events, and route merge order. Load sprint_orchestration_recover only when an expected transition stalls or an alert opens. Load sprint_orchestration_close only when every unit is terminal and main is green.
category: craft
common: false
---

# sprint_orchestration

Coordinate the sprint from durable records. Keep code context in worker shells
and coordination context here.

Use three procedures:

- This skill: declare, dispatch, monitor, and advance the normal path.
- `sprint_orchestration_recover`: diagnose and repair a stalled transition.
- `sprint_orchestration_close`: run conformance, revoke authority, and report.

Load a chain skill only when its trigger is true.

## 1. Establish the boundary

Confirm the objective, governing spec or decisions, and success condition with
the FnB. Ask two routing questions:

1. Which harness and model should every developer use?
2. Which harness and model should every reviewer use?

Treat account configuration and billing limits as operator-managed inputs. Use
the selected routes.

Read active decisions before choosing architecture or sequencing:

```sh
./sc mem get decisions
./sc mem get decisions <id>
```

For concurrent sprints, partition hard resources before dispatch:

- shells;
- branches and worktrees;
- migrations and other globally ordered identifiers;
- files with substantial overlap;
- external environments or exclusive services.

Give each sprint a distinct label and record every reservation. Read back
globally allocated IDs after creation; a planned number is provisional until
the authoritative store confirms it.

Use `S<doc-id>-U<seq>` in messages, flags, and reports. Unit labels are unique
only inside one sprint.

Keep the declared shell sets exclusive. Transfer a shell through an explicit
planner-to-planner handoff recorded on both sprint documents before booting it.

**Pass condition:** the sprint has one objective, one authority source, resolved
routes, and no silently shared hard resource.

## 2. Cut units and merge surfaces

Define units that one developer can own through merge. Add dependency edges
only for real code dependencies.

Predict each unit's file surface and compare intersections:

- empty intersection: parallel;
- shared files: sequence the units or record an explicit overlap protocol;
- one file shared by three or more units: reconsider the cut.

Assign one developer and one reviewer to every unit. Balance reviewer load
against the dependency graph.

Define the merge rule at kickoff:

- A verdict is bound to the reviewed head SHA.
- Unrelated movement on `main` preserves the verdict.
- Overlapping movement requires a rebase and an interference check.
- A changed contribution or hand-resolved hunk returns to the reviewer.

**Pass condition:** every unit has an owner, reviewer, dependency list, predicted
surface, and deterministic route to merge.

## 3. Declare the sprint

Create one unfrozen `documents` row titled `SPRINT: <title>`, kind `doc`. Keep
the body concise:

```markdown
# SPRINT: <title>
status: ACTIVE
declared: <date> · planner: <shortname>
models: devs=<harness>/<model> · reviewers=<harness>/<model>
spec: <doc id or decision ids>
success: <observable outcome>
resource reservations: <migration ranges, shells, environments>
```

The structured board lives in `sprint_units`; render it with:

```sh
./sc sprint board --sprint <doc-id>
```

Create its rows:

```sh
./sc sprint unit add --sprint <doc-id> --seq U1 --title "<unit>" \
  --dev <shortname> --reviewer <shortname> \
  --depends-on <U0|none> --overlap "<protocol|none>"
```

Use `unit set` for assignments, branch, PR, dependencies, and overlap. Use
`unit state` as the sole state writer:

```sh
./sc sprint unit set --sprint <doc-id> --seq U1 --branch feat/example --pr 123
./sc sprint unit state --sprint <doc-id> --seq U1 working
```

States are `pending`, `working`, `in_review`, `blocked`, `merged`, and
`cancelled`.

**Pass condition:** `./sc sprint board --sprint <doc-id>` reproduces the complete
assignment and sequencing plan without reading prose.

## 4. Arm event-driven wake

Arm the planner binding from the Interface Sprint wake panel or
`POST /api/interface/sprint-bindings` with:

```http
Idempotency-Key: sprint-bind-<doc>-<planner>-<attempt>

{"sprint_doc_id": <doc-id>, "planner_shell_id": <planner-shell-id>}
```

Generate one caller-stable attempt value and reuse it for transport retries of
that attempt. Generate a fresh value for a later re-arm.

Verify:

```sh
./sc sprint status
./sc sprint alerts
```

The binding must report armed with no critical alert before the first worker
boot.

PR events and scoped task/result rows drive the loop. Avoid scheduled trackers
and session-bound inbox waiters.

## 5. Dispatch ready work

Every sprint `task` and `result` row carries `--sprint <doc-id>`. This makes the
event wake-eligible and keeps parallel sprints separable.

Send exact assignments:

```sh
./sc mem message send <dev> \
  "Unit U1: <scope>. Spec <id>. Dependencies <list>. Reviewer <rev>. Start <now|after U0>. Load sprint_dev." \
  --kind task --sprint <doc-id>
```

The board reserves each reviewer. Dispatch the reviewer when a developer reports
a green exact head:

```sh
./sc mem message send <reviewer> \
  "Review U1 at PR #123 head <sha>. Major/Medium block; Low informs. Return the verdict to me. Load sprint_review." \
  --kind task --sprint <doc-id>
```

Boot only the actor that owns the next transition:

```sh
./sc run <dev> --harness <harness> -m <model> --effort high
```

Treat the board as assignment truth and the task row as the current instruction.
Update both before a reassignment or scope change.

**Pass condition:** every ready unit is `working`, every waiting unit is
`pending`, and the producer of each expected transition has one scoped task
naming the same unit record.

## 6. Advance from events

On each wake:

1. Drain inbox rows.
2. Read `./sc sprint board --sprint <doc-id>`.
3. Read `./sc sprint status` and `./sc sprint alerts`.
4. Apply the smallest state transition supported by the event.
5. Dispatch newly ready work.

Use this routing table:

| Event | Planner action |
|---|---|
| Developer starts | Confirm `working`. |
| PR opens | Record branch and PR; confirm its sprint watch exists. |
| Developer reports green review-ready head | Move unit to `in_review`; send the assigned reviewer an exact-head task and boot it. |
| Reviewer reports Major/Medium | Send a scoped fix task to the developer; keep `in_review`. |
| Reviewer reports clean + checks green | Send the developer a scoped merge instruction for the reviewed head. |
| PR merged + unit report | Move unit to `merged`; dispatch newly unblocked units. |
| Declared blocker | Move unit to `blocked`; rule on mechanics or escalate judgment. |
| Reconciler checkup or critical wake alert | Treat it as a request to verify, then load `sprint_orchestration_recover`. |
| All units terminal and `main` green | Load `sprint_orchestration_close`. |

Register each PR with the planner at PR open:

```sh
./sc watch pr <owner/repo> <number> --shell <planner> --sprint <doc-id>
```

Monitor before interrupting a worker. Send a new task when behavior must change,
not to request generic progress.

At each merge, resolve the merge base and compare the executed file surface with
every concurrent sprint. Send surface deviations to the affected planner.
Declarations record intent; merged files are the overlap evidence.

## 7. Preserve the authority boundary

An ACTIVE, unfrozen sprint grants:

- assigned developers: merge their unit after green checks and explicit
  review-clean;
- assigned reviewers: file findings and return a recommendation to the planner.

That authority is scoped to the recorded sprint and assigned unit. The close
procedure freezes the document and ends it.

Escalate changes to product meaning, scope, interface contracts, or human-only
inputs to the FnB. Resolve sequencing, reassignment, retries, CI triage, and
worker boots within the declared boundary.

## Steady-state completion

This procedure is complete when either:

- the next expected unit transition is dispatched and the binding is healthy;
- a diagnosed stall triggers `sprint_orchestration_recover`; or
- every unit is terminal with green `main`, triggering
  `sprint_orchestration_close`.
