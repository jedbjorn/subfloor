# Review — sprint 25 seq 6 · PR #507 (feat/interface-cli-parity) · r2

- PR: #507 @4de2426 (live head; planner's task named @5c51af4a — one newer
  commit 4de2426, a test-env fix, is on top). CI 6/6 green (verified live via
  `gh pr view`).
- Delta reviewed: r1 head b6322e9..4de2426 — +327/-23 across
  `.super-coder/scripts/interface_cli.py`, `tests/test_interface_cli.py`,
  `tests/test_vm_bake.py`. Nothing else in the PR moved since r1.
- Scope: re-gate flag #48 (r1 M1, three sub-issues) + regression sweep of the
  r1-clean areas. r1 deviation rulings (a/b/c) stand — not re-opened.

## Verdict: CLEAN — 0 Major, 0 Medium. Flag #48 closed. Lows are report-only.

## Sub-issue 1 — ACK-GATING: CLOSED (traced, not trusted)

- `run_stream` now holds `inflight` + `outbuf` under a lock; `pump()` sends
  iff `inflight is None`, pops one frame, stamps the seq, and sets `inflight`
  under the same lock before releasing — two concurrent `pump()` callers
  (sender thread, receiver via `acked()`) cannot double-send, because the
  second sees `inflight` non-None.
- No ack-before-send inversion: `acked(seq)` only clears when `seq ==
  inflight`, and an ack/reject for the inflight frame cannot exist before the
  frame is on the wire. Order preserved.
- Every server input frame settles the gate: `input_ack` (silent) and
  non-terminal `input_reject` (loud notice) both call `acked()`; terminal
  rejects go through `go_readonly` which clears `inflight` + `outbuf`. No
  wedge path short of a malformed server frame missing `seq` (our own server
  always sends it — interface_runtime.py:997-1020).
- The spec's bounded-buffers requirement is met client-side exactly as the
  browser does it (local `outbuf`, one unacked on the wire); the server-side
  unbounded `asyncio.Queue` can now be fed at most one frame per ack round
  trip per client.
- Test `test_input_is_ack_gated_one_unacked_frame` would turn red on the r1
  code (second keystroke would hit the wire immediately) and on a broken
  reject-settle (third frame would never drain). Real assertions on seq
  numbers and payload bytes.

## Sub-issue 2 — WRITER_REVOKED / read-only flip: CLOSED

- `input_reject` with `writer_revoked` or `stale_generation` → `go_readonly`:
  one loud raw-mode-safe notice, buffer dropped, input halted, output
  continues. Idempotent (early-return if already readonly), so the
  send-after-flip race (pump already past the lock when the flip lands) costs
  one server-rejected frame and no double notice — benign.
- Non-active `writer` control (`held`/`none` — the only other states the
  server emits, interface_runtime.py:1102-1116) while `role == "writer"` →
  same flip. This covers HTTP takeover (r1 deviation b): the displaced CLI
  now flips on the next writer-state broadcast instead of typing into the
  void. The r1 Low (no push on takeover) is correspondingly softened — the
  client now reacts to the broadcast that already exists.
- `delivery_unknown` (write failure → server revokes the lease) is handled
  non-terminal, and self-heals: the next input frame draws `writer_revoked`
  → flip. Verified the reject-reason universe against
  `_reject_reason`/`_accept_input` — no terminal reason is missing from the
  client's terminal set.
- Heartbeat thread re-checks `readonly` after every 20s sleep and exits —
  no heartbeating a lost lease.
- Tests: `test_writer_revoked_reject_flips_readonly` (red on r1 code — "b"
  would have been sent) and `test_writer_control_non_active_flips_readonly`.

## Sub-issue 3 — QUIET CONTROL FRAMES: CLOSED

- `input_ack` silent; heartbeat acks (`{"type":"heartbeat"}`) fall through
  the dispatch silently; `writer`/`lifecycle` frames print only on actual
  state change (prev-tracked under the lock); `resync`, errors, and
  transitions print via `notice()` with `\r\n` (raw-mode-safe). The old
  dimmed per-frame echo (`\x1b[2m[...]`) is gone.
- Unknown future frame types now fall through silently instead of printing —
  the right default for a raw-mode TUI attach.
- Test `test_routine_control_frames_are_silent` pins: duplicates noticed
  once, heartbeat/input_ack absent from stderr, no `\x1b[2m`, `terminated`
  still ends the loop.

## Regression sweep (r1-clean areas)

- Diff touches only `run_stream` + tests; verbs, `_attach_writer`, lease
  release in `finally`, termios restore, 0x00/0x04 output path, resize on
  start + SIGWINCH all unchanged in behavior (read in full at 4de2426).
- `_ws_connect` seam: lazy `websockets` import moved inside the seam —
  importing `interface_cli` (verbs, tests) never touches the package. Same
  pattern as `_http`. DEV3's CI-red explanation checks out.
- `MAX_INPUT_PAYLOAD` = 64 KiB server-side; `_read_stdin` caps at 65536 and
  the server rejects only `> 65536` — the docstring claim holds exactly.
- `tests/test_vm_bake.py` setUp clears `SC_SANDBOX` via `mock.patch.dict`
  (snapshot/restore) so a sandboxed suite env no longer leaks into every
  bake test; `test_refuses_in_sandbox` still sets it explicitly. Correct.
- Heartbeat/winch/pump concurrent `ws.send` from two threads is pre-existing
  (r1 had sender+heartbeater); websockets sync client serializes writes.
- Live CI on 4de2426: 6/6 SUCCESS (tests, verify, render-check, CodeQL,
  2× Analyze).

## New Lows (report-only, do not block)

- **L4 — persistent-desync rejects drain forever.** `seq_gap`/`input_locked`
  are treated as non-terminal: the gate settles and the buffer keeps
  draining, so a real desync yields one loud notice per keystroke
  indefinitely rather than a halt. Fail-visible, rare, no corruption —
  arguably the right call; noting it.
- **L5 — two behaviors verified by reading, not pinned by tests:** viewer
  role never buffering input (readonly-from-init) and the heartbeat thread
  halting on read-only (20s sleep makes it impractical to unit-test). Code
  read clean on both.
- r1 Lows unchanged: L1 (vacuous `test_status_named_shell_fetches_session`
  assertion) and L2 (docs/quick-start.md:76 still documents old `./sc
  enter`) were not touched by the fix push; L3 (takeover no-push) is
  softened by sub-issue 2's client-side flip but the server-side broadcast
  suggestion stands.

## Handoff

Review-clean → DEV3 merges (sprint scoped authority: green + clean + ACTIVE
doc). Flag #48 closed. Lows L1/L2/L4/L5 to the sprint report.
