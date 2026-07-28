---
name: sprint_orchestration_recover
description: Recover a declared sprint after a critical wake alert or a missing expected transition. Diagnose from the board, binding, messages, jobs, PR state, and worker process evidence; preserve completed work; apply one bounded repair; return to sprint_orchestration. Load only after the steady-state loop identifies a stall.
category: craft
common: false
---

# sprint_orchestration_recover

Recover one stalled transition without replaying completed work.

## Establish the last durable truth

Read:

```sh
./sc sprint board --sprint <doc-id>
./sc sprint status
./sc sprint alerts
./sc mem message check
./sc job list --all
./sc watch list --all
```

For the affected unit, confirm:

- last scoped task and result;
- board state, branch, PR, developer, and reviewer;
- live process or job evidence;
- PR head, checks, review state, and merge state;
- binding batch, park, quarantine, and alert state.

Quiet is a symptom. Declare a stall only when an expected transition is absent
and positive evidence shows no active process, job, review, CI run, or queued
wake that can produce it.

Validate every absence instrument against a known-positive target before using
its empty output. A missing command, rejected predicate, non-zero probe, or
positive control it cannot see makes the claim unmeasured, not absent. Inspect
the probe's exit status and stderr; never translate empty stdout alone into
"nothing is running."

A reconciler finding requests a checkup. For a headless shell:

1. Resolve the assigned subject from the sprint board, scoped task, launching
   planner, and worktree.
2. Find the harness process whose `/proc/<pid>/cwd` is that worktree.
3. Read `/proc/<pid>/stat` and reject state `Z`; a zombie directory and stat
   file are not liveness.
4. Sample `/proc/<pid>/stat` twice **5 seconds apart** and compare
   `utime + stime`. Five seconds is the interval for every sample in this
   procedure; take it inside the turn and never sleep longer to "be sure."
5. Treat a positive CPU delta as active work.
6. Treat process presence with no delta as indeterminate until the task,
   artifacts, and another 5-second sample establish progress or fault. A worker
   blocked on a provider response burns no CPU, so one flat sample is not stop
   evidence and no number of flat samples becomes one.
7. Treat no matching non-zombie process, combined with no live job or producing
   external operation, as positive stop evidence.

Interface availability, a missing Interface session row, and an open archive
describe session bookkeeping. Use them as context. `/proc` proves process
activity; the board and scoped task prove which sprint and unit that activity
belongs to.

Use reconciler explanations only to explain the reading. Transcript mtime, raw
`launch_shape` plus `cpu_delta`, persisted quota exhaustion with `resets_at`,
and process-local provider fault with its probe timestamp never decide whether
the worker is active.

**Diagnosis pass condition:** name the missing transition and the evidence that
its producer is inactive or unable to deliver it.

## Apply one recovery

| Diagnosis | Recovery |
|---|---|
| Worker stopped with a clean worktree and no artifact | Re-send the exact scoped task and boot the assigned shell. |
| Worker stopped with a branch, commit, PR, report, or dirty worktree | Preserve the owner and artifact. Send an idempotent continuation task naming the durable state, then boot that shell. |
| Worker is active | Leave it running. Record the observed process, send the scoped recovery result, and END THE TURN. The worker's own completion event is the next wake; a headless planner that stays resident to watch for it holds its session occupied and blocks the delivery it is waiting for. |
| Binding released while the sprint is still live | Re-arm it: `./sc sprint arm --sprint <doc-id>`, then `./sc sprint status`. Arming re-parents the released generation's queued wake items to the new binding and resolves the `binding_released_live_sprint` alert. Until it is armed the planner is deaf and nothing else will say so. |
| Unit must be abandoned | The planner cancels it — no one else may. Close the unit's PR on GitHub first (a close-without-merge retires its watch on the next poll; an unopened PR has no watch), state the branch and PR disposition in the cancellation result, then `./sc sprint unit state --sprint <doc-id> --seq <unit> cancelled`. `cancelled` is TERMINAL: it has no exit edge and the board refuses to move it back. Redo the work as a NEW unit at a new seq; the cancelled row stands as the record of what was declared. Close exempts cancelled units from the review-head requirement, so a cancellation never has to be dressed up as a review. |
| Session input parked with delivery unknown | Decide whether input landed, then run `./sc sprint retry --binding <id> --outcome delivered\|not_delivered`. |
| Wake batch parked before delivery | Run `./sc sprint retry --binding <id>`; the coordinator creates a new gated batch. |
| Item quarantined | Three completed wake turns left its message unread. Read and act on that message, mark it read, then clear the item: `./sc sprint retry --binding <id> --stuck quarantined --stuck-outcome requeue\|cancel`. Cancel when you have already acted; requeue only when the message still needs a wake turn. |
| Item parked in `reconcile` | An action receipt is still `intent` or `unknown`, so the coordinator will not requeue a side effect that may already have happened. Establish what actually landed, settle the receipt with `./sc sprint action reconcile <receipt-id> --detail "<what you established>"`, then clear the item with `./sc sprint retry --binding <id> --stuck reconcile --stuck-outcome requeue\|cancel`. Never clear the item first — the receipt is the evidence. |
| Reviewer unavailable | Reassign the board to an idle reviewer, send the exact-head request, and boot the new reviewer. |
| Developer unavailable before work starts | Reassign the board and task the replacement. |
| Developer unavailable after durable work exists | Hand the branch and exact head to a replacement; preserve authorship and report the handoff. |
| Real CI failure | Return a scoped fix task to the developer. |
| Runner, network, or known-flake failure | Rerun the failed job; escalate after two anomalous failures. |
| `main` is red independently | Hold dependent merges and add or assign a repair unit. |
| Provider auth, quota, or service failure | Surface the external condition to the FnB. Resume or reroute only after the operator changes the route. |
| Assignment or scope conflicts across parallel sprints | Stop the affected unit, resolve the hard-resource owner, update both boards, then resume one owner. |
| Product meaning or scope is ambiguous | Ask the FnB with the alternatives and downstream effect. |

If a timeout, bounded background task, or exiting shell wrapped `./sc run`, the
wrapper owns the worker's lifetime because `./sc run` executes the harness over
its own process. When the harness is still healthy, send SIGKILL to the wrapper
PID alone, never its process group. Recovery passes only when the harness PID
remains non-zombie and its parent becomes PID 1; otherwise preserve artifacts
and use the applicable stopped-worker row above.

Every continuation, reassignment, fix request, and recovery result uses
`--sprint <doc-id>`.

Compose message and task bodies via a file, never inline in a quoted shell
string: backticks and `$()` execute before `sc` receives the body. Write the
body to `./sprint-result.md`, send its content as one argument, then use
`message sent` to read the stored row back and confirm its body.

Record assignment changes before boot:

```sh
./sc sprint unit set --sprint <doc-id> --seq <unit> --dev <dev> --reviewer <rev>
./sc mem message send <worker> "$(<./sprint-result.md)" \
  --kind task --sprint <doc-id>
./sc mem message sent
```

Use `blocked` while the unit has no active path:

```sh
./sc sprint unit state --sprint <doc-id> --seq <unit> blocked
```

Move it back to `working` or `in_review` only after the replacement action is
durable.

## Engine-source recovery

When the sprint changes the engine that is running it, three trees answer
differently and you must name which one a claim is about:

- a feature worktree, which proves only what one shell has written;
- `origin/main`, which proves merged engine code — `git show origin/main:<path>`
  is the only reading that settles "did this land";
- the main checkout and its live DB, which define the running floor every
  `./sc` command actually executes.

A worktree being current does not update the running floor, so a stall
diagnosed against the wrong tree gets a confident wrong answer. Verify engine
claims against `origin/main`, and treat any behavior the floor contradicts as
evidence the floor is stale rather than as a defect in the merged code.

Defer pull, reconcile, migration of the live DB, and restart to a declared
sprint boundary — a restart kills live worker sessions, and the operator owns
that call. Surface the need to the FnB; never perform it as a recovery step.

Engine-source work itself belongs to the shell that owns the repo, under its
own procedure. Do not load that procedure here: the planner pack does not carry
it, and this section needs only the tree distinction above.

## Return to orchestration

Read the board and wake status again. Recovery succeeds when:

- the board names one active owner;
- the next transition has one producer;
- that producer has a durable scoped task or live job;
- the binding has no unresolved critical alert that blocks delivery; and
- completed artifacts were not duplicated or discarded.

Send a concise recovery result, then return to `sprint_orchestration`.
