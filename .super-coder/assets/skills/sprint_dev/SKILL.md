---
name: sprint_dev
description: Execute a developer unit in an ACTIVE structured sprint. Read the assigned unit, build within its scope, persist long work through sc job, open and register the PR, drive CI and review, merge the exact approved head under scoped authority, and send a structured unit report. Load when the boot sprint directive or a scoped task assigns a developer unit.
category: craft
common: false
---

# sprint_dev

Own the assigned unit from accepted task through merge.

## Activate from the record

The boot sprint directive names every active developer role. Read the scoped task
and board:

```sh
./sc mem message check
./sc sprint board --sprint <doc-id>
./sc mem get doc --doc <doc-id>
```

Confirm the unit, scope, dependency, reviewer, branch, and governing spec. The
board assigns ownership; the latest scoped task supplies current instructions.

Keep one current-state line while active:

```text
SPRINT doc=<id> unit=<seq> status=<pending|working|in_review|blocked>
```

Every transition and ruling request goes to the planner as a durable scoped
result:

```sh
./sc mem message send <planner> "<unit>: <transition or ruling request>" \
  --kind result --sprint <doc-id>
```

File findings, flags, PRs, and reports within your assigned authority. A question
printed only in final output reaches no sprint actor.

Message IDs are scoped to their recipient. Treat an ID from another actor's
inbox as provenance, never as a fetch instruction; the task row must quote the
substance you need. Ask the planner for that substance when it is absent.

For DB-assigned document, task, flag, and message IDs, use the ID returned by the
creating write. Confirm the target before an irreversible mutation. Refer to a
flag by `flag_id` plus its sprint-scoped label, such as
`#247 SC-S59-U8-ID-SPACES`. Establish absence through a complete direct read,
count, or exact-ID query. Before closing a flag by number, resolve and read back
its exact `flag_id`; display names and flag IDs share an integer range.

**Activation pass condition:** your reading of the task, board, and spec names
one executable unit and one observable completion condition.

## Make progress observable

The sprint reconciler compares the board's live expectations with positive work
and result evidence. It reports confirmed divergences to the planner; it never
changes the board or supervises the worker.

Send a scoped partial before any turn ends with work unfinished. Name completed
work, evidence, and the next untouched action. Send a scoped result when the
work finds nothing; "nothing found" is evidence the worker ran. A qualifying
result closes the reconciler's window for the current unit state.

Treat `read_at` in one direction only: READ proves something marked the row
read; UNREAD proves nothing about delivery, liveness, or work. Never infer a
fault or safe action from an unread marker.

## Resolve ambiguity before building

Test assumptions that can invalidate or dramatically simplify the unit. Send
the planner a ruling request when:

- the requested behavior has two materially different readings;
- the premise is false;
- a human-held credential or external action is required;
- the unit changes product meaning or another unit's interface;
- the work cannot stay inside its recorded resource boundary.

Include alternatives, evidence, and downstream effect. Mark the unit `blocked`
through the planner until the ruling arrives.

## Prepare and build

Load `spec` when a governing feature spec requires tracked tasks. Load `git`
before branch work. Sync your base and create one feature branch for the unit.

Branch from current `main` when the unit is independently buildable. Stack on an
upstream branch only for a real code dependency and accept the later retarget
and rebase duty.

Implement the smallest complete change. Verify in proportion to risk. Keep scope
changes as planner rulings or follow-up flags.

Give each high-value test method one property. When one setup must exercise
several properties, use `subTest` so an early assertion cannot mask the later
detectors.

Run session-surviving local suites, builds, and benches through:

```sh
./sc job start --label <slug> --timeout <seconds> -- <command> <args>
./sc job status <job-id>
./sc job tail <job-id>
./sc job wait --for <seconds> <job-id>
```

The job completion row is the durable transition. Use CI-to-CI measurements for
merge-gating performance claims.

## Open and register the PR

When dependencies are on `main` and the planner has released the unit:

1. Fetch and rebase onto `origin/main`.
2. Drain scoped messages immediately before the push.
3. Run the unit's verification gate.
4. Push and open the PR.
5. Register the planner's sprint watch.
6. Report the exact PR and head.

```sh
./sc watch pr <owner/repo> <pr-number> \
  --shell <planner> --sprint <doc-id>
./sc mem message send <planner> \
  "U1 pr-open: PR #123 head <sha>; verification <summary>" \
  --kind result --sprint <doc-id>
```

Update the board's branch and PR through the planner. An unregistered PR has no
event path back to orchestration.

A draft PR is a planner HOLD. Never mark it ready as a mechanical step; ask the
planner and wait for an explicit release.

## Drive CI

While live, use `gh pr checks <pr> --watch`. If the session ends, the registered
watch delivers later state to the planner.

Classify red checks:

- diff-caused: fix, verify, push, and report;
- runner, network, or known flake: rerun the failed job;
- failing independently on `main`: report evidence and block;
- uncertain after three honest fixes: report attempts and block.

One isolated rerun may distinguish a race from a diff-caused failure. Keep every
known flake or environmental exclusion enumerable as a skip or quarantine; a
known-flake list with no count is not a gate. Two repeated anomalous failures
escalate to the planner. Green means required checks completed successfully on
the merge head.

## Pass sprint review

After CI is green, send the planner an `in-review` result naming the PR and exact
head. The planner sends and boots the reviewer.

The planner returns Major and Medium findings as a scoped fix task. Fix them,
keep CI green, and report the new exact head for another review route. Low
findings enter follow-ups.

When a review ruling changes the file surface, report the new surface with the
ruling before continuing. The planner must re-send the overlap notice to every
affected concurrent sprint.

An explicit `review-clean` result at a known head is required for merge.

If `main` moves after review:

- compare intervening changes with this unit's files;
- preserve the verdict when surfaces are disjoint;
- rebase when surfaces overlap;
- compare the unit's contribution before and after rebase;
- return to the reviewer when the contribution changes or a hunk was
  hand-resolved.

Report the evidence and final head to the planner.

## Merge and report

Merge only when:

- the sprint document is ACTIVE and unfrozen;
- the board still assigns this unit to you;
- required checks are green on the merge head;
- the planner relayed a reviewer-clean verdict for that head or an explicit
  carry-through across a disjoint rebase;
- the planner's merge order releases the unit.

Drain scoped messages immediately before the merge. An earlier inbox check does
not satisfy this gate.

Merge with the repository's `git` procedure, then send one structured result:

```text
unit-report <doc-id> unit=<seq> pr=#<n>
shipped: <observable behavior>
judgements: <calls and final rulings, or none>
issues: <CI, stalls, review friction, or none>
deviations: <known departures and disposition, or none>
follow-ups: <Low findings and deferred work, or none>
```

Send it:

```sh
./sc mem message send <planner> "<unit-report body>" \
  --kind result --sprint <doc-id>
```

Clean the local branch according to `git`. Remove the sprint current-state line
when the unit reaches a terminal board state.

**Developer completion:** the recorded PR is merged at an approved green head,
the planner has the complete unit report, and the worktree is clean.
