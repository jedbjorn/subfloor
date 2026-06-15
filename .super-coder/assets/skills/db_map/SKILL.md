---
name: db_map
description: Schema map + reusable SQL for super-coder's shell_db.db. Check before composing any DB query — identity, memory, roadmap, documents, flags, skills.
category: substrate
common: true
---

# db_map — super-coder's DB at a glance

Source of truth: `.super-coder/shell_db.db` (gitignored; rebuilt from
`schema.sql` + `migrations/*.sql` + `.sc-state/content.sql`). All identity,
memory, and content live in tables — never flat files. Lazy-load: query for what
you need, don't bulk-read.

**Reads use raw `sqlite3` SELECT; writes go through `./sc mem`.** Two DBs are in
reach (this engine DB + the app's product DB) with overlapping table names, so a
raw INSERT against the wrong one succeeds silently. `./sc mem` resolves + guards
*this* DB and snapshots for you (the `.db` is a cache — un-snapshotted writes are
lost on rebuild). Table below = the schema for your SELECTs; `## Common writes` =
the `./sc mem` command for each change.

The repo map (`dr_*`) is **not here** — it lives in its own db, `.sc-state/map.db`
(see the `surface_catalogue` skill). This map covers only `shell_db.db`, your
memory/identity/content. Don't look for `dr_*` in `shell_db.db`.

## Tables

| Table | Holds | Write rule |
|---|---|---|
| `shells` | identity core: `mandate`, `system_prompt`, `current_state` (rolling, ~500 chars), `lineage_seed`, `active_archive_id`. (`connections`/`workspace` retired — boot `## CONNECTIONS` is derived from the `dr_*` map, not authored here) | UPDATE in place |
| `shell_identity_entries` | seed (cap 10) + L&S (`kind='lns'`, cap 20); triggers enforce caps | INSERT to add; UPDATE `retired_at` to curate out — never edit a seed body (Law 3) |
| `shell_decisions` | major decisions | INSERT only; supersede via `parent_decision_id` |
| `shell_memory_archives` | one row per session; `full_narrative` appended progressively | INSERT at session open; UPDATE narrative |
| `roadmap` | one row per planned feature; `roadmap_status` is a planning horizon (`brainstorm`→`in_progress`→`next`→`near_term`→`long_term`→`shipped`→`retired`), `sort_order` within a bucket. `shipped` = delivered; `retired` = taken off the board (decided-against / split / absorbed / replaced) without shipping — keep the row | INSERT/UPDATE |
| `documents` | the content store — specs/docs bodies live here; `frozen=1` on ship (immutable); `render_path` = flat-file target | INSERT a new `seq` per stage; never edit a frozen body |
| `flags` | open + resolved tasks; `feature_id` links a flag to the feature it blocks | INSERT to open; UPDATE `resolved=1` + `resolved_date` to close |
| `skills` / `shell_skills` | skill catalogue (system, seeded from `assets/skills/` via migration) + per-shell grants | catalogue via migration; grants via snapshot |
| `projects` / `project_shells` | project standing + shell linkage | UPDATE `standing`; INSERT to add |

`<self>` = your `shell_id` (in the boot doc's ACTIVE SESSION block).

## Common writes

Each guards the engine DB and snapshots for you. `./sc mem which` orients;
`./sc mem <cmd> -h` shows flags. Writes target your shell by default (`--shell` to override).

```
# current_state (rolling status, not a log — replaces in place):
./sc mem state "…"

# plant a seed / L&S entry (date stamped for you):
./sc mem seed "…"            # ./sc mem lns "…" for a lesson
./sc mem retire <entry_id>   # curate one out (frees a cap slot)

# record a Major decision (supersede with --parent <id>):
./sc mem decision "…" --rationale "…"

# roadmap: add a feature:
./sc mem roadmap add "…" --status brainstorm --summary "…"

# author a spec/doc body (--body-file reads the markdown), then freeze on ship:
./sc mem doc add "…" --kind spec --feature <id> --body-file ./draft.md --render-path specs_sc/….md
./sc mem doc freeze <document_id>

# open / close a flag:
./sc mem flag open "[Area] … | Blocker for: …" --name CC-001 [--feature <id>]
./sc mem flag close <flag_id> --notes "…"
```

**No `mem` verb yet** (raw `sqlite3` against the engine DB, then `./sc snapshot`):
moving a roadmap feature's horizon (`UPDATE roadmap SET roadmap_status=…`),
project standing (`projects`), and `spec_tasks`. Confirm the target with
`./sc mem which` before a raw write.

## After writing

`./sc mem` snapshots (and renders) for you — nothing more to run; just commit the
text it serialized. Only raw `sqlite3` writes need a manual `./sc snapshot` (and
`./sc render` if you changed documents/roadmap/skills). See the `snapshot` skill
for the full lifecycle.
