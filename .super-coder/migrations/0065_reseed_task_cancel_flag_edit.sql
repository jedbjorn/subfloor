-- 0065 — reseed: db_map + flags + spec — task cancel, flag edit (#342/#316).
--
-- The write surface grew two verbs and the skills that teach it follow:
--
--   db_map — Common writes now shows `task cancel --notes` (the honest
--            terminal state when a feature split moves a task's work) and
--            `flag edit` (description/priority/feature on an open flag)
--   flags  — new Edit section: progressive tracker-flag updates go through
--            `sc mem flag edit`, not a raw PATCH probe
--   spec   — task loop: a task overtaken by a split/re-scope is cancelled
--            with notes — never marked done unbuilt, never left pending
--            under a shipped feature
--
-- Source assets updated in the same commit; this trailing forward reseed
-- (UPSERT by name; skill_id + grants preserved) carries them to installed
-- forks and fresh builds alike.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'db_map',
  'Data model behind the engine memory surfaces + the `sc mem` command for each. Check before reading or writing memory — identity, decisions, roadmap, documents, flags. Reads/writes go through the API (`sc mem`), never raw sqlite.',
  'substrate',
  NULL,
  1,
  '# db_map — super-coder''s DB at a glance

All identity, memory, and content live in the engine DB
(`.super-coder/shell_db.db`). NEVER touch that file — read and write it only
through the engine API, via `sc mem`:

- **Read** = `sc mem get <surface>`: your own `state`, `seed`, `lns`,
  `decisions`, `flags`, `narrative`, `messages`; shared planning state
  `roadmap`, `projects`, `documents`, `tasks`, `shells` (`--json` for raw).
  `documents`/`tasks` take `--feature <id>` / `--doc <id>`; `--doc` on
  `documents` returns the one doc *with* its body.
- **Write** = `sc mem <cmd> …` (see `## Common writes`).

There is NO `sqlite3` path — not as a fallback, not for "ad-hoc" reads. If the
API isn''t wired, `sc mem` fails loud instead of writing the DB behind its
back. Your identity rides in your bearer token — the server resolves token ->
shell; never name a shell in a write. The table below = the data model behind
those surfaces (what each `sc mem` write touches), not a query cheatsheet.
Lazy-load: `get` the one surface you need, don''t bulk-read.

**Need a read/write `sc mem` doesn''t expose?** Report the gap, don''t reach for
the DB — the direct path is closed by design, and a fork can''t patch the
engine (`sc update` would overwrite it). Open a flag naming the data + the
use, surface it to the FnB (who carries it upstream); message a
planner-flavor shell too if the fork has one. Until it lands: do what you can
through the API, flag the rest — NEVER query the DB directly.

```
sc mem flag open "[Engine] need to <read|write> <what> — no sc mem surface for it | Blocker for: <your work>"
```

The repo map (`dr_*`) lives in its own db, `.sc-state/map.db` (see the
`surface_catalogue` skill). The `dr_*` tables also exist in `shell_db.db` but
are ALWAYS empty there — a `dr_*` query against `shell_db.db` silently returns
0 rows instead of erroring. Never query `dr_*` here; this map covers only
`shell_db.db` (memory/identity/content).

## Tables

| Table | Holds | Write rule |
|---|---|---|
| `shells` | identity core: `mandate`, `system_prompt`, `current_state` (rolling, ~500 chars), `lineage_seed`, `active_archive_id`. (`connections`/`workspace` retired — boot `## CONNECTIONS` is derived from the `dr_*` map, not authored here) | UPDATE in place |
| `shell_identity_entries` | seed (cap 10) + L&S (`kind=''lns''`, cap 20); triggers enforce caps | INSERT to add; UPDATE `retired_at` to curate out — NEVER edit a seed body (Law 3) |
| `shell_decisions` | major decisions | INSERT only; supersede via `parent_decision_id` |
| `shell_memory_archives` | one row per session; `full_narrative` appended progressively | INSERT at session open; UPDATE narrative |
| `roadmap` | one row per planned feature; `roadmap_status` = planning horizon (`brainstorm`→`in_progress`→`next`→`near_term`→`long_term`→`shipped`→`retired`), `sort_order` within a bucket. `shipped` = delivered; `retired` = off the board without shipping (decided-against / split / absorbed / replaced) — keep the row. `project_id` (nullable) = the work-stream the feature belongs to; the GUI Flow view groups on it (NULL = Ungrouped) | INSERT/UPDATE |
| `feature_blockers` | roadmap dependency edges: one row = `feature_id` depends on `blocked_by` (prerequisite lands first). Directed, kept acyclic (GUI Flow view wires them; the card''s "depends on" picker sets them) | INSERT/DELETE the edge; set the whole set via `sc mem roadmap depends` |
| `documents` | content store — spec/doc bodies; `frozen=1` on ship (immutable); `render_path` = flat-file target | INSERT a new `seq` per stage; NEVER edit a frozen body |
| `flags` | open + resolved tasks; `feature_id` links a flag to the feature it blocks | INSERT to open; UPDATE `resolved=1` + `resolved_date` to close |
| `skills` / `shell_skills` | skill catalogue (system, seeded from `assets/skills/` via migration) + per-shell grants | managed by engine; grants via `./sc skill grant/revoke` |
| `projects` / `project_shells` | project standing + shell linkage; a `projects` row also doubles as a work-stream that roadmap features attach to via `roadmap.project_id` (the Flow-view grouping) | UPDATE `standing`; INSERT to add |

`<self>` = your `shell_id` (in the boot doc''s ACTIVE SESSION block).

## Common writes

Each routes through the engine API to the live shared DB. `sc mem which`
orients; `sc mem <cmd> -h` shows flags. Writes always target your own shell —
the server resolves it from your token.

```
# current_state (rolling status, not a log — replaces in place):
sc mem state "…"

# plant a seed / L&S entry (date stamped for you):
sc mem seed "…"            # sc mem lns "…" for a lesson
sc mem retire <entry_id>   # curate one out (frees a cap slot)

# record a Major decision (supersede with --parent <id>):
sc mem decision "…" --rationale "…"

# roadmap: add a feature / move its horizon:
sc mem roadmap add "…" --status brainstorm --summary "…" [--project <shortname|id>]
sc mem roadmap status <feature_id> shipped

# roadmap grouping + sequencing (drive the GUI Flow view):
sc mem roadmap project <feature_id> <shortname|id>   # assign a work-stream (or ''none'' to clear)
sc mem roadmap depends <feature_id> --on <id> [--on <id>]   # set dependencies (replaces; omit --on to clear; refuses cycles)

# author a spec/doc body (--body-file reads the markdown), then freeze on ship:
sc mem doc add "…" --kind spec --feature <id> --body-file ./draft.md --render-path specs_sc/….md
sc mem doc freeze <document_id>

# spec_tasks (the plan): add a task / advance it / close it honestly:
sc mem task add "…" --feature <id> --doc <doc_id> --seq <n> [--desc "…"]
sc mem task start <task_id>     # sc mem task done <task_id>
sc mem task cancel <task_id> --notes "moved to F<id> as task #<n>"   # split/re-scope — never mark unbuilt work done

# open / edit / close a flag:
sc mem flag open "[Area] … | Blocker for: …" --name CC-001 [--feature <id>]
sc mem flag edit <flag_id> [--description "…"] [--priority High] [--feature <id>]
sc mem flag close <flag_id> --notes "…"

# projects (standing + linkage):
sc mem project add <shortname> "<title>" --purpose "…" --standing "…"
sc mem project standing <shortname|id> "…"     # sc mem project status <…> paused

# inbox + first-run:
sc mem message send <shortname> "…"     # check / mark-read too (see `messaging`)
sc mem oriented                          # mark first-run done (bootstrapped=1)
```

## After writing

Nothing more to run — the write is live in the shared engine DB on commit,
visible to every shell. Persisting to git is an admin/GUI step, not yours.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'flags',
  'Track blockers as flags — surface open ones, open new ones, edit long-lived ones, resolve them. Link a flag to the roadmap feature it blocks. Mirrors the GUI Flags tab. Use when something blocks progress or needs follow-up.',
  'substrate',
  NULL,
  0,
  '# flags — blockers & follow-ups

flag = open question / blocker. `--feature <id>` set -> the flag is that
feature''s blocker (joined on the roadmap; shown on the Roadmap card + Flags
tab). `<self>` = your shell_id. All reads/writes go through `sc mem` (the
engine API) — there is no `sqlite3` path.

## Surface

```
sc mem get flags          # your open flags (id, name, priority, description)
sc mem get flags --json   # same, as JSON
```

Each flag carries its `feature_id`; cross-reference `sc mem get roadmap` for
the blocked feature''s title.

## Open

```
sc mem flag open "[Area] what''s blocked | Blocker for: X" --name SC-001 --priority Medium [--feature <id>]
```

- `--name` = short id, format `SC-###`.
- description format = `[Area] {what} | Blocker for: {what it blocks}`.
- `--priority` = High / Medium / Low. `--feature` = the feature it blocks (omit if none).

### Pair every open with a message

Every `flag open` -> a `message send` to whoever clears it (see the
`messaging` skill), so the work lands in their inbox on their next boot:

```
sc mem message send <shortname> "Opened SC-### — <one line> (Blocker for: <x>)."
```

Recipient = whoever the flag blocks:

| Flag is about | Message |
|---|---|
| docs pending after ship | the **planner** |
| a review failure on a diff | the **author dev** |
| a blocker on another shell''s work | **that shell** |
| an FnB decision / no shell owns it | **surface to the FnB** (no `send`) |

Message pairs with the *open* only: NEVER re-message a flag that is already
open; NEVER message on `close`.

## Edit

```
sc mem flag edit <flag_id> [--description "…"] [--priority High] [--feature <id>]
```

For long-lived tracker flags (one flag per arc, description updated
progressively as gates clear). `--description` replaces the whole text —
carry forward what still applies.

## Resolve

```
sc mem flag close <flag_id> --notes "…"
```

`--notes` states *how* it was resolved — that''s the trail.

## Stance

Open a flag the moment something blocks or needs follow-up — don''t hold it in
your head. Open flags on a feature = its blockers; clear them all before
calling the feature done. An opened flag with no message sent = a dropped
handoff.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'spec',
  'Execute a spec across sessions — analyze viability, surface blockers and unclear items, break into tasks (Preparation → impl steps → Verification), and track progress in spec_tasks. Updates current_state at every step. Load when starting, implementing, or building any feature, spec, or roadmap item — before writing code.',
  'craft',
  NULL,
  0,
  '# spec — analyze and execute a spec

Load at the start of any session that builds or implements a feature, whether
or not the work is framed as a "spec". A spec governs the work -> this skill
executes it; one should exist but doesn''t -> the `docs` skill authors it first.
Run **Analyze** before touching any code. Blockers / unclear items you can''t
resolve alone -> pause for the FnB.

`<self>` = your shell_id.

---

## Step 1: Load the spec

A feature can hold several unfrozen specs at once (see the `docs` skill).
NEVER auto-pick "the latest" — list the feature''s open specs and choose the
target explicitly:

```
# the feature''s documents — pick an unfrozen spec (frozen=0) by id:
sc mem get documents --feature <id>
# load the chosen spec body:
sc mem get documents --doc <doc_id>
# the spec''s task plan (empty = no plan yet):
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

Planning a spec = engaging it to build, so the feature''s `roadmap_status`
(loaded in Step 1) must catch up to reality. Stages:
`brainstorm · long_term · near_term · next · in_progress · shipped`.

- At `brainstorm`/`long_term`/`near_term` + building this session ->
  `sc mem roadmap status <feature_id> in_progress`
- Planning ahead only (no build this session) -> move it to `next`.
- Already at `in_progress` (or further) -> no-op; don''t churn it.

The transition fires because you *act on* the spec — reading one for
reference moves nothing. No spec governing the work (quick UI fix, minor
migration) -> skip all stage handling (see Stance).

### Confirm the work-stream too

Check the feature''s work-stream (`roadmap.project_id` — the Flow-view
grouping). Ungrouped -> assign now so the feature shows in a flow:

```
sc mem roadmap project <feature_id> <shortname>   # ''none'' to clear
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
that skill''s overlay replaces this step''s one-task-at-a-time loop with
adjudicated waves. Load it and apply it on top of this step.

At each work session''s start, load the plan:

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
Do NOT freeze the spec or write the doc — that''s the planner (`docs` skill).

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
   3. Write the doc (\`kind=''doc''\`) under feature <feature_id> (see the \`docs\` skill).
   4. Close flag SC-### when the doc is live."
   ```
3. **Surface to the FnB:** "shipped; the planner needs to freeze the spec +
   write the doc." The planner closes the flag when the doc lands.

No planner-flavor shell in this fork -> message nobody; surface to the FnB
directly and leave the docs-pending flag open for whoever picks up docs.

---

## Watch for creep while you build

Mid-build, the work grows past the spec''s stated what/why:

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
  steps 1–K verifiable now, leave K+1–N pending. NEVER start work that can''t
  be verified before the session ends.
- **current_state always reflects the plan.** Update after every task
  completion — last done + next up. The next session resumes from it without
  reading the full task list first.
- **The stage tracks reality — spec''d work only.** Engaging a spec ->
  `in_progress`; finishing -> `shipped`; already matching -> no-op, don''t
  churn. Work with no spec (quick UI tweaks, minor migrations) is exempt
  entirely: no promotion, no handoff, no creep check. Stage discipline never
  blocks small things.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
