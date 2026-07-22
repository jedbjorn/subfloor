"""test_resize: resize frames deliver SIGWINCH to the pane and the stream /
shadow follow the new geometry."""
import time

from helpers import (PY, PROGRAMS, capture_pane, grids_equal, pane_fmt,
                     replay_capture, replay_dump, wait_file)


def test_resize(spike, make_session, ws_factory, tmp_path):
    logf = tmp_path / "winch.log"
    sess = make_session(command=f"{PY} {PROGRAMS}/winch.py {logf}",
                        worktree=str(tmp_path), cols=80, rows=24)
    sid = sess["session_id"]
    wait_file(str(logf) + ".ready")
    client = ws_factory(sid)

    client.send_resize(40, 132)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and "132x40" not in (
            open(logf).read() if logf.exists() else ""):
        time.sleep(0.05)
    client.send_resize(33, 100)
    deadline = time.monotonic() + 15
    seen = ""
    while time.monotonic() < deadline:
        seen = open(logf).read() if logf.exists() else ""
        if "100x33" in seen:
            break
        time.sleep(0.05)
    print(f"\nevidence: SIGWINCH log: {seen.split()}")
    assert "132x40" in seen and "100x33" in seen, f"pane missed resize: {seen!r}"

    time.sleep(0.5)
    width = pane_fmt(spike.sock, sess["pane_id"], "#{window_width}")
    height = pane_fmt(spike.sock, sess["pane_id"], "#{window_height}")
    capture = capture_pane(spike.sock, sess["pane_id"])
    first_line = capture.split(b"\n")[0]
    print(f"evidence: window now {width}x{height}; capture line width {len(first_line)}")

    # shadow followed the resize: fresh attach redraw replays at new geometry
    fresh = ws_factory(sid)
    fresh.wait_redraw()
    dump_shadow = replay_dump(100, 33, fresh.redraws[0])
    dump_tmux = replay_capture(100, 33, capture)
    same, detail = grids_equal(dump_shadow, dump_tmux)
    print(f"evidence: post-resize shadow-vs-capture grid compare: {detail}")
    assert (width, height) == ("100", "33")
    assert same, f"post-resize shadow != capture: {detail}"
    client.close()
    fresh.close()
