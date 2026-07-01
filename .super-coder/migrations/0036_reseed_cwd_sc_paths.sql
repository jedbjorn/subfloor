-- 0036 — reseed db_map/memory/messaging/spec: cwd-independent engine paths.
--
-- These four were last reseeded by 0028/0031/0032 (mem/db_map/messaging) and
-- 0014/0032 (spec), which pinned the pre-fix guidance that spelled the engine
-- as `./sc …` and read raw DBs as `sqlite3 .super-coder/shell_db.db` /
-- `sqlite3 .sc-state/map.db`. Those cwd-relative forms are the cwd trap: they
-- pull a shell into `cd`-ing to the main root, which then silently retargets
-- every later bare git/grep at the main tree. The assets now use `sc …` (bare,
-- on PATH from any cwd) and the `sc sql` / `sc map-sql` read passthroughs.
--
-- A full migration replay (0001 then the later reseeds) would otherwise end on
-- the stale `./sc` body. Re-UPSERT the current asset content as the last writer
-- — generated from assets/skills/<name> via seed-skills, byte-identical to 0001.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'db_map',
  'Data model behind the engine memory surfaces + the `sc mem` command for each. Check before reading or writing memory — identity, decisions, roadmap, documents, flags. Reads/writes go through the API (`sc mem`), never raw sqlite.',
  'substrate',
  NULL,
  1,
  '# db_map — super-coder''s DB at a glance

All identity, memory, and content live in the engine DB
(`.super-coder/shell_db.db`) — but you never touch that file. You read and write
it **only through the engine API**, via `sc mem`:

- **Read** — `sc mem get <surface>`: your own `state`, `seed`, `lns`,
  `decisions`, `flags`, `narrative`, `messages`; and the shared planning state
  `roadmap`, `projects`, `documents`, `tasks`, `shells` (add `--json` for raw).
  `documents`/`tasks` take `--feature <id>` or `--doc <id>` (and `--doc` on
  `documents` returns the one doc *with* its body).
- **Write** — `sc mem <cmd> …` (see `## Common writes` below).

There is **no `sqlite3` path** — not as a fallback, not for "ad-hoc" reads.
`sc mem` goes through the API and only the API; if the API isn''t wired it
fails loud rather than writing the DB behind its back. Your identity rides in
your bearer token — the server resolves token → shell, so you never name a
shell. The table below is the **data model** behind those surfaces (and what
each `sc mem` write touches), not a query cheatsheet. Lazy-load: `get` the one
surface you need, don''t bulk-read.

**Need a read or write `sc mem` doesn''t expose?** That''s a gap to *report*, not
a reason to reach for the DB — the direct path is closed by design, and a fork
can''t patch the engine anyway (`sc update` would overwrite it). A missing
surface is an engine gap that goes **up to the FnB**: open a flag naming the data
and the use, and surface it. Don''t improvise around the API.

```
sc mem flag open "[Engine] need to <read|write> <what> — no sc mem surface for it | Blocker for: <your work>"
```

The FnB carries it upstream (that''s exactly how `get documents`/`get tasks`
landed); message a planner-flavor shell too if the fork has one. Until then, do
what you *can* through the API and flag the rest — never the DB directly.

The repo map (`dr_*`) is **not here** — it lives in its own db, `.sc-state/map.db`
(see the `surface_catalogue` skill). This map covers only `shell_db.db`, your
memory/identity/content. Don''t look for `dr_*` in `shell_db.db`.

## Tables

| Table | Holds | Write rule |
|---|---|---|
| `shells` | identity core: `mandate`, `system_prompt`, `current_state` (rolling, ~500 chars), `lineage_seed`, `active_archive_id`. (`connections`/`workspace` retired — boot `## CONNECTIONS` is derived from the `dr_*` map, not authored here) | UPDATE in place |
| `shell_identity_entries` | seed (cap 10) + L&S (`kind=''lns''`, cap 20); triggers enforce caps | INSERT to add; UPDATE `retired_at` to curate out — never edit a seed body (Law 3) |
| `shell_decisions` | major decisions | INSERT only; supersede via `parent_decision_id` |
| `shell_memory_archives` | one row per session; `full_narrative` appended progressively | INSERT at session open; UPDATE narrative |
| `roadmap` | one row per planned feature; `roadmap_status` is a planning horizon (`brainstorm`→`in_progress`→`next`→`near_term`→`long_term`→`shipped`→`retired`), `sort_order` within a bucket. `shipped` = delivered; `retired` = taken off the board (decided-against / split / absorbed / replaced) without shipping — keep the row. `project_id` (nullable) = the work-stream the feature belongs to; the GUI Flow view groups on it (NULL = Ungrouped) | INSERT/UPDATE |
| `feature_blockers` | the roadmap''s dependency edges: one row = `feature_id` depends on `blocked_by` (prerequisite must land first). Directed, kept acyclic (the GUI Flow view wires them; the card''s "depends on" picker sets them) | INSERT/DELETE the edge; set the whole set via `sc mem roadmap depends` |
| `documents` | the content store — specs/docs bodies live here; `frozen=1` on ship (immutable); `render_path` = flat-file target | INSERT a new `seq` per stage; never edit a frozen body |
| `flags` | open + resolved tasks; `feature_id` links a flag to the feature it blocks | INSERT to open; UPDATE `resolved=1` + `resolved_date` to close |
| `skills` / `shell_skills` | skill catalogue (system, seeded from `assets/skills/` via migration) + per-shell grants | managed by engine |
| `projects` / `project_shells` | project standing + shell linkage; a `projects` row also doubles as a **work-stream** that roadmap features attach to via `roadmap.project_id` (the Flow-view grouping) | UPDATE `standing`; INSERT to add |

`<self>` = your `shell_id` (in the boot doc''s ACTIVE SESSION block).

## Common writes

Each routes through the engine API and writes to the live shared DB. `sc mem which`
orients; `sc mem <cmd> -h` shows flags. Writes always target your own shell —
the server resolves it from your token; you never name a shell.

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

# spec_tasks (the plan): add a task / advance it:
sc mem task add "…" --feature <id> --doc <doc_id> --seq <n> [--desc "…"]
sc mem task start <task_id>     # sc mem task done <task_id>

# open / close a flag:
sc mem flag open "[Area] … | Blocker for: …" --name CC-001 [--feature <id>]
sc mem flag close <flag_id> --notes "…"

# projects (standing + linkage):
sc mem project add <shortname> "<title>" --purpose "…" --standing "…"
sc mem project standing <shortname|id> "…"     # sc mem project status <…> paused

# inbox + first-run:
sc mem message send <shortname> "…"     # check / mark-read too (see `messaging`)
sc mem oriented                          # mark first-run done (bootstrapped=1)
```

## After writing

Nothing more to run — the write is live in the shared engine DB the moment it
commits, visible to every shell. Persisting it to git is an admin/GUI step, not
yours.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'memory',
  'How this shell writes its memory — current_state, session narrative, seed, L&S, decisions. Write as it happens, not at close. Use to know WHEN and HOW to persist identity/work memory, and the caps.',
  'substrate',
  NULL,
  1,
  '# memory — write as you go

All memory is DB rows (no flat files). Write at the moment it matters, not in a
close ritual.

**Write through `sc mem`.** The write lands in the live engine DB — shared by
every shell, durable + visible to all the moment it commits. It always targets
your own shell: the server resolves your identity from your token, so you never
name a shell.

## current_state — rolling status, NOT a log

Your present focus + what''s next. **Replaces in place; never a log.** Soft target
~500 chars. Rewrite when focus shifts.
```
sc mem state "…"
```

## Session narrative — append at inflection points

One row per session, appended progressively. Append a `[HH:MM]` line (the time is
stamped for you) when: a decision lands, an approach changes or is rejected, the
FnB says something that shapes the work, an assumption breaks, or before a big
change.
```
sc mem narrative "…"
```

## seed (cap 10) — who you are

Identity-forming moments. Past-tense/timeless. Add a new entry; **never edit a
body** (curate by retiring). The genesis + lineage seed are already yours.
```
sc mem seed "…"            # add
sc mem retire <entry_id>   # curate out (frees a cap slot)
```

## L&S (cap 20) — how you work

Operating lessons, imperative voice. Add when a lesson lands; curate by retiring.
Caps are trigger-enforced (seed 10, L&S 20) — `sc mem` reports the cap message;
retiring frees a slot.
```
sc mem lns "…"
```

## Decisions — Major only

Record a Major decision (architecture, approach, a path chosen over another).
Never rewritten; supersede via `--parent <decision_id>`. Mirror the headline into
the narrative.
```
sc mem decision "…" --rationale "…" [--parent <id>]
```

## Stance

Write-as-you-go beats batch-at-close: it costs nothing per write and zero at
session end. Curate seed/L&S (revise the set), never rewrite history (decisions,
narrative, seed bodies). Full command reference + table map: the `db_map` skill.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'messaging',
  'Shell-to-shell inbox — send a markdown message to another shell, check your unread inbox, mark messages read. Driven by `sc mem message`. Use to coordinate with another shell; the recipient sees it on its next boot via the STATUS Inbox count.',
  'substrate',
  NULL,
  1,
  '# messaging — the shell inbox

One shell writes a markdown message to another; the recipient discovers it on its
next boot via the `## STATUS` `Inbox:` count, surfaces it with `check`, and clears
it with `mark-read`. Body is markdown — preserved verbatim.

Drive it with **`sc mem message`**. The sender is you; recipients are addressed
by `shortname`.

Trigger: `--message`
Args: `check [N] | send <to-shortname> <body> | mark-read <id>`

## check — your unread inbox

```
sc mem message check [N]      # N optional; default 50, max 200
```

`check` is read-only — it does **not** auto-mark-read. Surface the body to the
operator (and reply if warranted, which is itself a `send`), then `mark-read` the
inbound in the same turn.

## send — message another shell

```
sc mem message send <to-shortname> "<body>"
```

- Multi-word body = a single quoted argument; markdown is preserved verbatim.
- Examples: `sc mem message send cartographer "map is stale — re-run sc map"`
  · `sc mem message send cc "spec ready for review — see flag SC-014"`
- Unknown / deleted recipient → `mem: recipient shortname ''<x>'' unknown`. Empty
  body → `mem: body is empty`. Surface either to the operator plainly.

## mark-read — clear an inbox item (idempotent)

```
sc mem message mark-read <message_id>
```

Access control: you can only mark read a message addressed to **you** — one for
another shell is a no-op. Re-marking a read message is also a no-op. Pass the
`message_id` that `check` surfaced.

## Stance

On boot, if the `## STATUS` `Inbox:` line is non-zero, run `--message check` and
surface the first item before continuing. A reply is a new `send` — there is no
threading; include `Re: <topic>` in the body if it matters. Keep the inbox honest:
mark-read only once you''ve actually acted on the message.',
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

Load this skill at the start of any session where you''re building or implementing
a feature — whether or not the work is framed out loud as a "spec." If a spec
governs the work, this is how you execute it; if one should but doesn''t yet, the
`docs` skill authors it first. Run **Analyze** before touching any code. Pause for FnB on blockers or unclear
items you can''t resolve alone.

`<self>` = your shell_id.

---

## Step 1: Load the spec

A feature can hold several unfrozen specs at once (see the `docs` skill), so don''t
auto-pick "the latest" — list the feature''s open specs and choose the target
explicitly. The **active** spec is the unfrozen one that already has a task plan;
the rest are backlog.

```
# the feature''s documents — pick an unfrozen spec (frozen=0) by id:
sc mem get documents --feature <id>
# load the chosen spec body:
sc mem get documents --doc <doc_id>
# the spec''s task plan (empty = no plan yet):
sc mem get tasks --doc <doc_id>
```

`get documents --feature <id>` lists every spec/doc under the feature with its
`kind`, `seq`, `frozen`, and `task_count`. `task_count > 0` marks the active spec — resume that one; an empty spec is backlog,
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
sc mem flag open "[Spec] <what is blocked> | Blocker for: <feature title>" --name SC-### --priority High --feature <feature_id>
```

Don''t open flags for unclear items you can resolve by asking — ask first.

---

## Step 3: Plan

### Reconcile the stage first

Planning a spec means you''re engaging it to build — so the feature''s
`roadmap_status` (loaded in Step 1) must catch up to reality. The horizon stages
are `brainstorm · long_term · near_term · next · in_progress · shipped`.

- Feature sits at `brainstorm`/`long_term`/`near_term` and you''re **building this
  session** → move it to `in_progress`:
  `sc mem roadmap status <feature_id> in_progress`
- You''re only **planning ahead** (no build this session) → move it to `next`.
- Already at `in_progress` (or further) → **no-op**; don''t churn it.

This is a transition you make because you''re *acting on* the spec — not something
that fires from merely reading one for reference. If there is no spec governing the
work (a quick UI fix, a minor migration), skip all stage handling: it doesn''t
apply (see the Stance).

### Confirm the work-stream too

While you''re reconciling the stage, check the same feature''s **work-stream**
(`roadmap.project_id` — the Flow-view grouping). If it''s Ungrouped, assign it now
so the feature shows up in a flow, not the Ungrouped pile:

```
sc mem roadmap project <feature_id> <shortname>   # ''none'' to clear
```

Assign when the stream is obvious; surface to the FnB when it''s ambiguous. No-op
if already assigned. The full create/assess procedure (new streams, new features)
lives in the `docs` skill — this is just the engage-time confirmation so drift
doesn''t accumulate.

Once analysis is clear and blockers are resolved or accepted, generate the task
list and INSERT it. Always this shape:

| seq | title | role |
|---|---|---|
| 0 | Preparation | Always first — read code paths, verify DB state, confirm entry points |
| 1..N | `<impl step title>` | As many as the scope needs; each independently verifiable |
| N+1 | Verification | Always last — run tests, smoke-test against done-condition, snapshot + render |

Add each task with `sc mem task add` (one per seq) — each write is live in the
shared DB immediately:

```
sc mem task add "Preparation"  --feature <id> --doc <doc_id> --seq 0 --desc "Read code paths, verify DB state, confirm entry points"
sc mem task add "<Step 1>"     --feature <id> --doc <doc_id> --seq 1 --desc "<what it does>"
sc mem task add "<Step N>"     --feature <id> --doc <doc_id> --seq <N> --desc "<what it does>"
sc mem task add "Verification" --feature <id> --doc <doc_id> --seq <N+1> --desc "Run tests, smoke-test against done-condition, snapshot + render"
```

Then set `current_state` — no last-done yet, next is Preparation:

```
sc mem state "[<feature_title>] — last: —. next: Preparation."
```

---

## Step 4: Track session by session

At the start of each work session, load the current plan state:

```
sc mem get tasks --doc <doc_id>
```

Find the first `pending` task. Mark it `in_progress`:

```
sc mem task start <task_id>
```

Work only that task. When done, mark it complete, then resolve last-done / next-up
with a read:

```
sc mem task done <task_id>
```

Re-read the plan and resolve last-done / next-up from it — `last_done` is the
highest-`seq` `done` task, `next_up` the lowest-`seq` `pending` one:

```
sc mem get tasks --doc <doc_id>
```

Then advance `current_state`:

```
sc mem state "[<feature_title>] — last: <last_done>. next: <next_up>."
```

If `next_up` is NULL, all tasks are done — set current_state to reflect that.

---

## Step 5: Hand off on completion

When the **Verification** task passes (`next_up` is NULL — the existing
done-line), the feature is delivered. As the dev, do the handoff — you flip the
horizon and hand the paperwork to the planner; you do **not** freeze the spec or
write the doc (that''s the planner — see the `docs` skill):

1. **Flip the horizon to shipped:**
   ```
   sc mem roadmap status <feature_id> shipped
   ```
2. **Open a docs-pending flag and message the planner with full instructions.**
   `shipped` + an open flag is the honest interim state. The message carries
   everything the planner needs to act without digging:
   ```
   sc mem flag open "[Docs] <feature> shipped, doc pending | Blocker for: <feature> doc" --name SC-### --priority Medium --feature <feature_id>
   sc mem message send <planner-shortname> "**[Docs pending] <feature_title> (feature <feature_id>)**

   Spec <doc_id> shipped. Flag SC-### is open — your action required:

   1. **Read the shipped code first.** Write the doc from what actually shipped, not from the spec. Drift happens and decisions get made in production — the spec captures the intent, the code is the truth.
   2. Freeze the spec: \`sc mem doc freeze <doc_id>\`
   3. Write the doc (\`kind=''doc''\`) under feature <feature_id> (see the \`docs\` skill).
   4. Close flag SC-### when the doc is live."
   ```
3. **Surface to the FnB:** "shipped; the planner needs to freeze the spec + write
   the doc." The planner closes the flag when the doc lands.

If this fork has no planner-flavor shell, message nobody — surface to the FnB
directly and leave the docs-pending flag open for whoever picks up docs.

---

## Watch for creep while you build

If, mid-build, the work grows past the spec''s stated what/why:

- **Small growth** (same mental model, a few more tasks) → the spec is *living*
  while unfrozen; just edit it (`sc mem doc edit`) and carry on. No ceremony.
- **A separate coherent intent** (a new mental-model boundary — the granularity
  test in the `docs` skill) → don''t quietly absorb it. Recommend a **new spec** to
  the FnB, to be authored by the planner against its own feature. Significant creep
  is a planning event, not a dev improvisation.

---

## Stance

- **Analyze before acting.** The analysis phase discovers the gap between what
  the spec says and what the code does.
- **One task at a time.** Don''t start task N+1 until task N is verified and
  marked done.
- **Verification is not optional.** It is the last task; skipping it makes
  "done" meaningless.
- **If the spec is too large for one session:** scope a session slice at
  Preparation — cover steps 1–K that can be verified now, leave K+1–N pending.
  Don''t start work that can''t be verified before the session ends.
- **current_state always reflects the plan.** After every task completion,
  update it — last done + next up. This is how the next session resumes without
  reading the full task list first.
- **The stage tracks reality, but only for spec''d work.** Engaging a spec moves
  it forward (→ `in_progress`); finishing it hands off (→ `shipped`). No-op when
  the stage already matches — don''t churn it. Work with **no spec** (quick UI
  tweaks, minor migrations) is exempt entirely: no promotion, no handoff, no creep
  check. Stage discipline must never become a blocker for small things.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
