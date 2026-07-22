"""test_bounded_buffers:
(a) a client that never reads its socket is closed (1011) while the pump
    keeps draining a 5 MB burst; other clients and the generation unaffected;
    no continuity_broken; broker RSS stays bounded;
(b) an input frame with >64 KiB payload is rejected, no bytes forwarded.
"""
import os
import socket
import time

from helpers import PY, PROGRAMS, sha


def rss_mb() -> float:
    with open("/proc/self/status") as fh:
        for line in fh:
            if line.startswith("VmRSS"):
                return int(line.split()[1]) / 1024.0
    return -1.0


def dead_client(port: int, sid: str, ticket: str) -> socket.socket:
    """Hand-rolled upgrade that never reads after the 101."""
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    key = "MDEyMzQ1Njc4OWFiY2RlZg=="  # base64 of 16 bytes
    s.sendall((f"GET /api/interface/session-streams/{sid}?ticket={ticket} HTTP/1.1\r\n"
               f"Host: 127.0.0.1:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
               f"Sec-WebSocket-Protocol: sc-term.v1\r\n"
               f"Origin: http://127.0.0.1:{port}\r\n\r\n").encode())
    head = b""
    while b"\r\n\r\n" not in head:
        head += s.recv(4096)
    assert b"101" in head.split(b"\r\n")[0], head[:100]
    return s  # never read again


def test_bounded_buffers(spike, make_session, writer, ws_factory, tmp_path):
    corpus = (b"SLOWCONSUMER-" + b"x" * 1000) * 5000  # ~5.06 MB
    cfile = tmp_path / "corpus.bin"
    cfile.write_bytes(corpus)
    go = tmp_path / "go"
    sess = make_session(command=f"{PY} {PROGRAMS}/emitter.py {cfile} {go}",
                        worktree=str(tmp_path))
    sid = sess["session_id"]

    st, tick = spike.api("POST", "/api/interface/stream-tickets",
                         {"session_id": sid, "role": "viewer"})
    assert st == 201
    slow = dead_client(spike.port, sid, tick["ticket"])
    good = ws_factory(sid)

    rss0 = rss_mb()
    t0 = time.monotonic()
    go.touch()
    try:
        out = good.wait_len(len(corpus), timeout=120)
    except TimeoutError:
        st, info = spike.api("GET", f"/api/interface/sessions/{sid}")
        raise TimeoutError(
            f"good client stalled at {len(good.output())}/{len(corpus)}; "
            f"session dbg={info.get('dbg')}")
    dt = time.monotonic() - t0
    rss1 = rss_mb()
    print(f"\nevidence (a): good client received {len(out)} bytes "
          f"sha256={sha(out[:len(corpus)])} (corpus {len(corpus)} sha256={sha(corpus)}) "
          f"in {dt:.1f}s; RSS {rss0:.0f} -> {rss1:.0f} MB")
    assert out == corpus, "good client stream corrupted"

    # slow client closed with 1011, generation unaffected
    slow.settimeout(30)
    code = None
    try:
        deadline = time.monotonic() + 30
        buf = b""
        while time.monotonic() < deadline:
            data = slow.recv(65536)
            if not data:
                break
            buf += data
            # scan for close frame with code 1011 (0x03 0xf3)
            if b"\x03\xf3" in buf:
                code = 1011
                break
    except (socket.timeout, ConnectionResetError):
        pass
    st, info = spike.api("GET", f"/api/interface/sessions/{sid}")
    print(f"evidence (a): slow client close code={code}; "
          f"continuity_broken={info['continuity_broken']}; clients={info['clients']}")
    assert code == 1011, "slow client not closed with 1011"
    assert not info["continuity_broken"], "continuity_broken raised — pump stalled"
    rss2 = rss_mb()
    print(f"evidence (c): RSS after slow-client close: {rss2:.0f} MB "
          f"(delta from start {rss2 - rss0:+.0f} MB)")
    assert rss2 - rss0 < 150, f"RSS grew unboundedly: {rss0:.0f} -> {rss2:.0f} MB"
    slow.close()

    # (b) oversized input frame rejected, no bytes forwarded
    out2 = tmp_path / "received.bin"
    sess2 = make_session(
        command=f"{PY} {PROGRAMS}/reader.py {out2} 999999",
        worktree=str(tmp_path))
    from helpers import wait_file
    wait_file(str(out2) + ".ready")
    w, _lease = writer(sess2["session_id"])
    w.send_input(1, b"y" * (70 * 1024))
    rej = w.control(lambda m: m.get("type") == "input_reject" and m.get("seq") == 1)
    time.sleep(0.5)
    received = open(out2, "rb").read() if out2.exists() else b""
    print(f"evidence (b): 70 KiB frame reject reason={rej['reason']}; "
          f"pane received {len(received)} bytes")
    assert rej["reason"] == "payload_too_large"
    assert received == b"", "bytes forwarded from oversized frame — gate failure"
    good.close()
    w.close()
