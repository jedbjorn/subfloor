---
name: local_skill_management
description: Create, persist, assign, and remove fork-specific skills — the correct authoring path so skills survive snapshot/rebuild cycles.
category: substrate
common: false
---

# local_skill_management — fork-specific skills that survive

Fork-specific skills live in the DB and persist via `.sc-state/content.sql`
(the snapshot). The asset file under `.super-coder/assets/skills/<name>/` is
the **authoring source only** — it sits in gitignored engine territory, and
that is safe: the engine/local boundary is the seed migration (0001,
upstream-owned in a fork), not asset-file presence. The snapshot serializes
your skill to content.sql whether or not the asset file is kept, and
`sc update` neither manifests it nor heals over its DB row. **content.sql =
the durable form; the asset file = your editor.**

The path: **file -> seed -> grant -> snapshot -> commit**.

## Creating a fork-specific skill

1. **Write the skill file** at `.super-coder/assets/skills/<name>/SKILL.md`.

   Required frontmatter:
   ```yaml
   ---
   name: skill_name
   description: One-line summary — shown in boot, catalogue, and the GUI Skills tab
   category: substrate   # or craft; omit for default
   ---
   ```
   Body: markdown procedure the shell will follow. Imperative, compressed —
   this boots into context.

2. **Seed into the live DB:**
   ```bash
   sc seed-skills
   ```
   UPSERTs every asset skill by name (id-stable) and reports what landed. In a
   fork it deliberately does NOT regenerate the seed migration — that file is
   upstream-owned engine territory. DB skills with no asset file = other local
   skills, left intact.

3. **Grant to target shell(s)** — by shell id or shortname:
   ```bash
   sc skill grant <skill_name> <shell>...
   ```
   Unknown skill/shell names = hard error (no silent no-op grants).
   `sc skill list` = catalogue with origins + current grants;
   `sc skill revoke <name> <shell>...` reverses a grant.

4. **Snapshot — the persistence step:**
   ```bash
   SC_ADMIN=1 sc snapshot && SC_ADMIN=1 sc render
   ```
   `snapshot.py` serializes local skills (any skill the engine seed doesn't
   own) into the active snapshot (`.sc-state/content.sql` in tracked mode,
   `.sc-state/local/content.sql` in local mode) — what survives `sc update` and
   `sc rebuild`; the row + grants reconstruct from content.sql. Skip this ->
   the skill is lost on next update.

5. **Finish.** Run `sc render-check` first — hermetic rebuild, fails if the
   `skills_sc/` mirror drifts from the DB render (the CI guard; see the
   `snapshot` skill). In tracked mode, stage `.sc-state/content.sql` +
   `skills_sc/` together. In local mode both stay ignored; only engine-owned
   assets/migrations are committed.

## Updating a skill

Edit the asset file -> repeat seed -> snapshot -> commit (steps 2, 4, 5).
Asset file gone (removed / authored elsewhere) -> recreate it from the DB body
first: `sc sql "SELECT content FROM skills WHERE name='<name>'"`.

## Assigning an existing skill to additional shells

```bash
sc skill grant <skill_name> <shell>...
```
Then `SC_ADMIN=1 sc snapshot && SC_ADMIN=1 sc render` + commit.

## Removing a skill

1. **Soft-delete the row + revoke its grants:**
   ```bash
   sc skill rm <skill_name>
   ```
   Refuses engine skills — the seed resurrects those on next update/rebuild.
   Engine skill this fork has superseded -> retire fork-wide:
   `sc skill retire <name>` (writes the tracked
   `.sc-state/skills_retired.json`, which rides updates; `sc skill unretire`
   reverses). Per-shell removal -> `sc skill revoke`.

2. **Remove the asset file** (`.super-coder/assets/skills/<name>/`) —
   otherwise the next `sc seed-skills` re-inserts the skill.

3. **Snapshot, render, commit:**
   ```bash
   SC_ADMIN=1 sc snapshot && SC_ADMIN=1 sc render
   ```

## How the GUI organizes skills

The review GUI Skills tab shows the full catalogue in sections with per-shell
grant toggles; the Shells tab groups its grant list by the same sections.

- **Repo skills** — lead section: skills authored in this fork. Membership is
  *derived* — a skill the engine seed doesn't own is repo-local. Same rule
  snapshot.py uses to decide what serializes into `.sc-state/content.sql`, so
  the section shows exactly what the snapshot keeps durable. No frontmatter
  flag exists or is needed.
- **Substrate / Craft / …** — engine skills, sectioned by `category`
  frontmatter. A repo skill's `category` displays as a row label but never
  moves it out of the Repo section.

GUI grant toggles hit the same DB table as `sc skill grant` — they still need
a snapshot (header button or `SC_ADMIN=1 sc snapshot`) to survive a rebuild.

## What NOT to do

- **NEVER skip the snapshot after creating a skill.** Seeding writes the live
  DB only; content.sql is what survives `sc update` and `sc rebuild`.
- **NEVER edit `0001_seed_skills.sql` by hand.** Generated, and in a fork
  upstream-owned engine territory — a local edit blocks the next update.
- **NEVER create skills via the GUI.** Toggling grants there is fine (snapshot
  after); creating is not — the GUI writes only the DB and cannot write the
  asset file or seed it. Use this procedure.
