"""Runs ledger for subagents (Feature #96, spec #273): upgrade, registration, limits.

Acceptance criteria covered here: 5 (concurrency), 6 (nesting), 7 (run time),
8 (the Reviewer rule), plus the upgrade of a populated ledger and the facts,
result and listing routes.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / ".super-coder"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))

import job
import migrate
import runs
from test_job import TOKEN, build_db
from test_runs import ApiFixture

MIGRATION = "0285_run_agents_and_time_limit.sql"
COMMIT = "a" * 40


def agent_payload(key: str, **changes) -> dict:
    agent = {
        "harness": "codex",
        "requested_model": "model-x",
        "requested_effort": None,
        "effective_effort": "default",
        "adapter_version": "codex-cli 0.0.0",
        "brief_bytes": 120,
        **changes.pop("agent", {}),
    }
    return {
        "registration_key": key,
        "kind": "agent",
        "argv": ["codex", "exec", "-m", "model-x", "-"],
        "commit": COMMIT,
        "agent": agent,
        **changes,
    }


class UpgradeTest(unittest.TestCase):
    """The rebuild carries a populated ledger through unchanged."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "prior.db"
        con = sqlite3.connect(self.path)
        con.executescript((ENGINE / "schema.sql").read_text())
        for migration in sorted((ENGINE / "migrations").glob("*.sql")):
            if migration.name >= MIGRATION:
                break
            con.executescript(migration.read_text())
            con.execute(
                "INSERT OR IGNORE INTO schema_migrations(filename) VALUES(?)", (migration.name,)
            )
        con.execute("INSERT INTO users (user_id, username, is_active) VALUES (1, 'T', 1)")
        con.execute(
            "INSERT INTO shells (shell_id, display_name, shortname, system_prompt, user_id, api_key) "
            "VALUES (1, 'Dev One', 'dev1', 'x', 1, ?)", (TOKEN,))
        con.commit()
        con.row_factory = sqlite3.Row
        self.con = con
        self.addCleanup(con.close)

    def _populate(self) -> None:
        rows = [
            (3, "job", "done", 0, '{"exit_code":0,"finished_at":"2026-10-04T10:00:00Z","state":"done"}', 0),
            (7, "probe", "running", None, None, 0),
            (9, "devkit", "failed", 2, '{"exit_code":2,"finished_at":"2026-10-04T10:00:00Z","state":"failed"}', 1),
            (12, "native", "lost", None, '{"finished_at":"2026-10-04T10:00:00Z","state":"lost"}', 0),
        ]
        for rid, kind, state, code, terminal, foreground in rows:
            self.con.execute(
                'INSERT INTO runs(run_id,owner_shell_id,registration_key,kind,label,argv,cwd,"commit",'
                "state,exit_code,terminal_json,evidence_path,foreground,attached_pr,evidence_pruned) "
                "VALUES(?,1,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, f"key-{rid}", kind, f"label {rid}", '["sh","-c","true"]', "/repo",
                 COMMIT, state, code, terminal, f"/engine/run/runs/{rid}/log", foreground,
                 41 if rid == 3 else None, 1 if rid == 9 else 0),
            )
        self.con.execute(
            "INSERT INTO run_attachment_events(run_id,actor_shell_id,target_json,outcome) "
            "VALUES(3,1,'{\"pr\":41}','attached')"
        )
        # The newest id was allocated but its row is not the table maximum any more.
        self.con.execute("UPDATE sqlite_sequence SET seq=20 WHERE name='runs'")
        self.con.commit()

    def _snapshot(self) -> list[dict]:
        return [dict(row) for row in self.con.execute("SELECT * FROM runs ORDER BY run_id")]

    def test_populated_ledger_upgrades_with_rows_ids_and_guards_intact(self):
        self._populate()
        before = self._snapshot()
        self.assertEqual([path.name for path in migrate.pending(self.con)], [MIGRATION])

        migrate.apply(self.con, ENGINE / "migrations" / MIGRATION)

        after = self._snapshot()
        self.assertEqual(
            [{k: v for k, v in row.items() if k != "timeout_seconds"} for row in after], before
        )
        self.assertTrue(all(row["timeout_seconds"] is None for row in after))
        self.assertEqual(
            self.con.execute("SELECT seq FROM sqlite_sequence WHERE name='runs'").fetchone()[0], 20
        )
        self.assertEqual(self.con.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(
            self.con.execute("SELECT run_id FROM run_attachment_events").fetchone()[0], 3
        )
        self.assertEqual(
            {row[0] for row in self.con.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='runs'")},
            {"runs_identity_immutable", "runs_path_immutable", "runs_terminal_immutable"},
        )
        self.assertEqual(
            {row[0] for row in self.con.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='runs' "
                "AND name LIKE 'idx_%'")},
            {"idx_runs_owner", "idx_runs_pending", "idx_runs_message", "idx_runs_attached_pr",
             "idx_runs_attached_unit", "idx_runs_live_agents"},
        )
        for statement in (
            "UPDATE runs SET cwd='/elsewhere' WHERE run_id=7",
            "UPDATE runs SET timeout_seconds=5 WHERE run_id=7",
            "UPDATE runs SET evidence_path='/x' WHERE run_id=7",
            "UPDATE runs SET state='done' WHERE run_id=3",
        ):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                self.con.execute(statement)
        self.con.rollback()
        # The next registration still allocates above the carried high-water mark.
        store = runs.RunStore(self.con, Path(self.temp.name) / "engine")
        row = store.register(1, {"registration_key": "next", "argv": ["true"], "cwd": "/repo"})
        self.assertEqual(row["run_id"], 21)
        self.assertNotIn("agent", row)
        self.assertEqual(store.get(3)["state"], "done")

    def test_empty_ledger_upgrades_and_accepts_every_kind(self):
        migrate.apply(self.con, ENGINE / "migrations" / MIGRATION)
        self.assertEqual(migrate.pending(self.con), [])
        for kind in ("job", "devkit", "probe", "native", "agent"):
            self.con.execute(
                "INSERT INTO runs(owner_shell_id,registration_key,kind,label,argv,cwd,evidence_path) "
                "VALUES(1,?,?,'l','[]','/repo','/e')", (kind, kind))
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO runs(owner_shell_id,registration_key,kind,label,argv,cwd,evidence_path) "
                "VALUES(1,'bad','watcher','l','[]','/repo','/e')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("UPDATE runs SET timeout_seconds=0")
        self.con.rollback()

    def test_a_failed_application_leaves_the_prior_ledger_in_place(self):
        self._populate()
        before = self._snapshot()
        # A second column of the same name makes the first statement fail.
        self.con.execute("ALTER TABLE runs ADD COLUMN timeout_seconds INTEGER")
        self.con.commit()
        with self.assertRaises(sqlite3.OperationalError):
            migrate.apply(self.con, ENGINE / "migrations" / MIGRATION)
        self.assertEqual(
            [{k: v for k, v in row.items() if k != "timeout_seconds"} for row in self._snapshot()],
            before,
        )
        self.assertEqual([path.name for path in migrate.pending(self.con)], [MIGRATION])


class AgentLedgerCase(ApiFixture, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_path = self.root / "test.db"
        build_db(str(self.db_path))
        self.con = sqlite3.connect(self.db_path)
        self.con.row_factory = sqlite3.Row
        self.addCleanup(self.con.close)
        for sid, name in ((2, "pln1"), (3, "rev1"), (4, "rev2")):
            self.con.execute(
                "INSERT INTO shells(shell_id,display_name,shortname,system_prompt,user_id,api_key) "
                "VALUES(?,?,?,'x',1,?)", (sid, name, name, f"{name}-token"))
        self.con.commit()
        self.store = runs.RunStore(self.con, self.root / "engine")
        self.start_api()

    def api_error(self, method, path, body=None) -> tuple[int, str]:
        with self.assertRaises(urllib.error.HTTPError) as caught:
            job._api(method, path, body)
        return caught.exception.code, json.loads(caught.exception.read())["error"]

    def finish(self, row, owner=1):
        return self.store.terminal(
            row["run_id"], owner,
            {"state": "done", "exit_code": 0, "finished_at": "2026-11-12T10:00:00Z"},
        )


class RegistrationTest(AgentLedgerCase):
    def test_agent_registers_with_its_facts_worktree_and_default_time_limit(self):
        row = job._api("POST", "/_sc/runs", agent_payload("one", agent={"branch": "feat/x"}))
        self.assertEqual(row["kind"], "agent")
        self.assertEqual(row["state"], "registered")
        self.assertEqual(row["timeout_seconds"], 7200)
        self.assertEqual(row["label"], "codex:model-x")
        worktree = str(self.root / ".sc-worktrees" / f"agent-{row['run_id']}")
        self.assertEqual(row["cwd"], worktree)
        self.assertEqual(
            row["agent"],
            {
                "harness": "codex", "requested_model": "model-x", "provider_model": None,
                "requested_effort": None, "effective_effort": "default", "session_ref": None,
                "worktree": worktree, "base_commit": COMMIT, "branch": "feat/x",
                "continues_run_id": None, "result_bytes": None, "usage_json": None,
                "adapter_version": "codex-cli 0.0.0", "worktree_removed_at": None,
            },
        )
        # Registration is idempotent on its key and refuses a changed request.
        self.assertEqual(
            job._api("POST", "/_sc/runs", agent_payload("one", agent={"branch": "feat/x"})), row
        )
        code, error = self.api_error(
            "POST", "/_sc/runs", agent_payload("one", agent={"branch": "feat/y"}))
        self.assertEqual(code, 400)
        self.assertIn("different input", error)

    def test_agent_projection_is_on_the_listing_and_absent_from_other_kinds(self):
        agent = job._api("POST", "/_sc/runs", agent_payload("one"))
        command = self.register(registration_key="plain", timeout_seconds=90)
        listed = {row["run_id"]: row for row in job._api("GET", "/_sc/runs")["runs"]}
        self.assertEqual(listed[agent["run_id"]]["agent"]["harness"], "codex")
        self.assertNotIn("agent", listed[command["run_id"]])
        self.assertEqual(listed[command["run_id"]]["timeout_seconds"], 90)
        only = job._api("GET", "/_sc/runs?kind=agent")
        self.assertEqual([row["run_id"] for row in only["runs"]], [agent["run_id"]])
        # The operator listing reads through the same projection.
        fleet = {row["run_id"]: row for row in self.store.list(None)}
        self.assertEqual(fleet[agent["run_id"]]["agent"]["requested_model"], "model-x")

    def test_malformed_agent_requests_register_nothing(self):
        cases = {
            "an agent object": {"agent": None},
            "unknown agent field": {"agent": {"continues_run_id": 4}},
            "engine-assigned worktree": {"cwd": str(self.root)},
            "base commit": {"commit": "main"},
            "harness must be": {"agent": {"harness": ""}},
            "requested_model must be": {"agent": {"requested_model": None}},
            "brief_bytes must be": {"agent": {"brief_bytes": 0}},
            "timeout_seconds must be": {"timeout_seconds": 0},
        }
        for fragment, changes in cases.items():
            with self.subTest(fragment=fragment):
                missing = "agent" in changes and changes["agent"] is None
                payload = agent_payload("bad", **({} if missing else dict(changes)))
                if missing:
                    payload["agent"] = None
                code, error = self.api_error("POST", "/_sc/runs", payload)
                self.assertEqual(code, 400)
                self.assertIn(fragment, error)
        code, error = self.api_error(
            "POST", "/_sc/runs",
            {"registration_key": "x", "argv": ["true"], "cwd": str(self.root),
             "agent": {"harness": "codex"}})
        self.assertIn("require an agent run", error)
        self.assertEqual(self.con.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT count(*) FROM run_agents").fetchone()[0], 0)

    def test_brief_size_is_capped_at_64_kib(self):
        row = job._api("POST", "/_sc/runs", agent_payload("fits", agent={"brief_bytes": 65536}))
        self.assertEqual(row["state"], "registered")
        code, error = self.api_error(
            "POST", "/_sc/runs", agent_payload("over", agent={"brief_bytes": 65537}))
        self.assertEqual(code, 400)
        self.assertIn("65537 bytes; the limit is 65536 bytes", error)


class ConcurrencyLimitTest(AgentLedgerCase):
    """AC 5: the fourth for one shell and the seventh on the install are refused."""

    def test_parallel_registrations_never_exceed_three_per_shell(self):
        def attempt(index):
            try:
                return job._api("POST", "/_sc/runs", agent_payload(f"parallel-{index}"))
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())["error"]

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(attempt, range(8)))
        held = sorted(o["run_id"] for o in outcomes if isinstance(o, dict))
        refused = [o for o in outcomes if isinstance(o, tuple)]
        self.assertEqual(len(held), 3)
        self.assertEqual(len(refused), 5)
        for code, error in refused:
            self.assertEqual(code, 429)
            self.assertIn("live subagent limit for one shell reached: 3 of 3 live", error)
            self.assertIn(f"(runs {', '.join(map(str, held))})", error)
        self.assertEqual(
            self.con.execute("SELECT count(*) FROM runs WHERE kind='agent'").fetchone()[0], 3)

    def test_a_finished_run_frees_its_slot_and_other_kinds_take_none(self):
        rows = [self.store.register(1, agent_payload(f"k{i}")) for i in range(3)]
        for index in range(4):
            self.register(registration_key=f"command-{index}")
        with self.assertRaises(runs.RunLimitError):
            self.store.register(1, agent_payload("fourth"))
        self.finish(rows[0])
        self.assertEqual(self.store.register(1, agent_payload("fourth"))["state"], "registered")

    def test_the_seventh_on_the_install_is_refused_naming_every_holder(self):
        held = []
        for owner in (1, 2):
            held += [self.store.register(owner, agent_payload(f"k{i}")) for i in range(3)]
        with self.assertRaises(runs.RunLimitError) as caught:
            self.store.register(3, agent_payload("seventh"))
        error = str(caught.exception)
        self.assertIn("live subagent limit for this install reached: 6 of 6 live", error)
        for row, name in zip(held, ["dev1"] * 3 + ["pln1"] * 3):
            self.assertIn(f"{row['run_id']} ({name})", error)
        self.assertEqual(
            self.con.execute("SELECT count(*) FROM runs WHERE kind='agent'").fetchone()[0], 6)
        # A command job is not a subagent: the install limit does not touch it.
        self.assertEqual(
            self.store.register(
                3, {"registration_key": "cmd", "argv": ["true"], "cwd": str(self.root)}
            )["kind"], "job")


class NestingTest(AgentLedgerCase):
    """AC 6: a registration that carries a subagent's run id is refused."""

    def test_a_subagent_cannot_launch_a_subagent_but_can_start_a_command_job(self):
        parent = job._api("POST", "/_sc/runs", agent_payload("parent"))
        code, error = self.api_error(
            "POST", "/_sc/runs",
            agent_payload("child", agent={"launched_from_run": str(parent["run_id"])}))
        self.assertEqual(code, 403)
        self.assertIn(runs.NESTING_CONFUSION, error)
        self.assertIn(f"subagent run {parent['run_id']}", error)
        self.assertIn("nesting depth 1", error)
        self.assertEqual(
            self.con.execute("SELECT count(*) FROM runs WHERE kind='agent'").fetchone()[0], 1)
        # The same environment may still start a command job; it belongs to the shell.
        command = self.register(registration_key="from-inside")
        self.assertEqual(command["kind"], "job")
        self.assertEqual(command["owner_shell_id"], parent["owner_shell_id"])


class RunTimeTest(AgentLedgerCase):
    """AC 7: default 7,200 recorded, more than 28,800 refused, the limit enforced."""

    def test_default_ceiling_and_refusal(self):
        self.assertEqual(self.store.register(1, agent_payload("default"))["timeout_seconds"], 7200)
        self.assertEqual(
            self.store.register(1, agent_payload("ceiling", timeout_seconds=28800))[
                "timeout_seconds"], 28800)
        code, error = self.api_error(
            "POST", "/_sc/runs", agent_payload("over", timeout_seconds=28801))
        self.assertEqual(code, 400)
        self.assertIn("limited to 28800 seconds (8 hours); 28801 was requested", error)
        self.assertEqual(
            self.con.execute("SELECT count(*) FROM runs WHERE kind='agent'").fetchone()[0], 2)

    def test_a_run_past_its_limit_ends_timeout_and_wakes(self):
        row = job._api("POST", "/_sc/runs", agent_payload("short", timeout_seconds=1))
        jobdir = Path(row["evidence_path"]).parent
        jobdir.mkdir(parents=True)
        (jobdir / "log").touch()
        job.write_meta(jobdir, {
            "run_id": row["run_id"], "job_id": str(row["run_id"]), "kind": "agent",
            "label": row["label"], "cmd": ["sleep", "60"], "cwd": str(self.root),
            "timeout": row["timeout_seconds"], "started_at": job._now(),
            "log": str(jobdir / "log"),
        })
        job.supervise(jobdir)
        final = job._api("GET", f"/_sc/runs/{row['run_id']}")
        self.assertEqual(final["state"], "timeout")
        self.assertEqual(final["wake_state"], "pending")
        self.assertIsNotNone(final["message_id"])
        body = self.con.execute(
            "SELECT body FROM shell_messages WHERE message_id=?", (final["inbox_message_id"],)
        ).fetchone()[0]
        self.assertIn(f"Run {row['run_id']} (agent: codex:model-x) timeout", body)
        # A finished run holds no slot.
        self.assertEqual(
            self.con.execute(
                "SELECT count(*) FROM runs WHERE kind='agent' AND state IN ('registered','running')"
            ).fetchone()[0], 0)


class ReviewerRuleTest(AgentLedgerCase):
    """AC 8: a Reviewer of an armed or paused run may not delegate."""

    def _move(self, run_id: int, lifecycle: str) -> None:
        outcome = "done" if lifecycle == "completed" else None
        self.con.execute(
            "UPDATE sprints SET lifecycle=?, terminal_outcome=? WHERE sprint_id=?",
            (lifecycle, outcome, run_id))
        self.con.commit()

    def _run(self, lifecycle: str, members: dict[int, str]) -> int:
        """One run walked through the ledger's own lifecycle guards."""
        run_id = self.con.execute(
            "INSERT INTO sprints(feature_id,originating_planner_shell_id) VALUES(1,2)"
        ).lastrowid
        for shell_id, role in members.items():
            self.con.execute(
                "INSERT INTO sprint_participants(sprint_id,shell_id,role,harness) VALUES(?,?,?,'codex')",
                (run_id, shell_id, role))
        reviewer = next(shell for shell, role in members.items() if role == "reviewer")
        self.con.execute(
            "UPDATE sprints SET merge_grant_enabled=1, conformance_reviewer_shell_id=?, "
            "conformance_owner_generation=1 WHERE sprint_id=?", (reviewer, run_id))
        self.con.commit()
        self._move(run_id, "armed")
        if lifecycle != "armed":
            self._move(run_id, lifecycle)
        return run_id

    def test_reviewer_is_refused_while_developer_and_planner_register(self):
        self._run("completed", {4: "reviewer"})
        armed = self._run("armed", {1: "developer", 2: "planner", 3: "reviewer"})
        with self.assertRaises(PermissionError) as caught:
            self.store.register(3, agent_payload("review"))
        error = str(caught.exception)
        self.assertIn(runs.REVIEWER_CONFUSION, error)
        self.assertIn(f"{armed} (armed)", error)
        self.assertEqual(self.con.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
        self.assertEqual(self.store.register(1, agent_payload("dev"))["state"], "registered")
        self.assertEqual(self.store.register(2, agent_payload("plan"))["state"], "registered")
        # A Reviewer whose run has ended, and one in none, register.
        self.assertEqual(self.store.register(4, agent_payload("free"))["state"], "registered")
        # The refused Reviewer may still start a command job.
        self.assertEqual(
            self.store.register(
                3, {"registration_key": "cmd", "argv": ["true"], "cwd": str(self.root)}
            )["kind"], "job")

    def test_paused_holds_the_rule_and_the_route_answers_403(self):
        run_id = self._run("paused", {1: "reviewer"})
        code, error = self.api_error("POST", "/_sc/runs", agent_payload("review"))
        self.assertEqual(code, 403)
        self.assertIn(runs.REVIEWER_CONFUSION, error)
        self.assertIn("(paused)", error)
        self._move(run_id, "armed")
        self.assertEqual(self.api_error("POST", "/_sc/runs", agent_payload("review"))[0], 403)
        self._move(run_id, "completed")
        self.assertEqual(
            job._api("POST", "/_sc/runs", agent_payload("review"))["state"], "registered")


class FactsAndResultTest(AgentLedgerCase):
    def test_facts_are_written_once_and_only_by_the_owner(self):
        row = job._api("POST", "/_sc/runs", agent_payload("one"))
        path = f"/_sc/runs/{row['run_id']}/facts"
        facts = {"session_ref": "thread-1", "provider_model": "model-x-served", "result_bytes": 12}
        self.assertEqual(job._api("POST", path, facts)["agent"]["session_ref"], "thread-1")
        again = job._api("POST", path, facts)["agent"]
        self.assertEqual(
            (again["provider_model"], again["result_bytes"]), ("model-x-served", 12))
        code, error = self.api_error("POST", path, {"session_ref": "thread-2"})
        self.assertEqual(code, 409)
        self.assertIn("conflicting agent fact: session_ref", error)
        for bad in ({}, {"harness": "claude"}, {"result_bytes": -1}, {"session_ref": ""},
                    {"worktree_removed_at": "yesterday"}):
            with self.subTest(bad=bad):
                self.assertEqual(self.api_error("POST", path, bad)[0], 400)
        with self.assertRaises(PermissionError):
            self.store.facts(row["run_id"], 2, {"result_bytes": 1})
        command = self.register(registration_key="plain")
        code, error = self.api_error(
            "POST", f"/_sc/runs/{command['run_id']}/facts", {"result_bytes": 1})
        self.assertIn("require an agent run", error)
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("UPDATE run_agents SET harness='claude'")
        self.con.rollback()

    def test_result_is_read_from_the_registered_path_and_bounded(self):
        row = job._api("POST", "/_sc/runs", agent_payload("one"))
        path = f"/_sc/runs/{row['run_id']}/result"
        empty = job._api("GET", path)
        self.assertEqual(
            (empty["available"], empty["text"], empty["truncated"], empty["result_bytes"]),
            (False, "", False, None))
        self.assertEqual(empty["agent"]["harness"], "codex")
        evidence = Path(row["evidence_path"]).parent
        evidence.mkdir(parents=True)
        (evidence / "result.md").write_text("the final message\n")
        whole = job._api("GET", path)
        self.assertEqual(
            (whole["available"], whole["text"], whole["truncated"], whole["result_bytes"]),
            (True, "the final message\n", False, 18))
        (evidence / "result.md").write_text("x" * (runs.AGENT_RESULT_MAX + 500))
        long = job._api("GET", path)
        self.assertEqual(len(long["text"]), 262144)
        self.assertTrue(long["truncated"])
        self.assertEqual(long["result_bytes"], runs.AGENT_RESULT_MAX + 500)
        with self.assertRaises(PermissionError):
            self.store.result(row["run_id"], 2)
        command = self.register(registration_key="plain")
        code, error = self.api_error("GET", f"/_sc/runs/{command['run_id']}/result")
        self.assertEqual(code, 400)
        self.assertIn("only an agent run has a result", error)


if __name__ == "__main__":
    unittest.main()
