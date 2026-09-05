---
name: surface_catalogue
description: Read the host repo's dr_* catalogue (sections, file behavior, deps, env, semantic layer) as abbreviated source documentation — one navigation resource beside grep, direct reads, and docs. Use to orient in an unfamiliar repo fast.
category: substrate
common: true
---

# surface_catalogue — the repo map as abbreviated documentation

The `dr_*` catalogue is a scan of the host repo: a resource for orienting, not
a required first step. Use it, grep, read files directly, read repository docs,
or use harness-native search as the work warrants. It is separate from Subfloor
control-plane memory and from the product's runtime database. Inspect structure
with `sc map-schema`; query data with `sc map-sql "…"`.

NEVER map the repo yourself. The map stays fresh automatically (git hooks
re-map on pull / branch-switch / rebase) and is owned by the **cartographer**
shell. Empty / stale / wrong map -> flag the cartographer, don't re-map.

| Table | Holds |
|---|---|
| `dr_repo` | the repo: name, root, remote, vcs, default_branch, file_count, mapped_at |
| `dr_section` | the navigational index: `name`, `path_prefix`, `description` — "UI here / API here / docs here". Rendered in the boot `## CONNECTIONS` block; start here. |
| `dr_filepath` | one row per file: `path`, `ext`, `lang`, `role` (code/doc/config/test/asset/env), `bytes`, `lines`, `desc` (cartographer one-line behavior: responsibility, mechanism, input, output; NULL until curated) |
| `dr_dependency` | deps from the manifests: `manager` (npm/pip/poetry/go/cargo), `name`, `version`, `kind`, `source_file` |
| `dr_env` | env-var names found in `.env.*` example files: `name`, `source_file` |
| `dr_endpoint` | HTTP routes: `method`, `path`, `handler` (file:line), `framework`, `source_file` |
| `dr_db_table` / `dr_db_column` | the app DB schema: tables/views + their columns (`type`, `pk`, `not_null`) |
| `dr_route` / `dr_component` | UI routes (`path`, `kind`) + components (`name`, `path`) |

First five = mapped on EVERY repo. Last three = the semantic layer, populated
only when the cartographer wired an extractor for this repo's stack (see the
`cartographer` skill). Empty `dr_endpoint` = no extractor wired, NOT "no
endpoints" — check before relying on it; flag the cartographer if a dimension
you need is missing.

## Orient fast

Boot `## CONNECTIONS` already shows the section index. Cheap flow: pick a
section there -> query that section's leaves (file names + descriptions) ->
read the one or two files you need. One query deep beats a full preload.

Run `sc map-schema` before the first structural query; pass = it lists the
expected `dr_*` object. Run `sc map-schema <dr_table>` before using unfamiliar
columns; pass = ordinal/name/type/nullability/default/PK + indexes are explicit.
Use `sc map-sql` only for data queries.

```sql
-- all of these run against the map db:  sc map-sql "<query>"
-- the section index (same as boot CONNECTIONS) — where to start:
SELECT name, path_prefix, description FROM dr_section ORDER BY sort_order, name;

-- a chosen section's leaves — the descriptions tell you which file to open:
SELECT path, desc, lines FROM dr_filepath
WHERE path LIKE 'shell_core/api/%' ORDER BY path;

-- the synthetic Repository Root group (not an authored dr_section row):
SELECT path, desc, lines FROM dr_filepath
WHERE instr(path, '/') = 0 ORDER BY path;

-- what is this repo + how big:
SELECT name, default_branch, file_count, mapped_at FROM dr_repo;

-- language mix:
SELECT lang, COUNT(*) n, SUM(lines) lines FROM dr_filepath
WHERE lang IS NOT NULL GROUP BY lang ORDER BY n DESC;

-- where the code lives (skip docs/config/assets):
SELECT path, lang, lines FROM dr_filepath WHERE role='code' ORDER BY lines DESC;

-- find files by area (grep or open them directly afterwards):
SELECT path FROM dr_filepath WHERE path LIKE '%auth%';

-- stack + config surface:
SELECT manager, name, version FROM dr_dependency ORDER BY manager, name;
SELECT name, source_file FROM dr_env ORDER BY name;

-- semantic layer (only if an extractor is wired for this repo — see cartographer):
SELECT method, path, handler FROM dr_endpoint ORDER BY path;            -- the API surface
SELECT name, kind, source_file FROM dr_db_table ORDER BY name;          -- the app DB schema
-- table_name is a string ref (cache; no FK): schema + migration files each
-- contribute their own copy of a table's columns — select source_file and
-- read one source's rows, or expect duplicates:
SELECT source_file, name, type, pk, not_null FROM dr_db_column
WHERE table_name='users' ORDER BY source_file;
SELECT path, kind, file FROM dr_route ORDER BY path;                    -- UI routes
```

## Stance

- **Any method.** The catalogue is a resource, not a mandate. Its value is the
  per-file behavioral `desc` and the semantic layer when wired; grep, direct
  reads, docs, and harness-native search remain equally valid.
- **Lazy-load.** Pull a file's contents once you know you need it. Carry the
  map, not the territory.
- **Map looks wrong?** Empty, stale (repo changed since `mapped_at`),
  mis-classified, a nested file under "other / unsectioned", or a `desc IS
  NULL` where you needed one -> Cartographer worklist item. Root files belong
  to `Repository Root`, not the unsectioned worklist. Flag the gap and keep
  working with another method; don't author the map yourself.
- **Semantic layer when wired.** Endpoints / DB schema / UI routes let you
  jump straight to the API surface or schema; a dimension is empty -> fall
  back to section + descriptions. Symbol-level semantics (functions/classes)
  are a later pass.
