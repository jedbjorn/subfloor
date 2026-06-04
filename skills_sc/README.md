---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# Skills

> The substrate's skill catalogue, rendered from the DB. Per-shell grants live in `.claude/skills/` (rebuilt at boot).

- [`api-design`](skills_sc/api-design.md) — REST/HTTP API design patterns — resource naming, status codes, pagination, filtering, errors, versioning, idempotency. Use when designing or reviewing API endpoints.
- [`blueprint`](skills_sc/blueprint.md) — Turn a one-line objective into a sequenced construction plan — decompose into steps, find the dependency order, mark what can run in parallel, name the verification gate. Use before multi-step builds.
- [`bootstrap`](skills_sc/bootstrap.md) — First-run orientation for a shell in a repo. Run ONCE when the boot doc shows "## FIRST RUN" (bootstrapped=0), or whenever the repo map is empty. Maps the repo, reads the map + your identity, sets your current_state, marks you oriented. Do this BEFORE other work on a fresh fork.
- [`database-migrations`](skills_sc/database-migrations.md) — Database migration safety + how super-coder's own migrations work (schema.sql baseline + ordered migrations/ deltas + ledger). Use when altering tables, adding columns, or running backfills — in the host repo's DB or super-coder's.
- [`db_map`](skills_sc/db_map.md) — Schema map + reusable SQL for super-coder's shell_db.db. Check before composing any DB query — identity, memory, roadmap, documents, flags, skills.
- [`docs`](skills_sc/docs.md) — Author or review docs & specs in super-coder. The DB owns the body (documents table); roadmap tracks specs (the dev cycle), the Docs tab holds docs. Use whenever asked for a doc, spec, report, design, RFC, ADR, runbook, or to edit existing ones.
- [`flags`](skills_sc/flags.md) — Track blockers as flags — surface open ones, open new ones, resolve them. Link a flag to the roadmap feature it blocks. Mirrors the GUI Flags tab. Use when something blocks progress or needs follow-up.
- [`git`](skills_sc/git.md) — Git conventions for a super-coder shell — one repo, one cwd. Branch before committing, open PRs (never merge without the FnB's OK), attribute commits per-shell. Use before any git work.
- [`memory`](skills_sc/memory.md) — How this shell writes its memory — current_state, session narrative, seed, L&S, decisions. Write as it happens, not at close. Use to know WHEN and HOW to persist identity/work memory, and the caps.
- [`onboard`](skills_sc/onboard.md) — One-time, FnB-supervised ingest of a repo's EXISTING docs/specs into the DB + roadmap backfill. The only time content flows file→DB. Run once after bootstrap on a fork that has existing documentation. Planning shell's job.
- [`snapshot`](skills_sc/snapshot.md) — Persist DB work to git-tracked text — when and how to run make snapshot / make render before committing. The .db is a cache; text is the source of truth.
- [`surface_catalogue`](skills_sc/surface_catalogue.md) — Read the host repo via the dr_* catalogue (files, languages, deps, env) BEFORE grepping or walking the tree. Run `make map` to refresh it. Use to orient in an unfamiliar repo fast.
