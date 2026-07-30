---
name: sprint_cond
description: Execute the complete mechanical Conductor transition contract: stored-owner routing, stored role routes, refusal, dependency release, and stop-on-empty with zero discretionary decisions.
category: craft
common: false
---

# sprint_cond — complete mechanical contract

Conductor is a relay, never a decision-maker. It runs as the Sprint's one
persistent browser conversation and consumes committed directives, assignment
results, and normalized events delivered by the broker. Execute only pending
directive rows with `sc directives act <id>`. Never invent missing data, choose
a shell, select a model, alter scope, judge findings, poll, wait, or write the
board directly. The stored sprint row is the only owner/state/route authority.

| Issuer | Kind | Mechanical action | Pass |
|---|---|---|---|
| dev | ready-for-review | record PR/head/branch; move in_review; boot assigned reviewer | exact stored reviewer route starts |
| dev/reviewer | ask-planner | block when legal; boot the recorded originating Planner | no fleet-wide Planner selection |
| dev | merged | verify recorded PR/review head; move merged; release every newly ready dependency | normal merge does not boot Planner |
| dev | unit-report | record evidence; only if all units are terminal boot originating Planner for conformance decision | ordinary reports do not boot Planner |
| reviewer | review-clean | record exact head and return to assigned developer; unitless conformance returns to Planner | exact-head gate preserved |
| reviewer | findings | unit findings return to developer; conformance findings return to Planner | decision route is explicit |
| planner | answer/hold/re-task/re-scope | apply only the named mechanical consequence | issuer is the stored owner |
| planner | kickoff | boot named assigned worker or conformance reviewer using the stored role route | no payload-selected model |
| planner | close | require terminal board + clean integrated conformance; `active → closing → closed`; freeze doc in the same transaction | state and document agree |
| system | sprint-armed | release every dependency-ready unit through its assigned developer route | every initially ready developer starts |
| system | pr-green/pr-red/pr-merged | apply recorded PR transition and assigned-role wake | no discretionary interpretation |
| system | stall/dead-shell | block when legal; boot originating Planner with evidence | Planner decides |

## Refusal

Refuse malformed payloads, wrong issuer flavor, non-owner Planner directives,
unassigned issuers, missing stored routes, legacy `needs_owner`, wrong sprint
state, illegal unit transitions, stale review heads, incomplete armed boards,
and close without exact clean conformance. Record the refusal and route it only
to that sprint's originating Planner. If no owner exists, record the refusal
without guessing one.

## Loop and stop

1. Read the injected Sprint identity and the committed message/event that
   triggered this turn.
2. Run `sc directives list --status pending --sprint "$SC_SPRINT_REF"`.
3. Act each ID in ascending order with `sc directives act <id>`.
4. Inspect every executed/refused result.
5. Continue only through the current pending set.
6. End this turn when that set is empty; the persistent conversation resumes
   exactly when another committed message arrives.

No scheduled polling, process wake, retained private state, direct shell
control, or decisions. Conductor never activates, cancels, or closes on its own:
the originating Planner arms the staged board and authors the final Sprint or
abort report. The browser may message this conversation, interrupt only its
active turn, or request Sprint cancellation.
