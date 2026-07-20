---
title: super-coder — Architecture
tags: [substrate, shells, agentic-coding, harness-agnostic, sqlite]
date: 2026-07-20
project: super-coder
purpose: The harness-overlay model, the engine/fork boundary, and the repo layout
---

# Architecture

### A harness overlay

A coding harness ships the **loop** — model, tools, context window — and
forgets everything else between sessions. super-coder is a **harness
overlay**: it supplies the properties a harness doesn't keep, and injects
every one of them through an extension point the harness itself already
ships — nothing patched, nothing forked:

| Property | Ours | Enters the harness via |
|---|---|---|
| **Boot context** | identity · memory · laws · current state | the boot doc it reads natively (`CLAUDE.md` / `AGENTS.md`) |
| **Native tooling** | the `./sc` CLI — `mem` · jobs · watches · brokers | the shell it already executes commands in |
| **Skills** | DB-canonical catalogue, per-shell grants | the skill dirs it already discovers |
| **Guardrails** | branch-guard · sandbox · worktrees | its own hook / plugin seams + the environment it boots into |
| **Orchestration** | eventing · headless boots · sprints | its headless mode (`claude -p` · `codex exec` · `opencode run`) |

The overlay makes the harness you rent behave like it has all of this built
in — without touching its loop. Think **distro over kernel**: the kernel (the
harness) runs the process; the distro (super-coder) gives it users, packages,
init, and permissions. Four harnesses, one overlay, zero forks of anyone's
loop — that is what harness-agnostic means in practice, and it's why a fork
is cheap: same overlay, whichever kernel you rent underneath.

This repo is also **dogfood**: super-coder maintains super-coder. Its own
`.super-coder/` engine manages the maintainer shell that builds it.

```stats
:::class1
value: 5
label: Coding harnesses
description: Claude · Codex · OpenCode · Vibe · Kimi
:::class3
value: 5
label: Shell flavors
description: planner · reviewer · dev · cartographer · admin
:::class2
value: 8
label: Review-GUI tabs
:::class2
value: 88xx
label: Per-repo port band
```

### Layout

```
.super-coder/         the engine — a gitignored, materialized DEPENDENCY in a
                      fork (see .super-coder/README.md); tracked only in this
                      source repo, where the engine IS the project
.sc-state/            fork-owned, tracked: content.sql (DB serialization / memory)
                      + engine.ref (the upstream SHA the engine is pinned at)
specs_sc/ docs_sc/    rendered from the DB, read-only (the _sc suffix = provenance)
skills_sc/ roadmap_sc.md
.claude/skills/       per-shell skills, rendered at boot — gitignored
.sc-worktrees/        one git worktree per shell — gitignored (admin excepted;
                      see "How shells share one repo")
CLAUDE.md / AGENTS.md boot artifact — gitignored, rebuilt at launch
```

A fork's git surfaces show **only its project** — the engine is a dependency,
not committed source, exactly like `node_modules/`. The one fork-owned artifact
that must survive is its DB, serialized to the tracked `.sc-state/content.sql`.
