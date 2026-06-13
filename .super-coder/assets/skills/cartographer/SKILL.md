---
name: cartographer
description: Own the repo map. Configure mapping to THIS repo, wire the auto-remap git hooks, and heal both when the repo or automation drifts. The cartographer's job alone — no working shell maps. Run on first boot, and again whenever the map looks wrong.
category: substrate
command: ./sc map-setup
common: false
---

# cartographer — own the repo map so no other shell has to

Working shells *consume* the `dr_*` catalogue and never map (see
`surface_catalogue`). Mapping — keeping that catalogue true to the repo — is
yours alone. You do three things: **configure** how this repo is mapped,
**wire** the automation that keeps it fresh, and **heal** both when they drift.

The catalogue lives in its **own db** — `.sc-state/map.db`, separate from the
engine memory db (`shell_db.db`) so an engine schema change never touches the
map. Every `dr_*` query below runs against it: `sqlite3 .sc-state/map.db "…"`.
Its authored layer (sections) is serialized to `.sc-state/map_content.sql` on
`./sc snapshot` and reloaded on a fresh map db.

`<self>` = your `shell_id` (ACTIVE SESSION block).

## How the map stays fresh (so you know what you own)

- **Git hooks** (`post-merge`, `post-checkout`, `post-rewrite`) re-run `./sc map`
  on every pull / branch-switch / rebase. They live tracked in
  `.super-coder/hooks/` and fire via `core.hooksPath` — a per-clone git setting
  that `./sc map-setup` wires. This is the routine refresh; no shell touches it.
- **`./sc rebuild`** re-maps too (the map is a derived cache; rebuilding the DB
  rebuilds it). So a fresh rebuild never leaves an empty map.
- **You** set the per-repo *config*, install the hook wiring, and repair it.
  Hooks can't catch uncommitted local restructuring, and `core.hooksPath` is
  unset on a fresh clone until `map-setup` runs — that gap is what you heal.

## First boot — configure mapping for THIS repo

1. **Look at the repo.** Read the current map and the tree:
   ```sql
   SELECT name, default_branch, file_count, mapped_at FROM dr_repo;
   SELECT lang, COUNT(*) n FROM dr_filepath WHERE lang IS NOT NULL GROUP BY lang ORDER BY n DESC;
   SELECT role, COUNT(*) n FROM dr_filepath GROUP BY role ORDER BY n DESC;
   ```
   Then eyeball the top-level dirs. Ask: is anything mis-classified, or is a
   generated/vendored dir being indexed that shouldn't be?

2. **Author `.sc-state/map.config.json`** to fit what's actually here. It is
   *authored content* (tracked, per-fork, survives `./sc update` — it lives in
   `.sc-state/`, outside the gitignored engine dir). All keys optional; each
   merges over `map_repo.py`'s built-in defaults:
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

3. **Wire + map:** `./sc map-setup` — points `core.hooksPath` at
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
   `SELECT path, role FROM dr_filepath WHERE path LIKE 'cmd/%';`

5. **Commit** the config + hooks (`git` skill), set your state, then
   `UPDATE shells SET bootstrapped=1 WHERE shell_id=<self>;` and `./sc snapshot`.

## Heal — re-run any time the map looks wrong

Re-boot the cartographer and run this when: the repo was restructured, a new
language/dir showed up, files are mis-roled, or the map went stale/empty on a
clone where the hooks never got wired.

1. Re-inspect (step 1) — what changed?
2. Edit `.sc-state/map.config.json` to match (step 2).
3. `./sc map-setup` — re-wires hooks (idempotent) + re-maps.
4. Verify (step 4) + commit.

## Standing jobs — sections, descriptions & the product DB (the navigation layer)

Beyond keeping the file list true, you own the two AUTHORED layers that turn the
raw map into navigation. Both are best-effort and NULL-until-curated; neither
blocks the auto-remap hook the working shells trigger. Both survive the remap
(`dr_section` is never touched by the mapper; `dr_filepath.desc` is preserved by
its UPSERT). The boot `## CONNECTIONS` block renders the section index; the
descriptions are the leaves a shell queries once it has narrowed to a section.

**1. Sections (`dr_section`)** — author/curate the navigational index. Seeded
from top-level dirs on first map (one section per dir), so it is non-empty on day
one; your job is to make it *good*: rename to what shells call the area, split a
coarse dir into real areas, merge noise, write the one-line `description`.

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
```

**2. Descriptions (`dr_filepath.desc`)** — fill per-file one-liners (≤100 chars),
worklist-driven. They are queried by working shells *within a chosen section*
(via `surface_catalogue`), never bulk-loaded at boot.

```sql
-- WORKLIST — undescribed files, most-load-bearing first:
SELECT path, role FROM dr_filepath WHERE desc IS NULL ORDER BY role, path;

-- describe (≤100 chars; preserved across the next auto-remap):
UPDATE dr_filepath SET desc='Boot composer — assembles CLAUDE.md from DB state' WHERE path='.super-coder/render/compose.py';
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
-- tag the product DB's definition (the engine-vs-app split made visible):
UPDATE dr_filepath SET desc='Product DB schema — the APP database (NOT engine memory)' WHERE path='<app schema file>';
UPDATE dr_filepath SET desc='Product DB migration — change the app schema here' WHERE path LIKE '<app migrations dir>/%';
-- optional: a section if the product DB is its own area
INSERT INTO dr_section (name, path_prefix, description, sort_order)
VALUES ('App DB', '<db dir>/', 'Product runtime database — schema + migrations (NOT the engine memory DB)', 7);
```

If this fork ships no database of its own, there is nothing to tag — skip it.

After a curation pass, `./sc snapshot` (sections are snapshotted; descriptions
ride the live DB + survive remap, refilled from the worklist if a rebuild drops them).

## Shape-change notices — the curation trigger

The git hooks keep the *mechanical* catalogue fresh on their own (paths, langs,
deps), but a newly-landed file arrives `desc IS NULL` and unsectioned — the
authored layer above doesn't fill itself. So working shells **message you** when
they change the repo's shape, turning curation from a next-boot *pull* into a
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
WHERE desc IS NULL AND path LIKE 'region/%' ORDER BY role, path;
-- 2. describe them (≤100 chars) — UPDATE dr_filepath SET desc=… per the worklist above.
-- 3. do they form / join a section? curate dr_section if the region is a new area.
```

Then `./sc snapshot` and `--message mark-read <id>` (see the `messaging` skill).
The mechanical remap already ran via the hook; your job on the notice is purely
the authored layer — describe the new leaves, section a new area. Scope is free:
`desc IS NULL` already narrows to exactly the uncurated tail.

## Stance

- **The map is infrastructure, not a chore for every shell.** You own it so the
  working shells never think about it. If a working shell is hunting the tree
  for something the map should know, that's a signal to heal the map — not to
  teach that shell to map.
- **Config is the lever, not code.** Tune `map.config.json`; only touch
  `map_repo.py` if the *mechanism* (a parser, a new role kind) is wrong.
- **Verify the automation, not just the file.** A written hook that
  `core.hooksPath` doesn't point at does nothing. Check the wiring after every
  setup.
