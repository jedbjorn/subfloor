# Review — sprint 25 seq 6 · PR #507 (feat/interface-cli-parity) · r1

- PR: #507 @b6322e9, base main @7e39ce0 (seq-5 merge). +1797/-96, CI 6/6 green (verified live).
- Spec: doc #20 (Interface-backed planner wake), task #82 — CLI parity + full Interface workflow.
- Reviewer: REV1 (Kimi). Diff read in full; claims verified against PR-head source, not the PR body.

## Verdict: NOT clean — 0 Major, 1 Medium (blocks), 3 Low (report)

## Verified clean (traced, not trusted)

1. **`sc enter` routes through the API broker.** `sc` maps `enter`/`enter-*` to
   `./sc interface enter`; `cmd_enter` resolves via `GET /shells` and either
   reserves (`POST /sessions`) or reattaches the occupied generation (writer if
   free, read-only + take-control notice on 409). No `tmux attach`, no provider
   resume. Raw interactive launch refuses in `run.py:main()` before `open_db`
   can create an archive; `interface_exec` calls `prepare_launch` directly and
   is unaffected (verified). `SC_RAW_BOOT` escape hatch + headless/RENDER_ONLY
   pass the gate; `test_style_spinner` updated accordingly.
2. **No private side paths.** Every verb goes through `api()` — operator bearer
   + uuid4 `Idempotency-Key` on every mutation, asserted on the real wrapper in
   tests. API outage → exit 3 with supervised-runtime remediation, no direct-DB/
   tmux fallback (test proves). `_flavor_harness` reads shell flavor from the
   engine DB — launch routing data, not interface state; acceptable, documented.
3. **Writer-lease liveness is sound.** `acquire_writer` stamps `heartbeat_at` at
   acquisition (40s grace); browser heartbeats 10s, CLI 20s < 40s sweep bound;
   detach-revoke and sweep are fenced by lease id + token hash + generation and
   scoped to runtime-owned generations; double-revoke is a no-op; stale detach
   can't clobber a re-acquired lease. 9 new tests cover each fence and the
   re-acquire race. Detach fires from the WS layer's `finally` — clean and
   error closes both revoke.
4. **UI flows match spec.** Rail projection is server-side; one primary New
   chat; picker sources harness/model/effort from `/api/models`; takeover is
   confirmed + explicit; End chat is graceful-first with force gated on
   `graceful_timeout` naming PID/generation (matches the API's
   `force_requires_graceful_timeout`); recovery-pane close is offered only
   while `unreconciled` and absence is re-proved server-side; mobile picker is
   CSS-hidden on desktop with a min-height terminal. `ifOpToken` is now nulled
   on EVERY non-ok bootstrap exit including network failure (seq-5 r3 Low —
   properly closed).
5. **Removed "revoked" writer-state case is correct.** `writer_control` emits
   only active/held/none (verified interface_runtime.py:1102-1116); the new
   "non-active while I believe I'm writer → ifRevoked" mapping is the right
   signal.

## Deviation assessments (the three the task asked me to gate)

- **(a) No permission-mode field on POST /sessions — SOUND, nothing lost.**
  The normal boot has no permission picker: permission rides
  `apply_sandbox` + adapter `launch_flags`/`env` (run.py:1377-1401) inside
  `prepare_launch`, which `interface_exec` uses. Spec Workflow 3 names only
  harness/model/effort choices. Adding a field would have been inventing API
  surface. Clean call.
- **(b) HTTP takeover does not push to the displaced writer — ACCEPTABLE as
  declared; recommend a cheap follow-up.** Revocation IS atomic server-side:
  the displaced writer's next frame is rejected `writer_revoked` (fenced by
  generation + token, interface_runtime.py:957-975), so the spec's "makes the
  old client read-only" holds as authority immediately. Only the notification
  is lazy. The browser flips gracefully on the reject; the CLI does not (M1
  below). `interface_routes.bind_runtime()` already exists — a one-call
  writer-state broadcast in `_acquire_lease` would close this properly. Low.
- **(c) wake_state disarmed — CORRECT.** Seq 8 territory; the field renders
  from the session detail and nothing pretends otherwise.

## Findings

### M1 (Medium, blocks) — CLI attach client lacks client-side protocol semantics

`run_stream` (interface_cli.py:352-429) is a verbatim spike port, and the
spike was a proof harness, not the production client:

1. **No one-unacknowledged-frame buffering.** Spec #20 Input Broker: "A writer
   may have only one unacknowledged input frame; the browser or CLI buffers
   later keystrokes locally." The browser implements inflight/outBuf; the CLI
   sender fires every read straight onto the wire. Server-side the generation
   queue is an UNBOUNDED `asyncio.Queue` (interface_runtime.py:257) — a stalled
   broker plus continued typing grows memory without bound, against the spec's
   bounded-buffers requirement. (Ordering itself is safe: one TCP stream,
   session-scoped monotonic seqs, server-side dedupe.)
2. **No reaction to `input_reject`/`writer_revoked`.** After an HTTP takeover
   (deviation b — no push), the displaced CLI writer keeps transmitting every
   keystroke into its revoked lease; each is rejected server-side and the only
   signal is a dimmed stderr line. The user types into the void with no
   prominent notice; the browser handles the same event with an explicit
   read-only flip + notice.
3. **Every control frame prints to stderr in raw mode.** One `input_ack` per
   keystroke, heartbeat acks, writer/lifecycle frames — all dimmed-printed to
   stderr while the tty is raw, garbling the attached TUI between redraws.

Not Major: no loss or corruption path — server-side ordering, dedupe, lease
enforcement, and fencing all hold (traced `_accept_input`). But `sc enter` as
a broker client is this unit's headline deliverable, the PR admits live attach
was never exercised ("Live end-to-end attach not exercised from the sandbox"),
and all three bite on first real use. Fix: ack-gate the sender (one inflight,
local buffer), treat `writer_revoked`/terminal rejects as read-only/halt with
a loud notice, and stop echoing routine control frames to stderr (errors and
state transitions only).

### Lows (report-only, do not block)

- **L1 — vacuous test assertion.** tests/test_interface_cli.py
  `test_status_named_shell_fetches_session`:
  `assertEqual(self.http.find(...), self.http.find(...))` compares a call list
  to itself — proves nothing. Meant to assert the fetch happened (non-empty).
- **L2 — docs drift.** docs/quick-start.md:76 still documents the old
  `./sc enter` (authenticate, pick shell, pick harness). The `sc` help text
  was updated; the doc wasn't.
- **L3 — takeover without push** (deviation b): enforcement is atomic, notice
  is lazy; one broadcast call via the already-bound runtime would close it.

## Handoff

M1 to DEV3 (author). Lows to the sprint report. Re-review on the fix push.
