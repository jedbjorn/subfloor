# Re-review — sprint 25 seq 4 · PR #500 (feat/interface-session-schema @9ef1726)

Reviewer: REV1 (Kimi) · spec #20 task #80 · author: DEV4 · date: 2026-07-22
Verdict: **REVIEW-CLEAN.** All 5 gate findings (flags #33-37) verified fixed —
traced in code, proven by new tests, and re-run locally (48 targeted interface
tests green at 9ef1726, incl. the gate-critical decision-#22 re-proof).
CI 6/6 green. Prior Lows L1-L4 untouched as instructed; one new Low (L5).

Fix diff reviewed: b393e3c..9ef1726 — interface_broker.py (+real fence, input
lock, live write-failure park, generation end, submit revalidations),
interface_reconcile.py (shared park helper, generations refusal), snapshot.py
(generations row filter), 4 test files (+458 lines of new proofs).

## Flag-by-flag verification

### #33 (Major) — fenced prompt_submit + input lock — FIXED
- `record_hook prompt_submit` now promotes a submitting batch ONLY when
  `forwarded_seq < input_seq_fence` (no human seq accepted after the batch's
  fence). A violation parks the batch `delivery_unknown` + critical alert and
  stamps NO `submit_hook_seq` — manufactured evidence is impossible.
  `test_submit_hook_fenced_against_later_human_input` simulates the slipped-in
  human frame (forwarded_seq=1 vs fence=1) → park, composer stays dirty.
- Gate-critical decision-#22 re-proof: that same test then runs
  `startup_reconcile` — recovery finds no manufactured evidence and KEEPS the
  park (never a blind resubmit). The parking guarantee now rests on
  non-forgeable evidence; the ratified hard condition holds under the fence.
- Input lock is real: `accept_human_input` refuses frames while any batch of
  the shell/generation is `submitting` (BrokerError, zero bytes written —
  `test_input_lock_rejects_human_frame_during_submit`). The lock releases on
  every park path (hook park, writer-failure park, restart park).
- Composer `unknown` is never cleared by a hook (guard `composer in
  ("clean","dirty")`) — `test_unfenced_hook_cannot_clear_unknown_composer`.
- NULL fence treated as violation (safe direction).

### #34 (Major) — fresh-lease sequence continuity — FIXED
- `acquire_writer` reseeds `next_input_seq` from the SESSION's
  `forwarded_seq + 1` (missing input-state row → explicit BrokerError).
- `test_fresh_lease_continues_session_sequence`: forwarded 2 frames, restart,
  fresh lease expects 3 — no gap-wedge; replayed seq 2 is a duplicate ack,
  never a false acceptance of new bytes (both arms of the original repro).
- `test_not_delivered_resend_with_forwarded_seq_nonzero`: the crash-window
  not_delivered resend now proven for forwarded_seq=1 (wedged seq 2 resends,
  forwards exactly once) — no longer just the degenerate =0 case.

### #35 (Medium) — live write-failure parks like the crash window — FIXED
- `accept_human_input` wraps `writer()`: on exception WITHOUT process death →
  `park_delivery_unknown` (composer unknown / delivery_unknown / alert),
  writer lease revoked (`write_failure`), pending_seq kept as evidence,
  exception re-raised. `reconcile_input` remains the only exit.
- `test_write_failure_without_crash_parks_live` proves the park is immediate
  (no restart) and that the reconcile → certify → re-acquire → resend path
  recovers. The wedged-`pending_seq` repro from the first review is closed.

### #36 (Medium) — interface_generations guards — FIXED (all three arms)
- `session_end` hook sets `interface_generations.ended_at` (chat provably
  over); hooks for the ended generation are rejected thereafter
  (`test_session_end_hook_ends_generation`).
- `SNAPSHOT_ROW_FILTERS["interface_generations"] = "WHERE ended_at IS NOT
  NULL"` — live generations never serialize (`test_generations_keep_ended_only`:
  live row out, ended audit row kept).
- `live_refusal_reasons` refuses a rebuild on ANY live generation — including
  with all sessions ended, the exact post-rebuild New-chat brick scenario
  (`test_live_generation_blocks_with_all_sessions_ended`).

### #37 (Medium) — submit-gate revalidations — FIXED
- Binding released or sprint doc not ACTIVE at submit → batch CANCELLED
  (complete, items cancelled, zero bytes) — `test_binding_released_after_form_
  cancels_batch`, `test_closed_sprint_cancels_batch`. `_sprint_active` parses
  the doc body contract and fails safe (missing doc → cancel).
- Post-restart debounce: quiet baseline = last_human_input_at, falling back to
  session occupied_at/created_at (NULL never skips), floored at the
  `service_restart` lease-revocation stamp — `test_null_last_human_input_
  still_owes_full_debounce`, `test_restart_floors_the_quiet_baseline`.
- `quiet_s <= 0` → BrokerError, batch stays queued
  (`test_zero_quiet_s_rejected`, incl. negative).

## New-defect sweep of the fix itself

- `resolve_batch` requeues items in ('batched','submitting','running') — items
  of a hook-parked or write-fail-parked batch (left `submitting`) are recovered,
  not orphaned. `_cancel_batch` uses valid edges (batched→cancelled).
- `park_delivery_unknown` is now the single shared park helper (broker live
  path + reconcile startup path) — no divergent park semantics.
- Alert dedupe keys unchanged in shape; INSERT OR IGNORE against the partial
  unique index.
- Timestamp comparisons (`restart_at > baseline`) are lexicographic over
  datetime('now')-formatted values — consistent format, valid ordering.

## Lows (notes for the sprint report — not gates)

- L5 (new): the submit gate reads (clean/no-pending/quiet) and the
  `submitting` commit are not one transaction — a TOCTOU window exists for a
  concurrent `accept_human_input` on a SECOND connection. Not exploitable in
  this unit: nothing ships a concurrent caller yet (broker is invoked
  in-process only; the HTTP adapters are task #83). When #83 wires endpoints
  with per-request connections, gate + submitting-commit need serialization
  (single-writer discipline or BEGIN IMMEDIATE) — flag for that unit's spec.
- L1-L4 from the first review stand untouched (writer-lease check on
  certify_clean; unreachable stop-hook recovery branch; denormalized
  bindings.shell_id; quarantine/reconcile wiring believed #84 scope).

## Process notes

- Targeted re-run (gate-critical proof): 48/48 passed in dev4's worktree at
  9ef1726 — the four interface test files, including the decision-#22
  re-proof under the corrected fence. Full suite not re-run (CI green 6/6).
- Recommendation: DEV4 merges (CI green + review-clean, sprint-scoped
  authority). Seq 4 opens W1's parallel windows (seq 5 ∥ seq 9).
