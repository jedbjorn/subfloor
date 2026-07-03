-- 0042 — reseed local-skill-persistence batch (upstream #253 / #237 pt2)
--
-- `sc seed-skills` now UPSERTs asset skills into the LIVE DB (the documented
-- contract), and snapshot/GUI classify engine-vs-local by the SEED's names
-- (0001), not asset-file presence — a fork-authored skill keeps its asset as
-- authoring source and still serializes to .sc-state/content.sql. Skill
-- grants have a first-class surface: `./sc skill list/grant/revoke/rm`.
-- Three skills updated to describe the now-true flow:
--   local_skill_management — seed live-upserts; grants via `./sc skill`;
--                            asset file is the durable authoring source (#253).
--   snapshot               — stale-mirror trap retired (seed-skills upserts
--                            the live DB; no rebuild step in the sequence).
--   db_map                 — skills/shell_skills row points at `./sc skill`.
--
-- 0001 is regenerated from the assets for fresh builds; this forward reseed
-- carries the same bodies to already-installed forks (UPSERT by name; skill_id
-- + grants preserved).

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

The repo map (`dr_*`) lives in its own db, `.sc-state/map.db` (see the
`surface_catalogue` skill). The `dr_*` tables also *exist* in `shell_db.db` but
are **always empty** there — a `dr_*` query against `shell_db.db` silently
returns 0 rows instead of erroring, so it looks like an empty map. Never query
`dr_*` here; this map covers only `shell_db.db`, your memory/identity/content.

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
| `skills` / `shell_skills` | skill catalogue (system, seeded from `assets/skills/` via migration) + per-shell grants | managed by engine; grants via `./sc skill grant/revoke` |
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
  'local_skill_management',
  'Create, persist, assign, and remove fork-specific skills — the correct authoring path so skills survive snapshot/rebuild cycles.',
  'substrate',
  NULL,
  0,
  '# local_skill_management — fork-specific skills that survive

Fork-specific skills live in the DB and are persisted via `.sc-state/content.sql`
(the snapshot). The asset file under `.super-coder/assets/skills/<name>/` is the
**authoring source** — edit it, re-seed, done. It sits in gitignored engine
territory, but that is safe: the engine/local boundary is the seed migration
(0001, upstream-owned in a fork), not asset-file presence, so the snapshot
serializes your skill to content.sql whether or not the asset file is kept, and
`sc update` neither manifests it nor heals over its DB row. **content.sql is
the durable form; the asset file is your editor.**

The path: **file → seed → grant → snapshot → commit**.

## Creating a fork-specific skill

1. **Write the skill file.**
   Path: `.super-coder/assets/skills/<name>/SKILL.md`

   Required frontmatter:
   ```yaml
   ---
   name: skill_name
   description: One-line summary — shown in boot, catalogue, and the GUI Skills tab
   category: substrate   # or craft; omit for default
   ---
   ```
   Body: Markdown. Write the procedure the shell will follow. Imperative,
   precise — this is what boots into context, so compress ruthlessly.

2. **Seed the skill into the live DB.**
   ```bash
   sc seed-skills
   ```
   UPSERTs every asset skill into the live DB by name (id-stable) and reports
   what landed. In a fork it deliberately does NOT regenerate the seed
   migration — that file is upstream-owned engine territory. Skills already in
   the DB with no asset file are other local skills, left intact.

3. **Grant the skill to the target shell(s)** — by shell id or shortname:
   ```bash
   sc skill grant <skill_name> <shell>...
   ```
   Unknown skill or shell names are hard errors (no silent no-op grants).
   `sc skill list` shows the catalogue with origins and current grants;
   `sc skill revoke <name> <shell>...` reverses a grant.

4. **Snapshot — this is the persistence step.**
   ```bash
   sc snapshot && sc render
   ```
   `snapshot.py` serializes local skills (any skill the engine seed doesn''t
   own) into `.sc-state/content.sql`. This is what survives `sc update` and
   `sc rebuild` — the skill row and its grants are reconstructed from
   content.sql. Without this step the skill is lost on next update.

5. **Commit.**
   Run `sc render-check` first — it rebuilds hermetically and fails if the
   `skills_sc/` mirror drifts from the DB render (the same CI guard; see the
   `snapshot` skill). Then stage `.sc-state/content.sql` and `skills_sc/`
   together — the snapshot without the re-rendered mirror is the drift.

## Updating a skill

Edit the asset file, then repeat seed → snapshot → commit (steps 2, 4, 5). If
the asset file is gone (removed, or authored elsewhere), recreate it from the
DB body first: `sc sql "SELECT content FROM skills WHERE name=''<name>''"`.

## Assigning an existing skill to additional shells

```bash
sc skill grant <skill_name> <shell>...
```
Then `sc snapshot && sc render` and commit.

## Removing a skill

1. **Soft-delete the row and revoke its grants:**
   ```bash
   sc skill rm <skill_name>
   ```
   Refuses engine skills — the seed would resurrect those on the next
   update/rebuild; `sc skill revoke` them per-shell instead.

2. **Remove the asset file** (`.super-coder/assets/skills/<name>/`) — otherwise
   the next `sc seed-skills` re-inserts the skill.

3. **Snapshot, render, commit.**
   ```bash
   sc snapshot && sc render
   ```

## How the GUI organizes skills

The review GUI has a **Skills tab**: the full catalogue in sections, with
per-shell grant toggles on every skill. The Shells tab groups its grant list
by the same sections.

- **Repo skills** — the lead section: skills authored in this fork. Membership
  is *derived*, not declared — a skill the engine seed doesn''t own is
  repo-local. This is the same rule snapshot.py uses to decide what serializes
  into `.sc-state/content.sql`, so the section shows exactly what the snapshot
  keeps durable. No frontmatter flag exists or is needed.
- **Substrate / Craft / …** — engine skills, sectioned by their `category`
  frontmatter. A repo skill''s `category` still displays as a label on its row,
  but never moves it out of the Repo section.

Grant toggles in the GUI hit the same DB table as `sc skill grant` — they
still need a **snapshot** (header button or `sc snapshot`) to survive a
rebuild.

## What NOT to do

- **Never skip the snapshot after creating a skill.** Seeding puts the row in
  the live DB only; content.sql is what survives `sc update` and `sc rebuild`.
- **Never edit `0001_seed_skills.sql` by hand.** It is generated, and in a
  fork it is upstream-owned engine territory — a local edit blocks the next
  update.
- **Never use the GUI to create skills.** Toggling grants in the GUI is fine
  (snapshot after); creating is not — the GUI writes only to the DB and cannot
  write the asset file or seed it. Use this procedure instead.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'snapshot',
  'Persist DB work to git-tracked text — the admin/GUI step that runs sc snapshot / sc render. The .db is the live shared source of truth; serializing it to git writes the shared main tree, so it is gated to admin (SC_ADMIN) + the GUI Publish button, NOT a per-write shell step.',
  'substrate',
  'sc snapshot',
  0,
  '# snapshot — serialize the DB back to text

The live `shell_db.db` is the **single source of truth shared by every shell** —
a `sc mem` write is durable and visible to all shells the instant it commits.
The `.db` is also **gitignored**, so it reconstructs from git-tracked text on
`sc rebuild`; an edit not yet serialized is discarded by a rebuild (like an
uncommitted working tree on a hard reset).

**Serializing is an admin/GUI operation, not a per-write shell step.** It writes
`.sc-state/` + the flat `_sc` mirror into the **shared MAIN worktree** — running
it from a shell''s linked worktree churns and collides with other shells. So
`sc snapshot` and `sc render flat` **refuse unless `SC_ADMIN=1`** (the GUI/API,
`install`, `update`, and `render-check` set it for you). A shell does not run them;
its writes are captured when admin snapshots (GUI **Publish**/Snapshot button, or
`SC_ADMIN=1 sc snapshot`) before a rebuild. The rest of this skill is for that
admin/GUI path.

## The three text serializations

| File(s) | What | Propagates? | Written by |
|---|---|---|---|
| `schema.sql` | the v1 baseline schema | yes (forks) | hand, rarely |
| `migrations/*.sql` | ordered schema + **system content** deltas (e.g. the skills catalogue) | yes (forks) | author / `sc seed-skills` |
| `.sc-state/content.sql` | **this repo''s** per-instance content + memory — shells, seed/L&S, decisions, roadmap, documents, flags, projects, skill grants. Tracked, fork-owned, kept OUTSIDE the gitignored engine dir | no (stays local) | `sc snapshot` |

The split that matters: **system content propagates via migrations; per-instance
content stays in the snapshot.** Skill *bodies* are system (migration); which
shell is *granted* a skill is per-instance (snapshot).

## When admin serializes (the GUI Publish button does all of this)

All commands below require `SC_ADMIN=1` and are run from the **main checkout**.

1. **`SC_ADMIN=1 sc snapshot`** — dumps the per-instance tables to
   `.sc-state/content.sql` (deterministic DELETE-then-INSERT in PK order, so
   re-running is byte-identical → clean diffs). Captures every shell''s accumulated
   changes to identity, memory, roadmap, documents, flags, projects, or grants.

2. **`SC_ADMIN=1 sc render`** — regenerates the tracked flat `_sc` visibility files
   (`specs_sc/`, `docs_sc/`, `skills_sc/`, `roadmap_sc.md`) from the DB. Run it
   when you changed a document body, the roadmap, or skills. Render is
   incremental — unchanged files aren''t rewritten. (`.claude/skills/` is
   rebuilt at boot, not here — it''s gitignored.)

3. **Verify the rebuild reproduces:** `sc rebuild && sc verify`. The DB
   should rebuild from text alone, byte-for-byte.

   **Before committing any `_sc` render, run `sc render-check`.** It rebuilds
   the DB hermetically (from text) and fails if the committed flat mirror drifts
   from that render — the CI guard, reproduced locally. A plain `sc render`
   renders from your *live* DB, which can lag the source you just edited (see the
   skill-catalogue trap below); `render-check`''s rebuild-first is what catches
   the stale mirror your live-DB render silently passed.

4. **Publish** the text — don''t hand-commit it. `sc snapshot`/`render` write
   `.sc-state/content.sql`, `.sc-state/engine.ref`, and the `_sc` files to the
   **main checkout root** (where the shared engine + DB live), not your worktree,
   so they aren''t yours to stage from a shell branch. The GUI **Publish** button
   commits them and opens one PR (snapshot → render → commit → push → PR on
   `sc_gui_content`); the admin shell on `main` can also commit them directly.
   Never commit the `.db` or anything under the gitignored `.super-coder/` engine
   dir. (In the super-coder SOURCE repo only, `schema.sql` + `migrations/` are
   tracked and committed here too.)

## Authoring vs. snapshotting

- **Per-instance content** (your memory, this repo''s roadmap/docs): edit the DB,
  then `sc snapshot`. The snapshot is the canonical reproducer.
- **Skill catalogue** (system, propagates): edit `assets/skills/<name>/SKILL.md`,
  then `sc seed-skills` — it upserts the live DB *and* (source repo only)
  regenerates the seed migration. Not the snapshot. See `seed_skills.py`.
  - Sequence is **`sc seed-skills && sc render`, then `sc render-check`**
    before committing. Commit the regenerated `migrations/0001_seed_skills.sql`
    *and* the re-rendered `skills_sc/` mirror together — the migration without
    the mirror is the drift.

> Steps 1–3 are durability — serialize so a `sc rebuild` can''t lose your work.
> Step 4 is the GUI **Publish** button: it runs snapshot → render → commit →
> push → PR on the `sc_gui_content` branch, so you rarely commit this text by
> hand. The serialization lives at the main checkout root, not a worktree.

## Related skills

This skill owns the render/snapshot pipeline and the `render-check` guard; the
skills that *feed* it link back here:

- `self_update` — `sc update` re-renders these same `_sc` files; its verify
  step runs `render-check` (this skill) before committing the engine bump.
- `local_skill_management` — fork-local skills persist via `sc snapshot`; run
  `render-check` before committing the `skills_sc/` mirror.
- `migration_management` — a **content-seed** migration (skills, flavor
  defaults) changes what renders; rebuild + render + `render-check` after.
- `docs` / `spec` — document bodies live in the DB and render to `docs_sc/` /
  `specs_sc/`; authored via `sc mem doc`, serialized here.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
