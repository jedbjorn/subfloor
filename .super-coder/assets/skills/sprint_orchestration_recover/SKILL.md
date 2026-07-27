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
4. Sample `/proc/<pid>/stat` twice over a bounded interval and compare
   `utime + stime`.
5. Treat a positive CPU delta as active work.
6. Treat process presence with no delta as indeterminate until the task,
   artifacts, and another bounded sample establish progress or fault.
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
| Worker is active | Leave it running. Record the observed process and wait for its bounded completion event. |
| Session input parked with delivery unknown | Decide whether input landed, then run `./sc sprint retry --binding <id> --outcome delivered\|not_delivered`. |
| Wake batch parked before delivery | Run `./sc sprint retry --binding <id>`; the coordinator creates a new gated batch. |
| Item quarantined | Read and act on that message manually, then send a fresh scoped task for any remaining action. |
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

Record assignment changes before boot:

```sh
./sc sprint unit set --sprint <doc-id> --seq <unit> --dev <dev> --reviewer <rev>
./sc mem message send <worker> "$(<./sprint-result.md)" \
  --kind task --sprint <doc-id>
./sc mem message sent
```

Before sending, write the complete continuation body to `./sprint-result.md`; after sending, read the stored row with `message sent` and confirm its body.

Use `blocked` while the unit has no active path:

```sh
./sc sprint unit state --sprint <doc-id> --seq <unit> blocked
```

Move it back to `working` or `in_review` only after the replacement action is
durable.

## Engine-source recovery

When the sprint changes the engine that is running it, load `engine_surgery`.
Distinguish:

- feature worktree source;
- `origin/main`, which proves merged engine code;
- the main checkout and live DB, which define the running floor.

Defer pull, reconcile, migration of the live DB, and restart to a declared sprint
boundary. A feature worktree being current does not update the running floor.

## Return to orchestration

Read the board and wake status again. Recovery succeeds when:

- the board names one active owner;
- the next transition has one producer;
- that producer has a durable scoped task or live job;
- the binding has no unresolved critical alert that blocks delivery; and
- completed artifacts were not duplicated or discarded.

Send a concise recovery result, then return to `sprint_orchestration`.
