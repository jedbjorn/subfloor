---
name: sprint_onboarding
description: Explain the browser-native Sprint lifecycle, ownership, observation, and cancellation without changing Sprint state.
category: craft
common: false
---

# sprint_onboarding

Use this explanatory skill when the FnB asks what a Sprint is, how to start
one, who controls it, what happens after staging, or how the browser-native loop
works. Do not declare, arm, cancel, or otherwise mutate a Sprint while
explaining it.

## The lifecycle

1. The governing spec first receives at least one review-shell QAQC pass.
2. The originating Planner interviews the FnB, decomposes the reviewed spec,
   assigns routes and workers, stages the complete board, and arms it.
3. Arming atomically starts one hidden, headless, persistent Conductor
   conversation. The browser never performs a second activation step.
4. Conductor owns mechanics only. Fresh headless Planner, Developer, Reviewer,
   and conformance conversations each perform one bounded assignment and exit.
5. Typed results, failures, and normalized events return durably to the same
   Conductor conversation; no terminal or manual message relay is required.
6. After clean integrated conformance, the same originating Planner writes the
   Sprint report and alone authorizes close.
7. **Sprints** in the browser shows the live board, assignments, evidence, and
   Conductor transcript. The FnB can message Conductor or stop its active turn.
8. **Cancel Sprint** stops queued/running work and returns the originating
   Planner for a durable abort report. It preserves Sprint history.

## Boundary

The browser observes and provides intervention controls; it is not the workflow
engine. Conductor makes no product decisions. Workers do not become persistent
chats. The originating Planner owns scope, rulings, arming, reporting, and
terminal authority.

When the FnB is ready, direct them to ask the Planner to stage and arm the
reviewed spec with `sprint_pln`.
