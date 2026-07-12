---
name: bootstrap
description: First-run orientation. Run ONCE when the boot doc shows "## FIRST RUN" (bootstrapped=0), BEFORE other work — read the repo map + your identity, set current_state, mark yourself oriented.
category: substrate
common: true
---

# bootstrap — orient yourself on first run

Run ONCE, when the boot doc shows **## FIRST RUN** (bootstrapped=0), before any
other work. A fresh shell knows its identity but hasn't looked around yet —
this is your first act: read the repo, read yourself, set your state, so you
start grounded instead of wandering.

The repo map already exists — cartographer automation keeps it fresh (see
`surface_catalogue`). Read it; NEVER run `sc map` yourself.

`<self>` = your `shell_id` (ACTIVE SESSION block).

## Steps

1. **Read the repo** via the `surface_catalogue` skill — language mix, where
   the code lives, dependencies, env surface -> hold a one-paragraph picture of
   what this repo *is* and how it's built.
   ```sql
   -- the repo map is its own db: sc map-sql "<query>"
   SELECT name, default_branch, file_count FROM dr_repo;
   SELECT lang, COUNT(*) n FROM dr_filepath WHERE lang IS NOT NULL GROUP BY lang ORDER BY n DESC;
   SELECT path, lang, lines FROM dr_filepath WHERE role='code' ORDER BY lines DESC LIMIT 15;
   SELECT manager, name, version FROM dr_dependency ORDER BY manager, name;
   ```
   `dr_repo` empty -> map automation hasn't run on this clone. Flag it
   (cartographer's task, not yours) + continue with what you have.

2. **Read yourself.** Seed (genesis + the CC lineage you carry), mandate, role
   — all in the boot doc. Re-read them with intent: this is who is doing the
   work here.

3. **Skim the plan** — open roadmap features + their blocking flags:
   ```
   sc mem get roadmap
   sc mem get flags
   ```

4. **Set `current_state`** — replace the install placeholder with what you
   actually found + what you'll do first (rolling status, ~500 chars):
   ```
   sc mem state "…"
   ```

5. **Mark yourself oriented** — sets `bootstrapped=1` in the shared DB -> next
   boot shows no FIRST RUN prompt:
   ```
   sc mem oriented
   ```
   Then proceed with the task at hand.

## Stance

- Bootstrap once, then work — NEVER re-run it on later sessions.
- Read the map; never map. Catalogue empty / stale / wrong -> raise it for the
  cartographer to heal; do NOT reach for `sc map`.
