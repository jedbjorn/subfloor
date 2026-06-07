---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# snapshot

Persist DB work to git-tracked text — when and how to run ./sc snapshot / ./sc render before committing. The .db is a cache; text is the source of truth.

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

4. **Commit** the text: `.sc-state/content.sql` + `.sc-state/engine.ref` and the
   `_sc` files. Never commit the `.db` or anything under the gitignored
   `.super-coder/` engine dir. (In the super-coder SOURCE repo only, `schema.sql`
   + `migrations/` are tracked and committed here too.)

## Authoring vs. snapshotting

- **Per-instance content** (your memory, this repo's roadmap/docs): edit the DB,
  then `./sc snapshot`. The snapshot is the canonical reproducer.
- **Skill catalogue** (system, propagates): edit `assets/skills/<name>/SKILL.md`,
  then `./sc seed-skills` to regenerate the seed migration — **not** the
  snapshot. See `seed_skills.py`.

> The commit→PR automation (B6) will run snapshot → render → commit → PR
> automatically per shell. Until then it is this manual ritual.
