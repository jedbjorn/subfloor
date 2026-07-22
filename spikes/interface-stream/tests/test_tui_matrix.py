"""test_tui_matrix: each real harness (claude, codex, kimi) booted through
the broker in a tmp worktree.

Per harness: non-trivial boot byte stream; shadow snapshot screen text
matches tmux capture-pane text (trailing whitespace normalized); a typed
ASCII string reaches the real TUI composer (visible in capture-pane); a
resize makes the TUI redraw at the new geometry. No prompt is ever
submitted (no Enter at the composer). A harness that cannot boot or
authenticate in this sandbox is xfailed with its captured evidence.
"""
import time

import pytest

from helpers import capture_text, pane_fmt, replay_capture, replay_dump

SETTLE_POLLS = 3


def norm_text(dump: dict) -> list[str]:
    return ["".join(c[1] for c in row).rstrip() for row in dump["grid"]]


def boot_and_settle(spike, sess, w, v, timeout=120):
    """Wait for boot bytes; accept trust prompts; wait for a stable capture."""
    v.wait_output(lambda d: len(d) > 200, timeout=timeout)
    seq = 0
    last_text, stable = None, 0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = capture_text(spike.sock, sess["pane_id"])
        low = text.lower()
        if "trust" in low and ("do you trust" in low or "trust the files" in low
                               or "yes, i trust" in low or "trusting" in low):
            seq += 1
            w.send_input(seq, b"\r")
            w.control(lambda m, s=seq: m.get("type") == "input_ack"
                      and m.get("seq") == s, timeout=10)
            time.sleep(3)
            continue
        if text == last_text and text.strip():
            stable += 1
            if stable >= SETTLE_POLLS:
                return seq, text
        else:
            stable = 0
            last_text = text
        time.sleep(1.5)
    raise TimeoutError("harness UI did not settle")


@pytest.mark.parametrize("harness", ["claude", "codex", "kimi"])
def test_tui_matrix(spike, make_session, writer, ws_factory, tmp_path, harness):
    sess = make_session(harness=harness, worktree=str(tmp_path), cols=100, rows=30)
    sid = sess["session_id"]
    evidence = ""
    try:
        w, _lease = writer(sid)
        v = ws_factory(sid)
        enter_seq, settled_text = boot_and_settle(spike, sess, w, v)
        boot_bytes = len(v.output())
        evidence += f"boot_bytes={boot_bytes} enter_presses={enter_seq}\n"
        assert boot_bytes > 200, "boot stream trivial"

        # shadow snapshot text vs capture-pane text
        fresh = ws_factory(sid)
        fresh.wait_redraw()
        dump_shadow = replay_dump(100, 30, fresh.redraws[0])
        from helpers import capture_pane
        dump_tmux = replay_capture(100, 30, capture_pane(spike.sock, sess["pane_id"]))
        t_shadow, t_tmux = norm_text(dump_shadow), norm_text(dump_tmux)
        text_match = t_shadow == t_tmux
        evidence += f"snapshot-text-match={text_match}\n"
        if not text_match:
            diff = [(i, a, b) for i, (a, b) in enumerate(zip(t_shadow, t_tmux)) if a != b][:5]
            evidence += f"first diffs: {diff}\n"
        assert text_match, "shadow snapshot text != capture-pane text"

        # typing reaches the real TUI composer (seq continues from Enter presses)
        type_seq = enter_seq + 1
        w.send_input(type_seq, b"spiketest")
        w.control(lambda m, s=type_seq: m.get("type") == "input_ack" and m.get("seq") == s,
                  timeout=10)
        deadline = time.monotonic() + 20
        typed = ""
        while time.monotonic() < deadline:
            typed = capture_text(spike.sock, sess["pane_id"])
            if "spiketest" in typed:
                break
            time.sleep(0.5)
        evidence += f"typed-visible={'spiketest' in typed}\n"
        assert "spiketest" in typed, "typed string did not reach the TUI"

        # resize: TUI redraws to the new geometry
        before = capture_text(spike.sock, sess["pane_id"])
        w.send_resize(40, 120)
        deadline = time.monotonic() + 20
        width = ""
        while time.monotonic() < deadline:
            width = pane_fmt(spike.sock, sess["pane_id"], "#{window_width}")
            after = capture_text(spike.sock, sess["pane_id"])
            if width == "120" and after != before:
                break
            time.sleep(0.5)
        evidence += f"resize width={width} redraw={after != before}\n"
        assert width == "120", "window did not resize"
        assert after != before, "TUI did not redraw after resize"

        # clean shutdown through the termination API
        st, _ = spike.api("POST", "/api/interface/termination-requests",
                          {"session_id": sid})
        spike.created.remove(sid)
        assert st == 202
        assert w.closed.wait(10), "server did not close clients on termination"
        evidence += "terminated-cleanly=True\n"
        print(f"\nevidence ({harness}):\n{evidence}")
        v.close()
        fresh.close()
    except AssertionError:
        raise  # gate failure is a failure, never an xfail
    except Exception as exc:
        try:
            evidence += "last capture:\n" + capture_text(spike.sock, sess["pane_id"])
        except Exception:
            pass
        pytest.xfail(f"{harness} failed in this sandbox: {exc!r}\n{evidence}")
