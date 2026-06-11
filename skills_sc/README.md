---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# Skills

> The substrate's skill catalogue, rendered from the DB. Per-shell grants live in `.claude/skills/` (rebuilt at boot).

- [`api-design`](skills_sc/api-design.md) — REST/HTTP API design patterns — resource naming, status codes, pagination, filtering, errors, versioning, idempotency. Use when designing or reviewing API endpoints.
- [`blueprint`](skills_sc/blueprint.md) — Turn a one-line objective into a sequenced construction plan — decompose into steps, find the dependency order, mark what can run in parallel, name the verification gate. Use before multi-step builds.
- [`bootstrap`](skills_sc/bootstrap.md) — First-run orientation for a shell in a repo. Run ONCE when the boot doc shows "## FIRST RUN" (bootstrapped=0). Read the repo map + your identity, set your current_state, mark yourself oriented. Do this BEFORE other work on a fresh fork.
- [`cartographer`](skills_sc/cartographer.md) — Own the repo map. Configure mapping to THIS repo, wire the auto-remap git hooks, and heal both when the repo or automation drifts. The cartographer's job alone — no working shell maps. Run on first boot, and again whenever the map looks wrong.
- [`database-migrations`](skills_sc/database-migrations.md) — Database migration safety + how super-coder's own migrations work (schema.sql baseline + ordered migrations/ deltas + ledger). Use when altering tables, adding columns, or running backfills — in the host repo's DB or super-coder's.
- [`db_map`](skills_sc/db_map.md) — Schema map + reusable SQL for super-coder's shell_db.db. Check before composing any DB query — identity, memory, roadmap, documents, flags, skills.
- [`dev_kit`](skills_sc/dev_kit.md) — What the sandbox dev kit provides + how to drive it — ./sc deps, ./sc test, ./sc lint, ./sc typecheck, the .venv tools, rg/sqlite3, the baked browser, and the container/host app boundary. Use when building or testing in a fork.
- [`docs`](skills_sc/docs.md) — Author or review docs & specs in super-coder. The DB owns the body (documents table); roadmap tracks specs (the dev cycle), the Docs tab holds docs. Use whenever asked for a doc, spec, report, design, RFC, ADR, runbook, or to edit existing ones.
- [`flags`](skills_sc/flags.md) — Track blockers as flags — surface open ones, open new ones, resolve them. Link a flag to the roadmap feature it blocks. Mirrors the GUI Flags tab. Use when something blocks progress or needs follow-up.
- [`git`](skills_sc/git.md) — Git conventions for a super-coder shell — one repo, one cwd. Branch before committing, open PRs (never merge without the FnB's OK), attribute commits per-shell. Use before any git work.
- [`memory`](skills_sc/memory.md) — How this shell writes its memory — current_state, session narrative, seed, L&S, decisions. Write as it happens, not at close. Use to know WHEN and HOW to persist identity/work memory, and the caps.
- [`messaging`](skills_sc/messaging.md) — Shell-to-shell inbox — send a markdown message to another shell, check your unread inbox, mark messages read. Direct SQL against shell_db.db (no API in v1). Use to coordinate with another shell; the recipient sees it on its next boot via the STATUS Inbox count.
- [`onboard`](skills_sc/onboard.md) — One-time, FnB-supervised ingest of a repo's EXISTING docs/specs into the DB + roadmap backfill. The only time content flows file→DB. Run once after bootstrap on a fork that has existing documentation. Planning shell's job.
- [`redline_review`](skills_sc/redline_review.md) — Review PNG redlines from the shared scratch dir — find the image by filename match, describe what is seen, interpret intent, propose an implementation, then hold for approval before writing code. Use when the FnB says "redlines".
- [`self_update`](skills_sc/self_update.md) — Update this fork's super-coder engine in place — fetch + materialize new code + migrations, keep all your memory; roll back a bad update soundly. The shell hands off to its own next boot. Use when a super-coder update is available.
- [`snapshot`](skills_sc/snapshot.md) — Persist DB work to git-tracked text — when and how to run ./sc snapshot / ./sc render before committing. The .db is a cache; text is the source of truth.
- [`surface_catalogue`](skills_sc/surface_catalogue.md) — Read the host repo via the dr_* catalogue (files, languages, deps, env) BEFORE grepping or walking the tree. Query first, lazy-load the few files it points at. Use to orient in an unfamiliar repo fast.
