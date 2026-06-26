---
title: Publish, GitHub auth & the webapp event log
tags: [super-coder, gui, publish, github, auth, logging]
date: 2026-06-26
project: super-coder
purpose: How the Review GUI's snapshot/publish work, how they authenticate to GitHub, and how to see what they did
---

# Publish, GitHub auth & the webapp event log

[![Open in md-converter](https://img.shields.io/badge/Open%20in-md--converter-6b46c1?style=flat-square)](https://md-converter.designs-os.com/?url=https://github.com/jedbjorn/super-coder/blob/main/docs/publish-and-gh-auth.md)

## What snapshot & publish do

The Review GUI header has two buttons:

- **snapshot ⤓** — serialize the live DB → `.sc-state/content.sql` and render the
  flat files (`specs_sc/`, `docs_sc/`, `skills_sc/`, `roadmap_sc.md`). **Local
  only, no git.** This is what makes your edits durable as git-tracked text the
  DB can be rebuilt from.
- **publish ⤴** — snapshot + render, then **commit → force-push → open/update one
  PR** from an ephemeral `sc_gui_content` branch, and return to `main`. It never
  merges; the open PR is the gate you merge on GitHub.

### The ephemeral-branch model

Each publish (re)creates the local `sc_gui_content` branch **from a clean
`main`**, commits the serialized content onto it, force-pushes, and opens/updates
**one rolling PR** — then returns to `main` and deletes the local branch. The
branch *name* is stable (one rolling PR); the local branch is *ephemeral* (rebuilt
and dropped every publish), so the working tree is always left clean on `main` and
branches never accumulate.

Everything publish touches is **regenerated from the live DB**, so its working-tree
edits are disposable — publish exploits this to recover from a tree left dirty or
stranded on the publish branch by a previous run. If *unrelated* files are dirty,
publish refuses rather than clobbering them: commit or stash them first.

## GitHub authentication

Push + PR need a GitHub token. `publish` resolves one in this order:

1. **`SC_GH_TOKEN`** env var (a repo-scoped PAT — the tightest option), else
2. **`GH_TOKEN`** env var (what `./sc launch` forwards into the sandbox), else
3. **`gh auth token`** from the host's `gh` login — the fallback for a
   **host-run** server (one started directly, not via the launch sandbox).

So you have two ways to be authenticated:

### Option A — `gh auth login` (web flow; recommended)

A normal browser-based `gh` login is enough — no token to copy or export.

```bash
gh auth login          # choose GitHub.com → HTTPS → Login with a web browser
gh auth status         # verify: "Logged in to github.com as <you>"
gh auth token          # should print a token (this is what publish falls back to)
```

- **Launch sandbox (`./sc launch`):** the launcher runs `gh auth token` on the
  **host** and forwards it as `GH_TOKEN` into the container. Web auth on the host
  is all you need; nothing to do inside the sandbox (where `gh` isn't installed).
- **Host-run server:** `_gh_token()` calls `gh auth token` directly, so a
  web-authed host `gh` works with no env var set.

> [!class3]
> **Run `gh auth login` on the host, not inside the sandbox.** The web flow opens a
> localhost callback your browser must reach; from inside the container that port
> isn't published. Same rule as harness sign-in.

Make sure the login grants the **`repo`** scope (push + PR). `gh auth status`
lists granted scopes; re-run `gh auth login -s repo` if it's missing.

### Option B — a scoped PAT in `SC_GH_TOKEN`

Tighter: a fine-grained / classic PAT limited to the one repo, with
**Contents: read/write** + **Pull requests: read/write** (classic: `repo`).

```bash
export SC_GH_TOKEN=ghp_xxx          # in the env that launches the server / ./sc
```

`SC_GH_TOKEN` wins over `GH_TOKEN`, and `./sc launch` prefers it when forwarding.

## Seeing what happened — the webapp event log

Every snapshot and publish (and any unhandled API error) is recorded to a
**rolling log**: one file, the **last 20 end-to-end events**, JSON-per-line.

- **File:** `.super-coder/logs/webapp.log` (gitignored — local + ephemeral).
- **API:** `GET /api/logs` → `{"events": [...newest first...], "max": 20}`.
- **Each event:** `ts`, `op` (`publish` / `snapshot` / `error`), `ok`, the
  op-specific fields (`pushed`, `pr_url`, `path`), and `detail` — the full
  step-by-step trace as a list, so each event stays one greppable line.

```bash
# tail the raw log (inside the sandbox, or on the host)
tail -n 20 .super-coder/logs/webapp.log

# or pretty-print the last publish from the API
curl -s localhost:$PORT/api/logs | python3 -m json.tool | less
```

A publish that "looked done" but didn't land is now answerable: read its event
and you'll see exactly where it stopped — `no GH_TOKEN`, `push failed`,
`force-pushed`, `opened/updated PR`, or `nothing to publish`.

## Troubleshooting

| Symptom in the log / toast | Cause | Fix |
|---|---|---|
| `⚠ committed locally, but no GH_TOKEN` | No token resolved | `gh auth login` on the host (web), or set `SC_GH_TOKEN`; relaunch so the server's env picks it up. |
| `✗ push failed: … 403` | Token lacks scope, or no write access | Ensure the token has `repo` (Contents + PRs write). `gh auth status` shows scopes. |
| `✗ push failed: … could not resolve host` | No network from the server | Check the sandbox network / proxy. |
| Publish succeeds but **no open PR** after you merged the last one | Expected — you merged the rolling PR; the next publish opens a fresh one when there are new edits | Just publish again with new content. |
| `✗ working tree has non-content changes — refusing to publish` | Unrelated edits are dirty | Commit or stash them, then publish. |
| `✓ no content changes vs main — nothing to publish` | DB already matches `main` | Nothing to do; your edits were already published/merged. |
| `recovered onto main from stranded sc_gui_content` | A previous run left the tree on the publish branch | None — publish auto-recovers; this line is informational. |

## See also

- [`README.md` → Review GUI](../README.md#review-gui)
- [`README.md` → Harness sign-in](../README.md#harness-sign-in) (same host-vs-sandbox login rule)
