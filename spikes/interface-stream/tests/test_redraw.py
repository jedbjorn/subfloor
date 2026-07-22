"""test_redraw: a fresh client attaching to a live alt-screen TUI receives a
redraw that reproduces grid, cursor, alt screen and private modes."""
import time

from helpers import (PY, PROGRAMS, capture_pane, grids_equal, pane_fmt,
                     replay_capture, replay_dump)


def test_redraw(spike, make_session, ws_factory, tmp_path):
    go = tmp_path / "go"
    sess = make_session(command=f"{PY} {PROGRAMS}/tui.py {go}",
                        worktree=str(tmp_path), cols=80, rows=24)
    sid = sess["session_id"]
    client_a = ws_factory(sid)
    go.touch()
    client_a.wait_output(lambda d: b"count=3" in d, timeout=30)
    time.sleep(0.5)  # cursor parked, screen quiet

    client_b = ws_factory(sid)  # fresh attach -> redraw snapshot
    client_b.wait_redraw()
    redraw = client_b.redraws[0]

    # private modes re-issued in the redraw byte string
    for seq, name in [(b"\x1b[?1049h", "alt screen"), (b"\x1b[?1000h", "mouse vt200"),
                      (b"\x1b[?1006h", "sgr mouse"), (b"\x1b[?25l", "cursor hidden")]:
        assert seq in redraw, f"redraw missing {name} re-issue ({seq!r})"

    cols, rows = sess["cols"], sess["rows"]
    dump_b = replay_dump(cols, rows, redraw)
    capture = capture_pane(spike.sock, sess["pane_id"])
    dump_tmux = replay_capture(cols, rows, capture)
    same, detail = grids_equal(dump_b, dump_tmux)

    cursor_x, cursor_y, alt = pane_fmt(
        spike.sock, sess["pane_id"], "#{cursor_x} #{cursor_y} #{alternate_on}").split()
    print(f"\nevidence: redraw={len(redraw)} bytes; modes re-issued ok; "
          f"grid compare: {detail}; replay cursor={dump_b['cursor']} vs "
          f"tmux=({cursor_x},{cursor_y}); replay alt={dump_b['alt']} vs tmux={alt}")
    assert same, f"redraw grid != tmux capture: {detail}"
    assert dump_b["cursor"] == [int(cursor_x), int(cursor_y)], "cursor mismatch"
    assert dump_b["alt"] == (alt == "1"), "alt-screen state mismatch"
    assert dump_b["modes"]["mouse"] == "vt200" and dump_b["modes"]["sgrMouse"]
    client_a.close()
    client_b.close()
