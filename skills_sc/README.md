---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# Skills

> The substrate's skill catalogue, rendered from the DB. Per-shell grants live in `.claude/skills/` (rebuilt at boot).

- [`db_map`](skills_sc/db_map.md) — Schema map + reusable SQL for super-coder's shell_db.db. Check before composing any DB query — identity, memory, roadmap, documents, flags, skills.
- [`snapshot`](skills_sc/snapshot.md) — Persist DB work to git-tracked text — when and how to run make snapshot / make render before committing. The .db is a cache; text is the source of truth.
