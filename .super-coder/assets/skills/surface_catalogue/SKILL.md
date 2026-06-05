---
name: surface_catalogue
description: Read the host repo via the dr_* catalogue (files, languages, deps, env) BEFORE grepping or walking the tree. Query first, lazy-load the few files it points at. Use to orient in an unfamiliar repo fast.
category: substrate
common: true
---

# surface_catalogue — read the repo from the map, not by grepping

super-coder lives inside a host repo. The **dr_\*** tables are a scan of that
repo — query them first to orient, instead of walking the tree blind.

You do **not** map the repo. The map is kept fresh for you automatically (git
hooks re-map on pull / branch-switch / rebase) and is owned by the
**cartographer** shell, which configures and heals it. Your job is to *read* it.
If it ever looks empty, stale, or wrong, that's a cartographer task — flag it,
don't map it yourself.

| Table | Holds |
|---|---|
| `dr_repo` | the repo: name, root, remote, vcs, default_branch, file_count, mapped_at |
| `dr_section` | the navigational index: `name`, `path_prefix`, `description` — "UI here / API here / docs here". Rendered in the boot `## CONNECTIONS` block; start here. |
| `dr_filepath` | one row per file: `path`, `ext`, `lang`, `role` (code/doc/config/test/asset/env), `bytes`, `lines`, `desc` (cartographer one-liner, NULL until curated) |
| `dr_dependency` | deps from the manifests: `manager` (npm/pip/poetry/go/cargo), `name`, `version`, `kind`, `source_file` |
| `dr_env` | env-var names found in `.env.*` example files: `name`, `source_file` |

## Orient fast

The boot `## CONNECTIONS` block already shows the **section index** (where to
start). The flow is: pick a section there → query *that section's leaves* (file
names + descriptions) → read the one or two files you need. Section-first, one
cheap query deep — never a full preload.

```sql
-- the section index (same as boot CONNECTIONS) — where to start:
SELECT name, path_prefix, description FROM dr_section ORDER BY sort_order, name;

-- a chosen section's leaves — the descriptions tell you which file to open:
SELECT path, desc, lines FROM dr_filepath
WHERE path LIKE 'shell_core/api/%' ORDER BY path;

-- what is this repo + how big:
SELECT name, default_branch, file_count, mapped_at FROM dr_repo;

-- language mix:
SELECT lang, COUNT(*) n, SUM(lines) lines FROM dr_filepath
WHERE lang IS NOT NULL GROUP BY lang ORDER BY n DESC;

-- where the code lives (skip docs/config/assets):
SELECT path, lang, lines FROM dr_filepath WHERE role='code' ORDER BY lines DESC;

-- find files by area (the map is the index; grep only what it points at):
SELECT path FROM dr_filepath WHERE path LIKE '%auth%';

-- stack + config surface:
SELECT manager, name, version FROM dr_dependency ORDER BY manager, name;
SELECT name, source_file FROM dr_env ORDER BY name;
```

## Stance

- **Map first, grep second.** Query `dr_filepath` to find the handful of files
  that matter, then read those — don't `grep -r` the whole tree.
- **Lazy-load.** The catalogue is the index; pull a file's contents only once
  the map points you at it. Carry the map, not the territory.
- **Map looks wrong?** Empty, stale (repo changed since `mapped_at`), or
  mis-classified — that's the cartographer's to fix. Raise it; don't re-map.
  A file under "other / unsectioned", or a `desc IS NULL` where you needed one,
  is also a cartographer worklist item — flag it, don't author the map yourself.
- Maps files / deps / env + the navigation layer (sections + per-file
  descriptions). Symbol-level semantics (functions/classes) are a later pass —
  until then, read the code the section + descriptions point you at.
