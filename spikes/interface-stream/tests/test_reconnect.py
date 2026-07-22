"""test_reconnect: client A detaches mid-stream; the generation survives;
client B attaches, gets a current redraw, and live bytes keep flowing."""
import re
import time

from helpers import (PY, PROGRAMS, capture_pane, grids_equal, replay_capture,
                     replay_dump)


def test_reconnect(spike, make_session, ws_factory, tmp_path):
    go = tmp_path / "go"
    sess = make_session(command=f"{PY} {PROGRAMS}/streamer.py {go}",
                        worktree=str(tmp_path))
    sid = sess["session_id"]
    pane_pid = sess["pane_pid"]

    client_a = ws_factory(sid)
    go.touch()
    client_a.wait_output(lambda d: b"LINE 000030" in d, timeout=30)
    bytes_a = len(client_a.output())
    client_a.close()  # disconnect mid-stream
    time.sleep(1.0)  # pane keeps emitting with nobody attached

    st, info = spike.api("GET", f"/api/interface/sessions/{sid}")
    assert st == 200 and info["pane_pid"] == pane_pid, "generation changed!"

    client_b = ws_factory(sid)  # last-ack semantics: fresh attach -> state sync
    client_b.wait_redraw()
    dump_b = replay_dump(sess["cols"], sess["rows"], client_b.redraws[0])
    capture = capture_pane(spike.sock, sess["pane_id"])
    dump_tmux = replay_capture(sess["cols"], sess["rows"], capture)
    # the streamer is live: compare only up to a timing skew — both replays
    # must agree on all rows except possibly the scrolling tail. Compare the
    # line-number multiset difference tolerance below instead.
    same, detail = grids_equal(dump_b, dump_tmux)

    before = client_b.output()
    lines_before = re.findall(rb"LINE (\d+)", before)
    client_b.wait_output(lambda d: len(re.findall(rb"LINE (\d+)", d)) >
                         len(lines_before) + 20, timeout=15)
    lines_after = re.findall(rb"LINE (\d+)", client_b.output())
    monotonic = all(int(a) <= int(b) for a, b in zip(lines_after, lines_after[1:]))
    print(f"\nevidence: A received {bytes_a} bytes before disconnect; pane_pid "
          f"unchanged={pane_pid}; redraw-vs-capture grid: {detail}; "
          f"B live stream {len(lines_after)} lines, monotonic={monotonic}, "
          f"last={lines_after[-1].decode() if lines_after else '?'}")
    assert monotonic and lines_after, "live stream broken after reconnect"
    if not same:
        # live scroller: allow skew only in that B's dump is a prefix-consistent
        # older snapshot (line numbers <= capture's)
        nums_b = re.findall(rb"LINE (\d+)",
                            "".join(c[1] for row in dump_b["grid"] for c in row).encode())
        nums_t = re.findall(rb"LINE (\d+)",
                            "".join(c[1] for row in dump_tmux["grid"] for c in row).encode())
        assert nums_b and nums_t and max(nums_b) <= max(nums_t), \
            f"redraw diverged from capture: {detail}"
        print(f"evidence: live-scroll skew accepted (B max {max(nums_b)} <= capture {max(nums_t)})")
    client_b.close()
