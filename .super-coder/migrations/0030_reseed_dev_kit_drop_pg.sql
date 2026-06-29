-- 0030 — reseed dev_kit skill: drop the Postgres 17 sidecar section
--
-- The engine is SQLite-only again (#207); the dev_kit "## Postgres 17 sidecar"
-- section (added by 0025/0026) now documents the removed ./sc pg-init sidecar.
-- 0001 is regenerated clean from assets/skills/dev_kit/SKILL.md, but 0025/0026
-- still re-add the section on a fresh rebuild — this forward reseed lands after
-- them so fresh builds AND already-installed forks converge on the clean body.
-- UPSERT by name; skill_id and grants are preserved.

BEGIN;

INSERT INTO skills (name, description, category, command, common, content, is_deleted) VALUES (
  'dev_kit',
  'What the sandbox dev kit provides + how to drive it — ./sc deps, ./sc test, ./sc lint, ./sc typecheck, the .venv tools, rg/sqlite3, the baked browser, and the container/host app boundary. Use when building or testing in a fork.',
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

COMMIT;
