---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Headless model routing catalogue
roadmap_status: shipped
frozen: false
title: Headless Model Routing
tags: [models, harnesses, headless]
date: 2026-07-21
project: super-coder
purpose: Exact callable model routes
---

# Headless Model Routing

## Overview

Model routing turns a requested harness, selector, and effort level into an
exact locally callable launch. Runtime discovery is authoritative: a public
catalogue entry is not runnable until the installed harness can route it.

> [!class1]
> A requested model or effort is applied exactly or resolution fails before a
> shell session opens. Routing never silently changes provider, model, or effort.

## Refresh

The Shells page **Refresh models** action and `sc models refresh` populate the
same local `model_routes` catalogue from installed harnesses.

- Claude CLI model output.
- Codex's signed-in model cache.
- Kimi aliases from `~/.kimi-code/config.toml`.
- OpenCode's CLI model list.

Advisory provider/catalogue sources never outrank local evidence. A failed
refresh retains prior rows as stale last-known routes and records the error.
Routes are machine and account state, so snapshots exclude them.

## Resolve

```bash
sc models list <harness>
sc models resolve <harness> <selector> --shell <shortname>
```

A successful resolve prints the route source and exact `sc run` invocation.
Resolution rejects advisory-only routes, missing headless adapters, and effort
levels the adapter cannot apply.

```linear
Refresh local routes :::class1 -> Select an alias :::class2 -> Resolve exactly :::class2 -> Launch headlessly :::class3
```

## Harnesses

| Harness | Selector | Headless effort |
|---|---|---|
| Claude | Local CLI alias or model id | `--effort <level>` |
| Codex | Signed-in CLI model id | `model_reasoning_effort` config |
| Kimi | Exact user-local alias | `KIMI_MODEL_THINKING_EFFORT` |
| OpenCode | Provider-prefixed model id | Adapter capability dependent |
| Vibe | Advisory only | No headless model seam |

Kimi's `-m` selects a user-local alias rather than a portable provider id.
The catalogue therefore preserves the configured selector and supported/default
effort values.

## Failure Modes

- **Missing binary or alias:** refresh cannot establish a local route.
- **Stale discovery:** the last route remains visible and labeled stale.
- **Unsupported effort:** resolution fails before token spend begins.
- **Unsupported headless adapter:** interactive visibility does not imply
  headless support.
- **Adapter mismatch:** `sc run` validates the resolved route before opening
  the session archive.

## Generic Use

Callers resolve a route before any bounded non-interactive shell task. The
caller supplies the task and follow-up contract; model routing owns only launch
correctness. It does not create workflow, grant merge authority, or wake another
shell.
