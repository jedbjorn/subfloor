-- 0041 — reseed CLI-hardening batch (upstream #237/#241)
--
-- `sc sql` / `sc map-sql` are now ENFORCED read-only (sqlite3 -readonly);
-- the explicit read-write passthroughs are `sc sql-rw` / `sc map-sql-rw`.
-- Four skills updated to name the command their writes run through:
--   local_skill_management — grant/assign/remove SQL runs via `./sc sql-rw`
--                            (was: SQL blocks with no named command; worked
--                            only because of the read-passthrough mislabel) (#237).
--   cartographer           — dr_* authoring writes run via `sc map-sql-rw`;
--                            reads stay on `sc map-sql` (#237).
--   app_deploy_setup       — project-local skill INSERT + grant run via
--                            `./sc sql-rw` (#237).
--   redline_review         — Step 1 hedges the missing drop dir on forks
--                            installed before install.py created
--                            shared/redlines/ (mkdir -p + check shared/ root) (#241).
--
-- 0001 is regenerated from the assets for fresh builds; this forward reseed
-- carries the same bodies to already-installed forks (UPSERT by name; skill_id
-- + grants preserved).

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'app_deploy_setup',
  'Admin-run, one-time scaffold — turn the shipped deploy template into this repo''s own project-local `deploy` skill (migration dirs, DB backup, ff-only sync, apply + move migrations, restart), then grant it to every shell.',
  'substrate',
  NULL,
  0,
  '# app_deploy_setup — scaffold this app''s deploy ritual (once, admin)

The engine deploys itself (`sc update`). The HOST APP this fork lives in has
its own deploy story — app process, app DB, app migrations — that the engine
cannot know. This skill turns the template below into the repo''s own `deploy`
skill, filled in with this app''s specifics.

**Why a NEW project-local skill instead of editing this one:** engine skills
self-heal on every `sc update` — a fork edit to any skill named in
`assets/skills/` is detected as stale and reverted to the shipped body. A
project-local skill (a name the engine doesn''t ship) is never touched by that
guard and persists through rebuilds via `sc snapshot` → `.sc-state/content.sql`.
Fill in the template, save it under a NEW name, leave this scaffold as shipped.

## 1. Scaffold the migration dirs

```bash
mkdir -p migrations_app/pending migrations_app/completed
touch migrations_app/pending/.gitkeep migrations_app/completed/.gitkeep
```

Commit them. Rename to fit the repo''s layout if you like (`db/migrations/…`,
`deploy/migrations/…`) — keep `pending/` and `completed/` as siblings and use
the same paths in the template. These are the APP''s schema migrations —
unrelated to `.super-coder/migrations/` (engine DB, ledger-tracked, owned by
`sc update`).

## 2. Fill the template

Every `⟨ADMIN: …⟩` slot is app-specific. Get each answer from the operator or
the repo itself, and **run each command once by hand** before writing it in —
a deploy skill is no place for untested commands.

```markdown
# deploy — ⟨ADMIN: app name⟩ post-merge deploy ritual

Run from the repo root on the host. Every step aborts loudly rather than
guessing; if a step fails, stop — the app is down and the DB is backed up.

1. **Down** — stop the app:
   ⟨ADMIN: stop command — e.g. pm2 stop ecosystem.config.cjs / systemctl stop <app> / docker compose down⟩

2. **Backup** — snapshot the app DB before anything mutates:
   ⟨ADMIN: backup command + destination + how many to retain⟩

3. **Sync main** — `git switch main` (if on a branch), then `git pull --ff-only`.
   `--ff-only` aborts on a diverged or dirty main — resolve by hand; never
   merge inside a deploy.

4. **Migrate** — apply every file in `migrations_app/pending/` in sort order:
   ⟨ADMIN: apply command per file — e.g. psql "$DB_URL" -f <file> / sqlite3 <db> < <file> / alembic upgrade head⟩
   After each success: `git mv migrations_app/pending/<file> migrations_app/completed/`
   On first failure: stop, restore the backup, investigate.

5. **Record** — commit and push the moves — the move IS the applied-ledger,
   and an uncommitted move dirties main and breaks the NEXT deploy''s --ff-only:
   `git add migrations_app && git commit -m "deploy: apply <files>" && git push`

6. **Up** — restart the app:
   ⟨ADMIN: start command⟩

7. **Verify** — prove the new code is serving:
   ⟨ADMIN: health check — e.g. curl -fsS http://127.0.0.1:<port>/health⟩
```

## 3. Save it as a project-local skill

Both SQL blocks below run via `./sc sql-rw "<SQL>"` — the explicit read-write
passthrough (`sc sql` is read-only and refuses writes).

```sql
INSERT INTO skills (name, description, category, content, common)
VALUES (''deploy'',
        ''Post-merge deploy ritual for this app — down, backup, ff-only sync, migrate pending→completed, restart, verify.'',
        ''substrate'',
        ''<the filled template>'',
        1);
```

`common=1` is the "grant to every shell" switch: new shells receive it at
creation, and `sc update` re-grants every common skill to every live shell.
Grant existing shells now without waiting for an update:

```sql
INSERT OR IGNORE INTO shell_skills (shell_id, skill_id)
SELECT s.shell_id, k.skill_id FROM shells s, skills k
WHERE COALESCE(s.is_deleted,0)=0 AND k.name=''deploy'' AND k.is_deleted=0;
```

Then persist: `sc snapshot` (project-local skills + grants live in
`.sc-state/content.sql`).

## 4. Optional make surface

If the operator wants make muscle-memory, add a bare `deploy` target to the
**repo''s own root Makefile** — that is the fork''s convention space. Do NOT add
it to `.super-coder/aliases.mk`: that file is engine-owned, every target must
delegate to `./sc`, and the engine knows nothing about the app.

## 5. Done

Dry-run the ritual once on a quiet window end-to-end. This scaffold stays
granted to admin only; the finished `deploy` skill is the one every shell
carries.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

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
map. Every `dr_*` read below runs against it: `sc map-sql "…"` (read-only).
The authoring writes in this skill (UPDATE/INSERT/DELETE on `dr_*`) run via
`sc map-sql-rw "…"` — the explicit read-write passthrough; `sc map-sql`
refuses writes.
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
  'local_skill_management',
  'Create, persist, assign, and remove fork-specific skills — the correct authoring path so skills survive snapshot/rebuild cycles.',
  'substrate',
  NULL,
  0,
  '# local_skill_management — fork-specific skills that survive

Fork-specific skills live in the DB and are persisted via `.sc-state/content.sql`
(the snapshot). The asset file under `.super-coder/assets/skills/` is used to
**seed the skill initially** — but that directory is gitignored engine territory:
`sc update` materializes upstream engine files there, which removes any local
additions. After the first seed + snapshot, **content.sql is the durable form**.

The correct path: **file → seed → grant → snapshot → commit**.

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

2. **Seed the skill into the DB.**
   ```bash
   cd <repo> && sc seed-skills
   ```
   UPSERTs the skill row into the live DB by name (id-stable). Does not touch
   skills already in the DB that are absent from assets — those are other local
   skills, left intact.

3. **Grant the skill to the target shell(s).**
   Skill grants have no API surface — these writes run via `./sc sql-rw "<SQL>"`
   (the explicit read-write passthrough; `sc sql` is read-only and will refuse).
   Find shell IDs:
   ```sql
   SELECT shell_id, display_name, flavor FROM shells WHERE is_deleted = 0;
   ```
   Grant:
   ```sql
   INSERT OR IGNORE INTO shell_skills (shell_id, skill_id)
   SELECT <shell_id>, skill_id FROM skills
   WHERE name = ''<skill_name>'' AND is_deleted = 0;
   ```

4. **Snapshot — this is the persistence step.**
   ```bash
   sc snapshot && sc render
   ```
   `snapshot.py` serializes local skills (any skill whose name is not in the
   upstream engine assets) into `.sc-state/content.sql`. This is what survives
   `sc update` — when the engine materialize overwrites `.super-coder/assets/
   skills/`, the skill row and its full content are reconstructed from
   content.sql on rebuild. Without this step the skill is lost on next update.

5. **Commit.**
   Run `sc render-check` first — it rebuilds hermetically and fails if the
   `skills_sc/` mirror drifts from the DB render (the same CI guard; see the
   `snapshot` skill). Then stage `.sc-state/content.sql` and `skills_sc/`
   together — the snapshot without the re-rendered mirror is the drift. The asset
   file and `0001_seed_skills.sql` are transient for local skills — don''t rely on
   them across updates.

## Assigning an existing skill to additional shells

Via `./sc sql-rw "<SQL>"`:
```sql
INSERT OR IGNORE INTO shell_skills (shell_id, skill_id)
SELECT <shell_id>, skill_id FROM skills
WHERE name = ''<skill_name>'' AND is_deleted = 0;
```
Then `sc snapshot && sc render` and commit.

## Removing a skill

1. **Soft-delete the DB row and revoke grants.**
   Via `./sc sql-rw "<SQL>"`:
   ```sql
   UPDATE skills SET is_deleted = 1 WHERE name = ''<name>'';
   DELETE FROM shell_skills
   WHERE skill_id = (SELECT skill_id FROM skills WHERE name = ''<name>'');
   ```

2. **Snapshot, render, commit.**
   ```bash
   sc snapshot && sc render
   ```
   The deletion serializes to content.sql. If the asset file still exists under
   `.super-coder/assets/skills/`, remove it too so `sc seed-skills` doesn''t
   re-insert it.

## How the GUI organizes skills

The review GUI has a **Skills tab**: the full catalogue in sections, with
per-shell grant toggles on every skill. The Shells tab groups its grant list
by the same sections.

- **Repo skills** — the lead section: skills authored in this fork. Membership
  is *derived*, not declared — a skill whose name has no
  `.super-coder/assets/skills/<name>/SKILL.md` is repo-local. This is the same
  rule snapshot.py uses to decide what serializes into `.sc-state/content.sql`,
  so the section shows exactly what the snapshot keeps durable. No frontmatter
  flag exists or is needed.
- **Substrate / Craft / …** — engine skills, sectioned by their `category`
  frontmatter. A repo skill''s `category` still displays as a label on its row,
  but never moves it out of the Repo section.
- One transient caveat: while a repo skill''s asset file still sits under
  `assets/skills/` (between authoring and the next `sc update` materialize),
  the derivation reads it as engine — it appears under its category section
  until the update wipes the asset. Harmless; the DB row is the durable thing.

Grant toggles in the GUI hit the same DB table as the SQL in this skill —
they still need a **snapshot** (header button or `sc snapshot`) to survive
a rebuild.

## What NOT to do

- **Never skip the snapshot after creating a skill.** The asset file under
  `.super-coder/assets/skills/` is overwritten by `sc update`. If you seed
  without snapshotting, the skill vanishes on the next engine update.
- **Never edit `0001_seed_skills.sql` by hand.** It is generated; hand edits
  are overwritten on the next `sc seed-skills`.
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
  'redline_review',
  'Review PNG redlines from the shared scratch dir — find the image by filename match, describe what is seen, interpret intent, propose an implementation, then hold for approval before writing code. Use when the FnB says "redlines".',
  'craft',
  NULL,
  0,
  '# redline_review — read a redline before you build it

A redline is a marked-up screenshot the FnB drops in the repo''s shared scratch
dir (`<repo>/shared/redlines/`) to communicate a change visually. This skill is
the discipline for turning that image into an approved plan **before** any code.

**Trigger:** the FnB says "redlines" (with or without specific context).

## Steps

1. **Find the image**
   - List `shared/redlines/`. If the dir doesn''t exist (fork installed before
     the engine created it), `mkdir -p <repo>/shared/redlines` and check
     `shared/` root — earlier drops land there.
   - Match a filename to the prompt context (fuzzy / keyword).
   - One file present and no strong mismatch → use it. Multiple → pick the best
     filename match; if it''s genuinely ambiguous, surface that rather than guess.

2. **Read the image** — use the Read tool to load the PNG visually.

3. **Report in three parts — skip none:**
   - **What I see:** literal description — layout, labels, UI elements,
     annotations, the markup itself.
   - **What I understand:** the interpreted intent — the change or requirement
     this redline is communicating.
   - **What I propose:** a concrete implementation plan — files, components,
     approach.

4. **Hold** — do not write or execute any code until the FnB explicitly approves
   the proposal.

5. **After resolution is confirmed** — once the FnB confirms the redline is
   resolved, delete the source `.png` from `shared/redlines/`. Delete only on
   explicit confirmation, never on assumed completion.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
