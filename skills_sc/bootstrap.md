---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# bootstrap

First-run orientation for a shell in a repo. Run ONCE when the boot doc shows "## FIRST RUN" (bootstrapped=0), or whenever the repo map is empty. Maps the repo, reads the map + your identity, sets your current_state, marks you oriented. Do this BEFORE other work on a fresh fork.

**Category:** substrate

---

# bootstrap — orient yourself on first run

A freshly-installed shell knows its identity but hasn't *looked around* yet. This
is your first act: map the repo, read it, read yourself, and set your state — so
you start work grounded instead of wandering. Run it when the boot doc shows
**## FIRST RUN**, or any time `dr_*` is empty.

`<self>` = your `shell_id` (ACTIVE SESSION block).

## Steps

1. **Ensure the repo is mapped.** Check the catalogue:
   ```sql
   SELECT COUNT(*) FROM dr_filepath;
   ```
   If it's 0 (fresh, or after a `make rebuild` — the map is a derived cache, not
   snapshotted), run `make map` to scan the repo, then continue.

2. **Read the repo** via the `surface_catalogue` skill — language mix, where the
   code lives, dependencies, env surface. Form a one-paragraph picture of what
   this repo *is* and how it's built.
   ```sql
   SELECT name, default_branch, file_count FROM dr_repo;
   SELECT lang, COUNT(*) n FROM dr_filepath WHERE lang IS NOT NULL GROUP BY lang ORDER BY n DESC;
   SELECT path, lang, lines FROM dr_filepath WHERE role='code' ORDER BY lines DESC LIMIT 15;
   SELECT manager, name, version FROM dr_dependency ORDER BY manager, name;
   ```

3. **Read yourself.** Your seed (genesis + the CC lineage you carry), mandate,
   and role are in the boot doc. Re-read them with intent — this is who is doing
   the work here.

4. **Skim the plan.** Open roadmap features + their blocking flags:
   ```sql
   SELECT feature_id, title, roadmap_status FROM roadmap ORDER BY sort_order;
   SELECT display_name, description FROM flags WHERE resolved=0 AND is_deleted=0;
   ```

5. **Set your `current_state`** — replace the install placeholder with what you
   actually found and what you'll do first (rolling status, ~500 chars):
   ```sql
   UPDATE shells SET current_state='…' WHERE shell_id=<self>;
   ```

6. **Mark yourself oriented** (clears the FIRST RUN prompt for next boot):
   ```sql
   UPDATE shells SET bootstrapped=1 WHERE shell_id=<self>;
   ```
   Then `make snapshot` so the new state survives a rebuild, and proceed with the
   task at hand.

## Stance

- Bootstrap once, then work — don't re-run it every session.
- If you ever boot and the map looks empty or stale (`dr_repo.mapped_at` old),
  `make map` before trusting it. Orientation is cheap; working blind is not.
