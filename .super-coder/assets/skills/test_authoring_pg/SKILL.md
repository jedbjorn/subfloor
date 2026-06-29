---
name: test_authoring_pg
description: Postgres test infrastructure for postgres-backed forks — throwaway DB, Alice/Bob tenants, psycopg2 direct assertions. Read alongside test_authoring for the rules.
category: craft
common: false
---

# test_authoring_pg — Postgres test infra

Read `test_authoring` for the foundational rules. This skill covers the
test infrastructure for Postgres-backed forks.

## Foundation

`tests/conftest.py` creates a throwaway Postgres DB at session start, applies
`schema.sql` + migrations, seeds two tenants (Alice / Bob) + a shared system
shell, and drives the real app through `TestClient` with real auth.

**Key identities (fixed rowids — address by literal in tests):**

| Name | Kind | ID |
|---|---|---|
| `USER_ADMIN` | admin user | 1 |
| `USER_A` / Alice | tenant user | 10 |
| `USER_B` / Bob | tenant user | 20 |
| `SHELL_SHARED` | shared system shell | 100 |
| `SHELL_A` / `SHELL_B` | per-tenant shells | 101 / 102 |
| `PROJ_A` / `PROJ_B` | per-tenant projects | 500 / 501 |
| `KEY_A` / `KEY_B` | shell bearer keys | `"ALICEKEY"` / `"BOBKEY"` |

**Throwaway DB setup:**
- An admin connection (`psycopg2.connect(DATABASE_URL_ADMIN)`) creates a
  unique `dosarch_test_<uuid>` database at session start and drops it at
  session teardown.
- `DATABASE_URL` is injected via `os.environ["DATABASE_URL"]` **before**
  importing the app; the app's DB layer reads it at import time.
- `schema.sql` (the postgres variant) + migrations are applied via
  `cur.execute(SCHEMA.read_text())` on the throwaway DB connection.
- A second throwaway database (or schema) isolates egress/spend rows
  (`DISPATCH_DATABASE_URL`).

**Callers:**
```python
alice   # session-cookie caller, USER_A identity
bob     # session-cookie caller, USER_B identity
admin   # session-cookie caller, USER_ADMIN identity
anon    # no auth
shell_a # bearer-key caller, KEY_A
shell_b # bearer-key caller, KEY_B
```
Same `Caller` pattern as the SQLite variant — identity carried via cookie
or `Authorization: Bearer` header.

**TestClient:**
- Created without a `with` block — skips startup hooks (catalogue / model
  sync) that would hit the network.
- Session-scoped (`scope="session"`) so the DB is shared across all tests
  in a run; tests that need isolation seed their own fixture rows and
  clean up explicitly.

**Direct DB assertions:**
```python
import psycopg2, psycopg2.extras, os
con = psycopg2.connect(os.environ["DATABASE_URL"])
con.autocommit = True
cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("SELECT * FROM table WHERE ...")
rows = cur.fetchall()
```
Assert against real rows, not the response payload.

**Mocking boundary:**
Mock only true external egress — outbound HTTP, broker calls, third-party
APIs. Never mock the router, the DB layer, or the function under test.
