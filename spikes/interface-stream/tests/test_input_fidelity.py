"""test_input_fidelity: WS -> broker -> tmux send-keys -H -> raw pane reader.

The pane runs a raw-mode byte reader writing to a file; the client pushes the
corpus (all bytes 0x00-0xFF, UTF-8, a bracketed-paste frame, a 114 KiB
pattern) in mixed-size frames. The received file must equal the corpus
exactly — any lost, duplicated, or reordered byte fails the hard gate.
"""
import hashlib
import os
import time

from helpers import PY, PROGRAMS, build_corpus, sha, wait_file


def test_input_fidelity(spike, make_session, writer, tmp_path):
    corpus = build_corpus()
    out = tmp_path / "received.bin"
    sess = make_session(
        command=f"{PY} {PROGRAMS}/reader.py {out} {len(corpus)}",
        worktree=str(tmp_path))
    wait_file(str(out) + ".ready")
    client, _lease = writer(sess["session_id"])
    client.wait_redraw()  # attached, initial snapshot received

    # mixed-size frames: 1, 3, 17, 512, 4096 cycling
    sizes = [1, 3, 17, 512, 4096, 100, 7]
    seq, off, frames = 1, 0, 0
    t0 = time.monotonic()
    while off < len(corpus):
        n = min(sizes[frames % len(sizes)], len(corpus) - off)
        client.send_input(seq, corpus[off:off + n])
        seq += 1
        off += n
        frames += 1
        if frames % 50 == 0:  # let the ack stream keep up
            client.control(lambda m, s=seq: m.get("type") == "input_ack"
                           and m.get("seq", 0) >= s - 1, timeout=30)

    last_ack = client.control(lambda m: m.get("type") == "input_ack"
                              and m.get("seq") == seq - 1, timeout=60)
    assert not last_ack.get("replayed")

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if os.path.exists(out) and os.path.getsize(out) >= len(corpus):
            break
        time.sleep(0.05)
    received = open(out, "rb").read()
    dt = time.monotonic() - t0
    print(f"\nevidence: corpus={len(corpus)} bytes in {frames} frames, "
          f"sha256={sha(corpus)}, received={len(received)} bytes "
          f"sha256={sha(received)}, wall={dt:.2f}s")
    assert len(received) == len(corpus), f"length {len(received)} != {len(corpus)}"
    assert received == corpus, "byte mismatch — hard gate failure"
    client.close()
