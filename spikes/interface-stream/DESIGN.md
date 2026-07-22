# Interface stream + input-broker spike — design record

Sprint 25 seq 3 / spec #20 task #79. Scope: choose and prove the
browser/CLI terminal-stream stack for Interface. Hard gate: any lost,
duplicated, bypassed, or interleaved input stops the build for rescope.

## Probe evidence (2026-07-22, tmux 3.5a, private server)

| Path | Result |
|---|---|
| Output: pane → `pipe-pane 'cat > fifo'` | Byte-exact. Corpus (all 256 byte values ×4 + SGR + UTF-8 + bracketed-paste markers) captured contiguously after accounting for tty ONLCR (`\n→\r\n`); TUIs run raw mode, so no transformation applies to real harness output. |
| Input: broker → `send-keys -H` | Byte-exact. 114,172 bytes (every byte 0x01–0xFF, 0x00, UTF-8, `\e[200~…\e[201~` paste frame, 112 KB paste in 512-byte calls) received exactly by a raw-mode pane reader. Note: `-H` takes space-separated hex args, one per byte; chunk ≤512 bytes/call. Ordering across calls is preserved. |
| Backpressure: slow pipe consumer | **Lossless flow control.** 2 MB burst with a 3 s consumer stall delivered exactly (marker-to-marker), as did 64 KB/256 KB/1 MB/5 MB bursts with a fast consumer. tmux back-pressures the pane pty instead of dropping. (An earlier reading of "~2 MB then silent drop" was a probe artifact: python's buffered `stdin.buffer.read(n)` blocks mid-stream until n bytes or EOF, stranding the tail; raw `os.read` shows the true behavior.) |
| Resize | `resize-window` delivers SIGWINCH to the pane process (verified 80×24 → 132×40). |
| Redraw data | `capture-pane -ep` emits the visible grid with SGR; cursor via `#{cursor_x}`, `#{cursor_y}`; `#{alternate_on}` reports alt screen. No alt-screen capture flag in 3.5a — modes (mouse, bracketed paste, app cursor) are NOT observable from tmux. |
| Harness TUIs | claude 2.1.217, codex-cli 0.145.0, kimi 0.27.0 all boot as real interactive TUIs in a private tmux server. |

## Chosen stack

- **Service**: Python 3.12 asyncio, `websockets` 16.1.1 (BSD-3-Clause).
  One process, one loopback port: HTTP API + static UI + WS upgrade.
  Replaces the stdlib request loop, as spec #20 permits.
- **Browser terminal**: `@xterm/xterm` 6.0.0 (MIT), vendored under
  `vendor/`; no CDN, per-message compression off.
- **Shadow emulator**: `@xterm/headless` 6.0.0 (MIT) in ONE Node 22
  sidecar process per service, multiplexed over stdio, volatile memory
  only. Identical emulation core to the browser client → snapshot
  fidelity by construction. Needed because tmux cannot report terminal
  modes; a reconnecting client must receive grid + modes + cursor.
  Cross-checked against `capture-pane -ep` in the proof matrix.
- **tmux I/O**: private server, mode-0700 socket. Output `pipe-pane
  'cat > <fifo>'` (broker pre-opens FIFO RDWR so the writer never
  blocks). Input `send-keys -H`, ≤512 bytes/call. Resize
  `resize-window`. Snapshot fallback `capture-pane -ep` + cursor
  formats.
- **Launch**: pane shell waits for a `pipe-ready` sentinel, then
  `exec`s the harness — zero lost boot bytes, pane still directly
  execs the harness.

## Wire protocol — subprotocol `sc-term.v1`

- Negotiated via WebSocket subprotocol; wrong/absent → reject.
- Client → server binary frame: `0x01 ‖ seq:uint64be ‖ payload`
  (human input, ≤64 KiB payload); `0x03 ‖ rows:uint16 ‖ cols:uint16`
  (resize; ordered, does not dirty composer).
- Server → client binary frame: `0x00 ‖ payload` (terminal output);
  `0x04` redraw snapshot (escape-sequence reconstruction from shadow:
  reset, modes, alt-screen entry, full grid with SGR, cursor).
- Text JSON control both ways: `input_ack{seq}`, `input_reject{seq,
  reason}`, `writer{state}`, `lifecycle{state}`, `wake{state}`,
  `resync{reason}`, `heartbeat`, `error{code}`.
- Input ack only after dirty-mark + broker-queue accept (durable state
  change before ack, per spec). Duplicate known-forwarded seq → replay
  prior ack, never forward twice. Gap/non-monotonic seq → reject, no
  bytes forwarded.

## Auth / tickets (spike-scale, per spec #20 shape)

- `POST /api/interface/stream-tickets` (operator bearer in spike)
  mints a single-use ticket bound to session, generation, client id,
  role (viewer|writer), 60 s expiry.
- WS upgrade: `?ticket=` + exact `Origin` check + subprotocol
  negotiation; ticket consumed on upgrade.
- Writer path additionally requires a lease token from
  `POST /api/interface/writer-leases`; takeover atomically revokes the
  prior lease (old token's frames rejected with `writer_revoked`).

## Broker ordering — generation-fenced queue

Per generation, one asyncio queue, one consumer task. Items:
`HumanInput{generation, lease, seq, bytes}` | `WakeSubmit{generation,
prompt}` | `Resize` | `Takeover`. Serialized execution ⇒ a wake
submission is one indivisible item → one `send-keys` call; no human
frame can interleave inside it. A human frame ordered first sets
composer `dirty` before its bytes are forwarded; a `WakeSubmit`
dequeued later revalidates gates (`idle`, `clean`, quiet ≥3 s, empty
human queue, generation current) and cancels without sending a byte.
Every item carries the generation id; stale items are dropped and
audited.

## Buffer limits

- Per-client outbound queue 2 MiB → close client `1011 slow
  consumer`; harness and other clients unaffected.
- Inbound data frame ≤64 KiB; WS `max_size` 1 MiB; ping 20 s /
  timeout 40 s.
- FIFO pump: dedicated blocking-read thread (raw `os.read` — never a
  buffered reader, which blocks mid-stream and strands the tail) →
  bounded asyncio bridge (≈8 MiB). Overflow ⇒ `continuity_broken`:
  drop queued bytes, mark shadow stale, resync clients from
  `capture-pane` + last-known modes, raise alert. tmux itself
  flow-controls the pipe (proven lossless), so this is defense in
  depth for broker-side stalls only.

## Fault model

- Pipe consumer stall ⇒ tmux flow-controls the pane pty (proven
  lossless); the pump still never blocks on client I/O; bridge
  overflow ⇒ continuity_broken + resync + alert.
- Broker crash ⇒ writer revoked, wake disarmed, composer `unknown`,
  unacknowledged human frame `delivery_unknown`, never replayed.
- Slow/dead client ⇒ connection closed; generation unaffected.
- Stale generation / revoked lease / duplicate seq / gap / malformed
  frame ⇒ rejected before any byte reaches tmux.
