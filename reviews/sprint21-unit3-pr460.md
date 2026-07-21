# Review — Sprint 21, Unit 3: Wake dispatcher + control API (PR #460)

- **Reviewer:** REV1 · **Author:** DEV3 · **Spec:** doc #20 (feature 14) · **Sprint:** doc #21
- **Head reviewed:** `3987995` (`feat/session-wake-dispatcher-api`) — all 6 checks green
- **Verdict (initial):** **2 blocking findings** — 1 Major (flag #17 / SC-460), 1 Medium (flag #18 / SC-461); 4 Lows for the report. Merge blocked until Major + Medium are fixed.
- **Re-review head:** `6883ca2` (fix commit `fix(session): restore retry and dispatch fences`) — all 6 checks green
- **Final verdict:** **review-clean.** Both blockers fixed and verified (addendum below); flags #17/#18 closed. Lows L1/L2/L4 stand as report notes; L3 addressed for the two blocking paths.

## Scope reviewed

New: `session_dispatcher.py` (poll/claim/finish/recover loop), token-scoped
`/_sc/session-control` API (status/manage/release/retry/bind/channel),
`session_cli.py` internal client, `service_supervisor.py` (`sc serve` now
supervises API + dispatcher), plus tests. Ratified judgements gated against:
retry schedule = initial attempt + 15s/60s/5m retries, terminal on 4th
unacknowledged completion (confirmed: `RETRY_DELAYS`/`MAX_ATTEMPTS` and the
claim-increments-attempt flow implement exactly this); internal
`sc session-control` in unit 3, public `sc session` deferred to unit 7
(confirmed: CLI is a thin token-scoped client, docstring states the boundary).

## Findings

### Major — retry/manage never restores autonomous wake (flag #17, SC-460)

`retry_session_control` (and `manage` on a released/error binding) transitions
the binding `error → starting` and requeues failed unread jobs — but nothing
ever advances `starting`:

- `claim_batch` only claims bindings in `foreground/idle/dormant` (`starting`
  is queue-only per the spec's state table);
- `reconcile_binding` (unit 2) returns `"vacant"` for a NULL lease without any
  state transition — the stale-cleared → `dormant` path needs a recorded owner;
- the dispatcher never transitions state based on the adapter's `dormant`
  probe.

**Verified by repro** against the PR head: binding in `error` with a failed
job → `retry` → state `starting`, job `queued` — then 5 `poll_once` cycles
with a `dormant` adapter, vacant owner, API up: **0 resumes, job queued
forever, no error surfaced**. The spec's retry contract ("requeue failed
unread work after the operator fixes auth, limits, or provider state") is
defeated end-to-end; the failure is silent (status shows `starting`,
queued=N, no last_error). The unit-1 design comment says re-manage/retry
routes through `starting` *so owner reconciliation happens* — the
follow-through (advance a vacant, native-ID-bearing `starting` binding to
`dormant`) exists in neither reconcile nor dispatcher.

**Proposed fix (dev's choice of seam):** in `poll_once`, when DB state is
`starting`, owner is `vacant`/`stale-cleared`, and `native_session_id` is
set, transition `starting → dormant` (edge already legal in the unit-1 state
machine) before the claim; or do the same inside `reconcile_binding`. Add the
missing test: retried binding dispatches again on the next cycle.

### Medium — PATCH can unfence a mid-turn binding (flag #18, SC-461)

`patch_session_binding` refuses only the *target* states `dispatching` and
`released`. A transition **out of** `dispatching` is legal in the state
machine (it is the dispatcher's own return path), so a token-scoped
`session-control bind --state idle` during a live turn succeeds — **verified
by repro** (`dispatching → idle` via PATCH). That unfences a binding the
dispatcher owns: with new queued jobs, the next cycle can claim and deliver a
second concurrent turn while the first is in flight — exactly the
concurrent-writer class the spec forbids ("no adapter may resume while
another validated owner or active provider turn exists") and that units 1–2
fence everywhere else. Realistic trigger: an adapter's status-report PATCH
racing the dispatcher (units 4–6 will do these), not a hostile caller.

**Proposed fix:** refuse `state` PATCHes while the current state is
`dispatching` (mirror the release guard: "dispatching is owned by the
dispatcher"); extend `test_binding_patch_cannot_bypass_dispatch_or_release_operations`
to cover the source-state case.

## Lows (report notes — not gates)

- **L1:** a channel-register fencing refusal (`ValueError("active channel PID
  is outside the binding worktree")`) surfaces through `_session_post`'s
  generic `except (TypeError, ValueError)` as HTTP 400 "invalid binding id" —
  misleading remediation for a real ownership refusal.
- **L2:** after a live delivery, `poll_once` hardcodes `return_state="idle"`
  even when the binding was claimed from `foreground` — the
  interactive-owner distinction is silently dropped; `finish_batch` already
  accepts `foreground`, the caller just never passes it.
- **L3:** coverage — no test drives a binding from `error` through
  `retry`/`manage` back to a successful dispatch (would have caught the
  Major); the PATCH-bypass test only asserts forbidden *targets* (would have
  caught the Medium); `test_retry_resets_only_failed_unread_jobs` encodes the
  stalled `starting` state as expected.
- **L4:** `service_supervisor` restarts a crash-looping dispatcher every 1s
  with no backoff — bounded but noisy in `sc logs` if the dispatcher dies
  deterministically (e.g. missing DB).

## What holds up (verified, not just read)

- Retry cadence, secret redaction (job rows, binding rows, attempt logs), and
  `read_at`-only acknowledgement are all directly tested; the dispatcher never
  touches `shell_messages` beyond SELECTs.
- `BEGIN IMMEDIATE` discipline is sound end-to-end: unit-2's
  `reconcile_binding`/`preflight_lease`/channel ops all commit-or-rollback
  internally, so the dispatcher's claim/finish transactions never nest.
- Concurrent claim (two dispatchers, one batch) proven under threads; the
  binding-state CAS is the lock.
- Crash recovery: running jobs with a vacant owner requeue with an audit
  reason and burn an attempt; a live/cleanup owner or active provider turn
  blocks recovery (no second writer). `_return_binding_state` refuses to
  stomp a release/error requested mid-turn.
- Turn-arrival handling matches spec: messages arriving during the turn get
  audit rows via the watermark, and are completed if acknowledged.
- API-down is a readiness gate, not an attempt: queue preserved, no adapter
  call, binding carries a clear last_error.
- Coalescing embeds no message bodies; the wake prompt is the spec's fixed
  string verbatim.
- CodeQL dynamic-SQL fix is real: per-field literal UPDATEs; remaining
  f-string SQL is placeholder-lists only.
- Release cancels queued+failed with audit reason and leaves messages unread;
  re-manage reactivates only still-unread cancelled rows; manage validates
  deliver/resume capability and single-managed-binding per shell.
- Endpoint validation: loopback/unix-socket only, no credentials in URL;
  binding rows never expose `api_key`; cross-shell access denied at the query.
- `sc serve` arg compatibility preserved (old server only ever parsed
  `--port`); container launch path (`./sc serve --port`) unchanged; dispatcher
  gets its own heartbeat row and status surface via GET.

## Re-review addendum — fix commit `6883ca2` (verified 2026-07-21)

One commit, exactly the two flagged seams plus tests; no unrelated scope.

**SC-460 (Major) — fixed.** `poll_once` now advances a stalled `starting`
binding before the claim: when DB state is `starting`, the adapter probe is
`dormant`, the reconciled owner is `vacant`/`stale-cleared`, and
`native_session_id` is set, it runs `transition_binding(expected="starting",
target="dormant")` and commits. Verified:

- `starting → dormant` is a legal edge in the unit-1 state machine.
- The transition is a CAS; concurrent interference raises `StaleBindingState`,
  which the loop's error handler surfaces as `last_error` — not silent.
- Placement is pre-claim inside the per-binding loop, after reconcile and the
  probe, with its own commit — no nesting with `claim_batch`'s
  `BEGIN IMMEDIATE`; the retried binding dispatches in the *same* cycle.
- The guard is exactly as narrow as proposed: a fresh `starting` binding with
  no native ID is untouched (launch is units 4–6 scope), and a non-dormant
  probe or non-vacant owner leaves the conservative stall in place.
- Test: `test_retry_resets_only_failed_unread_jobs` grew into
  `test_retry_resets_failed_unread_jobs_and_dispatches_again` — drives
  error → retry → poll_once → resume delivered, jobs done, binding `dormant`.
  This is the missing test named in L3; it would have caught the original bug.

**SC-461 (Medium) — fixed.** `patch_session_binding` now refuses any PATCH
carrying `state` when the *current* state is `dispatching`
("dispatching is owned by the dispatcher"). Verified:

- The guard reads the row inside the handler's `BEGIN IMMEDIATE`, and the
  dispatcher's claim/finish also run under `BEGIN IMMEDIATE`, so the check
  serializes against the dispatcher — no TOCTOU window.
- The dispatcher's own return path writes state directly
  (`_return_binding_state`), not via PATCH, so its exit from `dispatching` is
  unaffected; release/retry/manage are separate endpoints with their own
  guards, also unaffected. An adapter status-PATCH racing a live turn now gets
  a truthful refusal instead of unfencing the binding.
- Test: `test_binding_patch_cannot_bypass_dispatch_or_release_operations`
  extended with the source-state case — PATCH `dispatching → idle` refused,
  state unchanged. (Minor non-defect: a PATCH with `state: null` during
  `dispatching` is refused where it would have been a no-op — harmless
  over-refusal.)

**Lows:** L1 (misleading 400 on channel fencing refusals), L2 (foreground
return-state dropped to idle), L4 (1s crash-loop restart without backoff)
stand as report notes. L3's two blocking-path gaps are now covered; the
one-shell coverage nits within it remain notes.

## Batch-terminal note (not a defect)

When one coalesced job exhausts its budget, `finish_batch` fails every unread
sibling in the batch regardless of their attempt count. Defensible: the
binding enters `error` anyway, and `retry` requeues exactly the `failed` set —
consistent recovery. Flagging only so the behavior is on record as chosen.
