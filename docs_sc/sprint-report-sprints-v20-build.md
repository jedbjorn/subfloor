---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: 
roadmap_status: 
frozen: false
---

# SPRINT REPORT: Sprints v2.0 build (doc #51, feature #31)

- **Planner:** PLN1 · **Devs:** DEV3, DEV4 (codex/gpt-5.6-sol) · **Reviewers:** REV1, REV2 (kimi/kimi-code/k3), all `--effort high`
- **Governance:** FnB-directed organic sprint (decision #38): PLN1-native wakes (harness background runs + own GitHub poller), no substrate watch daemon, all merges PLN1's, single-reviewer units with 2× close-out conformance.
- **Spec:** #46 "Sprints v2.0 — Collaboration Loop", REV4 (QAQC-passed baseline) → REV9 (final). **Duration:** 2026-07-31 → 2026-08-01.

## Verdict

**Shipped, conforms after remediation.** 13 units / 13 PRs merged to `main` (final: 012c40a), main green throughout — no sprint merge ever broke it. The initial 10-unit build passed every unit review but **failed close-out conformance on both shards** (6 Major / 16 Medium / 28 Low): the engine internals were verified solid; the Majors were missing production surfaces (store-deep capability reachable only from tests) plus one spec-internal contradiction on completion semantics. FnB dispositioned (decision #45): spec write-back first (REV9), two serial remediation units, scoped opposite-eyes re-verification — **dual PASS, all 13 remediated findings closed, zero regressions** (docs #60/#61). An FnB-directed follow-up (board UI, spec #59) shipped as unit 13 inside the same sprint. Deferred with eyes open: 11 conformance Mediums and ~40 Lows, catalogued below and as flags; close-out completeness is **advisory by explicit FnB stance** — the engine surfaces gaps, it does not gate.

## Units Shipped

| # | Unit | Dev | PR | Merged at | Review outcome |
|---|---|---|---|---|---|
| 1 | Domain schema + lifecycle + armed switch | DEV3 | #850 | 471ce2b | 1 Md (#69 armed-INSERT/grant bypass) fixed; clean |
| 2 | Participant conversations, pills, FnB entry | DEV4 | #849 | 6abc593 | 1 Md (#71 pill projection → R2) fixed; re-review clean |
| 3 | sprint_messages + wake outbox + 3-attempt auto-pause | DEV3 | #858 | 13c599c | clean 0M/0Md |
| 4 | PR registration + armed watcher | DEV4 | #861 | e6ac3d5 | clean 0M/0Md; mechanical u5-conflict rebase |
| 5 | Work units, dependencies, waves + dispatcher | DEV3 | #860 | eb9d8e2 | clean 0M/0Md |
| 6 | Dev/review loop wiring | DEV4 | #863 | 171ee5c | 1 Md (SC-028 → R3) fixed; re-review clean |
| 7 | Liveness monitor + grace/nudge/escalation | DEV3 | #862 | c1770fb | 1 Major (SC-029) + 2 Md (SC-030/031) fixed; re-review clean |
| 8 | Pause/resume/recovery + reconciliation | DEV4 | #867 | 0f60c3b | clean 0M/0Md |
| 9 | Close/report compiler + 5 skills + command surface | DEV3 | #868 | b7e77d7 | clean 0M/0Md |
| 10 | Live vertical proof + adversarial sweep (+R6 surface) | DEV4 | #869 | cc33349 | clean 0M/0Md; external claims independently verified |
| 11 | **Remediation A:** production surfaces (all 6 conformance Majors + replan) | DEV3 | #877 | 6973f49 | clean 0M/0Md |
| 12 | **Remediation B:** R7 completion, head-move recovery, wake resume, conversation close | DEV4 | #879 | b5f5c29 | 2 Md (SC-035/036) fixed; re-review clean |
| 13 | **FnB board UI** (spec #59, tasks #200–#204, one PR) | DEV3 | #881 | 012c40a | 3 Md (SC-037/038/039) fixed over 3 rounds; clean |

Planned order held (waves W0–W7 as declared); units 8/9 ran the parallel-build pattern (draft early, rebase+ready on dependency merge) and unit 13 was appended mid-sprint by FnB directive #625. Phase 0 (2× independent QAQC on spec rev3) preceded all build: dual FAIL (5 disjoint Mediums), fixed in REV4, dual re-round PASS.

## Judgements Made

**Rulings (all recorded as spec revisions or task rows at decision time):**
- **R1** — v1-removal guard tests flag all v2 modules: extend the allowlist deliberately, never weaken v1 assertions, never reuse removed names. Applied by every unit; zero violations.
- **R2 (REV6)** — pill precedence for multi-participation shells: armed > most-recently-paused.
- **R3 (REV7)** — a registered PR has exactly one owning work unit; fan-out registration rejected.
- **R4** — bare run-state `unknown` is ambiguous silence, not proven failure; co-occurring proven signals escalate, the label never does. Decision #42 ratified: strong evidence resets an episode, supporting evidence suppresses without resetting.
- **R5 (REV8)** — closed-without-merge observation resolves the owning unit's review-request liveness expectations.
- **R6** — unit 10's live proof found no authenticated lifecycle/registration surface; ruled build-it (option A). Also set the real-GitHub boundary: fixture PRs on a throwaway base only, never main, cleanup in-unit. Executed exactly (fixture PRs #870/#871; origin/main verified untouched).
- **R7 (decision #45, REV9)** — merged observation completes only `merge_ready` units (the judgment happened at authorize_merge; observation executes it); grant-bypassed merges notify the Planner and never auto-complete. Settled conformance M3's spec-internal contradiction.
- **FnB stances (decision #45):** close-out completeness is advisory — shells make judgment calls, reports and follow-up patches absorb deviations, enforcement is the exception; head-move recovery is automatic; QAQC signer verification required; follow-up disposition is FnB-only (`accepted`/`resolved`/`dismissed`).
- **Dev judgement calls of note:** decision #44 (no new migration for recovery — existing authoritative records compose durable state; conformance verified no schema gap); u13's `sprintFeedIdentity` refresh key and source-derived emitter sweep; pause-reason limit aligned 1000→2000 across shell+UI (declared, not silent).

**Severity disputes:** none — no dev contested a reviewer's rating all sprint.

## Spec Accuracy

Phase 0 QAQC (docs #52–#55) caught 5 disjoint Mediums pre-build. Close-out conformance (docs #57/#58) then found what unit-scoped reviews structurally cannot: **the spec's shell-reachable surface was unimplemented in six Major places** (QAQC approval writes, sprint inbox accept/decline, shell-judged + report-only completion, final-report write path, follow-up disposition, replan), because the unit decomposition was layer-first and no unit owned the cross-cutting surface; and **one spec-internal contradiction** (loop section vs. Work-and-Parallelism on merge completion) that both QAQC rounds missed and unit 6 implemented faithfully — the `merge.grant_bypassed` event shows the seam was seen but never escalated for a ruling. Cross-check: every dev unit report said `deviations: none` and was honest — the gaps were between units, not inside them.

Spec revisions REV5–REV9 recorded every ruling in-flight; REV9 additionally wrote back the FnB's advisory-close-out stance, disposition semantics, QAQC signer rule, replacement-Planner authority (originating Planner or FnB only), head-change transitions, and the merge-execution vocabulary ("the engine never merges; shells merge under the grant, the watcher observes"). C2 re-verification (docs #60/#61): all 13 remediated findings **closed**, judged against both main and REV9 text.

## Issues Encountered

- **I1 — host /tmp exhaustion:** 30,843 leaked engine-test temp dirs (6.2 GB) filled the tmpfs mid-wave-1; ENOSPC killed run logging and the poller. Purged, poller re-armed, no work lost; filed subfloor#853.
- **I2 — dropped review handoff:** DEV4 reported a review request that never landed as a message row; REV2 booted into an empty inbox. Instituted the **verified-delivery rule** — every handoff confirmed as a durable `shell_messages` row before booting the recipient. Zero recurrences across ~20 subsequent handoffs.
- **Review-loop catches post-build:** SC-035/036 (unit 12: terminal-observation head-move firing; bypassed merges leaving live review expectations) and SC-037/038/039 (unit 13: polling feed collapse, projection drift, missing visual/restart proof — three review rounds) — all red-proven, fixed, re-reviewed clean.
- **Environment/tooling side-findings filed upstream:** subfloor#853 (test temp leak), #859 (live-DB rollback isolation), #872 (linked-worktree seed-skills), #876/SC-034 (dogfood snapshot drift blocking local render-check only), #878 + #769 (tooling gaps). SC-033 (completed-job status lookup).
- **Process frictions:** one launcher-test kill of a reviewer boot (recovered — unread inbox row made the re-boot lossless); repo map went stale twice under the sprint's file churn (cartographer re-runs requested); concurrent FnB merges to main (#875, #880) absorbed by the sync-before-handoff rule with zero conflicts.

## Deferred & Follow-ups

Flags opened at close (all feature #31): the 11 deferred conformance Mediums — A-Md2 (QAQC finding-resolution gate unchecked), A-Md3 (preparation checks absent: GitHub probe, fallback capacity), A-Md6 (fallback context packet write-only), A-Md7 (FnB messages don't enter sprint_messages), B-C-m3 (liveness suppression lacks a ceiling: live-but-hung process defers escalation forever — needs decision #42 write-back or code ceiling), B-C-m4 (git commits not consumed as strong evidence), B-C-m6 (bound-spec edit guard is resume-granular; no write-time detection or actor), B-C-m7 (evidence packet misses reconciliation events), B-C-m8 (FnB-drives-close positive path untested) — plus the U12 audit gap (out-of-band merge at an **unapproved** head while `merge_ready` completes silently, no bypass notice).

Low-tier backlog (report-only, in the source rows/docs): ~40 items across unit reports (#494/493/495/521/568/566/569/697/700/701/702/703/704) and conformance docs #57/#58/#60/#61 — recovery test-depth, UI listener accumulation, dead enum values, capture sparseness, wake retry backoff, C-L2 (no general judgment-recording surface), and the u11 close-out transactional nits.

## Spec Debt

Written back already (REV9): R7, head-change rule, advisory close-out, disposition semantics, signer rule, replacement-Planner authority, merge vocabulary. **Still owed a spec pass** (from doc #58's candidate list + reviews): coalescing vocabulary vs. in-flight-turn detection; conformance-during-pause; close-out reviewer must be declared up front (or an add-participant path); completion with non-terminal units (now advisory — say so); merge-grant binding time (no-grant Sprints expressible?); FnB dispatch/monitor when the Planner is down; "real browser conversations" vs. DB-enqueued proof vocabulary (R6); `cancelled`-unit dependents requiring replan (u11-L1); explicit passivity recording. These are one focused spec-editing session, not build work.

## Metrics

- 13 units, 13 PRs, 13/13 first-merge green at merge time (checks-green-at-merge verified on every PR); main never red from a sprint merge.
- Review rounds: 8 units clean first pass; 5 needed exactly one fix round (u1, u2, u6, u7, u12) except u13 (two fix rounds). 1 Major + 12 Mediums caught across unit reviews, 100% red-proven and closed pre-merge.
- Conformance: 2 QAQC rounds (dual FAIL → PASS), 2 close-out shards (dual FAIL: 6M/16Md/28L), 2 scoped re-runs (dual PASS: 13/13 closed).
- Worker boots: ~35 (devs 16, reviewers 15, report-only 4). Wakes: harness task completions + ~25 poller cycles; zero scheduled polling by any worker shell.
- Suite growth: 1270 → 1430 tests (+774 subtests at close). Spec: REV4 → REV9. Rulings: 7. Incidents: 2, both with instituted countermeasures.
- Heterogeneous pairing (Sol devs × Kimi reviewers, FnB-confirmed): every Major/Medium the reviewers caught was in contract seams the builder treated as settled; two independent same-spec QAQC rounds produced fully disjoint finding sets.
