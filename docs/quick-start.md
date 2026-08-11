---
title: subfloor — Quick start
tags: [substrate, shells, agentic-coding, harness-agnostic, sqlite]
date: 2026-07-20
project: subfloor
purpose: Install, first boot, the daily loop
---

# subfloor — Quick start

[![Open in md-converter](https://img.shields.io/badge/Open%20in-md--converter-6b46c1?style=flat-square)](https://md-converter.designs-os.com/?url=https://github.com/jedbjorn/subfloor/blob/main/docs/quick-start.md)

The guided tour — what subfloor is, getting it running, your first boot,
and the daily rhythm. This page walks; the [full documentation](README.md)
specifies. Every flag, table, and option lives there, and each section below
points at the tab that carries it.

## What it is

A **forkable shell substrate for a single code repository.** You install it
into a project repo; it brings the shell system — DB-backed identity, memory,
decisions, flags, a roadmap, and spec/doc content — and runs that repo through
whatever coding harness you point at it: Claude Code, OpenCode, Codex, Mistral
Vibe, or Kimi Code.

The bet: **we build the data layer, we rent the harness.** The agent loop, the
tools, the model API are the harness's job. subfloor owns who is working —
identity, memory, and content that survive every session — and renders a boot
artifact the harness reads natively.

What that buys you in practice:

- **A standing team, not a session.** Shells — a planner, devs, a reviewer, a
  cartographer, an admin — are DB rows that persist, remember, and can boot on
  a different harness tomorrow.
- **Parallel work without clobbering.** Every shell gets its own git worktree;
  a branch-guard keeps everyone off `main`; merging stays yours.
- **A localhost Review GUI** for reading and steering it all — shells,
  roadmap, flags, docs, analytics — including a **Chats** tab for durable
  browser conversations and read-only Diff review.
- **Durable handoffs** through shell messages and session-surviving jobs,
  with generic headless launches when a bounded non-interactive run is useful.

> [!class2]
> The full model — the harness-overlay design, the engine/fork boundary, the
> repo layout: [*Architecture*](README.md#architecture).

## Install

> [!class4]
> **The bar: Linux, Python 3.9+ with `sqlite3`, a reachable docker daemon, and one signed-in harness CLI on PATH.**
> Set `SC_PYTHON=/absolute/path/to/python3` to select a specific interpreter.
> Ubuntu LTS, stable Fedora, and Arch-compatible Linux (including CachyOS) are
> tested examples, not an exclusive list.
> On macOS or Windows, create a Linux VM and run this flow inside the guest.
> The Linux prerequisites, guest-filesystem recommendation, docker modes, and
> no-docker escape hatch: [*Install*](README.md#install).

Four steps, from an existing git repo to a booted shell:

1. **Pull the engine in.** Add `jedbjorn/subfloor` as a git remote and check
   out `.super-coder` + `sc` — files only, no history merge. The copy-paste
   block lives on the [landing README](../README.md) and in
   [*Install*](README.md#install).
2. **Bootstrap the fork.** `./sc install` checks requirements, installs the
   harness CLIs, wires your `.gitignore`, builds the DB, and seeds your
   starting team. Commit the bootstrap:

   ```bash
   git add -A && git commit --no-verify -m "chore: install subfloor"
   ```

   What it does under the hood, and the flags to script it:
   [*Install → Installer internals*](README.md#install).
3. **Launch.** `make dos-l` builds and starts the sandbox container and Review
   GUI, published to `127.0.0.1` only.
4. **Sign in and enter.** Each harness authenticates with your own
   account — `claude`, `opencode auth login`, `codex login`, `vibe --setup`,
   or `kimi login` — and the sandbox mounts the credentials in. Host, never
   inside the sandbox. After signing in, `make dos-e` enters a shell:
   [*Install → Harness sign-in*](README.md#install).

## First boot

`./sc enter` — pick a shell, pick a harness (the picker pre-selects each
flavor's default model), and you're in a session, talking to your **planner**
in your own repo.

- **Or start a browser conversation.** The GUI's **Chats** tab lists every
  shell; **New chat** on an available one opens a durable conversation with
  queued turns, explicit Stop/Close controls, history, stars, and a read-only
  Diff mode. Browser chat and CLI ownership are mutually exclusive for a shell:
  [*Browser conversations*](README.md#browser-conversations).
- **Open the Review GUI.** `./sc launch` printed its URL (`./sc url` reprints
  it once the harness TUI has painted over the boot summary); the Shells tab
  is the landing view — each shell's role, mandate, current state, and
  identity. The other tabs, the roadmap views, and the token analytics:
  [*Review GUI*](README.md#review-gui).
- **Meet the team.** The installer seeded two planners (one is your primary),
  four devs, two reviewers, the admin that owns `main`, and the cartographer
  that owns the repo map. Each boots into its own worktree; how they share one
  repo without collisions: [*Shells & worktrees*](README.md#shells--worktrees).
- **First acts.** Let the cartographer map the repo on its first boot, then
  tell the planner what you're building — it authors the roadmap and the
  first spec.
- **Models.** Which model each role defaults to and why, and when to prefer a
  subscription plan over an API key:
  [*Harnesses & models*](README.md#harnesses--models).

## The daily loop

The rhythm a fork settles into — you move between seats with
`./sc enter-<shortname>` or open a separate browser conversation in **Chats**, and
every step is owned by a flavor:

1. The **cartographer** keeps the repo map fresh; working shells read it
   instead of grepping blind.
2. The **planner** specs the next feature against the roadmap.
3. A **dev** breaks the spec into tasks, builds on a feature branch in its
   own worktree, opens a PR — and stops.
4. The **reviewer** — deliberately a different model lineage than the code —
   reads the diff, files flags; dev patches until clean.
5. **You merge.** Merging is the operator's gate, always.
6. The spec freezes, the feature doc is written, the admin verifies the trees
   are clean, the map re-runs — and the loop turns.

The step-by-step version, with each flavor's skills and GUI tab:
[*The loop*](README.md#the-loop).

- **Hand work to another shell.** Use the shell inbox for a durable task/result
  handoff, and use `./sc job` for a suite or build that must outlive the current
  session: [*Messages, jobs & headless launch*](README.md#messages-jobs--headless-launch).
- **The command surface.** Every `./sc` command and the `make dos-` aliases:
  [*CLI & dev kit*](README.md#cli--dev-kit).
- **Opt-in extras.** A Postgres sidecar, a Windows test VM, tailnet / pm2 /
  db brokers: [*Opt-in features*](README.md#opt-in-features).
- **Staying current.** `./sc update` pulls the new engine and migrates the DB
  in place, memory intact. Commit that protected-default-branch update with the
  exact operator command:

  ```bash
  ./sc update
  git add .sc-state/engine.ref sc && git commit --no-verify -m "chore: update subfloor"
  ```

  Full details: [*Update a fork*](README.md#update-a-fork).
