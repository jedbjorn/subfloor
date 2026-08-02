---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Sprints v2.0 — collaborative orchestration
roadmap_status: in_progress
frozen: false
---

# CONFORMANCE: Sprint 86 — Feature 31 defect pair

sprint doc: #86 · specs: #84 (unit 1), #85 as amended (unit 2) · judged against subfloor main @ 3a89a5d (CI green: tests, render-check, CodeQL) · conformance shell: REV2 · 2026-08-02

Method: spec bodies read from the DB; code read at the merge SHA only (never the diffs, never the trail). Ratified judgement calls per PLN1 kickoff #1263: (1) attempt ordinal — original failed launch = attempt 1, recovery wakes = attempts 2–5 with backoffs 15/60/180/300s (ruling #1233); (2) SC-051 resume-after-exhaustion = human episode reset, folded into spec #85 Edge cases (ruling #1251) — judged against the amended body.

## Unit 1 — Spec #84: CI-truthful check rollup normalization (PR #957, merged 6414909)

| Requirement | Verdict |
|---|---|
| Per-item verdict chain `conclusion → state → status`, first non-empty, uppercased | as-specced |
| Classification sets: failed unchanged; pending +WAITING/+REQUESTED; success unchanged | as-specced |
| Precedence rules 1–6 (failure first; pending; COMPLETED-null → pending; unrecognized → pending; all-success → SUCCESS; empty → created) | as-specced |
| Constants extracted at module level beside `_FAILED_CHECKS`; `_check_state()` only; no schema/watcher/API change | as-specced |
| PR description notes the expected `green → pending → green` transition correction on deploy | as-specced |
| Unit tests: queued+SUCCESS → PENDING; all-queued → PENDING; COMPLETED-null → PENDING; unknown state → PENDING; failed+queued → FAILURE; all-success → SUCCESS | as-specced |
| New StatusContext-shaped fixtures (state-only PENDING/SUCCESS/FAILURE) + mixed StatusContext+CheckRun rollup (PRs 821/822/827/828 + contract fixtures) | as-specced |
| Watcher-level test: queued CheckRun holds `pending`, no green owner wake; green wake only after final SUCCESS | as-specced |
| Out of scope respected: no required-checks/branch-protection work; no poll-backoff persistence (flag #136 not ridden in) | as-specced |

Diff surface confirms scope: `github_pull_requests.py` + fixtures + 3 test files only.

## Unit 2 — Spec #85 (amended): SHELL_BUSY wake contention recovery (PR #958, merged 3a89a5d)

| Requirement | Verdict |
|---|---|
| Classification: reconciler reads failed pickup turn's run row, `error_code='SHELL_BUSY'` selects contention path; everything else keeps once-only semantics | as-specced |
| Chained recovery keys `sprint-recovery:{sprint}:busy:{orig}:{n}` with escalating `available_at` backoffs 15/60/180/300s; no delivery-side change | as-specced |
| Attempt ordinal: original failed launch = attempt 1, recovery wakes = attempts 2–5 | deviated-intentionally — spec body left the base ambiguous; ratified by PLN1 ruling #1233, matches spec's "attempt 5 exhaustion" test contract and ~9.5-min worst case |
| Budget `WAKE_CONTENTION_ATTEMPTS = 5` + schedule as module constants; chain length derived by counting durable outbox rows on key prefix; restart-safe, no schema migration (spec permitted both) | as-specced |
| Event trail: exactly one `wake.requeued` per chain link carrying `{classification: shell_busy, attempt, backoff_seconds}` | as-specced |
| Event/relink dedupe keyed on (original wake, recovery wake) pair, applied to the non-contention path too | as-specced |
| Escalation: exhaustion pauses via `_pause_in_transaction` with reason `wake_contention_exhausted`; Planner notice names shell, attempt count, slot state (`busy`/`orphan` from `error_detail`) | as-specced |
| Decision #53 fit: chain = one bounded episode terminating in exactly one human escalation; no chain restarts after exhaustion | as-specced |
| Edge — orphan-held slot: exhaustion in ~9.5 min worst case, notice carries `orphan` pid remedy | as-specced |
| Edge — human Retry / message read mid-chain stops the chain naturally | as-specced |
| Edge — recovery wake's own SHELL_BUSY re-enters classification; `:949`-style exclusion narrowed to non-contention keys | as-specced |
| Edge — engine restart mid-chain resumes count from durable rows | as-specced |
| Edge — pause/resume interplay: distinct key namespaces; reconciler armed-only (no chain growth while paused) | as-specced |
| Edge — concurrent wakes collapse onto one deliverable | as-specced |
| Edge (SC-051 amendment) — resume after exhaustion = episode reset: stranded wake re-delivered pending with `available_at=now`, fresh budget, prior-episode rows excluded from chain-length derivation (new key root at reset wake id), stale exhausted evidence cannot re-pause resume | as-specced — against the amended spec body per ruling #1251 |
| `resume()` return value truthful: `ResumeReceipt.changed=False` when reconciliation ends paused | as-specced |
| Broker untouched (fail-fast SHELL_BUSY for browser turns unchanged); out-of-scope items respected (drain window, liveness interplay, sibling rollup spec) | as-specced |
| Verification suite: chain ordinals/backoffs, exhaustion notice, one-shot non-busy, restart mid-chain, `claim_next` backoff boundary, dedupe one-event-per-link | as-specced |

Diff surface confirms scope: `sprint_domain.py` + 2 test files only; no broker, watcher, or migration files.

## Findings

None. No `deviated-silently`, no `unimplemented`. 0 Major, 0 Medium, 0 Low.

### Carried review Lows (previously declared, for the sprint report — not conformance findings)

Unit 1: (1) explicit COMPLETED-in-states guard logically redundant with the unrecognized-verdict rule — spec-mandated explicitness, no action; (2) no fixture exercises a failing CheckRun via the conclusion field (fixture 822 converted to StatusContext-FAILURE) — same classification path covered via state-FAILURE, risk negligible.

Unit 2: (1) dedupe verification in `test_shell_busy_retries_durably_then_pauses_with_one_notice` is formally vacuous on the adoption path — the aggregate exactly-4-events assertion is the real one-event-per-link guard; (2) nits — `_wake_requeue_recorded` full event rescan per pulse, `_busy_recovery_origin` raise on handcrafted malformed keys, busy-chain non-SHELL_BUSY mid-chain failure unpinned by test.

## Cross-unit seams

Both units touch disjoint files (confirmed by per-merge diff surfaces) and their interaction point is nominal: the watcher (spec #84's consumer) projects `pending` where the wake machinery (spec #85) might now see more, not fewer, wake transitions — the append-only transition model absorbs this; spec #84's PR note declares it. No seam drift found.
