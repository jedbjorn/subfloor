---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# test_authoring_sqlite

SQLite test infrastructure for super-coder-style forks — throwaway DB, Alice/Bob tenants, Caller/TestClient. Read alongside test_authoring for the rules.

**Category:** craft

---

# test_authoring_sqlite — SQLite test infra

Read `test_authoring` for the foundational rules. This skill covers the
test infrastructure for SQLite-backed forks (super-coder, dos-arch).

## Foundation

`tests/conftest.py` builds a throwaway SQLite DB from `schema.sql` + the
post-059 migration replay, seeds two tenants (Alice / Bob) + a shared system
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
- `tempfile.NamedTemporaryFile(suffix=".db")` → path injected via
  `os.environ["SHELL_DB_PATH"]` **before** importing the app (the auth
  middleware calls `db()` directly; a `Depends` override alone misses it).
- `apply_schema_and_migrations(con)` builds the schema on the throwaway DB —
  single source shared by all test harnesses; do not copy-paste it.
- A second throwaway (`DISPATCH_DB_PATH`) isolates egress/spend rows.
- `os.environ.setdefault("AUTH_COOKIE_SECURE", "")` — plain `dsess` cookie,
  no `__Host-` prefix in tests.

**Callers:**
```python
alice   # session-cookie caller, USER_A identity
bob     # session-cookie caller, USER_B identity
admin   # session-cookie caller, USER_ADMIN identity
anon    # no auth
shell_a # bearer-key caller, KEY_A
shell_b # bearer-key caller, KEY_B
```
All are pytest fixtures. `shell_a` / `shell_b` use `Authorization: Bearer`.

**TestClient:**
- Created without a `with` block — skips startup hooks (catalogue / model
  sync) that would hit the network.
- Session-scoped (`scope="session"`) so the DB is shared across all tests in a
  run; tests must not depend on a clean DB unless they seed their own via
  `build_substrate_db()` (in-memory, returns a `sqlite3.Connection`).

**Direct DB assertions:**
```python
import sqlite3, os
con = sqlite3.connect(os.environ["SHELL_DB_PATH"])
con.row_factory = sqlite3.Row
rows = con.execute("SELECT * FROM table WHERE ...").fetchall()
```
Assert against real rows, not the response payload. The throwaway path is
stable for the lifetime of the test session.

**Mocking boundary:**
Mock only true external egress — outbound IMAP, HTTP, broker calls. Never
mock the router, the DB layer, or the function under test.
