---
name: snapshot
description: Serialize DB work via sc snapshot / sc render under the instance artifact policy. Tracked mode publishes through Git; local mode persists under .sc-state/local without creating content commits.
category: substrate
command: sc snapshot
common: false
---

# snapshot — serialize the DB back to text

Live `shell_db.db` = the single source of truth shared by every shell; a
`sc mem` write is durable + visible to all shells the instant it commits. The
`.db` is gitignored and reconstructs from schema, migrations, and the active
per-instance snapshot on `sc rebuild` —
an edit not yet serialized is discarded by a rebuild.

Serializing is an admin/GUI operation, NOT a per-write shell step: it writes
`.sc-state/` + the flat `_sc` mirror into the shared MAIN worktree, and from a
shell's linked worktree it churns and collides with other shells. `sc snapshot`
and `sc render flat` refuse unless `SC_ADMIN=1` (GUI/API, `install`, `update`,
and `render-check` set it for you). A shell does not run them; its writes are
captured when admin snapshots (GUI **Publish**/Snapshot button, or
`SC_ADMIN=1 sc snapshot`) before a rebuild. The rest of this skill = the
admin/GUI path.

## The three text serializations

| File(s) | What | Propagates? | Written by |
|---|---|---|---|
| `schema.sql` | the v1 baseline schema | yes (forks) | hand, rarely |
| `migrations/*.sql` | ordered schema + **system content** deltas (e.g. the skills catalogue) | yes (forks) | author / `sc seed-skills` |
| `.sc-state/content.sql` (tracked mode) or `.sc-state/local/content.sql` (local mode) | **this repo's** per-instance content + memory — shells, seed/L&S, decisions, roadmap, documents, flags, projects, skill grants | no (instance-only) | `sc snapshot` |

The split: system content propagates via migrations; per-instance content stays
in the snapshot. Skill *bodies* = system (migration); which shell is *granted*
a skill = per-instance (snapshot).

`artifact_mode` lives in `.super-coder/instance.json` and accepts `tracked` or
`local`; downstream forks default to `tracked`. Local mode still snapshots and
renders, but writes beneath `.sc-state/local/` (ignored) and Publish creates no
Git branch, commit, or PR.

## When admin serializes (the GUI Publish button does all of this)

All commands require `SC_ADMIN=1`, run from the main checkout.

1. `SC_ADMIN=1 sc snapshot` -> dumps the per-instance tables to the active
   snapshot path. Deterministic DELETE-then-INSERT in PK order ->
   re-running is byte-identical -> clean diffs.

2. `SC_ADMIN=1 sc render` -> regenerates the flat `_sc` files
   (`specs_sc/`, `docs_sc/`, `skills_sc/`, `roadmap_sc.md`) from the DB. Run
   after changing a document body, the roadmap, or skills. Incremental —
   unchanged files not rewritten. (`.claude/skills/` rebuilds at boot and is
   gitignored — not rendered here.)

3. Verify reproducibility: `sc rebuild && sc verify` -> DB rebuilds from text
   alone, byte-for-byte.
   Before committing any `_sc` render: `sc render-check` — rebuilds the DB
   hermetically from text and fails if the committed mirror drifts from that
   render (the CI guard, run locally). A plain `sc render` reads the *live* DB,
   which can lag the source just edited (skill-catalogue trap below);
   `render-check`'s rebuild-first catches the stale mirror the live-DB render
   silently passed.

4. In tracked mode, Publish writes
   `.sc-state/content.sql`, `.sc-state/engine.ref`, and the `_sc` files to the
   main checkout root (where the shared engine + DB live), not your worktree —
   they are not yours to stage. GUI **Publish** = snapshot -> render -> commit
   -> push -> PR on `sc_gui_content`; the admin shell on `main` may commit them
   directly. In local mode it only snapshots/renders and reports that nothing
   was published. NEVER commit the `.db` or anything under the gitignored
   `.super-coder/` engine dir. (super-coder SOURCE repo only: `schema.sql` +
   `migrations/` are tracked and committed here too.)

## Authoring vs. snapshotting

- **Per-instance content** (your memory, this repo's roadmap/docs): edit the
  DB -> `sc snapshot`. The snapshot is the canonical reproducer.
- **Skill catalogue** (system, propagates): edit
  `assets/skills/<name>/SKILL.md` -> `sc seed-skills` — upserts the live DB
  *and* (source repo only) regenerates the seed migration. Not the snapshot.
  See `seed_skills.py`.
  - Sequence: `sc seed-skills && sc render`, then `sc render-check` before
    committing. In tracked mode commit the regenerated
    `migrations/0001_seed_skills.sql` + re-rendered `skills_sc/` mirror together.
    In local mode only the migration is public; the mirror stays ignored.

Steps 1–3 = durability (a `sc rebuild` cannot lose serialized work). Step 4 =
the GUI Publish button; you rarely commit this text by hand.

## Related skills

This skill owns the render/snapshot pipeline + the `render-check` guard:

- `self_update` — `sc update` re-renders the same `_sc` files; its verify step
  runs `render-check` before committing the engine bump.
- `local_skill_management` — fork-local skills persist via `sc snapshot`; run
  `render-check` before committing the `skills_sc/` mirror.
- `migration_management` — a **content-seed** migration (skills, flavor
  defaults) changes what renders; rebuild + render + `render-check` after.
- `docs` / `spec` — document bodies live in the DB, render to `docs_sc/` /
  `specs_sc/`; authored via `sc mem doc`, serialized here.
