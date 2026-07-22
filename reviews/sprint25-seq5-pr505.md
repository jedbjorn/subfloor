# Review — Sprint 25 seq 5 · PR #505 (feat/interface-vertical-slice) @8fd1900

Reviewer: REV1 (Kimi K3) · 2026-07-22 · task #81 · spec #20 (delivery plan step 3)
Scope: first end-to-end Interface proof — +5383/-11, 23 files. Verdict: **NOT clean — 2 Major, 3 Medium, 11 Low.**

CI at review time: CodeQL/Analyze×2/render-check/verify green; `tests` job still
pending (22+ min at write-up). Local suite on PR head: 634 run, 3 failed — all
`test_vm_bake` (host-only bake tests refusing in-sandbox; environmental, fail
identically independent of this diff), 3 skipped (tmux integration — known gap,
decision #25). All 60 new Interface tests pass locally.

## What was verified (adversarial reads, not descriptions)

- **Transport swap** (`api/transport.py`, `server.py` shim): every existing
  route runs through the unchanged `do_*` methods via `_ShimHandler`; headers
  parsed identically (`http.client.parse_headers`); `Server`/`Date` still
  emitted via stdlib `send_response`; body delivered via `BytesIO` rfile;
  handlers on executor threads (same blocking-sqlite model as
  ThreadingHTTPServer); Interface import failure degrades to review-UI-only +
  503 `interface_unavailable` (req 13). WS demux validated: only
  `/api/interface/session-streams/<id>` upgrades; junk paths rejected.
  Connection:close = accepted ambiguity (decision #24) — not re-flagged.
- **Auth stack**: Host allowlist on ALL `/api/interface/*` HTTP (DNS-rebind);
  operator bearer; bootstrap same-origin fence (Origin==Host or SFS
  same-origin/none; `Origin: null` rejected); HttpOnly SameSite=Strict cookie +
  X-CSRF on browser mutations; hook token authenticates ONLY the callback
  route for its one generation (ended generations rejected); single-use 60s
  tickets bound to session/role/client/lease, viewer tickets drop the lease
  token; exact-Origin WS check; Idempotency-Key on every mutation — missing
  → 422, replay → original response with no second side effect (proven: one
  spawn, one session row), key+new-body → 409; legacy/unmanaged refusal (409
  `unmanaged_harness`, nothing reserved); occupied race → 409 with owner.
- **Brokered I/O**: two-phase `accept_human_input` (pending commit → single
  tmux write → forwarded commit) with dup-ack replay, gap rejection before any
  state change, wake-lock refusal, writer-failure park (delivery_unknown +
  writer revoked + alert, no replay); lease token checked per frame against
  the CURRENT generation's lease; per-generation single-consumer queue
  (no interleave); restart-reattach with exact pane/pid/start-ticks fencing
  (fail-closed); reconnect redraw via shadow snapshot (byte-loss-free attach
  ordering); never any provider resume.
- **Explicit end**: verified-identity SIGTERM, 10s grace, separate SIGKILL
  force; identity mismatch refuses to signal and marks unreconciled+lost;
  `_end_session` records durable closure (occupancy/lifecycle ended,
  generation ended, leases revoked) and New chat derives available only
  after it — proven by test.
- **Tests (test_authoring lens)**: API and exec suites are stringent — real
  DB, real HTTP stub, assertions on rows/bytes/exit codes, hook_token never
  on stderr, capability-before-archive. tmux integration tests are real
  end-to-end proofs where tmux exists (skipped in-sandbox — decision #25).

## Findings

### Major

1. **Flag #40 — Pane-death is never recorded.** `Generation._pump_loop` cannot
   observe pane death: `_open_fifo` keeps `_fifo_keep_fd` (O_RDWR) open for the
   generation's life, so a FIFO writer always exists and the blocking read
   never returns EOF when the pane's process dies and tmux's `cat` exits; the
   OSError path fires only during teardown, where `_on_pump_exit` is guarded by
   `gen.terminating`. `routes._on_unexpected_exit` — the ONLY occupied →
   lost/unreconciled transition — has no live trigger. Restart doesn't save it
   either: `InterfaceRuntime.start()` only *logs* `reattach_all`'s `lost`
   list; no DB transition (its own docstring says "the caller marks it", and
   the caller doesn't). Net: a dead harness shows `occupied`/`idle` forever —
   the rail lies (req 1), New chat stays blocked. Test gap:
   `test_unexpected_exit_marks_lost` calls the routes callback directly, so
   the missing trigger survives the suite. Fix needs an actual death signal
   (tmux control-mode `%window-close`, or a pane-existence probe) wired to the
   callback, the reattach `lost` list persisted, and a test that kills a real
   pane and watches the transition fire.

2. **Flag #41 — No road out of `unreconciled`.** Roads in: unexpected exit,
   terminate identity-mismatch, expired-reservation repair at startup. Roads
   out: none. `_reconcile` only promotes verified-live sessions to occupied or
   changes nothing; `_terminate` refuses non-occupied (409 `not_occupied`);
   nothing transitions unreconciled → ended. Spec requires operator close
   after proved absence (Occupancy Model: "the operator closes or replaces
   it"; Interface Layout: lost/error panes offer close/fresh-generation). As
   built, any unreconciled session bricks its shell's New chat until DB
   surgery. (If recovery actions are deliberately seq-6 "stop/recovery"
   scope, the planner should say so — but then seq 5 ships a shell-bricking
   dead end one route short.)

### Medium

3. **Flag #42 — Force not gated.** `POST /api/interface/termination-requests`
   accepts `force: true` first-touch; spec Workflow 9 allows force "only after
   graceful termination fails and shows the PID/generation it will end". The
   UI sequences it; the API (authority surface for the seq-6 CLI) enforces
   nothing. Gate force on a prior `graceful_timeout` for the session.

4. **Flag #43 — Bootstrap without the capability.** Spec API Resources: the
   same-origin bootstrap "exchanges **it** [the mode-0600 operator capability]
   for an HttpOnly SameSite=Strict browser session". As built, bootstrap
   checks same-origin proof only — any local process (any UID) can self-mint
   operator-equivalent mutation authority with no credential, making the
   mode-0600 token theater against local processes. Tests encode the behavior,
   so it reads deliberate — but it diverges from the spec text and decision
   #18's one-capability model. FnB/planner ruling needed: defect or spec
   write-back.

5. **Flag #44 — PID-reuse hole in terminate.** `_wait_gone` ANDs pane_gone
   with bare `/proc/<pid>` existence — never re-reads start ticks — so a
   recycled PID inside the grace window blocks gone-detection, and the
   follow-up SIGKILL fires at the recycled PID without re-verifying identity.
   The entry fence (exact ticks) is checked once; spec: "never kill an
   uncertain process". Treat a different-ticks occupant as gone and re-verify
   before SIGKILL.

### Low (report to planner; non-blocking)

- WS upgrade path has no Host allowlist (`interface_ws` derives allowed
  Origins from the request's own Host header). Defense-in-depth only — the
  ticket mint is Host-gated HTTP — but the spec names the Host allowlist as
  stack-wide DNS-rebind protection.
- CSP `connect-src 'self' ws: wss:` permits any-origin WebSockets; spec says
  the CSP limits the UI to "same-origin connection".
- Takeover never notifies the displaced writer: the UI handles a
  `writer: revoked` control but no server path emits one; the old writer
  learns only via `input_reject` on its next keystroke.
- A session reconciled back to occupied keeps lifecycle `lost` — no edge back
  to idle (cosmetic until seq-7 hooks).
- `_browser_sessions` grows unbounded — no expiry/eviction.
- transport: no `Expect: 100-continue` (curl stalls ~1s on large bodies);
  unknown methods now 405 (was 501); `dispatch_http`'s 500 leaks `str(exc)`
  with a JSON content-type that isn't JSON.
- Idempotency: crash between produce-commit and key-insert re-executes
  `produce` on retry (create → 409 shell_occupied instead of the original
  201). Narrow window; 409 carries the session_id.
- `launch-<id>.json` hook-token files linger when spawn fails or exec refuses
  before consuming the token.
- Graceful-timeout leaves lifecycle `stopping` with no edge back to `idle`
  short of force-ending (spec state machine's own shape — noting for the
  conformance pass).
- app.js: a >64 KiB paste is rejected and the buffered draft is dropped
  client-side (halted until reattach); the `starting` pane never self-refreshes
  on session_start promotion — manual reselect required.
- `test_vm_bake` × 3 fail locally in-sandbox (host-only by design) — noise,
  unrelated to this diff.

## Ambiguity rulings honored (decision #24 — NOT re-flagged)

operator-provisioning-at-boot; entrypoint-identity session_start;
minimal-lifecycle-until-#83; Connection:close. Transport swap itself was
spec-sanctioned and is cleanly done.

## Handoff

- DEV3: flags #40, #41 (Major), #42, #44 (Medium) — fix + re-push; re-review
  on the fix push.
- PLN1: flag #43 (bootstrap authority — defect-vs-spec-write-back ruling);
  Low list above for the sprint report. CI `tests` was still pending at
  write-up — merge gate needs it green regardless of review outcome.
