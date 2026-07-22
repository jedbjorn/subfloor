"""test_output_fidelity: pane -> tmux pipe-pane -> FIFO pump -> WS client.

(a) The pane (raw tty) emits the full corpus; the client must receive every
    byte exactly, contiguous and in order.
(b) A colored curses-like screen: the shadow snapshot replayed through a
    fresh @xterm/headless terminal must match tmux capture-pane -epN replayed
    through an identical fresh terminal, cell by cell.
"""
import time

from helpers import (PY, PROGRAMS, build_corpus, capture_pane, grids_equal,
                     replay_capture, replay_dump, sha)


def test_output_fidelity_bytes(spike, make_session, ws_factory, tmp_path):
    corpus = build_corpus()
    cfile = tmp_path / "corpus.bin"
    cfile.write_bytes(corpus)
    go = tmp_path / "go"
    sess = make_session(
        command=f"{PY} {PROGRAMS}/emitter.py {cfile} {go}",
        worktree=str(tmp_path))
    client = ws_factory(sess["session_id"])
    client.wait_redraw()
    t0 = time.monotonic()
    go.touch()
    out = client.wait_output(lambda d: len(d) >= len(corpus), timeout=60)
    dt = time.monotonic() - t0
    print(f"\nevidence: corpus={len(corpus)} bytes sha256={sha(corpus)}, "
          f"streamed={len(out)} bytes sha256={sha(out[:len(corpus)])}, wall={dt:.2f}s")
    assert len(out) == len(corpus), f"received {len(out)} bytes, expected {len(corpus)}"
    assert out == corpus, "stream not byte-exact — hard gate failure"
    client.close()


def test_output_fidelity_shadow_vs_capture(spike, make_session, ws_factory, tmp_path):
    go = tmp_path / "go"
    sess = make_session(command=f"{PY} {PROGRAMS}/screen.py {go}",
                        worktree=str(tmp_path))
    client = ws_factory(sess["session_id"])
    go.touch()
    client.wait_output(lambda d: b"SCREEN-DONE" in d, timeout=30)
    time.sleep(0.5)  # settle: shadow feed ordering guaranteed, capture needs quiet

    fresh = ws_factory(sess["session_id"])  # attach -> shadow snapshot redraw
    fresh.wait_redraw()
    redraw = fresh.redraws[0]
    capture = capture_pane(spike.sock, sess["pane_id"])

    cols, rows = sess["cols"], sess["rows"]
    dump_shadow = replay_dump(cols, rows, redraw)
    dump_capture = replay_capture(cols, rows, capture)
    same, detail = grids_equal(dump_shadow, dump_capture)
    print(f"\nevidence: shadow redraw={len(redraw)} bytes, capture={len(capture)} bytes, "
          f"grid compare: {detail}")
    assert same, f"shadow snapshot != tmux capture replay: {detail}"
    client.close()
    fresh.close()
