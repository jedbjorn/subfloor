---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Sprints v2.0 — collaborative orchestration
roadmap_status: in_progress
frozen: false
title: Sprints v2 completion doctrine
tags: [sprints, completion, authority]
date: 2026-08-02
project: super-coder
purpose: Define terminal-run deferral
---

# Sprints v2 completion doctrine

## Completion ownership

> [!class1]
> On `completed`, terminal cleanup defers the active run owned by the Sprint's originating Planner. The caller does not determine which run is deferred.

Both the owning Planner and FnB may authorize `sc sprint complete`. In either case, cleanup keys the deferral to `originating_planner_shell_id`: the owning Planner's live run receives close intent but no interrupt, so it can emit its terminal response before broker finalization closes the conversation.

- Other active participant runs still receive close intent and interruption.
- Queued follow-up turns are cancelled for every linked conversation.
- `aborted` remains interrupt-all, including the owning Planner.
- Completion replay remains idempotent and creates no second lifecycle or close event.

This records decision #59 and Sprint maintenance spec #78 Unit 3 as the feature doctrine. The regression test is `test_fnb_completion_still_defers_owning_planner_run`.
