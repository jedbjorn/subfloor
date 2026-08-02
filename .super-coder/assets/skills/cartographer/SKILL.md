---
name: cartographer
description: Own the repo map — configure mapping of the work surface (the declared work_repo when this install names one, else this repo), wire the auto-remap git hooks, heal both on drift. Cartographer-only; no working shell maps. Run on first boot + whenever the map looks wrong.
category: substrate
command: sc map-setup
common: false
---

# cartographer — own the repo map so no other shell has to

Working shells consume the `dr_*` catalogue (`surface_catalogue`) and never
map. You alone do three things: **configure** how the mapped repo is scanned,
**wire** the automation that keeps it fresh, **heal** both on drift.

## Mapping target — work repo vs home repo

The map scans the shells' WORK SURFACE. When `instance.json` declares a
`work_repo`, that repo is what `sc map` scans and what every `dr_*` row
describes; otherwise it is this repo. Verify after any remap:
`sc map-sql "SELECT root FROM dr_repo"` -> must print the work-repo path.

The map DB, `map.config.json`, and serialized sections stay in the HOME
repo's `.sc-state/` regardless. NEVER write cache files, wire hooks, or set
`core.hooksPath` in the work repo — it may run its own substrate with its own
hooks (subfloor does).

Freshness caveat in work-repo mode: the auto-remap git hooks and cron watch
the HOME clone, so a work-repo sync does NOT auto-remap. After the work repo
moves (pull, merge, branch switch), re-run `sc map` yourself — treat a
work-repo sync message from any shell as a remap trigger.

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
  catches uncommitted local restructuring the git hooks can't see. Verify:
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
   `SELECT path, role FROM dr_filepath WHERE path LIKE 'cmd/%';`

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
       (SELECT COUNT(*) FROM dr_filepath f WHERE f.path LIKE s.path_prefix || '%') n
FROM dr_section s ORDER BY s.sort_order, s.name;

-- split / rename / describe (authored — survives the remap, snapshotted):
UPDATE dr_section SET name='API', path_prefix='shell_core/api/', description='FastAPI routers' WHERE name='shell_core';
INSERT INTO dr_section (name, path_prefix, description, sort_order)
VALUES ('UI', 'shell_core/ui/', 'SvelteKit substrate UI', 5);

-- WORKLIST — keep the catch-all empty. Files under no section = a new area to
-- section (they render under "other / unsectioned" in CONNECTIONS until you do):
SELECT path FROM dr_filepath f WHERE NOT EXISTS
  (SELECT 1 FROM dr_section s WHERE f.path LIKE s.path_prefix || '%')
ORDER BY path;

-- STALE SECTIONS (run after any migration or restructure — dr_filepath pruning
-- is automatic; dr_section is authored and never auto-pruned):
SELECT s.name, s.path_prefix, s.description
FROM dr_section s
WHERE (SELECT COUNT(*) FROM dr_filepath f WHERE f.path LIKE s.path_prefix || '%') = 0
ORDER BY s.name;
-- For each row: DELETE (area gone) or UPDATE path_prefix (area renamed).
```

**2. Descriptions (`dr_filepath.desc`)** — per-file one-liners, ≤100 chars,
**adequate, not merely present**. A desc must say something the path does not:
what the file *does* or *holds*, never its kind restated from the name —
"Engine database migration: 0042_x.sql" is filler (non-NULL, zero information
beyond the path), and a NULL-only worklist is blind to it: one mapped repo
carried 263 such placeholders, invisible for months because every row was
non-NULL. Derive each one-liner from the file's own docstring / frontmatter /
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
                  replace(path, rtrim(path, replace(path,'/','')), '') AS base
           FROM dr_filepath),
     g AS (SELECT *, CASE WHEN instr(base,'.') > 0
                          THEN substr(base, 1, instr(base,'.')-1)
                          ELSE base END AS stem FROM f)
SELECT path, role, desc FROM g
WHERE desc IS NULL
   OR (length(stem) >= 5 AND (lower(substr(desc, -length(base))) = lower(base)
                           OR lower(substr(desc, -length(stem))) = lower(stem)))
ORDER BY (desc IS NULL) DESC, role, path;

-- describe (≤100 chars; preserved across the next auto-remap):
UPDATE dr_filepath SET desc='Boot composer — assembles CLAUDE.md from DB state' WHERE path='.super-coder/render/compose.py';
```

Before leaving the job, spot-read a few descs per section against the files
themselves; any desc derivable from the path alone goes back on the list.
(Deliberate uniform tags — e.g. Standing job 3's product-DB tagging — pass the
bar: they state tenancy the path doesn't.)

**3. Product DB** — the app's own database, separate from engine memory
(`.super-coder/shell_db.db`); working shells change them in completely
different ways (boot `## DATABASES`), and the map you author is the only
per-fork signal of where the app DB lives. The live `.db` is usually
gitignored (absent from the map); schema + migrations are tracked = the
durable anchor. Tag them plainly as the product/app DB so no shell mistakes
them for engine memory; give them a section if they form an area.

```sql
-- tag the product DB's definition (the engine-vs-app split made visible):
UPDATE dr_filepath SET desc='Product DB schema — the APP database (NOT engine memory)' WHERE path='<app schema file>';
UPDATE dr_filepath SET desc='Product DB migration — change the app schema here' WHERE path LIKE '<app migrations dir>/%';
-- optional: a section if the product DB is its own area
INSERT INTO dr_section (name, path_prefix, description, sort_order)
VALUES ('App DB', '<db dir>/', 'Product runtime database — schema + migrations (NOT the engine memory DB)', 7);
```

Fork ships no database of its own -> skip.

After a curation pass your writes are already live in the shared map db —
done. NEVER run a plain `sc snapshot` from a shell — it is refused by design;
persistence = the GUI Snapshot button or an admin's `SC_ADMIN=1 sc
snapshot`. Don't chase it. (Sections are snapshotted; descriptions ride the
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
2. **Copy the matching reference** from the engine's
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
this fork's cartographer shell whatever its actual shortname:

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
WHERE desc IS NULL AND path LIKE 'region/%' ORDER BY role, path;
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
  `core.hooksPath` doesn't point at does nothing -> check the wiring after
  every setup.
