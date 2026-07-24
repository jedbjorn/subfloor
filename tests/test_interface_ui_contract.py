"""Browser contract checks for spec #30 requirements 18-20 and 24.

The UI is deliberately build-free vanilla JS/CSS. These checks pin the shared
picker and responsive terminal behavior without inventing a second JS runtime
or duplicating application logic in a test fixture.
"""
import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".super-coder" / "scripts"))

import interface_cli as ic  # noqa: E402
import interface_recovery as recovery  # noqa: E402

APP = (ROOT / ".super-coder" / "ui" / "app.js").read_text()
CSS = (ROOT / ".super-coder" / "ui" / "style.css").read_text()
PICKER = APP[APP.index("function dmModelPicker"):
             APP.index("async function renderDefaultModels")]
RECOVERY = APP[APP.index("function ifRecoveryEvidenceRows"):
               APP.index("// Sprint wake panel")]
RENDER_INTERFACE = APP[APP.index("const IF_BADGE"):
                       APP.index("// A reservation is not a terminal")]
AVAILABLE = APP[APP.index("function ifAvailablePane"):
                APP.index("async function ifNewChatForm")]

BASE_PREVIEW = {
    "observation_id": "obs-1",
    "expires_in_s": 120,
    "classification": "exact_idle_orphan",
    "legal_actions": ["recover"],
    "evidence": {
        "shell": {"shell_id": 3, "shortname": "S3"},
        "session": {
            "session_id": 9, "generation": 1, "occupancy": "occupied",
            "lifecycle": "lost", "harness": "claude",
        },
        "generation": {
            "generation": 1, "ended_at": None, "last_hook_seq": 7,
        },
        "archive": {
            "archive_id": 12, "ended_at": None, "active": True,
        },
        "sprint_binding": {
            "binding_id": 15, "sprint_doc_id": 31,
        },
        "process": {
            "pane_id": "%1", "pane_pid": 4321, "pane_start_ticks": 999,
            "pane_present": False, "pid_state": "alive", "pgid": 4321,
        },
        "tmux": {
            "socket": "/run/if/tmux.sock", "session": "sc-S3",
            "window": "chat", "pane_id": "%1",
        },
        "unread_messages": 2,
        "git": {
            "worktree": "/x/s3", "branch": "fix/x", "dirty_tracked": 1,
            "untracked": 2, "unpushed_commits": 0,
        },
    },
}
BASE_PREVIEW["evidence_projection"] = recovery.evidence_projection(
    BASE_PREVIEW["evidence"], BASE_PREVIEW["classification"],
    BASE_PREVIEW["legal_actions"])


def run_recovery_js(body):
    """Exercise the real recovery UI helpers in Node with a minimal DOM."""
    el_helper = APP[APP.index("const el ="):APP.index("const esc =")]
    script = el_helper + r"""
let apiIf, confirm, prompt;
let ifSelected = "S3";
let ifAttach = null;
function ifDetach() {}
async function ifStartingPane() {}
async function ifSessionPane() {}
async function ifSprintPanel() {}
async function ifNewChatForm() {}
globalThis.location = { hash: "" };
""" + RENDER_INTERFACE + RECOVERY + AVAILABLE + r"""
class FakeElement {
  constructor(tag) {
    this.tagName = tag;
    this.nodeType = 1;
    this.children = [];
    this._text = "";
    this.checked = false;
    this.disabled = false;
    this.isConnected = true;
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; this._text = ""; }
  remove() {}
  closest() { return this; }
  set textContent(value) {
    this._text = String(value ?? "");
    this.children = [];
  }
  get textContent() {
    return this._text + this.children.map(
      (child) => typeof child === "string" ? child : child.textContent || ""
    ).join("");
  }
}
globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (text) => ({ nodeType: 3, textContent: String(text ?? "") }),
};
function all(root, predicate, found = []) {
  if (predicate(root)) found.push(root);
  for (const child of root.children || []) {
    if (child && child.nodeType === 1) all(child, predicate, found);
  }
  return found;
}
function button(root, label) {
  return all(root, (node) =>
    node.tagName === "button" && node.textContent === label)[0];
}
function invariant(ok, message) {
  if (!ok) throw new Error(message);
}
""" + "\nconst BASE_PREVIEW = " + json.dumps(BASE_PREVIEW) + ";\n" + \
        "\n(async () => {\n" + body + r"""
})().catch((error) => {
  console.error(error.stack || error);
  process.exit(1);
});
"""
    result = subprocess.run(
        ["node", "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_shared_picker_is_list_only_and_exact_route_only():
    assert 'label: "Harness default"' in PICKER
    assert 'm.availability === "available"' in PICKER
    assert "allMode" not in PICKER
    assert "use as typed" not in PICKER
    assert "dm-fam" not in PICKER
    assert "dm-raw" not in PICKER
    assert "ArrowDown" in PICKER and "ArrowUp" in PICKER
    assert "choices[highlighted].value" in PICKER
    assert APP.count("dmModelPicker(") == 3  # definition + both consumers


def test_shared_picker_scrolls_keyboard_highlight_through_large_catalogue():
    el_helper = APP[APP.index("const el ="):APP.index("const esc =")]
    script = el_helper + PICKER + r"""
class FakeClassList {
  toggle() {}
  remove() {}
}
class FakeElement {
  constructor(tag) {
    this.tagName = tag;
    this.nodeType = 1;
    this.children = [];
    this.classList = new FakeClassList();
    this.isConnected = true;
    this.value = "";
    this._text = "";
  }
  append(...nodes) { this.children.push(...nodes); }
  set textContent(value) {
    this._text = value;
    if (value === "") this.children = [];
  }
  get textContent() { return this._text; }
  contains(node) { return this === node || this.children.includes(node); }
  scrollIntoView(options) {
    globalThis.lastScrolled = { title: this.title, block: options.block };
  }
  blur() {}
}
globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (text) => ({ nodeType: 3, textContent: text }),
  addEventListener() {},
  removeEventListener() {},
};
const models = Array.from({ length: 65 }, (_, i) => ({
  id: `model-${i}`, name: `Model ${i}`, family: null,
  availability: "available",
}));
const picker = dmModelPicker(
  "codex", { stale: false, harnesses: { codex: { models } } },
  { model: null }, async () => {});
picker.input.onfocus();
for (let i = 0; i < 60; i += 1) {
  picker.input.onkeydown({ key: "ArrowDown", preventDefault() {} });
}
if (lastScrolled.title !== "model-59" || lastScrolled.block !== "nearest") {
  throw new Error(`active option was not scrolled: ${JSON.stringify(lastScrolled)}`);
}
"""
    result = subprocess.run(
        ["node", "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_interface_terminal_has_exact_caps_and_live_resize_reporting():
    assert "#view-interface { max-width: calc(230px + 1rem + 1300px); }" in CSS
    assert "max-width: 1300px" in CSS
    assert "max-height: 850px" in CSS
    assert "ResizeObserver(fit)" in APP
    assert "ws.onopen" in APP
    assert "ifSendResize(a, term.rows, term.cols)" in APP
    assert "new WebSocket(" in APP


def test_browser_only_downgrades_exact_writer_conflict():
    assert 'e.code !== "writer_held"' in APP
    assert "ifStartingPane" in APP
    assert 'textContent: "Cancel start"' in APP
    assert "sess.attachable" in APP


def test_alerts_render_provenance_action_and_durable_acknowledgement():
    assert "a.meaning" in APP
    assert "a.next_action" in APP
    assert "a.generation" in APP
    assert "/acknowledge" in APP
    assert "Alert history" in APP


def test_recovery_renders_only_server_listed_actions_and_full_diagnostics():
    run_recovery_js(r"""
renderInterface = async () => {};
confirm = () => true;
prompt = () => "S3";
const cases = [
  { legal: ["recover"], present: "Recover", absent: "Force recover" },
  { legal: ["force"], present: "Force recover", absent: "Recover" },
  { legal: [], present: null, absent: "Recover" },
];
for (const test of cases) {
  const projection = BASE_PREVIEW.evidence_projection.map((row) =>
    row.key === "legal_actions"
      ? { ...row, value: test.legal.join(", ") || "none" }
      : row);
  apiIf = async () => ({
    ...BASE_PREVIEW,
    legal_actions: test.legal,
    evidence_projection: projection,
  });
  const host = new FakeElement("div");
  ifRecoveryControls(host, { shell_id: 3, shortname: "S3" }, {});
  await button(host, "Preview recovery").onclick();
  const labels = all(host, (node) => node.tagName === "button")
    .map((node) => node.textContent);
  if (test.present) invariant(labels.includes(test.present),
    `missing server-listed ${test.present}: ${labels}`);
  invariant(!labels.includes(test.absent),
    `rendered unlisted ${test.absent}: ${labels}`);
  invariant(host.textContent.includes("session #9 · generation 1"),
    "session identity missing from diagnostics");
  invariant(host.textContent.includes("harness claude"),
    "session harness missing from diagnostics");
  invariant(host.textContent.includes("PID 4321 · start ticks 999 · PGID 4321"),
    "exact process identity missing from diagnostics");
  invariant(host.textContent.includes("generation 1 · open · last hook 7"),
    "generation evidence missing from diagnostics");
  invariant(host.textContent.includes("archive #12 · open · active"),
    "archive evidence missing from diagnostics");
  invariant(host.textContent.includes("binding #15 · sprint doc #31"),
    "sprint binding evidence missing from diagnostics");
  invariant(host.textContent.includes(
    "socket /run/if/tmux.sock · session sc-S3 · window chat · pane %1"),
    "tmux evidence missing from diagnostics");
  invariant(host.textContent.includes("2 · left unread"),
    "unread-message evidence missing from diagnostics");
  invariant(host.textContent.includes("not clean · 1 tracked · 2 untracked"),
    "worktree cleanliness missing from diagnostics");
  invariant(host.textContent.includes("branch fix/x"),
    "git branch missing from diagnostics");
  if (!test.legal.length) invariant(
    host.textContent.includes("server lists no legal recovery action"),
    "empty legal-action explanation missing");
}
""")


def test_browser_and_cli_render_identical_canonical_recovery_evidence():
    browser_output = run_recovery_js(r"""
confirm = () => false;
prompt = () => null;
apiIf = async () => BASE_PREVIEW;
const host = new FakeElement("div");
ifRecoveryControls(host, { shell_id: 3, shortname: "S3" }, {});
await button(host, "Preview recovery").onclick();
const rendered = all(host, (node) => Boolean(node.recoveryEvidenceKey))
  .map((node) => [
    node.recoveryEvidenceKey,
    node.recoveryEvidenceLabel,
    node.recoveryEvidenceValue,
  ]);
console.log(JSON.stringify(rendered));
""")
    browser_rows = json.loads(browser_output)

    cli_output = io.StringIO()
    with contextlib.redirect_stdout(cli_output):
        ic._print_recovery_preview(BASE_PREVIEW)
    cli_rows = []
    by_label = {
        row["label"]: (row["key"], row["label"], row["value"])
        for row in BASE_PREVIEW["evidence_projection"]
    }
    for line in cli_output.getvalue().splitlines():
        if not line.startswith("  "):
            continue
        label, value = line.strip().split(": ", 1)
        key, _, _ = by_label[label]
        cli_rows.append([key, label, value])

    expected = [
        [row["key"], row["label"], row["value"]]
        for row in BASE_PREVIEW["evidence_projection"]
    ]
    assert browser_rows == cli_rows == expected


def test_recovery_preview_execute_happy_path_uses_opaque_observation():
    run_recovery_js(r"""
const calls = [];
let rendered = 0;
let confirmText = "";
confirm = (text) => { confirmText = text; return true; };
prompt = () => null;
renderInterface = async () => { rendered += 1; };
apiIf = async (path, method = "GET", body) => {
  calls.push({ path, method, body });
  if (method === "GET") return BASE_PREVIEW;
  return { availability: "available" };
};
const host = new FakeElement("div");
ifRecoveryControls(host, { shell_id: 3, shortname: "S3" }, {});
await button(host, "Preview recovery").onclick();
await button(host, "Recover").onclick();
invariant(calls.length === 2, `expected GET+POST, got ${calls.length}`);
invariant(calls[1].path === "/interface/shells/3/recovery",
  `wrong POST path: ${calls[1].path}`);
invariant(calls[1].body.observation_id === "obs-1",
  "opaque observation id was not forwarded");
invariant(calls[1].body.mode === "recover", "wrong recovery mode");
invariant(calls[1].body.preserve_worktree === true,
  "ordinary recovery did not preserve the worktree");
invariant(!("confirm_force" in calls[1].body),
  "ordinary recovery carried force confirmation");
invariant(confirmText.includes("S3") && confirmText.includes("session #9") &&
  confirmText.includes("PID 4321") && confirmText.includes("worktree not clean"),
  `confirmation omitted scoped diagnostics: ${confirmText}`);
invariant(rendered === 0,
  `result was discarded by an automatic rerender: ${rendered}`);
invariant(host.textContent.includes("Recovery result") &&
  host.textContent.includes("Worktree preserved"),
  `successful result was not retained: ${host.textContent}`);
await button(host, "Refresh shell state").onclick();
invariant(rendered === 1, `explicit refresh did not rerender: ${rendered}`);
""")


def test_available_and_no_session_states_can_reach_server_owned_recovery():
    run_recovery_js(r"""
const archiveOnly = {
  ...BASE_PREVIEW,
  classification: "stale_durable_lock",
  legal_actions: ["recover"],
  evidence_projection: BASE_PREVIEW.evidence_projection.map((row) => {
    const replacements = {
      classification: "stale_durable_lock",
      session: "no Interface session",
      generation: "no generation record",
      sprint_binding: "no armed sprint binding",
      process: "no recorded process identity",
      tmux: "no tmux relation",
    };
    return replacements[row.key] === undefined
      ? row : { ...row, value: replacements[row.key] };
  }),
  evidence: {
    ...BASE_PREVIEW.evidence,
    session: null,
    generation: null,
    sprint_binding: null,
    process: {
      pane_id: null, pane_pid: null, pane_start_ticks: null,
      pane_present: null, pid_state: "none", pgid: null,
    },
    tmux: null,
  },
};
confirm = () => false;
prompt = () => null;
apiIf = async (path) => path === "/interface/shells"
  ? { shells: [{
      shell_id: 3, shortname: "S3", display_name: "Shell 3",
      availability: "available", session_id: null,
    }] }
  : archiveOnly;
const root = new FakeElement("div");
await renderInterface(root);
const availablePreview = button(root, "Preview recovery");
invariant(Boolean(availablePreview),
  "available/archive-only route omitted shell recovery preview");
await availablePreview.onclick();
invariant(Boolean(button(root, "Recover")),
  "available/archive-only route hid the server-listed recovery action");
invariant(root.textContent.includes("no Interface session") &&
  root.textContent.includes("no generation record") &&
  root.textContent.includes("no armed sprint binding") &&
  root.textContent.includes("no recorded process identity") &&
  root.textContent.includes("no tmux relation"),
  `absent recovery evidence was not rendered truthfully: ${root.textContent}`);

const residual = {
  ...archiveOnly,
  classification: "exact_idle_orphan",
  evidence_projection: archiveOnly.evidence_projection.map((row) =>
    row.key === "classification"
      ? { ...row, value: "exact_idle_orphan" }
      : row),
  evidence: {
    ...BASE_PREVIEW.evidence,
    live_session: false,
  },
};
const requested = [];
apiIf = async (path) => {
  requested.push(path);
  return residual;
};
const pane = new FakeElement("div");
await ifRecoveryPane(pane, {
  shell_id: 3, shortname: "S3", display_name: "Shell 3",
  availability: "unreconciled", session_id: null,
}, root);
const orphanPreview = button(pane, "Preview recovery");
invariant(Boolean(orphanPreview),
  "unreconciled/no-session route omitted shell recovery preview");
await orphanPreview.onclick();
invariant(Boolean(button(pane, "Recover")),
  "unreconciled/no-session route hid the server-listed recovery action");
invariant(!requested.some((path) => path.includes("/sessions/")),
  `no-session route fetched invalid session detail: ${requested}`);
""")


def test_recovery_partial_result_keeps_exact_remediation_until_refresh():
    run_recovery_js(r"""
let rendered = 0;
confirm = () => true;
prompt = () => "S3";
renderInterface = async () => { rendered += 1; };
apiIf = async (path, method = "GET") => {
  if (method === "GET") return BASE_PREVIEW;
  return {
    shell_id: 3, shortname: "S3",
    classification: "exact_idle_orphan", mode: "recover",
    signaled: null,
    closed: {
      session: {
        session_id: 9, end_reason: "operator_recovery",
        already_ended: false,
      },
      archive: { archive_id: 12, closed: true },
      binding: null,
      alerts_resolved: 3,
      parked: [{
        binding_id: 15,
        next_action: "review sprint delivery and release binding manually",
      }],
    },
    worktree: {
      worktree: "/x/s3", discarded: false, completed: ["reset"],
      failed: { step: "clean", error: "fatal: clean boom" },
    },
    unread_messages: 2,
    availability: "available",
  };
};
const host = new FakeElement("div");
ifRecoveryControls(host, { shell_id: 3, shortname: "S3" }, {});
await button(host, "Preview recovery").onclick();
const discard = all(host, (node) =>
  node.tagName === "input" && node.type === "checkbox")[0];
discard.checked = true;
await button(host, "Recover").onclick();
invariant(rendered === 0,
  "partial recovery result was swallowed by an automatic refresh");
invariant(host.textContent.includes(
  "Parked ambiguous binding #15: review sprint delivery and release binding manually"),
  `parked-binding remediation missing: ${host.textContent}`);
invariant(host.textContent.includes(
  "completed [reset], failed at clean (fatal: clean boom)"),
  `per-step discard remediation missing: ${host.textContent}`);
invariant(host.textContent.includes("Durable closure is committed"),
  "partial-success boundary was not explained");
await button(host, "Refresh shell state").onclick();
invariant(rendered === 1, "explicit refresh did not rerender");
""")


def test_stale_recovery_observation_repreviews_without_replaying_action():
    run_recovery_js(r"""
const calls = [];
const fresh = {
  ...BASE_PREVIEW,
  observation_id: "obs-2",
  classification: "verified_live",
  legal_actions: ["force"],
  evidence_projection: BASE_PREVIEW.evidence_projection.map((row) => {
    if (row.key === "classification")
      return { ...row, value: "verified_live" };
    if (row.key === "legal_actions")
      return { ...row, value: "force" };
    return row;
  }),
};
let previews = 0;
confirm = () => true;
prompt = () => null;
renderInterface = async () => {};
apiIf = async (path, method = "GET", body) => {
  calls.push({ path, method, body });
  if (method === "GET") return previews++ === 0 ? BASE_PREVIEW : fresh;
  const error = new Error("changed");
  error.status = 409;
  error.code = "recovery_observation_stale";
  throw error;
};
const host = new FakeElement("div");
ifRecoveryControls(host, { shell_id: 3, shortname: "S3" }, {});
await button(host, "Preview recovery").onclick();
await button(host, "Recover").onclick();
invariant(calls.map((call) => call.method).join(",") === "GET,POST,GET",
  `stale flow replayed or failed to preview: ${JSON.stringify(calls)}`);
invariant(!button(host, "Recover"), "stale action remained rendered");
invariant(Boolean(button(host, "Force recover")),
  "fresh server-listed action was not rendered");
invariant(host.textContent.includes("Review this fresh preview before acting"),
  "fresh-preview notice missing");
""")


def test_force_and_discard_require_independent_scoped_confirmations():
    run_recovery_js(r"""
const preview = {
  ...BASE_PREVIEW,
  classification: "verified_live",
  legal_actions: ["force"],
  evidence_projection: BASE_PREVIEW.evidence_projection.map((row) => {
    if (row.key === "classification")
      return { ...row, value: "verified_live" };
    if (row.key === "legal_actions")
      return { ...row, value: "force" };
    return row;
  }),
};
const calls = [];
let allowForce = false;
let typed = "WRONG";
let confirmText = "";
confirm = (text) => { confirmText = text; return allowForce; };
prompt = () => typed;
renderInterface = async () => {};
apiIf = async (path, method = "GET", body) => {
  calls.push({ path, method, body });
  return method === "GET" ? preview : { availability: "available" };
};
const host = new FakeElement("div");
ifRecoveryControls(host, { shell_id: 3, shortname: "S3" }, {});
await button(host, "Preview recovery").onclick();
const force = button(host, "Force recover");
await force.onclick();
invariant(calls.length === 1, "force POST escaped the scoped confirmation");

allowForce = true;
const discard = all(host, (node) =>
  node.tagName === "input" && node.type === "checkbox")[0];
discard.checked = true;
await force.onclick();
invariant(calls.length === 1,
  "discard POST escaped the exact-shortname confirmation");
invariant(host.textContent.includes("confirmation must exactly match S3"),
  "discard mismatch was not explained");

typed = "S3";
await force.onclick();
invariant(calls.length === 2, `expected one confirmed POST, got ${calls.length}`);
const body = calls[1].body;
invariant(body.mode === "force" && body.confirm_force === true,
  `force confirmation missing: ${JSON.stringify(body)}`);
invariant(body.preserve_worktree === false &&
  body.discard_worktree === true && body.confirm_shortname === "S3",
  `discard escalation incomplete: ${JSON.stringify(body)}`);
invariant(confirmText.includes("PID 4321") &&
  confirmText.includes("start ticks 999"),
  "force confirmation did not name exact verified identity");
""")
