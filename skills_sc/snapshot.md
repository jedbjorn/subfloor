---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# snapshot

Persist DB work to git-tracked text — when and how to run ./sc snapshot / ./sc render. The .db is a cache; text is the source of truth. Snapshot is durability; the GUI Publish button commits + PRs it.

**Category:** substrate  ·  **Command:** `./sc snapshot`

---

# snapshot — serialize the DB back to text

The live `shell_db.db` is **gitignored and disposable**. Everything in it
reconstructs from git-tracked text. So a DB edit that is not serialized is lost
on the next `./sc rebuild`. This skill is the "save my work" step.

## The three text serializations

| File(s) | What | Propagates? | Written by |
|---|---|---|---|
| `schema.sql` | the v1 baseline schema | yes (forks) | hand, rarely |
| `migrations/*.sql` | ordered schema + **system content** deltas (e.g. the skills catalogue) | yes (forks) | author / `./sc seed-skills` |
| `.sc-state/content.sql` | **this repo's** per-instance content + memory — shells, seed/L&S, decisions, roadmap, documents, flags, projects, skill grants. Tracked, fork-owned, kept OUTSIDE the gitignored engine dir | no (stays local) | `./sc snapshot` |

The split that matters: **system content propagates via migrations; per-instance
content stays in the snapshot.** Skill *bodies* are system (migration); which
shell is *granted* a skill is per-instance (snapshot).

## After editing the DB

1. **`./sc snapshot`** — dumps the per-instance tables to
   `.sc-state/content.sql` (deterministic DELETE-then-INSERT in PK order, so
   re-running is byte-identical → clean diffs). Do this after ANY change to
   identity, memory, roadmap, documents, flags, projects, or grants.

2. **`./sc render`** — regenerates the tracked flat `_sc` visibility files
   (`specs_sc/`, `docs_sc/`, `skills_sc/`, `roadmap_sc.md`) from the DB. Run it
   when you changed a document body, the roadmap, or skills. Render is
   incremental — unchanged files aren't rewritten. (`.claude/skills/` is
   rebuilt at boot, not here — it's gitignored.)

3. **Verify the rebuild reproduces:** `./sc rebuild && ./sc verify`. The DB
   should rebuild from text alone, byte-for-byte.

   **Before committing any `_sc` render, run `./sc render-check`.** It rebuilds
   the DB hermetically (from text) and fails if the committed flat mirror drifts
   from that render — the CI guard, reproduced locally. A plain `./sc render`
   renders from your *live* DB, which can lag the source you just edited (see the
   skill-catalogue trap below); `render-check`'s rebuild-first is what catches
   the stale mirror your live-DB render silently passed.

4. **Publish** the text — don't hand-commit it. `./sc snapshot`/`render` write
   `.sc-state/content.sql`, `.sc-state/engine.ref`, and the `_sc` files to the
   **main checkout root** (where the shared engine + DB live), not your worktree,
   so they aren't yours to stage from a shell branch. The GUI **Publish** button
   commits them and opens one PR (snapshot → render → commit → push → PR on
   `sc_gui_content`); the admin shell on `main` can also commit them directly.
   Never commit the `.db` or anything under the gitignored `.super-coder/` engine
   dir. (In the super-coder SOURCE repo only, `schema.sql` + `migrations/` are
   tracked and committed here too.)

## Authoring vs. snapshotting

- **Per-instance content** (your memory, this repo's roadmap/docs): edit the DB,
  then `./sc snapshot`. The snapshot is the canonical reproducer.
- **Skill catalogue** (system, propagates): edit `assets/skills/<name>/SKILL.md`,
  then `./sc seed-skills` to regenerate the seed migration — **not** the
  snapshot. See `seed_skills.py`.
  - **The stale-mirror trap:** `seed-skills` writes the new body into the
    *migration*, but your **live DB still holds the old body** until you apply
    it. So `./sc render` now renders the *stale* skill into `skills_sc/<name>.md`
    and passes — while CI's hermetic rebuild (new body) drifts and goes red.
    Sequence is **`./sc seed-skills && ./sc rebuild && ./sc render`, then
    `./sc render-check`** before committing. Commit the regenerated
    `migrations/0001_seed_skills.sql` *and* the re-rendered `skills_sc/` mirror
    together — the migration without the mirror is the drift.

> Steps 1–3 are durability — serialize so a `./sc rebuild` can't lose your work.
> Step 4 is the GUI **Publish** button: it runs snapshot → render → commit →
> push → PR on the `sc_gui_content` branch, so you rarely commit this text by
> hand. The serialization lives at the main checkout root, not a worktree.

## Related skills

This skill owns the render/snapshot pipeline and the `render-check` guard; the
skills that *feed* it link back here:

- `self_update` — `./sc update` re-renders these same `_sc` files; its verify
  step runs `render-check` (this skill) before committing the engine bump.
- `local_skill_management` — fork-local skills persist via `./sc snapshot`; run
  `render-check` before committing the `skills_sc/` mirror.
- `migration_management` — a **content-seed** migration (skills, flavor
  defaults) changes what renders; rebuild + render + `render-check` after.
- `docs` / `spec` — document bodies live in the DB and render to `docs_sc/` /
  `specs_sc/`; authored via `./sc mem doc`, serialized here.
