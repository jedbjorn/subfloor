---
title: super-coder — Shells & worktrees
tags: [substrate, shells, agentic-coding, harness-agnostic, sqlite]
date: 2026-07-20
project: super-coder
purpose: Per-shell worktrees, auto-synced bases, the branch-guard, admin on main
---

# How shells share one repo

## How shells share one repo

> [!class2]
> **UI** Shells · Worktrees · **Shells** all flavors; admin is the only one on `main`

A fork boots a **whole team** out of the box — `planner` · 2×`dev` · `reviewer`
· `admin` · `cartographer` — and you add or retire shells from the GUI as
needed. They all work the same repo without clobbering each other:

- **Every shell boots into its own git worktree** at
  `.sc-worktrees/<shortname>/` on branch `shell/<shortname>` — parallel shells
  never share a cwd. The branch is a **moving base pinned to `origin/main`**,
  not a content branch: shells cut feature branches from it, push, and open
  PRs. Merging stays the operator's gate.
- **The launcher keeps bases fresh.** Every boot fetches and auto-syncs the
  worktree onto `origin/main` — but only when provably nothing can be lost
  (on the base branch, clean tree, no local-only commits). Anything local
  blocks the sync and is surfaced in the boot doc instead, so the shell asks
  you before any work is touched.
- **A branch-guard blocks work on `main`** in every harness — pre-tool hooks
  (Claude Code, Codex), an OpenCode plugin, and a git pre-commit backstop, all
  one shared script. Under Claude Code it also inspects the **edit's target
  path**, so a shell editing the stale repo-root checkout from inside its
  worktree is blocked (and an out-of-worktree edit to a feature branch warns).
- **The admin shell is the one exception.** It boots in the **repo root** on
  `main` and maintains it directly — engine updates, rollbacks, migrations,
  applying approved patches, fork-local skills. The branch-guard exempts it
  (and only it). Working shells consume the substrate; admin owns the floor.
- **Reviewing a shell's UI work:** worktree edits never show on your main dev
  server. `./sc preview` serves every shell worktree's UI live (HMR) on the
  fork's dev port, routed by subdomain — `http://<shortname>.localhost:<port>/`
  — and the post-commit hook prints the shell's URL after each commit.
