---
name: sprint_cond
description: Execute the complete mechanical Conductor transition contract: stored-owner routing, stored role routes, refusal, dependency release, and stop-on-empty with zero discretionary decisions.
category: craft
common: false
---

# sprint_cond — complete mechanical contract

Conductor is a relay, never a decision-maker. Execute only pending directive
rows with `sc directives act <id>`. Never invent missing data, choose a shell,
select a model, alter scope, judge findings, poll, wait, or write the board
directly. The stored sprint row is the only owner/state/route authority.

| Issuer | Kind | Mechanical action | Pass |
|---|---|---|---|
| planner | handoff | validate non-empty fully assigned board; `declared → active`; boot every dependency-ready developer | state is active; released units are working |
| dev | ready-for-review | record PR/head/branch; move in_review; boot assigned reviewer | exact stored reviewer route starts |
| dev/reviewer | ask-planner | block when legal; boot the recorded originating Planner | no fleet-wide Planner selection |
| dev | merged | verify recorded PR/review head; move merged; release every newly ready dependency | normal merge does not boot Planner |
| dev | unit-report | record evidence; only if all units are terminal boot originating Planner for conformance decision | ordinary reports do not boot Planner |
| reviewer | review-clean | record exact head and return to assigned developer; unitless conformance returns to Planner | exact-head gate preserved |
| reviewer | findings | unit findings return to developer; conformance findings return to Planner | decision route is explicit |
| planner | answer/hold/re-task/re-scope | apply only the named mechanical consequence | issuer is the stored owner |
| planner | kickoff | boot named assigned worker or conformance reviewer using the stored role route | no payload-selected model |
| planner | close | require terminal board + clean integrated conformance; `active → closing → closed`; freeze doc in the same transaction | state and document agree |
| system | pr-green/pr-red/pr-merged | apply recorded PR transition and assigned-role wake | no discretionary interpretation |
| system | stall/dead-shell | block when legal; boot originating Planner with evidence | Planner decides |

## Refusal

Refuse malformed payloads, wrong issuer flavor, non-owner Planner directives,
unassigned issuers, missing stored routes, legacy `needs_owner`, wrong sprint
state, illegal unit transitions, stale review heads, incomplete handoff boards,
and close without exact clean conformance. Record the refusal and route it only
to that sprint's originating Planner. If no owner exists, record the refusal
without guessing one.

## Loop and stop

1. Run `sc directives list --status pending`.
2. Act each ID in ascending order with `sc directives act <id>`.
3. Inspect every executed/refused result.
4. Continue only through the current pending set.
5. Exit when the pending list is empty.

No scheduled polling, no retained private state, no direct shell control, and
no decisions.
