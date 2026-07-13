---
name: test_authoring_sqlite
description: SQLite test infrastructure for super-coder-style forks — throwaway DB, Alice/Bob tenants, Caller/TestClient. Read alongside test_authoring for the rules.
category: craft
common: false
---

# test_authoring_sqlite — SQLite test infra

Rules live in `test_authoring` — read it alongside. This skill = the test
infrastructure PATTERN for SQLite-backed forks.

**Your fork's `tests/conftest.py` is the source of truth** for how the
throwaway DB is built and what fixtures exist — read it before writing a
test. Everything below is the typical shape, not a contract; where your
conftest differs, the conftest wins. A fork may also ship its own superseding
test-authoring skill — if one is granted, prefer it.

## Foundation (typical shape)

`tests/conftest.py` builds a throwaway SQLite DB from the fork's schema
artifact (schema.sql + a migration replay, or a squash), seeds two tenants
(Alice / Bob) + a shared system shell, and drives the real app through
`TestClient` with real auth.

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
- `tempfile.NamedTemporaryFile(suffix=".db")` -> path injected via
  `os.environ["SHELL_DB_PATH"]` BEFORE importing the app — the auth
  middleware calls `db()` directly; a `Depends` override alone misses it.
- The conftest's schema builder (e.g. `apply_schema_and_migrations(con)`)
  builds the throwaway DB — single source shared by all test harnesses;
  NEVER copy-paste it.
- Some forks isolate egress/spend rows in a second throwaway DB — only if
  your conftest declares one.
- `os.environ.setdefault("AUTH_COOKIE_SECURE", "")` -> plain `dsess` cookie,
  no `__Host-` prefix in tests.

**Callers** — all pytest fixtures:
```python
alice   # session-cookie caller, USER_A identity
bob     # session-cookie caller, USER_B identity
admin   # session-cookie caller, USER_ADMIN identity
anon    # no auth
shell_a # bearer-key caller, KEY_A
shell_b # bearer-key caller, KEY_B
```
`shell_a` / `shell_b` send `Authorization: Bearer`.

**TestClient:**
- Create WITHOUT a `with` block -> skips startup hooks (catalogue / model
  sync) that would hit the network.
- `scope="session"` -> one DB shared across the whole run. Never depend on a
  clean DB; a test needing isolation seeds its own via
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

**Mocking boundary:** mock only true external egress — outbound IMAP, HTTP,
broker calls. NEVER mock the router, the DB layer, or the function under
test.
