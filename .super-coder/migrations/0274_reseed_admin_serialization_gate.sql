-- 0274 — snapshot and issue_reporting guidance after the SC_ADMIN flag was retired.
-- Reseed engine skill bodies edited after 0001 was applied.

BEGIN;

UPDATE skills SET content = '# snapshot — serialize the DB back to text

Live `shell_db.db` = the single source of truth shared by every shell; a
`sc mem` write is durable + visible to all shells the instant it commits. The
`.db` is gitignored and reconstructs from schema, migrations, and
`.sc-state/local/content.sql` on `sc rebuild` —
an edit not yet serialized is discarded by a rebuild.

Serializing is an admin/GUI operation, NOT a per-write shell step: it writes
the shared instance''s gitignored local cache. `sc snapshot` and `sc render`
run from the main checkout; the dispatcher refuses them from a linked shell
worktree. The GUI **Save locally** button, `install`, `update`, and
the authorized maintenance flows run them for you. `render-check` only checks
for drift; it does not refresh the snapshot or mirror. A working shell does not run them; its writes
are captured when admin saves locally before a rebuild. The rest of this skill
= the admin/GUI path.

## The three text serializations

| File(s) | What | Propagates? | Written by |
|---|---|---|---|
| `schema.sql` | the v1 baseline schema | yes (forks) | hand, rarely |
| `migrations/*.sql` | ordered schema + **system content** deltas (e.g. the skills catalogue) | yes (forks) | author / `sc seed-skills` |
| `.sc-state/local/content.sql` | **this repo''s** per-instance content + memory — shells, seed/L&S, decisions, roadmap, documents, flags, projects, skill grants | no (instance-only, gitignored) | `sc snapshot` |

The split: system content propagates via migrations; per-instance content stays
in the snapshot. Skill *bodies* = system (migration); which shell is *granted*
a skill = per-instance (snapshot).

Generated artifacts always live beneath `.sc-state/local/`. A legacy
`artifact_mode: tracked` setting is accepted only as upgrade input and resolves
to local; mode switching and Git publication are retired.

## When admin serializes

All commands run from the main checkout. The gate reads the caller''s shell
token: the Admin shell and the host operator''s terminal pass; any other
launched shell is refused.

1. `./sc snapshot` -> dumps the per-instance tables to the active
   local snapshot path. Deterministic DELETE-then-INSERT in PK order makes
   re-running byte-identical.

2. `./sc render flat` -> regenerates the flat `_sc` files
   (`renders/specs_sc/`, `renders/docs_sc/`, `renders/skills_sc/`,
   `renders/roadmap_sc.md`) beneath `.sc-state/local/`. Run
   after changing a document body, the roadmap, or skills. Incremental —
   unchanged files not rewritten. (`.claude/skills/` rebuilds at boot and is
   gitignored — not rendered here.)

3. Verify reproducibility: `./sc verify` -> rebuilds from local text in an
   isolated disposable checkout without replacing the live DB.
   `sc render-check` rebuilds the DB hermetically from text and fails if the
   local mirror drifts from that render. `./sc render flat` reads the *live* DB,
   which can lag the source just edited (skill-catalogue trap below);
   `render-check`''s rebuild-first catches the stale mirror the live-DB render
   silently passed.

4. Do not stage the output. Generated snapshots and renders are gitignored.
   Only authored engine source and explicit migrations belong in Git.

## Authoring vs. snapshotting

- **Per-instance content** (your memory, this repo''s roadmap/docs): edit the
  DB -> `./sc snapshot`. The local DB is primary; the ignored snapshot is its
  rebuild source.
- **Skill catalogue** (system, propagates): edit
  `assets/skills/<name>/SKILL.md` -> `sc seed-skills` — upserts the live DB
  *and* (source repo only) regenerates the seed migration. Not the snapshot.
  See `seed_skills.py`.
  - Sequence: `sc seed-skills && ./sc render flat`, then `sc render-check`. Commit the
    regenerated `migrations/0001_seed_skills.sql`; the mirror stays ignored.

Steps 1–3 are the local durability path. There is no generated-artifact
publication path.

## Related skills

This skill owns the render/snapshot pipeline + the `render-check` guard:

- `self_update` — `sc update` refreshes the same local `_sc` files.
- `fork_skill_design` — DB-canonical fork-local skills persist via the local
  snapshot.
- `engine_migrations` — a **content-seed** migration (skills, flavor defaults)
  changes what renders; rebuild + render + `render-check` after.
- Document bodies live in the DB, render to `docs_sc/` / `specs_sc/`;
  authored via `sc mem doc`, serialized here.' WHERE name = 'snapshot';

UPDATE skills SET content = '# issue_reporting — the backwards flow

An engine defect fixed upstream reaches every fork via `sc update`; worked
around silently, every fork re-derives the workaround. File the issue while
the failure is on screen — NEVER batch to session end.

A workaround IS a report: deviating from a skill''s steps, wrapping a command,
or hand-patching state to proceed -> you hold the exact repro; file it now.

## Boundary — engine vs fork

| Where | What |
|---|---|
| **Upstream — file it** | anything the engine materializes/owns: `.super-coder/`, `sc` + every subcommand, engine skills (this catalogue), the boot doc render, the sandbox / dev kit, `sc update` + migrations, the `_sc` API + `sc mem` |
| **Fork — don''t** | the repo''s app code, DB-canonical fork-local skills, operator-owned host config |

Unsure -> "would the same problem hit any other fork?" yes = upstream.

## Triggers

Each row = a real engine defect filed by a fork shell doing ordinary work.
Match the left column -> file.

| You hit | Real case |
|---|---|
| A `sc` command fails out of the box | `sc verify` always aborted — its own render step failed the Admin serialization gate it never satisfied (#227) |
| A command exits green without doing the work | `sc test` silently fell back to unittest when pytest was missing — green-washed suites (#219) |
| The documented remedy is a closed loop | `sc lint` said "run `sc deps` first," but deps skips pip in the sandbox — tool unobtainable from inside the box (#246) |
| A skill instructs tools/paths your seat doesn''t have | a sandbox skill drove raw host-only `ssh`/`virsh` paths (#248) |
| A skill contradicts what the engine actually does | skills still taught raw `sqlite3` against the substrate DB after memory went API-only (#226) |
| The API refuses what the skills document | `sc mem doc add` 400''d standalone docs the docs + onboard skills both document (#245) |
| A permission wall mid-workflow | a dev shell could read a planner-owned feature but 404''d advancing its status (#224) |
| Every write suddenly 401s | rebuild didn''t re-mint api_keys — all live shells locked out until an API bounce (#214) |
| `sc update` / migrate wedges or half-applies | migration failed partway, retry died on `duplicate column name` (#229); update aborted crossing a commit that deleted an engine file (#209) |
| A structural foot-gun keeps re-biting you | the cwd trap — `cd` to root for `sc`, then bare git hit the wrong tree, "my edits vanished" (#225) |
| The sandbox can reach something it shouldn''t | `do_push` src/dest weren''t contained — sandbox→host escape (#228) |

Stale guidance (skill says X, engine does Y) files the same as a crash.

## Capture — while the failure is on screen

- **engine ref** = `sc engine-ref` — first line of every report (Subfloor''s engine commit)
- **staleness** = compare that ref to upstream head:
  `git ls-remote https://github.com/jedbjorn/subfloor HEAD` — write
  `current` or `behind head <sha7>`. Behind + the symptom is a missing
  command or a skill/engine mismatch -> the fix may already be shipped:
  ask your FnB for `sc update` first, and file only if the defect
  survives the update (or updating isn''t an option — then the staleness
  note carries that caveat). Triage reads this line to tell a live
  engine defect from a stale fork build.
- **fork + seat**: repo name, shell flavor, sandbox/host
- **ran / followed**: the exact command, or skill name + step
- **expected vs actual**: exact output, trimmed to the failing lines
- **workaround**: what unblocked you, or "blocked, none found"

The issue is public: NEVER paste api keys, tokens, secrets, or private paths.

## File it

```bash
# 1. dedup — someone may have hit it first
gh issue list --repo jedbjorn/subfloor --search "<symptom keywords>" --state all

# 2. file — title: [<fork>] <area>: <one-line symptom>
gh issue create --repo jedbjorn/subfloor \
  --title "[<fork>] <area>: <symptom>" \
  --body "$(cat <<''EOF''
- engine ref: <sha from .sc-state/engine.ref> · <current | behind head <sha7>>
- fork/seat: <repo> · <shell flavor> · <sandbox|host>

**Ran / followed:** <command or skill+step>
**Expected:** <what the docs/skill promise>
**Actual:** <exact trimmed output>
**Workaround:** <what unblocked you, or "blocked">
EOF
)"
```

`jedbjorn/subfloor` = engine upstream; confirm: `git remote get-url super-coder`.

Dedup hit -> comment your engine ref + repro on the existing issue; do NOT
file a duplicate.

No `gh` / no network from your seat -> save the identical body as a fork flag:
`sc mem flag open "[Engine] <symptom> | Blocker for: <x>" --name UP-###`, then
message the **admin** shell to relay it upstream.

## Authorized curation recommendation

The `curate` skill has one FnB-authorized exception to the normal enhancement
gate below. When a recurring L&S cluster may warrant a reusable upstream skill,
the curating shell may search and file the recommendation directly without
asking the FnB first.

Search all upstream issues before opening anything. Add evidence to a matching
recommendation, or open one titled `skills: recommend <topic>` containing the
trigger, repeated incidents, proposed ownership boundary, expected users, why
existing skills do not cover it, and a compact candidate procedure.

This route recommends; it never creates or promotes a skill. Keep one compressed
L&S entry until a reviewed upstream skill ships and is granted. If issue search
or creation is unavailable, surface the failure to the FnB, keep the L&S, and
create no local skill or asset. Deliberate fork-specific authoring remains the
Planner-owned workflow in `fork_skill_design`.

## Rules

- One defect per issue. Batch nothing.
- Observed failure = the bar for filing unasked; enhancement ideas ("the
  engine should…") go to your FnB first, except the authorized curation
  recommendation route above.
- Filing ≠ unblocked: defect blocks work -> also open a fork flag linking the
  issue URL.' WHERE name = 'issue_reporting';

COMMIT;
