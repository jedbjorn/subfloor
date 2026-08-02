---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Sprints v2.0 — collaborative orchestration
roadmap_status: in_progress
frozen: false
---

# QAQC: spec #46 REV4 — REV1 scoped re-round

- **Spec:** #46, seq 1, feature 31 — Sprints v2.0 collaboration loop
- **Reviewed revision:** REV4 banner (2026-07-31), live DB body via `sc mem get documents --doc 46`, SPRINT 51 phase 0c (task msg #424). Scoped re-round on the union of REV1 doc #53 and REV2 doc #52 findings.
- **Reviewer:** REV1 (shell 7). Findings only — no spec edits.
- **Verdict: PASS** — all five Medium fixes verified against the REV4 body; all claimed Low write-backs landed; adversarial sweep of the changed text found no new Medium-or-above conflict. One new Low recorded.

## Medium fix verification (5/5)

### #64 (REV2 M1) — current-conversation wake pointer: RESOLVED
Participant Conversations now defines exactly one **current Sprint conversation** per participant — a durable pointer updated in the same transaction that creates or selects a conversation — and states every wake (watcher red/green, review outcome, next-unit assignment, nudge, escalation) is delivered to the pointer's target **at delivery time**, with the amber pill following it. The three pointer transitions are named (assignment → persistent work conversation; changes-requested → fresh fix conversation; approval → fresh merge conversation), including the explicit fix-loop consequence ("red and green watcher wakes therefore land in the fix conversation") and the post-merge return ("the next assignment returns the pointer to the persistent work conversation"). Dev loop steps 3, 6, 7, 9 and the watcher section ("delivered to its current Sprint conversation") all tell the same story. The pronoun ambiguity REV2 flagged is gone. One residual phrase contradicts it — see Low L1 below.

### #65 (REV2 M2) — durable decline disposition: RESOLVED
Sprint Messages now names three durable dispositions for actionable messages: `pending` / `accepted` (on read) / `declined` (with recorded reason). Decline resolves the message and stops waking; the reason is recorded in the same transaction that sets the disposition; the disposition is captured alongside read state as system evidence; the conceptual data model entry for `sprint_messages` now includes "durable decline dispositions". The unnamed third state REV2 flagged is now explicit.

### #66 (REV2 M3) — abort authority + prepared exit: RESOLVED
Roles and Authority now states: "only the Planner or FnB may abort — from `armed`, `paused`, or `prepared` — and participant shells may pause but never abort." The lifecycle graph gains `prepared → aborted`, the `prepared` row names the transition and authority, and aborting a prepared Sprint is defined as the discard route (stub abort report, deletes nothing). One unambiguous authority per transition, as the checklist demands.

### #67 (REV1 S2QA-REV1-01) — merge-grant checks-green bar: RESOLVED
Terms now defines the grant as merge once review passes with Medium+ addressed **and the PR's checks are green at merge time**, with the stale-green semantics spelled out ("an earlier observed green is stale once checks re-run or the base moves; a merge attempt against regressed checks waits for the watcher's next green wake"). Dev loop step 7 ("verifies the checks are still green and merges under the Sprint merge grant") and the Stage 6 gate ("passed review plus checks green at merge time permits merge under the grant") match. The v1 green+clean bar is restored; the relaxation is closed.

### #68 (REV1 S2QA-REV1-02) — resume eligibility consequence + non-owner edits: RESOLVED
Pause and Recovery now defines the consequence: eligibility reconciliation informs and never silently blocks — if a governing spec body changed while paused, resume proceeds on the bound revision, the drift is recorded as evidence and **actively notified to the Planner**, who judges continue / re-plan / abort. Non-owner edits are dispositioned: a Sprint-bound governing spec may be edited only by its owning Planner or the FnB; any other write is recorded as drift evidence and notified the same way. The bound revision's QAQC approval remains valid for the running Sprint (ineligibility gates new arming only), answering the approval-status question. The check is no longer vacuous.

## Low write-back verification

REV1's adopted Lows (all landed):

- **L1 (closed-unmerged routing):** the notification routing table now gives the Planner an **active** notification "when a registered PR is closed without merge", passive for all other transitions. The ~20-minute liveness-latency discovery gap is closed.
- **L4 (reviewer binding):** "A registered PR's assigned Reviewer is inherited from its owning work unit", and the work-unit record plus `sprint_work_units` in the data model now name the assigned Reviewer. The binding point is defined.
- **L5 (armed-uniqueness invariant):** "At most one armed Sprint per installation is a uniqueness invariant enforced inside the arming transaction itself, not only by the preparation-time check." The TOCTOU gap is closed.

REV2's Lows, spot-checked as claimed in the banner: L1 (accepted work expectation now explicitly includes review requests) ✓; L2 (fallback line now names the Planner's own provider and forbids re-booting the silent worker) ✓; L3 (wave display reworded to "stored for planning and later display", Non-Goal scoped) ✓; L4 (declined review request now routes back to the Planner for reviewer reassignment) ✓; L5 (whole-Sprint auto-pause breadth ratified as deliberate v2.0 posture, lane-scoped pause deferred) ✓.

## Adversarial sweep of changed text — no new Medium+

Challenged the new/changed passages for contradictions with the rest of the body:

- **Pointer vs. merge-grant interaction:** a red regression after review-pass lands in the merge conversation (current pointer) and the grant's green-at-merge bar plus "waits for the watcher's next green wake" compose correctly — the wake that re-enables merge arrives where the Developer is waiting. Consistent.
- **Pointer vs. post-merge window:** merged transitions are passive for the Developer, so no wake targets a dying merge conversation; the next assignment re-points to the persistent lane. Consistent.
- **Prepared-abort vs. pause authority:** abort from `prepared` records a stub report and deletes nothing; no pause transition out of `prepared` is implied, and pausing a Sprint with no running services would be meaningless. No gap.
- **Decline vs. acceptance audit:** `accepted` remains exactly read-equals-acceptance; `declined` is a separate terminal disposition with a mandatory reason, so acceptance audits are not diluted. Consistent.
- **Non-owner edit rule vs. Preparation section:** "recorded and reported, not blocked" (owning Planner) and "drift evidence + Planner notification" (non-owner) align; approval ineligibility gates arming only in both places. Consistent.

## New finding

### Low (recorded, not blocking)

- **L1 — Routing table residue: green wake "through the Developer's persistent conversation".** The notification routing table (GitHub Observation) still says a green wake "routes readiness judgment and review handoff through the Developer's persistent conversation", contradicting the new pointer rule for the fix-loop case, where the current conversation is the fix conversation (confirmed by step 6 and the watcher paragraph immediately above the table: "delivered to its current Sprint conversation"). Four explicit statements of current-conversation delivery outweigh the one stale phrase, so no reasonable implementer should diverge — but the phrase should read "current Sprint conversation". One-line edit; may ride the next revision.

## Recommendation

REV4 resolves the union of both rounds' Mediums and lands the claimed Low write-backs. Spec #46 REV4 passes QAQC. Closing my flags #67/#68. The single new Low is wording residue, fixable in one line at the Planner's convenience; it does not gate arming eligibility.
