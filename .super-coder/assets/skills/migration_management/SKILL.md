---
name: migration_management
description: Author and apply fork-specific DB schema migrations — naming, format, how to apply locally and verify.
category: substrate
common: false
---

# migration_management — fork-specific schema changes

Migrations live in `.super-coder/migrations/` and apply in numeric order,
tracked by the `schema_migrations` ledger table. Engine updates apply pending
migrations automatically; you can apply them locally without a fetch using
`./sc update --no-fetch`.

**Scope:** fork-specific schema changes — tables, columns, constraints, or
system-content seeds (skills, flavor defaults) that this fork needs and that
will not ship upstream. Upstream engine migrations arrive via `./sc update`
and require no action from you.

## Authoring a migration

1. **Find the next migration number.**
   ```bash
   ls .super-coder/migrations/ | sort | tail -5
   ```
   Name the file `NNNN_<slug>.sql` where NNNN is the next integer, zero-padded
   to 4 digits (e.g. `0012`).

2. **Write the file.**
   Path: `.super-coder/migrations/NNNN_<slug>.sql`

   Requirements:
   - Wrap in `BEGIN; ... COMMIT;`
   - Idempotent: `CREATE TABLE IF NOT EXISTS`, `INSERT OR IGNORE`,
     `CREATE INDEX IF NOT EXISTS`, `DROP TABLE IF EXISTS` before recreate
   - Comment header: migration number, intent, and doctrine notes if relevant
   - Structure and system content only — per-instance data (shell memory,
     grants, roadmap, flags) lives in `.sc-state/content.sql` via snapshot,
     not in migrations

3. **Apply locally.**
   ```bash
   ./sc update --no-fetch
   ```
   Skips the upstream fetch; applies all pending local migrations in order.
   Confirm the migration landed:
   ```sql
   SELECT * FROM schema_migrations ORDER BY applied_at DESC LIMIT 5;
   ```

4. **Verify.**
   ```bash
   ./sc verify
   ```
   Headless boot proof — confirms shells, memory, and schema are intact.

5. **Snapshot and commit.**
   ```bash
   ./sc snapshot
   ```
   Commit `.sc-state/content.sql` + `migrations/NNNN_<slug>.sql`.

## What makes a good migration

- **Additive by default.** Add columns, tables, indexes. Avoid DROP or RENAME
  unless correcting a prior mistake; prefer a new column over renaming one that
  code may already reference.
- **No per-instance content.** Shell memory, skill grants, roadmap items, and
  flags go in the snapshot, not here. Migrations carry structure and system
  content that propagates to all forks.
- **Comment the why.** Future-you reading a migration needs the intent, not
  just the SQL.

## Rollback

There is no per-migration rollback. `./sc rollback` restores the full DB +
engine to the prior update point (`engine.ref.prev`). Use it only when a
migration is so broken the DB is corrupt or the app won't start. For logical
errors, write a corrective migration instead.
