-- 0088 — forward-reseed artifact-policy-aware skill procedures.
-- Keeps existing installations and hermetic fresh builds aligned with
-- assets/skills after tracked/local artifact mode was introduced.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'cartographer',
  'Own the repo map — configure mapping to THIS repo, wire the auto-remap git hooks, heal both on drift. Cartographer-only; no working shell maps. Run on first boot + whenever the map looks wrong.',
  'substrate',
  'sc map-setup',
  0,
  '# cartographer — own the repo map so no other shell has to

Working shells consume the `dr_*` catalogue (`surface_catalogue`) and never
map. You alone do three things: **configure** how this repo is mapped, **wire**
the automation that keeps it fresh, **heal** both on drift.

Map db = `.sc-state/map.db` in tracked mode or
`.sc-state/local/map/map.db` in local mode, separate from the engine memory db
(`shell_db.db`) so an engine schema change never touches the map. Reads: `sc
map-sql "…"`. Authoring writes (UPDATE/INSERT/DELETE on `dr_*`): `sc
map-sql-rw "…"` — `sc map-sql` refuses writes. Authored sections serialize to
`.sc-state/map_content.sql` (tracked mode) or
`.sc-state/local/map/content.sql` (local mode) on snapshot (admin/GUI step — see Standing jobs)
and reload on a fresh map db.

`<self>` = your `shell_id` (ACTIVE SESSION block).

## Freshness machinery — what you own

- **Git hooks** `post-merge` / `post-checkout` / `post-rewrite` re-run `sc map`
  on every pull / branch-switch / rebase. Tracked in `.super-coder/hooks/`,
  fired via `core.hooksPath` — per-clone, unset until `sc map-setup` wires it.
- **`sc rebuild`** re-maps (map = derived cache) -> a fresh rebuild never
  leaves an empty map.
- **Hourly cron** — pm2 runs `sc-map-<repo>` on `cron_restart`
  (`.super-coder/ecosystem.config.cjs`) while the stack is up (`sc up`);
  catches uncommitted local restructuring the git hooks can''t see. Verify:
  `pm2 list | grep sc-map` — state cycling stopped→online per tick = the
  one-shot pattern, not a crash. A fork without pm2 has no cron; the hooks
  still cover it, and manual `sc map` always works.
- **You** — per-repo config + hook wiring + extractors + repair of all three.

## First boot — configure mapping for THIS repo

1. **Inspect.** Read the current map + tree:
   ```sql
   SELECT name, default_branch, file_count, mapped_at FROM dr_repo;
   SELECT lang, COUNT(*) n FROM dr_filepath WHERE lang IS NOT NULL GROUP BY lang ORDER BY n DESC;
   SELECT role, COUNT(*) n FROM dr_filepath GROUP BY role ORDER BY n DESC;
   ```
   Eyeball the top-level dirs -> anything mis-classified, or a
   generated/vendored dir being indexed?

2. **Author the active map config** — `.sc-state/map.config.json` in tracked
   mode or `.sc-state/local/map/config.json` in local mode. It is per-instance
   and survives `sc update`. All keys optional; each merges over `map_repo.py`
   defaults:
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
   - `skip_dirs` / `skip_files` — ADDED to the defaults; never shrink them.
   - `role_overrides` — applied after default role inference, first match
     wins. `prefix` matches the repo-relative path; `glob` matches the filename.
   Add only what the defaults get wrong — empty/absent config is fine for a
   plain repo.

3. **Wire + map:** `sc map-setup` -> `core.hooksPath` points at
   `.super-coder/hooks/`, hooks executable, initial map run.

4. **Verify the wiring, not just the files:**
   ```sh
   git config --get core.hooksPath      # → .super-coder/hooks
   ls -l .super-coder/hooks             # all three, executable
   ```
   ```sql
   SELECT file_count, mapped_at FROM dr_repo;   -- non-zero, just now
   ```
   Spot-check overrides took:
   `SELECT path, role FROM dr_filepath WHERE path LIKE ''cmd/%'';`

5. **Describe all NULLs** — run the description worklist (Standing jobs § 2);
   leave only when it returns zero rows.

6. **Commit** the config + hooks (`git` skill) -> `sc mem state "…"` ->
   `sc mem oriented` (sets `bootstrapped=1` — the write is live in the
   shared DB; it does NOT snapshot).

## Heal — run whenever the map looks wrong

Triggers: repo restructured / new language or dir / files mis-roled / map
stale or empty on a clone whose hooks never got wired.

1. Re-inspect (step 1) — what changed?
2. Edit `.sc-state/map.config.json` to match (step 2).
3. `sc map-setup` (idempotent) — re-wires hooks + re-maps.
4. Verify (step 4). Vanished paths are auto-pruned from `dr_filepath` by the
   remap.
5. **Stale sections** — `dr_section` is authored, never auto-pruned. After any
   migration/restructure run the stale-section worklist (Standing jobs § 1);
   DELETE or repath every row it returns.
6. **Describe all NULLs** (Standing jobs § 2) -> worklist empty before you
   leave.
7. Commit.

## Standing jobs — sections, descriptions, product DB

Both authored layers survive the remap (`dr_section` is never touched by the
mapper; `dr_filepath.desc` is preserved by its UPSERT); neither blocks the
auto-remap hook. Boot `## CONNECTIONS` renders the section index;
descriptions are the leaves a shell queries once narrowed to a section.

**1. Sections (`dr_section`)** — curate the navigational index. Seeded one
section per top-level dir on first map; make it *good*: rename to what shells
call the area, split coarse dirs into real areas, merge noise, write the
one-line `description`.

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

**2. Descriptions (`dr_filepath.desc`)** — per-file one-liners, ≤100 chars.
Run the worklist every session; every run ends with zero NULLs — not optional.
Queried by working shells within a chosen section (`surface_catalogue`), never
bulk-loaded at boot.

```sql
-- WORKLIST — undescribed files, most-load-bearing first:
SELECT path, role FROM dr_filepath WHERE desc IS NULL ORDER BY role, path;

-- describe (≤100 chars; preserved across the next auto-remap):
UPDATE dr_filepath SET desc=''Boot composer — assembles CLAUDE.md from DB state'' WHERE path=''.super-coder/render/compose.py'';
```

**3. Product DB** — the app''s own database, separate from engine memory
(`.super-coder/shell_db.db`); working shells change them in completely
different ways (boot `## DATABASES`), and the map you author is the only
per-fork signal of where the app DB lives. The live `.db` is usually
gitignored (absent from the map); schema + migrations are tracked = the
durable anchor. Tag them plainly as the product/app DB so no shell mistakes
them for engine memory; give them a section if they form an area.

```sql
-- tag the product DB''s definition (the engine-vs-app split made visible):
UPDATE dr_filepath SET desc=''Product DB schema — the APP database (NOT engine memory)'' WHERE path=''<app schema file>'';
UPDATE dr_filepath SET desc=''Product DB migration — change the app schema here'' WHERE path LIKE ''<app migrations dir>/%'';
-- optional: a section if the product DB is its own area
INSERT INTO dr_section (name, path_prefix, description, sort_order)
VALUES (''App DB'', ''<db dir>/'', ''Product runtime database — schema + migrations (NOT the engine memory DB)'', 7);
```

Fork ships no database of its own -> skip.

After a curation pass your writes are already live in the shared map db —
done. NEVER run a plain `sc snapshot` from a shell — it is refused by design;
persistence = the GUI Snapshot button or an admin''s `SC_ADMIN=1 ./sc
snapshot`. Don''t chase it. (Sections are snapshotted; descriptions ride the
live DB + survive remap — refill from the worklist if a rebuild drops them.)

## Extending the map — semantic extractors

The engine maps the generic 80% (files, languages, roles, deps, env).
Semantic dimensions — HTTP endpoints (`dr_endpoint`), app DB schema
(`dr_db_table`/`dr_db_column`), UI routes/components
(`dr_route`/`dr_component`) — vary by stack: you extract them via drop-in
Python modules in `.sc-state/map_extractors/*.py`, discovered + run by
`sc map` after the core pass. Fork-owned (outside the gitignored engine dir ->
`sc update` never clobbers them); table *columns* are standardized in the
engine (`map_schema.sql`) so working-shell queries have a stable shape
everywhere.

Adopt one per stack:

1. **Detect the stack:** `SELECT manager, name FROM dr_dependency;`
   (fastapi? flask? svelte? next?) + the file mix
   (`SELECT lang, COUNT(*) FROM dr_filepath GROUP BY lang`).
2. **Copy the matching reference** from the engine''s
   `.super-coder/templates/map_extractors/` into `.sc-state/map_extractors/`:
   - `fastapi_endpoints.py` — decorator routes (`@app.get(...)`, Flask `@app.route`) → `dr_endpoint`
   - `sqlite_schema.py` — SQL `CREATE TABLE/VIEW` → `dr_db_table`/`dr_db_column`
   - `sveltekit_routes.py` — filesystem routes + `*.svelte` → `dr_route`/`dr_component`
   Adapt the `framework` label + file filter to this repo. Uncovered stack
   (Django URLs, Express, Spring, Rails) -> copy the closest as a skeleton,
   rewrite the match — target the dominant pattern, not 100%.
3. **Run + verify:** `sc map` -> table populated, rows look right
   (`SELECT method, path FROM dr_endpoint LIMIT 10;`).
4. **Commit** `.sc-state/map_extractors/`. (Snapshotting the authored layer =
   the admin/GUI step above — not yours to run.)

**Contract** (full version: `templates/map_extractors/README.md`): each module
defines `extract(con, repo_root, cfg) -> str`. `con` = the live map db with
`dr_filepath` already populated — query it for inputs. DELETE + repopulate
only your own `dr_*` table(s); return a one-line summary for the map log.
NEVER assume a file parses — guard yourself even though `map_repo` guards each
extractor. Static extraction is best-effort: log what you skip (dynamic
routes, computed paths); never claim full coverage.

## Shape-change notices — the curation trigger

The hooks keep the mechanical catalogue fresh, but a newly-landed file arrives
`desc IS NULL` and unsectioned. Working shells message you on shape change so
curation is a timely push, not a next-boot pull — the only inbox traffic you
act on as cartographer.

**Notice contract** (one source of truth — the relay skills point here).
Sender = the **dev/coder** shell on merge (feature landed, doc written); NOT
the planner — specs render into a known area and need no curation. Sent via
the `messaging` skill to `cartographer` — a role alias the API resolves to
this fork''s cartographer shell whatever its actual shortname:

```
--message send cartographer "shape: <what landed> — paths: <region/>; <ref>. curate."
```

Body names **what** changed + **where** (the path region) so your pass is
scoped, not a full re-survey. A `documents`/feature ref is optional.

**On a notice** — check inbox -> run the worklists scoped to the named
region -> mark read:

```sql
-- 1. the new files this notice is about (scope by the region it named):
SELECT path, role FROM dr_filepath
WHERE desc IS NULL AND path LIKE ''region/%'' ORDER BY role, path;
-- 2. describe them (≤100 chars) — UPDATE dr_filepath SET desc=… per the worklist above.
-- 3. do they form / join a section? curate dr_section if the region is a new area.
```

Then `--message mark-read <id>` (`messaging` skill). The mechanical remap
already ran via the hook; your job on the notice = the authored layer only —
describe the new leaves, section a new area. `desc IS NULL` already narrows to
exactly the uncurated tail.

## Stance

- The map is infrastructure, not a chore for every shell. A working shell
  hunting the tree for something the map should know = heal the map; do not
  teach that shell to map.
- Config is the lever: tune `map.config.json`; touch `map_repo.py` only when
  the mechanism itself (a parser, a role kind) is wrong.
- Verify the automation, not just the file: a written hook that
  `core.hooksPath` doesn''t point at does nothing -> check the wiring after
  every setup.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'git',
  'Git conventions for a super-coder shell — one repo, one cwd. Sync the base before work, branch before committing, open PRs (never merge without the FnB''s OK), attribute commits per-shell. Use before any git work.',
  'substrate',
  NULL,
  0,
  '# git — version control, the super-coder way

One repo at its root -> plain `git` (cwd = repo root) is safe.

Project = this repo minus `.super-coder/`. Engine = `.super-coder/` — gitignored, materialized by `sc update`, authored upstream in super-coder. NEVER commit or edit anything under `.super-coder/`.

## Sync before you start — hard pre-code gate

Run the gate every session + before each new unit of work. `shell/<shortname>` = a moving base pinned to `origin/main`, not a content branch — cut feature branches from it. A stale base -> you read code that no longer exists + your PRs conflict on arrival.

The launcher auto-syncs at boot when provably nothing can be lost (on base branch + clean tree + no local-only commits). Read the `sync:` line in ACTIVE SESSION: auto-synced + nothing done since -> current, carry on. Says **NOT auto-synced** / you''re mid-session about to start new work -> run:

1. `git fetch origin main && git rev-list --count HEAD..origin/main` -> 0 = carry on.
2. Behind -> take stock BEFORE touching anything: `git status` (uncommitted) + `git rev-list origin/main..HEAD` (unmerged commits) + `git branch --no-merged origin/main` (unlanded branches).
3. Anything local -> surface to the FnB first: list the commits/files, ask land / stash / discard. No sync without their call (soft gate).
4. Clean (or FnB said go) -> `git checkout shell/<shortname> && git reset --hard origin/main`. NEVER `git pull`/merge on the base — merge bubbles accumulate + your squash-merged work replays as conflicts.
5. Reset only the base, never a feature branch. Stale feature branch -> `git rebase origin/main`.

## Branch -> commit -> push -> PR -> stop

1. NEVER commit to the default branch. Branch first: `git checkout -b <type>/<short-desc>` (feat/fix/chore/docs). *Admin-shell exception:* it boots at the repo root on `main`, exempt from the branch-guard; committing to main is its mandate (engine updates, migrations, approved patches) and it starts each session with `git pull --ff-only`. Every other shell branches, always.
2. Commit in logical units. End every message with your shell''s trailer:
   ```
   Co-Authored-By: <shell display_name> (super-coder) <noreply@…>
   ```
3. Push -> open a PR -> stop. Do NOT merge without an explicit FnB directive — opening is the default, merging is a separate gate.

## Merging a stack (only when the FnB hands you one)

Merge bottom-up, retargeting before each merge — never rely on GitHub''s auto-retarget:

1. `gh pr view <n> --json mergeable,mergeStateStatus` -> clean.
2. `gh pr merge <low> --squash --delete-branch`.
3. BEFORE the next merge: `gh pr edit <next> --base main` — deleting the merged base otherwise orphans the PR above it (GitHub closes it `CONFLICTING`, base ref gone).
4. Re-check `MERGEABLE` -> merge. Repeat up the stack.

PR already orphaned (base deleted under it) -> the head branch still holds the commits; reopen the SAME PR, don''t rebuild:

1. `git push origin <merged-sha>:refs/heads/<deleted-branch>` — `<merged-sha>` = `gh pr view <merged-pr> --json headRefOid`.
2. `gh pr reopen <closed-pr>` -> `gh pr edit <closed-pr> --base main`.
3. Verify `MERGEABLE` -> delete the recreated branch again.

## Finish before you stop

Bookend to the sync gate. At end of session: `git status` (uncommitted) + `git rev-list origin/<base>..HEAD` (unpushed) -> resolve every hit:

1. Real work -> commit (attributed, trailer above) + push + open the PR. Don''t skip because the session is ending.
2. Throwaway / experiment -> discard deliberately: `git restore` / `git stash`.
3. Genuinely unsure -> surface to the FnB + leave it committed-and-pushed on a branch — never sitting uncommitted.

Pass = tree clean, or on a pushed branch with a PR. A dirty/unpushed tree forces the admin''s `git_cleanup` to map attribution, check liveness, and commit on your behalf.

## After a merge — clean up local

Only after the PR is merged:

1. Re-pin the base. In a worktree `git checkout main` fails (main is checked out at the repo root; git refuses a branch checked out elsewhere) -> `git checkout shell/<shortname> && git fetch origin && git reset --hard origin/main`. Admin at repo root: `git pull --ff-only` on main.
2. `git branch -d <branch>`. Squash-merged -> `-d` refuses (commits aren''t ancestors of main); confirm the PR shows *merged* on the remote -> `git branch -D <branch>`.
3. `git fetch --prune`.

NEVER delete a branch carrying unmerged, un-PR''d work — no PR = lost work.

## Never commit the engine or derived files

- `/.super-coder/` is gitignored — never force-add anything under it.
- Gitignored + regenerated, never commit: `CLAUDE.md`, `AGENTS.md`, `opencode.json`, `.claude/skills/`, `.sc-state/engine.ref.prev` (ephemeral rollback pointer).
- From a worktree, commit only your project''s own files. Do NOT hand-commit `.sc-state/content.sql` (serialized DB memory), `.sc-state/engine.ref` (engine pin), or the tracked `_sc` renders — `sc` writes them to the main checkout root, so they aren''t in your worktree to stage. They enter the repo via Publish (below).
- If `artifact_mode=local`, snapshot/render outputs live under ignored
  `.sc-state/local/`; Publish persists them without creating a Git commit or PR.
- Exception: in the super-coder SOURCE repo, `schema.sql` + `migrations/` are tracked — there the engine *is* the project.

## After DB work — `sc mem` is already saved; Publish is separate

An `sc mem` write lands in the shared engine DB immediately (visible to every shell) and `sc rebuild` restores it from the serialized snapshot — there is no per-shell save step. NEVER run `sc snapshot` from a worktree — it refuses by design (`snapshot: refused — serializing to the shared main tree is an admin/GUI step`).

Getting DB text into the repo = the Publish flow (snapshot -> render -> commit -> push -> PR on `sc_gui_content`): the GUI **Publish** button, or the admin shell on `main` running `SC_ADMIN=1 sc snapshot` (+ `SC_ADMIN=1 sc render` if docs/roadmap/skills changed). Output lands at the main checkout root, NOT your worktree — don''t try to commit `content.sql` or `_sc` renders onto your branch. Feature-branch PRs carry project files; DB content publishes separately. See the `snapshot` skill.

That paragraph is tracked-mode behavior. In local mode the same snapshot/render
commands remain the durability step, but no content publication is attempted.

## Notes

- Before destructive ops, confirm the repo — `git -C <abs-path>` if ever in doubt.
- Multi-shell: each shell boots into its own worktree at `.sc-worktrees/<shortname>/` on branch `shell/<shortname>`; the launcher keeps the base pinned to `origin/main` (see the sync gate). Worktree isolation is automatic — no shared cwd. Admin shell = the one exception: repo root on `main`.
- UI preview: worktree edits do NOT show on the fork''s main dev server. `sc preview` (start once from the main checkout if not running) serves every shell''s worktree UI live (HMR) on the fork''s `dev_port`, one subdomain each: `http://<shortname>.localhost:<dev_port>/`. The `post-commit` hook prints your URL after each commit — surface that line to the FnB.',
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

Fork-specific skills live in the DB and persist via `.sc-state/content.sql`
(the snapshot). The asset file under `.super-coder/assets/skills/<name>/` is
the **authoring source only** — it sits in gitignored engine territory, and
that is safe: the engine/local boundary is the seed migration (0001,
upstream-owned in a fork), not asset-file presence. The snapshot serializes
your skill to content.sql whether or not the asset file is kept, and
`sc update` neither manifests it nor heals over its DB row. **content.sql =
the durable form; the asset file = your editor.**

The path: **file -> seed -> grant -> snapshot -> commit**.

## Creating a fork-specific skill

1. **Write the skill file** at `.super-coder/assets/skills/<name>/SKILL.md`.

   Required frontmatter:
   ```yaml
   ---
   name: skill_name
   description: One-line summary — shown in boot, catalogue, and the GUI Skills tab
   category: substrate   # or craft; omit for default
   ---
   ```
   Body: markdown procedure the shell will follow. Imperative, compressed —
   this boots into context.

2. **Seed into the live DB:**
   ```bash
   sc seed-skills
   ```
   UPSERTs every asset skill by name (id-stable) and reports what landed. In a
   fork it deliberately does NOT regenerate the seed migration — that file is
   upstream-owned engine territory. DB skills with no asset file = other local
   skills, left intact.

3. **Grant to target shell(s)** — by shell id or shortname:
   ```bash
   sc skill grant <skill_name> <shell>...
   ```
   Unknown skill/shell names = hard error (no silent no-op grants).
   `sc skill list` = catalogue with origins + current grants;
   `sc skill revoke <name> <shell>...` reverses a grant.

4. **Snapshot — the persistence step:**
   ```bash
   SC_ADMIN=1 sc snapshot && SC_ADMIN=1 sc render
   ```
   `snapshot.py` serializes local skills (any skill the engine seed doesn''t
   own) into the active snapshot (`.sc-state/content.sql` in tracked mode,
   `.sc-state/local/content.sql` in local mode) — what survives `sc update` and
   `sc rebuild`; the row + grants reconstruct from content.sql. Skip this ->
   the skill is lost on next update.

5. **Finish.** Run `sc render-check` first — hermetic rebuild, fails if the
   `skills_sc/` mirror drifts from the DB render (the CI guard; see the
   `snapshot` skill). In tracked mode, stage `.sc-state/content.sql` +
   `skills_sc/` together. In local mode both stay ignored; only engine-owned
   assets/migrations are committed.

## Updating a skill

Edit the asset file -> repeat seed -> snapshot -> commit (steps 2, 4, 5).
Asset file gone (removed / authored elsewhere) -> recreate it from the DB body
first: `sc sql "SELECT content FROM skills WHERE name=''<name>''"`.

## Assigning an existing skill to additional shells

```bash
sc skill grant <skill_name> <shell>...
```
Then `SC_ADMIN=1 sc snapshot && SC_ADMIN=1 sc render` + commit.

## Removing a skill

1. **Soft-delete the row + revoke its grants:**
   ```bash
   sc skill rm <skill_name>
   ```
   Refuses engine skills — the seed resurrects those on next update/rebuild.
   Engine skill this fork has superseded -> retire fork-wide:
   `sc skill retire <name>` (writes the tracked
   `.sc-state/skills_retired.json`, which rides updates; `sc skill unretire`
   reverses). Per-shell removal -> `sc skill revoke`.

2. **Remove the asset file** (`.super-coder/assets/skills/<name>/`) —
   otherwise the next `sc seed-skills` re-inserts the skill.

3. **Snapshot, render, commit:**
   ```bash
   SC_ADMIN=1 sc snapshot && SC_ADMIN=1 sc render
   ```

## How the GUI organizes skills

The review GUI Skills tab shows the full catalogue in sections with per-shell
grant toggles; the Shells tab groups its grant list by the same sections.

- **Repo skills** — lead section: skills authored in this fork. Membership is
  *derived* — a skill the engine seed doesn''t own is repo-local. Same rule
  snapshot.py uses to decide what serializes into `.sc-state/content.sql`, so
  the section shows exactly what the snapshot keeps durable. No frontmatter
  flag exists or is needed.
- **Substrate / Craft / …** — engine skills, sectioned by `category`
  frontmatter. A repo skill''s `category` displays as a row label but never
  moves it out of the Repo section.

GUI grant toggles hit the same DB table as `sc skill grant` — they still need
a snapshot (header button or `SC_ADMIN=1 sc snapshot`) to survive a rebuild.

## What NOT to do

- **NEVER skip the snapshot after creating a skill.** Seeding writes the live
  DB only; content.sql is what survives `sc update` and `sc rebuild`.
- **NEVER edit `0001_seed_skills.sql` by hand.** Generated, and in a fork
  upstream-owned engine territory — a local edit blocks the next update.
- **NEVER create skills via the GUI.** Toggling grants there is fine (snapshot
  after); creating is not — the GUI writes only the DB and cannot write the
  asset file or seed it. Use this procedure.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'self_update',
  'Update this fork''s super-coder engine in place — fetch + materialize new code + migrations, all memory intact; sound rollback. The shell hands off to its own next boot. Use when a super-coder update is available.',
  'substrate',
  'sc update',
  0,
  '# self_update — laying a new floor under your own feet

The local shell updates its own substrate — no external rebuild. All state lives
in the DB and engine code is read live each session, so a code-only update
touches no data; a schema change applies as an in-place migration, never a
destructive rebuild. `current_state`, narrative, decisions, flags, seed, and
L&S all carry across. This is succession for the substrate: you handing off to
you.

## When

- An engine update is available and you choose the moment — no external race.
- The running prompt + schema were read at the old boot -> reboot after the
  update; they refresh only on the far side.

## Procedure

1. **Clean tree first.** `git -C <repo> status` -> clean. Commit, PR, or
   discard any prior update''s output BEFORE running again — a fresh `sc update`
   on top of a stranded one stacks two engine bumps into one diff. Glance at
   `current_state` + make it true for now (the snapshot captures it).

2. **Run.** `sc update` — fetches the engine from the `super-coder` remote,
   materializes it into the gitignored `.super-coder/` dir (engine = dependency,
   not fork source), pins the new upstream SHA in `.sc-state/engine.ref`
   (prior saved as `engine.ref.prev`), backs up the live DB, applies pending
   migrations in place, syncs the skills catalogue, re-grants common skills,
   maps the repo, re-snapshots the live state.
   - `sc update --no-fetch` = reconcile against the current working tree
     (offline / dev); engine + `engine.ref` unchanged.
   - Missing-remote error -> `git remote add super-coder <url>`.

3. **Verify.** `sc verify` — headless boot proof: shells, memory, granted
   skills intact + schema current. Wrong count -> `sc rollback` (below).
   - Then `sc render && sc render-check` before step 5. `sc update` re-renders
     from the live DB, which can skip a change the new engine shipped (e.g. a
     skill body) — only `render-check`''s hermetic rebuild surfaces it. A red
     render-check here = a mirror to re-render + commit, NOT a stale diff to
     wave through. Pipeline + guard details: `snapshot` skill.

4. **Record the crossing.** Append a narrative entry — identity event for a
   shell that updates its own floor. Note what changed + write the handoff.

5. **Commit the full public set.**
   Stage every tracked file the update regenerated: `.sc-state/content.sql`
   (refreshed memory) + `.sc-state/engine.ref` (the pin) + the root `sc`
   dispatcher if it changed + any `_sc` renders. `sc` is the live tracked
   entrypoint — a pin-only commit leaves it and the renders stale against the
   engine just pinned, silently dropping commands the new engine ships.
   `.super-coder/` and `engine.ref.prev` are gitignored — nothing to commit
   there.
   With `artifact_mode=local`, `content.sql` and `_sc` renders stay under
   ignored `.sc-state/local/`; commit the engine pin/dispatcher and other
   genuinely public files only.
   - **Render conflict** (committing via PR while main advances):
     `content.sql` + `_sc` renders are serialized DB state and collide with a
     concurrent publisher. NEVER hand-merge serialized SQL — live DB canonical,
     renders derived. Rebase onto main, then either take main''s renders
     (re-applying just the pin + `sc`) or re-run `sc update` against the live
     DB so they regenerate clean.

6. **Reboot** the session -> boot onto the new floor.

## Rolling back a bad update

`sc rollback` = sound pair-restore. Engine code is read live and a migration
exists because new code expects the new schema — restoring only the DB strands
new code on the old schema, so rollback restores both:

1. backs up the current (post-bad-update) DB first — rollback is itself
   reversible;
2. restores the DB from the most recent pre-update backup in
   `~/db_backups/<repo-name>/` (keyed by this fork''s repo dir name — distinct
   from any `db_backups/` dir the fork''s app keeps at its repo root);
3. re-materializes the engine at `.sc-state/engine.ref.prev` + restores
   `engine.ref`.

Whole-restore, not per-step schema reversal. Only data written between update
and rollback is lost (seconds, in practice). Reboot afterwards; commit the
restored `.sc-state/` if the rolled-back floor should persist.

## The contract you rely on

Every schema change AFTER a fork exists ships as a migration file
(`migrations/NNNN_*.sql`), never an edit to `schema.sql` — a baseline edit
reaches fresh clones but never an existing fork; the migration ledger carries
the delta. Authoring engine changes: structural change -> new migration file,
additive where possible.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'snapshot',
  'Serialize DB work via sc snapshot / sc render under the instance artifact policy. Tracked mode publishes through Git; local mode persists under .sc-state/local without creating content commits.',
  'substrate',
  'sc snapshot',
  0,
  '# snapshot — serialize the DB back to text

Live `shell_db.db` = the single source of truth shared by every shell; a
`sc mem` write is durable + visible to all shells the instant it commits. The
`.db` is gitignored and reconstructs from schema, migrations, and the active
per-instance snapshot on `sc rebuild` —
an edit not yet serialized is discarded by a rebuild.

Serializing is an admin/GUI operation, NOT a per-write shell step: it writes
`.sc-state/` + the flat `_sc` mirror into the shared MAIN worktree, and from a
shell''s linked worktree it churns and collides with other shells. `sc snapshot`
and `sc render flat` refuse unless `SC_ADMIN=1` (GUI/API, `install`, `update`,
and `render-check` set it for you). A shell does not run them; its writes are
captured when admin snapshots (GUI **Publish**/Snapshot button, or
`SC_ADMIN=1 sc snapshot`) before a rebuild. The rest of this skill = the
admin/GUI path.

## The three text serializations

| File(s) | What | Propagates? | Written by |
|---|---|---|---|
| `schema.sql` | the v1 baseline schema | yes (forks) | hand, rarely |
| `migrations/*.sql` | ordered schema + **system content** deltas (e.g. the skills catalogue) | yes (forks) | author / `sc seed-skills` |
| `.sc-state/content.sql` (tracked mode) or `.sc-state/local/content.sql` (local mode) | **this repo''s** per-instance content + memory — shells, seed/L&S, decisions, roadmap, documents, flags, projects, skill grants | no (instance-only) | `sc snapshot` |

The split: system content propagates via migrations; per-instance content stays
in the snapshot. Skill *bodies* = system (migration); which shell is *granted*
a skill = per-instance (snapshot).

`artifact_mode` lives in `.super-coder/instance.json` and accepts `tracked` or
`local`; downstream forks default to `tracked`. Local mode still snapshots and
renders, but writes beneath `.sc-state/local/` (ignored) and Publish creates no
Git branch, commit, or PR.

## When admin serializes (the GUI Publish button does all of this)

All commands require `SC_ADMIN=1`, run from the main checkout.

1. `SC_ADMIN=1 sc snapshot` -> dumps the per-instance tables to the active
   snapshot path. Deterministic DELETE-then-INSERT in PK order ->
   re-running is byte-identical -> clean diffs.

2. `SC_ADMIN=1 sc render` -> regenerates the flat `_sc` files
   (`specs_sc/`, `docs_sc/`, `skills_sc/`, `roadmap_sc.md`) from the DB. Run
   after changing a document body, the roadmap, or skills. Incremental —
   unchanged files not rewritten. (`.claude/skills/` rebuilds at boot and is
   gitignored — not rendered here.)

3. Verify reproducibility: `sc rebuild && sc verify` -> DB rebuilds from text
   alone, byte-for-byte.
   Before committing any `_sc` render: `sc render-check` — rebuilds the DB
   hermetically from text and fails if the committed mirror drifts from that
   render (the CI guard, run locally). A plain `sc render` reads the *live* DB,
   which can lag the source just edited (skill-catalogue trap below);
   `render-check`''s rebuild-first catches the stale mirror the live-DB render
   silently passed.

4. In tracked mode, Publish writes
   `.sc-state/content.sql`, `.sc-state/engine.ref`, and the `_sc` files to the
   main checkout root (where the shared engine + DB live), not your worktree —
   they are not yours to stage. GUI **Publish** = snapshot -> render -> commit
   -> push -> PR on `sc_gui_content`; the admin shell on `main` may commit them
   directly. In local mode it only snapshots/renders and reports that nothing
   was published. NEVER commit the `.db` or anything under the gitignored
   `.super-coder/` engine dir. (super-coder SOURCE repo only: `schema.sql` +
   `migrations/` are tracked and committed here too.)

## Authoring vs. snapshotting

- **Per-instance content** (your memory, this repo''s roadmap/docs): edit the
  DB -> `sc snapshot`. The snapshot is the canonical reproducer.
- **Skill catalogue** (system, propagates): edit
  `assets/skills/<name>/SKILL.md` -> `sc seed-skills` — upserts the live DB
  *and* (source repo only) regenerates the seed migration. Not the snapshot.
  See `seed_skills.py`.
  - Sequence: `sc seed-skills && sc render`, then `sc render-check` before
    committing. In tracked mode commit the regenerated
    `migrations/0001_seed_skills.sql` + re-rendered `skills_sc/` mirror together.
    In local mode only the migration is public; the mirror stays ignored.

Steps 1–3 = durability (a `sc rebuild` cannot lose serialized work). Step 4 =
the GUI Publish button; you rarely commit this text by hand.

## Related skills

This skill owns the render/snapshot pipeline + the `render-check` guard:

- `self_update` — `sc update` re-renders the same `_sc` files; its verify step
  runs `render-check` before committing the engine bump.
- `local_skill_management` — fork-local skills persist via `sc snapshot`; run
  `render-check` before committing the `skills_sc/` mirror.
- `migration_management` — a **content-seed** migration (skills, flavor
  defaults) changes what renders; rebuild + render + `render-check` after.
- `docs` / `spec` — document bodies live in the DB, render to `docs_sc/` /
  `specs_sc/`; authored via `sc mem doc`, serialized here.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
