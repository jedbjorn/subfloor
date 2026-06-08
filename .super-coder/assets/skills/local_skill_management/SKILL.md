---
name: local_skill_management
description: Create, persist, assign, and remove fork-specific skills — the correct authoring path so skills survive snapshot/rebuild cycles.
category: substrate
common: false
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
   description: One-line summary — shown in boot and catalogue
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
   Stage `.sc-state/content.sql` and `skills_sc/`. The asset file and
   `0001_seed_skills.sql` are transient for local skills — don't rely on them
   across updates.

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

## What NOT to do

- **Never skip the snapshot after creating a skill.** The asset file under
  `.super-coder/assets/skills/` is overwritten by `./sc update`. If you seed
  without snapshotting, the skill vanishes on the next engine update.
- **Never edit `0001_seed_skills.sql` by hand.** It is generated; hand edits
  are overwritten on the next `./sc seed-skills`.
- **Never use the GUI to create skills.** The GUI writes only to the DB — it
  cannot write the asset file or trigger a snapshot. Use this procedure instead.
