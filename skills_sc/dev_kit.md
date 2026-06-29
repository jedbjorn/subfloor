---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# dev_kit

What the sandbox dev kit provides + how to drive it — ./sc deps, ./sc test, ./sc lint, ./sc typecheck, the .venv tools, rg/sqlite3, the baked browser, container/host app boundary, and the optional postgres 17 sidecar (DATABASE_URL). Use when building or testing in a fork.

**Category:** substrate

---

# dev_kit — the sandbox dev kit

What you have to build, test, and inspect a fork — and the one boundary that
trips shells up.

## You are in a container

You run **inside the sandbox container**; the repo is bind-mounted at its host
path. The app the FnB watches in their browser is a **separate instance** — the
host-supervised stack (pm2 / `make`), outside your container. To *see* the app
yourself, run a dev server **inside** the sandbox on `0.0.0.0:$SC_DEV_PORT`; the
FnB reaches that instance at `http://127.0.0.1:$SC_DEV_PORT`. (See the boot
doc's `RUNNING THE APP` section.)

## Install + run

- `./sc deps` — install the fork's deps into the bind-mount: a repo-root `.venv`
  from every `requirements*.txt` (fork pins win) + `npm ci`/`install` per
  `package.json`. Persists across image rebuilds. **Run this first.**
- `./sc test` — backend (`.venv` pytest honoring the fork's `pytest.ini`, else
  the engine's stdlib unittest) + UI (`npm run test` / vitest where a `test`
  script is declared). Non-zero if any suite fails.

## The `.venv` dev kit

`./sc deps` layers these onto the fork's own deps with `--upgrade-strategy
only-if-needed`, so a fork's pins and its `[tool.ruff]`/`[tool.mypy]` config
always win. **Available, not enforced** — opt in per fork.

- `./sc lint [paths]` → `.venv/bin/ruff check` — lint + format-check.
  (`.venv/bin/ruff format` to apply formatting.)
- `./sc typecheck [paths]` → `.venv/bin/mypy` — Python type-check.
- `.venv/bin/pytest` / `coverage` / `httpx` — test + HTTP client (also via `./sc test`).
- `.venv/bin/datasette <db.sqlite>` — browse a SQLite DB in a web GUI when the
  `sqlite3` CLI isn't enough. Bind `0.0.0.0:$SC_DEV_PORT` to view it from the host.

> The `.venv` baseline only materializes when the fork declares Python (a
> `requirements*.txt`). A pure-frontend fork gets node only.

## Baked into the image

Always present, no `./sc deps` needed:

- `rg` (ripgrep), `sqlite3` CLI, `curl`, `node` 22 / `npm`.
- **Playwright + Chromium** at `PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright`
  (world-readable). The fork's `@playwright/test` / `playwright` runner resolves
  it automatically — E2E drives the *running* app over HTTP, so start a dev
  server first.

## Frontend checks

`svelte-check`, `tsc`, vitest come from the fork's own `package.json` devDeps —
installed by `./sc deps`' `npm ci`, run via the fork's npm scripts (or `./sc test`).

## Postgres 17 sidecar

When the sys-admin has configured the PG sidecar (`"pg": {}` in
`.super-coder/instance.json`), `./sc launch` starts a `postgres:17` container
on the shared network and injects `DATABASE_URL` into the sandbox. No setup
needed inside the container.

**What you get:**
- `DATABASE_URL=postgresql://sc:sc@sc-pg-<repo>:5432/sc` is already in the environment
- `db_driver` switches to PG mode automatically — sqlite3 code paths are inert
- `psycopg2-binary` is in the baseline dev kit (`./sc deps`), so `pytest`
  connects to real postgres with zero extra install steps
- Data persists in a named Docker volume across restarts and image rebuilds

**Verifying it's live:**
```bash
echo $DATABASE_URL          # should show postgresql://...
```

**Running pytest against real PG:**
```bash
./sc deps                   # installs psycopg2-binary if not already present
.venv/bin/pytest tests/     # DATABASE_URL is already in the environment
```

If `DATABASE_URL` is not set, the sandbox is in SQLite mode — ask the
sys-admin to run `./sc pg-init && ./sc restart` on the host.

## Stance

`./sc deps` before anything else in a fresh sandbox — qwen's "node missing" was
just deps-not-installed. Lint/type-check are there when you want them, never
forced; respect the fork's config. To see the app, run a dev server in the
container — never restart the FnB's host stack.
