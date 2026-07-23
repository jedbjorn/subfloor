# REV2 review — sprint 25 seq 11 part B — PR #515 (real-tmux gate)

Branch `test/interface-real-tmux-gate` @0069c0a8 · dev: DEV3 · task #87 ·
feature #14 · spec #20. Verdict: **REVIEW-CLEAN — 0 Major / 0 Medium / 4 Low**
(report-only). Independent re-execution: 7/7 tmux-gated tests + full suite
808 passed / 0 skipped reproduced in the REV2 sandbox on tmux 3.5a.

## Independent verification (verify-don't-trust)

CI is green 6/6, but CI does **not** execute the tmux gate: `tests` job ran
803 passed / 7 skipped — the 7 skips are exactly the shadow-stack-gated tests
(4 `TmuxIntegrationTest` + 3 `WakeTmuxE2ETest`); GH runners lack the baked
stack. The gate evidence is sandbox-local, so REV2 re-ran it:

- `python3 -m unittest tests.test_interface_wake_tmux -v` → **3/3 OK** (13s)
- `python3 -m unittest tests.test_interface_runtime.TmuxIntegrationTest -v`
  → **4/4 OK** (3s)
- `pytest tests/ -q` (venv pytest) → **808 passed, 0 skipped** (2m36s) —
  reproduces the dev's claim exactly. (CI collects 810 vs local 808; the 2
  delta tests are environment-conditional — cosmetic, noted for the report.)
- Used the same uncommitted `/opt/sc-shadow` workaround (here:
  `.super-coder/shadow/node_modules` symlink) — flag #60 covers the real fix.

## The three runtime fixes (conformance-relevant)

1. **FIFO pump startup race** — VERIFIED. Stand-in `_fifo_keep_fd` is now
   held until `_pipe_pane` proves the writer attached:
   `exec 9>fifo && touch marker && exec cat >&9`. Trace: the fd-9 open
   blocks until a reader exists (pump fd + stand-in both open ⇒ no
   deadlock); the marker can only appear *after* a real writer owns the
   FIFO, so closing the stand-in post-marker has no lost-first-bytes
   window. Bounded: `TMUX_SYNC_TIMEOUT_S` (10s) deadline → `RuntimeError`,
   fail-closed; the routes layer maps that to `unreconciled` per spec #20
   (ambiguous tmux outcome, never auto-kill) — correct. EOF-as-pane-death
   PRESERVED: after the handshake the only writer is `cat` on fd 9; pane
   death → tmux closes the pipe → cat EOF → exit → pump EOF → death chain.
   Empirically: the pane-death integration test passes (it hung/failed
   pre-fix). Stale markers are unlinked at entry; a post-timeout late
   marker self-heals on the next call.
2. **Un-awaited `_on_pump_exit`** — VERIFIED. `run_coroutine_threadsafe`
   now actually schedules the coroutine; `coro.close()` on `RuntimeError`
   avoids the never-awaited warning. Single-fire (pump thread returns
   after one notify); teardown race guarded by `gen.terminating`; death
   chain runs to completion (generation-end / alert / lost transition —
   seen live in test logs). One residual gap → L1.
3. **Shadow rebuild trailing newline** — VERIFIED. Strip lives in
   `capture_pane` itself, so all three rebuild paths (attach fallback
   :378, reattach :901, resync :1051) share the one helper — no
   divergence. Exactly one `\n`, guarded by `endswith` (no-op when tmux
   emits none; a legitimately blank last row leaves a preceding `\n\n`
   intact after one strip). Empirically: the reattach test asserts
   `b"before-restart" in viewer.redraws[0]` — row-1 content — which turns
   red if row 1 scrolls off. Passes.

## The three new e2e tests (`test_interface_wake_tmux.py`)

All drive REAL tmux (private server, stub harness in raw mode + READY
marker), no mocks of the writer path:

- **wake-into-fresh**: aged `occupied_at`/`created_at` (the defect shape),
  readiness only via provider `session_start`; asserts no byte moves at
  0.6·quiet, submission only after the readiness debounce (≥0.8·quiet),
  exactly-once (`capture.count(prompt) == 1`, one batch row). Not
  vacuous; flag #49 end-to-end. ✓
- **out-of-order hooks under input load**: 20 frames byte-exact
  (`joined(outputs) == joined(frames)` after a 0.3s stray-window), seqs
  2/4 fenced stale, replays 3/5 rejected, `last_hook_seq` monotonic at 5,
  lifecycle idle, continuity intact. Matches `record_hook`'s
  `hook_seq <= last_hook_seq` rejection. ✓
- **parking-under-crash**: tmux server killed between preflight and
  send-keys ⇒ exactly one write attempt, batch parks `delivery_unknown`,
  `wake_batch_delivery_unknown` alert, and neither a fresh notify nor
  `startup_pass` replays (2s window; drain is event-driven, so no steady
  timer could fire later). Decision #22 on the real path. ✓

The seq-8 invariants REV2 cleared (single-batch parking, wake gate
idle+clean+quiet, no auto-replay) still hold under real tmux.

## Tooling fixes

- conftest `import server` collision: VERIFIED sound. Spike `server.py`
  has no top-level self-imports; nothing else in the spike imports plain
  `server`; the namespaced `importlib.util` load can't poison
  `sys.modules["server"]` for the engine API tests. ✓
- `shadow/package.json` script removal: VERIFIED correct — `./sc test`
  (sc:343-344) only runs `npm test` where a `scripts.test` is declared,
  so deleting the scaffolded failing script is exactly the fix. ✓

## Flake assessment (`test_bounded_buffers`)

Test-harness calibration flake, NOT a product defect. The hard windows
(30s read window for the 1011 close, 120s good-client delivery of a 5 MB
burst) live in the *test*, calibrated to host IO; in-sandbox the
slow-consumer close lands at 40–50s. Spike code untouched, engine gate
unaffected, passed 12/12 standalone. Concur with report-not-absorb;
ratify ambiguity call 1.

## Ambiguity calls — all ratified

1. 'full suite' = pytest tests/ + spike standalone + bare ./sc test —
   reasonable; flake fenced + disclosed.
2. Runtime defects fixed in-unit — they blocked the matrices and are the
   gate's raison d'être; fully disclosed. Correct call.
3. Stub pane raw mode + READY marker — test-side fix; canonical mode
   would swallow newline-free frames, READY proves stty ran. Sound.
4. Shadow-module path gap worked around locally, flag #60 filed —
   image/engine fix correctly out of scope.

## Lows (report-only, non-blocking)

- **L1** — `_on_pump_exit` is scheduled via `run_coroutine_threadsafe`
  but the future is never observed (`interface_runtime.py:315`): an
  exception inside the death chain (alert insert, `on_unexpected_exit`
  callback) vanishes with no log. Backstopped by restart-time
  reconciliation, hence Low. Suggest try/except + `_log` inside
  `_on_pump_exit` or a logging done-callback.
- **L2** — a `_pipe_pane` timeout raise mid-`spawn` leaks the
  generation's `_pump_fd`/`_fifo_keep_fd` and the shadow entry (gen never
  registers ⇒ `teardown` never runs). The DB side lands `unreconciled`
  per spec; it's a process-lifetime fd/resource leak per failed spawn
  only. Suggest cleanup-before-raise in `spawn`.
- **L3** — CI structurally cannot run the tmux gate (7 skips on GH
  runners). Evidence is sandbox-local; REV2 reproduced it independently
  this review. Worth a standing note in the seq-11 close-out that the
  gate tier has no CI coverage; also reconcile the 810 (CI) vs 808
  (local) collection delta in the sprint report.
- **L4** — a `_pipe_pane` deadline raise can leave a late-arriving
  `.pipeup` marker on disk; self-heals at the next call for the session.
  Negligible.

## Recommendation

Merge. DEV3 merges under scoped authority + files the part-B unit report.
