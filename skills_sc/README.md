---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# Skills

> The substrate's skill catalogue, rendered from the DB. Per-shell grants live in `.claude/skills/` (rebuilt at boot).

- [`bootstrap`](skills_sc/bootstrap.md) — First-run orientation for a shell in a repo. Run ONCE when the boot doc shows "## FIRST RUN" (bootstrapped=0), or whenever the repo map is empty. Maps the repo, reads the map + your identity, sets your current_state, marks you oriented. Do this BEFORE other work on a fresh fork.
- [`db_map`](skills_sc/db_map.md) — Schema map + reusable SQL for super-coder's shell_db.db. Check before composing any DB query — identity, memory, roadmap, documents, flags, skills.
- [`snapshot`](skills_sc/snapshot.md) — Persist DB work to git-tracked text — when and how to run make snapshot / make render before committing. The .db is a cache; text is the source of truth.
- [`surface_catalogue`](skills_sc/surface_catalogue.md) — Read the host repo via the dr_* catalogue (files, languages, deps, env) BEFORE grepping or walking the tree. Run `make map` to refresh it. Use to orient in an unfamiliar repo fast.
