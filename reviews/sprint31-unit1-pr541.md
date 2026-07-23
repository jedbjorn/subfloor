# Review — Sprint 31 (doc #31) Unit 1 · PR #541

- **Unit:** Lifecycle convergence — one `close_session` helper, cancel start (#519), hook/stop race convergence (#532), API error mapping (#523), #526 low follow-ups (spec #30 reqs 1–4 + Lifecycle Contract)
- **Author:** DEV5 · **Reviewer:** REV2 · **Branch:** `fix/lifecycle-convergence` → `main`, 4 commits, +944/−104
- **CI:** green (tests / verify / render-check / CodeQL / Analyze ×2) per dev report; suite not re-run locally per review stance — code paths traced against `origin/fix/lifecycle-convergence` instead.

## Verdict: BLOCKED — 1 Major / 1 Medium / 2 Low

Fix SC-064 + SC-065 and re-push; re-review on the fix push. Everything else in the unit is verified clean.

## Axis 1 — Code quality

- `close_session` (interface_broker.py) is the real convergence point: occupancy → ended with `ended_at`/`end_reason`, lifecycle walked through `stopping` only from idle/busy/approval/user_input, generation ended, leases revoked, input parked, wake batches resolved/parked. **Edge legality verified against `LIFECYCLE_EDGES`/`OCCUPANCY_EDGES` in interface_state.py** — every walk the helper can take is a legal edge (starting/lost/error/stopping have direct →ended; the four live states all reach stopping; reserved/occupied/unreconciled all reach ended). The dev's "no edge-map migration needed" claim is TRUE.
- `record_hook` session_end now runs full closure instead of ending only lifecycle — the #532 stranding (occupied + lifecycle ended, no route could converge) is gone, and its test (`test_terminate_on_ended_lifecycle_converges`) pins the legacy shape healing.
- Ended-generation hook handling is right at both layers: routes ack a token-valid `session_end` with 200 and reject every other event with the same 403 as a bad token (no oracle); broker acks without reopening.
- `_terminate`'s produce re-reads state inside the idempotency boundary and the `InterfaceTransitionError` catch correctly converges the hook-won race. Verified the catch's assumption: every nonterminal lifecycle has a legal →stopping edge, so that transition can only fail from `ended` — the `already_ended: True` response is truthful.
- `not_running` from the runtime is now distinguished from graceful timeout (prove absence → close, else unreconciled+lost) — kills the phantom `graceful_timed_out_at` that used to unlock force against nothing.
- Error mapping (#523): `_BadPathId` → 422, transition/broker errors → 409 `state_conflict`, anything else → sanitized 500 with correlation id + server-side traceback. Verified `transition()` still applies `extra_sets` on same-state moves (graceful-timeout re-stamp works), and `_mint_ticket` keeps its explicit 404.

## Axis 2 — Edge cases & gaps

### MAJOR — SC-064: cancel-during-spawn race — `cancelled_before_spawn` concluded from DB-NULL identity while a pane is being created

The cancel-start branch in `_terminate.produce()` judges "no pane or harness identity was ever established" from the DB row only (`tmux_pane_id IS NULL AND pane_pid IS NULL`). But `_create_session.produce()` commits the reservation and then blocks an executor thread on `_runtime.call(spawn(...))` — and `spawn()` only registers the `Generation` in `self.generations` at its very end, after the tmux window/pane exists. The API transport is threaded (executor threads, `run_coroutine_threadsafe`), so a terminate request runs concurrently with an in-flight spawn. Interleaving:

1. create: reservation committed (reserved/starting, pane NULL); spawn in flight — tmux window created, pane alive.
2. terminate: reads pane NULL/NULL → `close_session(cancelled_before_spawn)` → commit → `abandon(session_id)` — **no-op** (generation not yet registered).
3. spawn completes → live pane + registered runtime generation.
4. create unconditionally UPDATEs the pane identity onto the **ended** row and returns 201.

End state: a live harness pane on an ended session/generation. Its hooks 403 (ended generation), New chat 409s `unmanaged_harness` (liveness sees the process), End chat is an idempotent no-op, reconcile-close refuses (absence is disproved — the pane is alive). No supported API path out; manual tmux kill or service restart — the exact stuck-shell wound #519 exists to close, and a violation of req 2's truthful cancellation ("no pane established" is false). The trigger is the feature's primary use case: cancelling a slow or stuck start.

Fix direction (dev's call): make the spawn window visible to the cancel path — register the Generation (or a spawning marker) before the tmux work so `abandon` tears it down; and/or re-check session state after spawn returns and kill the just-created pane by its now-known exact identity; and/or have the cancel branch treat an in-flight spawn as "uncertain → unreconciled".

### MEDIUM — SC-065: `close_session` idempotency guard skips convergence for legacy partial rows

`if occupancy == "ended": return already_ended` — the guard checks occupancy only. Pre-fix code (the old spawn-failure closure) wrote occupancy=ended without touching lifecycle, leaving `ended`/`starting` rows on any DB that lived through the #532 era. On such rows the one closure helper silently no-ops: lifecycle stays nonterminal forever, generation/leases untouched. The Lifecycle Contract row "Already ended **with terminal children** → no churn" assumes terminal children; the guard doesn't check them, so req 1's "one ended session" doesn't hold for the legacy shape this sprint was called to repair. Fix: early-return only when occupancy AND lifecycle are both `ended`; otherwise converge (same-state occupancy is a legal no-op; every lifecycle state has an →ended path).

### Lows (to sprint report, non-blocking)

1. The `except (InterfaceTransitionError, BrokerError) → 409 state_conflict` catch-all also maps deep `BrokerError("interface session N not found")` to a state conflict — a not-found masquerading as a conflict. Only reachable in narrow races (sessions are never deleted), so Low.
2. Cancel start requires the runtime available (`503 interface_unavailable` otherwise) — a stuck reservation can't be cancelled while the runtime is down; reconcile is the road out. Inherits the pre-existing terminate gate; acceptable, noted.

## Axis 3 — Spec conformance (doc #30)

- **Req 1 (convergent termination):** operator request ✓, session_end hook ✓, repeated request (same key replays, fresh key semantic success) ✓, startup reconciliation via `_reconcile` → `close_session` ✓, pane exit → `on_unexpected_exit` → lost/unreconciled + absence-proof path (unchanged, in contract) ✓ — **but see SC-064**: a racing *spawn* is a producer the contract's cancellation row doesn't survive.
- **Req 2 (cancellable reservation):** no-identity → `cancelled_before_spawn` + available ✓ (test pins New chat after); verified identity → normal stop path ✓; uncertain → unreconciled, never silently ended ✓ — **except the in-flight-spawn window (SC-064)**.
- **Req 3 (no terminal→nonterminal):** verified against the edge maps and the race tests; the hook-won race converges instead of 404ing ✓.
- **Req 4 (error taxonomy):** 404/422 for bad path ids ✓, 409 `state_conflict` ✓, sanitized 500 + correlation ✓, broad `except ValueError → no_such_route` removed ✓.
- **#526 lows:** reservation race returns existing owner as 409 with occupancy ✓; provisioning curates SystemExit/OSError/`run_mod.LaunchError` (verified `LaunchError` exists, run.py:816) ✓; path validation distinguishes missing / non-directory / unusable (`.git` probe + writability probe) ✓.

### Ambiguity calls (dev-declared, reviewer-ratified)

1. Verified-identity cancel records `operator_end` — **ratified**: the contract names the normal verified stop path without a distinct reason; `cancelled_before_spawn` is correctly reserved for the no-identity case.
2. Closure from idle/busy/approval/user_input walks through `stopping` — **ratified**: verified all four reach stopping and stopping→ended exists; no edge-map/trigger migration needed.
3. Wake-state scope = input park + wake-batch resolve/park; alert resolution left to unit 2 — **ratified**: req 19 is unit 2's track per the sprint doc.
4. class4 real-host rerun deferred to unit 10's AMI gate — **ratified**: the sprint doc makes unit 10 the acceptance gate; hermetic interleavings + real-tmux integration are green in CI.

## Tests (test_authoring lens)

Strong: the `CloseSessionMatrixTest` walks every lifecycle state legally before closing (a realistic illegal-edge bug turns red); the #532 race test fires the real hook handler inside a mocked `terminate` — a true deterministic interleaving, not a state assertion; error-mapping tests pin all three categories including sanitization (asserts internals absent from the response body). Gaps: no test constructs the cancel-during-spawn interleaving (SC-064) and none closes an `ended`+nonterminal-lifecycle row (SC-065) — both should arrive with the fixes.

## Recommendation

**1 Major + 1 Medium — block.** Findings to DEV5 directly (scoped sprint handoff), one-line copy to PLN1. Re-review on the fix push; Lows 1–2 to the sprint report.

---

# Re-review — fix push @3d0449d (2026-07-23)

**Verdict: REVIEW-CLEAN — both blockers verified closed.** 0 Major / 0 Medium / 2 Low (unchanged, to sprint report).

## SC-064 (Major) — CLOSED, fix verified against the actual interleavings

- `spawn()` now registers the `Generation` in `self.generations` BEFORE the tmux work (interface_runtime.py), so the cancel path's `abandon()` pops and tears the in-flight spawn down — the DB-NULL-identity misjudgment is gone from the outcome space. The try/except pops the registration on any failure, guarded by `is gen` identity so a quick re-create's newer generation is never popped.
- Two `_abort_if_torn_down` checkpoints (after pane identity is known; after `_pipe_pane`, before the sentinel boot file is touched) kill the just-created pane by exact identity and raise `SpawnAborted` — the harness never boots on an ended row. `_pipe_pane`'s marker wait returns early on `terminated`, so an abandoned spawn can't wedge on the writer attach. The second-check → `open(sentinel)` gap is safe: no await between them (cooperative asyncio), and the window is already dead in the abandon-won case.
- `Generation.teardown` guards `if self.pane_id:` — no empty `-t` target to tmux. `abandon()` = pop + `teardown(kill_window=True)`, idempotent via the `terminated` flag; double-abandon (cancel path + create backstop) is a no-op.
- `_create_session` backstop (interface_routes.py): post-spawn occupancy re-check — `ended`/`unreconciled` → `abandon` + `con.commit` + **409 `session_cancelled`**, never a 201, pane identity never persisted onto the terminal row. Covers the pre-registration cancel window the runtime check can't see. `SpawnAborted` itself maps to the same 409.
- Uncertainty discipline kept: the except path deliberately does NOT kill the window on ambiguous tmux failure — that stays for the unreconciled path to judge. Only proven-abandoned spawns kill, by exact identity. Matches req 2's "uncertain → unreconciled, never silently ended".
- Consumers of `generations` (`enqueue_input`, wake writer, attach) all guard `gen is None or gen.terminated`; a mid-spawn pane-less generation degrades to the same `PreSendError` the pre-fix `None` produced.
- Tests: the API test drives the real interleaving on two threads (cancel lands while spawn is blocked pre-registration) and asserts the whole end state — 409, row `ended/ended/cancelled_before_spawn` with NULL identity, shell immediately re-creatable. The real-tmux test kills the pane by exact identity post-abandon and asserts `SpawnAborted`. Both would be red pre-fix.

## SC-065 (Medium) — CLOSED

- Guard is now `occupancy == "ended" and lifecycle == "ended"` — a legacy partial row (occupancy ended, lifecycle nonterminal) converges: occupancy transition skipped (same-state), lifecycle walked to `ended`, generation ended, leases revoked. Original `end_reason`/`ended_at` kept (`recorded_reason = prior_reason`) — the terminal record is not falsified. Fully-terminal repeat close stays a true no-op.
- Test constructs the exact legacy shape (occupancy ended directly, lease held, generation open) and asserts convergence + original record + second-close no-op. Red pre-fix, green post.

## Verification run (this review)

Temp worktree @3d0449d: both hermetic regression tests green via `python -m unittest`; the real-tmux one skips in this sandbox (no tmux/node sidecar) but is green in CI. Full touched files — `test_interface_api` + `test_interface_crash_window` + `test_interface_runtime`: **113 passed, 5 env-skipped, 0 failed**.

## Lows (unchanged, to sprint report)

1. `BrokerError("…not found")` → 409 `state_conflict` masquerade (narrow race only).
2. Cancel start requires the runtime available (503 otherwise); reconcile is the road out.
