---
title: Subfloor
tags: [substrate, shells, agentic-coding, harness-agnostic]
date: 2026-09-07
project: subfloor
purpose: Product overview and installation entry
---

[![tests](https://img.shields.io/github/actions/workflow/status/jedbjorn/subfloor/tests.yml?style=flat-square&label=tests)](https://github.com/jedbjorn/subfloor/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-6b46c1?style=flat-square)](LICENSE)

# Subfloor

## Overview

**An AI development team that stays with your repository.** Subfloor gives
agents durable identity, memory, specifications and handoffs, then runs them
through your coding harness. Install it into an existing Git project and work
with a standing team from the terminal or local browser UI.

**[Quick start](docs/quick-start.md) · [User and operator guide](docs/README.md)**

![Subfloor terminal demo: select a shell and harness, then enter its session](https://raw.githubusercontent.com/jedbjorn/subfloor/main/docs/demo.gif)

## What you get

- **A standing team.** A fresh installation seeds ten shells: two Planners,
  four Developers, two Reviewers, one Admin and one Cartographer. Their memory
  and decisions survive sessions. Six flavors are available, including DevOps.
- **Durable browser conversations.** Chats keeps message history, queues,
  exact session resume, Stop/Close controls and read-only Diff review.
  [Continue a conversation](docs/README.md#browser-conversations).
- **Sprints v2.** Have a Planner prepare independent work lanes, choose routes
  and assign reviewers. Arm the Sprint to authorize coordinated execution,
  monitor its Board, and follow review through authorized merge and cleanup.
  [Run a Sprint](docs/README.md#sprints).
- **Five harness adapters.** Claude Code, Codex, OpenCode, Mistral Vibe and
  Kimi Code share the same shell context. Supported surfaces differ by adapter;
  the current picker and route checks show what is available.
  [Harnesses and models](docs/README.md#harnesses--models).
- **Guarded worktrees.** Developers work on branches in separate worktrees.
  Outside an armed Sprint, a merge needs your explicit directive naming the PR;
  inside one, the owning Developer needs live authorization under your recorded
  Sprint grant. [The development loop](docs/README.md#the-loop).
- **Recoverable lifecycle.** Update the engine in place, roll back a bad
  engine/DB pair, or remove Subfloor through its guarded lifecycle. Private
  instance state stays separate from your project history.
  [Maintain an installation](docs/README.md#update-a-fork).

Subfloor owns the team's context and coordination; the harness owns the model,
tools and agent loop. The ten-tab Review GUI gives you a local view of both
ongoing work and durable records.

## Install

Use Linux: Arch Linux (including CachyOS) or Ubuntu LTS, Git, curl,
Python 3.14.x with `sqlite3`, and a reachable Docker daemon for the default sandbox.
Select `./sc install --runtime host` for the supervised host runtime without
Docker. On macOS or Windows, create a Linux VM; keep the checkout on guest storage.
Set `SC_PYTHON` to an absolute interpreter path if needed.

Run these commands from your own host terminal:

```bash
cd your-repo
git remote add -t main super-coder https://github.com/jedbjorn/subfloor.git
git fetch super-coder
git checkout super-coder/main -- .super-coder sc
./sc install
git add -A && git commit --no-verify -m "chore: install subfloor"
# Open a new terminal so the installed subfloor shell function is available.
subfloor launch
# Sign in to your chosen harness on the host before entering.
subfloor enter
```

The installer prompts for your operator username and seeds the team. It
installs missing harness CLIs; account sign-in remains yours. The bootstrap
commit above is an operator-owned exception on the default branch because
installation has already activated the branch guard. Subsequent shell work
uses feature branches.

Follow the **[quick start](docs/quick-start.md)** for harness sign-in, the first
Cartographer and Planner sessions, and your first reviewed PR. Use
`subfloor admin` from the host checkout for maintenance.

## Documentation

| Start here | Purpose |
|---|---|
| [Quick start](docs/quick-start.md) | Installation through the first reviewed PR |
| [User and operator guide](docs/README.md) | Everyday concepts, Sprints, browser conversations and lifecycle |
| [Engine reference](.super-coder/README.md) | Source layout, private state, rendering and focused runbooks |
| `sc help --all` and each verb's `--help` | Current command inventory and exact syntax |

The guide is themed Markdown: read it on GitHub or
[open its tabbed presentation](https://md-converter.designs-os.com/?url=https://github.com/jedbjorn/subfloor/blob/main/docs/README.md).

## License

[MIT](LICENSE) © 2026 jedbjorn.

### Browser driving

Opt in with `./sc feature enable browser`, then use **Scripts → Browser** to
link a dedicated Chromium profile named **Subfloor** with the Playwright
Extension. You create the profile, sign in to the intended accounts, keep its
window open, and approve each new shell connection. Claude, Codex, and OpenCode
on bare metal can then use `drive_browser` for a named task or logged-in preview
check. Kimi, Vibe, and container seats are unsupported.

`./sc browser doctor --json` verifies the locked package set and setup.
`./sc browser disarm` suspends access; `./sc feature disable browser` stops the
services and removes the feature grants. See the
[browser design and operator checklist](.super-coder/docs/browser-driving.md)
for setup, boundaries, and the separate operator-run real-extension acceptance.
