---
title: subfloor
tags: [substrate, shells, agentic-coding, harness-agnostic, sqlite]
date: 2026-07-20
project: subfloor
purpose: Forkable shell substrate for a repo
---

[![tests](https://img.shields.io/github/actions/workflow/status/jedbjorn/subfloor/tests.yml?style=flat-square&label=tests)](https://github.com/jedbjorn/subfloor/actions/workflows/tests.yml)
[![render-check](https://img.shields.io/github/actions/workflow/status/jedbjorn/subfloor/render-check.yml?style=flat-square&label=render-check)](https://github.com/jedbjorn/subfloor/actions/workflows/render-check.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-6b46c1?style=flat-square)](LICENSE)
[![Open in md-converter](https://img.shields.io/badge/Open%20in-md--converter-6b46c1?style=flat-square)](https://md-converter.designs-os.com/?url=https://github.com/jedbjorn/subfloor/blob/main/README.md)

# subfloor

**[Quick start](docs/quick-start.md) · [Full documentation](docs/README.md)**

## Overview

### What is subfloor?

In everyday terms, **subfloor is an AI development system — a whole AI
development team in a box.** Add it to an existing project from the command
line, install it, launch it, and start working with a Shell (subfloor's name for
an AI agent). Launching also prints the address of the local Review GUI: a
friendly home base where you can see your team, roadmap, specs and docs, open
flags, repo map, worktrees, and token use.

![./sc enter — pick a shell, pick a harness, boot into your agent with the Review GUI link on screen](https://raw.githubusercontent.com/jedbjorn/subfloor/main/docs/demo.gif)

### Getting Started

A good first run starts with the **Cartographer Shell**. It maps your repository
and adds short descriptions so every other Shell can get oriented without
re-reading the whole project. Next, start a **Planner Shell** and work through
your first feature and its spec. Specs and docs make the handoff smoother, so
for anything complex, send the spec through a fresh reviewer or QA session
before development begins.

When the spec is ready, ask the planner to message a **Dev Shell**. subfloor has
native messaging between Shells: boot the dev, tell it to check its inbox, and
watch it implement the spec and open a pull request. Send complex work through
review; simple work can go straight to your merge approval. Booting each Shell
manually is a good way to learn the system, and you can always ask the Shell
you're with what should happen next — every role knows the workflow.

Once that feels familiar, try a **sprint**: subfloor's built-in workflow for
larger efforts across models and providers. A Planner Shell oversees the work
from start to finish, handing units between developers and reviewers. Outside a
sprint, Shells never merge without your approval. During a declared sprint,
they receive narrow permission to merge reviewed, passing work; this runs most
smoothly when GitHub does not require a human to merge into `main`. If it does,
the planner will tell you when your approval is needed.

If you are ever unsure what is happening, open the Review GUI or boot the
**Admin Shell**. Admin works from `main` and looks after the system as a whole:
it can check the team's health, help update subfloor, and create skills tailored
to your repository.

### The technical details
Subfloor is a **forkable shell substrate for a single code repository.** You install it into
a project repo; it brings the shell system — DB-backed identity, memory, seed/L&S,
decisions, flags, a roadmap, and spec/doc content — and runs that repo through
whatever coding harness you point at it — **Claude Code, OpenCode, Codex,
Mistral Vibe, and Kimi Code**, all sandbox-integrated (or run on the no-docker
host path).
Free to use, open source, MIT License.

> [!class2]
> **Repo:** [github.com/jedbjorn/subfloor](https://github.com/jedbjorn/subfloor) — source, issues, and releases.

![subfloor's Review GUI, Shells tab — a shell's role, mandate, harness token count, editable current state, and identity (seed, lessons, decisions)](https://raw.githubusercontent.com/jedbjorn/subfloor/main/docs/images/cover.png)

### The headliners

- **Cross-provider orchestration.** A sprint runs planner → devs → reviewers
  **across providers** — devs on Codex, reviewers on Claude, the planner woken
  by events, workers booted headless per task. Zero scheduled polling: typed
  message rows, one PR-watch daemon, and session-surviving jobs carry the
  whole coordination. ([*Sprints*](docs/README.md#sprints))
- **A standing team, not a session.** Shells are DB rows — identity, memory,
  decisions, skills — that survive every session and boot on any of four
  harnesses; the same shell can run Claude Code today and OpenCode tomorrow.
  ([*The loop*](docs/README.md#the-loop) · [*Harnesses & models*](docs/README.md#harnesses--models))
- **Sidecars + brokers: capability without credentials.** A sandboxed shell
  tests against real Postgres, drives a real Windows VM, reaches tailnet
  hosts, bounces the host's pm2 stack, and reads the live app DB — while the
  DSN, the SSH key, the tailnet identity, and every route stay on the host,
  behind unix-socket brokers with fail-closed allowlists.
  ([*Opt-in features*](docs/README.md#opt-in-features))
- **Worktrees + guardrails.** Every shell boots into its own git worktree on
  a base pinned to `origin/main`; a branch-guard blocks work on `main` in
  every harness; merging stays the operator's gate. Parallel shells, no
  clobbering, no surprise commits.
  ([*Shells & worktrees*](docs/README.md#shells--worktrees))
- **Self-updating, in place.** `./sc update` pulls the new engine and
  migrates the DB under the fork's feet — memory intact, sound
  `./sc rollback`, and `./sc eject` the day you'd rather own it outright.
  ([*Update a fork*](docs/README.md#update-a-fork))

The bet: **we build the data layer, we rent the harness.** The agent loop, the
tools, the model API are the harness's job. We own identity + memory + content
and render a boot artifact the harness reads natively.

```mermaid
graph TD
  DB[(shell DB)]:::class1 --> REN[render chain]:::class2
  REN --> BOOT[CLAUDE.md / AGENTS.md]:::class2
  BOOT --> H[harness loop]:::class3
  H --> REPO[your repo]:::class4
  DB -.serialize.-> SQL[.sc-state/content.sql]:::class2
```

How the overlay works — every property injected through an extension point the
harness already ships, nothing patched, nothing forked: [*Architecture*](docs/README.md#architecture).

## Quick start

> [!class4]
> **The bar: a reachable docker daemon + one signed-in harness CLI on PATH.** `./sc doctor` reports what it finds and the exact next command. Full prerequisites table (Arch / macOS), docker modes, and the no-docker escape hatch: [*Install*](docs/README.md#install).

Drop subfloor into an existing git repo and boot a shell:

```bash
cd your-repo                                                  # an existing git repo

# 1. Pull in the engine + entry script (files only, no history merge):
git remote add super-coder https://github.com/jedbjorn/subfloor.git
git fetch super-coder
git checkout super-coder/main -- .super-coder sc

# 2. Bootstrap the fork — installs harness CLIs, builds the DB, seeds your starting team:
./sc install

# 3. Sign in to your harness once, on the HOST (not inside the sandbox):
claude                          # or:  opencode auth login  ·  codex login  ·  vibe --setup  ·  kimi login

# 4. Launch the sandbox (server + GUI) and attach a session:
./sc launch
./sc enter                      # auth + pick a shell + pick a harness + boot

# 5. Commit the install (engine is gitignored — only sc + .sc-state + config track):
git add -A && git commit -m "chore: install subfloor"
```

That's the happy path — you're talking to a planner shell in your repo, with a
whole team behind it. Installer internals and harness sign-in, step by step:
[*Install*](docs/README.md#install). First boot and the daily loop, guided:
[*Quick start*](docs/quick-start.md).

## Docs

One page, ten sections — [docs/README.md](docs/README.md), or tab through it
themed: [**open the docs in md-converter**](https://md-converter.designs-os.com/?url=https://github.com/jedbjorn/subfloor/blob/main/docs/README.md).

| Section | What's in it |
|---|---|
| [**Architecture**](docs/README.md#architecture) | The harness-overlay model, the engine/fork boundary, the repo layout |
| [**Install**](docs/README.md#install) | Prerequisites, install & launch, installer internals, harness sign-in |
| [**The loop**](docs/README.md#the-loop) | The everyday cycle: map → spec → build → review → freeze → verify |
| [**Harnesses & models**](docs/README.md#harnesses--models) | Plans over API keys; which model each role runs, and why |
| [**Shells & worktrees**](docs/README.md#shells--worktrees) | How a whole team shares one repo without clobbering it |
| [**Sprints**](docs/README.md#sprints) | The multi-shell mode: declared pushes on a zero-polling event loop |
| [**Update a fork**](docs/README.md#update-a-fork) | `./sc update` / `rollback`; customize vs upstream vs eject |
| [**CLI & dev kit**](docs/README.md#cli--dev-kit) | Every `./sc` command, the `make dos-` aliases, the sandbox toolchain |
| [**Opt-in features**](docs/README.md#opt-in-features) | pg sidecar · Windows Test VM · tailnet / pm2 / db brokers |
| [**Review GUI**](docs/README.md#review-gui) | The localhost GUI's nine tabs + token & session analytics |

> [!class2]
> **Reading the docs.** The docs are themed markdown — GitHub renders the page fine, and the md-converter link above serves the intended render: one tab per section, arrow keys to move between them.

## License

[MIT](LICENSE) © 2026 jedbjorn.
