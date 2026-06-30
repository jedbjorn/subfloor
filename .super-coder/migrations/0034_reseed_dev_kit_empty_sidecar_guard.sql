-- 0034 — reseed dev_kit: add the empty-sidecar provision-me guard
--
-- #218 (0033) restored the app-only PG sidecar *mechanism* to dev_kit, but not
-- the behavioural guard: fork shells kept reading a configured-but-empty sidecar
-- (DATABASE_URL set, schema not yet migrated) as "no DB / out of scope / blocked"
-- and either declared app-DB work impossible or hand-rolled a throwaway DB — when
-- the real app DB was at $DATABASE_URL the whole time (dos-arch flag 176 / SC-058B).
-- This adds the "Empty != unavailable" distinction + stance to the dev_kit skill.
--
-- 0001 is regenerated clean from the asset, but 0030 drops the PG section and 0033
-- re-adds the pre-guard body on a fresh rebuild — this forward reseed lands after
-- them so fresh builds AND already-installed forks converge on the guarded body.
-- UPSERT by name; skill_id + grants preserved.

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

Verify with `echo $DATABASE_URL`. *Unset* → the fork has no `pg` block; run
`./sc pg-init && ./sc restart` on the host.

**Empty ≠ unavailable.** A configured sidecar (`DATABASE_URL` *set*) whose schema
is empty is a **provision-me** signal — not "no DB / out of scope / blocked." It
is the fork''s real app DB, waiting to be migrated. Provision it the way the app
does — the fork''s own schema migrations + bootstrap (e.g. its `make migrate` /
`make bootstrap`, or whatever migration runner the repo map points to) — then
verify against it. Never hand-roll a separate throwaway DB, and never write the
task off as "no DB available." You have one.

## Stance

`./sc deps` before anything else in a fresh sandbox — qwen''s "node missing" was
just deps-not-installed. Lint/type-check are there when you want them, never
forced; respect the fork''s config. To see the app, run a dev server in the
container — never restart the FnB''s host stack. Before calling an app-DB task
blocked on a missing/empty DB: check `DATABASE_URL`; if it''s set but empty,
provision the sidecar with the fork''s own migrations. In a sandbox the DB is
never the blocker.',
  0
)
ON CONFLICT(name) DO UPDATE SET
  description=excluded.description, category=excluded.category,
  command=excluded.command, common=excluded.common,
  content=excluded.content, is_deleted=0;

COMMIT;
