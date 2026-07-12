---
name: query_authoring_pg
description: Compose + run SQL against a Postgres-backed fork's app DB — psql mechanics, psql variables vs driver :params, SQLite→Postgres dialect traps, read-only diagnostics, paste-ready handoff when the DB is outside the sandbox. Use when diagnosing app data issues or verifying data by query.
category: craft
common: false
---

# query_authoring_pg — diagnostic SQL against the app's Postgres

The pg kit's query half: `dev_kit` = the sidecar, `test_authoring_pg` = the
test infra, this = ad-hoc SQL against the fork's app DB. Use when diagnosing
data issues, verifying a migration's effect, or checking an invariant by
query.

## Know which DB you're pointed at

| DB | Where | What its data proves |
|---|---|---|
| Sandbox sidecar (`$DATABASE_URL`) | inside your container | your dev/test copy — only what you or the tests put there. Empty/missing rows here prove NOTHING about the FnB's data. |
| The FnB's stack DB (dev/prod) | on the host, outside your container | the data actually being diagnosed — reachable only by handoff (below) |

Name the DB in every finding. An FnB-reported data issue lives in THEIR DB —
never confirm or refute it against your sidecar. Reproduce the shape locally
if useful; the verdict query runs on their side.

## psql mechanics

SQL is not a shell command — `SELECT …` pasted at a fish/bash prompt dies at
the shell. Run it through psql:

```bash
psql "$DATABASE_URL" -X -P pager=off -c "SELECT count(*) FROM users;"   # one-shot
psql "$DATABASE_URL" -X -v ON_ERROR_STOP=1 -f diag.sql                  # scripted
```

- `-X` skips any psqlrc; `-P pager=off` keeps output capture-friendly.
- `\x auto` (in-session) for wide rows.
- Schema truth = `\dt` + `\d <table>` — check a column's actual type before
  writing predicates against it; never guess from habit.

## Parameters — `:name` is driver syntax

`:c`-style placeholders bind in psycopg/SQLAlchemy, NOT in psql. Either
substitute literals before running, or use psql variables:

```bash
psql "$DATABASE_URL" -X -v c=42 -v who=alice -f diag.sql
```
```sql
WHERE contact_id = :c AND username = :'who'
-- :var → raw substitution · :'var' → quoted literal · :"var" → identifier
```

NEVER hand anyone a query with unbound `:params` and no `-v` line to run it
with — it fails at their prompt, not yours.

## Dialect traps (SQLite habits)

Engine DB = SQLite; app DB = Postgres. Habits that break:

| SQLite habit | Postgres |
|---|---|
| `flag = 0` / `flag = 1` | on a `boolean` column that's a type error — use `NOT flag` / `flag`. A schema ported from SQLite may still use integers: `\d` decides. |
| `INSERT OR IGNORE` | `INSERT … ON CONFLICT DO NOTHING` |
| `datetime('now')` | `now()` |
| `strftime(…)` / date math | `to_char(…)`, `date_trunc(…)`, `now() - interval '7 days'` |
| `"double-quoted"` strings | double quotes mean **identifiers**; string literals are `'single-quoted'` only |
| `LIKE` (case-insensitive for ASCII) | `LIKE` is case-sensitive — use `ILIKE` |
| `GROUP_CONCAT(x)` | `string_agg(x, ',')` |
| `rowid` | doesn't exist — use the primary key |

## Diagnostic shape

- Read-only, ALWAYS. Diagnostics never mutate — a fix goes through the app
  or a migration, never a hand-run UPDATE. Scripted files open with
  `BEGIN; SET TRANSACTION READ ONLY;` and end with `ROLLBACK;`.
- One row, many answers — pack independent checks into
  `SELECT EXISTS(…) AS <check_name>` columns so a single row answers the
  whole question.
- `\echo '=== section ==='` between probes; one comment per section saying
  how to read its result.
- `LIMIT` every exploratory SELECT; never dump whole tables into a report.

## Handoff — DB outside your sandbox

1. Write `<topic>_diag.sql` to the fork's shared scratch dir,
   self-documenting to the skeleton below.
2. Give the operator ONE paste-ready line for their shell, resolving the DSN
   from wherever the fork keeps it (env file, secret store):

```bash
# bash/zsh
psql "$(grep -m1 '^DATABASE_URL=' <env-file> | cut -d= -f2-)" -X -f <abs-path>.sql
# fish — no quotes around the substitution
psql (grep -m1 '^DATABASE_URL=' <env-file> | cut -d= -f2-) -X -f <abs-path>.sql
```

3. In your message: how to read each section's output + what each outcome
   implies next.

Skeleton:

```sql
-- orphaned-orders diagnostic · 2026-07-06
-- run: psql "$DATABASE_URL" -X -v ON_ERROR_STOP=1 -v u=42 -f orders_diag.sql
BEGIN; SET TRANSACTION READ ONLY;

\echo === A. user :u — exists / active? ===
SELECT EXISTS (SELECT 1 FROM users WHERE user_id = :u)                    AS user_exists,
       EXISTS (SELECT 1 FROM users WHERE user_id = :u AND NOT is_deleted) AS user_active;
-- user_exists=f → wrong id; user_active=f → soft-deleted, explains missing rows.

\echo === B. orders with no owning user (expect zero rows) ===
SELECT o.order_id FROM orders o LEFT JOIN users u USING (user_id)
WHERE u.user_id IS NULL LIMIT 20;
-- any rows → the delete path leaks orders; note the ids.

ROLLBACK;
```
