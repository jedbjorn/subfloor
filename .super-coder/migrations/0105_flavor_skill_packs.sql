-- 0105 — flavor-owned skill packs + Bespoke-only shell grants
--
-- Hard cutover by FnB ruling: standard shells derive one shared pack from
-- their flavor. Existing per-shell deviations are deliberately discarded,
-- not reconciled. Bespoke shells (flavor IS NULL) keep shell_skills.

CREATE TABLE IF NOT EXISTS flavor_skills (
    flavor    TEXT    NOT NULL,
    skill_id  INTEGER NOT NULL REFERENCES skills(skill_id),
    PRIMARY KEY (flavor, skill_id)
);

DROP VIEW IF EXISTS resolved_shell_skills;
CREATE VIEW resolved_shell_skills AS
SELECT sh.shell_id, fs.skill_id
FROM shells sh
JOIN flavor_skills fs ON fs.flavor = sh.flavor
WHERE sh.flavor IS NOT NULL
UNION ALL
SELECT ss.shell_id, ss.skill_id
FROM shell_skills ss
JOIN shells sh ON sh.shell_id = ss.shell_id
WHERE sh.flavor IS NULL;

-- Every shipped flavor starts with the common catalogue.
WITH shipped_flavors(flavor) AS (
    VALUES
        ('admin'),
        ('cartographer'),
        ('dev'),
        ('devops'),
        ('planner'),
        ('reviewer')
)
INSERT OR IGNORE INTO flavor_skills (flavor, skill_id)
SELECT f.flavor, s.skill_id
FROM shipped_flavors f
CROSS JOIN skills s
WHERE s.is_deleted = 0 AND s.common = 1;

-- Then add each shipped template's opt-ins. This list is intentionally the
-- engine baseline, not a reconstruction of local/per-shell state.
WITH template_grants(flavor, skill_name) AS (
    VALUES
        ('admin', 'git'),
        ('admin', 'git_cleanup'),
        ('admin', 'self_update'),
        ('admin', 'local_skill_management'),
        ('admin', 'migration_management'),
        ('admin', 'flag_sweep'),
        ('admin', 'authoring_syntax'),
        ('cartographer', 'cartographer'),
        ('cartographer', 'git'),
        ('dev', 'docs'),
        ('dev', 'spec'),
        ('dev', 'flags'),
        ('dev', 'git'),
        ('dev', 'database-migrations'),
        ('dev', 'redline_review'),
        ('dev', 'dev_kit'),
        ('dev', 'test_authoring'),
        ('dev', 'agents'),
        ('dev', 'sprint_dev'),
        ('devops', 'git'),
        ('devops', 'flags'),
        ('devops', 'docs'),
        ('devops', 'database-migrations'),
        ('devops', 'tailscale'),
        ('planner', 'docs'),
        ('planner', 'flags'),
        ('planner', 'git'),
        ('planner', 'blueprint'),
        ('planner', 'api-design'),
        ('planner', 'onboard'),
        ('planner', 'sprint_orchestration'),
        ('planner', 'sprint_orchestration_recover'),
        ('planner', 'sprint_orchestration_close'),
        ('reviewer', 'review'),
        ('reviewer', 'flags'),
        ('reviewer', 'git'),
        ('reviewer', 'api-design'),
        ('reviewer', 'database-migrations'),
        ('reviewer', 'redline_review'),
        ('reviewer', 'test_authoring'),
        ('reviewer', 'agents'),
        ('reviewer', 'sprint_review')
)
INSERT OR IGNORE INTO flavor_skills (flavor, skill_id)
SELECT g.flavor, s.skill_id
FROM template_grants g
JOIN skills s ON s.name = g.skill_name
WHERE s.is_deleted = 0;

-- shell_skills is now the Bespoke store. Remove flavored-shell rows instead of
-- migrating their differences; the flavor packs above are the new floor.
DELETE FROM shell_skills
WHERE shell_id IN (
    SELECT shell_id FROM shells WHERE flavor IS NOT NULL
);

-- Forward-reseed the AI-facing grant procedures.
-- Generated from assets/skills/*/SKILL.md; UPSERT by name preserves ids.

INSERT INTO skills (name, description, category, command, common, content, is_deleted)
VALUES (
    'db_map',
    'Data model behind the engine memory surfaces + the `sc mem` command for each. Check before reading or writing memory — identity, decisions, roadmap, documents, flags. Reads/writes go through the API (`sc mem`), never raw sqlite.',
    'substrate',
    NULL,
    1,
    '# db_map — super-coder''s DB at a glance

All identity, memory, and content live in the engine DB
(`.super-coder/shell_db.db`). NEVER touch that file — read and write it only
through the engine API, via `sc mem`:

- **Read** = `sc mem get <surface>`: your own `state`, `seed`, `lns`,
  `decisions`, `flags`, `narrative`, `messages`; shared planning state
  `roadmap`, `projects`, `documents`, `tasks`, `shells` (`--json` for raw).
  `documents`/`tasks` take `--feature <id>` / `--doc <id>`; `--doc` on
  `documents` returns the one doc *with* its body.
- **Write** = `sc mem <cmd> …` (see `## Common writes`).

There is NO raw `sqlite3` path — not as a fallback, not for "ad-hoc" reads.
If the API isn''t wired, `sc mem` fails loud instead of writing the DB behind
its back. Your identity rides in your bearer token — the server resolves
token -> shell; never name a shell in a write. Decisions read FLEET-WIDE
(every row, tagged `@shortname`) so cross-shell citations resolve; every
other identity surface reads as you.

**The `sc sql` lane** (read-only; `sc sql-rw` gated) is real and blessed for
what `sc mem` doesn''t cover: admin/reporting reads and sweep queries — the
flag_sweep / git_cleanup skills run it by design. The doctrine is one level
down: memory-surface reads and writes go through `sc mem`; `sc sql` is for
reporting ACROSS surfaces, never a write path for identity/memory (that is
what `sc mem` scopes and validates).

The table below = the data model behind those surfaces (what each `sc mem`
write touches), not a query cheatsheet. Lazy-load: `get` the one surface you
need, don''t bulk-read.

**Need a read/write `sc mem` doesn''t expose?** Report the gap, don''t reach for
the DB — the direct path is closed by design, and a fork can''t patch the
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
| `shell_identity_entries` | seed (cap 10) + L&S (`kind=''lns''`, cap 20); triggers enforce caps | INSERT to add; UPDATE `retired_at` to curate out — NEVER edit a seed body (Law 3) |
| `shell_decisions` | major decisions | INSERT only; supersede via `parent_decision_id` |
| `shell_memory_archives` | one row per session; `full_narrative` appended progressively | INSERT at session open; UPDATE narrative |
| `roadmap` | one row per planned feature; `roadmap_status` = planning horizon (`brainstorm`→`in_progress`→`next`→`near_term`→`long_term`→`shipped`→`retired`), `sort_order` within a bucket. `shipped` = delivered; `retired` = off the board without shipping (decided-against / split / absorbed / replaced) — keep the row. `project_id` (nullable) = the work-stream the feature belongs to; the GUI Flow view groups on it (NULL = Ungrouped) | INSERT/UPDATE |
| `feature_blockers` | roadmap dependency edges: one row = `feature_id` depends on `blocked_by` (prerequisite lands first). Directed, kept acyclic (GUI Flow view wires them; the card''s "depends on" picker sets them) | INSERT/DELETE the edge; set the whole set via `sc mem roadmap depends` |
| `documents` | content store — spec/doc bodies; `frozen=1` on ship (immutable); `render_path` = flat-file target | INSERT a new `seq` per stage; NEVER edit a frozen body |
| `flags` | open + resolved tasks; `feature_id` links a flag to the feature it blocks | INSERT to open; UPDATE `resolved=1` + `resolved_date` to close |
| `skills` / `flavor_skills` / `shell_skills` | skill catalogue + shared packs for standard flavors + per-shell packs for Bespoke shells; `resolved_shell_skills` is the effective read view | managed by engine; name any standard shell to change its flavor pack, or a Bespoke shell to change only itself, via `./sc skill grant/revoke` |
| `projects` / `project_shells` | project standing + shell linkage; a `projects` row also doubles as a work-stream that roadmap features attach to via `roadmap.project_id` (the Flow-view grouping) | UPDATE `standing`; INSERT to add |

`<self>` = your `shell_id` (in the boot doc''s ACTIVE SESSION block).

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
sc mem roadmap project <feature_id> <shortname|id>   # assign a work-stream (or ''none'' to clear)
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
visible to every shell. Persisting to git is an admin/GUI step, not yours.',
    0
)
ON CONFLICT(name) DO UPDATE SET
    description=excluded.description,
    category=excluded.category,
    command=excluded.command,
    common=excluded.common,
    content=excluded.content,
    is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted)
VALUES (
    'engine_surgery',
    'Procedure for changing the engine you are running — pull/reconcile/restart cadence and their costs, three-artifact engine-skill commits with a hermetic mirror render, migrating the live DB safely, and verifying claims about engine code against the remote rather than a possibly-stale checkout. SOURCE-REPO ONLY; a fork consumes the engine as a pinned dependency and never edits it. Load before touching .super-coder/ in the repo that owns it.',
    'craft',
    NULL,
    0,
    '# engine_surgery — changing the engine you are running

This repo IS the engine. Every shell here runs on the code it edits, reads a DB
it migrates, and is served by a process started from the tree it commits to.
That is surgery on a moving car, and it has one characteristic failure mode:
**a command answers confidently from a target you did not mean.**

**Fork shells never load this.** A fork consumes `.super-coder/` as a gitignored
dependency pinned by `engine.ref` and updates it with `./sc update`; it never
authors engine changes. Granted only in the source repo.

## The four trees, and which one bites

| tree | what it is | who keeps it current |
|---|---|---|
| your worktree | `.sc-worktrees/<shortname>` — your cwd, your branch | you; boot reports it as `sync:` |
| the main checkout | resolves your `./sc`, hosts the live DB, runs the server | admin / the FnB; boot reports it as `floor:` |
| the running process | code already imported — changes only on restart | the FnB |
| `origin/main` | the truth | whoever merged last |

`sc:11-21` derives the engine root from git''s **common dir**, so `./sc` from any
worktree reads the MAIN CHECKOUT. Being current in your own tree tells you
nothing about it. Read the `floor:` line in ACTIVE SESSION.

**Verify any claim about engine code against the remote:**

```
git show origin/main:<path>     # correct
./sc help | grep <thing>        # answers from the main checkout — may be stale
```

Three wrong answers in one session came from skipping that: a help query, a
pending-migration check that came back empty because it globbed a stale
migrations dir and nearly made a reconcile a silent no-op, and dormant PR
watches against a stale running floor.

## EDIT IN YOUR WORKTREE — scripted writes bypass the branch guard

`branch-guard.sh` blocks harness file-edit tools from writing to a
default-branch checkout. **It does not see writes made by a script.** A
`cd /home/j3d1/super-coder && python3 -c "...patch..."` lands on `main`,
uncommitted, with no warning — and your worktree stays clean, so `git status`
there reassures you nothing happened.

Recovery, if you find edits on the wrong tree:

```
git -C <main-checkout> diff > /tmp/x.patch
git apply /tmp/x.patch                                  # in your worktree
git -C <main-checkout> checkout -- <files>
```

Prefer the harness edit tools, which are guarded. If you script an edit, `cd` to
your worktree or use absolute worktree paths, and check `git status` in **both**
trees afterwards.

## Cadence — pull often, restart rarely

| action | cost | fixes |
|---|---|---|
| pull the main checkout | cheap, safe, no session impact | stale reads |
| apply pending migrations | low; back up first | stale DB rows |
| `./sc update` + restart | refuses on live Interface state; **restart kills live sessions** | stale running process |

Pull after every merge. Reconcile and restart at **sprint boundaries** — never
mid-sprint, because a restart kills working devs and swapping the floor under an
in-flight unit is its own hazard. The restart is the FnB''s call.

## Migrating the live DB

The DB you migrate is the one every shell is using and the server has open.

1. **Fast-forward the main checkout first.** Pending-migration checks glob its
   `migrations/` dir, so a stale tree reports nothing pending and the reconcile
   silently does nothing.
2. **Name the DB path explicitly.** `./sc migrate` from a worktree resolves to
   the main checkout''s DB and says so nowhere (issue #569). Prefer
   `python3 .super-coder/scripts/migrate.py <explicit-path>`.
3. **Back up first**, WAL-safe, via SQLite''s online backup rather than a file
   copy:

   ```python
   src = sqlite3.connect(LIVE); dst = sqlite3.connect(BACKUP)
   with dst: src.backup(dst)
   ```

4. **Data-only migrations are safe under a running server** (row updates, no
   DDL). Schema changes want the restart window.
5. **Verify by read-back**, not by the migrate command''s own output.

## Engine skill edits are a three-artifact commit

All three, or CI goes red even when tests pass:

1. the source asset at `.super-coder/assets/skills/<name>/SKILL.md`;
2. a **trailing reseed migration** so existing installations converge — full-body
   upsert, `INSERT … ON CONFLICT(name) DO UPDATE SET`, patterned on the most
   recent `*_reseed_*.sql`. Generate it FROM the asset rather than hand-writing
   it, and store the body exactly as the guards read it
   (`split("---", 2)[2].strip()`) — an unstripped body fails three freshness
   guards;
3. the re-rendered mirror.

**Render the mirror through the guard''s own hermetic path**, never from the live
DB, so it cannot drift from what CI rebuilds:

```python
import render_check as rc, flat
rc._build_tracked_db(db)                  # schema → migrations → content.sql
flat.render_visibility(con, root=rc.ACTIVE_ROOT)
```

In **local artifact mode** the mirror lives under the ignored `.sc-state/local/`
and is not in the diff — the commit is then two artifacts, and `render-check`
still proves the migration.

Run the guard **from your own worktree** — `./sc render-check` resolves to the
main checkout and will judge code you are not committing (flag #47):

```
python3 .super-coder/scripts/render_check.py
```

## Adding a source-repo-only skill

Seeds carry **skills, not grants**: `0001` inserts catalogue rows. Standard
shells resolve one shared `flavor_skills` pack; Bespoke shells resolve their own
`shell_skills` rows. Newly-added common skills join every flavor/Bespoke pack
on update. Opt-ins remain ungranted until assigned.

A skill with **`common: false`** seeds into a fork''s catalogue without entering
any pack. Grant it here by naming one shell:

```
./sc skill grant <name> <shell> [<shell>…]
```

Naming a standard shell changes its whole flavor pack. Naming a Bespoke shell
changes only that shell.

## Stance

- A command reporting success against a target you did not intend is the house
  defect. Name the target; verify the effect.
- Never `SC_ADMIN=1` past a gate to save a step — the publish path is gated on
  purpose.
- Never auto-sync the main checkout from a shell. `sync_worktree` may
  `reset --hard` a shell base; the main checkout is the running server''s tree and
  a reset discards whatever the operator has in flight.
- Assert before you replace; read back after you write. A non-matching string
  replace does nothing and reports success.',
    0
)
ON CONFLICT(name) DO UPDATE SET
    description=excluded.description,
    category=excluded.category,
    command=excluded.command,
    common=excluded.common,
    content=excluded.content,
    is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted)
VALUES (
    'local_skill_management',
    'Create, persist, assign, and remove fork-specific skills — the correct authoring path so skills survive snapshot/rebuild cycles.',
    'substrate',
    NULL,
    0,
    '# local_skill_management — fork-specific skills that survive

Fork-specific skills live in the DB and persist via `.sc-state/content.sql`
(the snapshot). The asset file under `.super-coder/assets/skills/<name>/` is
the **authoring source only** — it sits in gitignored engine territory, and
that is safe: the engine/local boundary is the seed migration (0001,
upstream-owned in a fork), not asset-file presence. The snapshot serializes
your skill to content.sql whether or not the asset file is kept, and
`sc update` neither manifests it nor heals over its DB row. **content.sql =
the durable form; the asset file = your editor.**

The path: **file -> seed -> grant -> snapshot -> commit**.

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
   own) into the active snapshot (`.sc-state/content.sql` in tracked mode,
   `.sc-state/local/content.sql` in local mode) — what survives `sc update` and
   `sc rebuild`; the row + flavor/Bespoke grants reconstruct from content.sql.
   Skip this -> the skill is lost on next update.

5. **Finish.** Run `sc render-check` first — hermetic rebuild, fails if the
   `skills_sc/` mirror drifts from the DB render (the CI guard; see the
   `snapshot` skill). In tracked mode, stage `.sc-state/content.sql` +
   `skills_sc/` together. In local mode both stay ignored; only engine-owned
   assets/migrations are committed.

## Updating a skill

Edit the asset file -> repeat seed -> snapshot -> commit (steps 2, 4, 5).
Asset file gone (removed / authored elsewhere) -> recreate it from the DB body
first: `sc sql "SELECT content FROM skills WHERE name=''<name>''"`.

## Assigning an existing skill

```bash
sc skill grant <skill_name> <shell>...
```
Name one standard shell to update its whole flavor, or name a Bespoke shell to
update only that shell. Then `SC_ADMIN=1 sc snapshot && SC_ADMIN=1 sc render`
and commit the resulting artifacts.

## Removing a skill

1. **Soft-delete the row + revoke its grants:**
   ```bash
   sc skill rm <skill_name>
   ```
   Refuses engine skills — the seed resurrects those on next update/rebuild.
   Engine skill this fork has superseded -> retire fork-wide:
   `sc skill retire <name>` (writes the tracked
   `.sc-state/skills_retired.json`, which rides updates; `sc skill unretire`
   reverses). Flavor/Bespoke removal -> `sc skill revoke`.

2. **Remove the asset file** (`.super-coder/assets/skills/<name>/`) —
   otherwise the next `sc seed-skills` re-inserts the skill.

3. **Snapshot, render, commit:**
   ```bash
   SC_ADMIN=1 sc snapshot && SC_ADMIN=1 sc render
   ```

## How the GUI organizes skills

Shells → Skill Assignments shows the full catalogue in sections. Each standard
flavor appears once; Bespoke shells appear individually.

- **Repo skills** — lead section: skills authored in this fork. Membership is
  *derived* — a skill the engine seed doesn''t own is repo-local. Same rule
  snapshot.py uses to decide what serializes into `.sc-state/content.sql`, so
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
    description=excluded.description,
    category=excluded.category,
    command=excluded.command,
    common=excluded.common,
    content=excluded.content,
    is_deleted=0;
