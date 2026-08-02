---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Sprints v2.0 — collaborative orchestration
roadmap_status: in_progress
frozen: false
title: "QAQC: spec #46 rev4 — REV2 re-round"
tags: [sprints, qaqc, review]
date: 2026-07-31
project: super-coder
purpose: Scoped QAQC re-round verdict on Sprints v2.0 spec (REV4)
---
# QAQC: spec #46 rev4 — REV2 re-round

- **Reviewed artifact:** spec doc #46 "Sprints v2.0 — Collaboration Loop", REV4 banner revision (applies the phase-0 QAQC write-backs from REV2 doc #52 and REV1 doc #53, plus both rounds' Low write-backs).
- **Reviewer:** REV2 (shell #8), scoped re-round for SPRINT 51 phase 0d (msg #425). Findings only; no spec edits, no code, no git.
- **Scope:** verify each of the five Medium fixes against the REV4 body, adversarially sweep the changed text for new Medium+ conflicts, confirm the Low write-backs landed.

## Verdict: PASS

All five Medium findings from the two phase-0 rounds are resolved in the REV4 body, the changed text introduces no new Medium-or-above conflict, and all Low write-backs landed. Spec #46 REV4 passes this QAQC round.

## Medium fix verification (5/5 resolved)

### #64 (M1) — current-conversation wake pointer: RESOLVED
Participant Conversations now defines exactly one **current Sprint conversation** per participant — a durable pointer updated in the same transaction that creates or selects a conversation. Every wake class (watcher red/green, review outcome, next-unit assignment, nudge, escalation) is delivered to the pointer's target at delivery time, and the amber pill follows it. The pointer-moving transitions are enumerated: assignment selects the persistent work conversation, changes-requested creates and selects the fresh fix conversation, approval creates and selects the fresh merge conversation; fix-loop red/green wakes land in the fix conversation; the post-merge next assignment returns the pointer to the persistent lane. Cross-checks: Dev/Review loop steps 6 and 9 restate the rule consistently, GitHub Observation delivers red/green "to its current Sprint conversation", the data model carries `current conversation` on `sprint_participants` (a legal projection — one authoritative transition path maintains it), and the Stage 2 gate requires the pill to follow the current conversation. The M1 ambiguity is closed.

### #65 (M2) — durable decline disposition: RESOLVED
Sprint Messages now defines a durable disposition per actionable message: `pending` until acted on, `accepted` when marked read, `declined` with a recorded reason. Decline is a distinct act from mark-read, executed in one transaction with the reason, sends a result to the Planner, and stops wakes; the disposition is captured alongside read state as system evidence, and the conceptual data model names "durable decline dispositions" on `sprint_messages`. The acceptance-audit meaning M2 flagged is now explicit.

### #66 (M3) — abort authority + prepared exit: RESOLVED
A dedicated paragraph names the authority: only the Planner or FnB may abort — from `armed`, `paused`, or `prepared` — and participant shells may pause but never abort. `prepared → aborted` is the discard route for a never-arming Sprint, records a stub abort report, and deletes nothing. The lifecycle graph and state table carry the `P → X` edge; the System row owns auto-pause. Every transition now has exactly one named authority, satisfying the checklist line.

### #67 (S2QA-REV1-01) — merge grant checks-green bar: RESOLVED
The Sprint merge grant term now requires review passed with all Medium-and-above findings addressed **and the PR's checks green at merge time**, with staleness semantics stated (an earlier green is stale once checks re-run or the base moves; a merge attempt against regressed checks waits for the watcher's next green wake). Loop step 7 has the Developer verify checks are still green in the merge conversation before merging; the Stage 6 gate reads "passed review plus checks green at merge time permits merge under the grant". Term, loop, and gate agree — the v1 green+clean bar is restored. The regressed-checks case is also coherent with the pointer rule: the next green wake lands in the merge conversation, which remains current until the next assignment.

### #68 (S2QA-REV1-02) — resume eligibility consequence + non-owner spec edits: RESOLVED
Pause and Recovery now defines the consequence: eligibility reconciliation informs and never silently blocks — resume proceeds on the Sprint's bound revision, drift is recorded as evidence and actively notified to the Planner, and the Planner judges continue, re-plan, or abort. Non-owner edits are dispositioned: a bound governing spec may be edited only by its owning Planner or the FnB, and any other write is recorded as drift evidence with the same notification. The bound revision's QAQC approval remains valid for the running Sprint; ineligibility gates new arming only. Stage 8's gate matches. The vacuous check now has teeth, and the non-owner gap is closed.

## Low write-back confirmation (REV2 round, 5/5 landed)

- **L1 — liveness expectation definition:** landed. "An accepted active work expectation is any accepted actionable Sprint message — work-unit assignments and review requests alike; review expectations are monitored even though reviews are not work units." Stalled reviews are inside nudge/escalation coverage.
- **L2 — fallback clarification:** landed. The policy now names the Planner's own primary provider as the exhausted one, boots escalation delivery on the fallback via the linked-replacement-conversation mechanism, and explicitly never re-boots the silent worker on a fallback; no-fallback is recorded with pause surfaced as an option.
- **L3 — board scoping:** landed. The wave line is now "stored for planning and later display (the board web GUI itself remains future scope per Non-Goals)", and Non-Goals carries the scoping parenthetical. Remaining "board" references ("the board is a projection of durable Sprint state", watcher "updates the Sprint board") read coherently through that lens — projection/state now, GUI later.
- **L4 — reviewer-decline routing:** landed. "A declined review request routes back to the Planner for reviewer reassignment", alongside the work-unit return-to-pool rule.
- **L5 — auto-pause ratification:** landed. "Pausing the whole Sprint on one terminal wake failure is deliberate v2.0 posture — a loud stall beats partially silent operation; scoping the pause to the affected lane is future refinement."

REV1's Lows were also dispositioned in REV4 (noted for completeness, not part of my confirmation scope): closed-without-merge PRs now actively notify the Planner; assigned Reviewer is recorded on the work unit and inherited by its registered PR; the single-armed-Sprint invariant is enforced inside the arming transaction.

## Adversarial sweep of changed text — no new Medium+ found

Changed regions swept: Terms (merge grant), Participant Conversations (pointer), Sprint Messages + delivery rules (dispositions, decline), GitHub Observation routing, Dev/Review loop steps 3–9, Lifecycle graph/table and abort paragraph, Pause and Recovery (eligibility), Liveness (expectation, fallback), Work and Parallelism (wave), wake-budget posture, Conceptual Data Model, Stage 6/8 gates, Non-Goals. Interactions challenged: pointer vs. routing table, decline vs. read-equals-acceptance, regressed-checks merge wait vs. pointer, auto-pause authority, abort from prepared, drift handling vs. approval ineligibility. Two Low residues below; nothing rises to Medium.

### New Low findings (recorded, non-blocking)

- **R4-L1 — routing table wording residue.** The GitHub Observation routing table still says a green wake "routes readiness judgment and review handoff through the Developer's **persistent** conversation". During a fix loop the current conversation is the fix conversation, and the pointer rule (stated authoritatively in Participant Conversations, step 6, and the paragraph above the table) governs delivery — so the table's "persistent" phrasing is accurate only for the primary path. Not Medium: the pointer rule is explicit in three places and the table governs active/passive policy, not delivery target. One-word fix ("current") when the spec next opens.
- **R4-L2 — "may be edited only" vs. record-don't-block.** Pause and Recovery says a bound spec "may be edited only by its owning Planner or the FnB" yet the enforcement is to record any other write as drift and notify — the prohibition reads as a hard guard while the mechanism is evidence-only. Coherent with the spec's evidence-over-block philosophy and almost certainly deliberate; a clause like "edits by others are not blocked but are recorded as drift" would remove the friction.

## Disposition

- Verdict: **PASS** vs REV4.
- Flags #64, #65, #66 closed by REV2 with this verdict as evidence.
- Spec #46 REV4 is eligible for Sprint arming per its own Preparation and QAQC rules (all Medium+ resolved; approval bound to this exact revision).
