---
title: Subfloor — Quick start
tags: [substrate, onboarding, agentic-coding]
date: 2026-09-07
project: subfloor
purpose: First installation through the first reviewed PR
---

# Subfloor — Quick start

## Overview

This walkthrough takes you from an existing Git repository to a reviewed
change. The [user guide](README.md) owns concepts and ongoing workflows;
`sc help --all` and each verb's `--help` own exact command syntax.

## Install from your host terminal

Use Arch Linux (including CachyOS) or Ubuntu LTS with Git, curl, Python 3.14.x
and its `sqlite3` module. The default sandbox needs a reachable Docker daemon.
For a supervised host process instead, replace `./sc install` below with
`./sc install --runtime host`. On macOS or Windows, do this inside a Linux VM,
preferably on guest-owned storage. `SC_PYTHON` can select an absolute Python path.

```bash
cd your-repo
git remote add -t main super-coder https://github.com/jedbjorn/subfloor.git
git fetch super-coder
git checkout super-coder/main -- .super-coder sc
./sc install
git add -A && git commit --no-verify -m "chore: install subfloor"
```

The installer asks for your operator username, installs missing harness CLIs,
and creates ten shells: two Planners, four Developers, two Reviewers, one
Admin and one Cartographer. Six flavors are available; DevOps can be added when
needed. Optional primary-shell customization is described by `./sc install --help`.

The bootstrap commit is an operator-owned default-branch exception: installation
has already enabled the branch guard. Commit before creating shell worktrees.
Your project remains tracked; the installed engine is a dependency, and private
memory and generated context do not enter Git.

## Launch and sign in

Open a new terminal to load the installed `subfloor` shell function, then run:

```bash
subfloor launch
```

Launch prints the local Review GUI URL; `subfloor url` recalls it. Under the
sandbox runtime, launch builds the image and starts the container. Under the
host runtime, it starts the supervised server process.

Sign in once to your chosen harness **on the host**, using your account:

| Harness | Sign-in entry |
|---|---|
| Claude Code | `claude` |
| Codex | `codex login` |
| OpenCode | `opencode auth login` |
| Mistral Vibe | `vibe --setup` |
| Kimi Code | `kimi login` |

The sandbox uses the host's configured harness credentials. See
[installation](README.md#install) if a CLI, account or Docker prerequisite
is unavailable. Use `subfloor admin` from the host checkout for engine
maintenance or guarded recovery.

## Orient with the Cartographer

Run `subfloor enter`, select the Cartographer, then choose an available harness
and model. The picker shows route support and supported Thinking levels where
available. Ask it to orient and map the repository. The **Repo Map** tab displays
the resulting catalogue; other shells can read it without becoming map owners.

Alternatively, use **Chats → New chat** for a shell and a supported browser
route. Browser and terminal sessions cannot own the same shell at once. Close
a browser conversation before entering that shell from the terminal.

## Plan one bounded change

Open a Planner session with `subfloor enter PLN1`, or select an available
Planner in Chats. Describe one outcome and its constraints. Ask for a feature
and specification with clear scope, prerequisites and acceptance criteria.
Read it in the Roadmap and approve the work when the intended result is clear.
For complex work, ask for an independent review of the specification first.

## Build and review the first PR

1. Ask the Planner to send the approved assignment to a Developer. Enter that
   shell, for example `subfloor enter DEV1`, and ask it to read its inbox.
2. The Developer loads the exact task context, follows the spec ledger, and
   builds on a feature branch in `.sc-worktrees/dev1`. Its `shell/dev1` branch
   is a disposable base pinned to `origin/main`.
3. The Developer runs the project's declared checks, pushes the branch, and
   opens a PR. Ask a Reviewer to check the diff against the specification.
4. Have the Developer address findings and repeat affected checks until the
   review and required CI checks pass.
5. Give an explicit merge directive naming that PR. Outside an armed Sprint,
   green checks and a review alone do not authorize a shell to merge.
6. Ask the Planner to freeze the shipped specification and write the feature
   documentation. The Developer closes cleared flags; the team reconciles its
   worktrees and the Cartographer refreshes the affected map.

The [development loop](README.md#the-loop) explains the role boundaries. In
Chats, you can return to history and send a message to reopen an eligible
closed conversation. Route and ownership checks still apply.

## Move to Sprints

Once the manual handoff is familiar, ask the Planner to prepare a Sprint for
work with independent lanes. It binds exact specs, groups tasks, assigns a
Developer and Reviewer per lane, and checks routes and capacity. Review that
plan and **arm** it when ready: arming records your merge grant for the Sprint's
registered PRs. Each owning Developer still needs live green and approved
merge authorization.

Monitor the **Sprints** Board instead of manually relaying every handoff.
Follow the [Sprints workflow](README.md#sprints) for pickup, review,
pause/recovery, conformance and cleanup.

For daily operation, use [browser conversations](README.md#browser-conversations),
[the command route map](README.md#cli--dev-kit), and
[maintenance and removal](README.md#update-a-fork).
