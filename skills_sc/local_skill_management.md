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
(the snapshot). The asset file under `.super-coder/assets/skills/` is used to
**seed the skill initially** — but that directory is gitignored engine territory:
`./sc update` materializes upstream engine files there, which removes any local
additions. After the first seed + snapshot, **content.sql is the durable form**.

The correct path: **file → seed → grant → snapshot → commit**.

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

2. **Seed the skill into the DB.**
   ```bash
   cd <repo> && ./sc seed-skills
   ```
   UPSERTs the skill row into the live DB by name (id-stable). Does not touch
   skills already in the DB that are absent from assets — those are other local
   skills, left intact.

3. **Grant the skill to the target shell(s).**
   Find shell IDs:
   ```sql
   SELECT shell_id, display_name, flavor FROM shells WHERE is_deleted = 0;
   ```
   Grant:
   ```sql
   INSERT OR IGNORE INTO shell_skills (shell_id, skill_id)
   SELECT <shell_id>, skill_id FROM skills
   WHERE name = '<skill_name>' AND is_deleted = 0;
   ```

4. **Snapshot — this is the persistence step.**
   ```bash
   ./sc snapshot && ./sc render
   ```
   `snapshot.py` serializes local skills (any skill whose name is not in the
   upstream engine assets) into `.sc-state/content.sql`. This is what survives
   `./sc update` — when the engine materialize overwrites `.super-coder/assets/
   skills/`, the skill row and its full content are reconstructed from
   content.sql on rebuild. Without this step the skill is lost on next update.

5. **Commit.**
   Run `./sc render-check` first — it rebuilds hermetically and fails if the
   `skills_sc/` mirror drifts from the DB render (the same CI guard; see the
   `snapshot` skill). Then stage `.sc-state/content.sql` and `skills_sc/`
   together — the snapshot without the re-rendered mirror is the drift. The asset
   file and `0001_seed_skills.sql` are transient for local skills — don't rely on
   them across updates.

## Assigning an existing skill to additional shells

```sql
INSERT OR IGNORE INTO shell_skills (shell_id, skill_id)
SELECT <shell_id>, skill_id FROM skills
WHERE name = '<skill_name>' AND is_deleted = 0;
```
Then `./sc snapshot && ./sc render` and commit.

## Removing a skill

1. **Soft-delete the DB row and revoke grants.**
   ```sql
   UPDATE skills SET is_deleted = 1 WHERE name = '<name>';
   DELETE FROM shell_skills
   WHERE skill_id = (SELECT skill_id FROM skills WHERE name = '<name>');
   ```

2. **Snapshot, render, commit.**
   ```bash
   ./sc snapshot && ./sc render
   ```
   The deletion serializes to content.sql. If the asset file still exists under
   `.super-coder/assets/skills/`, remove it too so `./sc seed-skills` doesn't
   re-insert it.

## How the GUI organizes skills

The review GUI has a **Skills tab**: the full catalogue in sections, with
per-shell grant toggles on every skill. The Shells tab groups its grant list
by the same sections.

- **Repo skills** — the lead section: skills authored in this fork. Membership
  is *derived*, not declared — a skill whose name has no
  `.super-coder/assets/skills/<name>/SKILL.md` is repo-local. This is the same
  rule snapshot.py uses to decide what serializes into `.sc-state/content.sql`,
  so the section shows exactly what the snapshot keeps durable. No frontmatter
  flag exists or is needed.
- **Substrate / Craft / …** — engine skills, sectioned by their `category`
  frontmatter. A repo skill's `category` still displays as a label on its row,
  but never moves it out of the Repo section.
- One transient caveat: while a repo skill's asset file still sits under
  `assets/skills/` (between authoring and the next `./sc update` materialize),
  the derivation reads it as engine — it appears under its category section
  until the update wipes the asset. Harmless; the DB row is the durable thing.

Grant toggles in the GUI hit the same DB table as the SQL in this skill —
they still need a **snapshot** (header button or `./sc snapshot`) to survive
a rebuild.

## What NOT to do

- **Never skip the snapshot after creating a skill.** The asset file under
  `.super-coder/assets/skills/` is overwritten by `./sc update`. If you seed
  without snapshotting, the skill vanishes on the next engine update.
- **Never edit `0001_seed_skills.sql` by hand.** It is generated; hand edits
  are overwritten on the next `./sc seed-skills`.
- **Never use the GUI to create skills.** Toggling grants in the GUI is fine
  (snapshot after); creating is not — the GUI writes only to the DB and cannot
  write the asset file or seed it. Use this procedure instead.
