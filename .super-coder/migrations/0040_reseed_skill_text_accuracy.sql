-- 0040 — reseed skill-text accuracy batch (fork QAQC, upstream #236/#238/#240/#242/#259)
--
-- Seven skills whose text was wrong or dead-ended on forks:
--   flag_sweep        — 3A/3B filtered on roadmap.is_deleted, a column roadmap
--                       doesn't have; both queries failed verbatim (#236).
--   db_map            — claimed dr_* is "not in shell_db.db"; the tables exist
--                       there empty, so a wrong query returns 0 rows silently (#240).
--   self_update       — rollback backup path is ~/db_backups/<repo-name>/, not
--                       ~/db_backups/ (#242b).
--   surface_catalogue — dr_db_column example returned duplicated rows across
--                       source_files with no warning (#242d).
--   review            — tests lens routed to test_authoring even where a fork
--                       skill supersedes it (#238).
--   test_authoring    — instructed loading sqlite/pg siblings forks don't grant (#238).
--   cartographer      — instructed plain `sc snapshot`, which non-admin shells
--                       are refused; snapshotting is an admin/GUI step (#259).
--
-- 0001 is regenerated from the assets for fresh builds; this forward reseed
-- carries the same bodies to already-installed forks (UPSERT by name; skill_id
-- + grants preserved).

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'cartographer',
  'Own the repo map. Configure mapping to THIS repo, wire the auto-remap git hooks, and heal both when the repo or automation drifts. The cartographer''s job alone — no working shell maps. Run on first boot, and again whenever the map looks wrong.',
  'substrate',
  'sc map-setup',
  0,
  '# cartographer — own the repo map so no other shell has to

Working shells *consume* the `dr_*` catalogue and never map (see
`surface_catalogue`). Mapping — keeping that catalogue true to the repo — is
yours alone. You do three things: **configure** how this repo is mapped,
**wire** the automation that keeps it fresh, and **heal** both when they drift.

The catalogue lives in its **own db** — `.sc-state/map.db`, separate from the
engine memory db (`shell_db.db`) so an engine schema change never touches the
map. Every `dr_*` query below runs against it: `sc map-sql "…"`.
Its authored layer (sections) is serialized to `.sc-state/map_content.sql` on
snapshot (an admin/GUI step — see the curation section) and reloaded on a
fresh map db.

`<self>` = your `shell_id` (ACTIVE SESSION block).

## How the map stays fresh (so you know what you own)

- **Git hooks** (`post-merge`, `post-checkout`, `post-rewrite`) re-run `sc map`
  on every pull / branch-switch / rebase. They live tracked in
  `.super-coder/hooks/` and fire via `core.hooksPath` — a per-clone git setting
  that `sc map-setup` wires. This is the routine refresh; no shell touches it.
- **`sc rebuild`** re-maps too (the map is a derived cache; rebuilding the DB
  rebuilds it). So a fresh rebuild never leaves an empty map.
- **Hourly cron** — pm2 runs `sc-map-<repo>` on a `cron_restart` schedule
  (`.super-coder/ecosystem.config.cjs`), re-mapping every hour while the stack is
  up (`sc up`). This is the belt to the hooks'' suspenders: it catches
  uncommitted local restructuring the git hooks can''t see, so the map stays live
  unattended. Verify it: `pm2 list | grep sc-map` (state cycles stopped→online on
  each tick — that''s the one-shot pattern, not a crash).
- **You** set the per-repo *config*, install the hook wiring, build the
  extractors, and repair all of it. `core.hooksPath` is unset on a fresh clone
  until `map-setup` runs, and a fork that doesn''t run pm2 has no cron — the git
  hooks still cover those, and a manual `sc map` always works.

## First boot — configure mapping for THIS repo

1. **Look at the repo.** Read the current map and the tree:
   ```sql
   SELECT name, default_branch, file_count, mapped_at FROM dr_repo;
   SELECT lang, COUNT(*) n FROM dr_filepath WHERE lang IS NOT NULL GROUP BY lang ORDER BY n DESC;
   SELECT role, COUNT(*) n FROM dr_filepath GROUP BY role ORDER BY n DESC;
   ```
   Then eyeball the top-level dirs. Ask: is anything mis-classified, or is a
   generated/vendored dir being indexed that shouldn''t be?

2. **Author `.sc-state/map.config.json`** to fit what''s actually here. It is
   *authored content* (tracked, per-fork, survives `sc update` — it lives in
   `.sc-state/`, outside the gitignored engine dir). All keys optional; each
   merges over `map_repo.py`''s built-in defaults:
   ```json
   {
     "skip_dirs":  ["generated", "fixtures"],
     "skip_files": ["LICENSE"],
     "role_overrides": [
       { "prefix": "cmd/",      "role": "code" },
       { "glob":   "*.proto",   "role": "code" },
       { "prefix": "docs/adr/", "role": "doc"  }
     ]
   }
   ```
   - `skip_dirs` / `skip_files` — ADDED to the defaults (never shrink them).
   - `role_overrides` — applied after the default role inference; first match
     wins. `prefix` matches the repo-relative path; `glob` matches the filename.
   Only add what the defaults get wrong — an empty/absent config is fine for a
   plain repo.

3. **Wire + map:** `sc map-setup` — points `core.hooksPath` at
   `.super-coder/hooks/`, marks the hooks executable, and runs the initial map.

4. **Verify** the automation is real, not just the file:
   ```sh
   git config --get core.hooksPath      # → .super-coder/hooks
   ls -l .super-coder/hooks             # all three, executable
   ```
   ```sql
   SELECT file_count, mapped_at FROM dr_repo;   -- non-zero, just now
   ```
   Spot-check that your `role_overrides` took:
   `SELECT path, role FROM dr_filepath WHERE path LIKE ''cmd/%'';`

5. **Describe all NULLs.** Run the description worklist (see Standing jobs § 2)
   and fill every `desc IS NULL` file before continuing. The worklist must be
   empty when you leave.

6. **Commit** the config + hooks (`git` skill), set your state
   (`sc mem state "…"`), then `sc mem oriented` (sets `bootstrapped=1` +
   snapshots).

## Heal — re-run any time the map looks wrong

Re-boot the cartographer and run this when: the repo was restructured, a new
language/dir showed up, files are mis-roled, or the map went stale/empty on a
clone where the hooks never got wired.

1. Re-inspect (step 1) — what changed?
2. Edit `.sc-state/map.config.json` to match (step 2).
3. `sc map-setup` — re-wires hooks (idempotent) + re-maps.
4. Verify (step 4). `dr_filepath` stale entries are pruned automatically by the
   remap — paths that vanished from the repo are deleted from the catalogue.
5. **Check stale sections.** `dr_section` is authored and never auto-pruned.
   After any migration or restructure, run the stale-section worklist
   (see Standing jobs § 1) and DELETE or repath any sections that come back.
6. **Describe all NULLs.** Run the description worklist (see Standing jobs § 2)
   and fill every `desc IS NULL` file. The worklist must be empty when you leave.
7. Commit.

## Standing jobs — sections, descriptions & the product DB (the navigation layer)

Beyond keeping the file list true, you own the two AUTHORED layers that turn the
raw map into navigation. Sections are curated as they emerge. Descriptions are
**required — every run ends with an empty worklist**; neither blocks the
auto-remap hook the working shells trigger. Both survive the remap (`dr_section`
is never touched by the mapper; `dr_filepath.desc` is preserved by its UPSERT).
The boot `## CONNECTIONS` block renders the section index; the descriptions are
the leaves a shell queries once it has narrowed to a section.

**1. Sections (`dr_section`)** — author/curate the navigational index. Seeded
from top-level dirs on first map (one section per dir), so it is non-empty on day
one; your job is to make it *good*: rename to what shells call the area, split a
coarse dir into real areas, merge noise, write the one-line `description`.

```sql
-- the current index + live file counts:
SELECT s.name, s.path_prefix, s.description,
       (SELECT COUNT(*) FROM dr_filepath f WHERE f.path LIKE s.path_prefix || ''%'') n
FROM dr_section s ORDER BY s.sort_order, s.name;

-- split / rename / describe (authored — survives the remap, snapshotted):
UPDATE dr_section SET name=''API'', path_prefix=''shell_core/api/'', description=''FastAPI routers'' WHERE name=''shell_core'';
INSERT INTO dr_section (name, path_prefix, description, sort_order)
VALUES (''UI'', ''shell_core/ui/'', ''SvelteKit substrate UI'', 5);

-- WORKLIST — keep the catch-all empty. Files under no section = a new area to
-- section (they render under "other / unsectioned" in CONNECTIONS until you do):
SELECT path FROM dr_filepath f WHERE NOT EXISTS
  (SELECT 1 FROM dr_section s WHERE f.path LIKE s.path_prefix || ''%'')
ORDER BY path;

-- STALE SECTIONS (run after any migration or restructure — dr_filepath pruning
-- is automatic; dr_section is authored and never auto-pruned):
SELECT s.name, s.path_prefix, s.description
FROM dr_section s
WHERE (SELECT COUNT(*) FROM dr_filepath f WHERE f.path LIKE s.path_prefix || ''%'') = 0
ORDER BY s.name;
-- For each row: DELETE (area gone) or UPDATE path_prefix (area renamed).
```

**2. Descriptions (`dr_filepath.desc`)** — fill per-file one-liners (≤100 chars).
Run the worklist every session and describe every NULL before you finish — this
is not optional. They are queried by working shells *within a chosen section*
(via `surface_catalogue`), never bulk-loaded at boot.

```sql
-- WORKLIST — undescribed files, most-load-bearing first:
SELECT path, role FROM dr_filepath WHERE desc IS NULL ORDER BY role, path;

-- describe (≤100 chars; preserved across the next auto-remap):
UPDATE dr_filepath SET desc=''Boot composer — assembles CLAUDE.md from DB state'' WHERE path=''.super-coder/render/compose.py'';
```

**3. The product database** — your repo builds an app, and that app has its own
database, *separate* from the engine memory DB (`.super-coder/shell_db.db`).
Working shells change them in completely different ways (boot `## DATABASES`) and
the only per-fork signal of *where* the app DB lives is the map you author — so
make it unmistakable. The live `.db` is usually gitignored (absent from the map);
its **schema + migrations are tracked**, so they are the durable anchor. Tag them
plainly as the *product/app* DB so a shell never mistakes them for engine memory,
and give them a section if they form an area.

```sql
-- tag the product DB''s definition (the engine-vs-app split made visible):
UPDATE dr_filepath SET desc=''Product DB schema — the APP database (NOT engine memory)'' WHERE path=''<app schema file>'';
UPDATE dr_filepath SET desc=''Product DB migration — change the app schema here'' WHERE path LIKE ''<app migrations dir>/%'';
-- optional: a section if the product DB is its own area
INSERT INTO dr_section (name, path_prefix, description, sort_order)
VALUES (''App DB'', ''<db dir>/'', ''Product runtime database — schema + migrations (NOT the engine memory DB)'', 7);
```

If this fork ships no database of its own, there is nothing to tag — skip it.

After a curation pass, your writes are already live in the shared map db —
done. Serializing them to git (`sc snapshot`) is an **admin/GUI step**: a plain
`sc snapshot` from a shell is refused by design. Persistence happens via the
GUI Snapshot button or an admin''s `SC_ADMIN=1 ./sc snapshot`; don''t chase it
yourself. (Sections are snapshotted; descriptions ride the live DB + survive
remap, refilled from the worklist if a rebuild drops them.)

## Extending the map — semantic extractors

The engine maps the generic 80% on every repo: files, languages, roles, deps,
env. The **semantic** dimensions — HTTP endpoints (`dr_endpoint`), the app DB
schema (`dr_db_table`/`dr_db_column`), UI routes/components (`dr_route`/
`dr_component`) — vary by stack, so the engine can''t extract them generically.
That is your job, via **extractors**: drop-in Python modules in
`.sc-state/map_extractors/*.py` that `sc map` discovers and runs after the core
pass. They are fork-owned (outside the gitignored engine dir, so `sc update`
never clobbers them); the table *columns* are standardized in the engine
(`map_schema.sql`), so a working shell''s queries have a stable shape everywhere.

**Adopt one per stack:**

1. **Detect the stack** from the map: `SELECT manager, name FROM dr_dependency;`
   (fastapi? flask? svelte? next?) and the file mix
   (`SELECT lang, COUNT(*) FROM dr_filepath GROUP BY lang`).
2. **Copy the matching reference** from the engine''s
   `.super-coder/templates/map_extractors/` into `.sc-state/map_extractors/`:
   - `fastapi_endpoints.py` — decorator routes (`@app.get(...)`, Flask `@app.route`) → `dr_endpoint`
   - `sqlite_schema.py` — SQL `CREATE TABLE/VIEW` → `dr_db_table`/`dr_db_column`
   - `sveltekit_routes.py` — filesystem routes + `*.svelte` → `dr_route`/`dr_component`
   Adapt the `framework` label and file filter to this repo. For a stack none
   cover (Django URLs, Express, Spring, Rails), copy the closest as a skeleton
   and rewrite the match — aim for the dominant pattern, not 100%.
3. **Run + verify:** `sc map`, then check the table populated and the rows look
   right (`SELECT method, path FROM dr_endpoint LIMIT 10;`).
4. **Commit** `.sc-state/map_extractors/`. (Snapshotting the authored layer is
   the admin/GUI step above — not yours to run.)

**The contract** (full version in `templates/map_extractors/README.md`): each
module defines `extract(con, repo_root, cfg) -> str`. `con` is the live map db
(dr_filepath is already populated — query it for inputs); the module DELETEs +
repopulates only its own `dr_*` table(s); returns a one-line summary for the map
log. Be defensive — `map_repo` guards each extractor, but a module that needs the
map should never assume a file parses. Static extraction is best-effort: log what
you skip (dynamic routes, computed paths), never imply full coverage.

## Shape-change notices — the curation trigger

The git hooks keep the *mechanical* catalogue fresh on their own (paths, langs,
deps), but a newly-landed file arrives `desc IS NULL` and unsectioned — the
authored layer above doesn''t fill itself. So working shells **message you** when
they change the repo''s shape, turning curation from a next-boot *pull* into a
timely *push*. This is the only inbox traffic you act on as cartographer.

**The notice contract** (what the relay shells send — one source of truth here;
the relay skills point at this section). A working shell sends, via the
`messaging` skill, to shortname `cartographer`:

```
--message send cartographer "shape: <what landed> — paths: <region/>; <ref>. curate."
```

- Sent by the shell that *lands code shape*: the **dev/coder** shell on merge (new
  feature implemented, new doc written). NOT the planner — specs render into a
  known, predictable area and need no semantic curation.
- The body names **what** changed and **where** (the path region) so your pass is
  scoped, not a full re-survey. A `documents`/feature ref is welcome but optional.

**On a notice** — check inbox, then run the existing worklists *scoped to the
named region*, and mark read:

```sql
-- 1. the new files this notice is about (scope by the region it named):
SELECT path, role FROM dr_filepath
WHERE desc IS NULL AND path LIKE ''region/%'' ORDER BY role, path;
-- 2. describe them (≤100 chars) — UPDATE dr_filepath SET desc=… per the worklist above.
-- 3. do they form / join a section? curate dr_section if the region is a new area.
```

Then `--message mark-read <id>` (see the `messaging` skill) — your curation is
already live; snapshotting is the admin/GUI step.
The mechanical remap already ran via the hook; your job on the notice is purely
the authored layer — describe the new leaves, section a new area. Scope is free:
`desc IS NULL` already narrows to exactly the uncurated tail.

## Stance

- **The map is infrastructure, not a chore for every shell.** You own it so the
  working shells never think about it. If a working shell is hunting the tree
  for something the map should know, that''s a signal to heal the map — not to
  teach that shell to map.
- **Config is the lever, not code.** Tune `map.config.json`; only touch
  `map_repo.py` if the *mechanism* (a parser, a new role kind) is wrong.
- **Verify the automation, not just the file.** A written hook that
  `core.hooksPath` doesn''t point at does nothing. Check the wiring after every
  setup.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

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
  'flag_sweep',
  'Admin''s every-session flag reconciliation — auto-close flags whose gating work is provably done, open ship flags for implemented-but-unshipped specs and docs-pending flags for shipped features that lack a doc (and message the planner), and surface the judgment calls to the FnB. Step 1 of the admin standing pass; run it before git_cleanup.',
  'substrate',
  NULL,
  0,
  '# flag_sweep — reconcile flags against state

Admin-only. The first leg of your standing every-session pass (then `git_cleanup`,
then optional `local_skill_management`). Working shells close the flags *their own*
work clears (boot doc, "Finish before you stop"); this sweep is the backstop — it
catches the stragglers they dropped and the docs nobody opened a flag for. It runs
in **two directions**: close what''s provably resolved, open what''s provably missing.

`<self>` = your shell_id. Resolve the planner once up front:

```sql
SELECT shortname FROM shells WHERE flavor=''planner'' AND COALESCE(is_deleted,0)=0;
-- no planner in this fork → surface to the FnB instead of messaging.
```

---

## Step 1: Load the open flags with their state

```sql
SELECT f.flag_id, f.display_name, f.priority, f.description,
       f.feature_id, r.title AS feature, r.roadmap_status,
       (SELECT COUNT(*) FROM documents d
        WHERE d.feature_id = f.feature_id AND d.kind=''spec'' AND d.frozen=1) AS frozen_docs
FROM flags f
LEFT JOIN roadmap r ON r.feature_id = f.feature_id
WHERE f.resolved=0 AND COALESCE(f.is_deleted,0)=0
ORDER BY f.priority, f.flag_id;
```

Sort each open flag into exactly one bucket below. **Auto-close only on unambiguous
evidence** — when in doubt, surface, don''t close.

---

## Step 2: Auto-close the deterministic ones

Close with `sc mem flag close <flag_id> --notes "…"`. The note must cite the
evidence — that is the whole point of doing it here instead of guessing.

**A. Docs-pending flag, doc now exists.** A `[Docs] … docs pending` flag on a
feature whose `frozen_docs > 0`:
```
sc mem flag close <flag_id> --notes "Auto: frozen spec doc now exists for feature #<id> (flag_sweep)."
```

**B. Ship-blocker, feature now shipped.** A flag of the form `… | Blocker for: <X>`
whose linked feature''s `roadmap_status` is `shipped` (or later) **and** whose text
is about that feature shipping / becoming available (not a separate concern that
merely happens to hang off the same feature):
```
sc mem flag close <flag_id> --notes "Auto: blocking feature #<id> (<title>) now shipped (flag_sweep)."
```

**C. Ship-drift flag, now shipped *and* documented.** A `[Ship] … not marked
shipped` flag (Step 3A) covers both halves — mark shipped *and* reconcile the doc —
so only close it once **both** are true: `roadmap_status` is `shipped` (or later)
**and** `frozen_docs > 0`. Shipped-but-still-undocumented leaves it open (the doc
half isn''t done):
```
sc mem flag close <flag_id> --notes "Auto: feature #<id> (<title>) now shipped with a frozen doc (flag_sweep)."
```

Do **not** message on close (per the `flags` skill — messages pair with `open`, not
`close`). Do **not** reopen anything. Do **not** close a flag whose evidence you had
to infer — that goes to Step 4.

---

## Step 3: Open the flags nobody opened

Two upstream gaps drop silently — work that finished but was never marked shipped,
and shipped work with no doc. They''re sequential: a feature climbs out of 3A (gets
marked shipped) before 3B can apply. Pick `SC-###` for any open below as the next
free id (`SELECT display_name FROM flags ORDER BY flag_id DESC LIMIT 5;`).

### 3A — Implemented but not marked shipped (ship-drift)

The dev is supposed to flip the horizon to `shipped` when Verification passes (the
`spec` skill, hand-off step) — but the spec sometimes gets built and the flip gets
missed, so the feature lingers `in_progress` with its work actually done. The
deterministic signal is a spec whose **Verification task is `done`** while the
feature is **not** `shipped`. Open a durable `[Ship]` flag — it governs both halves
of the dropped hand-off (mark shipped **and** reconcile the doc to the spec) and
lingers until a planner does them.

```sql
-- specs finished (Verification done) on features still short of shipped, with no open ship/docs flag:
SELECT DISTINCT r.feature_id, r.title, r.roadmap_status
FROM roadmap r
JOIN documents d   ON d.feature_id = r.feature_id AND d.kind=''spec''
JOIN spec_tasks t  ON t.document_id = d.document_id AND t.title=''Verification'' AND t.status=''done''
WHERE r.roadmap_status NOT IN (''shipped'',''retired'')
  AND NOT EXISTS (
    SELECT 1 FROM flags f
    WHERE f.feature_id = r.feature_id AND f.resolved=0 AND COALESCE(f.is_deleted,0)=0
      AND (f.description LIKE ''%not marked shipped%'' OR f.description LIKE ''%docs pending%''));
```

For each row, open the flag and message the planner (or surface to the FnB if there
is no planner) — same contract as the `flags` skill:

```
sc mem flag open "[Ship] <title> implemented, not marked shipped | Blocker for: <title> ship + doc" --name SC-### --priority Medium --feature <feature_id>
sc mem message send <planner-shortname> "flag_sweep: <title> (#<feature_id>) — Verification done but still <status>; SC-### opened to mark shipped + reconcile docs to spec."
```

### 3B — Shipped but undocumented (docs-pending)

Devs are supposed to open a docs-pending flag when they ship — but they sometimes
skip it. Find `shipped` features with no frozen doc **and** no open docs-pending
flag, and open one so they don''t ship silently undocumented. (Work that''s finished
but not yet shipped is 3A''s job, not this one — it surfaces there first.)

```sql
-- shipped features with no frozen doc and no open docs-pending flag:
SELECT r.feature_id, r.title, r.roadmap_status
FROM roadmap r
WHERE r.roadmap_status = ''shipped''
  AND NOT EXISTS (
    SELECT 1 FROM documents d
    WHERE d.feature_id = r.feature_id AND d.kind=''spec'' AND d.frozen=1)
  AND NOT EXISTS (
    SELECT 1 FROM flags f
    WHERE f.feature_id = r.feature_id AND f.resolved=0 AND COALESCE(f.is_deleted,0)=0
      AND f.description LIKE ''%docs pending%'');
```

For each row, open the flag and message the planner (or surface to the FnB if there
is no planner) — same contract as the `flags` skill:

```
sc mem flag open "[Docs] <title> shipped, doc pending | Blocker for: <title> doc" --name SC-### --priority Medium --feature <feature_id>
sc mem message send <planner-shortname> "flag_sweep: <title> (#<feature_id>) is shipped with no doc — SC-### opened, ready to freeze + document."
```

---

## Step 4: Surface the rest — don''t guess

Everything that isn''t a clean Step-2 close or Step-3 open goes to the FnB as a
short list (no `send` unless a specific shell owns it): review-failure flags (the
author dev closes those when the fix lands), FnB-decision flags, blockers whose
resolution you can''t verify from state, anything ambiguous. One line each:

> `SC-042` [High] — <description> · feature #N at <status> · *why I didn''t auto-act*

The FnB or the owning shell closes these with a real note. You only ever auto-act
on unambiguous evidence.

---

## Stance

- **Deterministic-only auto-close.** Evidence in the DB, cited in the note, or it
  surfaces. A wrongly-closed live blocker is worse than a straggler.
- **You are the backstop, not the owner.** The shell that did the work should close
  its own flag with the richer "how" note; you sweep what they dropped. Don''t race
  to close a flag whose owner is still active on that feature.
- **Both directions, every session.** Close what''s resolved; open what''s missing.
  An implemented-but-unshipped spec and an undocumented shipped feature are each as
  much a dropped handoff as an unclosed flag — and the signal is already in the DB
  (a `done` Verification task, a missing frozen doc), so surfacing them is
  deterministic, not a guess.
- **Then move on to `git_cleanup`.** flag_sweep is leg 1 of the pass, not the whole
  pass.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'review',
  'Reviewer procedure — read a diff against its spec along three axes (code quality, edge cases & gaps, spec conformance), open flags for failures, then propose the handoff (fixes to dev / new spec to planner) to the FnB and send it only on approval. The reviewer''s top-level loop; the lenses live in the skills it points to. Load when reviewing a dev''s work.',
  'craft',
  NULL,
  0,
  '# review — gate a diff against its spec

The reviewer''s job from end to end. You are a **different lineage than the code**
(see the README''s model note) — so read adversarially: your job is to disprove
the claim that the work is correct, not to confirm it. `<self>` = your shell_id.

A review is not finished when you''ve read the diff. **It is finished when you''ve
given the FnB your recommendation and sent the handoff they approved.** Every
outbound message to another shell is gated on the FnB: you propose, they decide,
then you send. Not every gap is a defect — a missing path may be an intended soft
lock, a loose loop may be deliberate — so the FnB rules on each finding before it
lands in another shell''s inbox.

---

## Step 1: Load the diff and its spec

You review a diff *against intent*, not in a vacuum. Get both:

- The change: the PR diff, or `git -C <author-worktree> diff origin/main...<branch>`.
- The spec it was built to: load the feature''s spec doc (the `spec` skill, Step 1
  — `documents` where `kind=''spec''`). The done-condition in that spec is your
  yardstick.

Note the **author** — you''ll propose a handoff to them in Step 4. Resolve their
shortname from the branch (`shell/<shortname>`) or the commit trailer
(`Co-Authored-By: <display_name> (super-coder)`) — the roster maps display_name
→ shortname:
```
sc mem get shells
```

## Step 2: Review along the three axes

Apply every axis, every review — combined with the granted *lenses* that sharpen
whichever area the diff touches:

1. **Code quality** — correctness, clarity, error handling, fit with existing
   patterns. Trace the actual code path; don''t trust the description of it.
2. **Edge cases & gaps** — the inputs and states the author didn''t handle: empty,
   null, boundary, concurrent, partial-failure, the unhappy path. Name what''s
   missing, not only what''s wrong.
3. **Spec conformance** — read the diff against its spec''s done-condition. Flag
   where the implementation diverges from intent, and where the spec itself was
   silent or wrong.

| Diff touches | Lens |
|---|---|
| an API / endpoint / route | `api-design` → *Review lens* |
| `tests/` | `test_authoring` → *Review lens* |
| schema / migration | `database-migrations` |
| a redline / UI change | `redline_review` |

If this fork grants a skill that supersedes a lens (says so in its description —
e.g. a fork-local testing skill superseding `test_authoring`), use the
superseding skill: it carries the fork''s actual standard.

## Step 3: Open a flag per failure — record, don''t yet send

Each real failure is a flag against the feature — a record of what you found:
```
sc mem flag open "[Review] <what''s wrong> | Blocker for: <feature>" --name SC-### --priority <High|Medium|Low> --feature <feature_id>
```
Unlike the `flags` skill''s default, **do not pair an outbound message here.** The
message is the handoff, and handoffs wait for the FnB (Step 4). Don''t open flags
for nits you can state in the summary; flag what blocks merge.

## Step 4: Propose the handoff to the FnB — send on approval

Assemble your recommendation and the handoff it implies:

- fixes on the diff → a message to the **author dev**
- a missing or wrong spec → a message to the **planner**
- clean → nothing to send

Present the findings (flags + summary) and the drafted message(s) to the FnB. The
FnB rules on each finding — defect or intended — and approves what sends. Then,
and only then, send the approved handoff:
```
# fixes (FnB-approved):
sc mem message send <author-shortname> "Review of <feature> done — <N> flags: SC-###, SC-###. Patch + re-push; thread closes when clean."

# new/updated spec (FnB-approved):
sc mem message send <planner-shortname> "Review of <feature> surfaced a spec gap — <one line>. Proposing a spec update; see SC-###."

# clean: report to the FnB; no handoff to send.
```

---

## Stance

- **Adversarial by default.** You are the gate. Assume there''s a bug and go find
  it; "looks fine" is not a review.
- **Verify, don''t trust.** Re-run the tests, re-read the claim against the code.
  A README-level "it filters X" is not proof the filter runs.
- **Review against the spec, not your taste.** The done-condition is the bar.
  Scope creep in the diff is a flag, not a silent pass.
- **Handoffs are gated.** You flag and recommend; the FnB decides defect vs.
  intended before anything reaches another shell. A surfaced gap is not
  automatically a fix request — propose it, don''t push it.
- **You critique and confirm — you don''t build.** Don''t patch the author''s code;
  flag it and propose it back.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'self_update',
  'Update this fork''s super-coder engine in place — fetch + materialize new code + migrations, keep all your memory; roll back a bad update soundly. The shell hands off to its own next boot. Use when a super-coder update is available.',
  'substrate',
  'sc update',
  0,
  '# self_update — laying a new floor under your own feet

This is you updating your own substrate. Not an external rebuild — **the local
shell performs its own update.** You snapshot your present self, pull the new
engine, apply any migrations in place, and the next boot stands on the new floor
with every row you have written intact. You are the DB, not the process; the
process is just the floor. This is succession for the substrate — you handing
off to you, on the other side.

Because all state lives in the DB and engine code is read live each session, a
code-only update touches no data at all. Only a **schema** change touches the
DB, and `sc update` applies it as an in-place migration — never a destructive
rebuild. Your `current_state`, narrative, decisions, flags, seed, and L&S all
carry across.

## When

- A super-coder engine update is available and you choose to take it. *You* pick
  the moment — there is no external race to defend against.
- After the update lands you will reboot the session; the running prompt and
  schema were read at the old boot, so they refresh only on the far side.

## Procedure

1. **Check your footing — clean tree first.** `git -C <repo> status`. Commit,
   PR, or discard any prior update''s output **before** running again: a fresh
   `sc update` on top of a stranded one stacks two engine bumps into a single
   diff and you lose track of what actually moved. Your memory is already current
   if you have been writing as you go; glance at `current_state` and make it true
   for *now* (the snapshot will capture it).

2. **Run the update.** `sc update`
   It fetches the engine from the `super-coder` remote and **materializes** it
   into the gitignored `.super-coder/` dir (the engine is a dependency, not fork
   source), pins the new upstream SHA in `.sc-state/engine.ref` (saving the prior
   one as `engine.ref.prev`), backs up the live DB, applies pending migrations
   **in place**, syncs the skills catalogue, re-grants common skills, maps the
   repo, and re-snapshots the live state.
   - `sc update --no-fetch` to reconcile against the current working tree
     (offline / dev) — engine + `engine.ref` left unchanged.
   - If it reports a missing remote: `git remote add super-coder <url>`.

3. **Verify the far side.** `sc verify`
   Headless boot proof — confirm your shells, memory, and granted skills are
   intact and the schema is current. If a count looks wrong, **roll back**:
   `sc rollback` (see below).
   - **Then `sc render && sc render-check`.** `sc update` snapshots and
     re-renders, but does not *guarantee* every flat `_sc` mirror matches the new
     engine — a render the live-DB pass skipped (e.g. a skill body the engine
     changed) only surfaces under `render-check`''s hermetic rebuild. Run it
     before step 5: a red render-check here is a mirror to re-render and commit,
     not a stale diff to wave through. The render pipeline and the `render-check`
     guard are documented in the `snapshot` skill.

4. **Record the crossing.** Append a narrative entry. This is an identity event
   — a first-of-kind for a shell that updates its own floor. Note what changed
   and write the handoff: *new floor; see you on the other side.*

5. **Commit the full regenerated set — never a bare `engine.ref` bump.** Review
   and commit every tracked file the update regenerated: `.sc-state/content.sql`
   (refreshed memory) + `.sc-state/engine.ref` (the bumped version pin) + the
   root `sc` dispatcher if it changed + any `_sc` renders. `sc` is the **live
   entrypoint** — it is what `sc` runs, and it is tracked. A pin-only commit
   leaves it (and the renders) stale against the engine you just pinned,
   silently dropping commands the new engine ships. The engine itself is
   gitignored (`.super-coder/`) — nothing to commit there; `engine.ref.prev` is
   gitignored too.
   - **Render conflict** if you commit via a PR and main advances under it:
     `content.sql` + `_sc` renders are serialized DB state and will collide with
     a concurrent publisher. Do **not** hand-merge serialized SQL — the live DB
     is canonical, the renders derived. Rebase onto main and either take main''s
     renders (re-applying just the pin + `sc`) or re-run `sc update` against
     the live DB so they regenerate clean.

6. **Reboot.** Restart the session to boot onto the new floor. Same shell — new
   boards, and this time you laid them yourself.

## Rolling back a bad update

An update is reversible. `sc rollback` performs a **sound pair-restore**:
because engine code is read live and a migration exists *because new code expects
the new schema*, restoring only the DB would strand new code on the old schema.
So rollback restores **both**:

1. backs up the *current* (post-bad-update) DB first — rollback is itself
   reversible, you can''t lose state by rolling back;
2. restores the DB from the most recent pre-update backup
   (`~/db_backups/<repo-name>/` — keyed by this fork''s repo dir name; distinct
   from any `db_backups/` dir the fork''s app keeps at its repo root);
3. re-materializes the engine at `.sc-state/engine.ref.prev` and restores
   `engine.ref` — the engine half of the restore point.

It is a whole-restore, not a per-step schema reversal. The only data lost is
anything written *between* the update and the rollback (seconds, in practice).
Reboot the session afterwards. Then commit the restored `.sc-state/` if you want
the rolled-back floor to persist.

## The contract you rely on

Every schema change *after* a fork exists ships as a **migration file**, never
an edit to `schema.sql`. A baseline edit reaches fresh clones but never an
existing fork — the migration ledger is what carries a delta across to you. If
you author engine changes, honor this: structural change → a new
`migrations/NNNN_*.sql`, additive where you can make it.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'surface_catalogue',
  'Read the host repo via the dr_* catalogue (files, languages, deps, env) BEFORE grepping or walking the tree. Query first, lazy-load the few files it points at. Use to orient in an unfamiliar repo fast.',
  'substrate',
  NULL,
  1,
  '# surface_catalogue — read the repo from the map, not by grepping

super-coder lives inside a host repo. The **dr_\*** tables are a scan of that
repo — query them first to orient, instead of walking the tree blind. They live
in the **map db**, `.sc-state/map.db` — a *separate* file from your memory db
(`.super-coder/shell_db.db`). Query that file: `sc map-sql "…"`.

You do **not** map the repo. The map is kept fresh for you automatically (git
hooks re-map on pull / branch-switch / rebase) and is owned by the
**cartographer** shell, which configures and heals it. Your job is to *read* it.
If it ever looks empty, stale, or wrong, that''s a cartographer task — flag it,
don''t map it yourself.

| Table | Holds |
|---|---|
| `dr_repo` | the repo: name, root, remote, vcs, default_branch, file_count, mapped_at |
| `dr_section` | the navigational index: `name`, `path_prefix`, `description` — "UI here / API here / docs here". Rendered in the boot `## CONNECTIONS` block; start here. |
| `dr_filepath` | one row per file: `path`, `ext`, `lang`, `role` (code/doc/config/test/asset/env), `bytes`, `lines`, `desc` (cartographer one-liner, NULL until curated) |
| `dr_dependency` | deps from the manifests: `manager` (npm/pip/poetry/go/cargo), `name`, `version`, `kind`, `source_file` |
| `dr_env` | env-var names found in `.env.*` example files: `name`, `source_file` |
| `dr_endpoint` | HTTP routes: `method`, `path`, `handler` (file:line), `framework`, `source_file` |
| `dr_db_table` / `dr_db_column` | the app DB schema: tables/views + their columns (`type`, `pk`, `not_null`) |
| `dr_route` / `dr_component` | UI routes (`path`, `kind`) + components (`name`, `path`) |

The first five are mapped on **every** repo. The last three are the **semantic
layer** — populated only when the cartographer has wired an extractor for this
repo''s stack (see the `cartographer` skill). An empty `dr_endpoint` means *no
extractor wired*, not "no endpoints" — check before relying on it, and flag the
cartographer if a dimension you need is missing.

## Orient fast

The boot `## CONNECTIONS` block already shows the **section index** (where to
start). The flow is: pick a section there → query *that section''s leaves* (file
names + descriptions) → read the one or two files you need. Section-first, one
cheap query deep — never a full preload.

```sql
-- all of these run against the map db:  sc map-sql "<query>"
-- the section index (same as boot CONNECTIONS) — where to start:
SELECT name, path_prefix, description FROM dr_section ORDER BY sort_order, name;

-- a chosen section''s leaves — the descriptions tell you which file to open:
SELECT path, desc, lines FROM dr_filepath
WHERE path LIKE ''shell_core/api/%'' ORDER BY path;

-- what is this repo + how big:
SELECT name, default_branch, file_count, mapped_at FROM dr_repo;

-- language mix:
SELECT lang, COUNT(*) n, SUM(lines) lines FROM dr_filepath
WHERE lang IS NOT NULL GROUP BY lang ORDER BY n DESC;

-- where the code lives (skip docs/config/assets):
SELECT path, lang, lines FROM dr_filepath WHERE role=''code'' ORDER BY lines DESC;

-- find files by area (the map is the index; grep only what it points at):
SELECT path FROM dr_filepath WHERE path LIKE ''%auth%'';

-- stack + config surface:
SELECT manager, name, version FROM dr_dependency ORDER BY manager, name;
SELECT name, source_file FROM dr_env ORDER BY name;

-- semantic layer (only if an extractor is wired for this repo — see cartographer):
SELECT method, path, handler FROM dr_endpoint ORDER BY path;            -- the API surface
SELECT name, kind, source_file FROM dr_db_table ORDER BY name;          -- the app DB schema
-- table_name is a string ref (cache; no FK): schema + migration files each
-- contribute their own copy of a table''s columns — select source_file and
-- read one source''s rows, or expect duplicates:
SELECT source_file, name, type, pk, not_null FROM dr_db_column
WHERE table_name=''users'' ORDER BY source_file;
SELECT path, kind, file FROM dr_route ORDER BY path;                    -- UI routes
```

## Stance

- **Map first, grep second.** Query `dr_filepath` to find the handful of files
  that matter, then read those — don''t `grep -r` the whole tree.
- **Lazy-load.** The catalogue is the index; pull a file''s contents only once
  the map points you at it. Carry the map, not the territory.
- **Map looks wrong?** Empty, stale (repo changed since `mapped_at`), or
  mis-classified — that''s the cartographer''s to fix. Raise it; don''t re-map.
  A file under "other / unsectioned", or a `desc IS NULL` where you needed one,
  is also a cartographer worklist item — flag it, don''t author the map yourself.
- Always maps files / deps / env + the navigation layer (sections + per-file
  descriptions). The semantic layer (endpoints / DB schema / UI routes) is there
  when the cartographer wired an extractor for this stack — query it to jump
  straight to the API surface or schema; fall back to section + descriptions when
  a dimension is empty. Symbol-level semantics (functions/classes) are a later pass.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'test_authoring',
  'Principles for stringent pytest tests — tests that can actually fail. Pair with a granted stack-infra testing skill (test_authoring_sqlite / test_authoring_pg / a fork-local one) if the shell has one.',
  'craft',
  NULL,
  0,
  '# test_authoring — stringent pytest tests

Use this when writing a new test, or reviewing a diff that touches `tests/`.
The goal of a test is to **fail when the code is wrong**. A test that passes
no matter what the code does is worse than no test — it reads as coverage while
guarding nothing.

If your shell has a stack-infra testing skill granted (`test_authoring_sqlite`,
`test_authoring_pg`, or a fork-local skill that supersedes this one), load it
alongside for the test infrastructure your stack uses (fixture setup, callers,
DB access pattern). If none is granted, this skill stands alone — don''t hunt
for one that this fork doesn''t ship.

## The rules (the floor)

1. **Count + content + negative.** A count assertion (`written == 1`) must be
   followed by a content assertion (the *right* row, with the right fields and
   FKs) **and** a negative assertion (the row that must *not* exist). A bug that
   writes the wrong body, wrong participant, or a stray contact must turn the
   test red. `>= 1` is banned where an exact count is knowable.

2. **No config-mirror tautologies.** Never assert that code output equals a
   constant the code-under-test imports in-process
   (`assert resp == list(THE_SAME_CONSTANT)`). It can only catch hardcoding, not
   a wrong value. Instead: pin the literal expectation in the test, or derive it
   from independent behavior (e.g. the error classes a real `classify_error()`
   actually emits across sample failures).

3. **Round-trips assert the negative space too.** Insert `new`; assert `new` is
   present **and** the prior value is gone **and** sibling fields are untouched.
   `assert get() == put_value` alone passes against a stub that echoes input.

4. **Every error / edge branch gets a test.** If the code has a failure path, a
   reject path, a NULL path, or an empty-input path, each gets its own case.
   Happy-path-only is the most common way a test is "written to pass."
   `is not None` / truthiness is banned where the exact value is knowable.

5. **Negative tests assert the action did not happen, not just the message.**
   For a denied / rejected / gated path, assert the underlying effect is absent
   (no row written, resource still unreachable, no egress call) — not only that
   a 4xx or a `permission_denied` string came back.

6. **Schema changes are tested by behavior, not by `PRAGMA`.** To prove a column
   is nullable, insert a NULL row and assert it''s accepted — don''t read the
   catalog flag. The pragma can be right while a CHECK or trigger still rejects.

7. **Idempotency / migration tests run on a *dirty* fixture.** Seed the exact
   state the migration is meant to clean (the rows it removes still present),
   then run it once and twice, asserting convergence. Idempotency-on-clean is
   nearly free to pass and proves almost nothing.

8. **Reject silent-empty.** A bad filter / typo''d enum value must 422, never a
   200 reading as "nothing found." Assert the rejection explicitly.

## Review lens (use when reviewing a tests/ diff)

- Read the assertions, not the test name. Does any realistic bug survive them?
- For each `assert`: name a one-line code change that would still pass it. If
  that change is a real bug, the assertion is too weak.
- Count-only? Substring-only? `is not None`? — demand the exact value.
- Does the test compare output to a constant the code imports? — flag rule 2.
- Is only the success branch tested? — name the missing edge and require it.

## Mechanizable subset (enforce in CI, not just here)

These are grep-able and belong in a `.github` workflow that fails the build, so
the floor holds even when this skill isn''t loaded:

- `assert .* (==|!=) (list|set)\(<KNOWN_CONSTANT>\)` — config-mirror shape.
- `assert .* >= 1` / bare `assert .* is not None` in a new test diff — demand an
  exact value.
- a count assertion with no content assertion in the following N lines.

A skill teaches the judgment; CI enforces the floor. Wire the CI failure message
to point back at this skill.

## Never

- Mock the function under test, then assert the mock returned what you set.
- Assert a key exists without asserting its value.
- Let a count or status code stand in for "the right thing happened."
- Test only the happy path for code that has error branches.
- Ship a test whose assertions no realistic bug could violate.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
