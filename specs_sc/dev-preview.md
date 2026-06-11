---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Dev shell live UI preview
roadmap_status: shipped
frozen: false
---

# Dev shell live UI preview

## Problem

Dev shells each own a git worktree (`.sc-worktrees/<shortname>/`) — see
[dev-worktrees](dev-worktrees.md). That isolates *edits*, but it breaks
*viewing*: the fork's dev server runs from the main checkout, so a shell's UI
changes — made in its worktree — never show on the live dev server. Pointing the
one dev server at a single worktree would just clobber the isolation the
worktrees exist to provide, and two shells previewing at once would fight over
one port.

## Solution

One router on the fork's `dev_port` that fans out to a per-worktree vite, routed
by **subdomain**:

    http://dev1.localhost:<dev_port>/      dev1's worktree UI, live (HMR)
    http://dev2.localhost:<dev_port>/      dev2's worktree UI, live (HMR)
    http://localhost:<dev_port>/           index of available shells

`*.localhost` resolves to 127.0.0.1 on modern systems — no hosts-file or DNS
setup. Each worktree's vite runs at root on a private internal port; the router
reads the `Host` header and proxies to the matching backend.

### Why subdomain, not path-prefix (`:port/dev1/...`)

The fork UIs are SvelteKit with SSR server routes (the same-origin `/api/*`
trust seam is a real server route). Serving several apps under one port by
**path prefix** would force every app to emit `/dev1/`-prefixed URLs — i.e.
`kit.paths.base`, an app-wide build-time setting baked into every internal link
*and* the `/api` seam — injected per-shell into each fork's committed config,
plus an HMR-behind-prefix dance. **Subdomain** sidesteps all of it: each
subdomain is a distinct origin, so the app serves from root unchanged — no base
config, `/api` seam intact, native HMR.

### Why per-connection routing works

A browser keeps one TCP connection per origin, so the first `Host` seen on a
connection is its route for life. The router reads the request header block,
picks the backend, then splices raw bytes both directions — HTTP keep-alive and
websocket upgrades (HMR) flow through untouched, no per-request reparsing.

## Surfaces

### 1. `preview.py` (`./sc preview`)
Discovers `.sc-worktrees/*` with a UI dir; for each: symlinks the main
checkout's `node_modules` into the worktree UI (same repo + lockfile → valid),
best-effort `git submodule update --init`, writes a generated sidecar vite
config (`.sc-preview.vite.config.js`, gitignored under `.sc-worktrees/`) that
extends the worktree's own config with `allowedHosts: ['.localhost']` and
`hmr.clientPort = dev_port` (so the HMR client dials the front port, not the
private one), then launches `vite dev` on a free internal port. An asyncio
proxy on `dev_port` routes by `Host` subdomain label. A 5s reconcile loop picks
up new worktrees and reaps removed ones — dynamic, no restart.

Binds `$SC_BIND` (0.0.0.0 in the sandbox so the published port is reachable;
127.0.0.1 on the host). Front port is `$SC_DEV_PORT` if set (the sandbox
publishes it) else the repo-derived `dev_port`. Binds the front port first, so a
clash (e.g. the sandbox already publishes `dev_port`) fails fast with guidance
instead of spawning vites it would have to reap.

### 2. `sc` — `preview)` dispatch + help line.

### 3. `post-commit` hook
Fires via `core.hooksPath`. On a commit from a worktree
(`*/.sc-worktrees/*`), prints `→ preview: http://<shortname>.localhost:<dev_port>/`.
Silent on the main checkout. Best-effort — never blocks the commit.

### 4. `git` skill
A note so the shell surfaces the printed preview URL to the FnB after committing
UI work, and starts `./sc preview` if it isn't already up.

## Runtime model

`./sc preview` is meant to run where the shells run: inside the sandbox
container `dev_port` is free (the publish maps it out to the host), so the router
binds it there and `http://<shortname>.localhost:<dev_port>/` is reachable from
the host browser. On a pm2/host fork, run it on the host where `dev_port` is
free. It is long-lived (one per fork) and reaps its vites on Ctrl-C.

## Out of scope (this spec)

- A pm2/supervised long-run wrapper — run it in the foreground or background it.
- HTTPS / non-`.localhost` hostnames — dev-only, plain HTTP.
- Reviewer/planner shells — git read-only, no UI to preview.

## Done condition

With two dev-shell worktrees that carry a UI, `./sc preview` serves each at
`http://<shortname>.localhost:<dev_port>/` — independent content per branch,
assets and the `/api` seam intact, and HMR live (a `101 Switching Protocols`
through the router). Committing in a worktree prints that worktree's preview URL.
