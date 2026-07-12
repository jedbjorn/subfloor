---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# database-migrations

Database migration safety + how super-coder's own migrations work (schema.sql baseline + ordered migrations/ deltas + ledger). Use when altering tables, adding columns, or running backfills — in the host repo's DB or super-coder's.

**Category:** craft

---

# database-migrations — change schemas safely

Catalogue skill (opt-in). Two halves: super-coder's own migration model, and
general safety for the host repo's database.

## super-coder's model

- `schema.sql` = current baseline (full schema). `migrations/*.sql` = ordered,
  additive deltas applied on top; the `schema_migrations` ledger dedups so
  each runs once. `rebuild` = schema -> migrations -> snapshot-load.
- NEVER fold a migration back into `schema.sql` — it double-applies. Add a
  new numbered migration instead. Exception: pre-fork (no downstream forks
  yet), editing the baseline directly is acceptable; once forks exist, only
  additive migrations propagate.
- System content (e.g. the skill catalogue) = seeded by migration + re-seed;
  per-instance content rides in the snapshot. See `db_map` / `snapshot`.

## General safety (host repo DBs)

- **Expand -> migrate -> contract**: add columns/tables before reading them;
  deploy code that tolerates both shapes; remove the old shape only after
  nothing uses it.
- **Backfills**: batch large updates (no table-long lock); make them
  resumable + idempotent; separate the schema change from the data change.
- **New columns on a populated table**: nullable or defaulted — `NOT NULL`
  with no default fails on existing rows.
- **Reversibility**: know each migration's rollback before applying it; a
  destructive change (drop/rename) needs a deploy plan, not just a script.
- **SQLite**: limited `ALTER` — changing a constraint = recreate-and-copy
  (new table -> copy -> drop -> rename) with `foreign_keys` off during the
  swap. Renames break FK references — check them.

## Stance

Migrate forward in small, reversible steps. A schema change is a deploy
event: migrated ≠ deployed — restart the consumer, then verify the running
process, not just the DB.
