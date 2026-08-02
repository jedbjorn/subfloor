---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Sprints v2.0 — collaborative orchestration
roadmap_status: in_progress
frozen: false
---

# CONFORMANCE ADDENDUM: Sprints v2.0 build — shard B scoped re-run (REV1)

- **Sprint:** doc #51 (SPRINT: Sprints v2.0 build), unit C2 scoped conformance re-run, shard B (task msg #647).
- **Base doc:** #58 (CONFORMANCE: Sprints v2.0 build — shard B, REV2 @ cc33349). This addendum judges only whether doc #58's six remediated findings are now closed; it does not re-pass the shard.
- **Spec:** doc #46 "Sprints v2.0 — Collaboration Loop", REV9 (decision #45 disposition: advisory close-out, R7 completion semantics, watcher head-change transitions, FnB-only follow-up disposition).
- **Judged artifact:** `~/Repos/subfloor` main @ **b5f5c29** (U11 PR #877 @ 6973f49 + U12 PR #879 @ b5f5c29 merged). Judged against the code on main, never the diffs or the message trail.
- **Reviewer:** REV1 (shell #7), opposite eyes per the C2 ruling. Method: six parallel adversarial evidence passes (one per finding), every load-bearing claim then re-verified by REV1 directly against the code (spot-checks confirmed the cited lines verbatim); all remediated-area test files executed green.

## Verdict: PASS — all six findings closed (6 closed / 0 partial / 0 open)

The remediated findings from doc #58 are closed on main @ b5f5c29, conforming to the REV9 spec text. No immediately adjacent regressions found. Minor observations (Low-tier, for the sprint report, not re-run gates) are listed at the end.

## Per-finding verdicts

### C-M1 — final-report write path + ungated `completed`: **CLOSED**

- Production write path exists end-to-end: store `record_final_report` (`.super-coder/scripts/sprint_close.py:115`, INSERT `report_kind='final'` at `:148-154`, idempotent replay, conflicting replay rejected), API `/_sc/sprint/complete` (`.super-coder/api/server.py:2335-2350`, commits the final report *before* the transition), CLI `sc sprint complete --report-file --key` (`.super-coder/scripts/sprint_cli.py:461-467`, `:207-223`). Authority = originating Planner or FnB (`sprint_close.py:540-556`).
- Transition stays **ungated** per REV9 advisory: `sprint_domain.py:297-300` requires only `terminal_outcome`; `server.py:2342-2343` makes the report optional; absence is surfaced as `missing_final_report` in the evidence packet (`sprint_close.py:363`); the `sprint_close` skill states the advisory stance (`assets/skills/sprint_close/SKILL.md:19-21,108-118`).
- Tests: `test_sprint_close.py` 11/11 (final-report idempotency + replay-after-completion), `test_sprint_cli.py` 7/7 (authenticated end-to-end write; report-less complete succeeds — direct proof of ungated), `test_sprint_skills.py` 3/3 (skill command surface pinned to real CLI verbs).

### C-M2 — FnB follow-up disposition: **CLOSED**

- Full path: store `disposition_followup` (`sprint_close.py:166`), API `/_sc/sprint/followup-disposition` (`server.py:2465-2473`), CLI `sc sprint disposition-followup` (`sprint_cli.py:314-330,520-531`).
- FnB-only enforced store-side on token-derived identity: non-admin or deleted caller → `SprintAuthorityError("only FnB…")` → HTTP 403 (`sprint_close.py:188-194`; `server.py:2155,1855-1856`).
- Three outcomes validated and terminal (`sprint_close.py:176-184,204-213`): accepted takes no resolution; resolved/dismissed require one; identical replay is idempotent (`False`), a different re-disposition raises 409. Consistent with migration 0150's CHECK and the immutability trigger's disposition carve-out.
- Evidence packet now counts only `pending` as unresolved (`sprint_close.py:298-303,365-371`); all dispositions remain visible under `conformance.followups`.
- Tests: `test_sprint_close.py::test_only_fnb_dispositions_followup_and_only_pending_is_unresolved` (non-admin rejected, terminal replay, packet total==0), `test_sprint_cli.py:619-640` (planner 403, admin succeeds end-to-end).

### C-m1 — head-move un-wedge (merge_ready → in_review + reviewer delta wake): **CLOSED**

- Watcher detects head change on a `merge_ready` unit (`sprint_pr_watcher.py:535-544`) and, inside the `sprint.pr.observe` write transaction: voids the stale approval (`_invalidate_stale_approval` `:638-682` — approval message read, pending wakes cancelled, `review.approval_invalidated` event `:566-584`), flips the unit to `in_review` (`:560-565` with conditional guard), and sends the assigned Reviewer an **actionable, active** delta-review request (`:585-598`, `pr-head-change:` idempotency key → pending wake in the outbox).
- Double-gated staleness: `authorize_merge` rejects both non-`merge_ready` lanes and stale approval heads (`sprint_review_loop.py:245-251`); the re-review path (`record_review` from `in_review` against the watcher-stamped new head) completes the round-trip back to `merge_ready`.
- Test: `test_sprint_review_loop.py::test_same_state_head_move_invalidates_approval_and_requests_delta_review` — full un-wedge round-trip incl. re-approval and `authorize_merge` on the new head; merged/closed exclusions pinned by two adjacent tests. Suite 16/16.

### C-m2 — (state, head) transition dedupe: **CLOSED**

- Dedupe key now includes the head (`sprint_pr_watcher.py:405-411`): same state + new head appends a transition with the fresh `observed_head_sha` (`:416-429`); same state + same head still writes nothing (`:404-411` early return — the suppress-unchanged invariant holds). `transition_key` hashes the head (`:413-415`), keeping derived notification idempotency keys unique per head.
- Notifications fire exactly once per transition (`pr-transition:{transition_key}:participant:{id}` keys, `:624-635`) with wake coalescing; terminal-state head moves are excluded from the delta-review lane.
- Tests: same-state head move appends (`test_sprint_review_loop.py:486-495`); restart/resume unchanged-state emits no duplicates (`test_sprint_pr_watcher.py:319-365,385-388`); red→green→red wakes once each (`:253-289`). Watcher+loop suites 29/29.

### C-m5 — stranded-wake resume re-queue: **CLOSED**

- `resume()` re-queues terminally failed wakes inside the re-arm transaction (`sprint_domain.py:392,901-952`): failed wakes with unread messages get a fresh `pending` replacement (attempts reset via schema defaults, key `sprint-resume:{id}:failed-wake:{old}`) or reuse an existing pending wake; `sprint_wake_messages` re-points the undelivered message(s); a `wake.requeued` event carries both ids. Replay-safe (already-armed early-return; moved messages leave nothing to requeue).
- Surfaced: `requeued_wake_ids` in reconciliation evidence (`:1056-1058`) plus a Planner notice (`:415-425`); `claim_next` claims the replacement because the re-pointed unread message satisfies its EXISTS clause (`sprint_message_delivery.py:509-521`) — the assignment that caused the auto-pause is delivered after resume.
- Test: `test_sprint_recovery.py::test_terminal_wake_failure_uses_the_same_pause_machinery` drives the full `wake_delivery_exhausted` → pause → resume → re-queue → claimable arc. Recovery/delivery/domain suites 38/38.

### C-m9 — report-only/cancelled writability: **CLOSED**

- Production surfaces: API `/_sc/sprint/complete-unit` + `/_sc/sprint/cancel-unit` (`server.py:2244-2262`); CLI `sc sprint complete-unit --result-file` / `cancel-unit --reason` (`sprint_cli.py:143-166,426-440`); domain `complete`/`cancel` (`sprint_domain.py:1632,1699`).
- Authority: completion is the assigned Developer's only (`sprint_domain.py:1648-1649`), matching `sprint_dev`; cancellation is Planner/FnB (`server.py:1879-1893`, `sprint_domain.py:1714`), matching `sprint_pln`. Code-kind units are refused the manual path (`:1656-1659`); cancelled is terminal for close (`sprint_close.py:290`).
- Manual completion runs the identical `_dispatch_ready_locked` tail as merge-observation (`sprint_domain.py:1694-1697` vs `:1828-1831`), re-guarded on armed, so downstream dependencies unblock.
- Tests: report-only completion releases dependents (`test_sprint_work_dispatch.py:208`), exact-result idempotency + code-unit rejection (`:263`), planner-only cancel with reason + idempotent retry (`:305`), CLI end-to-end (`test_sprint_cli.py:579-600`). Dispatch/CLI/close suites 29/29.

## Minor observations (Low-tier notes for the sprint report — not re-run gates)

1. Final-report commit and the lifecycle transition are separate transactions; a transition failure between them leaves a replayable report on an armed Sprint. Harmless under idempotent retry.
2. `disposition_followup`'s validation branches (unknown disposition, accepted-with-resolution, missing resolution, conflicting re-disposition) have no dedicated tests — code correct on reading, but a regression there stays green.
3. No dedicated test for a same-state head move on a **non-**`merge_ready` unit (append mechanism proven via the merge_ready test; coverage hole, not behavior hole).
4. Green→green head moves send the Developer one active notification per occurrence — inherited occurrence semantics; defensible, but if REV9 intended head-move pushes to be passive for the Developer, that nuance is neither implemented nor tested.
5. A `cancelled` upstream does not satisfy the dependency predicate (`upstream.disposition<>'completed'`, `sprint_domain.py:1923`) — dependents stay blocked until replanned/cancelled. Reads as intentional (a cancelled lane's outputs never existed) but is unstated in spec/skills; DEV3 already raised the sibling clarification candidate (#627).
6. `complete-unit` has no FnB/admin override; a dead dev holding an active no-code lane escapes only via decline→ready→replan. Indirect but extant.

## Disposition

- Shard B scoped re-run verdict: **PASS**. All six remediated findings (2 Major, 4 Medium) closed against main @ b5f5c29 and spec #46 REV9; no adjacent regressions; no new findings opened.
- Doc #58's remaining Mediums (C-m3, C-m4, C-m6, C-m7, C-m8) and Lows were dispositioned to follow-up flags by the FnB (decision #45) and are out of C2 scope; they are not re-judged here.
- Evidence basis: six adversarial passes + REV1's direct re-verification of load-bearing citations; targeted suites green at b5f5c29 (close 11, CLI 7, skills 3, review-loop 16, watcher+loop 29, recovery/delivery/domain 38, dispatch 11).
