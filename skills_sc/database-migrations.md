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
general migration safety for the host repo's database.

## super-coder's model
- `schema.sql` = the **current baseline** (full schema). `migrations/*.sql` =
  **ordered, additive deltas** applied on top; the `schema_migrations` ledger
  dedups so each runs once. `rebuild` = schema → migrations → snapshot-load.
- **Never fold a migration back into `schema.sql`** (double-apply). Add a
  delta as a new numbered migration instead. *(Pre-fork, while there are no
  downstream forks, editing the baseline directly is acceptable — once forks
  exist, only additive migrations propagate.)*
- System content (e.g. the skill catalogue) is seeded by migration + re-seed;
  per-instance content rides in the snapshot. See `db_map` / `snapshot`.

## General safety (host repo DBs)
- **Additive first**: add columns/tables before you read them; deploy code that
  tolerates both shapes; remove the old shape only after nothing uses it
  (expand → migrate → contract).
- **Backfills**: batch large updates (don't lock the table); make them
  resumable and idempotent; separate the schema change from the data change.
- **Nullable or defaulted** new columns on a populated table — a `NOT NULL` with
  no default fails on existing rows.
- **Reversibility**: know the rollback for each migration before applying it; a
  destructive change (drop/rename) needs a deploy plan, not just a script.
- **SQLite specifics**: limited `ALTER` — changing a constraint means
  recreate-and-copy (new table → copy → drop → rename), with `foreign_keys` off
  during the swap. Renames break FK references; check them.

## Stance
Migrate forward in small, reversible steps. A schema change is a deploy event:
migrated ≠ deployed — restart the consumer, then verify the running process, not
just the DB.
