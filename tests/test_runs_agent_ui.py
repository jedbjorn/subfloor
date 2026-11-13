"""The Runs view for subagents and watches (Feature #96, spec #273 "GUI surface").

Two halves. The API half runs the real server over a temporary ledger: the
`limits` object of the run list and the labels of `agent-<run_id>` worktrees.
The page half runs the real row code of app.js under node against a minimal
fake DOM: time elapsed and remaining, the Result action, a continued subagent
folded into one thread with its tokens, watch rows, the limits line, and the
note on a lost run whose harness process was left running.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))

import job
import runs
from job_agent_fixtures import DelegationCase
from test_gui_drawer_models import FAKE_DOM

APP = (ENGINE / "ui" / "app.js").read_text()
STYLE = (ENGINE / "ui" / "style.css").read_text()
SERVER = (ENGINE / "api" / "server.py").read_text()
ROWS = APP[APP.index("// Runs use the same rows in the shell drawer"):
           APP.index("function renderProcessTile(")]
FLEET = APP[APP.index("async function renderRunsFleet("):
            APP.index("async function renderConversionStatus(")]
WORKTREES = APP[APP.index("async function renderWorktrees("):
                APP.index("// ── Settings drawer: Tokens, Models, Engine")]

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is required")


# ── API ──────────────────────────────────────────────────────────────────────

class RunLimitsTest(DelegationCase, unittest.TestCase):
    def agent(self, key: str) -> dict:
        return job._api("POST", "/_sc/runs", {
            "registration_key": key, "kind": "agent", "argv": ["x"], "commit": self.base,
            "agent": {"harness": "codex", "requested_model": "m", "brief_bytes": 1}})

    def watch(self, key: str) -> dict:
        return job._api("POST", "/_sc/runs", {
            "registration_key": key, "kind": "probe", "cwd": str(self.root),
            "argv": ["sc", "job", "watch", "timer", "--after", "60"], "timeout_seconds": 60})

    def test_the_list_states_live_runs_against_their_limits(self):
        empty = job._api("GET", "/_sc/runs")["limits"]
        self.assertEqual(empty, {
            "agents": {"per_shell": 3, "per_install": 6, "install_live": 0, "shell_live": 0},
            "watchers": {"per_shell": 10, "install_live": 0, "shell_live": 0}})
        first, second = self.agent("a"), self.agent("b")
        self.watch("w")
        # A command job is neither a subagent nor a watch.
        job._api("POST", "/_sc/runs", {
            "registration_key": "j", "argv": ["true"], "cwd": str(self.root)})
        listed = job._api("GET", "/_sc/runs?kind=agent")
        self.assertEqual([row["run_id"] for row in listed["runs"]],
                         [second["run_id"], first["run_id"]])
        self.assertEqual(listed["limits"]["agents"]["shell_live"], 2)
        self.assertEqual(listed["limits"]["agents"]["install_live"], 2)
        self.assertEqual(listed["limits"]["watchers"]["shell_live"], 1)
        # A shell is told its own count and the install's, not who holds the rest.
        self.assertNotIn("by_shell", listed["limits"]["agents"])
        # A finished run frees its slot.
        job._api("POST", f"/_sc/runs/{first['run_id']}/terminal", {
            "state": "killed", "finished_at": "2026-11-12T10:00:00Z"})
        self.assertEqual(job._api("GET", "/_sc/runs")["limits"]["agents"]["shell_live"], 1)

    def test_the_fleet_reading_names_each_shells_count(self):
        self.con.execute(
            "INSERT INTO shells (shell_id, display_name, shortname, system_prompt, user_id, "
            "api_key) VALUES (2, 'Dev Two', 'dev2', 'x', 1, 'second-token')")
        self.con.commit()
        self.agent("a")
        for key in ("b", "c"):
            self.store.register(2, {
                "registration_key": key, "kind": "agent", "argv": ["x"], "commit": self.base,
                "agent": {"harness": "codex", "requested_model": "m", "brief_bytes": 1}})
        fleet = self.store.limits(fleet=True)
        self.assertEqual(fleet["agents"], {
            "per_shell": 3, "per_install": 6, "install_live": 3, "by_shell": {"1": 1, "2": 2}})
        self.assertEqual(fleet["watchers"]["by_shell"], {})
        one = self.store.limits(2, fleet=True)
        self.assertEqual((one["agents"]["shell_live"], one["agents"]["install_live"]), (2, 3))
        # It counts as registration counts: a run proven to have no process holds no slot.
        gone = self.con.execute("SELECT max(run_id) FROM runs").fetchone()[0]
        self.con.execute(
            "UPDATE runs SET state='running', supervisor_pid=1, supervisor_start_ticks=1, "
            "boot_id=? WHERE run_id=?", (runs.boot_id(), gone))
        self.con.commit()
        self.assertIn(gone, self.store._agents_without_a_process())
        self.assertEqual(self.store.limits(2)["agents"]["shell_live"], 1)

    def test_a_subagent_worktree_is_labelled_with_its_run_and_launching_shell(self):
        first = self.agent("a")
        # A finished first run with a session, then a continue in its worktree.
        job._api("POST", f"/_sc/runs/{first['run_id']}/facts", {"session_ref": "s-1"})
        job._api("POST", f"/_sc/runs/{first['run_id']}/terminal", {
            "state": "killed", "finished_at": "2026-11-12T10:00:00Z"})
        again = job._api("POST", "/_sc/runs", {
            "registration_key": "again", "kind": "agent", "argv": ["x"], "commit": self.base,
            "agent": {"harness": "codex", "requested_model": "m", "brief_bytes": 1,
                      "continues_run_id": first["run_id"]}})
        self.assertEqual(again["cwd"], first["cwd"])
        worktrees = [
            {"abs": first["cwd"], "path": f".sc-worktrees/agent-{first['run_id']}"},
            {"abs": str(Path(first["cwd"]).with_name("agent-900")), "path": "x"},
            {"abs": str(Path(first["cwd"]).with_name("dev1")), "path": ".sc-worktrees/dev1"},
            {"abs": str(Path(first["cwd"]).with_name("agent-smith")), "path": "y"},
            {"abs": str(self.root), "path": "."},
        ]
        self.store.label_worktrees(worktrees)
        self.assertEqual(worktrees[0]["agent"], {
            "run_id": first["run_id"], "known": True, "label": "codex:m",
            "owner_shell_id": 1, "owner": "dev1", "harness": "codex",
            "requested_model": "m", "latest_run_id": again["run_id"], "state": "registered"})
        self.assertEqual(worktrees[1]["agent"], {"run_id": 900, "known": False})
        for entry in worktrees[2:]:
            self.assertNotIn("agent", entry)

    def test_a_stray_directory_named_for_a_continue_is_not_that_runs_worktree(self):
        # Run N is a continue: it works in agent-M, the worktree of its first
        # run M. A directory agent-N, left by anything else, carries N's number
        # but is not where N works, and is not labelled as N's.
        first = self.agent("m")
        job._api("POST", f"/_sc/runs/{first['run_id']}/facts", {"session_ref": "s-1"})
        job._api("POST", f"/_sc/runs/{first['run_id']}/terminal", {
            "state": "done", "exit_code": 0, "finished_at": "2026-11-12T10:00:00Z"})
        again = job._api("POST", "/_sc/runs", {
            "registration_key": "n", "kind": "agent", "argv": ["x"], "commit": self.base,
            "agent": {"harness": "codex", "requested_model": "m", "brief_bytes": 1,
                      "continues_run_id": first["run_id"]}})
        self.assertNotEqual(again["run_id"], first["run_id"])
        stray = Path(first["cwd"]).with_name(f"agent-{again['run_id']}")
        self.assertNotEqual(str(stray), again["cwd"])
        worktrees = [{"abs": str(stray), "path": f".sc-worktrees/{stray.name}"}]
        self.store.label_worktrees(worktrees)
        self.assertEqual(worktrees[0]["agent"], {"run_id": again["run_id"], "known": False})

    def test_the_worktrees_route_labels_what_it_lists(self):
        route = SERVER[SERVER.index('== "/api/git-state":'):]
        route = route[:route.index("# Rolling webapp event log")]
        self.assertIn("state = git_hygiene.compute(fetch=fetch)", route)
        self.assertIn('RunStore(con, ENGINE).label_worktrees(state["worktrees"])', route)
        self.assertIn("return self._send(200, state)", route)


# ── Page ─────────────────────────────────────────────────────────────────────

PAGE = FAKE_DOM + r"""
const calls = [], modals = [], toasts = [];
let responses = {};
async function api(path, method = "GET", body) {
  calls.push([method, path]);
  if (!(path in responses)) throw new Error("no fixture for " + path);
  return responses[path];
}
const openModal = (options) => { modals.push(options); return () => {}; };
const toast = (message) => toasts.push(message);
const confirm = () => true;
const navigator = { clipboard: { writeText: async () => {} } };
let intervals = 0;
globalThis.setInterval = () => { intervals++; return 1; };
globalThis.clearInterval = () => {};
""" + ROWS + r"""
const NOW = Date.parse("2026-11-12T12:00:00Z");
Date.now = () => NOW;
const shells = [{shell_id: 1, shortname: "PLN1"}, {shell_id: 2, shortname: "DEV1"}];
const agent = (id, extra = {}) => ({
  harness: "codex", requested_model: "gpt-6-luna", requested_effort: null,
  effective_effort: "default", worktree: "/repo/.sc-worktrees/agent-" + id,
  continues_run_id: null, usage_json: null, ...extra});
const run = (run_id, extra = {}) => ({
  run_id, owner_shell_id: 1, kind: "job", label: "run " + run_id, state: "done",
  exit_code: 0, wake_state: "consumed", argv: ["true"], timeout_seconds: null,
  created_at: "2026-11-12 11:00:00", started_at: "2026-11-12T11:00:00+00:00",
  finished_at: "2026-11-12T11:10:00+00:00", terminal_json: null, last_error: null, ...extra});
const byClass = (node, name) => all(node, (n) => String(n.className).split(" ").includes(name));
const buttons = (node) => all(node, (n) => n.tagName === "BUTTON").map(text);
const lines = (node) => byClass(node, "run-detail").map(text);
"""


def run_page(script: str) -> dict:
    proc = subprocess.run(
        ["node", "-e", PAGE + script], text=True, capture_output=True, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@needs_node
def test_time_elapsed_and_remaining_come_from_the_start_and_the_limit():
    out = run_page(r"""
const live = run(1, {state: "running", finished_at: null, timeout_seconds: 7200,
                     started_at: "2026-11-12T11:48:00+00:00"});
console.log(JSON.stringify({
  live: runClock(live),
  later: runClock(live, NOW + 60000),
  over: runClock({...live, timeout_seconds: 600}),
  unbounded: runClock({...live, timeout_seconds: null}),
  registered: runClock(run(2, {state: "registered", started_at: null, finished_at: null,
                               created_at: "2026-11-12 11:59:30", timeout_seconds: 28800})),
  finished: runClock(run(3, {timeout_seconds: 7200})),
  finishedUnbounded: runClock(run(4)),
  noEnd: runClock(run(5, {state: "lost", finished_at: null, timeout_seconds: 7200})),
  spans: [0, 59, 60, 3599, 3600, 7200, 28800, 86400].map(runSpan),
}));
""")
    assert out == {
        "live": "elapsed 12m00s · 1h48m left of 2h00m",
        "later": "elapsed 13m00s · 1h47m left of 2h00m",
        "over": "elapsed 12m00s · past its limit of 10m00s",
        "unbounded": "elapsed 12m00s · no time limit",
        "registered": "elapsed 30s · 7h59m left of 8h00m",
        "finished": "ran 10m00s of 2h00m limit",
        "finishedUnbounded": "ran 10m00s",
        "noEnd": "run time not recorded · limit 2h00m",
        "spans": ["0s", "59s", "1m00s", "59m59s", "1h00m", "2h00m", "8h00m", "24h00m"],
    }


@needs_node
def test_a_subagent_row_shows_its_route_its_time_and_a_result_action():
    out = run_page(r"""
(async () => {
  const host = el("div");
  const live = run(7, {kind: "agent", label: "check spec", state: "running", exit_code: null,
    wake_state: "none", finished_at: null, timeout_seconds: 7200,
    started_at: "2026-11-12T11:48:00+00:00", agent: agent(7, {effective_effort: "low"})});
  const done = run(6, {kind: "agent", timeout_seconds: 3600,
    agent: agent(6, {usage_json: {input_tokens: 900, output_tokens: 100, cache_read_tokens: 5000}})});
  renderRunRows(host, [live, done, run(5)], shells, async () => {});
  const rows = byClass(host, "run-row");
  responses = {"/runs/6/result": {run_id: 6, state: "done", exit_code: 0, available: true,
      text: "<b>final</b> message", truncated: true, result_bytes: 400000, agent: done.agent},
    "/runs/7/result": {run_id: 7, state: "running", exit_code: null, available: false,
      text: "", truncated: false, result_bytes: null, agent: live.agent}};
  for (const row of rows.slice(0, 2))
    await all(row, (n) => n.tagName === "BUTTON" && text(n) === "Result")[0].onclick();
  console.log(JSON.stringify({
    rows: rows.map((row) => ({lines: lines(row), buttons: buttons(row)})),
    intervals,
    modals: modals.map((m) => ({title: m.title, text: text(m.bodyNode),
      pre: byClass(m.bodyNode, "run-log").map((n) => n.tagName + ":" + text(n))})),
    calls,
  }));
})();
""")
    live, done, plain = out["rows"]
    assert live["lines"] == [
        "codex · gpt-6-luna · effort low",
        "elapsed 12m00s · 1h48m left of 2h00m",
        "Input + output tokens: not read yet (they arrive with a usage sweep)",
    ]
    assert live["buttons"] == ["Tail", "Result", "Copy status command", "Kill"]
    assert done["lines"] == [
        "codex · gpt-6-luna · effort default", "ran 10m00s of 1h00m limit", "Input + output tokens: 1000"]
    assert done["buttons"] == ["Tail", "Result", "Copy status command", "Attach receipt"]
    # A command job has the time line and no Result action.
    assert plain["lines"] == ["ran 10m00s"]
    assert plain["buttons"] == ["Tail", "Copy status command", "Attach receipt"]
    # Only the live row asks for a clock tick.
    assert out["intervals"] == 1
    assert out["calls"] == [["GET", "/runs/7/result"], ["GET", "/runs/6/result"]]
    waiting, finished = out["modals"]
    assert waiting["title"] == "Result of run #7 · check spec"
    assert waiting["pre"] == ["PRE:No result is recorded yet."]
    assert finished["pre"] == ["PRE:<b>final</b> message"]   # text, never markup
    assert "done · exit 0 · codex · gpt-6-luna · effort default · 400000 bytes" in finished["text"]
    assert "Truncated: the first 256 KiB are shown." in finished["text"]


@needs_node
def test_a_continued_subagent_is_one_thread_with_its_tokens_on_the_newest_row():
    out = run_page(r"""
(async () => {
  const first = run(10, {kind: "agent", agent: agent(10, {usage_json: {input_tokens: 700, output_tokens: 50}})});
  const second = run(12, {kind: "agent", agent: agent(10, {continues_run_id: 10})});
  const third = run(15, {kind: "agent", state: "running", finished_at: null, exit_code: null,
    timeout_seconds: 7200, agent: agent(10, {continues_run_id: 12})});
  const other = run(14, {kind: "agent", agent: agent(14)});
  const host = el("div");
  renderRunRows(host, [third, other, run(13), second, first], shells, async () => {});
  const top = host.children.map((n) => n.tagName + "." + n.className
    + (n.tagName === "ARTICLE" ? "#" + n.ariaLabel : ""));
  const thread = byClass(host, "run-thread")[0];
  // The same thread when its first run lies beyond the rows the list loaded.
  responses = {"/runs/10": first};
  const cut = el("div");
  renderRunRows(cut, [third, second], shells, async () => {});
  const before = byClass(cut, "run-tokens").map(text);
  await tick();
  console.log(JSON.stringify({
    top, summary: text(all(thread, (n) => n.tagName === "SUMMARY")[0]),
    folded: byClass(thread, "run-row").map((n) => n.ariaLabel + "|" + n.className),
    head: lines(host.children[0]),
    foldedTokens: byClass(thread, "run-tokens").map(text),
    basis: byClass(host, "run-tokens").map((n) => n.title),
    otherTokens: byClass(host.children[2], "run-tokens").map(text),
    threads: runThreads([third, other, run(13), second, first]).map((t) => [t.head.run_id, t.earlier.map((r) => r.run_id)]),
    before, after: byClass(cut, "run-tokens").map(text), calls,
  }));
})();
""")
    assert out["threads"] == [[15, [12, 10]], [14, []], [13, []]]
    assert out["top"] == [
        "ARTICLE.run-row#Run 15", "DETAILS.run-thread", "ARTICLE.run-row#Run 14",
        "ARTICLE.run-row#Run 13"]
    assert out["summary"] == "2 earlier runs of this subagent"
    assert out["folded"] == ["Run 12|run-row run-row-earlier", "Run 10|run-row run-row-earlier"]
    assert out["head"][0] == "codex · gpt-6-luna · effort default · continues run #12"
    # The basis is input + output, said on the row and in its tooltip.
    assert out["head"][-1] == "Input + output tokens: 750 for this thread, counted on run #10"
    assert set(out["basis"]) == {
        "Input plus output tokens, as the usage sweep read them. Cache reads and cache "
        "writes are not included here; the Analytics tab counts them as well."}
    # The folded first run shows the same figure, labelled as the thread's; the
    # middle run has none.
    assert out["foldedTokens"] == [
        "Input + output tokens: 750 for this thread, counted on run #10"]
    assert out["otherTokens"] == [
        "Input + output tokens: not read yet (they arrive with a usage sweep)"]
    # Beyond the loaded rows the first run is read once, and the thread shows its tokens.
    assert out["before"] == ["Input + output tokens: not read yet (they arrive with a usage sweep)"]
    assert out["after"] == ["Input + output tokens: 750 for this thread, counted on run #10"]
    assert out["calls"] == [["GET", "/runs/10"]]


@needs_node
def test_watch_rows_show_form_condition_last_attempt_and_time():
    out = run_page(r"""
(async () => {
  const pr = run(20, {kind: "probe", label: "PR #1786", state: "running", exit_code: null,
    finished_at: null, timeout_seconds: 3600, started_at: "2026-11-12T11:50:00+00:00",
    argv: ["sc", "job", "watch", "pr", "--pr", "1786", "--repository", "owner/name"]});
  const timer = run(21, {kind: "probe", state: "running", exit_code: null, finished_at: null,
    timeout_seconds: 600, started_at: "2026-11-12T11:58:00+00:00",
    argv: ["sc", "job", "watch", "timer", "--after", "600"]});
  const until = run(22, {kind: "probe", timeout_seconds: 3600, exit_code: 0,
    argv: ["/bin/sh", "-c", "test -f ready"],
    terminal_json: JSON.stringify({state: "done", exit_code: 0,
      probe_attempt: {number: 4, exit_code: 0, excerpt: "ready\n"}})});
  const silent = run(23, {kind: "probe", state: "killed", exit_code: null,
    argv: ["/bin/sh", "-c", "false"], terminal_json: JSON.stringify({state: "killed"})});
  responses = {"/runs/20/tail": {text:
    '{"number": 1, "exit_code": 1, "excerpt": "pending at head abc"}\n'
    + '{"number": 2, "exit_code": 1, "excerpt": "pending at head def\\n"}\n'}};
  const host = el("div");
  renderRunRows(host, [pr, timer, until, silent], shells, async () => {});
  await tick();
  console.log(JSON.stringify({
    rows: byClass(host, "run-row").map(lines), calls,
    forms: [pr, timer, until, run(24)].map(runWatch),
    buttons: buttons(byClass(host, "run-row")[0]),
  }));
})();
""")
    assert out["rows"] == [
        ["watch pr · PR #1786 in owner/name: checks settle (green or red), or it is merged or closed",
         "elapsed 10m00s · 50m00s left of 1h00m",
         "Last attempt 2: exit 1 · pending at head def"],
        ["watch timer · 600 seconds pass", "elapsed 2m00s · 8m00s left of 10m00s"],
        ["watch until · test -f ready", "ran 10m00s of 1h00m limit",
         "Last attempt 4: exit 0 · ready"],
        ["watch until · false", "ran 10m00s", "No attempt was recorded."],
    ]
    # Only the live watch that has attempts reads its log.
    assert out["calls"] == [["GET", "/runs/20/tail"]]
    assert [form and form["form"] for form in out["forms"]] == ["pr", "timer", "until", None]
    assert out["buttons"] == ["Tail", "Copy status command", "Kill"]


@needs_node
def test_the_limits_are_stated_once_and_a_list_at_its_limit_says_so():
    out = run_page(r"""
const say = (limits, shell) => {
  const host = el("div");
  renderRunRows(host, [], shells, async () => {}, {limits, shell});
  return host.children.map((n) => n.className + "|" + text(n));
};
const limits = (agents, watchers) => ({
  agents: {per_shell: 3, per_install: 6, ...agents}, watchers: {per_shell: 10, ...watchers}});
console.log(JSON.stringify({
  shell: say(limits({shell_live: 2, install_live: 4}, {shell_live: 1, install_live: 3}), shells[0]),
  shellFull: say(limits({shell_live: 3, install_live: 6}, {shell_live: 10, install_live: 10}), shells[0]),
  fleet: say(limits({install_live: 4, by_shell: {"1": 3, "2": 1}}, {install_live: 11, by_shell: {"2": 10, "1": 1}}), null),
  absent: say(undefined, shells[0]),
  withRows: (() => { const host = el("div");
    renderRunRows(host, [run(1)], shells, async () => {}, {limits: limits({shell_live: 0, install_live: 0}, {shell_live: 0, install_live: 0}), shell: shells[1]});
    return host.children.map((n) => n.className); })(),
}));
""")
    assert out["shell"] == [
        "run-limits|Subagents: 2 of 3 live for PLN1 · 4 of 6 on this install. "
        "Watchers: 1 of 10 live for PLN1.",
        "muted|No runs."]
    assert out["shellFull"][0] == (
        "run-limits run-limits-full|Subagents: 3 of 3 live for PLN1 · 6 of 6 on this install. "
        "Watchers: 10 of 10 live for PLN1. PLN1 is at its limit of 3 live subagents. "
        "PLN1 is at its limit of 10 live watchers. "
        "This install is at its limit of 6 live subagents. "
        "A further launch is refused until one ends.")
    assert out["fleet"][0] == (
        "run-limits run-limits-full|Subagents: 4 of 6 live on this install; one shell may run 3. "
        "Watchers: 11 live on this install; one shell may run 10. "
        "PLN1 is at its limit of 3 live subagents. DEV1 is at its limit of 10 live watchers. "
        "A further launch is refused until one ends.")
    assert out["absent"] == ["muted|No runs."]
    assert out["withRows"] == ["run-limits", "run-row"]


@needs_node
def test_a_lost_run_says_what_the_engine_found_of_its_harness_on_its_row():
    out = run_page(r"""
const note = "harness process 4242 was running and was left running: it could not be tied to this run's worktree";
const lost = run(30, {kind: "agent", state: "lost", exit_code: null, finished_at: "2026-11-12T11:30:00+00:00",
  timeout_seconds: 7200, agent: agent(30),
  terminal_json: JSON.stringify({state: "lost", finished_at: "x", spawn_error: note})});
const failed = run(31, {kind: "agent", state: "failed", exit_code: 127, agent: agent(31),
  terminal_json: JSON.stringify({state: "failed", exit_code: 127, spawn_error: "subagent not started: no worktree"})});
const blocked = run(32, {kind: "agent", state: "running", wake_state: "blocked", finished_at: null,
  agent: agent(32), last_error: "reconciliation blocked; the engine stopped harness process 4243: its supervisor was gone"});
const quiet = run(33, {kind: "agent", state: "lost", exit_code: null, agent: agent(33),
  terminal_json: JSON.stringify({state: "lost"}), last_error: "kept as a tooltip only"});
const host = el("div");
renderRunRows(host, [quiet, blocked, failed, lost], shells, async () => {});
console.log(JSON.stringify(byClass(host, "run-row").map((row) => byClass(row, "run-note").map(text))));
""")
    assert out == [
        [],
        ["reconciliation blocked; the engine stopped harness process 4243: "
         "its supervisor was gone"],
        ["Reason: subagent not started: no worktree"],
        # What the engine found when it marked the run lost, not a present-tense claim.
        ["When the run was marked lost: harness process 4242 was running and was left "
         "running: it could not be tied to this run's worktree"],
    ]


def test_the_fleet_view_filters_by_agent_and_passes_the_limits():
    assert '["job", "agent", "probe", "devkit"].map((s) => [s, s])' in FLEET
    assert "limits = result.limits;" in FLEET
    assert "{ limits, shell: shells.find((s) => String(s.shell_id) === shell.value) || null }" in FLEET
    # The chat rail splits Jobs from Sub Agents; each lists, counts and states
    # the limits of its own kind for its own shell.
    assert '{ label: "Jobs", className: "jobs-drawer", filter: "exclude_kind=agent", part: "watchers"' in APP
    assert '{ label: "Sub Agents", className: "subagents-drawer", filter: "kind=agent", part: "agents"' in APP
    assert "api(`/runs?shell_id=${shell.shell_id}&${drawer.filter}&limit=${LIST_LIMIT}`)" in APP
    assert "{ limits, shell, part: drawer.part, empty: drawer.empty });" in APP
    # The page offers no control that launches a run.
    assert not [line for line in APP.splitlines() if "/runs" in line and '"POST"' in line
                and "/kill" not in line and "/attach" not in line]


def test_the_worktrees_tab_labels_a_subagent_worktree():
    assert "`subagent run #${w.agent.run_id} · launched by ${w.agent.owner || " in WORKTREES
    assert "`subagent run #${w.agent.run_id} · not in the run ledger`" in WORKTREES
    assert "`latest run #${w.agent.latest_run_id} ${w.agent.state}`" in WORKTREES
    assert "sc job clean ${w.agent.latest_run_id}" in WORKTREES
    for selector in (".run-detail", ".run-note", ".run-limits", ".run-limits-full",
                     ".run-thread", ".wt-agent"):
        assert selector + " " in STYLE or selector + "," in STYLE


if __name__ == "__main__":
    unittest.main()


def test_sc_job_list_names_the_token_basis():
    # The same basis as the run row: input + output, never cache tokens.
    row = {"state": "done", "started_at": "2026-11-12T11:00:00+00:00",
           "timeout_seconds": 7200,
           "agent": {"harness": "codex", "requested_model": "m", "effective_effort": None,
                     "usage_json": {"input_tokens": 700, "output_tokens": 50,
                                    "cache_read_tokens": 90000, "cache_write_tokens": 4000}}}
    assert job._row_detail(row, "agent") == (
        " [codex · m · default · limit 2h00m · 750 input + output tokens]")
    row["agent"]["usage_json"] = None
    assert job._row_detail(row, "agent") == " [codex · m · default · limit 2h00m]"
