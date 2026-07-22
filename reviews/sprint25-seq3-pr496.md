# Review — sprint 25 seq 3 · PR #496 (feat/interface-stream-spike)

Reviewer: REV1 (Kimi K3 boot, re-issued task — supersedes any earlier seq-2 notes)
Spec: doc #20 task #79 · Sprint doc: 25 · Dev: DEV3
Verdict: **GATE PASS — review-clean.** No Major, no Medium. Build may open seq 4.
CI at review: 6/6 green (tests, verify, render-check, CodeQL, Analyze ×2), mergeable.

This was the hard gate: spec #20 stops the build for rescope on ANY silent loss,
duplicate, bypass, or interleaving. I reviewed the evidence, not just the code,
and re-ran the full matrix independently (`./sc job` 30-rev1-pr496-proofs-r2,
exit 0, 12/12 passed in 55 s).

## 1. Ordering fence (human vs wake) — HOLDS

Re-run evidence (proofs-20260722-194534.log): 210 iterations, 424 human frames
accepted with globally-unique markers, human multiset exact; 121 wakes (20
submitted / 101 cancelled); every submitted wake prompt contiguous in the
received stream; zero quiet-gate violations; 58 duplicate-seq ack replays, no
double-forward. Code trace confirms the mechanism, not just the claim: one
asyncio queue + one consumer per generation; a wake is a single indivisible
item (one `send-keys` call, no interleave possible inside it); a human frame
ordered first sets composer `dirty` before its bytes forward, so a later-dequeued
`WakeSubmit` fails gate revalidation and cancels without sending a byte.

Could not construct a silent-loss/interleave case the spike missed. The
design's weak points are declared, not hidden: pump→loop bridge overflow is
`continuity_broken` → resync (declared, alerted — not silent); crash window is
`delivery_unknown`, never replayed (see §3).

Low: RESULTS.md/task wording "210 iterations × 3 seeds" overstates — it is 210
races with the RNG re-seeded per iteration from 3 base seeds
(`SEEDS[it % 3] * 100003 + it`). The 210 races are real; there is no 630-run
matrix. Fix the wording in the final record.

## 2. Gate-caught defects — genuinely FIXED, not masked

**Sans-io pong flush (clients ping-timing-out ~40 s):** verified in
`server.py` `_ws_read_loop` — `data_to_send()` is flushed to the transport
immediately after every `receive_data()`, before event handling, so the sans-io
layer's automatic pong replies actually reach the wire. Second layer: the
writer loop refreshes liveness only after a *successful* `drain()`, and
keepalive closes at 40 s of true idleness. Adversarial check: a dead client
being flooded cannot survive on drain-refresh alone — when its OS buffers fill,
`drain()` blocks, the 2 MiB outbound bound trips, and the broker closes it
1011 (re-run evidence: slow client close code=1011, healthy client got all
5,065,000 bytes sha256-exact in 0.2 s). Bounded on both sides; not masked.

**Viewer-role wake hole:** verified in `server.py` — `{"type":"wake"}` from a
non-writer role is rejected `wake_requires_writer` before any enqueue; writer
transfer test re-ran clean (viewer input rejected `viewer_read_only`, takeover
revokes, dup seq → ack replay, pane file exactly `AC`). Declared spike-scoped:
production moves wake submission to the coordinator/API per spec — ratified
ambiguity call, matches spec §Hooks.

## 3. Ruling — crash-window / delivery_unknown parking deferred to seq 4

Spec task #79 literally says "Prove that every crash window parks unknown
without replay", so the deferral is a **deviation from spec text and must be
ratified as such** — not silently absorbed. My ruling: **acceptable for the
gate**, on two grounds:

1. Parking is durable state — the pending-input/generation/idempotency schema
   is seq 2's deliverable by the spec's own delivery plan. A spike-scale
   "parking proof" would be in-memory theatre; it cannot prove the real thing.
2. The gate criterion is *silent* loss/dup/bypass/interleave, and the crash
   window produces none at transport level: ack-after-forward means an acked
   frame is provably delivered; an unacked frame is visibly unacked (declared
   unknown, not silent); the never-replay rule plus the tested dup-ack-replay
   path (58 replays, no double-forward) closes the retry-duplicate hole.

**Condition (hard):** seq 4 must implement and prove crash-window parking
against the durable schema before any wake/retry mechanism ships. Planner:
record this as a ratified deviation so the conformance pass doesn't re-flag it
as silent.

## 4. Pinned stack — maintained + license-clean

- `websockets` 16.1.1 — BSD-3-Clause (dist metadata verified); latest patch,
  released 2026-07-17. Maintained.
- `@xterm/xterm` + `@xterm/headless` 6.0.0 — MIT (package.json verified);
  6.0.0 is the current npm release line. Maintained.
- tmux 3.5a — ISC.

Low (license hygiene): no LICENSE text file accompanies the vendored
`static/vendor/xterm/` assets or the `@xterm/headless` package dir. MIT
requires notice retention with copies — production vendoring must include the
license files. Not a gate issue for a spike.

## 5. Additional Lows (sprint report, non-blocking)

- `run_proofs.sh` hardcodes DEV3's venv path
  (`/home/j3d1/super-coder/.sc-worktrees/dev3/.venv/bin/python`) — not
  portable; my re-run only worked because that venv exists on this host. Seq 4
  needs a real requirements/lockfile.
- Reconnect test tolerates a one-cell live-scroll skew vs capture-pane
  (documented, intentional: capture is authoritative mid-scroll). Acceptable;
  noted so it isn't mistaken for a false green later.
- Ambiguity call ratified: reconnect redraw sourced from the volatile
  `@xterm/headless` shadow (tmux cannot report modes), capture-pane as
  cross-check/fallback — matches spec intent and is proven grid-identical in
  the matrix.

## Gate statement

Input: exact, ordered, fenced, deduplicated — independently re-run. Output:
exact incl. 5 MB burst + slow consumer. Ordering: no interleave/loss/dup in
210 races; fixes verified in code, not masked. Deferral ruled acceptable with
the seq-4 condition above. **The feature build opens.**
