---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# spec

Execute a spec across sessions — analyze viability, surface blockers and unclear items, break into tasks (Preparation → impl steps → Verification), and track progress in spec_tasks. Updates current_state at every step. Load when starting any feature spec.

**Category:** craft

---

# spec — analyze and execute a spec

Load this skill at the start of any session where you're working a feature spec.
Run **Analyze** before touching any code. Pause for FnB on blockers or unclear
items you can't resolve alone.

`<self>` = your shell_id.

---

## Step 1: Load the spec

A feature can hold several unfrozen specs at once (see the `docs` skill), so don't
auto-pick "the latest" — list the feature's open specs and choose the target
explicitly. The **active** spec is the unfrozen one that already has a task plan;
the rest are backlog.

```sql
-- a feature's open (unfrozen) specs, newest seq first — pick the target by id:
SELECT r.feature_id, r.title AS feature_title, r.roadmap_status,
       d.document_id, d.seq, d.title AS spec_title,
       (SELECT COUNT(*) FROM spec_tasks t WHERE t.document_id = d.document_id) AS task_count
FROM roadmap r
JOIN documents d ON d.feature_id = r.feature_id AND d.kind = 'spec'
WHERE (r.title LIKE '%<keyword>%' OR r.feature_id = <id>)
  AND d.frozen = 0
ORDER BY d.seq DESC;

-- load the chosen spec body:
SELECT document_id, seq, title, body FROM documents WHERE document_id = <doc_id>;

-- check if a plan already exists for this spec:
SELECT task_id, seq, title, status, completed_date
FROM spec_tasks
WHERE document_id = <doc_id>
ORDER BY seq;
```

`task_count > 0` marks the active spec — resume that one; an empty spec is backlog,
and starting it (Step 3) makes it active. If more than one open spec matches and
which to work is unclear, ask the FnB.

If tasks already exist, skip to **Step 4** (Track).

Read the entire spec body before going further. Do not skim.

---

## Step 2: Analyze

Surface the following before any planning or code:

### Viability
- Can this be completed in the current session? Bounded + clear entry points:
  yes. Multiple layers, migrations, unknown dependencies: no — say so and
  propose a session-sized slice.
- Does the spec state a clear done-condition? If not, that is the first unclear
  item.

### Unclear items
Things you cannot act on without guessing:
- Ambiguous between two interpretations
- Missing a critical detail (which table? which endpoint? which component?)
- Implies knowledge not stated in the spec

List these and ask the FnB before writing the plan.

### Blockers
Hard stops — prior work not shipped, missing environment state, unresolved
external dependency. Open a flag for each:

```
./sc mem flag open "[Spec] <what is blocked> | Blocker for: <feature title>" --name SC-### --priority High --feature <feature_id>
```

Don't open flags for unclear items you can resolve by asking — ask first.

---

## Step 3: Plan

### Reconcile the stage first

Planning a spec means you're engaging it to build — so the feature's
`roadmap_status` (loaded in Step 1) must catch up to reality. The horizon stages
are `brainstorm · long_term · near_term · next · in_progress · shipped`.

- Feature sits at `brainstorm`/`long_term`/`near_term` and you're **building this
  session** → move it to `in_progress`:
  `./sc mem roadmap status <feature_id> in_progress`
- You're only **planning ahead** (no build this session) → move it to `next`.
- Already at `in_progress` (or further) → **no-op**; don't churn it.

This is a transition you make because you're *acting on* the spec — not something
that fires from merely reading one for reference. If there is no spec governing the
work (a quick UI fix, a minor migration), skip all stage handling: it doesn't
apply (see the Stance).

### Confirm the work-stream too

While you're reconciling the stage, check the same feature's **work-stream**
(`roadmap.project_id` — the Flow-view grouping). If it's Ungrouped, assign it now
so the feature shows up in a flow, not the Ungrouped pile:

```
./sc mem roadmap project <feature_id> <shortname>   # 'none' to clear
```

Assign when the stream is obvious; surface to the FnB when it's ambiguous. No-op
if already assigned. The full create/assess procedure (new streams, new features)
lives in the `docs` skill — this is just the engage-time confirmation so drift
doesn't accumulate.

Once analysis is clear and blockers are resolved or accepted, generate the task
list and INSERT it. Always this shape:

| seq | title | role |
|---|---|---|
| 0 | Preparation | Always first — read code paths, verify DB state, confirm entry points |
| 1..N | `<impl step title>` | As many as the scope needs; each independently verifiable |
| N+1 | Verification | Always last — run tests, smoke-test against done-condition, snapshot + render |

Add each task with `./sc mem task add` (one per seq). For a multi-task plan, pass
`--no-sync` on all but the last so you snapshot once at the end:

```
./sc mem task add "Preparation"  --feature <id> --doc <doc_id> --seq 0 --desc "Read code paths, verify DB state, confirm entry points" --no-sync
./sc mem task add "<Step 1>"     --feature <id> --doc <doc_id> --seq 1 --desc "<what it does>" --no-sync
./sc mem task add "<Step N>"     --feature <id> --doc <doc_id> --seq <N> --desc "<what it does>" --no-sync
./sc mem task add "Verification" --feature <id> --doc <doc_id> --seq <N+1> --desc "Run tests, smoke-test against done-condition, snapshot + render"
```

Then set `current_state` — no last-done yet, next is Preparation:

```
./sc mem state "[<feature_title>] — last: —. next: Preparation."
```

---

## Step 4: Track session by session

At the start of each work session, load the current plan state:

```sql
SELECT task_id, seq, title, description, status, completed_date
FROM spec_tasks
WHERE document_id = <doc_id>
ORDER BY seq;
```

Find the first `pending` task. Mark it `in_progress`:

```
./sc mem task start <task_id>
```

Work only that task. When done, mark it complete, then resolve last-done / next-up
with a read:

```
./sc mem task done <task_id>
```
```sql
-- resolve last-done and next-pending in one query (raw read):
SELECT
  (SELECT title FROM spec_tasks WHERE document_id=<doc_id> AND status='done'
   ORDER BY seq DESC LIMIT 1) AS last_done,
  (SELECT title FROM spec_tasks WHERE document_id=<doc_id> AND status='pending'
   ORDER BY seq ASC LIMIT 1) AS next_up;
```

Then advance `current_state`:

```
./sc mem state "[<feature_title>] — last: <last_done>. next: <next_up>."
```

If `next_up` is NULL, all tasks are done — set current_state to reflect that.

---

## Step 5: Hand off on completion

When the **Verification** task passes (`next_up` is NULL — the existing
done-line), the feature is delivered. As the dev, do the handoff — you flip the
horizon and hand the paperwork to the planner; you do **not** freeze the spec or
write the doc (that's the planner — see the `docs` skill):

1. **Flip the horizon to shipped:**
   ```
   ./sc mem roadmap status <feature_id> shipped
   ```
2. **Open a docs-pending flag** so `shipped` doesn't silently claim a doc that
   isn't written yet (`shipped` + an open flag is the honest interim state). Per
   the `flags` skill, opening it also messages the party who clears it — the
   planner:
   ```
   ./sc mem flag open "[Docs] <feature> shipped, doc pending | Blocker for: <feature> doc" --name SC-### --priority Medium --feature <feature_id>
   ./sc mem message send <planner-shortname> "<feature> shipped — spec <doc_id> ready to freeze + document. Docs-pending flag SC-### open."
   ```
3. **Surface to the FnB:** "shipped; the planner needs to freeze the spec + write
   the doc." The planner closes the flag when the doc lands.

If this fork has no planner-flavor shell, message nobody — surface to the FnB
directly and leave the docs-pending flag open for whoever picks up docs.

---

## Watch for creep while you build

If, mid-build, the work grows past the spec's stated what/why:

- **Small growth** (same mental model, a few more tasks) → the spec is *living*
  while unfrozen; just edit it (`./sc mem doc edit`) and carry on. No ceremony.
- **A separate coherent intent** (a new mental-model boundary — the granularity
  test in the `docs` skill) → don't quietly absorb it. Recommend a **new spec** to
  the FnB, to be authored by the planner against its own feature. Significant creep
  is a planning event, not a dev improvisation.

---

## Stance

- **Analyze before acting.** The analysis phase discovers the gap between what
  the spec says and what the code does.
- **One task at a time.** Don't start task N+1 until task N is verified and
  marked done.
- **Verification is not optional.** It is the last task; skipping it makes
  "done" meaningless.
- **If the spec is too large for one session:** scope a session slice at
  Preparation — cover steps 1–K that can be verified now, leave K+1–N pending.
  Don't start work that can't be verified before the session ends.
- **current_state always reflects the plan.** After every task completion,
  update it — last done + next up. This is how the next session resumes without
  reading the full task list first.
- **The stage tracks reality, but only for spec'd work.** Engaging a spec moves
  it forward (→ `in_progress`); finishing it hands off (→ `shipped`). No-op when
  the stage already matches — don't churn it. Work with **no spec** (quick UI
  tweaks, minor migrations) is exempt entirely: no promotion, no handoff, no creep
  check. Stage discipline must never become a blocker for small things.
