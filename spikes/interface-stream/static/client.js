/* interface-stream spike browser client: xterm.js <-> sc-term.v1 WebSocket. */
(function () {
  const $ = (id) => document.getElementById(id);
  const statusEl = $("status");
  let ws = null, seq = 1, hbTimer = null;
  const state = { writer: "?", lifecycle: "?", composer: "?", wake: "?" };

  const term = new Terminal({
    cols: Number($("cols").value), rows: Number($("rows").value),
    fontFamily: "monospace", fontSize: 15,
  });
  term.open($("term"));

  function show() {
    statusEl.textContent =
      `writer=${state.writer} lifecycle=${state.lifecycle} composer=${state.composer} wake=${state.wake}`;
  }
  show();

  async function api(path, body) {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Authorization": "Bearer " + $("token").value,
                 "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(path + " -> " + r.status + " " + JSON.stringify(j));
    return j;
  }

  $("leasebtn").onclick = async () => {
    try {
      const j = await api("/api/interface/writer-leases",
        { session_id: $("sid").value, takeover: true });
      $("lease").value = j.lease_token;
    } catch (e) { state.writer = String(e); show(); }
  };

  function sendInput(text) {
    if (!ws || ws.readyState !== 1) return;
    const payload = new TextEncoder().encode(text);
    const frame = new Uint8Array(9 + payload.length);
    frame[0] = 0x01;
    new DataView(frame.buffer).setBigUint64(1, BigInt(seq++));
    frame.set(payload, 9);
    ws.send(frame);
  }

  function sendResize() {
    if (!ws || ws.readyState !== 1) return;
    const frame = new Uint8Array(5);
    frame[0] = 0x03;
    new DataView(frame.buffer).setUint16(1, term.rows);
    new DataView(frame.buffer).setUint16(3, term.cols);
    ws.send(frame);
  }

  $("connect").onclick = async () => {
    const role = $("role").value;
    const body = { session_id: $("sid").value, role };
    if (role === "writer") body.lease_token = $("lease").value;
    let ticket;
    try {
      ticket = (await api("/api/interface/stream-tickets", body)).ticket;
    } catch (e) { state.writer = String(e); show(); return; }
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/api/interface/session-streams/` +
                       `${$("sid").value}?ticket=${ticket}`, ["sc-term.v1"]);
    ws.binaryType = "arraybuffer";
    ws.onmessage = (ev) => {
      if (typeof ev.data === "string") {
        const m = JSON.parse(ev.data);
        if (m.type === "writer") state.writer = m.state;
        else if (m.type === "lifecycle") { state.lifecycle = m.state; state.composer = m.composer; }
        else if (m.type === "wake") state.wake = m.state + (m.reason ? ":" + m.reason : "");
        else if (m.type === "resync") term.reset();
        else if (m.type === "input_reject") state.wake = "reject:" + m.reason;
        show();
        return;
      }
      const buf = new Uint8Array(ev.data);
      if (buf[0] === 0x00 || buf[0] === 0x04) term.write(buf.subarray(1));
    };
    ws.onclose = () => { state.writer = "closed"; show(); clearInterval(hbTimer); };
    ws.onopen = () => { hbTimer = setInterval(() => {
      if (ws.readyState === 1) ws.send(JSON.stringify({ type: "heartbeat" })); }, 20000); };
  };

  $("wake").onclick = () => { if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "wake" })); };
  term.onData(sendInput);
  term.onResize(sendResize);
  window.addEventListener("resize", () => { /* fit: fixed-geometry spike page */ });
  sendResize();
})();
