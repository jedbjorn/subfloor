---
name: spec
description: Execute a spec across sessions — analyze viability, surface blockers and unclear items, break into tasks (Preparation → impl steps → Verification), and track progress in spec_tasks. Updates current_state at every step. Load when starting, implementing, or building any feature, spec, or roadmap item — before writing code.
category: craft
common: false
---

# spec — analyze and execute a spec

Load at the start of any session that builds or implements a feature, whether
or not the work is framed as a "spec". A spec governs the work -> this skill
executes it; one should exist but doesn't -> the `docs` skill authors it first.
Run **Analyze** before touching any code. Blockers / unclear items you can't
resolve alone -> pause for the FnB.

`<self>` = your shell_id.

---

## Step 1: Load the spec

A feature can hold several unfrozen specs at once (see the `docs` skill).
NEVER auto-pick "the latest" — list the feature's open specs and choose the
target explicitly:

```
# the feature's documents — pick an unfrozen spec (frozen=0) by id:
sc mem get documents --feature <id>
# load the chosen spec body:
sc mem get documents --doc <doc_id>
# the spec's task plan (empty = no plan yet):
sc mem get tasks --doc <doc_id>
```

`get documents --feature <id>` lists every spec/doc with `kind`, `seq`,
`frozen`, `task_count`. Active spec = the unfrozen one with `task_count > 0`
— resume it. `task_count = 0` = backlog; starting it (Step 3) makes it
active. More than one open spec and the target unclear -> ask the FnB.

Tasks already exist -> skip to **Step 4** (Track).

Read the entire spec body before going further. Do not skim.

---

## Step 2: Analyze

Surface all three before any planning or code:

### Viability
- Session-completable? Bounded + clear entry points = yes. Multiple layers /
  migrations / unknown dependencies = no -> say so + propose a session-sized
  slice.
- No stated done-condition in the spec -> that is the first unclear item.

### Unclear items
Anything you cannot act on without guessing:
- Ambiguous between two interpretations
- Missing a critical detail (which table? which endpoint? which component?)
- Implies knowledge not stated in the spec

List them and ask the FnB before writing the plan.

### Blockers
Hard stops — prior work not shipped, missing environment state, unresolved
external dependency. Open one flag per blocker:

```
sc mem flag open "[Spec] <what is blocked> | Blocker for: <feature title>" --name SC-### --priority High --feature <feature_id>
```

NEVER open a flag for an unclear item resolvable by asking — ask first.

---

## Step 3: Plan

### Reconcile the stage first

Planning a spec = engaging it to build, so the feature's `roadmap_status`
(loaded in Step 1) must catch up to reality. Stages:
`brainstorm · long_term · near_term · next · in_progress · shipped`.

- At `brainstorm`/`long_term`/`near_term` + building this session ->
  `sc mem roadmap status <feature_id> in_progress`
- Planning ahead only (no build this session) -> move it to `next`.
- Already at `in_progress` (or further) -> no-op; don't churn it.

The transition fires because you *act on* the spec — reading one for
reference moves nothing. No spec governing the work (quick UI fix, minor
migration) -> skip all stage handling (see Stance).

### Confirm the work-stream too

Check the feature's work-stream (`roadmap.project_id` — the Flow-view
grouping). Ungrouped -> assign now so the feature shows in a flow:

```
sc mem roadmap project <feature_id> <shortname>   # 'none' to clear
```

Stream obvious -> assign; ambiguous -> surface to the FnB; already assigned
-> no-op. Full create/assess procedure (new streams, new features) = the
`docs` skill; this is only the engage-time confirmation.

### Write the task plan

Analysis clear + blockers resolved or accepted -> generate the task list.
Always this shape:

| seq | title | role |
|---|---|---|
| 0 | Preparation | Always first — read code paths, verify DB state, confirm entry points |
| 1..N | `<impl step title>` | As many as the scope needs; each independently verifiable |
| N+1 | Verification | Always last — run tests, smoke-test against done-condition, snapshot + render |

Add one task per seq with `sc mem task add` — each write is live in the
shared DB immediately:

```
sc mem task add "Preparation"  --feature <id> --doc <doc_id> --seq 0 --desc "Read code paths, verify DB state, confirm entry points"
sc mem task add "<Step 1>"     --feature <id> --doc <doc_id> --seq 1 --desc "<what it does>"
sc mem task add "<Step N>"     --feature <id> --doc <doc_id> --seq <N> --desc "<what it does>"
sc mem task add "Verification" --feature <id> --doc <doc_id> --seq <N+1> --desc "Run tests, smoke-test against done-condition, snapshot + render"
```

Then set `current_state` — nothing done yet, next = Preparation:

```
sc mem state "[<feature_title>] — last: —. next: Preparation."
```

---

## Step 4: Track session by session

**Agents overlay:** this shell granted `agents` + FnB invoked `--agents` ->
that skill's overlay replaces this step's one-task-at-a-time loop with
adjudicated waves. Load it and apply it on top of this step.

At each work session's start, load the plan:

```
sc mem get tasks --doc <doc_id>
```

Find the first `pending` task -> mark it in progress:

```
sc mem task start <task_id>
```

Work ONLY that task. When done:

```
sc mem task done <task_id>
```

A planned task overtaken by a feature split or re-scope (its work moved to
another feature/spec, never built here) is cancelled, not done:

```
sc mem task cancel <task_id> --notes "moved to F<id> as task #<n>"
```

NEVER mark unbuilt work `done` and NEVER leave it `pending` under a shipped
feature — the task ledger is how a planner answers "is this feature actually
finished."

Re-read the plan (`sc mem get tasks --doc <doc_id>`) and resolve from it:
`last_done` = highest-`seq` `done` task; `next_up` = lowest-`seq` `pending`.
Advance `current_state`:

```
sc mem state "[<feature_title>] — last: <last_done>. next: <next_up>."
```

`next_up` NULL = all tasks done -> set current_state to reflect that.

---

## Step 5: Hand off on completion

Verification task passes (`next_up` NULL — the existing done-line) = feature
delivered. As the dev: flip the horizon + hand the paperwork to the planner.
Do NOT freeze the spec or write the doc — that's the planner (`docs` skill).

1. **Flip the horizon to shipped:**
   ```
   sc mem roadmap status <feature_id> shipped
   ```
2. **Open a docs-pending flag + message the planner with full instructions.**
   `shipped` + an open flag = the honest interim state; the message carries
   everything the planner needs without digging:
   ```
   sc mem flag open "[Docs] <feature> shipped, doc pending | Blocker for: <feature> doc" --name SC-### --priority Medium --feature <feature_id>
   sc mem message send <planner-shortname> "**[Docs pending] <feature_title> (feature <feature_id>)**

   Spec <doc_id> shipped. Flag SC-### is open — your action required:

   1. **Read the shipped code first.** Write the doc from what actually shipped, not from the spec. Drift happens and decisions get made in production — the spec captures the intent, the code is the truth.
   2. Freeze the spec: \`sc mem doc freeze <doc_id>\`
   3. Write the doc (\`kind='doc'\`) under feature <feature_id> (see the \`docs\` skill).
   4. Close flag SC-### when the doc is live."
   ```
3. **Surface to the FnB:** "shipped; the planner needs to freeze the spec +
   write the doc." The planner closes the flag when the doc lands.

No planner-flavor shell in this fork -> message nobody; surface to the FnB
directly and leave the docs-pending flag open for whoever picks up docs.

---

## Watch for creep while you build

Mid-build, the work grows past the spec's stated what/why:

- **Small growth** (same mental model, a few more tasks) -> the unfrozen spec
  is living; edit it (`sc mem doc edit`) and carry on. No ceremony.
- **A separate coherent intent** (a new mental-model boundary — the
  granularity test in the `docs` skill) -> do NOT quietly absorb it.
  Recommend a **new spec** to the FnB, authored by the planner against its
  own feature. Significant creep = planning event, not dev improvisation.

---

## Stance

- **Analyze before acting.** Analysis finds the gap between what the spec
  says and what the code does.
- **One task at a time.** Start task N+1 only after task N is verified +
  marked done.
- **Verification is not optional.** It is the last task; skipping it makes
  "done" meaningless.
- **Spec too large for one session** -> scope a slice at Preparation: cover
  steps 1–K verifiable now, leave K+1–N pending. NEVER start work that can't
  be verified before the session ends.
- **current_state always reflects the plan.** Update after every task
  completion — last done + next up. The next session resumes from it without
  reading the full task list first.
- **The stage tracks reality — spec'd work only.** Engaging a spec ->
  `in_progress`; finishing -> `shipped`; already matching -> no-op, don't
  churn. Work with no spec (quick UI tweaks, minor migrations) is exempt
  entirely: no promotion, no handoff, no creep check. Stage discipline never
  blocks small things.
