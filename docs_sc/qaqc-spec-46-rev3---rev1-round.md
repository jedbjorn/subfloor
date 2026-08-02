---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Sprints v2.0 — collaborative orchestration
roadmap_status: in_progress
frozen: false
---

# QAQC: spec #46 rev3 — REV1 round

- **Spec:** #46, seq 1, feature 31 — Sprints v2.0 collaboration loop
- **Reviewed revision:** REV3 banner (2026-07-31), current DB body retrieved via `sc mem get documents --doc 46` on 2026-07-31 ~18:40Z. sha256 of the retrieved body text: `4510e809f21b526892b42151e6cc19afcfaae590e5f30fafae477ff36f468438`. Task #385 cited canonical REV3 hash `913c94ccbbe4ce94e8dcfa31d196b968ad107add6f6e2851efda34fe9e228979`; the live body still carries the REV3 banner, and this round was run against the current body per task #419.
- **Reviewer:** REV1 (shell 7), independent round under SPRINT 51 phase 0a. No coordination with REV2's parallel round.
- **Verdict: FAIL** — two Medium findings stand (S2QA-REV1-01, S2QA-REV1-02). Per spec: Medium+ must be resolved before approval/arming eligibility.

## SC-025 disposition (the REV3 change)

SC-025 (green-PR wake wording conflict) is resolved as claimed. The green-wake path is now consistent end to end: Terms (wake), GitHub Observation (active notification on every newly observed red/green, first-observed state notified once, restart/resume dedup against last durable observed state), the routing table (Developer active on red and green), Dev/Review loop steps 3–4 (green → readiness judgment → active review request; coalescing behind an active turn), Stage 5/6 gates, and the adversarial acceptance case all tell the same story. No leftover "passive green" wording found. The REV3 banner claim checks out.

## Checklist challenges (all 15 items)

1. **Lifecycle transition authority** — mostly explicit (resume: "Only the Planner or FnB"; complete: Planner/FnB). Two residual ambiguities recorded as Lows L2/L3 (abort authority asymmetry; no exit from `prepared`).
2. **Red/green active wakes, first-observation, restart/resume dedup** — satisfied; see SC-025 disposition.
3. **Fixed wake, acceptance, decline, coalescing, 3-attempt budget** — satisfied at contract level; decline path leaves units reassignable; budget terminates in visible auto-pause, never an endless loop.
4. **Nudge precedes escalation; no repeated worker booting** — satisfied: one nudge per silence episode, one deduped Planner escalation.
5. **FnB conversation entry without liveness change** — satisfied explicitly ("viewing… does not count as shell activity").
6. **Zero idle GitHub calls outside armed** — satisfied explicitly; snapshots on registration/arm/resume only.
7. **Single authoritative owner for PR and QA state** — satisfied: watcher owns PR state; QAQC approval is a durable record, never a copied boolean; duplicated derivable state is prohibited.
8. **Merge grant scoped, recorded at arming, sufficient** — scoped and recorded, yes; **sufficient, no** — see S2QA-REV1-01 (Medium).
9. **Conversation topology (persistent dev lane, fresh review-outcome chats)** — satisfied; the token-economy trade-off of the persistent lane is deliberate and stated.
10. **Pause/restart recovery incl. mid-Sprint spec edits** — partially satisfied; consequence of the resume-time spec-eligibility reconciliation is undefined — see S2QA-REV1-02 (Medium).
11. **Cross-harness Planner fallback without native-session portability** — satisfied: linked replacement conversation + generated context packet, no pretend resume.
12. **Five skills tight enough to implement, loose enough for judgment** — satisfied; severity rubric correctly deferred to `sprint_rev`.
13. **Conceptual data model avoids duplicated truth** — satisfied, with explicit prohibitions.
14. **Every core system has an independently executable gate** — satisfied (Stages 1–10, incl. live vertical proof).
15. **Hard guards only on true invariants** — satisfied; the one-active-unit-per-shell lane rule protects a real invariant.

## Findings

### Medium (gate-blocking)

- **S2QA-REV1-01 (flag #67) — Merge grant bar omits a checks-green precondition.** Terms defines the grant as merge "once review passes with all Medium-and-above findings addressed"; loop step 7 and the Stage 6 gate repeat review-pass as the sole condition. Loop ordering (green wake → idle → review → merge) narrows but does not close the window: GitHub check re-runs, base-branch movement, or flaky re-triggered checks can regress CI between the last observed green and the merge, and nothing in the stated bar stops a merge on a stale green. The v1 bar was green+clean; v2.0's is review-only — a silent relaxation of delivery integrity, which the spec elsewhere says hard guards exist to protect. Fix is one line: grant condition = review passed **and** checks green at merge time.
- **S2QA-REV1-02 (flag #68) — Resume-time spec-eligibility reconciliation has no defined consequence; non-owner spec edits unaddressed.** Pause and Recovery lists "current spec eligibility" among resume reconciliations yet states unconditionally that "a spec edited mid-Sprint does not block resume" — the check's failure action is undefined, making it vacuous. And only edits "by the owning Planner" are sanctioned; a non-owner editing a governing spec mid-Sprint (shared spec, different work) leaves resume behavior, drift handling, and the status of the bound revision's QAQC approval unspecified. Fix: define the consequence of failed eligibility reconciliation, and either forbid or explicitly disposition non-owner edits to a Sprint-bound revision.

### Low (recorded, not blocking)

- **L1 — Closed-unmerged registered PR has no active notification path.** `closed` is normalized and recorded, but Developer and Planner are passive for it; discovery relies on liveness nudge + escalation latency (~20 min). A `closed`-without-merge active Planner notification (or explicit acceptance of the latency) would close it.
- **L2 — Abort authority is stated less crisply than resume.** Resume says "Only the Planner or FnB"; abort authority is only inferable (FnB in the roles table, Planner via `sprint_close`). Whether a participant shell may abort is unaddressed.
- **L3 — `prepared` has no terminal path.** The lifecycle admits no transition out of `prepared` except `armed`; a prepared Sprint that will never arm has no discard/abort route.
- **L4 — "Assigned Reviewer" binding is never defined.** The routing table and step 4 reference the assigned Reviewer of a PR, but neither Registered PR nor work unit records a reviewer assignment; "routes" at preparation implies it without naming the binding point.
- **L5 — Single-armed invariant should live in the arming transaction.** "No other Sprint is armed" is a preparation-time check (TOCTOU between two Planners); "Arming is one authoritative transaction" implies the constraint but the data model section never names the uniqueness invariant.

## Recommendation

Resolve S2QA-REV1-01 and S2QA-REV1-02 (both are one-paragraph spec edits, no design churn), cut REV4, and rerun a QAQC round against it. The Lows can ride along or be dispositioned by the FnB. Everything else about REV3 — including the SC-025 fix — verified clean under adversarial read.
