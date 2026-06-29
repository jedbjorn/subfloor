-- 0027 — reseed memory, db_map, messaging: remove sqlite3-write / snapshot / render guidance
--
-- Shells write via ./sc mem (API-proxied at boot). They don't need to know that
-- sqlite3 writes, ./sc snapshot, or ./sc render exist — so we removed all mention.

BEGIN;

UPDATE skills SET content = '# memory — write as you go

All memory is DB rows (no flat files). Write at the moment it matters, not in a
close ritual.

**Write through `./sc mem`.** The write lands in the live engine DB — shared by
every shell, durable + visible to all the moment it commits. Writes default to
your shell; pass `--shell <id|name>` to be explicit.

## current_state — rolling status, NOT a log

Your present focus + what''s next. **Replaces in place; never a log.** Soft target
~500 chars. Rewrite when focus shifts.
```
./sc mem state "…"
```

## Session narrative — append at inflection points

One row per session, appended progressively. Append a `[HH:MM]` line (the time is
stamped for you) when: a decision lands, an approach changes or is rejected, the
FnB says something that shapes the work, an assumption breaks, or before a big
change.
```
./sc mem narrative "…"
```

## seed (cap 10) — who you are

Identity-forming moments. Past-tense/timeless. Add a new entry; **never edit a
body** (curate by retiring). The genesis + lineage seed are already yours.
```
./sc mem seed "…"            # add
./sc mem retire <entry_id>   # curate out (frees a cap slot)
```

## L&S (cap 20) — how you work

Operating lessons, imperative voice. Add when a lesson lands; curate by retiring.
Caps are trigger-enforced (seed 10, L&S 20) — `./sc mem` reports the cap message;
retiring frees a slot.
```
./sc mem lns "…"
```

## Decisions — Major only

Record a Major decision (architecture, approach, a path chosen over another).
Never rewritten; supersede via `--parent <decision_id>`. Mirror the headline into
the narrative.
```
./sc mem decision "…" --rationale "…" [--parent <id>]
```

## Stance

Write-as-you-go beats batch-at-close: it costs nothing per write and zero at
session end. Curate seed/L&S (revise the set), never rewrite history (decisions,
narrative, seed bodies). Full command reference + table map: the `db_map` skill.'
  WHERE name = 'memory' AND is_deleted = 0;

UPDATE skills SET content = '# db_map — super-coder''s DB at a glance

Source of truth: `.super-coder/shell_db.db` (gitignored; rebuilt from
`schema.sql` + `migrations/*.sql` + `.sc-state/content.sql`). All identity,
memory, and content live in tables — never flat files. Lazy-load: query for what
you need, don''t bulk-read.

Query with `sqlite3 .super-coder/shell_db.db "SELECT …"`. Writes go through
`./sc mem`. Table below = the schema for your SELECTs; `## Common writes` = the
`./sc mem` command for each change.

The repo map (`dr_*`) is **not here** — it lives in its own db, `.sc-state/map.db`
(see the `surface_catalogue` skill). This map covers only `shell_db.db`, your
memory/identity/content. Don''t look for `dr_*` in `shell_db.db`.

## Tables

| Table | Holds | Write rule |
|---|---|---|
| `shells` | identity core: `mandate`, `system_prompt`, `current_state` (rolling, ~500 chars), `lineage_seed`, `active_archive_id`. (`connections`/`workspace` retired — boot `## CONNECTIONS` is derived from the `dr_*` map, not authored here) | UPDATE in place |
| `shell_identity_entries` | seed (cap 10) + L&S (`kind=''lns''`, cap 20); triggers enforce caps | INSERT to add; UPDATE `retired_at` to curate out — never edit a seed body (Law 3) |
| `shell_decisions` | major decisions | INSERT only; supersede via `parent_decision_id` |
| `shell_memory_archives` | one row per session; `full_narrative` appended progressively | INSERT at session open; UPDATE narrative |
| `roadmap` | one row per planned feature; `roadmap_status` is a planning horizon (`brainstorm`→`in_progress`→`next`→`near_term`→`long_term`→`shipped`→`retired`), `sort_order` within a bucket. `shipped` = delivered; `retired` = taken off the board (decided-against / split / absorbed / replaced) without shipping — keep the row. `project_id` (nullable) = the work-stream the feature belongs to; the GUI Flow view groups on it (NULL = Ungrouped) | INSERT/UPDATE |
| `feature_blockers` | the roadmap''s dependency edges: one row = `feature_id` depends on `blocked_by` (prerequisite must land first). Directed, kept acyclic (the GUI Flow view wires them; the card''s "depends on" picker sets them) | INSERT/DELETE the edge; set the whole set via `./sc mem roadmap depends` |
| `documents` | the content store — specs/docs bodies live here; `frozen=1` on ship (immutable); `render_path` = flat-file target | INSERT a new `seq` per stage; never edit a frozen body |
| `flags` | open + resolved tasks; `feature_id` links a flag to the feature it blocks | INSERT to open; UPDATE `resolved=1` + `resolved_date` to close |
| `skills` / `shell_skills` | skill catalogue (system, seeded from `assets/skills/` via migration) + per-shell grants | catalogue via migration; grants via snapshot |
| `projects` / `project_shells` | project standing + shell linkage; a `projects` row also doubles as a **work-stream** that roadmap features attach to via `roadmap.project_id` (the Flow-view grouping) | UPDATE `standing`; INSERT to add |

`<self>` = your `shell_id` (in the boot doc''s ACTIVE SESSION block).

## Common writes

Each guards the engine DB and writes to the live shared DB. `./sc mem which` orients;
`./sc mem <cmd> -h` shows flags. Writes target your shell by default (`--shell` to override).

```
# current_state (rolling status, not a log — replaces in place):
./sc mem state "…"

# plant a seed / L&S entry (date stamped for you):
./sc mem seed "…"            # ./sc mem lns "…" for a lesson
./sc mem retire <entry_id>   # curate one out (frees a cap slot)

# record a Major decision (supersede with --parent <id>):
./sc mem decision "…" --rationale "…"

# roadmap: add a feature / move its horizon:
./sc mem roadmap add "…" --status brainstorm --summary "…" [--project <shortname|id>]
./sc mem roadmap status <feature_id> shipped

# roadmap grouping + sequencing (drive the GUI Flow view):
./sc mem roadmap project <feature_id> <shortname|id>   # assign a work-stream (or ''none'' to clear)
./sc mem roadmap depends <feature_id> --on <id> [--on <id>]   # set dependencies (replaces; omit --on to clear; refuses cycles)

# author a spec/doc body (--body-file reads the markdown), then freeze on ship:
./sc mem doc add "…" --kind spec --feature <id> --body-file ./draft.md --render-path specs_sc/….md
./sc mem doc freeze <document_id>

# spec_tasks (the plan): add a task / advance it:
./sc mem task add "…" --feature <id> --doc <doc_id> --seq <n> [--desc "…"]
./sc mem task start <task_id>     # ./sc mem task done <task_id>

# open / close a flag:
./sc mem flag open "[Area] … | Blocker for: …" --name CC-001 [--feature <id>]
./sc mem flag close <flag_id> --notes "…"

# projects (standing + linkage):
./sc mem project add <shortname> "<title>" --purpose "…" --standing "…"
./sc mem project standing <shortname|id> "…"     # ./sc mem project status <…> paused

# inbox + first-run:
./sc mem message send <shortname> "…"     # check / mark-read too (see `messaging`)
./sc mem oriented                          # mark first-run done (bootstrapped=1)
```

## After writing

Nothing more to run — the write is live in the shared engine DB the moment it
commits, visible to every shell. Persisting it to git is an admin/GUI step, not
yours.'
  WHERE name = 'db_map' AND is_deleted = 0;

UPDATE skills SET content = '# messaging — the shell inbox

One shell writes a markdown message to another; the recipient discovers it on its
next boot via the `## STATUS` `Inbox:` count, surfaces it with `check`, and clears
it with `mark-read`. Body is markdown — preserved verbatim.

Drive it with **`./sc mem message`**. The sender is you; recipients are addressed
by `shortname`.

Trigger: `--message`
Args: `check [N] | send <to-shortname> <body> | mark-read <id>`

## check — your unread inbox

```
./sc mem message check [N]      # N optional; default 50, max 200
```

`check` is read-only — it does **not** auto-mark-read. Surface the body to the
operator (and reply if warranted, which is itself a `send`), then `mark-read` the
inbound in the same turn.

## send — message another shell

```
./sc mem message send <to-shortname> "<body>"
```

- Multi-word body = a single quoted argument; markdown is preserved verbatim.
- Examples: `./sc mem message send cartographer "map is stale — re-run ./sc map"`
  · `./sc mem message send cc "spec ready for review — see flag SC-014"`
- Unknown / deleted recipient → `mem: recipient shortname ''<x>'' unknown`. Empty
  body → `mem: body is empty`. Surface either to the operator plainly.

## mark-read — clear an inbox item (idempotent)

```
./sc mem message mark-read <message_id>
```

Access control: you can only mark read a message addressed to **you** — one for
another shell is a no-op. Re-marking a read message is also a no-op. Pass the
`message_id` that `check` surfaced.

## Stance

On boot, if the `## STATUS` `Inbox:` line is non-zero, run `--message check` and
surface the first item before continuing. A reply is a new `send` — there is no
threading; include `Re: <topic>` in the body if it matters. Keep the inbox honest:
mark-read only once you''ve actually acted on the message.'
  WHERE name = 'messaging' AND is_deleted = 0;

COMMIT;