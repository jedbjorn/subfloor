---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Sprint model routing catalogue
roadmap_status: shipped
frozen: false
title: Sprint Model Routing
tags: [sprints, models, harnesses]
date: 2026-07-21
project: super-coder
purpose: Exact callable sprint routes
---

# Sprint Model Routing

## Overview

Sprint model routing turns a planner's harness and model choices into exact, locally callable headless launches. Runtime discovery is authoritative: a model shown by a public catalogue is not considered runnable until the local harness can route it and the requested effort level.

> [!class1]
> A requested model or effort is applied exactly or the launch fails before opening a worker session. Routing never silently changes provider, lineage, model, or effort.

## Refresh

The Shells page **Refresh models** action and `sc models refresh` populate the same runtime `model_routes` catalogue.

Local sources take precedence:

- Claude CLI model output.
- Codex's signed-in model cache.
- Kimi aliases from `~/.kimi-code/config.toml`.
- OpenCode's CLI model list.

Provider APIs, models.dev, and static entries remain advisory fallbacks. Refresh reads routing metadata but never stores credentials. If discovery fails, prior rows remain available as stale last-known routes with the error recorded; a failed refresh does not erase the last successful catalogue.

The catalogue is machine and account state, so it is deliberately excluded from content snapshots. A rebuilt or moved fork refreshes its own routes.

## Resolve

Use the resolver before declaring the sprint's dev and reviewer choices:

```bash
sc models list <harness>
sc models resolve <harness> <selector> --shell <shortname>
```

A successful resolve prints the route source and the exact `sc run` call. The resolver rejects routes that are only advisory, lack a headless adapter, or cannot apply the requested high effort.

```linear
Refresh local routes :::class1 -> Select exact aliases :::class2 -> Resolve both roles :::class2 -> Launch workers :::class3
```

## Harnesses

| Harness | Model selector | Headless effort |
|---|---|---|
| Claude | Local CLI alias or model id | `--effort <level>` |
| Codex | Signed-in CLI model id | `model_reasoning_effort` config |
| Kimi | Exact local alias, such as `kimi-code/k3` | `KIMI_MODEL_THINKING_EFFORT` |
| OpenCode | Provider-prefixed model id | No verified high-effort seam |
| Vibe | Advisory only | No headless model seam |

Kimi's `-m` argument selects a user-local alias, not a portable provider id. The catalogue therefore preserves the configured alias and its effective `support_efforts` / `default_effort` values. A Kimi headless call renders as `kimi -m <alias> -p <prompt>` with effort supplied through the environment.

## Failure Modes

- **Missing binary or alias:** refresh cannot establish a local route; resolve fails with the missing selection.
- **Stale discovery:** the last route is retained and labeled stale rather than deleted.
- **Unsupported effort:** resolve fails before a session or token spend begins.
- **Unsupported headless adapter:** the model may remain visible for interactive use, but it is not sprint-runnable.
- **Adapter mismatch:** `sc run` validates model and effort support before opening the session archive.

## Sprint Use

The `sprint_orchestration` workflow interviews separately for dev and reviewer harness/model choices. Each answered route is resolved lazily before kickoff, then every worker boot uses the returned selector with explicit high effort. Reboots and stall nudges reuse the same declared sprint route.

Model routing governs worker launch correctness. Planner wake-up after `result` or `pr_event` delivery is a separate event-dispatch concern; an available headless route alone does not make a dormant planner autonomous.
