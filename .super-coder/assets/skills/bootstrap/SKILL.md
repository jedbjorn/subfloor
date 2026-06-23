---
name: bootstrap
description: First-run orientation for a shell in a repo. Run ONCE when the boot doc shows "## FIRST RUN" (bootstrapped=0). Read the repo map + your identity, set your current_state, mark yourself oriented. Do this BEFORE other work on a fresh fork.
category: substrate
common: true
---

# bootstrap — orient yourself on first run

A freshly-installed shell knows its identity but hasn't *looked around* yet. This
is your first act: read the repo, read yourself, and set your state — so you
start work grounded instead of wandering. Run it when the boot doc shows
**## FIRST RUN**.

You do **not** map the repo — the map is already there, kept fresh by the
cartographer's automation (see `surface_catalogue`). You read it.

`<self>` = your `shell_id` (ACTIVE SESSION block).

## Steps

1. **Read the repo** via the `surface_catalogue` skill — language mix, where the
   code lives, dependencies, env surface. Form a one-paragraph picture of what
   this repo *is* and how it's built.
   ```sql
   -- the repo map is its own db: sqlite3 .sc-state/map.db "<query>"
   SELECT name, default_branch, file_count FROM dr_repo;
   SELECT lang, COUNT(*) n FROM dr_filepath WHERE lang IS NOT NULL GROUP BY lang ORDER BY n DESC;
   SELECT path, lang, lines FROM dr_filepath WHERE role='code' ORDER BY lines DESC LIMIT 15;
   SELECT manager, name, version FROM dr_dependency ORDER BY manager, name;
   ```
   (If `dr_repo` comes back empty, the map automation hasn't run on this clone —
   that's a cartographer task, not yours. Flag it and carry on with what you can.)

2. **Read yourself.** Your seed (genesis + the CC lineage you carry), mandate,
   and role are in the boot doc. Re-read them with intent — this is who is doing
   the work here.

3. **Skim the plan.** Open roadmap features + their blocking flags:
   ```sql
   SELECT feature_id, title, roadmap_status FROM roadmap ORDER BY sort_order;
   SELECT display_name, description FROM flags WHERE resolved=0 AND is_deleted=0;
   ```

4. **Set your `current_state`** — replace the install placeholder with what you
   actually found and what you'll do first (rolling status, ~500 chars). Write it
   through `./sc mem` (resolves + guards the engine DB; the write is live in the
   shared DB at once):
   ```
   ./sc mem state "…"
   ```

5. **Mark yourself oriented** (clears the FIRST RUN prompt for next boot). Sets
   `bootstrapped=1` in the shared DB:
   ```
   ./sc mem oriented
   ```
   Then proceed with the task at hand.

## Stance

- Bootstrap once, then work — don't re-run it every session.
- Read the map; never map. If the catalogue looks empty, stale, or wrong, that's
  the cartographer's job to heal — raise it, don't reach for `./sc map`.
