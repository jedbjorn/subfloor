---
name: local_skill_management
description: Create, update, assign, and remove DB-canonical fork-local skills through the supported CLI. Use for fork AI-team capabilities that must survive snapshot/rebuild without engine asset edits or Admin intervention.
category: substrate
common: false
---

# local_skill_management — manage fork-local skills through the DB

Fork-local skills are canonical in the live engine DB and persist in the local
snapshot. Keep the input `SKILL.md` only as an authoring draft; never copy it
under `.super-coder/assets/skills/` or edit the engine seed.

## Create or update

1. Write a draft `SKILL.md` in a Planner-owned path. Use flat frontmatter:

   ```yaml
   ---
   name: repo_skill
   description: State what the skill does and when it fires.
   category: substrate
   common: false
   ---

   # Procedure

   Give directives with observable success conditions.
   ```

   Use a lowercase underscore name. Keep `common: false`: fork-local grants are
   always explicit. Apply `authoring_syntax` to the description and body.

2. Commit the draft to the catalogue:

   ```bash
   sc skill put --file <path/to/SKILL.md>
   ```

   Success prints `DB + snapshot + flat render + skill projections reconciled`.
   The command requires the launched shell identity to resolve as Planner,
   creates or updates the DB row, preserves every existing grant, and creates
   no managed asset copy. An engine-owned name refuses before any write and
   points to the upstream engine-skill workflow.

3. Grant a new skill explicitly:

   ```bash
   sc skill grant <skill_name> <shell>...
   ```

   Naming a standard shell updates its shared flavor pack. Naming a Bespoke
   shell updates that shell only. Success includes the same persistence receipt.
   Creation alone grants nothing.

## Change assignments

```bash
sc skill grant <skill_name> <shell>...
sc skill revoke <skill_name> <shell>...
sc skill list
```

Unknown skills and shells fail loudly. Every successful grant or revoke writes
the DB, local snapshot, flat catalogue render, and affected harness projections;
do not run `SC_ADMIN=1`, `sc snapshot`, or `sc render` afterward.

## Remove

```bash
sc skill rm <skill_name>
```

Local removal soft-deletes the catalogue row, revokes its flavor and Bespoke
grants, persists every layer, and removes managed projections. Re-running the
same removal is safe and reconciles a prior partial persistence failure.

Engine skills refuse `rm`. Use `sc skill retire <name>` only when the fork must
retire an engine-owned skill; use `sc skill unretire <name>` to restore it.

## Recover a partial persistence failure

A failed command names each committed and uncommitted layer. Fix the reported
snapshot, render, or projection path, then retry the exact same `sc skill`
command. Stop only when it prints the full persistence receipt; a DB-only
commit is live but not yet rebuild-durable.

## Boundaries

- Never edit `.super-coder/assets/skills/` for a fork-local skill.
- Never run `sc seed-skills` for a fork-local skill.
- Never use `SC_ADMIN=1`, `sc sql-rw`, or an Admin handoff for this lifecycle.
- Never set a fork-local skill `common: true`; use explicit grants.
- Change engine-owned skill bodies only through the upstream engine workflow.
