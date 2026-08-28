---
name: engine_migrations
description: Maintain Subfloor's schema baseline, ordered migration ledger, live-DB backup boundary, rebuild/update compatibility, and source-repository migration files. Admin-only by default.
category: substrate
common: false
---

# engine_migrations — maintain Subfloor's database floor

Subfloor owns `.super-coder/schema.sql` as the current baseline and
`.super-coder/migrations/*.sql` as ordered additive deltas. The
`schema_migrations` ledger applies each delta once. `sc rebuild` creates the
baseline, applies every migration, then restores instance content; `sc update`
materializes source and reconciles migrations before the next boot.

## Author in the source repository

Add one next-numbered migration. Keep historical migrations append-only and
change `schema.sql` only when the current baseline itself must describe a new
schema object. Never fold an already shipped delta into the baseline in a way
that makes rebuild apply it twice.

For seeded system content, update the authoritative asset or generator and add
a trailing reconciliation migration. Preserve per-instance rows carried by the
snapshot. Pass = fresh build, in-place migration, and rebuild from an older
snapshot converge to the same state.

## Protect the live cache

The live engine DB is `.super-coder/shell_db.db` in the main checkout, not a
Developer worktree. Before an authorized live migration, resolve that exact
path and create the workflow's WAL-safe backup. The FnB owns the restart and
cutover boundary. Never point engine work at `$DATABASE_URL`; that variable is
for the fork application's database.

## Verify compatibility

Run the migration on a dirty fixture containing the stale rows it must
reconcile, then run it again. Require:

- one application recorded in `schema_migrations`;
- identical desired state after repeated migration and rebuild;
- preserved shell memory and genuine fork-local content;
- no stale grant, projection, or system row restored by an older snapshot; and
- the running engine healthy after the authorized restart.

Stop before live application when the backup, exact DB path, compatibility
fixture, or FnB maintenance authority is absent.
