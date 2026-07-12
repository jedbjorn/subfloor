---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# migration_management

Author and apply fork-specific DB schema migrations — naming, format, how to apply locally and verify.

**Category:** substrate

---

# migration_management — fork-specific schema changes

Migrations live in `.super-coder/migrations/`, apply in numeric order, tracked
by the `schema_migrations` ledger table. Engine updates apply pending
migrations automatically; apply locally without a fetch via
`sc update --no-fetch`.

**Scope:** fork-specific changes — tables, columns, constraints, or
system-content seeds (skills, flavor defaults) this fork needs that will not
ship upstream. Upstream engine migrations arrive via `sc update`; no action
from you.

## Authoring a migration

1. **Find the next number:**
   ```bash
   ls .super-coder/migrations/ | sort | tail -5
   ```
   Name the file `NNNN_<slug>.sql`, NNNN = next integer zero-padded to 4
   digits (e.g. `0012`).

2. **Write the file** at `.super-coder/migrations/NNNN_<slug>.sql`:
   - Wrap in `BEGIN; ... COMMIT;`
   - Idempotent: `CREATE TABLE IF NOT EXISTS`, `INSERT OR IGNORE`,
     `CREATE INDEX IF NOT EXISTS`, `DROP TABLE IF EXISTS` before recreate
   - Comment header: migration number + intent (+ doctrine notes if relevant)
   - Structure + system content only — per-instance data (shell memory,
     grants, roadmap, flags) lives in `.sc-state/content.sql` via snapshot,
     never in migrations

3. **Apply locally:**
   ```bash
   sc update --no-fetch
   ```
   Skips the upstream fetch; applies all pending local migrations in order.
   Confirm it landed:
   ```sql
   SELECT * FROM schema_migrations ORDER BY applied_at DESC LIMIT 5;
   ```

4. **Verify:**
   ```bash
   sc verify
   ```
   Headless boot proof — shells, memory, and schema intact.

5. **Snapshot + commit:**
   ```bash
   sc snapshot
   ```
   Commit `.sc-state/content.sql` + `migrations/NNNN_<slug>.sql`.
   - **Content-seed migration** (seeds system content that renders — skills,
     flavor defaults) also changes the flat `_sc` mirrors, but only once the
     new rows are in the DB: after `sc update --no-fetch`, run
     `sc render && sc render-check` and commit the re-rendered `_sc` files
     alongside the migration. A render against a DB predating the seed passes
     locally while CI's hermetic rebuild goes red — the stale-mirror trap; see
     the `snapshot` skill.

## What makes a good migration

- **Additive by default.** Add columns/tables/indexes. No DROP or RENAME
  unless correcting a prior mistake; prefer a new column over renaming one
  code may reference.
- **No per-instance content.** Shell memory, skill grants, roadmap items,
  flags -> snapshot. Migrations carry structure + system content that
  propagates to all forks.
- **Comment the why** — future readers need the intent, not just the SQL.

## Rollback

No per-migration rollback. `sc rollback` restores the full DB + engine to the
prior update point (`engine.ref.prev`). Use only when a migration is so broken
the DB is corrupt or the app won't start; for logical errors, write a
corrective migration instead.
