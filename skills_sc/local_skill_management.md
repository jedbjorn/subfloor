---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# local_skill_management

Create, persist, assign, and remove fork-specific skills — the correct authoring path so skills survive snapshot/rebuild cycles.

**Category:** substrate

---

# local_skill_management — fork-specific skills that survive

Fork-specific skills live in the DB and are persisted via `.sc-state/content.sql`
(the snapshot). The asset file under `.super-coder/assets/skills/<name>/` is the
**authoring source** — edit it, re-seed, done. It sits in gitignored engine
territory, but that is safe: the engine/local boundary is the seed migration
(0001, upstream-owned in a fork), not asset-file presence, so the snapshot
serializes your skill to content.sql whether or not the asset file is kept, and
`sc update` neither manifests it nor heals over its DB row. **content.sql is
the durable form; the asset file is your editor.**

The path: **file → seed → grant → snapshot → commit**.

## Creating a fork-specific skill

1. **Write the skill file.**
   Path: `.super-coder/assets/skills/<name>/SKILL.md`

   Required frontmatter:
   ```yaml
   ---
   name: skill_name
   description: One-line summary — shown in boot, catalogue, and the GUI Skills tab
   category: substrate   # or craft; omit for default
   ---
   ```
   Body: Markdown. Write the procedure the shell will follow. Imperative,
   precise — this is what boots into context, so compress ruthlessly.

2. **Seed the skill into the live DB.**
   ```bash
   sc seed-skills
   ```
   UPSERTs every asset skill into the live DB by name (id-stable) and reports
   what landed. In a fork it deliberately does NOT regenerate the seed
   migration — that file is upstream-owned engine territory. Skills already in
   the DB with no asset file are other local skills, left intact.

3. **Grant the skill to the target shell(s)** — by shell id or shortname:
   ```bash
   sc skill grant <skill_name> <shell>...
   ```
   Unknown skill or shell names are hard errors (no silent no-op grants).
   `sc skill list` shows the catalogue with origins and current grants;
   `sc skill revoke <name> <shell>...` reverses a grant.

4. **Snapshot — this is the persistence step.**
   ```bash
   sc snapshot && sc render
   ```
   `snapshot.py` serializes local skills (any skill the engine seed doesn't
   own) into `.sc-state/content.sql`. This is what survives `sc update` and
   `sc rebuild` — the skill row and its grants are reconstructed from
   content.sql. Without this step the skill is lost on next update.

5. **Commit.**
   Run `sc render-check` first — it rebuilds hermetically and fails if the
   `skills_sc/` mirror drifts from the DB render (the same CI guard; see the
   `snapshot` skill). Then stage `.sc-state/content.sql` and `skills_sc/`
   together — the snapshot without the re-rendered mirror is the drift.

## Updating a skill

Edit the asset file, then repeat seed → snapshot → commit (steps 2, 4, 5). If
the asset file is gone (removed, or authored elsewhere), recreate it from the
DB body first: `sc sql "SELECT content FROM skills WHERE name='<name>'"`.

## Assigning an existing skill to additional shells

```bash
sc skill grant <skill_name> <shell>...
```
Then `sc snapshot && sc render` and commit.

## Removing a skill

1. **Soft-delete the row and revoke its grants:**
   ```bash
   sc skill rm <skill_name>
   ```
   Refuses engine skills — the seed would resurrect those on the next
   update/rebuild. For an engine skill this fork has superseded, retire it
   fork-wide instead: `sc skill retire <name>` (writes the tracked
   `.sc-state/skills_retired.json`, which rides updates; `sc skill unretire`
   reverses). For a per-shell removal, `sc skill revoke`.

2. **Remove the asset file** (`.super-coder/assets/skills/<name>/`) — otherwise
   the next `sc seed-skills` re-inserts the skill.

3. **Snapshot, render, commit.**
   ```bash
   sc snapshot && sc render
   ```

## How the GUI organizes skills

The review GUI has a **Skills tab**: the full catalogue in sections, with
per-shell grant toggles on every skill. The Shells tab groups its grant list
by the same sections.

- **Repo skills** — the lead section: skills authored in this fork. Membership
  is *derived*, not declared — a skill the engine seed doesn't own is
  repo-local. This is the same rule snapshot.py uses to decide what serializes
  into `.sc-state/content.sql`, so the section shows exactly what the snapshot
  keeps durable. No frontmatter flag exists or is needed.
- **Substrate / Craft / …** — engine skills, sectioned by their `category`
  frontmatter. A repo skill's `category` still displays as a label on its row,
  but never moves it out of the Repo section.

Grant toggles in the GUI hit the same DB table as `sc skill grant` — they
still need a **snapshot** (header button or `sc snapshot`) to survive a
rebuild.

## What NOT to do

- **Never skip the snapshot after creating a skill.** Seeding puts the row in
  the live DB only; content.sql is what survives `sc update` and `sc rebuild`.
- **Never edit `0001_seed_skills.sql` by hand.** It is generated, and in a
  fork it is upstream-owned engine territory — a local edit blocks the next
  update.
- **Never use the GUI to create skills.** Toggling grants in the GUI is fine
  (snapshot after); creating is not — the GUI writes only to the DB and cannot
  write the asset file or seed it. Use this procedure instead.
