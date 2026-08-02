---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: 
roadmap_status: 
frozen: false
---

# SPRINT REPORT: Feature 31 defect pair — CI rollup truth + wake contention recovery

sprint doc: #86 (frozen) · conformance: doc #87 · planner: PLN1 · closed 2026-08-02

## Verdict

2 units / 2 PRs (#957, #958), both merged; **conformance: conforms** (0 findings; one deviated-intentionally = the ratified attempt-ordinal call); main green @ 3a89a5d (tests, render-check, CodeQL). Nothing deferred beyond the pre-declared out-of-scope flag #136 and the reviewers' Lows below. Downstream: the dos-arch Sprint 3 resume gate (flag #135) has its first condition met — both PRs merged — and now waits on the dos-arch fork engine update.

## Units Shipped

| seq | unit | shell | reviewer | branch | pr | outcome |
|---|---|---|---|---|---|---|
| 1 | Spec #84 — CI-truthful check rollup normalization | DEV5 | REV2 | fix/ci-truthful-check-rollup | #957 | merged 14:52, review-clean first pass |
| 2 | Spec #85 — SHELL_BUSY wake contention recovery | DEV3 (reassigned from DEV6 at kickoff) | REV2 | fix/shell-busy-wake-recovery | #958 | merged 15:42, clean on re-review after one Medium fix loop |

Planned vs actual order: fully parallel as planned; unit 1 merged first. One planned-vs-actual change: unit 2's dev was DEV6 on the declared board, reassigned to DEV3 pre-start because DEV6's session slot was held by a live interactive session (pid 2216193) — no work lost.

## Judgements Made

1. **Unit 2 attempt ordinal** (DEV3 ambiguity #1232 → ruling #1233, RATIFIED): original failed launch = attempt 1; recovery wakes = attempts 2–5 carrying backoffs 15/60/180/300s. Confirmed by the spec's own ~9.5-min worst case and "attempt 5 exhaustion" test contract. Final state: shipped as ruled; conformance filed it as deviated-intentionally (ratified).
2. **SC-051 resume-after-exhaustion re-pause trap** (REV2 Medium, flag #139 → ruling #1251): PLN1 ruled direction (b) — a human resume() after a `wake_contention_exhausted` pause is an episode reset: the stranded wake re-delivers (pending, available_at=now) with a fresh budget; prior-episode rows excluded from the new budget's count; resume()'s changed-value made truthful. Grounded in decision #53 (chain = one episode ending in one human escalation). Spec #85 amended before the fix was built. Final state: fixed in #958, re-review clean, flag #139 closed.
3. **Kickoff reassignment** (PLN1): unit 2 DEV6 → DEV3 rather than killing DEV6's live session or letting the unit stall on the liveness lock. Superseding stand-down sent to DEV6 (#1227) to prevent duplicate work off the stale task row.

No severity disputes.

## Spec Accuracy

Conformance doc #87 (REV2, kimi/k3, judged specs-vs-main @ 3a89a5d, never the diffs): unit 1 **as-specced**; unit 2 **as-specced** against the amended #85 body, with the ordinal call as the single **deviated-intentionally** (ratified). No deviated-silently, no unimplemented. Cross-check against unit reports: both declared `deviations: none`, consistent with the conformance verdicts — the unit-2 "deviation" was a planner-ratified reading, not a dev deviation.

## Issues Encountered

- **One real CI red** (unit 2 @ 155c612): the board projection rejected the SC-051 fix's new event name; DEV3 self-corrected in the same loop by reusing `wake.requeued` with `classification=contention_episode_reset` (the cleaner design). No anomalous reds; no phantom-red rulings needed.
- **One review fix loop** (unit 2): the SC-051 Medium — see Judgements. First-pass review caught it precisely because spec #85 was silent on the edge; the spec now carries it.
- **Environment: two rounds of external kills** of the planner's background tasks (a DEV3 merge boot, a REV2 conformance boot, inbox watchers). Both recovered losslessly from durable rows — unread task rows meant clean re-boots. Worth FnB attention if it recurs; cause unknown, outside the sprint machinery.
- **Kickoff liveness collision** (DEV6) — see Judgements 3.

## Deferred & Follow-ups

- **Flag #136** (Low, poll-backoff persistence): pre-declared out of scope; verified it rode into neither PR. Still open.
- **REV2 Low, unit 1**: explicit COMPLETED-in-states guard is logically redundant with the unrecognized-verdict rule — spec-mandated explicitness, no action.
- **REV2 Low, unit 1**: fixture PR 822's rollup was converted CheckRun-FAILURE → StatusContext-FAILURE, so no fixture exercises a failing CheckRun via `conclusion`; classification path covered via state-FAILURE. Candidate: add a CheckRun-conclusion-FAILURE fixture.
- **REV2 Low, unit 2**: the duplicate-pulse dedupe assertion scans zero rows — only the aggregate event-count assertion substantively gates one-event-per-link; candidate: adoption-path double-pulse test.
- **REV2 Low, unit 2**: wake.requeued rescan cost may grow; malformed busy keys could wedge reconciliation; non-SHELL_BUSY failure mid-chain is conformant but unpinned by a test.
- **dos-arch gate**: update the dos-arch fork engine to a subfloor ref ≥ 3a89a5d, then close flag #135 and resume dos-arch Sprint 3.

## Spec Debt

- Spec #85 was **amended mid-sprint** with the resume-after-exhaustion episode-reset edge (SC-051) — already written back; no residual debt from the ruling itself.
- Spec #85's event contract was silent on the board projection's event-name allowlist — the CI red came from exactly that. Future specs introducing events should name the projection contract they must satisfy.
- Spec #84's fixture inventory described StatusContext items as needing to be ADDED (REV1's pre-sprint correction held up); the CheckRun-conclusion fixture gap (Low above) is the remaining fixture debt.

## Metrics

- Review cycles: unit 1 — 1; unit 2 — 2 (one Medium fix loop).
- CI reds: 1 (unit 2, self-corrected same cycle).
- Boots: DEV5 ×2, DEV3 ×3 (+1 killed externally, re-booted), REV2 ×3 (+1 killed externally, re-booted), DEV6 ×0 (refused by liveness guard, reassigned).
- Wall clock: kickoff 14:30 → frozen ~16:00 (≈1h30 for two defect fixes incl. one review loop and a conformance pass).
- Message trail: rows #1222–#1267, kind != 'shell'.
