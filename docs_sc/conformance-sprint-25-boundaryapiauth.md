---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: Interface chats and interactive planner wake
roadmap_status: in_progress
frozen: false
title: "CONFORMANCE: Sprint 25 boundary/API/auth"
tags: [sprints, conformance, interface, auth, api]
date: 2026-07-23
project: super-coder
purpose: Sprint 25 seq-11 conformance — REV1 shard verdicts
---

# CONFORMANCE: Sprint 25 boundary/API/auth (REV1 shard)

- Spec: doc #20 (Interface-backed planner wake) · Sprint: doc #25
- Judged: code on `main` @13f5405 (detached worktree; never the diffs, never
  the trail). Suite run at that SHA: **801 passed, 4 skipped** (the 4 skips are
  the tmux-gated integration tests — see Verification).
- Shard: Overview, Product Boundary, API Resources, Occupancy×auth, Sprint
  Scope, Security And Privacy, Verification.
- Narrative input (only): decisions #19, #23, #26→#30, #28/#31, #32, #33, #34;
  frozen-CANCEL intentional. Auth surface verified first-hand by REV1;
  occupancy/sprint-scope, security-bullet, and test-coverage sweeps delegated
  to adversarial explore agents and spot-checked against the code.

**Bottom line: 0 Major, 2 Medium, 13 Low.** No finding reopens the sprint.

## Decision records (not findings)

- **Decision #26 recorded as direction-superseded by #30** (per the ratified
  list). The shipped seq-5 bootstrap — exchange of the mode-0600 operator
  capability for an HttpOnly SameSite=Strict browser session + CSRF token
  (`interface_routes.py:1470-1493`) — STANDS; the absence of automatic
  same-origin bootstrap is NOT flagged. Verified the exchange matches what
  #26 required: same-origin proof + capability + 401 without it; cookie not
  JS-readable; response body carries only the anti-forgery token; the UI
  nulls the pasted capability immediately (`ui/app.js:2140-2157`). The
  browser never holds the operator capability — decision-#30 boundary holds.
- **Frozen-CANCEL** (freeze releases bindings + cancels queued wake like a
  close; `server.py:2027-2036`, `interface_broker.py:807-816`, test
  `test_frozen_active_sprint_blocks_submit`): **deviated-intentionally** —
  stronger than the spec minimum, as ratified.
- **Unmanaged-client probe fails open on tmux unreachability**
  (`interface_runtime.py:567-575`): **deviated-intentionally** per decision
  #32 — the required compensation is present and total: the writer preflight
  owns unreachable-tmux as definite PreSendError
  (`interface_runtime.py:543-552` → `interface_broker.py:919-931`); no byte
  moves after a wave-through.
- Seq-10 route shapes (`GET /api/interface/sprint-bindings`, `GET
  /api/interface/sprint-alerts`, `POST …/retry`): **deviated-intentionally**
  per decision #33.

## Verdict table

### Overview — as-specced
- Fixed wake prompt exactly `Check your inbox and act on unread sprint
  events.` (`interface_broker.py:28`); message bodies never enter the
  terminal (hook contract metadata-only, unknown fields rejected);
  delivery_unknown parking never auto-replayed (crash-window tests); tmux
  remains the process host; broker-owned input is the only writable path
  (client `wake` frames rejected, `interface_ws.py:338-341`).

### Product Boundary — as-specced
- One supervised service, one loopback port, HTTP+WS multiplexed
  (`transport.py`); stack replaced the stdlib loop with maintained
  `websockets` sans-io framing (terminal/stream protocol not reimplemented);
  CLI is an API client; `sc run` workers unchanged; provider TUI intact —
  Interface is a terminal frontend, not a chat protocol.

### API Resources — as-specced with deviations (F1, F3, F4, F11)
- 15 of 16 table routes present and conforming: 201+Location on create
  (`:433-436`), occupied race 409 with the current session ref (`:328-338`,
  `:438-450`), 422 validation, 401/403 auth, `{error:{code,message,details}}`
  envelope (`:111-113`), idempotency machinery complete (missing key 422,
  exact replay returns stored response, key+other-body 409, actor+operation
  scoped, insert-race backstop — `:197-240`).
- Deviations: F1 (pr-watches routes), F3 (unknown-field rejection partial),
  F4 (bootstrap idempotency), F11 (timestamp form) — below.

### Occupancy Model × auth — as-specced with Lows (F5, F7, F8)
- Five dimensions with the spec's states; `available` derived only after no
  live/uncertain generation remains AND the liveness scan clears the worktree
  (`:256-282`); busy ≠ available; browser disconnect never frees the shell;
  lost → unreconciled until prove-absence + operator close (`:760-771`).
- Occupancy/lifecycle edge maps match the spec + ratified #23 additions,
  enforced in app and SQL trigger, pinned pair-by-pair by
  `test_interface_transitions.py`. Composer machine as specced
  (unknown→clean only via provider session_start with no accepted human
  sequence; clean→dirty before forwarded bytes; fenced submit or writer
  certification for dirty→clean; ambiguity → unknown).
- Unmanaged-harness refusal verified: New chat 409 `unmanaged_harness`,
  shell projected unreconciled (`:339-350`).
- Auth interactions: shell actors reach ONLY sprint-binding/alert/receipt
  routes (`:1526-1532`) and only their own planner; lease release requires
  the lease's own token; a viewer cannot mint a writer ticket without the
  current lease_token (`:592-602`); force-terminate gated behind a recorded
  graceful timeout (`:665-671`).
- Lows: F5 (`starting→error` edge), F7 (client-dimension projection),
  F8 (illegal-edge audit gaps).

### Sprint Scope — as-specced with one Low (F6)
- Wake eligibility checks kind ∈ task/result/pr_event, same sprint_doc_id,
  addressed-to-planner, doc exists+unfrozen+ACTIVE, unreleased binding,
  occupied session, unreplaced generation, mandatory-hook capability
  (`interface_wake.py:38-82`); `shell`-kind and unscoped messages never wake;
  bodies never parsed; one ACTIVE binding per planner/sprint (partial unique
  indexes). Sprint close atomically releases + cancels with audit reason,
  messages stay unread, chat untouched (`interface_broker.py:636-695`).
- Low: F6 (broker-health not an eligibility condition; submit gate
  compensates).

### Security And Privacy — 7 bullets as-specced, 1 deviated-intentionally, 1 deviated-silently (M1)
1. Localhost-only — as-specced (`server.py:2730`; container publish jails
   `0.0.0.0` to `127.0.0.1` on the host).
2. Token hashes only, snapshot-excluded — **deviated-silently → M1** (below).
3. tmux sockets / token files 0700/0600 — as-specced.
4. Content never persisted/logged — as-specced (broker stores seq/length
   only; output never touches DB/event log; hook emitter discards stdin;
   poller stores fingerprints only).
5. Bounded buffers; bad clients fail without touching the harness —
   as-specced (1 MiB WS message, 64 KiB input, 2 MiB outbound → 1011,
   ping/timeout; all rejects precede the tmux write). Wording nuance → F12.
6. Origin + per-request anti-forgery enforced even on localhost —
   as-specced (Host allowlist + X-CSRF + Origin/Sec-Fetch-Site at the
   single dispatch chokepoint). WS-path Host nuance → F2.
7. Single-use ticket, exact-Origin mandatory, compression disabled, CSP —
   as-specced (verified `ServerProtocol` negotiates no permessage-deflate;
   tickets pop-on-consume, 60 s TTL, session/role/client/lease-bound).
   CSP looseness → F9.
8. Mutations shell-/operator-scoped; no viewer escalation — as-specced.
9. Capability probes fail closed — as-specced, with the unmanaged-probe
   seam deviated-intentionally per #32 (compensation verified total).

### Verification — partially covered (M2, F10, F13)
- Covered (real assertions, not vacuous): A session/occupancy, C authority
  (operator/browser/hook separation, CSRF, tickets, idempotency), E broker
  semantics, F every legal/illegal edge at app+trigger layers, G wake
  items/receipts, H crash windows, I PR polling incl. cutover/single-poller.
- Partial: B streaming (slow client, server-side bounded buffers,
  stream-loss resync, server-side resize ordering untested → F10); J restart
  matrices (container/host restarts, exactly-once startup reconcile
  unasserted → F10).
- **D byte-fidelity matrix: no shipped-code coverage** → M2.
- Provider smoke tests (10 scenarios): no harness exists — pending-by-design
  under decision #34 (real-sprint gate on the dos-app clone).
- The 4 tmux-gated integration tests are skipped in CI (no tmux/node on the
  runner) and unwired anywhere else — locally confirmed 4 skips. Seq-11 B
  (decision #34) runs the integration matrices in-sandbox; recorded, F13.

## Findings

### M1 — Plaintext stream/lease credentials reach the git-tracked snapshot (Medium, deviated-silently)
- Spec: Security bullet 2 — "Token hashes, not plaintext, are durable in the
  live DB and excluded from snapshot."
- `_idempotent` stores each mutation's full response in
  `interface_idempotency_keys.response_resource`
  (`interface_routes.py:218-227`). For `writer-leases` that response carries
  the **plaintext `lease_token`** (`:542`); for `stream-tickets` the
  **plaintext ticket** (`:604-608`). The table is snapshotted with no row
  filter and no sensitive-column exclusion (`snapshot.py:88`;
  `SNAPSHOT_ROW_FILTERS`/`SENSITIVE_COLUMNS` don't cover it), so `./sc
  snapshot` writes live plaintext writer-lease tokens into git-tracked
  `.sc-state/content.sql`, valid up to the 24 h idem TTL while the lease is
  unrevoked.
- Impact bounded by decision #30 (use still needs loopback + a browser
  session/operator cap), but the spec sentence is categorical and the leak
  crosses the credential-exposure boundary #30 names (git remote).
- Fix direction (planner's call): strip credential fields from stored
  idempotency responses, or exclude `response_resource` for
  `acquire_lease`/`mint_ticket` from the snapshot.

### M2 — Byte-fidelity matrix unenforced on the shipped broker (Medium, unimplemented vs Verification D)
- Spec Verification requires byte-for-byte ASCII/UTF-8/bracketed
  paste/control/meta/function-keys/mouse/alt-screen/copy-mode/nested-tmux
  coverage on all three harness TUIs. Zero shipped tests; the only evidence
  is `spikes/interface-stream/tests/` against the SPIKE's broker/server,
  unwired (`run_proofs.sh` hardcodes a dev worktree venv). The shipped
  tmux-gated test does one ASCII echo only. This was the ship-gate's core
  concern (silent loss/dup/interleave); hermetic ordering/dup/gap tests
  mitigate but do not replace it.

### Lows (report-only; none block)
- F1 `POST/DELETE /api/interface/pr-watches` absent; the watch contract
  (baseline-before-arm, ACTIVE-sprint scoping, idempotent registration,
  explicit reconcile) ships on the pre-existing `/_sc/watches` shell-token
  surface (`server.py:2084-2247`). Not in the ratified list →
  deviated-silently. Contract honored; route location and auth scope differ
  from the table.
- F2 WS upgrade path bypasses the Host allowlist (transport demuxes
  straight to `handle_ws`, `transport.py:98-99`; only exact Origin==Host at
  `interface_ws.py:199-204`). A rebind-shaped Origin==Host pair passes;
  single-use ticket (mintable only via Host-checked, session+CSRF HTTP)
  compensates. Defense-in-depth gap, Low.
- F3 Unknown-payload-field rejection only on `sessions` create and
  `hook-callbacks` (`:309-312`, `:1353-1359`); ~10 other mutations ignore
  unknown fields. Spec: "Unknown payload fields are rejected." Low.
- F4 Bootstrap requires Idempotency-Key but never stores it — an exact
  replay mints a SECOND browser session rather than returning the original
  (`:1485-1493`). Benign; Low.
- F5 `starting→error` is a legal lifecycle edge in app map + SQL trigger
  (`interface_state.py:31`) — not in the spec list, not in the ratified #23
  additions, and undriven by any code path. Deviated-silently, Low.
- F6 "input broker healthy" is not a wake-eligibility condition
  (`interface_wake.py:38-82`); the submit gate compensates (preflight →
  bounded retries → critical alert, never a blind byte). Low.
- F7 The client dimension is not projected in the spec's
  none/viewer/writer/unmanaged vocabulary (session GET returns
  writer.held/client_id + clients count). Low.
- F8 Illegal-edge rejections are log-audited only on the hook path (flag
  #51 scope); the receipt PATCH swallows them into a bare 409
  (`:1320-1321`); triggers keep no audit row. Low.
- F9 CSP `connect-src 'self' ws: wss:` admits ws/wss to ANY host, broader
  than "same-origin connection" (`server.py:104-111`). Report-only, Low.
- F10 Verification groups B/J partial: slow-client, server-side bounded
  buffers, stream-loss resync, resize ordering, container/host restart
  matrices, exactly-once startup reconcile unasserted. Low.
- F11 Timestamps are sqlite `datetime('now')` form ("YYYY-MM-DD HH:MM:SS")
  rather than ISO-8601 'T'/'Z' form. Nitpick, Low.
- F12 Replay/seq-gap reject the frame and keep the connection; the spec's
  wording says such clients "fail the client connection". Protective
  property intact; wording-only, Low.
- F13 The 4 tmux-gated integration tests never run in CI (runner has no
  tmux/node; locally confirmed skipped) and are wired nowhere else;
  decision #34's in-sandbox matrices are the intended runner. Recorded, Low.
- Observation (no verdict): `_browser_sessions` is an unbounded in-memory
  dict with no expiry — sessions live until restart. Consistent with the
  personal-machine boundary.

## Method note
Auth surface (bootstrap, cookies, CSRF, Host/Origin, tickets, hook tokens,
idempotency, ack-gating, actor scoping, route table) read line-by-line by
REV1 at 13f5405. Occupancy/sprint-scope, security bullets, and test-coverage
maps produced by adversarial explore agents against the same worktree;
findings M1/F2/F5/F6 independently re-verified by REV1 before inclusion.
