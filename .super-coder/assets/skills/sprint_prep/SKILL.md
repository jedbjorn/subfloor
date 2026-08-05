---
name: sprint_prep
description: Prepare and arm a Sprints v2 run — bind reviewed spec revisions, shape work units and dependencies, assign routes and capacity, and refuse arming until the durable plan is eligible.
category: workflow
common: false
---

# sprint_prep — declare the riverbed

Use as the owning Planner while a Sprint is `prepared`. Preparation ends at one
atomic arming decision; it does not launch participants piecemeal.

Use the simplest path supported by current durable state. Treat authority,
lifecycle preconditions, durable writes, and typed handoffs as hard boundaries;
use judgment for planning and evidence gathering within them. Repeat a read only
when later activity could have changed it or the final command requires live
revalidation.

## Outcome

Produce one editable prepared Sprint with:

- one roadmap feature;
- exact governing spec revision hashes and their qualifying QAQC approvals;
- work units made from existing spec tasks, each with one Developer and one
  assigned Reviewer;
- dependency edges and planned waves;
- one validated harness/model/effective effort selection per participant;
- a committed Sprint merge grant; and
- a capacity plan sized to justified parallel work and review demand, with the
  local/GitHub capacity to execute it.

The arming transaction validates every recorded selection (explicit null
model/effort means the route default), records the armed transition, publishes
the initial assignment messages, and declares a New wake to the overseeing
Planner. Defaults satisfy the gate, but dispatch never precedes that validation.
Participant chats are created or re-entered later by wake delivery.

## Eligibility pass

Read the feature, selected spec bodies, task ledgers, QAQC records, shell roster,
model routes, quota state, repository access, and worktree availability. Record
the exact revision hash you inspected; a title or document id is not a revision.

The Review shell records its verdict against the current exact body through the
authenticated Sprint surface:

```text
sc sprint record-qaqc --document <spec-document-id> --verdict pass \
  [--findings-document <document-id>]
```

Use `fail` until every blocking finding is resolved. A body edit changes the
revision hash and therefore needs a fresh signed record.

Request pre-Sprint QAQC explicitly from the Review shell through the ordinary
shell-to-shell channel. No Sprint relay or inbox exists yet. Once the signed
approval id is available, continue preparation here; after arming, switch to
`sprint_pln`.

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

### Balance capacity and parallelism

Optimize for the smallest participant set that keeps justified critical-path
development and review moving without avoidable queues. Neither minimum
headcount nor maximum shell occupancy is a goal.

Before choosing participants, analyze the task ledger and dependency graph for
coherent non-overlapping editing lanes, expected readiness, critical-path work,
and likely review demand. Put dependency-free Developer lanes in the same wave
and plan Reviewer capacity so ready reviews can run alongside ongoing
independent development. Do not serialize work merely because it appears in
task order, split coherent work, or start a review before its unit is ready just
to create concurrency.

- For one coherent small lane, normally use one Developer and one Reviewer.
- Add a Developer only when another independent lane can start or make useful
  progress without conflicting ownership and has enough review capacity.
- Add Reviewer capacity when expected concurrent review demand would otherwise
  queue critical-path work. Reuse a Reviewer across units when their review
  readiness is unlikely to overlap.
- Leave eligible capacity unassigned when the roster allows, preserving room
  for correction, re-plan, or urgent work. Use every eligible shell only when
  the work graph and review demand justify simultaneous work and coordination
  cost does not erase the expected time-to-completion gain.

Record the capacity rationale: chosen participants, parallel lanes, expected
review overlap, retained reserve, and why another shell would or would not
shorten the critical path.

For every participant, record role, route, model, and effective effort. Never
pretend a native session can resume across harnesses.

Declare the prepared envelope from a JSON array of participant objects, then
add each editing lane from existing spec tasks:

```text
sc sprint declare --feature <feature-id> \
  --spec-approval <approval-id> --participants-file <path> --merge-grant
sc sprint plan-unit --sprint <id> \
  --developer-shell <id> --reviewer-shell <id> --title <title> \
  --expected-output-file <path> --task <task-id> \
  [--task <task-id>] [--wave <n>] [--depends-on <work-unit-id>] \
  [--output-kind code|report-only|no-code]
```

The participant file contains `shell_id`, `role`, and `harness`, with optional
`model`, `effort`, and `route`. FnB may add `--planner-shell <id>` when declaring
for the originating Planner. Keep the Sprint prepared while shaping the plan.

## Final arming check

Immediately before arming, re-read the spec revision hashes, QAQC records,
participant capacity, single-armed invariant, repository access, and merge
grant. The final read and durable plan commit belong to the authoritative
arming transaction; external harness and GitHub work occurs after it commits.

Arming succeeds only when the first assignments and wake intents are durable.
A process crash after commit is outbox recovery; a crash before commit exposes
no partial Sprint.

```text
sc sprint arm --sprint <id>
```

After `arm` succeeds, participant pickup belongs to native delivery. The armed
runtime dispatches ready work and wake recovery reconciles unread pickup; the
preparing Planner does not manually boot participants or create a second wake
path. Initial assignments use Force-new delivery; a live turn reaches its
natural boundary before delivery and the runtime owns rotation and recovery.

## Handoff

Once armed, hand control to `sprint_pln` and stop preparation work. Give the FnB
a compact declaration:
Sprint id, feature, exact spec revisions, participants/routes, work-unit graph,
planned waves, capacity rationale and reserve, merge-grant state, and known
accepted risks.

Stop when the Sprint is armed or when one concrete eligibility blocker has been
surfaced. Do not dispatch from a partially prepared plan.
