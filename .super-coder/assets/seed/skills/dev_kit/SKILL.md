---
name: dev_kit
description: What the sandbox dev kit provides + how to drive it — ./sc deps, ./sc test, ./sc lint, ./sc typecheck, the .venv tools, rg/sqlite3, the baked browser, the container/host app boundary, and the optional app-only Postgres sidecar (DATABASE_URL). Use when building or testing in a fork.
category: substrate
common: false
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

## Fork-owned lifecycle hooks

`./sc deps`, `./sc test`, `./sc lint`, and `./sc typecheck` are stable engine
verbs, but their behavior belongs entirely to the fork. The engine loads the
tracked `.subfloor/dev-kit.json`, validates it, and executes only the exact argv
declared for that hook. It never discovers manifests, selects language tools,
creates a virtualenv, installs packages, or supplies a missing test runner.

- Boot says `no fork dev kit declared` when the declaration is absent.
- An absent declaration or missing named hook exits `78`; there is no fallback.
- Invalid declaration or invocation exits `64`; an unavailable executable exits
  `126`; a started child keeps its shell-observable status.
- `SC_DEVKIT_ROOT`, `SC_DEVKIT_SEAT`, and `SC_DEVKIT_HOOK` tell a fork-owned
  script which checkout, seat, and hook the engine selected.

Read `.subfloor/dev-kit.json` and its declared executable before invoking a
hook. Run `./sc deps` first only when that declaration gives `deps` the fork's
provisioning policy. A fork may choose `.venv`, npm, another package manager, or
no dependency step at all. In Docker, a fork script must treat an out-of-repo
interpreter as host-managed and shared: verify it, but never pip-install into it.

## Baked into the image

Always present, no `./sc deps` needed:

- `rg` (ripgrep), `sqlite3` CLI, `curl`, `node` 22 / `npm`.
- **Playwright + Chromium** at `PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright`
  (world-readable). The fork's `@playwright/test` / `playwright` runner resolves
  it automatically — E2E drives the *running* app over HTTP, so start a dev
  server first.

## Frontend checks

Frontend tools such as `svelte-check`, `tsc`, and vitest come from the fork's
own dependencies. Whether and how they are installed or run is declared by the
fork's lifecycle hooks, not inferred from `package.json` by the engine.

## Postgres sidecar (app-only)

When a fork sets `"pg": {}` in `.super-coder/instance.json` (`./sc pg-init` adds
it), `./sc launch` starts a `postgres:17` sidecar (`sc-pg-<fork>` on `SC_NET`) and
forwards `DATABASE_URL` into the sandbox — so you can develop + test the fork's
**app** against real Postgres. This is for the *app only*: the engine DB is always
SQLite and the engine never reads `DATABASE_URL`, so the sidecar can't point the
review GUI at the wrong DB.

- `DATABASE_URL=postgresql://sc:sc@sc-pg-<fork>:5432/sc` is in the sandbox env,
  reachable by **container name** on `SC_NET` — *not* `127.0.0.1`, which inside
  the sandbox is its own loopback. Override with `SC_DATABASE_URL` on the host.
- Data persists in a named Docker volume across restarts + image rebuilds.
- The **Postgres driver is the fork's own dependency**, not the engine dev kit:
  declare `psycopg[binary]` (psycopg 3) through the fork's own dependency system
  and make its declared `deps` hook provision it. Then the app and declared test
  hook can connect.

Verify with `echo $DATABASE_URL`. *Unset* → the fork has no `pg` block; run
`./sc pg-init && ./sc restart` on the host.

**Empty ≠ unavailable.** A configured sidecar (`DATABASE_URL` *set*) whose schema
is empty is a **provision-me** signal — not "no DB / out of scope / blocked." It
is the fork's real app DB, waiting to be migrated. Provision it the way the app
does — the fork's own schema migrations + bootstrap (e.g. its `make migrate` /
`make bootstrap`, or whatever migration runner the repo map points to) — then
verify against it. Never hand-roll a separate throwaway DB, and never write the
task off as "no DB available." You have one.

## Stance

The declaration is the truth: inspect it before assuming what any lifecycle
verb provides. An exit `78` means the fork did not declare that hook, not that
the engine forgot to discover its stack. To see the app, run a dev server in the
container — never restart the FnB's host stack. Before calling an app-DB task
blocked on a missing/empty DB: check `DATABASE_URL`; if it is set but empty,
provision the sidecar with the fork's own migrations. In a sandbox the DB is
never the blocker.
