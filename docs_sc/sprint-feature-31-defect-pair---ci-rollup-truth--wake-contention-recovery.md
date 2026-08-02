---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: 
roadmap_status: 
frozen: true
---

# SPRINT: Feature 31 defect pair — CI rollup truth + wake contention recovery
status: CLOSED
closed: 2026-08-02 · conformance: doc #87, 0 findings · main @ 3a89a5d green (tests, render-check, CodeQL)
declared: 2026-08-02 · planner: PLN1
models: devs=codex/gpt-5.6-sol · reviewers=kimi/kimi-code/k3

| seq | unit | shell | reviewer | depends on | branch | pr | status |
|---|---|---|---|---|---|---|---|
| 1 | Spec #84 — CI-truthful check rollup normalization (`_check_state`, github_pull_requests.py) | DEV5 | REV2 | — | fix/ci-truthful-check-rollup | #957 | merged |
| 2 | Spec #85 — SHELL_BUSY wake contention recovery (reconciler classification + backoff) | DEV3 | REV2 | — | fix/shell-busy-wake-recovery | #958 | merged |

Notes:
- 2026-08-02 16:30: unit 2 reassigned DEV6 → DEV3 at kickoff — DEV6's slot held by a live interactive session (pid 2216193, since 13:25); DEV6 stood down via msg #1227, task moved via #1226. No work lost (pre-start).
- Units are independent (disjoint files) — full parallel, no dependency edges.
- Both build against subfloor main @ 3d1f9f3, separate branches + PRs per the git skill.
- Specs are the authority: doc #84 (decision #63), doc #85 (decision #64). REV1 QAQC passed both (result #1221); findings already folded into the spec bodies.
- Out of scope by directive: flag #136 (poll-backoff persistence) — must not ride into either PR.
Review Lows (for the report):
- Unit 1 / PR #957 (REV2, msg #1240): (1) explicit COMPLETED-in-states guard logically redundant with the unrecognized-verdict rule (spec rule 3 vs 4) — spec-mandated explicitness, no action; (2) fixture PR 822 rollup converted CheckRun-FAILURE → StatusContext-FAILURE, so no fixture exercises a failing CheckRun via conclusion — classification path covered via state-FAILURE, risk negligible.

Judgement calls:
- Unit 2 (msg #1232 → ruling #1233): attempt ordinal — original failed launch counts as attempt 1; recovery wakes are attempts 2–5 carrying backoffs 15/60/180/300s. DEV3's call, RATIFIED by PLN1 (matches spec's ~9.5-min worst case and "attempt 5 exhaustion" test contract).

- Unit 2 Medium (SC-051 / flag #139, REV2 msg #1249-1250 → ruling msg #1251): resume-after-exhaustion re-pause trap — spec #85 was silent. PLN1 ruled direction (b): human resume = episode reset, stranded wake re-delivered (pending, available_at=now) with fresh budget; prior-episode rows must not count against the new budget; resume() changed-value fixed. Spec #85 amended (Edge cases, last bullet). Unit 2 → fixing. REV2's 2 Lows to report.

Issues:
- Unit 2, one CI red @ 155c612 (15:28): board projection rejected the SC-051 fix's new event name; DEV3 self-corrected same cycle — episode reset reuses wake.requeued with classification contention_episode_reset; re-pushed, local suites green.

- Downstream gate (not this sprint's units): both PRs merged → dos-arch fork engine update → close flag #135 → dos-arch Sprint 3 resumes.
