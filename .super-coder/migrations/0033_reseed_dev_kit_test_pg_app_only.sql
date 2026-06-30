-- 0033 — reseed dev_kit + test_authoring_pg: app-only Postgres, psycopg 3
--
-- #217 restored the app-only PG sidecar (sc-pg-<fork> + DATABASE_URL forwarding,
-- engine stays SQLite). Two seed-skill docs lagged:
--   - dev_kit: its PG section was dropped by 0030 (when #207 removed the sidecar);
--     re-add it, now accurate — app-only, container-name DSN, engine never reads
--     DATABASE_URL, psycopg is the fork's own dep (not the engine dev kit).
--   - test_authoring_pg: still showed psycopg2; the codebase is psycopg 3.
--
-- 0001 is regenerated clean from the assets, but 0025/0026 re-add the OLD dev_kit
-- PG section and 0030 drops it on a fresh rebuild — this forward reseed lands
-- after them so fresh builds AND already-installed forks converge on the corrected
-- body. UPSERT by name; skill_id + grants preserved.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'dev_kit',
  'What the sandbox dev kit provides + how to drive it — ./sc deps, ./sc test, ./sc lint, ./sc typecheck, the .venv tools, rg/sqlite3, the baked browser, the container/host app boundary, and the optional app-only Postgres sidecar (DATABASE_URL). Use when building or testing in a fork.',
  'substrate',
  NULL,
  0,
  '# dev_kit — the sandbox dev kit

What you have to build, test, and inspect a fork — and the one boundary that
trips shells up.

## You are in a container

You run **inside the sandbox container**; the repo is bind-mounted at its host
path. The app the FnB watches in their browser is a **separate instance** — the
host-supervised stack (pm2 / `make`), outside your container. To *see* the app
yourself, run a dev server **inside** the sandbox on `0.0.0.0:$SC_DEV_PORT`; the
FnB reaches that instance at `http://127.0.0.1:$SC_DEV_PORT`. (See the boot
doc''s `RUNNING THE APP` section.)

## Install + run

- `./sc deps` — install the fork''s deps into the bind-mount: a repo-root `.venv`
  from every `requirements*.txt` (fork pins win) + `npm ci`/`install` per
  `package.json`. Persists across image rebuilds. **Run this first.**
- `./sc test` — backend (`.venv` pytest honoring the fork''s `pytest.ini`, else
  the engine''s stdlib unittest) + UI (`npm run test` / vitest where a `test`
  script is declared). Non-zero if any suite fails.

## The `.venv` dev kit

`./sc deps` layers these onto the fork''s own deps with `--upgrade-strategy
only-if-needed`, so a fork''s pins and its `[tool.ruff]`/`[tool.mypy]` config
always win. **Available, not enforced** — opt in per fork.

- `./sc lint [paths]` → `.venv/bin/ruff check` — lint + format-check.
  (`.venv/bin/ruff format` to apply formatting.)
- `./sc typecheck [paths]` → `.venv/bin/mypy` — Python type-check.
- `.venv/bin/pytest` / `coverage` / `httpx` — test + HTTP client (also via `./sc test`).
- `.venv/bin/datasette <db.sqlite>` — browse a SQLite DB in a web GUI when the
  `sqlite3` CLI isn''t enough. Bind `0.0.0.0:$SC_DEV_PORT` to view it from the host.

> The `.venv` baseline only materializes when the fork declares Python (a
> `requirements*.txt`). A pure-frontend fork gets node only.

## Baked into the image

Always present, no `./sc deps` needed:

- `rg` (ripgrep), `sqlite3` CLI, `curl`, `node` 22 / `npm`.
- **Playwright + Chromium** at `PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright`
  (world-readable). The fork''s `@playwright/test` / `playwright` runner resolves
  it automatically — E2E drives the *running* app over HTTP, so start a dev
  server first.

## Frontend checks

`svelte-check`, `tsc`, vitest come from the fork''s own `package.json` devDeps —
installed by `./sc deps`'' `npm ci`, run via the fork''s npm scripts (or `./sc test`).

## Postgres sidecar (app-only)

When a fork sets `"pg": {}` in `.super-coder/instance.json` (`./sc pg-init` adds
it), `./sc launch` starts a `postgres:17` sidecar (`sc-pg-<fork>` on `SC_NET`) and
forwards `DATABASE_URL` into the sandbox — so you can develop + test the fork''s
**app** against real Postgres. This is for the *app only*: the engine DB is always
SQLite and the engine never reads `DATABASE_URL`, so the sidecar can''t point the
review GUI at the wrong DB.

- `DATABASE_URL=postgresql://sc:sc@sc-pg-<fork>:5432/sc` is in the sandbox env,
  reachable by **container name** on `SC_NET` — *not* `127.0.0.1`, which inside
  the sandbox is its own loopback. Override with `SC_DATABASE_URL` on the host.
- Data persists in a named Docker volume across restarts + image rebuilds.
- The **Postgres driver is the fork''s own dependency**, not the engine dev kit:
  declare `psycopg[binary]` (psycopg 3) in the fork''s `requirements*.txt` so
  `./sc deps` installs it. Then the app + `pytest` connect with no extra steps.

Verify with `echo $DATABASE_URL`. Empty → the fork has no `pg` block; run
`./sc pg-init && ./sc restart` on the host.

## Stance

`./sc deps` before anything else in a fresh sandbox — qwen''s "node missing" was
just deps-not-installed. Lint/type-check are there when you want them, never
forced; respect the fork''s config. To see the app, run a dev server in the
container — never restart the FnB''s host stack.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'test_authoring_pg',
  'Postgres test infrastructure for postgres-backed forks — throwaway DB, Alice/Bob tenants, psycopg 3 direct assertions. Read alongside test_authoring for the rules.',
  'craft',
  NULL,
  0,
  '# test_authoring_pg — Postgres test infra

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
- An admin connection (`psycopg.connect(DATABASE_URL_ADMIN, autocommit=True)`)
  creates a unique `dosarch_test_<uuid>` database at session start and drops it
  at session teardown.
- `DATABASE_URL` is injected via `os.environ["DATABASE_URL"]` **before**
  importing the app; the app''s DB layer reads it at import time.
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
import os, psycopg
from psycopg.rows import dict_row
con = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, row_factory=dict_row)
cur = con.cursor()
cur.execute("SELECT * FROM table WHERE ...")
rows = cur.fetchall()
```
Assert against real rows, not the response payload.

**Mocking boundary:**
Mock only true external egress — outbound HTTP, broker calls, third-party
APIs. Never mock the router, the DB layer, or the function under test.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
