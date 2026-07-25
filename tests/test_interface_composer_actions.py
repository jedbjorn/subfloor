"""Composer actions (spec #43 U7): Enter as the sole submit, red End, +Chat.

The unit's value is one property — the +Chat chain is FAIL-CLOSED: it starts a
new session on an explicit ``terminated`` and on nothing else. Every
recovery-shaped outcome must both land on the recovery pane AND make no start
attempt, so each fail-closed test asserts BOTH. Pinning only "landed on
recovery" would stay green while the chain fired a start it must never fire,
which is exactly the partially-pinned shape sprint 38 kept shipping (flag: five
findings across five units).

Driven through node against the real app.js regions rather than the browser, so
the chain's branches are reachable without a live session; the rendered suite
covers the pixels.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / ".super-coder" / "ui" / "app.js").read_text()

EL = APP[APP.index("const el ="):APP.index("const esc =")]
# Sliced, not restated: the lifecycle gate the composer buttons obey has to be
# the app's own set, or a test would keep passing against a stale copy of it.
LIFECYCLES = APP[APP.index("const IF_ATTACHABLE_LIFECYCLES"):
                 APP.index("function ifModelLabel")]
MODEL_LABEL = APP[APP.index("function ifModelLabel"):
                  APP.index("// What the surface may say")]
COMPOSER = APP[APP.index("function ifSizeComposer"):APP.index("// End chat")]
ACTIONS = APP[APP.index("// End chat (spec Workflow 9)"):
              APP.index("// Take-over:")]

HAS_NODE = shutil.which("node") is not None

pytestmark = pytest.mark.skipif(not HAS_NODE, reason="node is required")


FAKE_DOM = r"""
class FakeElement {
  constructor(tag) {
    this.tagName = tag; this.nodeType = 1; this.children = [];
    this.textContent = ""; this.disabled = false; this.style = {};
  }
  append(...nodes) { this.children.push(...nodes); }
  closest() { return this; }
}
globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (text) => ({ nodeType: 3, textContent: String(text ?? "") }),
};
function invariant(ok, message) { if (!ok) throw new Error(message); }
"""

# The chain's collaborators. Every one is observable so a test can assert what
# the chain did NOT do, which is where fail-closed actually lives.
CHAIN_HARNESS = r"""
let apiIf, confirm;
let detached = 0;
let rendered = 0;
let toasts = [];
let calls = [];
let confirms = [];
let sleeps = [];
function ifDetach() { detached += 1; }
async function renderInterface() { rendered += 1; }
function toast(msg) { toasts.push(String(msg)); }
const realSetTimeout = globalThis.setTimeout;
// The retry's 2s wait is asserted, not endured: record the delay and run the
// continuation at once, so a regression that drops the wait is still visible.
globalThis.setTimeout = (fn, ms) => { sleeps.push(ms); return realSetTimeout(fn, 0); };

function reset() {
  detached = 0; rendered = 0; toasts = []; calls = []; confirms = []; sleeps = [];
}
function attach(over) {
  const a = {
    sessionId: 7,
    st: { harness: "codex", modelRoute: "gpt-5.6-terra", launchEffort: "high",
          note: "" },
    composerActionsBusy: false,
    painted: 0,
    paint() { this.painted += 1; },
  };
  return Object.assign(a, over || {});
}
const SEL = { shell_id: 3, shortname: "DEV3", display_name: "Code-01" };
function pane() { return new FakeElement("div"); }
// Two 409 shapes, because the server really does emit both and the client
// branches on them differently (ifError: `code` comes from an error envelope,
// `reason` from a bare terminated/reason body).
function fail(status, code, message) {
  const e = new Error(message);
  e.status = status; e.code = code;
  e.body = { error: { code, message } };
  return e;
}
function failReason(status, reason) {
  const e = new Error(reason);
  e.status = status; e.code = undefined;
  e.body = { terminated: false, reason };
  return e;
}
// Records every call WITH the busy flag as it stood at call time — the only way
// to prove the double-activation guard held for the chain's whole duration
// rather than merely at its start and end.
function recorder(handlers, a) {
  return async (path, method, body) => {
    calls.push({ path, method, body, busy: a.composerActionsBusy });
    return handlers(path, method, body, calls.length);
  };
}
function starts() { return calls.filter((c) => c.path === "/interface/sessions"); }
"""


def run_node(script: str) -> None:
    result = subprocess.run(["node", "-e", script], text=True,
                            capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def chain_script(body: str) -> str:
    return (EL + MODEL_LABEL + FAKE_DOM + CHAIN_HARNESS + ACTIONS
            + "\n(async () => {\n" + body
            + "\n})().catch((error) => { console.error(error.stack || error);"
              " process.exit(1); });\n")


def test_occupied_composer_has_no_send_button_and_a_static_hint():
    """U7's first bullet, and the reason the hint is static: U2's note is the
    single state surface, so a hint that moved with gate state would be a second
    surface able to contradict it. Pinned as two properties — the hint never
    changes, AND the note demonstrably does (otherwise an inert composer would
    satisfy the first assertion by doing nothing at all)."""
    script = EL + LIFECYCLES + FAKE_DOM + r"""
let synced = [];
function ifSyncBrowserComposer(a, state) { synced.push(state); }
""" + COMPOSER + r"""
const a = {
  composerEl: new FakeElement("div"),
  sel: {}, pane: new FakeElement("div"),
  st: { note: "", lifecycle: "idle" },
  awaiting: false, halted: false, outBuf: "", role: "writer",
  legalActions: new Set(["send_input"]),
  stateReason: "",
  composerPendingSeq: null, composerSubmitLatched: false,
  browserComposerState: "clean", browserComposerWanted: "clean",
  browserComposerSyncing: false, browserComposerError: "",
  composerActionsBusy: false,
  paint() {},
};
ifBuildComposer(a);
const buttons = [];
const walk = (node) => {
  for (const kid of node.children || []) {
    if (kid.tagName === "button") buttons.push(kid);
    walk(kid);
  }
};
walk(a.composerEl);
const labels = buttons.map((b) => b.textContent);
invariant(!labels.includes("Send"),
  `the Send button survived U7: ${JSON.stringify(labels)}`);
invariant(JSON.stringify(labels) === JSON.stringify(["End", "+Chat"]),
  `composer actions are not [End, +Chat]: ${JSON.stringify(labels)}`);
// End keeps the red styling family and the whole termination contract.
const end = buttons[0];
invariant(end.className.includes("act bad") &&
  end.className.includes("if-end-chat"),
  `End left the red act-bad family: ${end.className}`);

const hints = [];
const findHint = (node) => {
  for (const kid of node.children || []) {
    if (kid.className === "if-composer-hint") hints.push(kid);
    findHint(kid);
  }
};
findHint(a.composerEl);
invariant(hints.length === 1 && hints[0].textContent === "enter to send",
  `static submit hint is missing: ${JSON.stringify(hints.map((h) => h.textContent))}`);

// Enter is wired as a submit; there is no click path left to reach it.
let sent = 0;
globalThis.ifComposerSend = () => { sent += 1; };
a.composerInput.value = "x";
a.composerInput.onkeydown({ key: "Enter", shiftKey: false, preventDefault() {} });
invariant(sent === 1, "Enter is no longer the submit affordance");
a.composerInput.onkeydown({ key: "Enter", shiftKey: true, preventDefault() {} });
invariant(sent === 1, "Shift+Enter stopped inserting a newline");

// The hint is static ACROSS gate states, while the note moves.
const notes = new Set();
const hintTexts = new Set();
for (const state of [
  () => { a.halted = false; a.role = "writer"; a.legalActions = new Set(["send_input"]); },
  () => { a.halted = true; },
  () => { a.halted = false; a.role = "viewer"; },
  () => { a.role = "writer"; a.legalActions = new Set(); a.stateReason = "generation has ended"; },
]) {
  state();
  ifPaintComposer(a);
  notes.add(a.composerNote.textContent);
  hintTexts.add(hints[0].textContent);
}
invariant(hintTexts.size === 1 && hintTexts.has("enter to send"),
  `the hint tracked gate state: ${JSON.stringify([...hintTexts])}`);
invariant(notes.size > 1,
  `the note never moved, so hint-invariance proves nothing: ${JSON.stringify([...notes])}`);
"""
    run_node(script)


def test_both_composer_actions_share_one_lifecycle_and_busy_gate():
    """SC-167: +Chat used to outlive the lifecycle gate End obeyed, because the
    two halves of that state had two writers in two paint functions. Both
    buttons terminate the session — +Chat's own chain begins with an End — so
    both are asserted through both gates, and against EACH other: an inactive
    lifecycle hides and disables them, an in-flight chain disables them while
    they stay visible, and a live lifecycle restores them. Painting is what is
    exercised, not the flag: the chain tests pin the flag, and it was the paint
    half that shipped unpinned."""
    script = EL + LIFECYCLES + FAKE_DOM + r"""
function ifSyncBrowserComposer() {}
""" + COMPOSER + r"""
const a = {
  composerEl: new FakeElement("div"),
  sel: {}, pane: new FakeElement("div"),
  st: { note: "", lifecycle: "idle" },
  awaiting: false, halted: false, outBuf: "", role: "writer",
  legalActions: new Set(["send_input"]),
  stateReason: "",
  composerPendingSeq: null, composerSubmitLatched: false,
  browserComposerState: "clean", browserComposerWanted: "clean",
  browserComposerSyncing: false, browserComposerError: "",
  composerActionsBusy: false,
  paint() {},
};
ifBuildComposer(a);
const both = () => [
  { name: "End", el: a.composerEnd },
  { name: "+Chat", el: a.composerNewChat },
];
const check = (expect, why) => {
  for (const { name, el } of both()) {
    invariant(el.hidden === expect.hidden,
      `${name} hidden=${el.hidden}, expected ${expect.hidden} ${why}`);
    invariant(el.disabled === expect.disabled,
      `${name} disabled=${el.disabled}, expected ${expect.disabled} ${why}`);
  }
};

ifPaintComposer(a);
check({ hidden: false, disabled: false }, "on a live lifecycle");

// Every non-attachable lifecycle, not just one: the gate is the app's set, and
// `ended` alone would leave `lost` / `error` free to regress.
for (const lifecycle of ["ended", "lost", "error", "unreconciled", "available"]) {
  a.st.lifecycle = lifecycle;
  ifPaintComposer(a);
  check({ hidden: true, disabled: true }, `on lifecycle ${lifecycle}`);
}

// A chain in flight holds BOTH buttons — and leaves them visible, because the
// operator has to see the action they already pressed.
a.st.lifecycle = "busy";
a.composerActionsBusy = true;
ifPaintComposer(a);
check({ hidden: false, disabled: true }, "while a chain is in flight");

// The gate releases; neither button is left stuck off.
a.composerActionsBusy = false;
ifPaintComposer(a);
check({ hidden: false, disabled: false }, "after the chain finished");
"""
    run_node(script)


def test_new_chat_reuses_the_ended_session_launch_triple_verbatim():
    """+Chat's whole point: harness, model route and effort go back unchanged.
    The body is asserted as a closed world — an extra or renamed field is a
    different launch, which is the one thing this must not do."""
    run_node(chain_script(r"""
  const a = attach();
  confirm = (msg) => { confirms.push(msg); return true; };
  apiIf = recorder((path) => {
    if (path === "/interface/termination-requests") return { terminated: true };
    return { session_id: 9 };
  }, a);
  await ifNewChatSameRoute(a, SEL, pane());

  invariant(calls.length === 2, `chain made ${calls.length} calls, expected 2`);
  invariant(calls[0].path === "/interface/termination-requests" &&
    calls[0].body.force === false && calls[0].body.session_id === 7,
    `graceful termination was not first: ${JSON.stringify(calls[0])}`);
  const start = calls[1];
  invariant(start.path === "/interface/sessions" && start.method === "POST",
    `start POST missing: ${JSON.stringify(start)}`);
  invariant(JSON.stringify(start.body) === JSON.stringify(
    { shell_id: 3, harness: "codex", model: "gpt-5.6-terra", effort: "high" }),
    `launch triple was not reused verbatim: ${JSON.stringify(start.body)}`);
  invariant(detached === 1 && rendered === 1,
    `chain did not detach and re-render once: ${detached}/${rendered}`);
  // Double activation: the guard held across BOTH legs, not just at the edges.
  invariant(calls.every((c) => c.busy === true),
    "actions were re-enabled mid-chain");

  // The confirm names both effects and the exact route being reused.
  invariant(confirms.length === 1, `expected one confirm: ${confirms.length}`);
  const text = confirms[0];
  invariant(text.includes("End session #7") && text.includes("start a new chat") &&
    text.includes("codex") && text.includes("GPT 5.6 TERRA") &&
    text.includes("draft is discarded"),
    `confirm did not name both effects and the route: ${text}`);
"""))


def test_null_legs_of_the_triple_relaunch_as_harness_default():
    """A NULL model or effort is a real value meaning "harness default" — for
    pre-migration rows and for launches that named none. It must be OMITTED,
    because an absent field is how the API spells that, and a null would be a
    validation error rather than the same launch."""
    run_node(chain_script(r"""
  const a = attach({ st: { harness: "claude", modelRoute: null,
                           launchEffort: null, note: "" } });
  confirm = (msg) => { confirms.push(msg); return true; };
  apiIf = recorder((path) => {
    if (path === "/interface/termination-requests") return { terminated: true };
    return {};
  }, a);
  await ifNewChatSameRoute(a, SEL, pane());

  const start = starts()[0];
  invariant(JSON.stringify(start.body) === JSON.stringify(
    { shell_id: 3, harness: "claude" }),
    `NULL legs were not omitted: ${JSON.stringify(start.body)}`);
  invariant(confirms[0].includes("HARNESS DEFAULT"),
    `confirm hid the harness-default route: ${confirms[0]}`);
"""))


@pytest.mark.parametrize("leg,handler", [
    # The non-throwing 409: the server answers 200/409 with a reason body.
    ("identity_mismatch",
     'return { terminated: false, reason: "identity_mismatch" };'),
    ("not_running",
     'throw failReason(409, "not_running");'),
    # The thrown 409 legs End already treats as recovery-shaped.
    ("not_occupied",
     'throw fail(409, "not_occupied", "session 7 is unreconciled");'),
    ("identity_unverified",
     'throw fail(409, "identity_unverified", "identity not verified");'),
])
def test_recovery_shaped_termination_never_starts_a_session(leg, handler):
    """THE unit's load-bearing property. Two assertions per leg, deliberately:
    the chain lands on recovery AND makes no start attempt. The spec's mutation
    proof targets exactly this — make identity_mismatch fall through to the
    start POST and this test must go red."""
    body = r"""
  const a = attach();
  confirm = () => true;
  apiIf = recorder((path) => {
    if (path === "/interface/termination-requests") { __HANDLER__ }
    return {};
  }, a);
  await ifNewChatSameRoute(a, SEL, pane());

  invariant(starts().length === 0,
    `FAIL-CLOSED VIOLATED: __LEG__ started a session over an unreconciled ` +
    `shell: ${JSON.stringify(starts())}`);
  invariant(rendered === 1,
    `__LEG__ did not land on the recovery pane: rendered=${rendered}`);
  invariant(a.st.note === "",
    `__LEG__ left a terminal error on a recoverable shell: ${a.st.note}`);
"""
    body = body.replace("__HANDLER__", handler).replace("__LEG__", leg)
    run_node(chain_script(body))


def test_declined_force_kill_ends_with_a_note_and_starts_nothing():
    """Spec step 4's declined branch: stop with the existing note, nothing
    started — and give the operator the buttons back, since the session is
    still alive and the chain is over."""
    run_node(chain_script(r"""
  const a = attach();
  // First confirm = the chain's own; second = the force-kill gate, declined.
  confirm = (msg) => { confirms.push(msg); return confirms.length === 1; };
  apiIf = recorder((path) => {
    if (path === "/interface/termination-requests")
      return { terminated: false, reason: "graceful_timeout", pid: 4321,
               generation: 9 };
    return {};
  }, a);
  await ifNewChatSameRoute(a, SEL, pane());

  invariant(starts().length === 0,
    `a declined force kill still started a session: ${JSON.stringify(starts())}`);
  invariant(rendered === 0 && detached === 0,
    `a declined force kill tore down a live session: ${rendered}/${detached}`);
  invariant(a.st.note.includes("graceful stop timed out"),
    `declined force kill said nothing: ${a.st.note}`);
  invariant(a.composerActionsBusy === false,
    "actions stayed disabled after the chain stopped");
  invariant(confirms[1].includes("PID 4321") && confirms[1].includes("generation 9"),
    `force confirm did not name the exact identity: ${confirms[1]}`);
"""))


def test_confirmed_force_kill_continues_into_the_start():
    """Spec step 4's other half: confirmed and terminated → continue to step 3."""
    run_node(chain_script(r"""
  const a = attach();
  confirm = () => true;
  apiIf = recorder((path, method, body) => {
    if (path === "/interface/termination-requests")
      return body.force
        ? { terminated: true }
        : { terminated: false, reason: "graceful_timeout", pid: 1, generation: 1 };
    return {};
  }, a);
  await ifNewChatSameRoute(a, SEL, pane());

  invariant(starts().length === 1,
    `force-then-start did not reach the start: ${JSON.stringify(calls)}`);
  invariant(starts()[0].body.model === "gpt-5.6-terra",
    "the forced path lost the launch route");
"""))


def test_shell_occupied_race_is_retried_exactly_once_after_two_seconds():
    """Spec step 6: the occupancy flip can lag the termination we just made, so
    one retry — bounded. The delay is asserted so dropping the wait is visible,
    and the attempt count is asserted so the bound cannot drift upward."""
    run_node(chain_script(r"""
  const a = attach();
  confirm = () => true;
  apiIf = recorder((path) => {
    if (path === "/interface/termination-requests") return { terminated: true };
    if (starts().length === 1) throw fail(409, "shell_occupied", "still occupied");
    return {};
  }, a);
  await ifNewChatSameRoute(a, SEL, pane());

  invariant(starts().length === 2,
    `the occupancy race was not retried exactly once: ${starts().length}`);
  invariant(sleeps.includes(2000),
    `the retry did not wait 2s: ${JSON.stringify(sleeps)}`);
  invariant(rendered === 1 && toasts.length === 0,
    `a recovered race still reported failure: ${JSON.stringify(toasts)}`);
"""))


def test_second_shell_occupied_surfaces_the_server_message_and_stops():
    """Bounded retry: a second failure hands the shell to the normal New-chat
    path with the server's own words, and does NOT try a third time."""
    run_node(chain_script(r"""
  const a = attach();
  confirm = () => true;
  apiIf = recorder((path) => {
    if (path === "/interface/termination-requests") return { terminated: true };
    throw fail(409, "shell_occupied", "a live harness process holds this shell");
  }, a);
  await ifNewChatSameRoute(a, SEL, pane());

  invariant(starts().length === 2,
    `retry bound drifted: ${starts().length} start attempts`);
  invariant(toasts.length === 1 &&
    toasts[0].includes("shell_occupied") &&
    toasts[0].includes("a live harness process holds this shell"),
    `the server's message did not reach the operator: ${JSON.stringify(toasts)}`);
  invariant(rendered === 1,
    "the shell was not returned to the normal New-chat path");
"""))


def test_unavailable_model_route_is_surfaced_not_downgraded():
    """Declared judgement call, spec-silent: POST /interface/sessions preflights
    the route and 422s invalid_model_route when a once-valid route has since
    gone away. This is NOT the occupancy race, so it is not retried — and it is
    emphatically not downgraded to a harness-default launch, because silently
    changing the triple is the one failure this unit cannot have."""
    run_node(chain_script(r"""
  const a = attach();
  confirm = () => true;
  apiIf = recorder((path) => {
    if (path === "/interface/termination-requests") return { terminated: true };
    throw fail(422, "invalid_model_route",
      "stored or requested model route 'gpt-5.6-terra' is not currently available");
  }, a);
  await ifNewChatSameRoute(a, SEL, pane());

  invariant(starts().length === 1,
    `a 422 was retried like an occupancy race: ${starts().length} attempts`);
  invariant(starts()[0].body.model === "gpt-5.6-terra",
    "the start silently dropped the model route");
  invariant(toasts.length === 1 && toasts[0].includes("invalid_model_route"),
    `the 422 was swallowed: ${JSON.stringify(toasts)}`);
  invariant(rendered === 1,
    "the shell was not returned to the normal New-chat path");
"""))


def test_declining_the_chain_confirm_touches_nothing():
    """One confirm gates the whole chain; declining it must not terminate, not
    disable the actions, and not leave a note."""
    run_node(chain_script(r"""
  const a = attach();
  confirm = () => false;
  apiIf = recorder(() => ({}), a);
  await ifNewChatSameRoute(a, SEL, pane());
  invariant(calls.length === 0,
    `a declined +Chat still called the API: ${JSON.stringify(calls)}`);
  invariant(a.composerActionsBusy === false && a.st.note === "" &&
    detached === 0 && rendered === 0,
    "a declined +Chat left state behind");
"""))


def test_end_chat_keeps_its_own_contract_through_the_shared_path():
    """End's flow is unchanged by U7 (text-only). It now shares the termination
    sequence with +Chat, so assert End still detaches and re-renders on success
    and still reports an unrelated failure rather than swallowing it.

    It also shares the busy flag, so the guard is asserted the way +Chat's is —
    held at call time, and RELEASED on the one leg that stays on this pane. A
    guard that never releases is the same bug in the other direction: an End
    that failed for an unrelated reason would leave both actions dead."""
    run_node(chain_script(r"""
  const a = attach();
  confirm = () => true;
  apiIf = recorder(() => ({ terminated: true }), a);
  await ifEndChat(a, SEL, pane());
  invariant(detached === 1 && rendered === 1 && starts().length === 0,
    `End did not terminate cleanly: ${detached}/${rendered}`);
  // The guard covers End's chain too, so +Chat cannot be fired underneath it.
  invariant(calls.every((c) => c.busy === true),
    "End left the composer actions live for the duration of its own chain");

  reset();
  const b = attach();
  apiIf = recorder(() => { throw fail(500, "internal", "boom"); }, b);
  await ifEndChat(b, SEL, pane());
  invariant(detached === 0 && rendered === 0,
    "an unrelated failure was treated as recovery");
  invariant(b.st.note.includes("end chat failed"),
    `End swallowed an unrelated failure: ${b.st.note}`);
  // Two paints: the guard going on, and the release that also shows the note.
  invariant(b.composerActionsBusy === false && b.painted === 2,
    `a failed End did not release the actions onto the pane: ` +
    `busy=${b.composerActionsBusy} painted=${b.painted}`);
"""))
