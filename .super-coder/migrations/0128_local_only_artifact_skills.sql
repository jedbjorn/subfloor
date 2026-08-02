-- 0128 — generated instance artifacts are local-only.
--
-- Re-apply the seven engine skills whose current guidance retires tracked
-- snapshot/render mode and GUI Git publication. Full UPSERTs keep fresh and
-- upgraded databases byte-identical to the authored skill catalogue.

BEGIN;
INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'app_deploy_setup',
  'Admin-run, one-time scaffold — turn the shipped deploy template into this repo''s own project-local `deploy` skill (migration dirs, DB backup, ff-only sync, apply + move migrations, restart), then grant it to every shell.',
  'substrate',
  NULL,
  0,
  '# app_deploy_setup — scaffold this app''s deploy ritual (once, admin)

The engine deploys itself (`sc update`); the host app''s deploy — app process,
app DB, app migrations — is the fork''s own. Fill the template below with this
app''s specifics and save it as a NEW project-local `deploy` skill.

NEVER save the result by editing this skill: engine skills self-heal on every
`sc update` — a fork edit to any skill named in `assets/skills/` is detected
as stale and reverted to the shipped body. A project-local name (one the
engine doesn''t ship) is never touched and persists through rebuilds via
`sc snapshot` -> `.sc-state/local/content.sql`. Leave this scaffold as shipped.

## 1. Scaffold the migration dirs

```bash
mkdir -p migrations_app/pending migrations_app/completed
touch migrations_app/pending/.gitkeep migrations_app/completed/.gitkeep
```

Commit them. Renaming to fit the repo''s layout (`db/migrations/…`,
`deploy/migrations/…`) is fine -> keep `pending/` + `completed/` as siblings
and use the same paths in the template. These hold the APP''s schema
migrations — NOT `.super-coder/migrations/` (engine DB, ledger-tracked, owned
by `sc update`).

## 2. Fill the template

Every `⟨ADMIN: …⟩` slot is app-specific — get it from the operator or the
repo. Run each command once by hand before writing it in; an untested command
does not enter a deploy skill.

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

## 3. Save as a project-local skill

Persist the filled template through the `local_skill_management` path — the
ONE authoring lane for fork-local skills (#321: hand-rolled `sc sql-rw`
INSERTs leave no asset file to re-seed from and contradict that skill''s
contract in the same catalogue):

1. Write the asset file at `.super-coder/assets/skills/deploy/SKILL.md` —
   frontmatter carries the identity; body = the filled template:

   ```markdown
   ---
   name: deploy
   description: Post-merge deploy ritual for this app — down, backup, ff-only sync, migrate pending→completed, restart, verify.
   category: substrate
   common: true
   ---
   <the filled template>
   ```

   `common: true` = grant-to-every-shell: new shells receive it at creation,
   and `sc update` re-grants every common skill to every live shell.

2. Seed it into the catalogue + grant it live: `sc seed-skills` (upserts the
   asset into the DB, grants common skills to every live shell).

3. Persist: `SC_ADMIN=1 sc snapshot` → the skill + grants survive in the
   ignored local snapshot. There is no generated-content commit.

Details, updates, and removal: the `local_skill_management` skill.

## 4. Optional make surface

Operator wants make muscle-memory -> add a bare `deploy` target to the repo''s
own root Makefile (the fork''s convention space). NEVER add it to
`.super-coder/aliases.mk` — engine-owned; every target there must delegate to
`./sc`, and the engine knows nothing about the app.

## 5. Done

Dry-run the ritual end-to-end once in a quiet window -> all 7 steps pass
before any shell relies on it. This scaffold stays granted to admin only; the
finished `deploy` skill is the one every shell carries.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

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

Map db = `.sc-state/local/map/map.db`, separate from the engine memory db
(`shell_db.db`) so an engine schema change never touches the map. Reads: `sc
map-sql "…"`. Authoring writes (UPDATE/INSERT/DELETE on `dr_*`): `sc
map-sql-rw "…"` — `sc map-sql` refuses writes. Authored sections serialize to
`.sc-state/local/map/content.sql` on snapshot (admin/GUI step — see Standing jobs)
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

2. **Author the active map config at the canonical live root** —
   `$SC_ROOT/.sc-state/local/map/config.json`. The mapper
   deliberately reads the shared live checkout, not your shell worktree. It is
   per-instance and survives `sc update`. All keys optional; each merges over
   `map_repo.py` defaults:
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

5. **Describe — NULLs and filler** — run the description worklist (Standing
   jobs § 2); leave only when it returns zero rows, NULLs and filler both.

6. **Persist locally.** Hook wiring and map config are per-clone runtime state,
   never a commit. Then `sc mem state "…"` -> `sc mem oriented` (sets
   `bootstrapped=1` — the write is live in the shared DB; it does NOT snapshot).

## Heal — run whenever the map looks wrong

Triggers: repo restructured / new language or dir / files mis-roled / map
stale or empty on a clone whose hooks never got wired.

1. Re-inspect (step 1) — what changed?
2. Edit the active canonical-root config from step 2 to match.
3. `sc map-setup` (idempotent) — re-wires hooks + re-maps.
4. Verify (step 4). Vanished paths are auto-pruned from `dr_filepath` by the
   remap.
5. **Stale sections** — `dr_section` is authored, never auto-pruned. After any
   migration/restructure run the stale-section worklist (Standing jobs § 1);
   DELETE or repath every row it returns.
6. **Describe — NULLs and filler** (Standing jobs § 2) -> worklist empty
   before you leave.
7. Persist by mode as in first-boot step 6.

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

**2. Descriptions (`dr_filepath.desc`)** — per-file one-liners, ≤100 chars,
**adequate, not merely present**. A desc must say something the path does not:
what the file *does* or *holds*, never its kind restated from the name —
"Engine database migration: 0042_x.sql" is filler (non-NULL, zero information
beyond the path), and a NULL-only worklist is blind to it: one mapped repo
carried 263 such placeholders, invisible for months because every row was
non-NULL. Derive each one-liner from the file''s own docstring / frontmatter /
header comment; hand-write the few with nothing extractable. Run the worklist
every session; every run ends with zero rows — NULLs *and* filler — not
optional. Queried by working shells within a chosen section
(`surface_catalogue`), never bulk-loaded at boot.

```sql
-- WORKLIST — undescribed OR filler, most-load-bearing first. The filler clause
-- is a heuristic (desc ENDS with the filename or its stem — the "<kind
-- restated>: <name>" shape); judge each hit — and treat a desc you could have
-- written from the path alone as filler even if the query missed it:
WITH f AS (SELECT path, role, desc,
                  replace(path, rtrim(path, replace(path,''/'','''')), '''') AS base
           FROM dr_filepath),
     g AS (SELECT *, CASE WHEN instr(base,''.'') > 0
                          THEN substr(base, 1, instr(base,''.'')-1)
                          ELSE base END AS stem FROM f)
SELECT path, role, desc FROM g
WHERE desc IS NULL
   OR (length(stem) >= 5 AND (lower(substr(desc, -length(base))) = lower(base)
                           OR lower(substr(desc, -length(stem))) = lower(stem)))
ORDER BY (desc IS NULL) DESC, role, path;

-- describe (≤100 chars; preserved across the next auto-remap):
UPDATE dr_filepath SET desc=''Boot composer — assembles CLAUDE.md from DB state'' WHERE path=''.super-coder/render/compose.py'';
```

Before leaving the job, spot-read a few descs per section against the files
themselves; any desc derivable from the path alone goes back on the list.
(Deliberate uniform tags — e.g. Standing job 3''s product-DB tagging — pass the
bar: they state tenancy the path doesn''t.)

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
persistence = the GUI Snapshot button or an admin''s `SC_ADMIN=1 sc
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
   `.super-coder/templates/map_extractors/` into
   `$SC_ROOT/.sc-state/map_extractors/`:
   - `fastapi_endpoints.py` — decorator routes (`@app.get(...)`, Flask `@app.route`) → `dr_endpoint`
   - `sqlite_schema.py` — SQL `CREATE TABLE/VIEW` → `dr_db_table`/`dr_db_column`
   - `sveltekit_routes.py` — filesystem routes + `*.svelte` → `dr_route`/`dr_component`
   Adapt the `framework` label + file filter to this repo. Uncovered stack
   (Django URLs, Express, Spring, Rails) -> copy the closest as a skeleton,
   rewrite the match — target the dominant pattern, not 100%.
3. **Run + verify:** `sc map` -> table populated, rows look right
   (`SELECT method, path FROM dr_endpoint LIMIT 10;`).
4. **Hand off authored extractor code** to admin via the `messaging` skill,
   naming each changed `.sc-state/map_extractors/` path and the verification
   result. Extractor code is deliberate source; generated map DB/content stays
   local.

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
- From a worktree, commit only your project''s authored files. Generated
  snapshots and `_sc` renders live under ignored `.sc-state/local/` and never
  enter Git. `.sc-state/engine.ref` is the deliberate tracked exception: it is
  the dependency pin and is updated by `sc update`.
- Exception: in the super-coder SOURCE repo, `schema.sql` + `migrations/` are tracked — there the engine *is* the project.

## After DB work

An `sc mem` write lands in the shared engine DB immediately. The admin/API
save-local path refreshes the ignored snapshot and renders used by rebuild and
review. There is no generated-content commit or Publish PR. See `snapshot`.

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

Fork-specific skills live in the DB and persist via `.sc-state/local/content.sql`
(the snapshot). The asset file under `.super-coder/assets/skills/<name>/` is
the **authoring source only** — it sits in gitignored engine territory, and
that is safe: the engine/local boundary is the seed migration (0001,
upstream-owned in a fork), not asset-file presence. The snapshot serializes
your skill to content.sql whether or not the asset file is kept, and
`sc update` neither manifests it nor heals over its DB row. **The live DB plus
its local snapshot are durable; the asset file is your editor.**

The path: **file -> seed -> grant -> local snapshot**.

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

3. **Grant to the target pack** — name a shell by id or shortname:
   ```bash
   sc skill grant <skill_name> <shell>...
   ```
   A standard shell targets its shared flavor pack; every shell of that flavor
   receives the skill. A Bespoke shell targets only itself. To create an
   intentional one-shell assignment, create/use a Bespoke shell.
   Unknown skill/shell names = hard error (no silent no-op grants).
   `sc skill list` = catalogue with origins + current grants;
   `sc skill revoke <name> <shell>...` reverses a grant.

4. **Snapshot — the persistence step:**
   ```bash
   SC_ADMIN=1 sc snapshot && SC_ADMIN=1 sc render
   ```
   `snapshot.py` serializes local skills (any skill the engine seed doesn''t
   own) into `.sc-state/local/content.sql` — what survives `sc update` and
   `sc rebuild`; the row + flavor/Bespoke grants reconstruct from content.sql.
   Skip this -> the skill is lost on next update.

5. **Finish.** Run `sc render-check` — it fails if the local `skills_sc/`
   mirror drifts from the DB render. Snapshot and renders stay ignored; commit
   only deliberately authored engine assets/migrations in the source repo.

## Updating a skill

Edit the asset file -> repeat seed -> snapshot (steps 2, 4, 5).
Asset file gone (removed / authored elsewhere) -> recreate it from the DB body
first: `sc sql "SELECT content FROM skills WHERE name=''<name>''"`.

## Assigning an existing skill

```bash
sc skill grant <skill_name> <shell>...
```
Name one standard shell to update its whole flavor, or name a Bespoke shell to
update only that shell. Then `SC_ADMIN=1 sc snapshot && SC_ADMIN=1 sc render`
to refresh the local artifacts.

## Removing a skill

1. **Soft-delete the row + revoke its grants:**
   ```bash
   sc skill rm <skill_name>
   ```
   Refuses engine skills — the seed resurrects those on next update/rebuild.
   Engine skill this fork has superseded -> retire fork-wide:
   `sc skill retire <name>` (writes the ignored local
   `.sc-state/local/skills_retired.json`; `sc skill unretire`
   reverses). Flavor/Bespoke removal -> `sc skill revoke`.

2. **Remove the asset file** (`.super-coder/assets/skills/<name>/`) —
   otherwise the next `sc seed-skills` re-inserts the skill.

3. **Snapshot and render locally:**
   ```bash
   SC_ADMIN=1 sc snapshot && SC_ADMIN=1 sc render
   ```

## How the GUI organizes skills

Shells → Skill Assignments shows the full catalogue in sections. Each standard
flavor appears once; Bespoke shells appear individually.

- **Repo skills** — lead section: skills authored in this fork. Membership is
  *derived* — a skill the engine seed doesn''t own is repo-local. Same rule
  snapshot.py uses to decide what serializes into local `content.sql`, so
  the section shows exactly what the snapshot keeps durable. No frontmatter
  flag exists or is needed.
- **Substrate / Craft / …** — engine skills, sectioned by `category`
  frontmatter. A repo skill''s `category` displays as a row label but never
  moves it out of the Repo section.

GUI grant toggles hit the same ownership boundary as `sc skill grant`:
`flavor_skills` for standard flavors, `shell_skills` for Bespoke shells. They
still need a snapshot (header button or `SC_ADMIN=1 sc snapshot`) to survive a
rebuild.

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
  'migration_management',
  'Author and apply fork-specific DB schema migrations — naming, format, how to apply locally and verify.',
  'substrate',
  NULL,
  0,
  '# migration_management — fork-specific schema changes

Migrations live in `.super-coder/migrations/`, apply in numeric order, tracked
by the `schema_migrations` ledger table. Engine updates apply pending
migrations automatically; apply locally without a fetch via
`sc update --no-fetch`.

**Scope:** fork-specific changes — tables, columns, constraints, or
system-content seeds (skills, flavor defaults) this fork needs that will not
ship upstream. Upstream engine migrations arrive via `sc update`; no action
from you.

## Authoring a migration

1. **Find the next number:**
   ```bash
   ls .super-coder/migrations/ | sort | tail -5
   ```
   Name the file `NNNN_<slug>.sql`, NNNN = next integer zero-padded to 4
   digits (e.g. `0012`).

2. **Write the file** at `.super-coder/migrations/NNNN_<slug>.sql`:
   - Wrap in `BEGIN; ... COMMIT;`
   - Idempotent: `CREATE TABLE IF NOT EXISTS`, `INSERT OR IGNORE`,
     `CREATE INDEX IF NOT EXISTS`, `DROP TABLE IF EXISTS` before recreate
   - Comment header: migration number + intent (+ doctrine notes if relevant)
   - Structure + system content only — per-instance data (shell memory,
     grants, roadmap, flags) lives in `.sc-state/local/content.sql` via snapshot,
     never in migrations

3. **Apply locally:**
   ```bash
   sc update --no-fetch
   ```
   Skips the upstream fetch; applies all pending local migrations in order.
   Confirm it landed:
   ```sql
   SELECT * FROM schema_migrations ORDER BY applied_at DESC LIMIT 5;
   ```

4. **Verify:**
   ```bash
   sc verify
   ```
   Headless boot proof — shells, memory, and schema intact.

5. **Snapshot + commit:**
   ```bash
   SC_ADMIN=1 sc snapshot
   ```
   Commit only `migrations/NNNN_<slug>.sql`; the snapshot remains local.
   - **Content-seed migration** (seeds system content that renders — skills,
     flavor defaults) also changes the flat `_sc` mirrors, but only once the
     new rows are in the DB: after `sc update --no-fetch`, run
     `SC_ADMIN=1 sc render && sc render-check`. The `_sc` files remain ignored.
     A render against a DB predating the seed passes
     locally while CI''s hermetic rebuild goes red — the stale-mirror trap; see
     the `snapshot` skill.

## What makes a good migration

- **Additive by default.** Add columns/tables/indexes. No DROP or RENAME
  unless correcting a prior mistake; prefer a new column over renaming one
  code may reference.
- **No per-instance content.** Shell memory, skill grants, roadmap items,
  flags -> snapshot. Migrations carry structure + system content that
  propagates to all forks.
- **Comment the why** — future readers need the intent, not just the SQL.

## Rollback

No per-migration rollback. `sc rollback` restores the full DB + engine to the
prior update point (`engine.ref.prev`). Use only when a migration is so broken
the DB is corrupt or the app won''t start; for logical errors, write a
corrective migration instead.',
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
     render-check here = a local mirror to regenerate. Pipeline + guard details:
     `snapshot` skill.

4. **Record the crossing.** Append a narrative entry — identity event for a
   shell that updates its own floor. Note what changed + write the handoff.

5. **Commit only the public update.**
   Stage `.sc-state/engine.ref` (the pin), the root `sc` dispatcher if it
   changed, and other deliberately authored public files. Snapshot SQL and
   `_sc` renders remain ignored beneath `.sc-state/local/`; never force-add
   them. `.super-coder/` and `engine.ref.prev` are also gitignored in forks.

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
  'Refresh the gitignored local DB snapshot and flat renders. Generated instance state never enters Git.',
  'substrate',
  'sc snapshot',
  0,
  '# snapshot — serialize the DB back to text

Live `shell_db.db` = the single source of truth shared by every shell; a
`sc mem` write is durable + visible to all shells the instant it commits. The
`.db` is gitignored and reconstructs from schema, migrations, and
`.sc-state/local/content.sql` on `sc rebuild` —
an edit not yet serialized is discarded by a rebuild.

Serializing is an admin/GUI operation, NOT a per-write shell step: it writes
the shared instance''s gitignored local cache. `sc snapshot`
and `sc render flat` refuse unless `SC_ADMIN=1` (GUI/API, `install`, `update`,
and `render-check` set it for you). A shell does not run them; its writes are
captured when admin saves locally (GUI **Save locally** button, or
`SC_ADMIN=1 sc snapshot`) before a rebuild. The rest of this skill = the
admin/GUI path.

## The three text serializations

| File(s) | What | Propagates? | Written by |
|---|---|---|---|
| `schema.sql` | the v1 baseline schema | yes (forks) | hand, rarely |
| `migrations/*.sql` | ordered schema + **system content** deltas (e.g. the skills catalogue) | yes (forks) | author / `sc seed-skills` |
| `.sc-state/local/content.sql` | **this repo''s** per-instance content + memory — shells, seed/L&S, decisions, roadmap, documents, flags, projects, skill grants | no (instance-only, gitignored) | `sc snapshot` |

The split: system content propagates via migrations; per-instance content stays
in the snapshot. Skill *bodies* = system (migration); which shell is *granted*
a skill = per-instance (snapshot).

Generated artifacts always live beneath `.sc-state/local/`. A legacy
`artifact_mode: tracked` setting is accepted only as upgrade input and resolves
to local; mode switching and Git publication are retired.

## When admin serializes

All commands require `SC_ADMIN=1`, run from the main checkout.

1. `SC_ADMIN=1 sc snapshot` -> dumps the per-instance tables to the active
   local snapshot path. Deterministic DELETE-then-INSERT in PK order makes
   re-running byte-identical.

2. `SC_ADMIN=1 sc render` -> regenerates the flat `_sc` files
   (`renders/specs_sc/`, `renders/docs_sc/`, `renders/skills_sc/`,
   `renders/roadmap_sc.md`) beneath `.sc-state/local/`. Run
   after changing a document body, the roadmap, or skills. Incremental —
   unchanged files not rewritten. (`.claude/skills/` rebuilds at boot and is
   gitignored — not rendered here.)

3. Verify reproducibility: `sc rebuild && sc verify` -> DB rebuilds from local text
   alone, byte-for-byte.
   `sc render-check` rebuilds the DB hermetically from text and fails if the
   local mirror drifts from that render. A plain `sc render` reads the *live* DB,
   which can lag the source just edited (skill-catalogue trap below);
   `render-check`''s rebuild-first catches the stale mirror the live-DB render
   silently passed.

4. Do not stage the output. Generated snapshots and renders are gitignored.
   Only authored engine source and explicit migrations belong in Git.

## Authoring vs. snapshotting

- **Per-instance content** (your memory, this repo''s roadmap/docs): edit the
  DB -> `sc snapshot`. The local DB is primary; the ignored snapshot is its
  rebuild source.
- **Skill catalogue** (system, propagates): edit
  `assets/skills/<name>/SKILL.md` -> `sc seed-skills` — upserts the live DB
  *and* (source repo only) regenerates the seed migration. Not the snapshot.
  See `seed_skills.py`.
  - Sequence: `sc seed-skills && sc render`, then `sc render-check`. Commit the
    regenerated `migrations/0001_seed_skills.sql`; the mirror stays ignored.

Steps 1–3 are the local durability path. There is no generated-artifact
publication path.

## Related skills

This skill owns the render/snapshot pipeline + the `render-check` guard:

- `self_update` — `sc update` refreshes the same local `_sc` files.
- `local_skill_management` — fork-local skills persist via the local snapshot.
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

