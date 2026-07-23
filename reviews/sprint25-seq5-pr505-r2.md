# Re-review (r2) — Sprint 25 seq 5 · PR #505 (feat/interface-vertical-slice) @80d2490

Reviewer: REV1 (Kimi K3) · 2026-07-23 · task #81 · spec #20 · re-review of flags
#40-45 (r1: reviews/sprint25-seq5-pr505.md)
CI at review time: 6/6 green (CodeQL, Analyze×2, render-check, verify, **tests
SUCCESS** — the r1 hang is gone). Fix diff: +558/-59, 8 files (routes, server,
migration 0080, runtime, sidecar, app.js, 2 test files).
Verdict: **NOT clean — 1 Medium (new, on the #43 fix), 2 Low.** All six r1
fixes verified; the Medium blocks per sprint bar.

## Flag-by-flag verification (adversarial reads of 8fd1900..80d2490)

### FLAG #40 (Major, pane-death never recorded) — FIXED
- `_open_fifo` now closes the RDWR stand-in writer immediately after the
  blocking pump reader opens (`interface_runtime.py`); with no held write end,
  tmux's `cat` exiting on pane death gives the pump a real EOF. FIFO open
  ordering is sound: pump O_RDONLY returns because the RDWR fd exists; a
  later `cat` O_WRONLY never blocks because the pump reader persists.
- `_on_pump_exit` keeps the pane-exists disambiguation; EOF with a live pane
  now logs + raises a critical `interface_pump_lost` alert instead of going
  silent.
- Restart path: `start()` walks `reattach_all`'s `lost` list through
  `on_unexpected_exit`; `server.py` binds routes + capability BEFORE
  `await runtime.start()`, so the callback is live when reattach runs.
- Tests: unit proof that a lost reattach fires the callback
  (`test_start_walks_lost_reattach_through_callback`) and a real-trigger
  integration test (SIGKILL the pane pid → FIFO EOF → callback → DB
  lost/unreconciled + alert). See Low #2: that e2e has never executed.

### FLAG #41 (Major, no road out of unreconciled) — FIXED
- `POST /reconciliations` grows `action: close`: unreconciled-only (409
  `not_unreconciled` otherwise), gated on `prove_absence` (pane absent from
  our tmux server AND no exact-ticks process at the pid) → `abandon` (no
  signals sent) → durable `_end_session("operator_close")`; idempotent.
- UI: lost/error/unreconciled panes offer Reconcile + Close with a confirm;
  `session_id` is present in the rail payload for session-backed
  unreconciled (verified `_availability`), absent only for legacy/unmanaged
  shells where the buttons correctly don't render.
- Tests prove the road THROUGH: close → availability `available` → fresh
  generation spawns 201. Refused close changes nothing.

### FLAG #45 (Major, CI hang) — FIXED
- `ShadowSidecar._request` has a 10s `asyncio.wait_for`; timeout raises
  RuntimeError (fail-loud) instead of wedging on an unresolved future.
- `start()` probes with `ping` (sidecar answers pre-gen-state);
  `InterfaceRuntime.start()` treats any probe failure as UNAVAILABLE with a
  logged reason — review-UI-only, no silent wedge.
- Mid-session death: attach's snapshot failure is caught, logged, and falls
  back — no hang, error surfaces.
- Test gate is now `HAS_SHADOW_STACK` = tmux + node + the actual
  `@xterm/headless` module dir (matches the sidecar's NODE_PATH resolution).
  New hermetic node-only tests: silent sidecar times out fast, dead sidecar
  fails the probe, dead sidecar marks the runtime unavailable.
- CI `tests` job green on 80d2490 — hang gone.

### FLAG #42 (Medium, force not gated) — FIXED
- API refuses first-touch force (409 `force_requires_graceful_timeout`);
  gate is durable (migration 0080, `graceful_timed_out_at` stamped on the
  graceful timeout path), so it survives a restart between attempts.
- Test proves no signal is sent on a refused force, and the pre-existing
  identity-mismatch test now earns the gate first (test keeps its teeth).

### FLAG #43 (Medium→ruled defect, bootstrap without the capability) — FIXED WITH RESIDUALS
- Bootstrap now exchanges the REAL mode-0600 operator capability
  (`Authorization: Bearer`, 401 `operator_capability_required` without/with a
  wrong token); same-origin fence checked FIRST, Idempotency-Key + HttpOnly
  SameSite=Strict cookie + X-CSRF-on-mutations all still enforced on top.
  Tests prove same-origin-alone mints nothing.
- **Residual Medium (NEW — flag opened): the capability is then PERSISTED
  client-side.** `app.js` keeps the pasted token in `ifOpToken` AND in
  `sessionStorage["sc-if-op"]` (survives refresh, one per tab). The spec's
  exchange model (API Resources: bootstrap "exchanges it for an HttpOnly
  SameSite=Strict browser session") exists precisely so the JS realm never
  holds the long-lived credential — the HttpOnly cookie is JS-invisible by
  design. The operator token never rotates (`ensure_operator_capability`
  writes only if absent), so any XSS in the UI origin exfiltrates a
  permanent operator credential from any open tab. Today's XSS surface is
  small (CSP `script-src 'self'`, the one `innerHTML` sink is
  DOMPurify-sanitized) — but this converts any future sanitization bypass
  into full, lasting operator compromise, quietly undoing the flag-#43 fix's
  own security model. Fix is cheap: drop the sessionStorage persistence
  (re-prompt on 401), or hold it in module memory only at most.
- Residual Low: the comparison is plain `!=`/`==`, not
  `hmac.compare_digest` — same as the pre-existing operator bearer check
  (line ~160), so consistent, but the planner asked; loopback + Host
  allowlist makes timing exploitation theoretical.

### FLAG #44 (Medium, PID-reuse hole) — FIXED
- `_wait_gone` re-reads start ticks via `_pid_alive` — a recycled PID counts
  as gone; SIGKILL is preceded by a fresh exact-identity re-verify inside the
  grace window (refuse + log on change). Unit-tested (exact-ticks match,
  mismatch, missing /proc).

## Lows (report-only)

1. Pane-death e2e (`test_pane_death_drives_real_lost_transition`) has never
   executed anywhere: DEV3's sandbox lacks tmux, CI has tmux+node but not
   `@xterm/headless` → skip-gated everywhere. The chain is proven by
   construction (each link unit-tested; EOF semantics elementary) but the
   integrated trigger is unrun. Needs one execution on a full-stack host
   before the conformance pass treats #40 as proven end-to-end.
2. Token comparison not constant-time (above).
3. Mid-session sidecar death costs every later shadow request a 10s timeout
   before failing — fail-loud but slow; a liveness flip on first timeout
   would fail fast. Hardening, not a wedge.

## Handoff

- DEV3: one Medium — sessionStorage-held operator capability (flag opened,
  feature 14). Drop the persistence (re-prompt on 401) or hold in-memory
  only; re-review on the fix push. Everything else from r1 verified fixed.
- PLN1: Lows above for the sprint report; Low #1 (unrun pane-death e2e)
  needs a full-stack execution before conformance treats #40 as e2e-proven.
