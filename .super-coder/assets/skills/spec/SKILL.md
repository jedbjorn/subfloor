---
name: spec
description: Load before implementing any feature, spec, or roadmap item. Analyze viability, surface blockers, plan Preparation → implementation → Verification, and track spec_tasks/current_state across sessions.
category: craft
common: false
---

# spec — analyze and execute a spec

Load before any feature/spec/roadmap build. Missing spec -> author via `docs`.
Analyze before code; ask FnB on ambiguity, flag hard blockers. `<self>` = your
shell id.

## 1. Select the spec

Never auto-pick the latest document. Read the complete selected body + task
ledger:

```text
sc mem get documents --feature <id>
sc mem get documents --doc <doc_id>
sc mem get tasks --doc <doc_id>
```

The list includes `kind`, `seq`, `frozen`, and `task_count`. Resume the unfrozen
spec with tasks; zero tasks = backlog and needs the plan below. Multiple
plausible specs -> ask FnB. Existing tasks -> track the first unfinished one.

## 2. Analyze before planning

Return these findings before any code or task writes:

| Check | Pass condition / action |
|---|---|
| Viability | Bounded, clear entry points, session-sized verification. Otherwise propose a verifiable slice. Missing done-condition = unclear. |
| Current Posture | Preparation code/DB state matches the documented baseline. A mismatch stops for Planner/FnB; never redefine it silently. |
| Scope | Every In Scope promise is planned; every Out of Scope item stays out. New/substantively revised specs contain both `## Current Posture` and `## Scope`; ambiguity stops. |
| Anticipated User Activity | Plan stated roles, audience/reach, access, validation, recovery, authority, safety, process curation, and tenancy invariants. Older absence is allowed; ambiguity is not. |
| Unclear item | Two plausible readings, missing target/interface, or unstated required knowledge -> ask FnB; do not flag a question they can answer. |
| Blocker | Missing prerequisite/environment/external dependency -> open one High feature flag and stop at its boundary. |

```text
sc mem flag open "[Spec] <blocked fact> | Blocker for: <feature>" \
  --name SC-### --priority High --feature <feature_id>
```

## 3. Engage and plan

Building now moves `brainstorm|long_term|near_term` to `in_progress`; planning
ahead -> `next`; matching/later stages and reference reads do not move.

```text
sc mem roadmap status <feature_id> in_progress
```

Confirm `roadmap.project_id`. Assign the obvious stream; ask FnB if ambiguous;
leave an existing assignment unchanged:

```text
sc mem roadmap project <feature_id> <shortname>
```

Create exactly: Preparation, independently verifiable implementation steps,
then Verification. Each write is immediately durable:

```text
sc mem task add "Preparation" --feature <id> --doc <doc_id> --seq 0 \
  --desc "Read code paths, verify DB state, confirm entry points"
sc mem task add "<step>" --feature <id> --doc <doc_id> --seq <n> \
  --desc "<independently verifiable outcome>"
sc mem task add "Verification" --feature <id> --doc <doc_id> --seq <last> \
  --desc "Test every scope promise, done-condition, audience assurance, snapshot, and render"
sc mem state "[<feature>] — last: —. next: Preparation."
```

No task plan = no implementation.

## 4. Execute one task at a time

```text
sc mem get tasks --doc <doc_id>
sc mem task start <task_id>
# work and verify only this task
sc mem task done <task_id>
```

After completion, re-read the ledger. Set `current_state` to the highest done
task + lowest pending task. Start the next only after the current task is
verified and durably done. Work moved to another feature/spec is cancelled,
never marked done or left pending under a shipped feature:

```text
sc mem task cancel <task_id> --notes "moved to F<id> as task #<n>"
sc mem state "[<feature>] — last: <last_done>. next: <next_up>."
```

Cancellation applies when work is copied or replanned into a different spec.
When the existing unfrozen spec itself starts a fresh feature era, use
`sc mem doc move <document_id> --feature <target_feature_id>` instead: the
document, its task ledger, and its document-linked decisions move atomically,
so their identities and statuses remain intact. Follow the `docs` split
workflow to verify the target and annotate the historical feature.

Final Verification follows the boot `TESTING POSTURE`. Complete code; run every
available focused proof; use observed registered-PR checks only for an
unavailable local gate: pending -> wait, red -> fix, green -> review. No checks
or untrustworthy watcher after one bounded read -> block; an optional browser
skip is non-failing. Require every In Scope done-condition + Anticipated User
Activity contract; unexpected reach, weakened hardening, or crossed tenancy
fails. A large spec may stop after a verified task slice; leave later tasks
pending and state the next one.

## 5. Ship and hand docs to Planner

All tasks done + Verification green -> deliver:

1. `sc mem roadmap status <feature_id> shipped`
2. Open one Medium docs-pending feature flag.
3. Message the planner: read shipped code, freeze the spec, write a `kind=doc`
   document under the feature, then close the flag. Confirm the durable message.
4. Tell FnB that code shipped and Planner owns freeze + docs. No planner shell ->
   message nobody; tell FnB and leave the flag open.

```text
sc mem flag open "[Docs] <feature> shipped, doc pending | Blocker for: <feature> doc" \
  --name SC-### --priority Medium --feature <feature_id>
```

Do not freeze or author the shipped doc as Developer.

## Scope change and stop rules

Small growth within the same intent -> revise the unfrozen spec with `docs`, add
tasks, continue. A separate mental model -> stop and recommend a new feature +
spec; never absorb it silently.

Stop for unresolved ambiguity/blocker, at the end of a verified session slice,
or after the shipped/docs handoff. `current_state` must always point to the
durable plan rather than reproduce its rationale.
