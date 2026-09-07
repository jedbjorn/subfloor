---
name: sprint_prep
description: Prepare and arm a Sprints v2 run — bind exact current specs, optionally gather QA/QC evidence, shape work units and dependencies, and enforce every launch invariant.
category: workflow
common: false
---

# sprint_prep — declare the riverbed

Load `sprint_protocol` first. Use this as the owning Planner while a Sprint is
`prepared`. Preparation ends at one atomic arming decision; it does not launch
participants piecemeal.

## Outcome

Produce one editable prepared Sprint with:

- one roadmap feature;
- exact governing spec revision hashes and any optional QA/QC evidence;
- work units made from existing spec tasks, each with one Developer and one
  assigned Reviewer;
- dependency edges and planned waves;
- one harness/model/Thinking level (`effort`) intent per participant;
- the Sprint merge grant — the FnB's merge authorization for every registered
  Sprint PR, given by deciding to run the Sprint and recorded with
  `--merge-grant`; the engine refuses to declare or arm without it; and
- a capacity plan sized to justified parallel work and review demand, with the
  local/GitHub capacity to execute it.

Preview every participant with
`sc models resolve <harness> [<model>] [--effort <level>]`; omit `--effort` for
Vibe and Harness default. Pass = each controlled preview names the requested
Thinking level and each uncontrolled preview returns explicit `effort: null`.
Arm binds every route, records the armed transition, and publishes the first
assignments in one transaction; any mismatch rolls the whole arm back.

## Eligibility pass

Read the feature, selected spec bodies, task ledgers, available QA/QC records,
shell roster, model routes, quota state, repository access, and worktree
availability. Record the exact revision hash you inspected; a title or document
id is not a revision.

The FnB decides whether pre-Sprint QA/QC is useful. If requested, ask the
Review shell through ordinary inbox mail (no Sprint relay exists yet); it
signs the current exact body with:

```text
sc mem doc qaqc <spec-document-id> --verdict pass|fail [--findings-doc <document-id>]
```

The record is inspectable evidence, not launch authorization. Its absence,
verdict, findings, revision age, or signer state never blocks declaration or
arming. A body edit makes the prior record historical evidence. Proceed with
preparation regardless of whether review was performed or what it found.

Refuse arming when any of these is true:

- no current non-empty `spec` document belonging to the feature is bound;
- a bound spec body changed after declaration, so its current hash no longer
  matches the exact declared revision;
- a selected task belongs to no work unit or more than one work unit;
- a dependency cycle exists;
- a work unit lacks an assigned Developer or Reviewer;
- participant routes or required capacity are unavailable;
- another Sprint is armed, or a selected shell already participates in an armed
  Sprint.

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

For every participant, record role, route, nullable model, and Thinking level
(`effort`). Controlled exact routes default omitted effort to `high` only when
the preview proves support. Set both model and effort to JSON null for Harness
default. Vibe requires effort null and reports **Thinking control unavailable**.
Never pretend a native session can resume across harnesses.

Declare the prepared envelope from a JSON array of participant objects, binding
each current governing document directly. The server reads and hashes the body
inside the declaration transaction; the client never supplies a revision hash.
Then add each editing lane from existing spec tasks:

```text
sc sprint declare --feature <feature-id> \
  --spec <spec-document-id> --participants-file <path> --merge-grant
sc sprint plan-unit --sprint <id> \
  --developer-shell <id> --reviewer-shell <id> --title <title> \
  --expected-output-file <path> --task <task-id> \
  [--task <task-id>] [--wave <n>] [--depends-on <work-unit-id>] \
  [--output-kind code|report-only|no-code]
```

Repeat `--spec` for multiple governing documents. The deprecated
`--spec-approval <approval-id>` selector remains compatible when an old caller
must also retain a specific review row as evidence, but its verdict and reviewed
revision do not affect eligibility and direct `--spec` is canonical.

The participant file contains `shell_id`, `role`, and `harness`, with optional
nullable `model`, Thinking level (`effort`), and `route`. FnB may add
`--planner-shell <id>` when declaring for the originating Planner. Keep the
Sprint prepared while shaping the plan.

## Final arming check

Immediately before arming, select exactly one participating Reviewer as the
whole-Sprint conformance owner. Then re-read the exact spec revision hashes,
available QA/QC evidence, task coverage, participant routes and capacity,
single-armed invariant, repository access, and merge grant. Review evidence is
summarized, never interpreted as authorization. A prior Sprint's cleanup never
gates arming.

```text
sc sprint arm --sprint <id> --conformance-reviewer-shell <shell-id>
```

Require the receipt to identify the selected owner and one revision-1 binding
for every participant. After `arm` succeeds, participant pickup belongs to
native delivery: the armed runtime dispatches ready work and wake recovery
reconciles unread pickup. Do not boot participants or create a second wake
path.

## Handoff

Once armed, hand control to `sprint_pln` and stop preparation work. Give the FnB
a compact declaration: Sprint id, feature, exact spec revisions,
participants/routes, work-unit graph, planned waves, capacity rationale and
reserve, merge-grant state, and known accepted risks. State whether pre-Sprint
QA/QC was performed and summarize any available evidence without treating it
as an eligibility result.

Stop when the Sprint is armed or when one concrete eligibility blocker has been
surfaced. Do not dispatch from a partially prepared plan.
