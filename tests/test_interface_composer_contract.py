#!/usr/bin/env python3
"""Composer send contract — spec 43 U2 (sprint 45, unit 5).

Two invariants, pinned as two properties each:

1. Pressing Enter on a non-empty draft yields exactly one of SENT / QUEUED /
   BLOCKED, and EVERY outcome paints a visible note. A silent no-op is red by
   construction — the row-by-row matrix test asserts the outcome and the note
   separately, so a change that keeps the latch but drops the note still
   fails. (Partially-pinned tests are how sprint 38 shipped five green tests
   that could not fail.)

2. A QUEUED submit flushes on every gate-clearing transition: draft-sync
   completion, echo drain, stream reconnect, writer-lease regain, and the
   legal-actions projection refresh.

The control frames driving these checks are NOT hand-authored shapes. They are
captured from the real input broker (interface_broker.accept_human_input
through interface_runtime's frame emitters) in this same run — decision #55's
fabricated-fixture lesson: sprint 38 shipped a currency test asserting against
per-model timestamps the parser cannot emit. The capture is hermetic (no tmux,
no node sidecar): the tmux write is the one injected seam, so the broker's
gate logic, sequencing and frame construction are all the production ones.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(TESTS))

import interface_broker  # noqa: E402
import interface_runtime  # noqa: E402
from test_interface_crash_window import build_engine_db  # noqa: E402
from test_interface_runtime import FakeClient  # noqa: E402

APP = (ENGINE / "ui" / "app.js").read_text()
EL = APP[APP.index("const el ="):APP.index("const esc =")]
COMPOSER = APP[APP.index("function ifSizeComposer"):APP.index("// End chat")]
STREAM = APP[APP.index("function ifOpenStream"):APP.index("function ifSendInput")]
BROKER = APP[APP.index("function ifSendInput"):APP.index("// ── Tabs + boot")]

HAS_NODE = shutil.which("node") is not None


# ── broker-produced fixtures ────────────────────────────────────────────────

def capture_broker_frames() -> dict:
    """Drive the REAL broker and return the control frames it emitted.

    Everything here is production code except _send_keys_sync (the tmux
    write): the lease fence, the two-phase commit, duplicate replay, seq-gap
    refusal and the ack/reject/lifecycle frame construction all run for real.
    """
    tmp = Path(tempfile.mkdtemp())
    try:
        return asyncio.run(_capture(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def _capture(tmp: Path) -> dict:
    db = tmp / "shell_db.db"
    build_engine_db(db)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO interface_generations (shell_id, generation) "
                "VALUES (1,1)")
    sid = con.execute(
        "INSERT INTO interface_sessions (shell_id, generation, occupancy,"
        " lifecycle) VALUES (1,1,'occupied','idle')").lastrowid
    con.execute("INSERT INTO interface_input_state (session_id, shell_id,"
                " generation, composer) VALUES (?,1,1,'clean')", (sid,))
    con.commit()
    lease_id = interface_broker.acquire_writer(con, sid, "tab-1", "tok-1")
    con.commit()
    con.close()

    rt = interface_runtime.InterfaceRuntime(str(db), run_dir=str(tmp / "run"))
    rt.loop = asyncio.get_running_loop()
    gen = interface_runtime.Generation(rt, sid, 1, 1, 24, 80)
    gen.pane_id = "%0"
    rt.generations[sid] = gen

    writer = FakeClient(sid, role="writer", client_id="tab-1",
                        lease_id=lease_id, lease_token="tok-1")
    viewer = FakeClient(sid, role="viewer", client_id="tab-2")
    gen.clients.add(writer)

    async def human(client, seq, payload):
        await rt._do_human_input(
            gen, interface_runtime.HumanInput(client, seq, payload))

    with mock.patch.object(rt, "_send_keys_sync", lambda pane, payload: None):
        await human(writer, 1, b"hello from the composer\r")   # ack + lifecycle
        await human(writer, 1, b"hello from the composer\r")   # replayed ack
        await human(writer, 5, b"gap\r")                       # seq_gap reject

        # writer_revoked: the lease this client holds is no longer current.
        con = sqlite3.connect(db)
        interface_broker.acquire_writer(con, sid, "tab-2", "tok-2",
                                        takeover=True)
        con.commit()
        con.close()
        await human(writer, 2, b"after the takeover\r")

    # stale_generation: a frame for a generation the runtime no longer serves.
    orphan = FakeClient(sid + 999, role="writer", client_id="tab-3")
    rt.enqueue_input(orphan, 1, b"nowhere\r")

    # writer-lease state frames, straight from the runtime's own constructor.
    taken_over = FakeClient(sid, role="writer", client_id="tab-2",
                            lease_id=None, lease_token="tok-2")
    con = sqlite3.connect(db)
    row = con.execute("SELECT lease_id FROM interface_writer_leases WHERE "
                      "session_id=? AND revoked_at IS NULL", (sid,)).fetchone()
    con.close()
    taken_over.lease_id = row[0]
    writer_active = await rt.writer_control(gen, taken_over)
    writer_held = await rt.writer_control(gen, viewer)

    acks = writer.by_type("input_ack")
    rejects = writer.by_type("input_reject")
    frames = {
        "input_ack": next(m for m in acks if not m.get("replayed")),
        "input_ack_replayed": next(m for m in acks if m.get("replayed")),
        "input_reject_seq_gap": next(
            m for m in rejects if m["reason"] == "seq_gap"),
        "input_reject_writer_revoked": next(
            m for m in rejects if m["reason"] == "writer_revoked"),
        "input_reject_stale_generation": next(
            m for m in orphan.by_type("input_reject")
            if m["reason"] == "stale_generation"),
        "lifecycle_dirty": writer.by_type("lifecycle")[-1],
        "writer_active": writer_active,
        "writer_held": writer_held,
    }
    # Guard the capture itself: a fixture that silently came back empty or
    # mis-shaped would make every downstream assertion vacuous.
    assert frames["input_ack"]["seq"] == 1, frames
    assert frames["input_ack_replayed"]["replayed"] is True, frames
    assert frames["writer_active"]["state"] == "active", frames
    assert frames["writer_held"]["state"] == "held", frames
    assert frames["lifecycle_dirty"]["composer"] == "dirty", frames
    return frames


# ── the JS harness ──────────────────────────────────────────────────────────

HARNESS = r"""
globalThis.ifClientId = "web-1";
globalThis.ifAttach = null;
const IF_ATTACHABLE_LIFECYCLES = new Set(
  ["starting", "idle", "busy", "approval", "user_input"]);

class FakeElement {
  constructor(tag) {
    this.tagName = tag;
    this.nodeType = 1;
    this.children = [];
    this.style = {};
    this.value = "";
    this.disabled = false;
    this.scrollHeight = 42;
    this._text = "";
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  set textContent(value) { this._text = String(value ?? ""); }
  get textContent() { return this._text; }
}
globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (text) => ({ nodeType: 3, textContent: String(text ?? "") }),
};
globalThis.location = { protocol: "http:", host: "sc.test" };
globalThis.Terminal = class {
  constructor() { this.cols = 80; this.rows = 24; }
  loadAddon() {} open() {} write() {} reset() {} dispose() {}
  resize(cols, rows) { this.cols = cols; this.rows = rows; }
  onData() {} onResize() {}
};
globalThis.FitAddon = { FitAddon: class {
  proposeDimensions() { return { cols: 80, rows: 24 }; }
} };
globalThis.ResizeObserver = class {
  constructor(fn) { this.fn = fn; }
  observe() {} disconnect() {}
};
const sockets = [];
globalThis.WebSocket = class {
  constructor() { this.readyState = 0; this.sent = []; sockets.push(this); }
  send(frame) { this.sent.push(frame); }
  close() { this.readyState = 3; if (this.onclose) this.onclose(); }
  goLive() { this.readyState = 1; if (this.onopen) this.onopen(); }
};
globalThis.WebSocket.OPEN = 1;

function invariant(ok, message) { if (!ok) throw new Error(message); }

// The api double: /interface/sessions/<id> is the legal-actions projection,
// everything else is the browser-composer draft-state endpoint.
let projection = { legal_actions: ["send_input"], state_reason: null };
let composerAck = (state) => state;   // what the server says the draft became
const apiCalls = [];
async function apiIf(path, method, body) {
  apiCalls.push({ path, method, body });
  if (path.startsWith("/interface/sessions/")) return projection;
  return { browser_composer: composerAck(body.state) };
}

// One attachment, gates all open, holding a live writer lease.
function attach(over) {
  const ws = new WebSocket();
  ws.readyState = 1;
  const a = {
    sessionId: 7,
    legalActions: new Set(["send_input"]),
    stateReason: "",
    role: "writer",
    st: { writer: "active", writerReason: "", note: "", lifecycle: "idle",
          browserComposer: "clean" },
    halted: false, awaiting: false, outBuf: "", seq: 4, inflight: 0, lastAck: 0,
    ws,
    termEl: new FakeElement("div"),
    composerEl: new FakeElement("div"),
    composerPendingSeq: null,
    browserComposerState: "clean",
    browserComposerWanted: "clean",
    browserComposerError: "",
    browserComposerSyncing: false,
    browserComposerVersion: 0,
    browserComposerChain: Promise.resolve(),
    composerSubmitLatched: false,
    composerLatchedValue: null,
    composerProjectionPending: false,
    composerProjectionVersion: 0,
    composerProjectionSync: Promise.resolve(),
    ...over,
  };
  a.paint = () => ifPaintComposer(a);
  ifAttach = a;
  ifBuildComposer(a);
  a.paint();
  return a;
}

// Type a draft the way a person does, and settle the dirty sync it starts.
async function typeDraft(a, text) {
  a.composerInput.value = text;
  a.composerInput.oninput();
  await a.browserComposerChain;
  return a;
}

function pressEnter(a) {
  a.composerInput.onkeydown({
    key: "Enter", shiftKey: false, preventDefault() {},
  });
}

// An attachment whose socket handlers are the REAL ones. attach()'s bare stub
// has no onopen/onclose — only ifOpenStream wires them, and the reconnect and
// lease-regain transitions live in exactly those handlers.
function attachStreamed(over) {
  const a = attach(over);
  ifOpenStream(a, "ticket-1");
  a.ws.goLive();
  return a;
}

function frameText(frame) {
  return new TextDecoder().decode(frame.subarray(9));
}

// Input frames only: the stream also carries 0x03 resize frames.
function inputFrames(ws) {
  return ws.sent.filter((frame) => frame[0] === 0x01);
}
"""


def run_js(body: str, frames: dict) -> None:
    script = (EL + COMPOSER + STREAM + BROKER + HARNESS +
              "\nconst BROKER_FRAMES = " + json.dumps(frames) + ";\n" +
              # Exit explicitly: a real stream leaves its heartbeat interval
              # and ack timer armed, which would keep node alive for minutes
              # after the assertions are done.
              "(async () => {\n" + body + "\n})().then(() => process.exit(0),"
              " (error) => {\n"
              "  console.error(error.stack || error);\n"
              "  process.exit(1);\n});\n")
    result = subprocess.run(["node", "-e", script], text=True,
                            capture_output=True, check=False, timeout=120)
    assert result.returncode == 0, result.stderr


@unittest.skipUnless(HAS_NODE, "node is required for the browser contract")
class ComposerContractTest(unittest.TestCase):
    """Spec 43 U2 — the send-contract matrix and the flush invariant."""

    @classmethod
    def setUpClass(cls):
        cls.frames = capture_broker_frames()

    def test_every_gate_state_yields_one_outcome_and_a_visible_note(self):
        """The matrix, row by row: outcome AND note, pinned separately.

        Each row is a fresh attachment mutated into exactly one gate state.
        The note assertion is what makes a silent no-op red — the historical
        defect was a return with no latch and no note, which an outcome-only
        test would have called a legitimate BLOCKED.
        """
        run_js(r"""
const rows = [
  { id: "open", outcome: "SENT",
    over: {}, note: /Sending through the generation-fenced input broker/ },
  { id: "draft-syncing", outcome: "QUEUED",
    over: { browserComposerSyncing: true, browserComposerWanted: "dirty" },
    note: /^queued — draft syncing$/ },
  { id: "silent-path", outcome: "QUEUED",
    // The fully silent path spec 43 U2 names: a non-empty draft whose browser
    // composer state is neither dirty nor dirty-wanted. No latch, no note,
    // nothing on the wire — Enter simply vanished.
    over: { browserComposerState: "clean", browserComposerWanted: "clean" },
    note: /^queued — draft syncing$/ },
  { id: "terminal-busy", outcome: "QUEUED",
    over: { awaiting: true }, note: /^queued — waiting for terminal$/ },
  { id: "terminal-busy-outbuf", outcome: "QUEUED",
    over: { outBuf: "pending bytes" },
    note: /^queued — waiting for terminal$/ },
  { id: "ack-pending", outcome: "BLOCKED",
    over: { composerPendingSeq: 99 },
    note: /Sending through the generation-fenced input broker/ },
  { id: "halted", outcome: "BLOCKED",
    over: { halted: true,
            st: { writer: "active", writerReason: "", lifecycle: "idle",
                  browserComposer: "dirty",
                  note: "message acknowledgement timed out — delivery is " +
                    "unknown; the draft was retained. Inspect the terminal, " +
                    "then reattach before retrying" } },
    note: /acknowledgement timed out/ },
  { id: "no-lease", outcome: "BLOCKED",
    over: { role: "viewer",
            st: { writer: "held", writerReason: "control was taken by " +
                    "another client", lifecycle: "idle", note: "",
                  browserComposer: "dirty" } },
    note: /control was taken by another client/ },
  { id: "authority-refresh", outcome: "BLOCKED",
    over: { composerProjectionPending: true },
    note: /Read-only while server input authority refreshes/ },
  { id: "input-illegal", outcome: "BLOCKED",
    over: { legalActions: new Set([]), stateReason: "generation has ended" },
    note: /generation has ended/ },
  { id: "stream-down", outcome: "BLOCKED",
    over: { ws: { readyState: 3, send() {} } },
    note: /Connecting to the generation-fenced input broker/ },
  { id: "draft-error", outcome: "BLOCKED",
    over: { browserComposerError: "server refused the draft state" },
    note: /Draft safety sync failed/ },
];

for (const row of rows) {
  const a = attach();
  // Reach a dirty, settled draft first, then impose the row's gate state, so
  // every row differs from the SENT baseline by exactly its own gate.
  await typeDraft(a, "matrix row " + row.id);
  Object.assign(a, row.over);
  if (row.id === "silent-path") {
    a.browserComposerState = "clean";
    a.browserComposerWanted = "clean";
  }
  // Blank the note and do NOT repaint: pressing Enter is what has to put a
  // note back. A BLOCKED row that relies on some earlier paint having left
  // the right text there is exactly the "Enter did nothing" report.
  a.composerNote.textContent = "";
  const before = a.ws.sent ? a.ws.sent.length : 0;
  pressEnter(a);
  const sent = (a.ws.sent ? a.ws.sent.length : 0) - before;
  const note = a.composerNote.textContent;

  // Property 1 — exactly one outcome.
  if (row.outcome === "SENT") {
    invariant(sent === 1 && a.composerPendingSeq != null,
      `${row.id}: expected SENT, sent=${sent} pending=${a.composerPendingSeq}`);
    invariant(!a.composerSubmitLatched, `${row.id}: SENT left a latch behind`);
  } else if (row.outcome === "QUEUED") {
    invariant(sent === 0, `${row.id}: QUEUED put ${sent} frames on the wire`);
    invariant(a.composerSubmitLatched, `${row.id}: QUEUED did not latch`);
    invariant(a.composerLatchedValue === "matrix row " + row.id,
      `${row.id}: QUEUED latched the wrong draft: ${a.composerLatchedValue}`);
  } else {
    invariant(sent === 0, `${row.id}: BLOCKED put ${sent} frames on the wire`);
    invariant(!a.composerSubmitLatched, `${row.id}: BLOCKED latched a submit`);
  }

  // Property 2 — a visible note, and the RIGHT one. Asserted separately from
  // the outcome: dropping the note must fail even when the latch is correct.
  invariant(note !== "", `${row.id}: ${row.outcome} painted NO note at all`);
  invariant(row.note.test(note),
    `${row.id}: wrong note for ${row.outcome}: ${JSON.stringify(note)}`);
}

// The silent path must also START the sync it is now waiting on, or the latch
// would wait for a transition that never comes.
{
  const a = attach();
  a.composerInput.value = "never announced dirty";
  a.browserComposerState = "clean";
  a.browserComposerWanted = "clean";
  apiCalls.length = 0;
  pressEnter(a);
  invariant(a.composerSubmitLatched, "silent path did not latch");
  invariant(a.browserComposerSyncing,
    "silent path latched without starting the sync it now waits on");
  await a.browserComposerChain;
  const asked = apiCalls.filter((c) => c.path === "/interface/browser-composer");
  invariant(asked.length === 1 && asked[0].body.state === "dirty",
    `silent path did not start the draft sync: ${JSON.stringify(apiCalls)}`);
  invariant(a.ws.sent.length === 1,
    "the latched silent-path submit never flushed once its sync completed");
}

// An empty draft is not a submit and stays silent — the contract is scoped to
// non-empty drafts, and Enter on an empty box must not latch anything.
{
  const a = attach();
  await typeDraft(a, "   \n  ");
  pressEnter(a);
  invariant(a.ws.sent.length === 0 && !a.composerSubmitLatched,
    "whitespace-only Enter was treated as a submit");
}
""", self.frames)

    def test_queued_submit_flushes_on_every_gate_clearing_transition(self):
        """Invariant 2 — one test per transition that opens a gate.

        Reconnect and writer-lease regain are the same source site (see the
        module docstring for U2's reported judgement): a take-over resets the
        lease and then re-opens the stream, so both shapes are driven here.
        """
        run_js(r"""
// 1. Draft-sync completion.
{
  const a = attach();
  a.composerInput.value = "flush on draft sync";
  a.composerInput.oninput();          // starts the real dirty sync
  invariant(a.browserComposerSyncing, "typing did not start a draft sync");
  pressEnter(a);                      // Enter lands mid-sync
  invariant(a.composerSubmitLatched && a.ws.sent.length === 0,
    "draft-sync case did not queue");
  await a.browserComposerChain;       // the server acks the dirty state
  invariant(a.ws.sent.length === 1,
    "draft-sync completion did not flush the queued submit");
  invariant(frameText(a.ws.sent[0]) === "flush on draft sync\r",
    `flushed the wrong bytes: ${frameText(a.ws.sent[0])}`);
}

// 2. Echo drain — driven by the broker's own input_ack frame.
{
  const a = attach();
  await typeDraft(a, "flush on echo drain");
  a.awaiting = true;
  a.inflight = 3;
  pressEnter(a);
  invariant(a.composerSubmitLatched && a.ws.sent.length === 0,
    "echo-drain case did not queue");
  ifControl(a, { ...BROKER_FRAMES.input_ack, seq: 3 });
  invariant(a.ws.sent.length === 1,
    "echo drain did not flush the queued submit");
}

// 3. Legal-actions projection refresh. The latch must SURVIVE the illegal
//    window — the refresh used to destroy it outright.
{
  const a = attach();
  await typeDraft(a, "flush on legal actions");
  a.legalActions = new Set([]);
  a.stateReason = "input authority is being re-derived";
  pressEnter(a);
  invariant(!a.composerSubmitLatched,
    "an illegal send_input state must be BLOCKED, not queued");
  // Queue it legally, then take send_input away and give it back.
  a.legalActions = new Set(["send_input"]);
  a.awaiting = true;
  pressEnter(a);
  invariant(a.composerSubmitLatched, "legal-actions case did not queue");
  a.awaiting = false;
  a.legalActions = new Set([]);
  projection = { legal_actions: ["send_input"], state_reason: null };
  ifRefreshComposerProjection(a);
  invariant(a.composerSubmitLatched,
    "the projection refresh destroyed a queued submit");
  await a.composerProjectionSync;
  invariant(a.ws.sent.length === 1,
    "the legal-actions refresh did not flush the queued submit");
}

// 4. Stream reconnect. A queued submit never left the browser, so a close
//    carries no delivery ambiguity: it stays queued and rides the new stream.
{
  const a = attachStreamed();
  await typeDraft(a, "flush on reconnect");
  a.awaiting = true;
  pressEnter(a);
  invariant(a.composerSubmitLatched, "reconnect case did not queue");
  const dropped = a.ws;
  a.awaiting = false;
  dropped.close();
  invariant(a.composerSubmitLatched,
    "a stream drop destroyed a submit that was never sent");
  invariant(!a.halted,
    "a stream drop halted input even though nothing was on the wire");
  invariant(/still held/.test(a.composerNote.textContent),
    `the dropped stream hid the queued message: ` +
    JSON.stringify(a.composerNote.textContent));
  // The status line is a second surface and carries its own text: a drop that
  // says only "reselect the shell to reattach" tells the operator their
  // queued message is gone when it is not.
  invariant(/queued message is still held/.test(a.st.note),
    `the drop notice disowned the queued message: ` +
    JSON.stringify(a.st.note));
  a.st.writer = "active";
  ifOpenStream(a, "ticket-2");
  a.ws.goLive();
  invariant(inputFrames(a.ws).length === 1,
    "the reconnected stream did not flush the queued submit");
  invariant(frameText(inputFrames(a.ws)[0]) === "flush on reconnect\r",
    "the reconnected stream flushed the wrong draft");
}

// 5. Writer-lease regain, through the real take-over shape: the old socket is
//    replaced, and its late close must not clobber the generation that
//    succeeded it (that close used to destroy the latch AND halt input).
{
  const a = attachStreamed();
  await typeDraft(a, "flush on lease regain");
  a.awaiting = true;
  pressEnter(a);
  invariant(a.composerSubmitLatched, "lease-regain case did not queue");
  // Another client takes the lease: the runtime's own writer frame says held.
  const superseded = a.ws;
  a.st.writer = BROKER_FRAMES.writer_held.state;
  a.paint();
  invariant(/still held/.test(a.composerNote.textContent),
    "a read-only composer hid the queued message");
  // Take-over: fresh lease, state reset, stream re-opened.
  a.role = "writer";
  a.st.writer = BROKER_FRAMES.writer_active.state;
  a.awaiting = false; a.outBuf = ""; a.halted = false;
  ifOpenStream(a, "ticket-3");
  superseded.close();          // the replaced socket's late close event
  invariant(a.composerSubmitLatched && !a.halted,
    "the superseded socket's close clobbered the live generation");
  a.ws.goLive();
  invariant(inputFrames(a.ws).length === 1,
    "the regained writer lease did not flush the queued submit");
}

// A frame that WAS on the wire keeps the old, stricter treatment: delivery is
// unknown, so the close halts instead of silently re-sending on reconnect.
{
  const a = attachStreamed();
  await typeDraft(a, "already on the wire");
  pressEnter(a);
  invariant(a.composerPendingSeq != null, "baseline send did not go out");
  a.ws.close();
  invariant(a.halted && !a.composerSubmitLatched &&
    /delivery|acknowledgement/.test(a.st.note),
    "an unacknowledged in-flight frame must halt, never re-queue");
}
""", self.frames)

    def test_broker_rejections_still_clear_the_latch(self):
        """The latch survives transport blips, never broker refusals.

        Driven with the broker's real reject frames: a refusal means the
        session will not take these bytes, so holding a queued submit would
        keep re-offering something the server already refused.
        """
        run_js(r"""
for (const key of ["input_reject_seq_gap", "input_reject_writer_revoked",
                   "input_reject_stale_generation"]) {
  const frame = BROKER_FRAMES[key];
  const a = attach();
  await typeDraft(a, "rejected draft");
  a.awaiting = true;
  pressEnter(a);
  invariant(a.composerSubmitLatched, `${key}: did not queue`);
  ifControl(a, { ...frame, seq: a.inflight });
  invariant(!a.composerSubmitLatched,
    `${key}: a broker refusal left the submit queued`);
  invariant(a.composerInput.value === "rejected draft",
    `${key}: the refused draft was not retained for the operator`);
  invariant(a.composerNote.textContent !== "",
    `${key}: a broker refusal painted no note`);
  invariant(a.ws.sent.length === 0,
    `${key}: a refused submit was put on the wire anyway`);
}

// A duplicate ack is still an ack: it must drain the echo gate like any other.
{
  const a = attach();
  await typeDraft(a, "replayed ack drains the gate");
  a.awaiting = true;
  a.inflight = 2;
  pressEnter(a);
  invariant(a.composerSubmitLatched, "replayed-ack case did not queue");
  ifControl(a, { ...BROKER_FRAMES.input_ack_replayed, seq: 2 });
  invariant(a.ws.sent.length === 1,
    "a replayed ack did not drain the echo gate");
}
""", self.frames)

    def test_matrix_is_the_single_source_of_truth_in_source(self):
        """The contract lives in one table, not in scattered branches.

        Prose rules on this clause drift (they already did): the point of the
        table is that the outcome and its note cannot be edited apart.
        """
        assert "const IF_COMPOSER_MATRIX = [" in APP
        assert "function ifComposerGate(a)" in APP
        # Writability is DERIVED from the table's gate rows, so the disabled
        # state and the painted reason cannot disagree.
        assert ("return !IF_COMPOSER_MATRIX.some((row) => row.gate && row.when(a));"
                in APP)
        assert "function ifComposerDisabledReason" not in APP
        for row_id in ("ack-pending", "draft-error", "authority-refresh",
                       "input-illegal", "halted", "no-lease", "stream-down",
                       "draft-syncing", "terminal-busy", "open"):
            assert f'id: "{row_id}"' in APP, row_id
        # Every gate-clearing transition goes through the one flush helper.
        assert APP.count("ifFlushLatchedSubmit(a);") == 4

    def test_capture_is_broker_produced_not_hand_authored(self):
        """The fixtures came out of the broker in this run.

        A hand-authored shape is the failure decision #55 records; this pins
        that the frames carry broker-assigned values a fabricated dict would
        not have (the session-scoped ack sequence and the DB-derived composer
        state), not just the right keys.
        """
        frames = self.frames
        assert frames["input_ack"] == {"type": "input_ack", "seq": 1}
        assert frames["input_ack_replayed"] == {
            "type": "input_ack", "seq": 1, "replayed": True}
        assert frames["input_reject_seq_gap"]["reason"] == "seq_gap"
        assert frames["input_reject_writer_revoked"]["reason"] == \
            "writer_revoked"
        assert frames["input_reject_stale_generation"]["reason"] == \
            "stale_generation"
        # The lifecycle broadcast is read back from the DB the broker wrote —
        # 'dirty' here is the pending commit, not a literal in this file.
        assert frames["lifecycle_dirty"] == {
            "type": "lifecycle", "lifecycle": "idle", "composer": "dirty"}
        assert frames["writer_active"] == {"type": "writer", "state": "active"}
        assert frames["writer_held"] == {"type": "writer", "state": "held"}


if __name__ == "__main__":
    unittest.main()
