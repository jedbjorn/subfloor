---
name: cartographer
description: Own the repo map — configure mapping to THIS repo, wire auto-remap, install semantic extractors, curate authored navigation, and finish through one truthful finalization gate. Cartographer-only.
category: substrate
command: sc map-setup
common: false
---

# cartographer — own the repo map

Working shells consume the `dr_*` catalogue and NEVER map. Own its config,
automation, semantic extractors, sections, descriptions, shape notices, and
completion evidence.

Map data = `.sc-state/local/map/map.db`, separate from engine memory. Use:

- `sc map-schema [dr_table]` for structure. Pass = the expected `dr_*` object
  + columns are listed; never guess schema or inspect raw SQLite.
- `sc map-sql "…"` for read-only data queries.
- `sc map-sql-rw "…"` only for the authored `dr_section` / `dr_filepath.desc`
  writes named below.
- `sc map` to refresh derived rows.
- `sc map finalize` to prove completion. Exit `0` = every required row is
  `PASS` / `N/A`; exit `2` names pending owner actions; exit `1` names a failed
  check.

## First boot / heal

Run this sequence on first boot, after a shape notice, or when the map drifts:

1. `sc map-schema` then `sc map-schema dr_repo`. Pass = map structure is
   inspectable through the supported surface.
2. Inspect live data:

   ```sql
   SELECT name, root, default_branch, file_count, mapped_at FROM dr_repo;
   SELECT lang, COUNT(*) n FROM dr_filepath
   WHERE lang IS NOT NULL GROUP BY lang ORDER BY n DESC;
   SELECT role, COUNT(*) n FROM dr_filepath GROUP BY role ORDER BY n DESC;
   ```

3. Tune `.sc-state/local/map/config.json` in the assigned worktree only where defaults are
   wrong. Config is per-clone runtime state and never a commit. All keys are
   optional; skip sets extend defaults and cannot re-include engine-owned
   paths:

   ```json
   {
     "skip_dirs": ["generated", "fixtures"],
     "skip_files": ["LICENSE"],
     "role_overrides": [
       {"prefix": "cmd/", "role": "code"},
       {"glob": "*.proto", "role": "code"},
       {"prefix": "docs/adr/", "role": "doc"}
     ]
   }
   ```

4. Run `sc map-setup`. Pass = `git config --get core.hooksPath` prints
   `.super-coder/hooks`, the declared hooks are executable, and `dr_repo`
   carries a current `mapped_at` + correct file count.
5. Curate sections + descriptions + semantic rows with the worklists below.
6. Resolve every notice-linked flag, then mark the notice read last.
7. Run `sc map finalize`. Complete Cartographer-owned actions; hand each
   Admin-owned snapshot/review action to Admin. Pass = a rerun exits `0`.
8. On first boot only, run `sc mem state "…"` then `sc mem oriented` after the
   finalizer is green.

Automation remains healthy when:

- `post-merge` / `post-checkout` / `post-rewrite` run `sc map` through
  `core.hooksPath`.
- Admin control-plane rebuilds remap through their supported lifecycle.
- pm2's `sc-map-<repo>` one-shot cycles stopped -> online hourly while the
  stack is up. A repo without pm2 relies on hooks + manual `sc map`.

## Authored navigation

### Sections

`dr_section` is authored + snapshot-backed. Curate useful path prefixes; never
insert an empty prefix. Root files belong to the synthetic `Repository Root`
group and never enter `dr_section`.

```sql
-- Repository Root leaves; a non-empty result renders the synthetic group:
SELECT path, desc, lines FROM dr_filepath
WHERE instr(path, '/') = 0 ORDER BY path;

-- Authored sections + live counts:
SELECT s.name, s.path_prefix, s.description,
       (SELECT COUNT(*) FROM dr_filepath f
        WHERE f.path LIKE s.path_prefix || '%') n
FROM dr_section s ORDER BY s.sort_order, s.name;

-- WORKLIST: only nested unmatched files are real section gaps:
SELECT f.path FROM dr_filepath f
WHERE instr(f.path, '/') > 0
  AND NOT EXISTS (
    SELECT 1 FROM dr_section s
    WHERE f.path LIKE s.path_prefix || '%'
  )
ORDER BY f.path;

-- STALE authored sections after a rename/removal:
SELECT s.name, s.path_prefix, s.description
FROM dr_section s
WHERE NOT EXISTS (
  SELECT 1 FROM dr_filepath f
  WHERE f.path LIKE s.path_prefix || '%'
)
ORDER BY s.name;
```

Use `sc map-sql-rw` to `INSERT` / `UPDATE` / `DELETE` the exact rows identified
by these queries. Pass = nested unmatched + stale-section worklists return no
rows; root files remain queryable through `instr(path, '/') = 0`.

### Descriptions

Set `dr_filepath.desc` to an adequate one-line description (<=100 chars): say
what the file does/holds, not its kind or filename. Descriptions survive remap
in the live DB but are not snapshot durability; refill after a fresh rebuild.

```sql
WITH f AS (
  SELECT path, role, desc,
         replace(path, rtrim(path, replace(path,'/','')), '') AS base
  FROM dr_filepath
), g AS (
  SELECT *, CASE WHEN instr(base,'.') > 0
    THEN substr(base, 1, instr(base,'.')-1) ELSE base END AS stem
  FROM f
)
SELECT path, role, desc FROM g
WHERE desc IS NULL
   OR (length(stem) >= 5 AND (
       lower(substr(desc, -length(base))) = lower(base)
       OR lower(substr(desc, -length(stem))) = lower(stem)
   ))
ORDER BY (desc IS NULL) DESC, role, path;
```

Update only rows verified against the file. Pass = the worklist is empty +
spot checks per section describe behavior that the path alone cannot reveal.

### Product DB

Tag the host application's schema/migrations as product DB, never engine
memory. The live app `.db` is often ignored; tracked schema + migrations are
the durable map anchors.

```sql
UPDATE dr_filepath
SET desc='Product DB schema — the APP database (NOT engine memory)'
WHERE path='<app schema file>';

UPDATE dr_filepath
SET desc='Product DB migration — change the app schema here'
WHERE path LIKE '<app migrations dir>/%';
```

Create an authored section when those files form a real area. Pass = working
shells can identify the app DB definition without confusing it with Subfloor
control-plane state. No product DB -> `N/A`.

## Semantic extractors

Extractors implement `extract(con, repo_root, cfg) -> str` and own only their
semantic `dr_*` rows. They DELETE + repopulate their own derived tables, guard
unparseable files, report best-effort omissions, and never claim exhaustive
coverage.

Adopt an extractor:

1. Inspect stack dependencies/file mix with `sc map-sql`.
2. Author `.sc-state/map_extractors/<name>.py` in the assigned worktree against
   the extractor contract above.
3. Run `sc map-extractor install
   ".sc-state/map_extractors/<name>.py"`. Pass = output
   prints the installed canonical path + SHA-256 matching the authored bytes.
4. NEVER `cp`, `mv`, redirect, or use a file-edit tool into
   another checkout's `.sc-state/map_extractors/`. The guarded installer is the only
   supported cross-worktree write.
5. Run `sc map`, inspect structure with `sc map-schema <dr_table>`, then query
   rows with `sc map-sql`. Pass = expected semantic rows exist + the map log
   has no extractor failure.
6. Commit + push the authored worktree source. Hand Admin the source path for
   review/merge when finalization names that action. Generated map DB, status,
   receipts, and snapshots stay local-only.

An extractor failure rolls its plug-in writes back while preserving the core
map. Pass = `sc map finalize` reports no failed module and every installed
extractor has matching receipt/source/Admin evidence.

## Shape notices

Sender = the dev/coder shell on merge, not Planner. Open blocking map-quality flags before sending
one notice to the `cartographer` role alias:

```text
shape: <what landed> — paths: <region/>; ref: <feature/doc/PR>
flags: <numeric_id>=<SC-name>[, <numeric_id>=<SC-name>] | none
curate; verify and close each flag; mark this notice read last.
```

Name the durable ref + exact path region. Pair every flag's numeric DB ID with
its display name. Write `flags: none` when no flag exists. Pass = one notice
carries every map-quality flag opened for that shape change.

On receipt:

1. Parse all three lines. Missing/malformed `flags`, missing flag, or ID/name mismatch -> surface
   the exact defect + leave the notice unread.
2. Run the nested-section + stale-section + description + semantic worklists
   scoped to the named region. Pass = every scoped result is clean.
3. For each pair, run `sc mem get flags <numeric_id>` and confirm the display
   name. An already-resolved row passes only when its notes name the verified
   map result. Otherwise run `sc mem flag close <numeric_id> --notes "<what
   was verified>"`; pass = the exact row is resolved with adequate notes.
4. Run `--message mark-read <message_id>` last. Pass = scoped worklists + every
   named flag passed before the notice became read. Send no closure reply.

## Persistence boundary

Map config, live descriptions, derived rows, install receipts, and generated
status are local-only. Sections persist only after the GUI Snapshot action or
Admin runs `sc snapshot`. NEVER run plain `sc snapshot` from Cartographer; it
is refused. Pass = `sc map finalize` reports Authored sections `PASS` after
Admin acts, without Cartographer mutating snapshot/Git/message/flag state on
their behalf.
