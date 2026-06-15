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

```sql
-- find a feature and its active (non-frozen) spec:
SELECT r.feature_id, r.title AS feature_title, r.roadmap_status,
       d.document_id, d.seq, d.title AS spec_title, d.body, d.frozen
FROM roadmap r
JOIN documents d ON d.feature_id = r.feature_id AND d.kind = 'spec'
WHERE (r.title LIKE '%<keyword>%' OR r.feature_id = <id>)
  AND d.frozen = 0
ORDER BY d.seq DESC LIMIT 1;

-- check if a plan already exists for this spec:
SELECT task_id, seq, title, status, completed_date
FROM spec_tasks
WHERE document_id = <doc_id>
ORDER BY seq;
```

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

Once analysis is clear and blockers are resolved or accepted, generate the task
list and INSERT it. Always this shape:

| seq | title | role |
|---|---|---|
| 0 | Preparation | Always first — read code paths, verify DB state, confirm entry points |
| 1..N | `<impl step title>` | As many as the scope needs; each independently verifiable |
| N+1 | Verification | Always last — run tests, smoke-test against done-condition, snapshot + render |

`spec_tasks` has no `./sc mem` verb yet, so write it with raw `sqlite3` against
the engine DB, then `./sc snapshot` (`./sc mem which` confirms you're on the
engine DB first):

```sql
INSERT INTO spec_tasks (feature_id, document_id, seq, title, description, shell_id)
VALUES
  (<feature_id>, <doc_id>, 0,   'Preparation',  'Read all relevant code paths, verify DB state, confirm entry points', <self>),
  (<feature_id>, <doc_id>, 1,   '<Step 1>',     '<what it does>',                                                     <self>),
  (<feature_id>, <doc_id>, 2,   '<Step 2>',     '<what it does>',                                                     <self>),
  (<feature_id>, <doc_id>, <N>, 'Verification', 'Run tests, smoke-test against done-condition, snapshot + render',    <self>);
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

```sql
UPDATE spec_tasks SET status='in_progress' WHERE task_id=<id>;
```

Work only that task. When done, mark it complete (raw write — `spec_tasks` has no
`mem` verb — then snapshot), and resolve last-done / next-up:

```sql
UPDATE spec_tasks
SET status='done', completed_date=date('now')
WHERE task_id=<id>;

-- resolve last-done and next-pending in one query:
SELECT
  (SELECT title FROM spec_tasks WHERE document_id=<doc_id> AND status='done'
   ORDER BY seq DESC LIMIT 1) AS last_done,
  (SELECT title FROM spec_tasks WHERE document_id=<doc_id> AND status='pending'
   ORDER BY seq ASC LIMIT 1) AS next_up;
```

Then advance `current_state` (this also snapshots, persisting the task update):

```
./sc mem state "[<feature_title>] — last: <last_done>. next: <next_up>."
```

If `next_up` is NULL, all tasks are done — set current_state to reflect that.

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
