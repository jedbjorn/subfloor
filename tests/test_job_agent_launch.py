"""`sc job agent`, `result`, `continue`, `clean` end to end (Feature #96, spec #273).

Hermetic: the real verbs, the real detached supervisor, a real API over a
temporary ledger and real git worktrees, with stand-in harness binaries.

Acceptance criteria covered here: 9 (result, facts and wake together), 10 (a
long final message is bounded), 11 (continue resumes the same session), 12
(stop, timeout and loss reported truthfully), 13 (worktrees removed only when
safe), and the Kimi and OpenCode halves of 17.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))

import job
import job_agent
import runs
from job_agent_fixtures import STUBS, DelegationCase
from test_runs import wait_for

RECORDED = json.loads((STUBS / "harness_controls.json").read_text())["harnesses"]


class LaunchTest(DelegationCase, unittest.TestCase):
    """AC 9: one terminal row, one result, one wake carrying every line of the form."""

    def test_completion_writes_result_facts_and_wake_together(self):
        row = self.launch("RESULT:the final message of the subagent", "--label", "survey",
                          "--effort", "low")
        final = self.finished(row)
        rid = final["run_id"]
        start = self.starts()[0]

        # The harness ran in the engine's worktree, as the launching shell,
        # with the seat marker cleared and the run id set.
        worktree = self.root / ".sc-worktrees" / f"agent-{rid}"
        self.assertEqual(start["cwd"], str(worktree))
        self.assertEqual(start["env"]["SC_JOB_AGENT_RUN"], str(rid))
        self.assertIsNone(start["env"]["SC_SEAT"])
        self.assertTrue(start["env"]["SC_API_TOKEN"])
        self.assertIn("tracked.txt", start["worktree_listing"])
        # The brief arrived on standard input and is in the evidence directory,
        # never in the recorded command.
        self.assertEqual(start["brief"], "RESULT:the final message of the subagent")
        run_dir = Path(final["evidence_path"]).parent
        self.assertEqual((run_dir / "brief.md").read_text(), start["brief"])
        self.assertNotIn("final message", json.dumps(final["argv"]))
        self.assertEqual(start["argv"][:2], ["--model", "model-x"])
        self.assertEqual(start["argv"][start["argv"].index("--effort") + 1], "low")
        session = start["argv"][start["argv"].index("--session-id") + 1]

        # One terminal row.
        self.assertEqual(final["state"], "done")
        self.assertEqual(final["exit_code"], 0)
        self.assertEqual(json.loads(final["terminal_json"])["agent"],
                         {"head_commit": self.base, "uncommitted": False})
        # One result, on disk, with its facts.
        self.assertEqual((run_dir / "result.md").read_text(),
                         "the final message of the subagent")
        facts = final["agent"]
        self.assertEqual(facts["session_ref"], session)
        self.assertEqual(facts["result_bytes"], len("the final message of the subagent"))
        self.assertEqual(facts["effective_effort"], "low")
        self.assertIsNotNone(facts["worktree_removed_at"])
        self.assertFalse(worktree.exists())

        # One wake, every line of the agent form.
        lines = self.wake(final).splitlines()
        self.assertRegex(
            lines[0],
            rf"^Run {rid} \(agent: survey\) done; exit=0; claude · model-x · low; "
            r"ran \d+s of 2h00m\.$")
        self.assertEqual(
            lines[1],
            f"Worktree removed, unchanged (ignored files were not kept); "
            f"detached at {self.base[:12]}; "
            f"HEAD {self.base[:12]}; uncommitted changes: no.")
        self.assertEqual(
            lines[2],
            f"Result: {facts['result_bytes']} bytes. Read it with `sc job result {rid}`; "
            f"log with `sc job tail {rid}`.")
        self.assertEqual(
            lines[3],
            f"Continue with `sc job continue {rid} --brief-file <path>`; "
            f"remove a kept worktree with `sc job clean {rid}`.")
        self.assertEqual(
            lines[4:],
            ["You are accountable for what this subagent did under your brief. "
             "Check its claims",
             "against the branch, the PR and its checks before you rely on them."])
        # No part of the final message is placed in the wake.
        self.assertNotIn("final message of the subagent", "\n".join(lines))

        # A repeated terminal submission is the same outcome and wake.
        again = job._api("POST", f"/_sc/runs/{rid}/terminal", json.loads(final["terminal_json"]))
        self.assertEqual(again["message_id"], final["message_id"])
        self.assertEqual(len(self.wake_bodies(final)), 1)

        # The result verb, plain and structured.
        shown = self.cli("result", str(rid))
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("the final message of the subagent", shown.stdout)
        self.assertIn(f"session_ref: {session}", shown.stdout)
        structured = json.loads(self.cli("result", str(rid), "--json").stdout)
        self.assertEqual(structured["text"], "the final message of the subagent")
        self.assertFalse(structured["truncated"])

    def test_the_verb_reports_what_was_resolved_and_the_list_filters_by_kind(self):
        done = self.cli("agent", "--harness", "claude", "--model", "model-x", "--brief",
                        "SLEEP:30", "--timeout", "600", "--branch", "agent/topic")
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        row = job._api("GET", "/_sc/runs")["runs"][0]
        rid = row["run_id"]
        for part in (f"job: {rid} started", "claude · model-x · effort default",
                     "time limit 600s", f"worktree: {row['cwd']} (agent/topic)",
                     "end your turn; completion wakes you"):
            self.assertIn(part, done.stdout)
        self.assertEqual(row["agent"]["requested_effort"], None)
        self.assertEqual(row["agent"]["effective_effort"], "default")
        self.wait_started()
        self.assertNotIn("--effort", self.starts()[0]["argv"])
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=row["cwd"]),
                         "agent/topic")

        command = self.cli("start", "--label", "plain", "--", "sleep", "30")
        self.assertEqual(command.returncode, 0, command.stderr)
        agents = self.cli("list", "--kind", "agent").stdout
        self.assertRegex(agents, rf"{rid}\s+running .*\[claude · model-x · default · \d+m\d+s left of 10m00s\]")
        self.assertNotIn("plain", agents)
        jobs = self.cli("list", "--kind", "job").stdout
        self.assertIn("plain", jobs)
        self.assertNotIn("claude · model-x", jobs)
        self.assertIn("plain", self.cli("list").stdout)
        self.assertIn("claude · model-x", self.cli("list").stdout)

        self.assertEqual(self.cli("kill", str(rid)).returncode, 0)
        self.assertEqual(self.finished(row)["state"], "killed")

    def test_a_refused_launch_registers_nothing_and_starts_nothing(self):
        cases = {
            "requires --model": ("agent", "--harness", "claude", "--brief", "x"),
            "has no headless adapter": (
                "agent", "--harness", "vibe", "--model", "m", "--brief", "x"),
            "the brief is empty": ("agent", "--harness", "claude", "--model", "m", "--brief", " "),
            "limited to 28800 seconds": (
                "agent", "--harness", "claude", "--model", "m", "--brief", "x",
                "--timeout", "28801"),
            "does not name a commit": (
                "agent", "--harness", "claude", "--model", "m", "--brief", "x",
                "--base", "no-such-ref"),
        }
        for reason, args in cases.items():
            with self.subTest(reason=reason):
                done = self.cli(*args)
                self.assertNotEqual(done.returncode, 0)
                self.assertIn(reason, done.stderr)
        # A subagent's own call carries its run id and is refused as nesting.
        nested = self.cli("agent", "--harness", "claude", "--model", "m", "--brief", "x",
                          env={**self.env, "SC_JOB_AGENT_RUN": "7"})
        self.assertNotEqual(nested.returncode, 0)
        self.assertIn(runs.NESTING_CONFUSION, nested.stderr)
        self.assertEqual(self.newest_id(), 0)
        self.assertEqual(self.starts(), [])
        self.assertFalse((self.root / ".sc-worktrees").exists())

    def test_a_worktree_that_cannot_be_created_ends_the_run_failed(self):
        # Left behind by an earlier run of the number the next run will take.
        (self.root / ".sc-worktrees" / "agent-1").mkdir(parents=True)
        done = self.cli("agent", "--harness", "claude", "--model", "m", "--brief", "x")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("ended before the subagent started", done.stderr)
        self.assertIn("already exists", done.stderr)
        row = self.finished({"run_id": 1})
        self.assertEqual((row["state"], row["exit_code"]), ("failed", 127))
        self.assertIn("Run 1 (agent: claude:m) failed; exit=127", self.wake(row))
        self.assertEqual(self.starts(), [])

    def test_kimi_takes_the_brief_as_an_argument_the_ledger_never_holds(self):
        row = self.finished(self.launch("RESULT:from kimi", "--effort", "low", harness="kimi"))
        self.assertIn("{brief}", row["argv"])
        self.assertNotIn("from kimi", json.dumps(row["argv"]))
        start = self.starts()[0]
        self.assertEqual(start["brief"], "RESULT:from kimi")
        self.assertEqual(start["argv"][start["argv"].index("-p") + 1], "RESULT:from kimi")
        self.assertEqual(start["env"]["KIMI_MODEL_THINKING_EFFORT"], "low")
        self.assertEqual(row["agent"]["session_ref"], "session_stub-0001")
        self.assertEqual(json.loads(self.cli("result", str(row["run_id"]), "--json").stdout)["text"],
                         "from kimi")

    def test_a_failed_run_keeps_the_harness_error_in_the_log(self):
        row = self.finished(self.launch(
            "NORESULT:1\nEXIT:3\nSTDERR:provider rejected the model id"))
        self.assertEqual((row["state"], row["exit_code"]), ("failed", 3))
        self.assertIn("failed; exit=3", self.wake(row))
        self.assertIn("provider rejected the model id", self.cli("tail", str(row["run_id"])).stdout)
        # A run without a final message has a result that says so.
        result = job._api("GET", f"/_sc/runs/{row['run_id']}/result")
        self.assertEqual(result["text"], job_agent.NO_RESULT)
        self.assertEqual(result["result_bytes"], 0)


class GroupEndTest(DelegationCase, unittest.TestCase):
    def test_children_left_by_a_harness_that_exits_on_its_own_are_stopped(self):
        row = self.finished(self.launch("SPAWN:1\nRESULT:finished normally"))
        start = self.starts()[0]
        self.assertEqual((row["state"], row["exit_code"]), ("done", 0))
        # The leader exited by itself; what it left in its group went with it.
        self.assert_gone(start["pid"], start["spawned"])


class ReportReadTest(unittest.TestCase):
    """The log is read a line at a time; the answers match the whole-text readers."""

    def test_one_pass_gives_the_last_message_and_the_first_session(self):
        for harness, recorded in RECORDED.items():
            adapter = json.loads((ENGINE / f"adapters/{harness}/adapter.json").read_text())
            keys = adapter["headless"]["agent"]
            run = recorded["dash_briefs"]["runs"][0] if harness == "kimi" else recorded["launch"]
            with self.subTest(harness=harness), tempfile.TemporaryDirectory() as tmp:
                run_dir = Path(tmp)
                (run_dir / "log").write_text("not json\n" + run["stdout"])
                if "last_message_file" in run:
                    (run_dir / "last-message.md").write_text(run["last_message_file"])
                message, session = job_agent.read_report(
                    keys["result"], keys["session"], run_dir / "log", run_dir, "pinned-id")
                self.assertEqual(message.strip(), "NONCE-7f3a")
                self.assertEqual(session, "pinned-id" if harness == "claude" else run["session_ref"])
                self.assertEqual(
                    message, job_agent.read_result(keys["result"], run["stdout"], run_dir))

    def test_a_document_spread_over_several_lines_is_still_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "log").write_text(json.dumps({"result": "spread"}, indent=2))
            message, session = job_agent.read_report(
                {"source": "stdout_json", "field": "result"}, {"source": "pinned"},
                run_dir / "log", run_dir, None)
            self.assertEqual((message, session), ("spread", None))


class BoundedResultTest(DelegationCase, unittest.TestCase):
    """AC 10: at most 256 KiB is returned, flagged; the file holds everything."""

    def test_a_long_final_message_is_bounded(self):
        size = runs.AGENT_RESULT_MAX + 40000
        row = self.finished(self.launch(f"BIG:{size}"))
        rid = row["run_id"]
        self.assertEqual(
            (Path(row["evidence_path"]).parent / "result.md").stat().st_size, size)
        result = job._api("GET", f"/_sc/runs/{rid}/result")
        self.assertEqual(len(result["text"].encode()), runs.AGENT_RESULT_MAX)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["result_bytes"], size)
        shown = self.cli("result", str(rid))
        self.assertIn("truncated: the first 256 KiB are shown", shown.stdout)
        self.assertIn(f"Result: {size} bytes.", self.wake(row))
        # The database holds no copy of the result.
        columns = {found[1] for found in self.con.execute("PRAGMA table_info(run_agents)")}
        self.assertEqual({name for name in columns if "result" in name}, {"result_bytes"})
        stored = json.dumps([dict(found) for found in self.con.execute(
            "SELECT * FROM runs r JOIN run_agents a USING(run_id) WHERE r.run_id=?", (rid,))])
        self.assertNotIn("r" * 64, stored)
        # Another shell's token reads nothing.
        self.con.execute(
            "INSERT INTO shells(shell_id,display_name,shortname,system_prompt,user_id,api_key) "
            "VALUES(2,'Other','pln1','x',1,'other-token')")
        self.con.commit()
        with unittest.mock.patch.object(job, "SC_API_TOKEN", "other-token"):
            self.assertEqual(self.api_error("GET", f"/_sc/runs/{rid}/result")[0], 403)


class TruthfulEndTest(DelegationCase, unittest.TestCase):
    """AC 12: killed, timeout and lost are what happened; nothing restarts."""

    def test_stop_is_reported_as_killed(self):
        row = self.launch("SLEEP:120\nSPAWN:1")
        start = self.wait_started()[0]
        self.assertEqual(self.cli("kill", str(row["run_id"])).returncode, 0)
        final = self.finished(row)
        self.assertEqual(final["state"], "killed")
        self.assertIn(f"Run {row['run_id']} (agent: claude:model-x) killed;", self.wake(final))
        # No harness process is left behind, and the unchanged worktree is gone.
        self.assert_gone(start["pid"], start["spawned"])
        self.assertIsNotNone(final["agent"]["worktree_removed_at"])
        self.assertEqual(len(self.starts()), 1)

    def test_the_run_time_limit_ends_the_run_as_timeout(self):
        row = self.launch("SLEEP:120\nSPAWN:1\nWRITE:half-written.txt", "--timeout", "2")
        start = self.wait_started()[0]
        final = self.finished(row)
        self.assertEqual(final["state"], "timeout")
        body = self.wake(final)
        self.assertRegex(body, r"timeout; exit=-?\d+; claude · model-x · default; ran \d+s of 2s\.")
        # What it left is kept and stated.
        self.assertIn(f"Worktree {row['cwd']}; ", body)
        self.assertIn("uncommitted changes: yes.", body)
        self.assertTrue((Path(row["cwd"]) / "half-written.txt").exists())
        self.assert_gone(start["pid"], start["spawned"])
        self.assertEqual(len(self.starts()), 1)

    def test_a_supervisor_that_dies_leaves_lost_and_no_harness_process(self):
        row = self.launch("SLEEP:120")
        start = self.wait_started()[0]
        running = wait_for(lambda: (
            r if (r := job._api("GET", f"/_sc/runs/{row['run_id']}"))["pid"] else None))
        os.kill(running["supervisor_pid"], signal.SIGKILL)
        wait_for(lambda: not self.alive(running["supervisor_pid"]))
        # The harness leads its own process group, so it outlived its supervisor.
        self.assertTrue(self.alive(start["pid"]))
        # A slot is still held while the harness is proven alive.
        self.assertEqual(self.store._agents_without_a_process(), frozenset())

        self.store.reconcile()
        final = job._api("GET", f"/_sc/runs/{row['run_id']}")
        self.assertEqual((final["state"], final["exit_code"]), ("lost", None))
        self.assert_gone(start["pid"])
        body = self.wake(final)
        self.assertIn(f"Run {row['run_id']} (agent: claude:model-x) lost; exit=unknown;", body)
        self.assertIn("HEAD unknown; uncommitted changes: unknown.", body)
        self.assertIn("Result: not recorded.", body)
        # Unknown, never success, never restarted.
        self.store.reconcile()
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(self.newest_id(), row["run_id"])
        self.assertEqual(len(self.wake_bodies(final)), 1)

    def test_a_supervisor_told_to_stop_takes_its_harness_with_it(self):
        row = self.launch("SLEEP:120\nSPAWN:1")
        start = self.wait_started()[0]
        running = wait_for(lambda: (
            r if (r := job._api("GET", f"/_sc/runs/{row['run_id']}"))["pid"] else None))
        os.kill(running["supervisor_pid"], signal.SIGTERM)
        final = self.finished(row)
        self.assertEqual(final["state"], "failed")
        self.assertIn("supervisor stopped by signal 15",
                      json.loads(final["terminal_json"])["spawn_error"])
        self.assert_gone(start["pid"], start["spawned"])


class OrphanStopTest(DelegationCase, unittest.TestCase):
    """A harness is signalled only on evidence that it belongs to the run."""

    def orphan(self, key: str, pid: int, **changes) -> dict:
        """A registered agent run whose owner reports `pid` as its harness and a
        supervisor that is proven gone (pid 1 with start ticks it never had)."""
        row = job._api("POST", "/_sc/runs", {
            "registration_key": key, "kind": "agent", "argv": ["x"], "commit": self.base,
            "agent": {"harness": "codex", "requested_model": "m", "brief_bytes": 1}})
        ticks = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
        job._api("POST", f"/_sc/runs/{row['run_id']}/running", {
            "pid": pid, "start_ticks": ticks, "supervisor_pid": 1,
            "supervisor_start_ticks": 1, "boot_id": runs.boot_id(), **changes})
        return row

    def bystander(self, cwd=None) -> subprocess.Popen:
        process = subprocess.Popen(["sleep", "300"], start_new_session=True, cwd=cwd)
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        return process

    def test_a_process_the_owner_merely_names_is_not_signalled(self):
        # An unrelated process in its own session: alive, the recorded
        # incarnation, a group leader, but not in the run's worktree.
        other = self.bystander()
        row = self.orphan("names-a-stranger", other.pid)
        self.store.reconcile()
        final = job._api("GET", f"/_sc/runs/{row['run_id']}")
        self.assertEqual(final["state"], "lost")
        self.assertIsNone(other.poll())
        self.assertTrue(self.alive(other.pid))
        # The outcome says what is still true.
        self.assertIn(
            f"harness process {other.pid} is still running and was not stopped",
            json.loads(final["terminal_json"])["spawn_error"])
        self.assertIn(") lost; exit=unknown;", self.wake(final))

    def test_pid_one_and_the_server_itself_are_never_the_answer(self):
        row = self.orphan("names-the-server", os.getpid())
        stored = dict(self.con.execute(
            "SELECT * FROM runs WHERE run_id=?", (row["run_id"],)).fetchone())
        self.assertIs(runs.process_alive(stored["pid"], stored["start_ticks"],
                                         stored["boot_id"]), True)
        self.assertFalse(self.store._tied_to_run(stored))
        self.assertEqual(self.store._stop_orphan(stored), "unverified")
        for pid in (1, os.getpgrp()):
            self.assertFalse(self.store._tied_to_run({**stored, "pid": pid}))
        # Even from inside the worktree: this process leads no group of its own
        # that is not the server's.
        worktree = Path(stored["cwd"])
        worktree.mkdir(parents=True)
        here = os.getcwd()
        os.chdir(worktree)
        try:
            self.assertFalse(self.store._tied_to_run(stored))
        finally:
            os.chdir(here)
        # A row whose cwd is not an engine-assigned worktree proves nothing.
        self.assertFalse(self.store._tied_to_run({**stored, "cwd": str(self.root)}))

    def test_a_group_leader_in_the_runs_worktree_is_stopped(self):
        row = job._api("POST", "/_sc/runs", {
            "registration_key": "legitimate", "kind": "agent", "argv": ["x"],
            "commit": self.base,
            "agent": {"harness": "codex", "requested_model": "m", "brief_bytes": 1}})
        Path(row["cwd"]).mkdir(parents=True)
        harness = self.bystander(cwd=row["cwd"])
        ticks = int(Path(f"/proc/{harness.pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
        job._api("POST", f"/_sc/runs/{row['run_id']}/running", {
            "pid": harness.pid, "start_ticks": ticks, "supervisor_pid": 1,
            "supervisor_start_ticks": 1, "boot_id": runs.boot_id()})
        self.store.reconcile()
        final = job._api("GET", f"/_sc/runs/{row['run_id']}")
        self.assertEqual(final["state"], "lost")
        self.assertEqual(harness.wait(timeout=10), -signal.SIGKILL)
        self.assertNotIn("spawn_error", json.loads(final["terminal_json"]))

    def test_a_blocked_row_with_a_dead_supervisor_has_its_harness_stopped(self):
        row = self.launch("SLEEP:120")
        start = self.wait_started()[0]
        running = wait_for(lambda: (
            r if (r := job._api("GET", f"/_sc/runs/{row['run_id']}"))["pid"] else None))
        self.con.execute(
            "UPDATE runs SET wake_state='blocked', last_error='reconciliation blocked' "
            "WHERE run_id=?", (row["run_id"],))
        self.con.commit()
        # With its supervisor alive the row is left alone.
        self.store.reconcile()
        self.assertTrue(self.alive(start["pid"]))
        os.kill(running["supervisor_pid"], signal.SIGKILL)
        wait_for(lambda: not self.alive(running["supervisor_pid"]))
        self.assertTrue(self.alive(start["pid"]))
        self.store.reconcile()
        self.assert_gone(start["pid"])
        after = job._api("GET", f"/_sc/runs/{row['run_id']}")
        # Still blocked for operator recovery, and it says what was done, once.
        self.assertEqual((after["state"], after["wake_state"]), ("running", "blocked"))
        self.assertEqual(
            after["last_error"],
            "reconciliation blocked; harness stopped: its supervisor is gone")
        self.store.reconcile()
        self.assertEqual(
            job._api("GET", f"/_sc/runs/{row['run_id']}")["last_error"], after["last_error"])
        # It holds no slot any more.
        self.assertIn(row["run_id"], self.store._agents_without_a_process())

    def test_a_blocked_row_naming_a_stranger_leaves_it_running(self):
        other = self.bystander()
        row = self.orphan("blocked-stranger", other.pid)
        self.con.execute(
            "UPDATE runs SET wake_state='blocked', last_error='reconciliation blocked' "
            "WHERE run_id=?", (row["run_id"],))
        self.con.commit()
        self.store.reconcile()
        self.assertTrue(self.alive(other.pid))
        self.assertIn("not stopped: it could not be tied to this run's worktree",
                      job._api("GET", f"/_sc/runs/{row['run_id']}")["last_error"])


class LateSupervisorTest(DelegationCase, unittest.TestCase):
    """A supervisor that arrives after its run ended starts nothing."""

    def lost_run(self, payload: dict) -> dict:
        row = job._api("POST", "/_sc/runs", payload)
        # No supervisor ever checked in: after the grace the run is lost.
        self.store.reconcile()
        self.assertEqual(job._api("GET", f"/_sc/runs/{row['run_id']}")["state"], "registered")
        self.con.execute(
            "UPDATE runs SET created_at=datetime('now','-60 seconds') WHERE run_id=?",
            (row["run_id"],))
        self.con.commit()
        self.store.reconcile()
        final = job._api("GET", f"/_sc/runs/{row['run_id']}")
        self.assertEqual((final["state"], final["exit_code"]), ("lost", None))
        return final

    def supervise(self, row: dict, **meta) -> tuple[int, list]:
        jobdir = Path(row["evidence_path"]).parent
        jobdir.mkdir(parents=True)
        (jobdir / "log").touch()
        job.write_meta(jobdir, {
            "run_id": row["run_id"], "job_id": str(row["run_id"]), "kind": row["kind"],
            "label": row["label"], "cwd": row["cwd"], "timeout": 30,
            "started_at": job._now(), "log": str(jobdir / "log"), **meta})
        notified: list = []
        return job.supervise(jobdir, notify=notified.append), notified

    def test_a_registered_agent_row_with_no_supervisor_is_lost_after_the_grace(self):
        final = self.lost_run({
            "registration_key": "never-supervised", "kind": "agent", "argv": ["x"],
            "commit": self.base,
            "agent": {"harness": "codex", "requested_model": "m", "brief_bytes": 1}})
        self.assertIn(f"Run {final['run_id']} (agent: codex:m) lost; exit=unknown;",
                      self.wake(final))
        self.assertEqual(self.store._agents_without_a_process(), frozenset())
        self.assertEqual(
            self.con.execute("SELECT count(*) FROM runs WHERE state IN ('registered','running')"
                             ).fetchone()[0], 0)

    def test_the_running_route_refuses_a_terminal_row_and_nothing_is_launched(self):
        marker = Path(self.temp.name) / "launched"
        agent = self.lost_run({
            "registration_key": "late-agent", "kind": "agent", "argv": ["touch", str(marker)],
            "commit": self.base,
            "agent": {"harness": "codex", "requested_model": "m", "brief_bytes": 1}})
        command = self.lost_run({
            "registration_key": "late-command", "kind": "job",
            "argv": ["touch", str(marker)], "cwd": str(self.root)})
        for row in (agent, command):
            with self.subTest(kind=row["kind"]):
                code, error = self.api_error("POST", f"/_sc/runs/{row['run_id']}/running", {
                    "supervisor_pid": os.getpid(), "supervisor_start_ticks": 1,
                    "boot_id": runs.boot_id()})
                self.assertEqual(code, 409)
                self.assertIn("already has a terminal result (lost)", error)
                returned, notified = self.supervise(
                    row, cmd=row["argv"],
                    agent={"harness": "codex", "stdin": False, "brief_index": None,
                           "run_dir_indexes": [], "env": {}, "env_unset": [],
                           "result": {"source": "stdout_json", "field": "result"},
                           "session": {"source": "pinned"}, "session_ref": None,
                           "base_commit": self.base, "branch": None,
                           "continues_run_id": None})
                self.assertEqual(returned, 127)
                # Not launched, not submitted, and the ledger keeps its outcome.
                self.assertEqual(notified, [])
                self.assertFalse(marker.exists())
                meta = job.read_meta(Path(row["evidence_path"]).parent)
                self.assertTrue(meta["not_launched"])
                self.assertIsNone(meta.get("pid"))
                self.assertEqual(
                    job._api("GET", f"/_sc/runs/{row['run_id']}")["state"], "lost")
        self.assertFalse((self.root / ".sc-worktrees").exists())
        self.assertEqual(self.starts(), [])


class UnchangedMeansUnchangedTest(DelegationCase, unittest.TestCase):
    """H1: git configuration never makes a changed worktree look unchanged."""

    def setUp(self):
        super().setUp()
        excludes = Path(self.temp.name) / "global-excludes"
        excludes.write_text("notes.txt\n")
        Path(self.env["GIT_CONFIG_GLOBAL"]).write_text(
            f"[core]\n\texcludesFile = {excludes}\n"
            "[status]\n\tshowUntrackedFiles = no\n\tbranch = true\n"
            "[diff]\n\tignoreSubmodules = all\n")
        # And in the repository's own configuration, which every worktree shares.
        self.git("config", "status.showUntrackedFiles", "no")
        self.git("config", "status.branch", "true")

    def test_an_untracked_file_keeps_the_worktree_whatever_git_is_told_to_hide(self):
        for name in ("draft.md", "notes.txt"):  # the second is in the global excludes file
            with self.subTest(name=name):
                row = self.finished(self.launch(f"WRITE:{name}"))
                worktree = Path(row["cwd"])
                self.assertTrue((worktree / name).exists())
                self.assertIsNone(row["agent"]["worktree_removed_at"])
                body = self.wake(row)
                self.assertIn(f"Worktree {worktree}; ", body)
                self.assertIn("uncommitted changes: yes.", body)
                self.assertNotIn("removed, unchanged", body)
                # Git, asked the plain way under this configuration, shows nothing.
                plain = subprocess.run(
                    ["git", "-C", str(worktree), "status", "--porcelain"],
                    env=self.env, capture_output=True, text=True, check=True).stdout
                self.assertNotIn(name, plain)
                refused = self.cli("clean", str(row["run_id"]))
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn("uncommitted changes: yes", refused.stderr)
                self.assertTrue((worktree / name).exists())
                self.assertEqual(self.cli("clean", str(row["run_id"]), "--discard").returncode, 0)

    def test_a_read_only_run_is_still_removed_under_that_configuration(self):
        row = self.finished(self.launch("RESULT:read only"))
        self.assertIsNotNone(row["agent"]["worktree_removed_at"])
        self.assertFalse(Path(row["cwd"]).exists())

    def test_a_file_the_repository_ignores_is_not_work(self):
        # Planner ruling: ignored files do not count; the wake says they were not kept.
        (self.root / ".gitignore").write_text("engine/\n.sc-worktrees/\n*.cache\n")
        self.git("add", ".gitignore")
        self.git("-c", "user.name=Test", "-c", "user.email=test@subfloor.local",
                 "commit", "-q", "-m", "ignore caches")
        row = self.finished(self.launch("WRITE:build.cache"))
        self.assertFalse(Path(row["cwd"]).exists())
        self.assertIn("Worktree removed, unchanged (ignored files were not kept);",
                      self.wake(row))


class ContinueTest(DelegationCase, unittest.TestCase):
    """AC 11 and 17: the second command resumes the first run's session and worktree."""

    def test_each_harness_resumes_its_recorded_session_in_the_same_worktree(self):
        for harness in ("claude", "codex", "kimi", "opencode"):
            with self.subTest(harness=harness):
                recorded = RECORDED[harness]
                first = self.finished(self.launch(
                    "REPLAY:1", "--effort", "low", harness=harness, model=recorded["model"]))
                launch = self.starts()[-1]
                self.assertEqual(first["state"], "done", first)
                session = first["agent"]["session_ref"]
                if harness == "claude":  # pinned by the engine at launch
                    self.assertEqual(
                        session, launch["argv"][launch["argv"].index("--session-id") + 1])
                elif harness == "kimi":  # the stream-json launch recorded for task #910
                    self.assertEqual(session, recorded["dash_briefs"]["runs"][0]["session_ref"])
                else:
                    self.assertEqual(session, recorded["launch"]["session_ref"])
                result = job._api("GET", f"/_sc/runs/{first['run_id']}/result")
                self.assertEqual(result["text"].strip(), "NONCE-7f3a")
                # Unchanged, so removed; the continue recreates it at the same path.
                self.assertFalse(Path(first["cwd"]).exists())

                second = self.finished(self.continue_run(first, "REPLAY:1"))
                resume = self.starts()[-1]
                self.assertEqual(second["state"], "done", second)
                self.assertEqual(second["agent"]["continues_run_id"], first["run_id"])
                self.assertEqual(second["cwd"], first["cwd"])
                self.assertEqual(resume["cwd"], first["cwd"])
                self.assertIn("tracked.txt", resume["worktree_listing"])
                self.assertIn(session, resume["argv"])
                # The session the harness reports is the one recorded. Kimi's
                # recorded resume came from a different session than its
                # recorded stream-json launch, so its replay reports that one.
                self.assertEqual(
                    second["agent"]["session_ref"],
                    recorded["resume"]["session_ref"] if harness == "kimi" else session)
                self.assertEqual(second["agent"]["harness"], harness)
                self.assertEqual(second["agent"]["requested_model"], recorded["model"])
                self.assertEqual(second["agent"]["base_commit"], self.base)
                # A new run with its own clock and its own evidence.
                self.assertNotEqual(second["run_id"], first["run_id"])
                self.assertEqual(second["timeout_seconds"], runs.AGENT_DEFAULT_TIMEOUT)
                self.assertNotEqual(second["evidence_path"], first["evidence_path"])
                self.assertEqual(resume["env"]["SC_JOB_AGENT_RUN"], str(second["run_id"]))
                # The recorded resume form, with this run's own values in place.
                # `--skip-git-repo-check` was a scratch-directory flag of the
                # recorded run; a subagent worktree is a git checkout.
                run_dir = str(Path(second["evidence_path"]).parent)
                swap = {"{brief}": "REPLAY:1", "{run_dir}/last-message.md":
                        f"{run_dir}/last-message.md",
                        recorded["resume"]["session_ref"]: session}
                expected = [swap.get(arg, arg) for arg in recorded["resume"]["argv"][1:]
                            if arg != "--skip-git-repo-check"]
                argv = resume["argv"]
                self.assertEqual(set(expected) - set(argv), set())
                self.assertNotIn("--skip-git-repo-check", argv)
                form = {"claude": "--resume", "kimi": "-S", "opencode": "-s"}.get(harness)
                if form:
                    self.assertEqual(argv[argv.index(form) + 1], session)
                    self.assertNotIn("--session-id", argv)
                else:
                    # `codex exec resume` has no --sandbox or -C option.
                    self.assertEqual(argv[:2], ["exec", "resume"])
                    at = argv.index('sandbox_mode="danger-full-access"')
                    self.assertEqual(argv[at - 1], "-c")
                    self.assertNotIn("--sandbox", argv)
                    self.assertNotIn("-C", argv)
                    self.assertEqual(argv[-2:], [session, "-"])
                self.assertIn(f"`sc job continue {second['run_id']}", self.wake(second))

    def test_each_continue_takes_a_slot_and_a_fresh_clock(self):
        first = self.finished(self.launch("RESULT:one"))
        second = self.continue_run(first, "SLEEP:60", "--timeout", "900", "--label", "follow-up")
        self.wait_started(2)
        self.assertEqual(second["timeout_seconds"], 900)
        self.assertEqual(second["label"], "follow-up")
        # Two continues at once: the second is refused because the first is live.
        twice = self.cli("continue", str(first["run_id"]), "--brief", "x")
        self.assertNotEqual(twice.returncode, 0)
        self.assertIn(f"run {second['run_id']} is still live in its worktree", twice.stderr)
        # The live continue holds one of the launching shell's three slots.
        others = [self.launch("SLEEP:60") for _ in range(2)]
        fourth = self.cli("agent", "--harness", "claude", "--model", "m", "--brief", "x")
        self.assertNotEqual(fourth.returncode, 0)
        self.assertIn("live subagent limit for one shell reached: 3 of 3 live", fourth.stderr)
        self.assertIn(
            f"(runs {', '.join(str(r['run_id']) for r in (second, *others))})", fourth.stderr)
        for row in (second, *others):
            self.assertEqual(self.cli("kill", str(row["run_id"])).returncode, 0)
            self.finished(row)

    def test_refusals(self):
        live = self.launch("SLEEP:60")
        self.wait_started()
        refused = self.cli("continue", str(live["run_id"]), "--brief", "x")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("is still live", refused.stderr)
        # The ledger holds the same rule for a caller that skips the verb's check.
        code, error = self.api_error("POST", "/_sc/runs", self._continue_payload(live))
        self.assertEqual(code, 400)
        self.assertIn("is still live in its worktree", error)
        self.assertEqual(self.cli("kill", str(live["run_id"])).returncode, 0)
        self.finished(live)

        silent = self.finished(self.launch("NOSESSION:1", harness="opencode"))
        self.assertIsNone(silent["agent"]["session_ref"])
        refused = self.cli("continue", str(silent["run_id"]), "--brief", "x")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("captured no session reference", refused.stderr)
        code, error = self.api_error("POST", "/_sc/runs", self._continue_payload(silent))
        self.assertEqual(code, 400)
        self.assertIn("no session reference was captured", error)

        done = self.finished(self.launch("RESULT:one"))
        code, error = self.api_error(
            "POST", "/_sc/runs", self._continue_payload(done, harness="codex"))
        self.assertEqual(code, 400)
        self.assertIn("it ran on 'claude'", error)
        # A subagent cannot continue a subagent.
        nested = self.cli("continue", str(done["run_id"]), "--brief", "x",
                          env={**self.env, "SC_JOB_AGENT_RUN": str(done["run_id"])})
        self.assertNotEqual(nested.returncode, 0)
        self.assertIn(runs.NESTING_CONFUSION, nested.stderr)
        # A command job is not a subagent.
        command = self.cli("start", "--", "true")
        self.assertEqual(command.returncode, 0)
        plain = job._api("GET", "/_sc/runs")["runs"][0]
        refused = self.cli("continue", str(plain["run_id"]), "--brief", "x")
        self.assertIn("not a subagent", refused.stderr)
        # An adapter with no resume form cannot continue.
        adapter = json.loads((ENGINE / "adapters/claude/adapter.json").read_text())
        del adapter["headless"]["agent"]["resume"]
        with self.assertRaises(job_agent.AgentRefusal) as caught:
            job_agent.build_command(
                "claude", "m", resume_session="abc", adapter=adapter,
                which=lambda _binary: "/bin/true", cli_version=lambda _binary: "0")
        self.assertIn("no `headless.agent.resume` key", str(caught.exception))
        self.assertEqual(
            self.con.execute(
                "SELECT count(*) FROM run_agents WHERE continues_run_id IS NOT NULL"
            ).fetchone()[0], 0)

    def _continue_payload(self, row: dict, harness: str | None = None) -> dict:
        facts = row["agent"]
        return {
            "registration_key": f"continue-{row['run_id']}-{harness}", "kind": "agent",
            "argv": ["x"], "commit": facts["base_commit"],
            "agent": {
                "harness": harness or facts["harness"],
                "requested_model": facts["requested_model"], "effective_effort": "default",
                "brief_bytes": 1, "continues_run_id": row["run_id"],
            },
        }

    def test_a_continue_is_refused_when_the_slots_are_full(self):
        done = self.finished(self.launch("RESULT:one"))
        held = [self.launch("SLEEP:60") for _ in range(3)]
        refused = self.cli("continue", str(done["run_id"]), "--brief", "x")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("HTTP 429", refused.stderr)
        self.assertIn("live subagent limit for one shell reached: 3 of 3 live", refused.stderr)
        self.assertEqual(self.newest_id(), held[-1]["run_id"])
        self.assertEqual(len(self.wait_started(4)), 4)
        for row in held:
            self.assertEqual(self.cli("kill", str(row["run_id"])).returncode, 0)
            self.finished(row)

    def test_the_supervisor_does_not_report_an_unusable_session_reference(self):
        for value in ("--dangerously-skip-permissions", 'ses"quoted'):
            with self.subTest(value=value):
                row = self.finished(self.launch(f"SESSION:{value}", harness="opencode"))
                self.assertEqual(row["state"], "done")
                self.assertIsNone(row["agent"]["session_ref"])
                recorded = json.loads(
                    (Path(row["evidence_path"]).parent / "facts.json").read_text())
                self.assertNotIn("session_ref", recorded)
                self.assertNotIn(value, json.dumps(row["agent"]))
                refused = self.cli("continue", str(row["run_id"]), "--brief", "x")
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn("captured no session reference", refused.stderr)

    def test_a_malformed_session_reference_is_refused_when_it_is_written(self):
        row = self.launch("SLEEP:60")
        self.wait_started()
        for value in ("--dangerously-anything", "has space ", 'quote"d', "new\nline", ""):
            with self.subTest(value=value):
                code, error = self.api_error(
                    "POST", f"/_sc/runs/{row['run_id']}/facts", {"session_ref": value})
                self.assertEqual(code, 400)
                self.assertIn("session_ref", error)
        self.assertIsNone(job._api("GET", f"/_sc/runs/{row['run_id']}")["agent"]["provider_model"])
        self.assertEqual(self.cli("kill", str(row["run_id"])).returncode, 0)
        self.finished(row)


class SpawnPlanTest(unittest.TestCase):
    """The token positions cross the process boundary as data, never by search."""

    def test_only_the_recorded_positions_are_filled(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "brief.md").write_text("the brief")
            # A model that spells both tokens, beside the engine's own.
            argv = ["kimi", "-m", "{brief}", "-o", "{run_dir}/last-message.md",
                    "--note", "{run_dir}", "-p", "{brief}"]
            plan = json.loads(json.dumps(
                {"stdin": False, "brief_index": 8, "run_dir_indexes": [4]}))
            filled, stdin = job_agent.materialize_argv(argv, run_dir, plan)
            self.assertIsNone(stdin)
            self.assertEqual(filled, [
                "kimi", "-m", "{brief}", "-o", f"{run_dir}/last-message.md",
                "--note", "{run_dir}", "-p", "the brief"])
            # A plan that does not fit the command is refused, not repaired.
            for wrong in ({"brief_index": 2 + 7}, {"brief_index": 1},
                          {"brief_index": None, "run_dir_indexes": [1]},
                          {"brief_index": None, "run_dir_indexes": ["4"]}):
                with self.subTest(plan=wrong), self.assertRaises(ValueError):
                    job_agent.materialize_argv(argv, run_dir, wrong)

    def test_the_plan_is_what_the_command_builder_recorded(self):
        adapter = json.loads((ENGINE / "adapters/codex/adapter.json").read_text())
        command = job_agent.build_command(
            "codex", "{run_dir}", adapter=adapter,
            which=lambda _binary: "/bin/true", cli_version=lambda _binary: "0")
        plan = json.loads(json.dumps(job_agent.spawn_plan(command)))
        self.assertEqual(plan["run_dir_indexes"], list(command.run_dir_indexes))
        self.assertEqual(plan["brief_index"], command.brief_index)
        self.assertTrue(plan["stdin"])
        with tempfile.TemporaryDirectory() as tmp:
            filled, stdin = job_agent.materialize_argv(command.argv, Path(tmp), plan)
            self.assertEqual(filled, command.materialize(Path(tmp))[0])
            self.assertEqual(filled[filled.index("-m") + 1], "{run_dir}")
            self.assertEqual(stdin, Path(tmp) / "brief.md")


class WorktreeLifeTest(DelegationCase, unittest.TestCase):
    """AC 13: removed only when unchanged; kept otherwise; clean refuses to lose work."""

    def test_an_unchanged_worktree_is_removed_and_recreated_by_continue(self):
        first = self.finished(self.launch("RESULT:read only", "--branch", "agent/review"))
        worktree = Path(first["cwd"])
        self.assertFalse(worktree.exists())
        self.assertIsNotNone(first["agent"]["worktree_removed_at"])
        self.assertIn(
            "Worktree removed, unchanged (ignored files were not kept); agent/review;",
            self.wake(first))
        self.assertNotIn(str(worktree), self.git("worktree", "list"))

        second = self.finished(self.continue_run(first, "WRITE:notes.txt"))
        start = self.starts()[-1]
        self.assertEqual(start["cwd"], str(worktree))
        # Recreated at the same path, on the branch the first run created, and
        # kept this time because the subagent changed it.
        self.assertTrue((worktree / "notes.txt").exists())
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=worktree),
                         "agent/review")
        self.assertIsNone(second["agent"]["worktree_removed_at"])
        body = self.wake(second)
        self.assertIn(f"Worktree {worktree}; agent/review; HEAD {self.base[:12]}; "
                      "uncommitted changes: yes.", body)

    def test_a_changed_worktree_is_kept_and_clean_refuses_to_lose_work(self):
        kept = self.finished(self.launch("COMMIT:work.txt"))
        worktree = Path(kept["cwd"])
        head = self.git("rev-parse", "HEAD", cwd=worktree)
        self.assertNotEqual(head, self.base)
        self.assertIn(f"Worktree {worktree}; detached at {self.base[:12]}; HEAD {head[:12]}; "
                      "uncommitted changes: no.", self.wake(kept))
        self.assertEqual(json.loads(kept["terminal_json"])["agent"]["head_commit"], head)

        # Live: a continue is running in that worktree.
        live = self.continue_run(kept, "SLEEP:60\nWRITE:draft.txt")
        self.wait_started(2)
        refused = self.cli("clean", str(kept["run_id"]), "--discard")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn(f"run {live['run_id']} is live in its worktree", refused.stderr)
        self.assertTrue(worktree.exists())
        self.assertEqual(self.cli("kill", str(live["run_id"])).returncode, 0)
        self.finished(live)

        # Unpushed commits and uncommitted changes: refused without --discard.
        refused = self.cli("clean", str(kept["run_id"]))
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("uncommitted changes: yes; commits on no remote branch: 1", refused.stderr)
        self.assertIn("holds work that is on no remote branch", refused.stderr)
        self.assertTrue((worktree / "draft.txt").exists())
        self.assertIsNone(
            job._api("GET", f"/_sc/runs/{kept['run_id']}")["agent"]["worktree_discarded_at"])

        done = self.cli("clean", str(kept["run_id"]), "--discard")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("removed with its uncommitted or unpushed work", done.stdout)
        self.assertFalse(worktree.exists())
        self.assertNotIn(str(worktree), self.git("worktree", "list"))
        after = job._api("GET", f"/_sc/runs/{kept['run_id']}")["agent"]
        self.assertIsNotNone(after["worktree_removed_at"])
        self.assertIsNotNone(after["worktree_discarded_at"])
        # Idempotent, and a discarded worktree cannot be continued, from either run.
        self.assertEqual(self.cli("clean", str(kept["run_id"])).returncode, 0)
        for row in (kept, live):
            refused = self.cli("continue", str(row["run_id"]), "--brief", "x")
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("was removed with --discard", refused.stderr)
        self.assertEqual(len(self.starts()), 2)

    def test_work_that_is_on_a_remote_branch_is_removed_without_discard(self):
        remote = Path(self.temp.name) / "remote.git"
        self.git("init", "-q", "--bare", str(remote))
        self.git("remote", "add", "origin", str(remote))
        kept = self.finished(self.launch("COMMIT:work.txt", "--branch", "agent/pushed"))
        worktree = Path(kept["cwd"])
        self.assertTrue(worktree.exists())
        refused = self.cli("clean", str(kept["run_id"]))
        self.assertNotEqual(refused.returncode, 0)
        # True to what clean does: the commits stay on the local branch it keeps.
        self.assertIn("commits on no remote branch: 1 (local branch agent/pushed is kept "
                      "and still holds its commits)", refused.stderr)
        self.assertNotIn("nowhere else", refused.stderr)
        self.git("push", "-q", "origin", "agent/pushed", cwd=worktree)
        done = self.cli("clean", str(kept["run_id"]))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Branch agent/pushed is kept", done.stdout)
        self.assertFalse(worktree.exists())
        after = job._api("GET", f"/_sc/runs/{kept['run_id']}")["agent"]
        self.assertIsNone(after["worktree_discarded_at"])
        # Nothing was discarded, so the session can still be continued: the
        # worktree comes back on its branch, with the pushed commit.
        again = self.finished(self.continue_run(kept, "RESULT:after clean"))
        self.assertEqual(again["state"], "done")
        self.assertIn("work.txt", self.starts()[-1]["worktree_listing"])

    def test_clean_is_for_subagent_runs_of_the_calling_shell(self):
        self.assertEqual(self.cli("start", "--", "true").returncode, 0)
        plain = job._api("GET", "/_sc/runs")["runs"][0]
        refused = self.cli("clean", str(plain["run_id"]))
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("not a subagent", refused.stderr)
        self.assertEqual(self.api_error("POST", f"/_sc/runs/{plain['run_id']}/clean", {})[0], 400)


class SlotTest(DelegationCase, unittest.TestCase):
    """What holds a slot: a process that can still be running."""

    def register(self, key: str) -> dict:
        return self.store.register(1, {
            "registration_key": key, "kind": "agent", "argv": ["x"], "commit": self.base,
            "agent": {"harness": "codex", "requested_model": "m", "brief_bytes": 1}})

    def test_a_blocked_row_frees_its_slot_only_without_a_live_supervisor(self):
        rows = [self.register(f"k{i}") for i in range(3)]
        me = os.getpid()
        ticks = int(Path(f"/proc/{me}/stat").read_text().rsplit(")", 1)[1].split()[19])
        # Blocked, but its supervisor is this live process: the slot is held.
        self.con.execute(
            "UPDATE runs SET state='running', wake_state='blocked', supervisor_pid=?, "
            "supervisor_start_ticks=?, boot_id=? WHERE run_id=?",
            (me, ticks, runs.boot_id(), rows[0]["run_id"]))
        self.con.commit()
        self.assertEqual(self.store._agents_without_a_process(), frozenset())
        with self.assertRaises(runs.RunLimitError):
            self.register("fourth")
        # Blocked with no supervisor proven alive: the slot is free.
        self.con.execute(
            "UPDATE runs SET supervisor_pid=NULL, supervisor_start_ticks=NULL WHERE run_id=?",
            (rows[0]["run_id"],))
        self.con.commit()
        self.assertEqual(self.store._agents_without_a_process(), frozenset({rows[0]["run_id"]}))
        self.assertEqual(self.register("fourth")["state"], "registered")

    def test_a_dead_supervisor_holds_its_slot_while_the_harness_lives(self):
        rows = [self.register(f"k{i}") for i in range(3)]
        me = os.getpid()
        ticks = int(Path(f"/proc/{me}/stat").read_text().rsplit(")", 1)[1].split()[19])
        # Supervisor from an earlier boot is proven gone; no harness recorded.
        self.con.execute(
            "UPDATE runs SET state='running', supervisor_pid=1, supervisor_start_ticks=1, "
            "boot_id='an-earlier-boot' WHERE run_id=?", (rows[0]["run_id"],))
        self.con.commit()
        self.assertEqual(self.store._agents_without_a_process(), frozenset({rows[0]["run_id"]}))
        # Supervisor gone (pid 1 with other start ticks), harness proven alive.
        self.con.execute(
            "UPDATE runs SET boot_id=?, pid=?, start_ticks=? WHERE run_id=?",
            (runs.boot_id(), me, ticks, rows[0]["run_id"]))
        self.con.commit()
        self.assertEqual(self.store._agents_without_a_process(), frozenset())
        with self.assertRaises(runs.RunLimitError):
            self.register("fourth")


if __name__ == "__main__":
    unittest.main()
