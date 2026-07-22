"""test_ordering: deterministic-random race harness (>=200 iterations across
seeds) around the wake quiet-window boundary.

The pane is a raw byte reader (with echo so the simulated composer returns
to clean). A writer pushes human frames with globally unique marker payloads
plus wake submissions (fixed prompt b"WAKEPROMPT\\n") with sleeps straddling
the quiet window. On the received byte file we assert:
  (i)   every wake prompt occurrence is contiguous, never interleaved with
        human bytes (the whole file parses into wake|human segments);
  (ii)  timing consistency: submitted wakes had no accepted human input
        within the quiet window; quiet-cancelled wakes really were inside it;
  (iii) no human byte lost or duplicated (per-payload multiset equality);
  (iv)  duplicate-seq frames are ack-replayed, never double-forwarded.
"""
import random
import time
from collections import Counter

from helpers import PY, PROGRAMS, wait_file

WAKE = b"WAKEPROMPT\n"
QUIET_MS = 250
ITERATIONS = 210
SEEDS = (1234, 5678, 999)


def marker(fc: int) -> bytes:
    """Globally unique per frame counter: distinct byte + length combos."""
    byte = 0x80 + (fc % 112)
    length = 8 + (fc // 112) % 12
    return bytes([byte]) * length


def test_ordering(spike, make_session, writer, tmp_path):
    out = tmp_path / "received.bin"
    sess = make_session(
        command=f"{PY} {PROGRAMS}/reader.py {out} 999999999 --echo",
        worktree=str(tmp_path), quiet_ms=QUIET_MS, idle_quiet_ms=150,
        wake_prompt=WAKE.decode())
    sid = sess["session_id"]
    wait_file(str(out) + ".ready")
    w, _lease = writer(sid)

    # drive the lifecycle to idle: one human input -> echo -> output quiet
    w.send_input(1, marker(0))
    w.control(lambda m: m.get("type") == "input_ack" and m.get("seq") == 1, timeout=10)
    w.control(lambda m: m.get("type") == "lifecycle" and m.get("state") == "idle",
              timeout=10)

    seq = 2
    fc = 1
    accepted: Counter = Counter()
    accepted[marker(0)] += 1
    wakes = []           # (state, reason, dt_since_last_ack)
    dup_replays = 0
    rng = random.Random(0)
    last_ack_ts = time.monotonic()

    for it in range(ITERATIONS):
        rng.seed(SEEDS[it % len(SEEDS)] * 100003 + it)
        for _ in range(rng.randint(1, 3)):
            payload = marker(fc)
            fc += 1
            w.send_input(seq, payload)
            w.control(lambda m, s=seq: m.get("type") == "input_ack"
                      and m.get("seq") == s, timeout=30)
            last_ack_ts = time.monotonic()
            accepted[payload] += 1
            seq += 1
            if rng.random() < 0.15:  # duplicate-seq probe
                w.send_input(seq - 1, payload)
                w.control(lambda m, s=seq - 1: m.get("type") == "input_ack"
                          and m.get("seq") == s and m.get("replayed"), timeout=10)
                dup_replays += 1
        if rng.random() < 0.6:
            time.sleep(rng.choice([0.0, 0.05, QUIET_MS / 1000 * 0.6,
                                   QUIET_MS / 1000 * 1.4, 0.02, 0.08]))
            dt = time.monotonic() - last_ack_ts
            w.send_wake()
            resp = w.control(lambda m: m.get("type") == "wake", timeout=10)
            wakes.append((resp["state"], resp.get("reason", ""), dt))

    time.sleep(1.0)
    received = open(out, "rb").read()

    # (i) parse the stream into wake | human segments
    pos, wake_count, human_seen = 0, 0, Counter()
    while pos < len(received):
        if received.startswith(WAKE, pos):
            wake_count += 1
            pos += len(WAKE)
            continue
        byte = received[pos]
        end = pos
        while end < len(received) and received[end] == byte:
            end += 1
        payload = received[pos:end]
        assert 0x80 <= byte < 0xF0 and accepted.get(payload, 0) > 0, (
            f"unparsable segment at {pos}: {payload[:16]!r} — interleaving or "
            f"corruption, hard gate failure")
        human_seen[payload] += 1
        pos = end
    assert pos == len(received)

    submitted = sum(1 for st, _, _ in wakes if st == "submitted")
    cancelled = len(wakes) - submitted
    # (iii) multiset equality: every accepted human payload exactly once
    assert human_seen == accepted, (
        f"lost/duplicated human bytes: {accepted - human_seen} missing, "
        f"{human_seen - accepted} extra — hard gate failure")
    # (i) count: every submitted wake appears exactly once, contiguous (parse)
    assert wake_count == submitted, (
        f"wake prompt count {wake_count} != submitted wakes {submitted} — "
        f"hard gate failure")
    # (ii) timing consistency (client-side evidence, boundary slack 50ms)
    violations = []
    for state, reason, dt in wakes:
        if state == "submitted" and dt < (QUIET_MS / 1000 - 0.15):
            violations.append(f"submitted with dt={dt:.3f}s")
        if state == "cancelled" and reason == "quiet_window" and dt > (QUIET_MS / 1000 + 0.15):
            violations.append(f"quiet-cancelled with dt={dt:.3f}s")
    assert not violations, f"quiet-gate violations: {violations}"

    print(f"\nevidence: {ITERATIONS} iterations, {sum(accepted.values())} human "
          f"frames accepted ({fc} markers), {len(wakes)} wakes "
          f"({submitted} submitted / {cancelled} cancelled), "
          f"{dup_replays} duplicate-seq replays; received={len(received)} bytes; "
          f"parse ok: every wake prompt contiguous, human multiset exact, "
          f"no quiet-gate violations")
    w.close()
