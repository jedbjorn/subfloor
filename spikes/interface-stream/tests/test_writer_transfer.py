"""test_writer_transfer: viewer read-only, writer takeover, revoked-token
rejection, duplicate-seq ack replay with exactly-once forwarding."""
import os
import time

from helpers import PY, PROGRAMS, wait_file


def test_writer_transfer(spike, make_session, writer, ws_factory, tmp_path):
    out = tmp_path / "received.bin"
    sess = make_session(
        command=f"{PY} {PROGRAMS}/reader.py {out} 999999",
        worktree=str(tmp_path))
    sid = sess["session_id"]
    wait_file(str(out) + ".ready")

    w1, lease1 = writer(sid)
    viewer = ws_factory(sid, role="viewer")

    # viewer input is rejected and forwards nothing
    viewer.send_input(1, b"V")
    rej = viewer.control(lambda m: m.get("type") == "input_reject" and m.get("seq") == 1)
    assert rej["reason"] == "viewer_read_only", rej

    # W1 active: input lands
    w1.send_input(1, b"A")
    w1.control(lambda m: m.get("type") == "input_ack" and m.get("seq") == 1)

    # takeover by W2 atomically revokes W1's lease
    w2, lease2 = writer(sid, takeover=True)
    assert lease2["lease_id"] != lease1["lease_id"]
    revoked = w1.control(lambda m: m.get("type") == "writer" and m.get("state") == "revoked")
    assert revoked

    # W1's further frames are rejected (writer_revoked), no bytes forwarded
    w1.send_input(2, b"B")
    rej = w1.control(lambda m: m.get("type") == "input_reject" and m.get("seq") == 2)
    assert rej["reason"] == "writer_revoked", rej

    # W2's input lands exactly once; duplicate seq replays the ack
    w2.send_input(1, b"C")
    w2.control(lambda m: m.get("type") == "input_ack" and m.get("seq") == 1)
    w2.send_input(1, b"C")  # duplicate
    dup = w2.control(lambda m: m.get("type") == "input_ack" and m.get("seq") == 1
                     and m.get("replayed"), timeout=10)
    assert dup["replayed"]

    time.sleep(1.0)
    received = open(out, "rb").read() if out.exists() else b""
    print(f"\nevidence: viewer reject=viewer_read_only; W1 seq2 reject=writer_revoked; "
          f"W2 dup seq1 -> ack replay; received file={received!r}")
    assert received == b"AC", f"expected exactly b'AC', got {received!r} — gate failure"
    viewer.close()
    w1.close()
    w2.close()
