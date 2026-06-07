---
name: db_map
description: Schema map + reusable SQL for super-coder's shell_db.db. Check before composing any DB query — identity, memory, roadmap, documents, flags, skills.
category: substrate
common: true
---

# db_map — super-coder's DB at a glance

Source of truth: `.super-coder/shell_db.db` (gitignored; rebuilt from
`schema.sql` + `migrations/*.sql` + `.sc-state/content.sql`). All identity,
memory, and content live in tables — never flat files. **The `.db` is a cache:**
after any content write, `./sc snapshot` re-serializes to text (see the
`snapshot` skill). Lazy-load: query for what you need, don't bulk-read.

## Tables

| Table | Holds | Write rule |
|---|---|---|
| `shells` | identity core: `mandate`, `system_prompt`, `current_state` (rolling, ~500 chars), `connections` (authored "where things live" notes → boot `## CONNECTIONS`), `lineage_seed`, `active_archive_id` | UPDATE in place |
| `dr_section` | the navigation index — `name`, `path_prefix`, `description`; rendered in boot `## CONNECTIONS`. Cartographer-authored | INSERT/UPDATE (cartographer) |
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

```sql
-- current_state (rolling status, not a log — rewrite in place):
UPDATE shells SET current_state='…' WHERE shell_id=<self>;

-- plant a seed / L&S entry (date in the column, not the body):
INSERT INTO shell_identity_entries (shell_id, kind, entry_date, body)
VALUES (<self>, 'seed', date('now'), '…');     -- kind='lns' for a lesson

-- curate one out (preserves the row, frees a cap slot):
UPDATE shell_identity_entries SET retired_at=datetime('now') WHERE entry_id=?;

-- record a decision:
INSERT INTO shell_decisions (shell_id, decision_date, title, body)
VALUES (<self>, date('now'), '…', '…');

-- roadmap: move a feature's horizon / add one / retire one off the board:
UPDATE roadmap SET roadmap_status='next' WHERE feature_id=?;
UPDATE roadmap SET roadmap_status='retired' WHERE feature_id=?;  -- decided-against / split / absorbed; keeps the row
INSERT INTO roadmap (title, roadmap_status, sort_order, owning_shell, summary)
VALUES ('…', 'brainstorm', 0, <self>, '…');

-- author a spec body (DB owns it; render_path points at the flat file):
INSERT INTO documents (feature_id, kind, seq, title, body, render_path)
VALUES (?, 'spec', 1, '…', '…', 'specs_sc/….md');
-- freeze on ship (immutable thereafter):
UPDATE documents SET frozen=1, frozen_date=date('now') WHERE document_id=?;

-- open / close a flag:
INSERT INTO flags (display_name, description, shell_id, feature_id)
VALUES ('CC-001', '[Area] … | Blocker for: …', <self>, ?);
UPDATE flags SET resolved=1, resolved_date=date('now'), resolution_notes='…'
WHERE flag_id=?;
```

## After writing

Content lives in the `.db` until you serialize it. Run `./sc snapshot` (and
`./sc render` if you changed documents/roadmap/skills), then commit the text —
see the `snapshot` skill for the full lifecycle.
