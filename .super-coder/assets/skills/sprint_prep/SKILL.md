---
name: sprint_prep
description: Prepare and arm a Sprints v2 run — bind reviewed spec revisions, shape work units and dependencies, assign routes and capacity, and refuse arming until the durable plan is eligible.
category: workflow
common: false
---

# sprint_prep — declare the riverbed

Use as the owning Planner while a Sprint is `prepared`. Preparation ends at one
atomic arming decision; it does not launch participants piecemeal.

## Outcome

Produce one editable prepared Sprint with:

- one roadmap feature;
- exact governing spec revision hashes and their qualifying QAQC approvals;
- work units made from existing spec tasks, each with one Developer and one
  assigned Reviewer;
- dependency edges and planned waves;
- one primary harness/model/effective effort per participant plus eligible
  Planner fallback capacity;
- a committed Sprint merge grant; and
- enough local/GitHub capacity to execute the plan.

The arming transaction creates every participant conversation, the initial
assignment messages and wake intents, and the armed transition together.

## Eligibility pass

Read the feature, selected spec bodies, task ledgers, QAQC records, shell roster,
model routes, quota state, repository access, and worktree availability. Record
the exact revision hash you inspected; a title or document id is not a revision.

Refuse arming when any of these is true:

- a selected current revision lacks Review-shell QAQC approval;
- any Medium-or-higher QAQC finding is unresolved;
- a selected task belongs to no work unit or more than one work unit;
- a dependency cycle exists;
- a work unit lacks an assigned Developer or Reviewer;
- participant routes or required capacity are unavailable;
- another Sprint is armed, or a selected shell already participates in an armed
  Sprint; or
- the merge grant was not committed as part of the final plan.

Deficiencies remain editable in `prepared`. Do not weaken an invariant merely
to get to `armed`; surface the missing fact or capacity to the FnB.

## Shape work, do not script behavior

A work unit is one coherent editing lane and may group related spec tasks. Use
dependencies only for hard prerequisites. Waves express intent and later report
comparison; they do not forbid safe out-of-order completion. Reviews are not
editing lanes.

Prefer the smallest dependency graph that preserves correctness. Record the
expected output in outcome language. Do not encode a shell's implementation
steps into the durable plan when its role skill and judgment can decide them.

For every participant, record role, route, model, effective effort, persistent
conversation ownership, and fallback facts the plan actually depends on. Never
pretend a native session can resume across harnesses.

## Final arming check

Immediately before arming, re-read the spec revision hashes, QAQC records,
participant capacity, single-armed invariant, repository access, and merge
grant. The final read and durable plan commit belong to the authoritative
arming transaction; external harness and GitHub work occurs after it commits.

Arming succeeds only when the first assignments and wake intents are durable.
A process crash after commit is outbox recovery; a crash before commit exposes
no partial Sprint.

## Handoff

Once armed, hand control to `sprint_pln`. Give the FnB a compact declaration:
Sprint id, feature, exact spec revisions, participants/routes, work-unit graph,
planned waves, merge-grant state, and known accepted risks.

Stop when the Sprint is armed or when one concrete eligibility blocker has been
surfaced. Do not dispatch from a partially prepared plan.
