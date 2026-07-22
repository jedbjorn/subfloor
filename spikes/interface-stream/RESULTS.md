# Interface stream + input-broker spike — results

Sprint 25 seq 3 · spec #20 task #79 · branch `feat/interface-stream-spike`
Date: 2026-07-22 · DEV3

## Verdict

**GREEN.** A maintained stack relays real tmux-hosted Claude, Codex, and
Kimi TUIs with exact byte fidelity, redraw, resize, reconnect, writer
transfer, bounded buffers, and generation-fenced human-vs-wake ordering.
No lost, duplicated, bypassed, or interleaved input was observed in the
final matrix. The stack in DESIGN.md is recommended for seq 4+.

Reproduce: `cd spikes/interface-stream && ./run_proofs.sh`
(final run: see "Final matrix" below).

## Chosen stack (pinned)

| Component | Choice | Version | License |
|---|---|---|---|
| Service | Python asyncio, one loopback port (HTTP API + static + WS) | 3.12 | — |
| WS library | `websockets` (sans-io `ServerProtocol` embedding) | 16.1.1 | BSD-3-Clause |
| Browser terminal | `@xterm/xterm`, vendored `static/vendor/xterm/` | 6.0.0 | MIT |
| Shadow emulator | `@xterm/headless`, one Node sidecar per service, stdio-multiplexed | 6.0.0 | MIT |
| Process host | tmux private server, mode-0700 socket | 3.5a | ISC |

- Wire subprotocol `sc-term.v1`: binary `0x01|seq:u64be|payload` input,
  `0x03|rows|cols` resize; binary `0x00` output / `0x04` redraw; JSON
  text control (acks, rejects, writer, lifecycle, wake, resync,
  heartbeat, error). Input ack only after dirty-mark + ordered-queue
  accept + forward; duplicate seq → prior-ack replay, never re-forward.
- Auth/ticket: operator-bearer HTTP API; single-use 60 s stream tickets
  bound to session/role/lease; exact-Origin check; writer requires the
  current lease token; takeover atomically revokes.
- Launch: pane waits on a pipe-ready sentinel, then `exec`s the harness
  — zero lost boot bytes; pane still directly execs the harness.
- Buffer limits: per-client outbound 2 MiB (close 1011), input frame
  ≤64 KiB, WS max_size 1 MiB, ping 20 s / timeout 40 s, pump→loop
  bridge ≈8 MiB with continuity_broken → capture-pane resync.

## Proof evidence

tmux mechanics (probed directly, tmux 3.5a):

- **Input**: `send-keys -H` (space-separated hex args, ≤512-byte chunks)
  — 114,172-byte corpus incl. 0x00, UTF-8, bracketed-paste frame:
  byte-exact.
- **Output**: `pipe-pane 'cat > fifo'` — byte-exact (tty ONLCR
  accounted; TUIs run raw).
- **Bursts**: 64 KB / 256 KB / 1 MB / 5 MB marker-to-marker exact; a
  2 MB burst with a 3 s consumer stall also exact — tmux flow-controls
  the pipe, **no silent loss**. (An earlier "tmux drops after ~2 MB"
  reading was a probe artifact: python buffered `stdin.buffer.read(n)`
  blocks mid-stream until n/EOF. Recorded here so seq 4+ never re-learns
  it: pump threads must use raw `os.read`.)
- **Resize**: `resize-window` → SIGWINCH confirmed.
- **Redraw data**: tmux cannot report terminal modes → reconnect needs
  the `@xterm/headless` shadow (grid + modes + cursor), cross-checked
  against `capture-pane -ep` + `#{cursor_x},#{cursor_y}`.

Final matrix (pytest, this branch): see run log `proofs-*.log`.

- test_input_fidelity — 117,279-byte corpus in 173 mixed frames through
  WS→broker→tmux→raw pane reader: sha256 identical.
- test_output_fidelity — 117,279 bytes pane→WS byte-exact; colored
  screen: shadow redraw vs capture-pane replayed through two fresh
  headless terminals → grids identical cell-by-cell.
- test_redraw — fresh attach mid-session: grid identical, cursor
  [19,9]==tmux, alt-screen + mouse + bracketed-paste + cursor-hidden
  modes re-issued (`?1049h ?1000h ?1006h ?25l` present in redraw).
- test_resize — 80×24→132×40→100×33 SIGWINCH log exact; post-resize
  shadow-vs-capture grids identical.
- test_reconnect — pane PID unchanged across mid-stream disconnect;
  new attach gets current redraw + live stream (monotonic).
- test_writer_transfer — viewer rejected `viewer_read_only`; takeover →
  old writer `writer_revoked`; new writer lands exactly once; dup seq →
  ack replay; pane file exactly `AC`.
- test_bounded_buffers — 5 MB burst to a healthy client byte-exact
  while a never-reading client is closed 1011; generation unaffected;
  70 KiB input frame rejected `payload_too_large`, zero bytes forwarded.
- test_ordering — 210 iterations × 3 seeds: 424 human frames accepted,
  multiset exact, per-frame contiguity verified by stream parse; 121
  wakes (20 submitted / 101 cancelled); every submitted wake prompt
  contiguous in the received stream; zero quiet-gate violations; 58
  duplicate-seq ack replays, no double-forward.
- test_tui_matrix — claude 2.1.217 / codex 0.145.0 / kimi 0.27.0 each
  booted through the broker: non-trivial boot stream, shadow snapshot
  text == capture-pane text, "spiketest" typed via broker visible in the
  real TUI, resize redraw, clean termination. No xfail needed.
  Fidelity assertions hard-fail (only boot/auth failures xfail).

## Defects found by the gate process (fixed, re-proven)

1. **Connection liveness during output floods (real, would have
   shipped).** A long transfer to a reading client could still die at
   ~40 s, observed as a 5 MB transfer permanently stalling part-way
   (broker-side counters proved pump == fanout == full corpus; the loss
   was past the broker). Two compounding layers, both fixed and
   re-proven:
   - the sans-io layer's automatic pong replies were never flushed to
     the transport (server.py now flushes `data_to_send()` after every
     `receive_data`), so spec-compliant clients ping-timed-out;
   - server keepalive measured liveness only by inbound traffic, so a
     client slowly-but-steadily consuming a flood (its pongs queued
     behind megabytes of unread data) was killed mid-transfer; the
     writer loop now refreshes liveness on successful drain.
   A test-side contributor (polling the accumulated buffer by copying
   it whole every 20 ms) starved the receive thread and made slow runs
   snowball; the harness now polls by length (`wait_len`). Post-fix:
   8/8 + 20/20 + 6/6 stress iterations and the full matrix green.
2. **Viewer wake authority hole.** `{"type":"wake"}` was accepted from
   read-only clients. Now writer-gated; production moves wake
   submission to the coordinator/API anyway (not a client frame).
3. **Test-side artifacts** (not stack defects): buffered-reader stall
   (above); capture-pane replay needed absolute CUP rows; xfail scope
   initially masked fidelity assertions — now AssertionError hard-fails.

## Known limitations → follow-ups for seq 4+ (not gate blockers)

- **Lifecycle/composer is simulated** in the spike (output-quiet →
  idle, any output → clean). Production must drive these from the
  authenticated harness hooks per spec #20 — dirty→clean only via
  fenced submit callback or writer certification. The ordering proofs
  hold for the queue discipline; the hook contract is seq 7/8 work.
- **Snapshot coverage**: redraw re-issues grid, SGR attrs, cursor
  position/visibility, alt screen, bracketed paste, mouse modes, app
  cursor/keypad, wraparound. Not covered: origin mode (?6), DEC
  charset shifts, cursor style (DECSCUSR) — re-issue if a harness is
  observed using them (TUI matrix found none).
- **Pump→loop handoff** uses `call_soon_threadsafe` (unbounded callback
  queue if the loop itself stalls). Production: count in-flight
  callbacks against the bridge budget.
- **Spike elisions** by design: idempotency keys, CSRF/browser-session
  bootstrap, hook tokens, engine-DB persistence, `sc enter` routing,
  one-session-per-shell uniqueness, PID/start-tick fencing. All specced
  for seq 4+; nothing in the spike contradicts them.
- Pytest teardown prints "Task was destroyed" warnings (cosmetic
  event-loop shutdown; not failures).

## Gate statement

Input bytes: exact, ordered, fenced, deduplicated — in every matrix run
after the pong fix. Output bytes: exact end-to-end incl. 5 MB bursts
and slow consumers. Ordering: human-first cancels wake (composer dirty
set before forward); wake-first submits indivisibly; no interleave
observed in 210 races. The build may proceed to seq 4 on this stack.
