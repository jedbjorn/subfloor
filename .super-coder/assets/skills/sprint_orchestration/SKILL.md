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

Reserve the close-time conformance reviewer now, while reviewer independence is
still available. Record that shell's conflict set: units authored, unit reviews
performed, rulings supplied, and any other reason it would grade its own work.
Pass only with a named reviewer whose recorded set is disjoint from conformance
scope, or an explicit FnB disposition for every unavoidable conflict.

Define the merge rule at kickoff:

- A verdict is bound to the reviewed head SHA.
- Unrelated movement on `main` preserves the verdict.
- Overlapping movement requires a rebase and an interference check.
- A changed contribution or hand-resolved hunk returns to the reviewer.

**Pass condition:** every unit has an owner, reviewer, dependency list, predicted
surface, and deterministic route to merge.

## 3. Declare the sprint

Create one unfrozen `documents` row titled `SPRINT: <title>`, kind `doc`, linked
to its governing roadmap feature with `--feature <feature-id>`. Keep the body
concise:

```markdown
# SPRINT: <title>
status: ACTIVE
declared: <date> · planner: <shortname>
models: devs=<harness>/<model> · reviewers=<harness>/<model>
spec: <doc id or decision ids>
success: <observable outcome>
resource reservations: <migration ranges, shells, environments>
```

Create the row and confirm the sprint document appears under its structured
feature:

```sh
./sc mem doc add "SPRINT: <title>" --kind doc --feature <feature-id> \
  --body-file <draft.md>
./sc mem get documents --feature <feature-id>
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

**Pass condition:** the `<feature-id>` document read-back names the sprint
document, and
`./sc sprint board --sprint <doc-id>` reproduces the complete assignment and
sequencing plan without reading prose.

## 4. Arm event-driven wake

A confirmation that is not about the thing it appears to confirm is not a
confirmation. Verify the durable artifact that governs the next action.

Declare the board before you arm. A sprint doc with no `sprint_units` rows is
not yet live, so a binding armed ahead of section 3 arms against a sprint the
wake path does not recognize.

Arm the binding with the verb; it owns its own idempotency key, so a transport
retry and a later re-arm both need nothing from you:

```sh
./sc sprint arm --sprint <doc-id>
```

A shell arms only itself. `--planner <shell-id>` is the operator's argument;
never pass it to arm a different shell.

Verify:

```sh
./sc sprint status
./sc sprint alerts
```

The binding must report armed with no critical alert before the first worker
boot.

PR events and scoped task/result rows drive the loop. Avoid scheduled trackers
and session-bound inbox waiters.

When the sprint pauses, disarm it:

```sh
./sc sprint disarm --sprint <doc-id> --reason pause
./sc sprint status --all
```

Disarm cancels the binding's queued wake items and leaves their messages
UNREAD — the rows survive, the wake turns do not. Drain the inbox before
disarming, or accept that those items are delivered only by the next re-arm.
Read `./sc sprint status --all` back and stop only when the binding reports
released or disarmed; plain status hides released bindings and cannot prove
release. A paused sprint may remain ACTIVE and unfrozen; it must not remain
armed.

## 5. Dispatch ready work

Every sprint `task` and `result` row carries `--sprint <doc-id>`. This makes the
event wake-eligible and keeps parallel sprints separable.

Compose message and task bodies via a file, never inline in a quoted shell
string: backticks and `$()` execute before `sc` receives the body. Write the
body to `./sprint-result.md`, send its content as one argument, then use
`message sent` to read the stored row back and confirm its body.

Send exact assignments:

```sh
./sc mem message send <dev> "$(<./sprint-result.md)" \
  --kind task --sprint <doc-id>
./sc mem message sent
```

Quote every fact the worker needs in its own task row. A message ID addressed to
another shell is provenance, never a fetch instruction; inbox IDs are
recipient-scoped.

A message you sent is not an instruction the recipient has received. Treat
delivery and receipt as separate facts; do not infer receipt from the send
result or from an unread row.

The board reserves each reviewer. Dispatch the reviewer when a developer reports
a green exact head:

```sh
./sc mem message send <reviewer> "$(<./sprint-result.md)" \
  --kind task --sprint <doc-id>
./sc mem message sent
```

Boot only the actor that owns the next transition:

```sh
./sc run <dev> --harness <harness> -m <model> --effort high
```

Run `./sc run` from a durable interactive planner process. It executes the
harness over its own process, so a timeout, bounded background task, or exiting
wrapper becomes the worker's lifetime. If a wrapper already owns a live harness,
load `sprint_orchestration_recover` and unwrap it there.

Dispatch passes when the harness PID is non-zombie at the assigned worktree and
no temporary launcher bounds its lifetime.

Treat the board as assignment truth and the task row as the current instruction.
Update both before a reassignment or scope change.

**Pass condition:** every ready unit is `working`, every waiting unit is
`pending`, and the producer of each expected transition has one scoped task
naming the same unit record.

## 6. Advance from events

On each wake:

1. Drain inbox rows.
2. Mark each row read as you fold it in.
3. Read `./sc sprint board --sprint <doc-id>`.
4. Read `./sc sprint status` and `./sc sprint alerts`.
5. Apply the smallest state transition supported by the event.
6. Dispatch newly ready work.

```sh
./sc mem message check
./sc mem message mark-read <message-id>
```

Step 2 is the loop's completion contract, not bookkeeping. Infrastructure never
marks a message read; the wake coordinator reconciles each delivered item from
`read_at` alone. A row you acted on but left unread is requeued for another
wake turn, and the THIRD completed wake still finding it unread quarantines the
item and opens an alert — so a fully handled message that was never marked
costs three planner turns and then reports itself as a fault. Mark it read in
the same turn you act on it; never mark a row you have not acted on.

Record a receipt around any action that is destructive, externally visible, or
long enough to outlive the turn — a merge, a freeze, a boot, a release:

```sh
./sc sprint action begin --message <id> --operation <verb> --target <thing>
./sc sprint action complete <receipt-id> --detail "<what landed>"
```

Begin BEFORE the side effect, complete AFTER observing its result. `begin`
returning `SUPPRESSED` means a completed receipt already covers this action —
do not perform it again. When the result is genuinely unobserved, record
`./sc sprint action unknown <receipt-id>` rather than guessing: an open
`intent` or `unknown` receipt is what makes the coordinator park that item in
`reconcile` with a `wake_item_reconcile` alert instead of blindly requeuing a
side effect that may already have happened. Settle such a receipt with
`./sc sprint action reconcile <receipt-id> --detail "<what you established>"`
once you know what actually landed; the parked wake item itself is a separate
exit, and `sprint_orchestration_recover` owns it.

The reconciler runs independently of PR watches and returns a report-only
reading for every live sprint expectation. Each reading carries `expectation`,
`signal`, `confirmed`, `evidence`, `measurement`, `observed_at`, and
`explanation`; classification never writes. After confirmation, every
actionable signal produces one `planner_alerts` row keyed
`(sprint_doc_id, unit_id, role, signal)`; `shell_id` stays off-key. A planner
message is only the push layer and is emitted only when `board_writer` resolves
a recipient. Open alerts dedupe, resolve when evidence returns, and re-arm after
resolution.

Read `sc watch list` for the reconciler's own heartbeat. A stale `reconcile`
line means the watchdog may be wedged; no alerts from a stale watchdog do not
prove a quiet sprint.

Treat the explanation tier as WHY, never WHETHER: transcript mtime, the raw
argv-derived `launch_shape` plus `cpu_delta`, persisted quota exhaustion with
`resets_at`, and process-local provider fault with its probe timestamp may
explain a reading but never change its classification.

Use this routing table:

| Event | Planner action |
|---|---|
| Developer starts | Confirm `working`. |
| Developer declares its branch | Record it: `./sc sprint unit set --sprint <doc-id> --seq <unit> --branch <name>`. |
| PR opens | Record the PR; confirm the developer's sprint watch exists. |
| Developer requests a ruling | Rule on mechanics, sequencing, or scope inside the boundary; escalate product meaning, contracts, or human-only inputs to the FnB. Move the unit to `blocked` only while it has no path; return the ruling as a scoped task naming the unit. |
| Developer reports green review-ready head | Move unit to `in_review`; send the assigned reviewer an exact-head task and boot it. |
| Reviewer reports Major/Medium | Send a scoped fix task to the developer; keep `in_review`. |
| Reviewer reports clean + checks green | Record `--review-head <sha>`, then send the developer a scoped merge instruction for that head. |
| PR merged + unit report | Move unit to `merged`; dispatch newly unblocked units. |
| Declared blocker | Move unit to `blocked`; rule on mechanics or escalate judgment. |
| Reconciler checkup | Report-only. Verify against the board and the worker's own evidence; route to recovery only if that verification finds a real stall. |
| Critical wake alert | Treat it as a request to verify, then load `sprint_orchestration_recover`. |
| All units terminal and `main` green | Load `sprint_orchestration_close`. |

The developer registers its own PR watch at PR open — they hold the PR context.
Confirm the registration rather than repeating it:

```sh
./sc watch list
```

A missing or unscoped watch is the developer's to fix; re-register it yourself
only when that shell is gone.

The board's `branch` column is what makes a heads-down developer classify as
`working` rather than as a stalled one, so record a declared branch on the wake
turn that carries it — the developer cannot write the board and the column
stays empty until you fill it.

Monitor before interrupting a worker. Send a new task when behavior must change,
not to request generic progress.

Never use a draft PR as a planner hold. A state the worker's own procedure
teaches it to clear cannot carry a stop; record the hold in board state plus a
scoped task. No reliable interrupt exists today, and flag #321 owns that gap.
A stop signal indistinguishable from an obstacle is an instruction to proceed;
this rule does not close flag #321.

When a review ruling changes a unit's file surface, update the overlap record
and re-send the surface notice to every affected concurrent planner before the
next durable action. The ruling that widens the surface triggers the notice.

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
