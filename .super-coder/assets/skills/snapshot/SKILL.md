---
name: snapshot
description: Refresh the gitignored local DB snapshot and flat renders. Generated instance state never enters Git.
category: substrate
command: sc snapshot
common: false
---

# snapshot — serialize the DB back to text

Live `shell_db.db` = the single source of truth shared by every shell; a
`sc mem` write is durable + visible to all shells the instant it commits. The
`.db` is gitignored and reconstructs from schema, migrations, and
`.sc-state/local/content.sql` on `sc rebuild` —
an edit not yet serialized is discarded by a rebuild.

Serializing is an admin/GUI operation, NOT a per-write shell step: it writes
the shared instance's gitignored local cache. `sc snapshot` and `sc render`
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
| `.sc-state/local/content.sql` | **this repo's** per-instance content + memory — shells, seed/L&S, decisions, roadmap, documents, flags, projects, skill grants | no (instance-only, gitignored) | `sc snapshot` |

The split: system content propagates via migrations; per-instance content stays
in the snapshot. Skill *bodies* = system (migration); which shell is *granted*
a skill = per-instance (snapshot).

Generated artifacts always live beneath `.sc-state/local/`. A legacy
`artifact_mode: tracked` setting is accepted only as upgrade input and resolves
to local; mode switching and Git publication are retired.

## When admin serializes

All commands run from the main checkout. Admin must explicitly set `SC_ADMIN=1`
for snapshot and render; the Admin shell identity alone does not satisfy the gate.

1. `SC_ADMIN=1 ./sc snapshot` -> dumps the per-instance tables to the active
   local snapshot path. Deterministic DELETE-then-INSERT in PK order makes
   re-running byte-identical.

2. `SC_ADMIN=1 ./sc render flat` -> regenerates the flat `_sc` files
   (`renders/specs_sc/`, `renders/docs_sc/`, `renders/skills_sc/`,
   `renders/roadmap_sc.md`) beneath `.sc-state/local/`. Run
   after changing a document body, the roadmap, or skills. Incremental —
   unchanged files not rewritten. (`.claude/skills/` rebuilds at boot and is
   gitignored — not rendered here.)

3. Verify reproducibility: `./sc verify` -> rebuilds from local text in an
   isolated disposable checkout without replacing the live DB.
   `sc render-check` rebuilds the DB hermetically from text and fails if the
   local mirror drifts from that render. `SC_ADMIN=1 ./sc render flat` reads the *live* DB,
   which can lag the source just edited (skill-catalogue trap below);
   `render-check`'s rebuild-first catches the stale mirror the live-DB render
   silently passed.

4. Do not stage the output. Generated snapshots and renders are gitignored.
   Only authored engine source and explicit migrations belong in Git.

## Authoring vs. snapshotting

- **Per-instance content** (your memory, this repo's roadmap/docs): edit the
  DB -> `SC_ADMIN=1 ./sc snapshot`. The local DB is primary; the ignored snapshot is its
  rebuild source.
- **Skill catalogue** (system, propagates): edit
  `assets/skills/<name>/SKILL.md` -> `sc seed-skills` — upserts the live DB
  *and* (source repo only) regenerates the seed migration. Not the snapshot.
  See `seed_skills.py`.
  - Sequence: `sc seed-skills && SC_ADMIN=1 ./sc render flat`, then `sc render-check`. Commit the
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
  authored via `sc mem doc`, serialized here.
