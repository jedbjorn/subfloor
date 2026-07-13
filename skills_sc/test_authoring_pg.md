---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# test_authoring_pg

Postgres test infrastructure for postgres-backed forks — throwaway DB, Alice/Bob tenants, psycopg 3 direct assertions. Read alongside test_authoring for the rules.

**Category:** craft

---

# test_authoring_pg — Postgres test infra

Rules live in `test_authoring` — read it alongside. This skill = the test
infrastructure PATTERN for Postgres-backed forks.

**Your fork's `tests/conftest.py` is the source of truth** for the throwaway
DB's naming, what schema artifact seeds it (a live `schema.sql`, a squash, a
migration replay), and the fixture roster — read it before writing a test.
Everything below is the typical shape, not a contract; where your conftest
differs, the conftest wins. A fork may also ship its own superseding
test-authoring skill — if one is granted, prefer it.

## Foundation (typical shape)

`tests/conftest.py` creates a throwaway Postgres DB at session start, applies
the fork's schema artifact, seeds two tenants (Alice / Bob) + a shared system
shell, and drives the real app through `TestClient` with real auth.

**Key identities (an example roster — confirm against your conftest):**

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
- An admin connection (`psycopg.connect(DATABASE_URL_ADMIN, autocommit=True)`)
  creates a uniquely-named `<fork>_test_<unique>` database at session start
  and drops it at session teardown — the naming scheme is the conftest's.
- `DATABASE_URL` injected via `os.environ["DATABASE_URL"]` BEFORE importing
  the app — the app's DB layer reads it at import time.
- The fork's schema artifact applied on the throwaway connection — which
  artifact (postgres `schema.sql`, a schema squash, a migration replay) is a
  per-fork choice; read the conftest, don't assume.
- Some forks isolate egress/spend rows in a second throwaway DB/schema —
  only if your conftest declares one.

**Callers** — same `Caller` pattern as the SQLite variant; identity carried
via cookie or `Authorization: Bearer` header:
```python
alice   # session-cookie caller, USER_A identity
bob     # session-cookie caller, USER_B identity
admin   # session-cookie caller, USER_ADMIN identity
anon    # no auth
shell_a # bearer-key caller, KEY_A
shell_b # bearer-key caller, KEY_B
```

**TestClient:**
- Create WITHOUT a `with` block -> skips startup hooks (catalogue / model
  sync) that would hit the network.
- `scope="session"` -> one DB shared across the whole run. A test needing
  isolation seeds its own fixture rows + cleans up explicitly.

**Direct DB assertions:**
```python
import os, psycopg
from psycopg.rows import dict_row
con = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, row_factory=dict_row)
cur = con.cursor()
cur.execute("SELECT * FROM table WHERE ...")
rows = cur.fetchall()
```
Assert against real rows, not the response payload.

**Mocking boundary:** mock only true external egress — outbound HTTP, broker
calls, third-party APIs. NEVER mock the router, the DB layer, or the
function under test.
