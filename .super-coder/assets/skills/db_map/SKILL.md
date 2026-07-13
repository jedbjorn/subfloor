---
name: db_map
description: Data model behind the engine memory surfaces + the `sc mem` command for each. Check before reading or writing memory — identity, decisions, roadmap, documents, flags. Reads/writes go through the API (`sc mem`), never raw sqlite.
category: substrate
common: true
---

# db_map — super-coder's DB at a glance

All identity, memory, and content live in the engine DB
(`.super-coder/shell_db.db`). NEVER touch that file — read and write it only
through the engine API, via `sc mem`:

- **Read** = `sc mem get <surface>`: your own `state`, `seed`, `lns`,
  `decisions`, `flags`, `narrative`, `messages`; shared planning state
  `roadmap`, `projects`, `documents`, `tasks`, `shells` (`--json` for raw).
  `documents`/`tasks` take `--feature <id>` / `--doc <id>`; `--doc` on
  `documents` returns the one doc *with* its body.
- **Write** = `sc mem <cmd> …` (see `## Common writes`).

There is NO `sqlite3` path — not as a fallback, not for "ad-hoc" reads. If the
API isn't wired, `sc mem` fails loud instead of writing the DB behind its
back. Your identity rides in your bearer token — the server resolves token ->
shell; never name a shell in a write. The table below = the data model behind
those surfaces (what each `sc mem` write touches), not a query cheatsheet.
Lazy-load: `get` the one surface you need, don't bulk-read.

**Need a read/write `sc mem` doesn't expose?** Report the gap, don't reach for
the DB — the direct path is closed by design, and a fork can't patch the
engine (`sc update` would overwrite it). Open a flag naming the data + the
use, surface it to the FnB (who carries it upstream); message a
planner-flavor shell too if the fork has one. Until it lands: do what you can
through the API, flag the rest — NEVER query the DB directly.

```
sc mem flag open "[Engine] need to <read|write> <what> — no sc mem surface for it | Blocker for: <your work>"
```

The repo map (`dr_*`) lives in its own db, `.sc-state/map.db` (see the
`surface_catalogue` skill). The `dr_*` tables also exist in `shell_db.db` but
are ALWAYS empty there — a `dr_*` query against `shell_db.db` silently returns
0 rows instead of erroring. Never query `dr_*` here; this map covers only
`shell_db.db` (memory/identity/content).

## Tables

| Table | Holds | Write rule |
|---|---|---|
| `shells` | identity core: `mandate`, `system_prompt`, `current_state` (rolling, ~500 chars), `lineage_seed`, `active_archive_id`. (`connections`/`workspace` retired — boot `## CONNECTIONS` is derived from the `dr_*` map, not authored here) | UPDATE in place |
| `shell_identity_entries` | seed (cap 10) + L&S (`kind='lns'`, cap 20); triggers enforce caps | INSERT to add; UPDATE `retired_at` to curate out — NEVER edit a seed body (Law 3) |
| `shell_decisions` | major decisions | INSERT only; supersede via `parent_decision_id` |
| `shell_memory_archives` | one row per session; `full_narrative` appended progressively | INSERT at session open; UPDATE narrative |
| `roadmap` | one row per planned feature; `roadmap_status` = planning horizon (`brainstorm`→`in_progress`→`next`→`near_term`→`long_term`→`shipped`→`retired`), `sort_order` within a bucket. `shipped` = delivered; `retired` = off the board without shipping (decided-against / split / absorbed / replaced) — keep the row. `project_id` (nullable) = the work-stream the feature belongs to; the GUI Flow view groups on it (NULL = Ungrouped) | INSERT/UPDATE |
| `feature_blockers` | roadmap dependency edges: one row = `feature_id` depends on `blocked_by` (prerequisite lands first). Directed, kept acyclic (GUI Flow view wires them; the card's "depends on" picker sets them) | INSERT/DELETE the edge; set the whole set via `sc mem roadmap depends` |
| `documents` | content store — spec/doc bodies; `frozen=1` on ship (immutable); `render_path` = flat-file target | INSERT a new `seq` per stage; NEVER edit a frozen body |
| `flags` | open + resolved tasks; `feature_id` links a flag to the feature it blocks | INSERT to open; UPDATE `resolved=1` + `resolved_date` to close |
| `skills` / `shell_skills` | skill catalogue (system, seeded from `assets/skills/` via migration) + per-shell grants | managed by engine; grants via `./sc skill grant/revoke` |
| `projects` / `project_shells` | project standing + shell linkage; a `projects` row also doubles as a work-stream that roadmap features attach to via `roadmap.project_id` (the Flow-view grouping) | UPDATE `standing`; INSERT to add |

`<self>` = your `shell_id` (in the boot doc's ACTIVE SESSION block).

## Common writes

Each routes through the engine API to the live shared DB. `sc mem which`
orients; `sc mem <cmd> -h` shows flags. Writes always target your own shell —
the server resolves it from your token.

```
# current_state (rolling status, not a log — replaces in place):
sc mem state "…"

# plant a seed / L&S entry (date stamped for you):
sc mem seed "…"            # sc mem lns "…" for a lesson
sc mem retire <entry_id>   # curate one out (frees a cap slot)

# record a Major decision (supersede with --parent <id>):
sc mem decision "…" --rationale "…"

# roadmap: add a feature / move its horizon:
sc mem roadmap add "…" --status brainstorm --summary "…" [--project <shortname|id>]
sc mem roadmap status <feature_id> shipped

# roadmap grouping + sequencing (drive the GUI Flow view):
sc mem roadmap project <feature_id> <shortname|id>   # assign a work-stream (or 'none' to clear)
sc mem roadmap depends <feature_id> --on <id> [--on <id>]   # set dependencies (replaces; omit --on to clear; refuses cycles)

# author a spec/doc body (--body-file reads the markdown), then freeze on ship:
sc mem doc add "…" --kind spec --feature <id> --body-file ./draft.md --render-path specs_sc/….md
sc mem doc freeze <document_id>

# spec_tasks (the plan): add a task / advance it / close it honestly:
sc mem task add "…" --feature <id> --doc <doc_id> --seq <n> [--desc "…"]
sc mem task start <task_id>     # sc mem task done <task_id>
sc mem task cancel <task_id> --notes "moved to F<id> as task #<n>"   # split/re-scope — never mark unbuilt work done

# open / edit / close a flag:
sc mem flag open "[Area] … | Blocker for: …" --name CC-001 [--feature <id>]
sc mem flag edit <flag_id> [--description "…"] [--priority High] [--feature <id>]
sc mem flag close <flag_id> --notes "…"

# projects (standing + linkage):
sc mem project add <shortname> "<title>" --purpose "…" --standing "…"
sc mem project standing <shortname|id> "…"     # sc mem project status <…> paused

# inbox + first-run:
sc mem message send <shortname> "…"     # check / mark-read too (see `messaging`)
sc mem oriented                          # mark first-run done (bootstrapped=1)
```

## After writing

Nothing more to run — the write is live in the shared engine DB on commit,
visible to every shell. Persisting to git is an admin/GUI step, not yours.
