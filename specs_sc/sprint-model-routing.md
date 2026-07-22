---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Sprint model routing catalogue
roadmap_status: shipped
frozen: true
title: Sprint Model Routing
tags: [sprints, models, harnesses]
date: 2026-07-21
project: super-coder
purpose: Reliable sprint model calls
---

# Sprint Model Routing

## Intent

Give sprint planners a locally authoritative, self-healing catalogue of exact model routes. The Shells GUI `Refresh models` action populates it; the orchestration skill lazy-loads only the selected dev and reviewer routes through a CLI resolver.

> [!class1]
> Sprint launches use high effort for every harness that exposes an effort control. A requested model must be applied or the launch fails before opening the worker.

## Requirements

1. Add runtime catalogue storage for harness/model routes, freshness, discovery source, installed CLI version, runnable status, and supported effort metadata. Derived catalogue rows are not serialized into content snapshots.
2. Refresh from locally authoritative sources before advisory public sources: Codex signed-in model cache, Kimi configured aliases, OpenCode CLI listing, then provider APIs/models.dev/static fallbacks where appropriate.
3. Preserve stale rows when a refresh source fails; mark their status and retain the last successful route instead of erasing it.
4. Expose a CLI resolver that returns the exact `sc run` invocation, route source/status, and corrective error for one harness/model selection.
5. Make headless command construction harness-aware. Kimi must accept configured aliases such as `kimi-code/k3` and render `kimi -m <alias> -p <prompt>`.
6. Never record or print a requested model that the selected adapter did not apply.
7. Sprint launches default to high effort across Claude, Codex, and Kimi; unsupported harnesses fail clearly or report that effort control is unavailable.
8. Update Default Models to show local route status and make `Refresh models` populate the runtime catalogue.
9. Update `sprint_orchestration` to resolve the two interviewed routes lazily before kickoff and use the resolver's exact command.

## Done

- Claude Fable/Opus and Codex Sol/Terra resolve to runnable high-effort launches.
- Kimi local aliases resolve to runnable high-effort launches and appear exactly in the generated argv.
- Missing binaries, missing aliases, stale discovery, and unroutable selections produce actionable status without silent fallback.
- Existing flavor defaults and advisory model suggestions continue working.
- Migration/rebuild, focused backend tests, UI tests or static checks, full test suite, lint, snapshot/render verification, and skill reseed coverage pass.

## Non-Goals

- Spending tokens to probe every model.
- Silently switching providers or model lineages when a route fails.
- Making Vibe sprint-runnable before it has a verified headless model seam.
