-- 0160 — route migration authors through the scaffold and document backups.
--
-- Full-body UPSERT converges existing installations; 0001_seed_skills.sql
-- remains the fresh-install seed generated from the authoritative skill asset.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'migration_management',
  'Author and apply fork-specific DB schema migrations — naming, format, how to apply locally and verify.',
  'substrate',
  NULL,
  0,
  '# migration_management — fork-specific schema changes

Migrations live in `.super-coder/migrations/`, apply in numeric order, tracked
by the `schema_migrations` ledger table. Engine updates apply pending
migrations automatically; apply local pending migrations with `./sc migrate`.

**Scope:** fork-specific changes — tables, columns, constraints, or
system-content seeds (skills, flavor defaults) this fork needs that will not
ship upstream. Upstream engine migrations arrive via `sc update`; no action
from you.

## Authoring a migration

1. **Create it through the guardrail:**
   ```bash
   ./sc migration new <slug>
   ```
   Use a lowercase `snake_case` slug. The command refuses unexpected duplicate
   number prefixes, allocates the next free zero-padded number, writes the
   standard transaction/idempotence skeleton, and (in the subfloor source
   repo) updates the source removal-test allowlist in the same act. The
   exact historical `0155` pair is frozen and explicitly allowed; never
   renumber an applied migration.

2. **Fill in the generated file** at
   `.super-coder/migrations/NNNN_<slug>.sql`:
   - Wrap in `BEGIN; ... COMMIT;`
   - Idempotent: `CREATE TABLE IF NOT EXISTS`, `INSERT OR IGNORE`,
     `CREATE INDEX IF NOT EXISTS`, `DROP TABLE IF EXISTS` before recreate
   - Comment header: migration number + intent (+ doctrine notes if relevant)
   - Structure + system content only — per-instance data (shell memory,
     grants, roadmap, flags) lives in `.sc-state/local/content.sql` via snapshot,
     never in migrations

3. **Apply locally:**
   ```bash
   ./sc migrate
   ```
   This takes a WAL-safe `premigrate` backup before opening the migration
   chain and keeps the newest 5 backups in that lifecycle class. `./sc update`
   retains its separate `preupdate` backup and does not double-back up during
   the same update run. Both paths apply pending migrations in order.
   Confirm it landed:
   ```sql
   SELECT * FROM schema_migrations ORDER BY applied_at DESC LIMIT 5;
   ```

4. **Verify:**
   ```bash
   ./sc verify
   ```
   Headless boot proof — shells, memory, and schema intact.

5. **Snapshot + commit:**
   ```bash
   SC_ADMIN=1 sc snapshot
   ```
   Commit the migration and any authoritative source asset it carries; the
   snapshot remains local.
   - **Engine skill seed:** edit `assets/skills/<name>/SKILL.md`, run
     `./sc seed-skills` to regenerate `0001_seed_skills.sql`, and put the same
     full-body UPSERT in the new trailing migration so existing installations
     converge. Then run `./sc render-check`: its hermetic rebuild proves the
     ignored local `skills_sc/` mirror, which is verification output rather
     than a tracked artifact.

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
the DB is corrupt or the app won''t start; for logical errors, write a
corrective migration instead.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
